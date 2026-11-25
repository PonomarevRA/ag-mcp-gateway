"""Route tools through policy, inflight deduplication and telemetry."""

import asyncio
import json
from bisect import bisect_left
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import cached_property
from hashlib import blake2b
from time import monotonic

from mcp import ClientSession, McpError, types

from .connection_diagnostics import docker_recovery
from .connections import BackendConnections, Connection
from .credentials import SecretStore
from .hook_lifecycle import HOOK_META
from .mcp_client import CatalogSession, PublishedOutputError
from .mcp_server import ToolSort, catalog_window
from .module_data import ModuleError, json_signature, object_required
from .module_runtime import check_schema, run_local, transform_result
from .profile import HttpBackend, Profile
from .schema_checks import SchemaChecks
from .session_context import BRIDGE_SESSION_META, SESSION_META, TASK_META, session_attribution, task_tag
from .telemetry import Telemetry
from .tool_keywords import ToolKeywords
from .trace_context import TraceContext, without_trace_fields


def tool_error(message: str, *, details: dict | None = None) -> types.CallToolResult:
    """Return an MCP tool failure carrying the supplied safe client-facing message."""
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)],
                                structuredContent=details, isError=True)


@dataclass(frozen=True)
class Route:
    """Pair a descriptor with the exact upstream MCP session that advertised it."""

    tool: types.Tool
    session: ClientSession
    worker: Connection | None = None
    schema_generation: int = 0
    schema_signature: bytes | None = None


@dataclass
class FlightResult:
    """Hold the shared response and lazily size it for callers using its original JSON."""

    result: types.CallToolResult
    error_code: str | None = None

    @classmethod
    def failed(cls, message: str, error_code: str):
        """Preserve the client-facing error and a separate gateway-owned code."""
        return cls(tool_error(message), error_code)

    @cached_property
    def output_bytes(self) -> int:
        """Serialize an unchanged shared response once for the journal byte estimate."""
        return len(self.result.model_dump_json(by_alias=True, exclude_none=True).encode())


