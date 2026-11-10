"""Route tools through policy, inflight deduplication and telemetry."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic

from mcp import ClientSession, types

from .connections import BackendConnections, Connection
from .credentials import SecretStore
from .hook_lifecycle import HOOK_META
from .mcp_client import CatalogSession
from .module_data import ModuleError, json_signature, object_required
from .module_runtime import check_schema, run_local, transform_result
from .profile import HttpBackend, Profile
from .session_context import SESSION_META, SessionContext
from .telemetry import Telemetry
from .trace_context import TraceContext, without_trace_fields


def tool_error(message: str) -> types.CallToolResult:
    """Return an MCP tool failure carrying the supplied safe client-facing message."""
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)


@dataclass(frozen=True)
class Route:
    """Pair a descriptor with the exact upstream MCP session that advertised it."""

    tool: types.Tool
    session: ClientSession
    worker: Connection | None = None
    schema_generation: int = 0
    schema_signature: bytes | None = None


class Router:
    """Own catalog, upstream workers and the singleflight scope of one profile."""

    def __init__(self, profile: Profile, telemetry: Telemetry, *,
                 store: SecretStore | None = None, profile_file: str = ""):
        """Start with an empty catalog for the supplied profile and metrics sink."""
        self.profile = profile
        self.telemetry = telemetry
        self.routes: dict[str, Route] = {}
        self.catalog_revision = 0
        self.statuses = dict.fromkeys(profile.backends, "starting")
        self.pending: dict[tuple[str, bytes, int], asyncio.Task] = {}
        self.modules = None
        self.management_singleflight: set[str] = set()
        self.connections = BackendConnections(profile, self._publish, store, profile_file)

    def _publish(self, prefix: str, worker: Connection):
        """Replace only this backend's catalog with the selected generation."""
        self.statuses[prefix] = worker.status
        self.catalog_revision += 1
        old_routes = {name: route for name, route in self.routes.items() if name.startswith(prefix + ".")}
        for name in old_routes:
            del self.routes[name]
        if worker.session is not None:
            for name, tool in worker.tools.items():
                previous = old_routes.get(name)
                schema_generation = self.catalog_revision
                signature = None
                if self.modules is not None:
                    signature = json_signature([tool.inputSchema, tool.outputSchema])
                    if previous is not None and previous.schema_signature == signature:
                        schema_generation = previous.schema_generation
                self.routes[name] = Route(tool, worker.session, worker, schema_generation, signature)


    @asynccontextmanager
    async def running(self):
        """Serve discovered routes and settle shared dispatches before closing transports."""
        async with self.connections.running():
            try:
                yield
            finally:
                flights = tuple(self.pending.values())
                for flight in flights:
                    flight.cancel()
                await asyncio.gather(*flights, return_exceptions=True)

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

    def singleflight_active(self, name: str, route: Route) -> bool:
        """Require live read-only metadata for management opt-ins, even after rediscovery."""
        if name not in self.profile.singleflight:
            return False
        if name not in self.management_singleflight:
            return True
        annotations = route.tool.annotations
        return annotations is not None and annotations.readOnlyHint is True

    def snapshot(self) -> dict:
        """Describe tool availability and measured traffic without revealing profile secrets."""
        catalog = []
        for name, route in sorted(self.routes.items()):
            catalog.append({
                "name": name,
                "description": route.tool.description or "",
                "denied": name in self.profile.denied,
                "singleflight": self.singleflight_active(name, route),
            })
        return {
            "profile": self.profile.name,
            "environment": self.profile.environment,
            "backends": self.statuses.copy(),
            "connections": self.connection_catalog(catalog),
            "catalog": catalog,
            "metrics": self.telemetry.snapshot(),
        }

    def connection_catalog(self, tools: list[dict]) -> list[dict]:
        """Summarize readiness, authentication, dispatch time and tool count per backend."""
        counts = dict.fromkeys(self.profile.backends, 0)
        for tool in tools:
            if not tool["denied"]:
                prefix = tool["name"].partition(".")[0]
                if prefix in counts:
                    counts[prefix] += 1
        items = []
        for name, backend in self.profile.backends.items():
            auth = "none"
            if isinstance(backend, HttpBackend):
                if backend.basic_keychain:
                    auth = "basic"
                elif backend.bearer_token_env or backend.bearer_token_keychain:
                    auth = "bearer"
            items.append({
                "name": name,
                "transport": backend.transport,
                "auth": auth,
                "status": self.statuses[name],
                "last_call_at": self.telemetry.last_upstream_at(name),
                "tools": counts[name],
            })
        return items

    async def call(self, name: str, arguments: dict, *, metadata: dict | None = None,
                   require_read_only: bool = False) -> types.CallToolResult:
        """Check deny policy before routing, then observe one inbound tool call."""
        start = monotonic()
        denied = name in self.profile.denied
        route = self.routes.get(name)
        metric_name = name if denied or route else "(unknown)"
        outcome = "error"
        joined = False
        input_bytes = output_bytes = 0
        context = None
        hook_usage = None
        modules = self.modules
        hook_token = None
        if metadata is not None and HOOK_META in metadata:
            metadata = metadata.copy()
            hook_token = metadata.pop(HOOK_META)
            if hook_token is None:
                hook_token = ""
            metadata = metadata or None
        trace = TraceContext.from_metadata(metadata)
        metadata = without_trace_fields(metadata)
        try:
            if metadata is not None and SESSION_META in metadata:
                try:
                    context = SessionContext.parse(metadata[SESSION_META])
                except ValueError:
                    if not denied:
                        return tool_error("Invalid gateway session context")
            if denied:
                outcome = "denied"
                return tool_error("This tool is disabled by the gateway profile")
            if route is None:
                outcome = "unknown"
                return tool_error("This tool is unavailable in the gateway catalog")
            annotations = route.tool.annotations
            if require_read_only and (name not in self.profile.module_chain_tools or
                                      annotations is None or annotations.readOnlyHint is not True):
                return tool_error("Module steps require a read-only tool")
            if modules is None:
                if hook_token is not None:
                    return tool_error("Hook sessions are disabled for this profile")
                before, after = None, None
            else:
                before, after, hook_usage = modules.for_tool(name, route, hook_token)
            if before is not None:
                arguments = object_required(run_local(before, arguments))
            encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8")
            input_bytes = len(encoded)
            if metadata is None and trace is None and self.singleflight_active(name, route):
                if before is not None:
                    check_schema(arguments, route.tool.inputSchema)
                task, joined = self._shared(name, encoded, route, arguments)
                result = await asyncio.shield(task)
            else:
                try:
                    arguments, forwarded = self._contextual_call(name, route, arguments, metadata, context, trace)
                except ValueError:
                    return tool_error("Configured session parameter is absent from the upstream schema")
                if before is not None:
                    check_schema(arguments, route.tool.inputSchema)
                result = await self._dispatch(name, route, arguments, forwarded)
            if after is not None and not result.isError:
                try:
                    result = transform_result(after, result, route.tool.outputSchema)
                except ModuleError:
                    return tool_error("Response hook failed after the upstream call completed")
            if hook_usage is not None and not result.isError:
                modules.succeeded(hook_usage)
            outcome = "error" if result.isError else "ok"
            output_bytes = len(result.model_dump_json(by_alias=True, exclude_none=True).encode())
            return result
        except ModuleError as error:
            return tool_error(str(error))
        except ValueError:
            return tool_error("Arguments must be valid JSON")
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            if hook_usage is not None:
                modules.release(hook_usage)
            elapsed_ms = (monotonic() - start) * 1000
            session_tag = context.tag if context else None
            self.telemetry.completed(metric_name, outcome, elapsed_ms, joined, input_bytes, output_bytes,
                                     session_tag=session_tag, trace_id=trace.trace_id if trace else None)

    def _contextual_call(self, name, route, arguments, metadata, context, trace):
        """Apply the selected backend's forwarding rules to request metadata and identity."""
        backend = self.profile.backends[name.partition(".")[0]]
        # Progress tokens belong to the inbound session. Gateway identity requires local opt-in.
        forwarded = {key: value for key, value in (metadata or {}).items()
                     if key not in {"progressToken", SESSION_META}}
        if trace is not None and backend.forward_trace_context:
            forwarded.update(trace.carrier())
        if context is not None:
            if backend.forward_session_context:
                forwarded[SESSION_META] = context.values
            mapping = backend.session_args.get(route.tool.name)
            if mapping:
                arguments = context.bind(arguments, mapping, route.tool.inputSchema)
        return arguments, forwarded or None

    def _shared(self, name, encoded, route, arguments):
        """Join only calls on the same published route and tool definition."""
        key = (name, encoded, id(route))
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
        worker = route.worker
        if worker is not None:
            if self.routes.get(name) is not route or not worker.live:
                return tool_error("Upstream connection changed before dispatch")
            worker.acquire()
        self.telemetry.upstream_started(name)
        try:
            async with asyncio.timeout(self.profile.timeout_seconds):
                if isinstance(route.session, CatalogSession):
                    return await route.session.call_published_tool(route.tool, arguments, metadata)
                return await route.session.call_tool(route.tool.name, arguments, meta=metadata)
        except TimeoutError:
            return tool_error("Upstream call timed out")
        except Exception:
            return tool_error("Upstream transport failed")
        finally:
            if worker is not None:
                worker.release()
