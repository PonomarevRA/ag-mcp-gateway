"""Route tools through policy, inflight deduplication and telemetry."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic

from mcp import ClientSession, types

from .profile import Profile
from .telemetry import Telemetry
from .upstream import run_backend


def tool_error(message: str) -> types.CallToolResult:
    """Return an MCP tool failure carrying the supplied safe client-facing message."""
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)


@dataclass(frozen=True)
class Route:
    """Pair a descriptor with the exact upstream MCP session that advertised it."""

    tool: types.Tool
    session: ClientSession


class Router:
    """Own catalog, upstream workers and the singleflight scope of one profile."""

    def __init__(self, profile: Profile, telemetry: Telemetry):
        """Start with an empty catalog for the supplied profile and metrics sink."""
        self.profile = profile
        self.telemetry = telemetry
        self.routes: dict[str, Route] = {}
        self.catalog_revision = 0
        self.statuses = dict.fromkeys(profile.backends, "starting")
        self.pending: dict[tuple[str, bytes, int], asyncio.Task] = {}

    @asynccontextmanager
    async def running(self):
        """Discover all backends, keeping failures isolated, and drain tasks on shutdown."""
        started = {name: asyncio.Event() for name in self.profile.backends}

        def publish(prefix, session, tools, status):
            """Replace this backend's routes atomically and settle its readiness."""
            self.statuses[prefix] = status
            self.catalog_revision += 1
            old_names = [name for name in self.routes if name.startswith(prefix + ".")]
            for name in old_names:
                del self.routes[name]
            if session is not None:
                for name, tool in tools.items():
                    self.routes[name] = Route(tool, session)
            started[prefix].set()

        workers = [
            asyncio.create_task(run_backend(name, backend, self.profile.timeout_seconds, publish))
            for name, backend in self.profile.backends.items()
        ]
        try:
            await asyncio.gather(*(event.wait() for event in started.values()))
            yield
        finally:
            flights = tuple(self.pending.values())
            for flight in flights:
                flight.cancel()
            await asyncio.gather(*flights, return_exceptions=True)
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    def visible_tools(self) -> list[types.Tool]:
        """Expose accessible tools under stable names without upstream UI or task metadata."""
        items = []
        for name, route in sorted(self.routes.items()):
            if name in self.profile.denied:
                continue
            tool = route.tool
            items.append(types.Tool(
                name=name, title=tool.title, description=tool.description,
                inputSchema=tool.inputSchema, outputSchema=tool.outputSchema,
                annotations=tool.annotations,
            ))
        return items

    def snapshot(self) -> dict:
        """Describe tool availability and measured traffic without revealing profile secrets."""
        catalog = []
        for name, route in sorted(self.routes.items()):
            catalog.append({
                "name": name,
                "description": route.tool.description or "",
                "denied": name in self.profile.denied,
                "singleflight": name in self.profile.singleflight,
            })
        return {
            "profile": self.profile.name,
            "backends": self.statuses.copy(),
            "catalog": catalog,
            "metrics": self.telemetry.snapshot(),
        }

    async def call(self, name: str, arguments: dict, *, metadata: dict | None = None) -> types.CallToolResult:
        """Check deny policy before routing, then observe one inbound tool call."""
        start = monotonic()
        denied = name in self.profile.denied
        route = self.routes.get(name)
        metric_name = name if denied or route else "(unknown)"
        outcome = "error"
        joined = False
        input_bytes = output_bytes = 0
        try:
            if denied:
                outcome = "denied"
                return tool_error("This tool is disabled by the gateway profile")
            if route is None:
                outcome = "unknown"
                return tool_error("This tool is unavailable in the gateway catalog")
            encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8")
            input_bytes = len(encoded)
            if metadata is None and name in self.profile.singleflight:
                task, joined = self._shared(name, encoded, route, arguments)
                result = await asyncio.shield(task)
            else:
                # Progress tokens belong to the inbound session. No progress stream is relayed.
                forwarded = {key: value for key, value in (metadata or {}).items() if key != "progressToken"}
                result = await self._dispatch(name, route, arguments, forwarded or None)
            outcome = "error" if result.isError else "ok"
            output_bytes = len(result.model_dump_json(by_alias=True, exclude_none=True).encode())
            return result
        except ValueError:
            return tool_error("Arguments must be valid JSON")
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            elapsed_ms = (monotonic() - start) * 1000
            self.telemetry.completed(metric_name, outcome, elapsed_ms, joined, input_bytes, output_bytes)

    def _shared(self, name, encoded, route, arguments):
        """Join one flight in this session, or start a flight with no completed-result cache."""
        key = (name, encoded, id(route.session))
        existing = self.pending.get(key)
        if existing is not None:
            return existing, True

        async def run():
            """Forget the flight after success, failure or shutdown cancellation."""
            try:
                return await self._dispatch(name, route, arguments)
            finally:
                self.pending.pop(key, None)

        task = asyncio.create_task(run())
        self.pending[key] = task
        return task, False

    async def _dispatch(self, name, route, arguments, metadata: dict | None = None) -> types.CallToolResult:
        """Forward to upstream once and convert transport faults to safe tool errors."""
        self.telemetry.upstream_started(name)
        try:
            async with asyncio.timeout(self.profile.timeout_seconds):
                return await route.session.call_tool(route.tool.name, arguments, meta=metadata)
        except TimeoutError:
            return tool_error("Upstream call timed out")
        except Exception:
            return tool_error("Upstream transport failed")