class Router:
    """Own catalog, upstream workers and the singleflight scope of one profile."""

    def __init__(self, profile: Profile, telemetry: Telemetry, *,
                 store: SecretStore | None = None, profile_file: str = ""):
        """Start with an empty catalog for the supplied profile and metrics sink."""
        self.profile = profile
        self.telemetry = telemetry
        self.keywords = ToolKeywords()
        self.telemetry.register_backends(profile.backends)
        self.routes: dict[str, Route] = {}
        self.catalog_revision = 0
        self._visible_revision = -1
        self._visible_names: tuple[str, ...] = ()
        self._ordered_names: tuple[str, ...] = ()
        self._available_counts: dict[str, int] = {}
        self._usage_pages: OrderedDict[str, tuple[str, tuple[str, ...]]] = OrderedDict()
        self.statuses = dict.fromkeys(profile.backends, "starting")
        self.pending: dict[tuple[str, bytes, int], asyncio.Task[FlightResult]] = {}
        self.schema_checks = SchemaChecks()
        self.modules = None
        self.management_singleflight: set[str] = set()
        self.connections = BackendConnections(profile, self._publish, store, profile_file,
                                              technical=telemetry.technical)

    def _publish(self, prefix: str, worker: Connection):
        """Replace only this backend's catalog with the selected generation."""
        previous_status = self.statuses[prefix]
        self.statuses[prefix] = worker.status
        if self.telemetry.technical is not None and worker.status not in {previous_status, "starting"}:
            self.telemetry.technical.connection(prefix, worker.status)
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
        publications = self.modules.state.publications if self.modules is not None else ()
        self.telemetry.prune_tools(self.routes, self.profile.denied, publications=publications)


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

    def _catalog_names(self) -> tuple[str, ...]:
        """Cache visibility and ordering for one publication or policy revision."""
        if self._visible_revision != self.catalog_revision:
            self._usage_pages.clear()
            self._ordered_names = tuple(sorted(self.routes))
            self._visible_names = tuple(name for name in self._ordered_names if name not in self.profile.denied)
            counts = dict.fromkeys(self.profile.backends, 0)
            for name in self._visible_names:
                prefix = name.partition(".")[0]
                if prefix in counts:
                    counts[prefix] += 1
            self._available_counts = counts
            self._visible_revision = self.catalog_revision
        return self._visible_names

    def _visible_descriptor(self, name: str, *, original_name: bool = False) -> types.Tool:
        """Create a public descriptor using the name expected by its catalog scope."""
        tool = self.routes[name].tool
        return types.Tool(name=tool.name if original_name else name, title=tool.title, description=tool.description,
                          inputSchema=tool.inputSchema, outputSchema=tool.outputSchema, annotations=tool.annotations)

    def visible_tools(self) -> list[types.Tool]:
        """Return all public descriptors for callers that explicitly need the complete list."""
        return [self._visible_descriptor(name) for name in self._catalog_names()]

    def _docker_catalog_meta(self, backend: str | None = None) -> dict | None:
        """Report blocked dependencies even when the selected catalog is empty."""
        names = self.statuses if backend is None else (backend,)
        blocked = []
        for name in names:
            recovery = docker_recovery(name, self.statuses[name])
            if recovery is not None:
                blocked.append(recovery)
        if blocked:
            return {"ag-mcp-gateway/backend-recovery": blocked}
        return None

    def _list_window(self, names: tuple[str, ...], cursor: str | None, sort: ToolSort | None,
                     scope: str) -> tuple[tuple[str, ...], str | None]:
        """Freeze usage order across pages while new calls update the live counters."""
        if cursor is not None and cursor.startswith("usage:"):
            snapshot_id = cursor.split(":", 2)[1]
            saved = self._usage_pages.get(snapshot_id)
            if sort == "name" or saved is None or saved[0] != scope:
                raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="Invalid catalog cursor"))
            names = saved[1]
        elif cursor is not None or sort == "name":
            if sort == "usage":
                raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="Invalid catalog cursor"))
            window, next_cursor = catalog_window(len(names), cursor, self.catalog_revision, scope=f"name:{scope}")
            return names[window], next_cursor
        else:
            # Stable sorting preserves the base order for tools with equal usage.
            names = tuple(sorted(names, key=lambda name: -self.telemetry.tool_uses(name)))
            # Reuse snapshots for repeated first-page requests with identical ordering.
            ordering = "\0".join((scope, *names))
            snapshot_id = blake2b(ordering.encode(), digest_size=12).hexdigest()

        window, next_cursor = catalog_window(len(names), cursor, self.catalog_revision, scope=f"usage:{snapshot_id}")
        if cursor is None and next_cursor is not None:
            # Retain a bounded number of distinct orders per profile.
            self._usage_pages[snapshot_id] = (scope, names)
        if snapshot_id in self._usage_pages:
            self._usage_pages.move_to_end(snapshot_id)
            if len(self._usage_pages) > 64:
                self._usage_pages.popitem(last=False)
        return names[window], next_cursor

    def visible_page(self, cursor: str | None, *, published: tuple[types.Tool, ...] = (),
                     sort: ToolSort | None = None) -> types.ListToolsResult:
        """Rank published modules and allowed upstream tools in the profile catalog."""
        names = self._catalog_names()
        modules = {tool.name: tool for tool in published}
        names = tuple(modules) + names
        if sort == "name" or (cursor is not None and cursor.startswith("name:")):
            names = tuple(sorted(names))
        names, next_cursor = self._list_window(names, cursor, sort, self.profile.endpoint_key)
        tools = [modules[name] if name in modules else self._visible_descriptor(name) for name in names]
        return types.ListToolsResult(tools=tools, nextCursor=next_cursor, _meta=self._docker_catalog_meta())

    def backend_page(self, backend: str, cursor: str | None, *, sort: ToolSort | None = None) -> types.ListToolsResult:
        """List allowed tools within one backend using their original MCP names."""
        if backend not in self.profile.backends:
            raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="Unknown MCP backend"))
        names = self._catalog_names()
        # '/' immediately follows the '.' namespace separator in the sorted names.
        start = bisect_left(names, f"{backend}.")
        end = bisect_left(names, f"{backend}/", lo=start)
        names, next_cursor = self._list_window(names[start:end], cursor, sort, f"{self.profile.endpoint_key}/{backend}")
        tools = [self._visible_descriptor(name, original_name=True) for name in names]
        return types.ListToolsResult(tools=tools, nextCursor=next_cursor, _meta=self._docker_catalog_meta(backend))

    async def call_backend(self, backend: str, name: str, arguments: dict, *,
                           metadata: dict | None = None) -> types.CallToolResult:
        """Dispatch the original tool name only inside its configured backend namespace."""
        if backend not in self.profile.backends:
            return tool_error("Unknown MCP backend")
        return await self.call(f"{backend}.{name}", arguments, metadata=metadata)

    def singleflight_active(self, name: str, route: Route) -> bool:
        """Require live read-only metadata for management opt-ins, even after rediscovery."""
        if name not in self.profile.singleflight:
            return False
        if name not in self.management_singleflight:
            return True
        annotations = route.tool.annotations
        return annotations is not None and annotations.readOnlyHint is True

    def snapshot(self) -> dict:
        """Describe the complete catalog for machine-readable status callers."""
        self._catalog_names()
        return self._catalog_snapshot(self._ordered_names, self.telemetry.snapshot())

    def dashboard_snapshot(self, query: str, offset: int, backend: str | None, *, page_size: int,
                           sort: ToolSort = "usage", published: tuple[types.Tool, ...] = ()) -> dict:
        """Filter the full catalog and materialize only the requested browser page."""
        self._catalog_names()
        term = query.casefold()
        modules = {tool.name: tool for tool in published} if backend is None else {}
        names = tuple(modules) + self._ordered_names
        matches = []
        for name in names:
            prefix = name.partition(".")[0]
            if backend is not None and prefix != backend:
                continue
            if term and term not in name.casefold():
                tool = modules[name] if name in modules else self.routes[name].tool
                if term not in (tool.description or "").casefold():
                    continue
            matches.append(name)
        if sort == "usage":
            matches.sort(key=lambda name: -self.telemetry.tool_uses(name))
        else:
            matches.sort()
        total = len(matches)
        last_page = max(0, total - 1) // page_size * page_size
        offset = min(offset // page_size * page_size, last_page)
        page = matches[offset:offset + page_size]
        result = self._catalog_snapshot(page, self.telemetry.snapshot(page), published=modules)
        result["page"] = {"total": total, "offset": offset, "size": page_size, "backend": backend, "sort": sort}
        return result

    def _catalog_snapshot(self, names, metrics: dict, *, published: dict[str, types.Tool] | None = None) -> dict:
        """Assemble current catalog policy and connection metadata for the selected names."""
        catalog = []
        for name in names:
            if published is not None and name in published:
                catalog.append({"name": name, "description": published[name].description or "",
                                "denied": False, "singleflight": False})
                continue
            route = self.routes[name]
            catalog.append({"name": name, "description": route.tool.description or "",
                            "denied": name in self.profile.denied,
                            "singleflight": self.singleflight_active(name, route)})
        return {
            "profile": self.profile.name,
            "environment": self.profile.environment,
            "backends": self.statuses.copy(),
            "connections": self.connection_catalog(),
            "catalog": catalog,
            "metrics": metrics,
        }

    def connection_catalog(self) -> list[dict]:
        """Summarize readiness, authentication, dispatch time and tool count per backend."""
        counts = self._available_counts
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

    def allows_module_read(self, name: str) -> bool:
        """Check the profile allowlist against the current upstream descriptor and deny policy."""
        route = self.routes.get(name)
        annotations = route.tool.annotations if route is not None else None
        return (name not in self.profile.denied and name in self.profile.module_chain_tools
                and annotations is not None and annotations.readOnlyHint is True)

    async def call(self, name: str, arguments: dict, *, metadata: dict | None = None,
                   require_read_only: bool = False, mock_trace: dict | None = None) -> types.CallToolResult:
        """
        Check deny policy, apply configured hooks and dispatch an inbound tool call.

        An internal mock caller may supply a fresh dictionary for stage outcomes.
        The trace records stage states and a change flag without copying payloads.
        """
        start = monotonic()
        if mock_trace is not None:
            mock_trace.update(before_hook="skipped", arguments_changed=False, dispatched=False,
                              output_schema="skipped", after_hook="skipped")
        denied = name in self.profile.denied
        route = self.routes.get(name)
        metric_name = name if denied or route else "(unknown)"
        outcome = "error"
        error_code = "internal_error"
        joined = False
        input_bytes = output_bytes = 0
        context = None
        session_tag = workflow_tag = None
        hook_usage = None
        flight = None
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
            if metadata is not None:
                try:
                    context, session_tag = session_attribution(metadata)
                except ValueError:
                    if not denied:
                        error_code = "invalid_session"
                        return tool_error("Invalid gateway session context")
                if TASK_META in metadata:
                    try:
                        workflow_tag = task_tag(metadata[TASK_META])
                    except ValueError:
                        if not denied:
                            error_code = "invalid_task"
                            return tool_error("Invalid gateway task ID")
            if denied:
                outcome = "denied"
                error_code = "policy_denied"
                return tool_error("This tool is disabled by the gateway profile")
            if route is None:
                backend = name.partition(".")[0]
                recovery = docker_recovery(backend, self.statuses.get(backend, ""))
                if recovery is not None:
                    error_code = recovery["status"]
                    return tool_error(recovery["message"], details=recovery)
                outcome = "unknown"
                error_code = "unknown_tool"
                return tool_error("This tool is unavailable in the gateway catalog")
            if require_read_only and not self.allows_module_read(name):
                error_code = "read_only_required"
                return tool_error("Module steps require a read-only tool")
            if modules is None:
                if hook_token is not None:
                    error_code = "hooks_disabled"
                    return tool_error("Hook sessions are disabled for this profile")
                before, after = None, None
            else:
                before, after, hook_usage = modules.for_tool(name, route, hook_token)
            if before is not None:
                if mock_trace is not None:
                    mock_trace["before_hook"] = "failed"
                    original_arguments = arguments
                arguments = object_required(run_local(before, arguments))
                if mock_trace is not None:
                    mock_trace["arguments_changed"] = json_signature(arguments) != json_signature(original_arguments)
            encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8")
            input_bytes = len(encoded)
            use_singleflight = metadata is None and trace is None and self.singleflight_active(name, route)
            forwarded = None
            if not use_singleflight:
                try:
                    arguments, forwarded = self._contextual_call(name, route, arguments, metadata, context, trace)
                except ValueError:
                    error_code = "invalid_session_mapping"
                    return tool_error("Configured session parameter is absent from the upstream schema")
            if before is not None:
                check_schema(arguments, route.tool.inputSchema, self.schema_checks)
            if mock_trace is not None:
                mock_trace["before_hook"] = "applied" if before is not None else "skipped"
                mock_trace["dispatched"] = True
            if use_singleflight:
                task, joined = self._shared(name, encoded, route, arguments)
                flight = await asyncio.shield(task)
                dispatch = flight
            else:
                dispatch = await self._dispatch(name, route, arguments, forwarded)
            result = dispatch.result
            if mock_trace is not None and isinstance(route.session, CatalogSession) and route.tool.outputSchema is not None:
                if dispatch.error_code == "invalid_upstream_output":
                    mock_trace["output_schema"] = "failed"
                elif not result.isError:
                    mock_trace["output_schema"] = "passed"
            if after is not None and not result.isError:
                try:
                    result = transform_result(after, result, route.tool.outputSchema, self.schema_checks)
                except ModuleError:
                    if mock_trace is not None:
                        mock_trace["after_hook"] = "failed"
                    error_code = "response_hook_failed"
                    return tool_error("Response hook failed after the upstream call completed")
                if mock_trace is not None:
                    mock_trace["after_hook"] = "applied"
            if hook_usage is not None and not result.isError:
                modules.succeeded(hook_usage)
            if flight is not None and result is flight.result:
                output_bytes = flight.output_bytes
            else:
                output_bytes = len(result.model_dump_json(by_alias=True, exclude_none=True).encode())
            outcome = "error" if result.isError else "ok"
            if outcome == "ok" and not joined:
                self.keywords.observe(name, arguments, result.structuredContent)
            error_code = (dispatch.error_code or "upstream_tool_error") if result.isError else None
            return result
        except ModuleError as error:
            error_code = "request_hook_failed"
            return tool_error(str(error))
        except ValueError:
            error_code = "invalid_arguments"
            return tool_error("Arguments must be valid JSON")
        except asyncio.CancelledError:
            outcome = "cancelled"
            error_code = None
            raise
        finally:
            if mock_trace is not None:
                mock_trace["error_code"] = error_code
            if hook_usage is not None:
                modules.release(hook_usage)
            elapsed_ms = (monotonic() - start) * 1000
            self.telemetry.completed(metric_name, outcome, elapsed_ms, joined, input_bytes, output_bytes,
                                     session_tag=session_tag, task_tag=workflow_tag, trace_id=trace.trace_id if trace else None,
                                     error_code=error_code, module_step=require_read_only,
                                     track_tool=metric_name == "(unknown)" or metric_name in self.routes
                                     or metric_name in self.profile.denied)

    def _contextual_call(self, name, route, arguments, metadata, context, trace):
        """Apply the selected backend's forwarding rules to request metadata and identity."""
        backend = self.profile.backends[name.partition(".")[0]]
        # Progress tokens belong to the inbound session. Gateway identity requires local opt-in.
        forwarded = {key: value for key, value in (metadata or {}).items()
                     if key not in {"progressToken", SESSION_META, BRIDGE_SESSION_META, TASK_META}}
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

    async def _dispatch(self, name, route, arguments, metadata: dict | None = None) -> FlightResult:
        """Forward to upstream once and convert transport faults to safe tool errors."""
        worker = route.worker
        if worker is not None:
            if self.routes.get(name) is not route or not worker.live:
                return FlightResult.failed("Upstream connection changed before dispatch", "connection_changed")
            worker.acquire()
        self.telemetry.upstream_started(name)
        try:
            async with asyncio.timeout(self.profile.timeout_seconds):
                if isinstance(route.session, CatalogSession):
                    result = await route.session.call_published_tool(route.tool, arguments, metadata, self.schema_checks)
                else:
                    result = await route.session.call_tool(route.tool.name, arguments, meta=metadata)
            return FlightResult(result)
        except TimeoutError:
            return FlightResult.failed("Upstream call timed out", "upstream_timeout")
        except PublishedOutputError:
            return FlightResult.failed("Upstream tool output failed schema validation", "invalid_upstream_output")
        except Exception:
            return FlightResult.failed("Upstream transport failed", "upstream_transport")
        finally:
            if worker is not None:
                worker.release()
