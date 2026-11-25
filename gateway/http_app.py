"""Serve MCP Streamable HTTP and the local dashboard behind one boundary."""

import asyncio
import logging
from base64 import b64decode
from binascii import Error as Base64Error
from contextlib import AsyncExitStack, asynccontextmanager
from hmac import compare_digest
from html import escape
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import urlencode

from mcp import types
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
)
from starlette.routing import Route

from .connection_diagnostics import FAILURE_LABELS, docker_recovery
from .connections import ReauthorizationError
from .credential_ui import CredentialsUI, LIVE_SETTINGS_HTML, SECURE_HTML, read_form
from .dashboard import CATALOG_PAGE_SIZE, STYLE, catalog_url, render, render_statistics
from .diagnostic_page import DiagnosticPage
from .management import Management
from .mcp_server import ToolSort, make_server, requested_sort
from .mock_test_page import MockTestPage
from .module_data import ModuleError
from .module_page import ModulePage, inactive_modules_page
from .profile import StdioBackend, mcp_path
from .router import tool_error
from .skill_tools import maintain_skill_cache
from .telemetry import LOG_RETENTION_INTERVAL_SECONDS
from .telemetry_api import TelemetryAPI
from .trace_context import with_http_trace
from .ui_locale import BrowserLocale, tr
from .ui_shell import LOGO, SECTIONS, shell
from .workspace import WORKSPACE_SCRIPT


_LOG = logging.getLogger("gateway")


_WORKSPACE_ASSETS = frozenset({"tool-catalog.js", "statistics-masonry.js", "mock-test-workspace.css", "mock-test-tabs.js",
                               "ag-controls/masonry-layout.js", "ag-controls/select.css", "ag-controls/select-indicator.css",
                               "ag-controls/primitives.css", "ag-controls/data-table.css"} | {"ag-rail/" + name for name in (
    "content-rail.js", "runtime.js", "rail-preview.js", "styles.css", "feed-rail.js",
    "feed-rail-position.js", "feed-rail-progress.js", "feed-rail-tracker.js", "feed-rail.css",
)})


def authorized(header: str, token: str, *, basic: bool, bearer: bool = True) -> bool:
    """Check the profile token using the schemes allowed for the current route."""
    kind, _, value = header.partition(" ")
    if bearer and kind.lower() == "bearer":
        return compare_digest(value.encode(), token.encode())
    if basic and kind.lower() == "basic":
        try:
            return compare_digest(b64decode(value, validate=True), f"gateway:{token}".encode())
        except (Base64Error, ValueError):
            return False
    return False


class LocalBoundary:
    """Restrict every HTTP surface to the selected loopback origin and profile token."""

    def __init__(self, app, port: int, token: str | None):
        """Wrap an ASGI app with the selected local port and optional shared token."""
        self.app = app
        self.host = f"127.0.0.1:{port}"
        self.origin = f"http://{self.host}"
        self.token = token

    async def __call__(self, scope, receive, send):
        """Check Host and Origin before auth or access to any route."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        hosts = request.headers.getlist("host")
        origins = request.headers.getlist("origin")
        if hosts != [self.host] or origins not in ([], [self.origin]):
            response = PlainTextResponse("Invalid local origin", status_code=403)
            await response(scope, receive, send)
            return
        path = request.url.path
        mcp = path.startswith(("/mcp/", "/manage/"))
        browser_action = (path in {"/credentials", "/modules"} or path.startswith(("/credentials/", "/reconnect/"))
                          or path == "/tests" or path.startswith("/tests/")
                          or (path == "/settings" and request.method == "POST"))
        header = request.headers.get("authorization", "")
        if self.token and not authorized(header, self.token, basic=not mcp, bearer=not browser_action):
            challenge = "Bearer" if mcp else 'Basic realm="ag-mcp-gateway"'
            response = PlainTextResponse("Authorization required", status_code=401,
                                         headers={"WWW-Authenticate": challenge})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def stop_maintenance(task: asyncio.Task) -> None:
    """Stop one maintenance loop owned by this lifespan."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def maintain_hooks(registry):
    """Expire short-lived capabilities and unused definitions during idle periods."""
    while True:
        try:
            registry.sweep()
        except (OSError, ModuleError):
            _LOG.warning("Hook retention sweep failed")
        await asyncio.sleep(60)


async def maintain_call_journal(journal, retention_days: int):
    """Expire call segments even while the gateway receives no requests."""
    while True:
        await asyncio.sleep(LOG_RETENTION_INTERVAL_SECONDS)
        journal.prune_expired(retention_days)


async def maintain_mock_reports(service):
    """
    Expire test reports even when operators stop submitting new runs.

    The service owns the profile-local retention policy and storage limits.
    """
    while True:
        try:
            service.prune_reports()
        except OSError:
            _LOG.warning("Mock report retention sweep failed")
        await asyncio.sleep(60)


def create_app(router, port: int, token: str | None, *, settings=None, skill_store=None, feedback=None,
               technical=None, mock_tests=None):
    """Attach MCP, status JSON and credential management to one local profile."""
    origin = f"http://127.0.0.1:{port}"
    credentials = CredentialsUI(router.connections, origin, bool(token))
    telemetry_api = TelemetryAPI(router.telemetry, technical) if technical is not None else None
    diagnostics = DiagnosticPage(telemetry_api, router.profile) if telemetry_api is not None else None
    reconnect_receipts: dict[str, str] = {}
    bridge_enabled = bool(router.profile.management_bridge and token and settings is not None)
    if not (bridge_enabled and router.profile.environment == "test" and router.profile.mock_tests_enabled):
        mock_tests = None
    management = Management(router, settings, origin, credentials.enabled,
                            skill_store if bridge_enabled else None,
                            feedback if bridge_enabled and router.profile.agent_discovery else None,
                            telemetry_api if bridge_enabled else None,
                            csrf=credentials.csrf if token else None, bridge_enabled=bridge_enabled,
                            mock_tests=mock_tests)
    modules = ModulePage(management.modules, origin, credentials.csrf) if bridge_enabled and management.modules else None
    test_page = MockTestPage(mock_tests, origin, credentials.csrf) if mock_tests is not None else None
    docker_backends = tuple(name for name, config in router.profile.backends.items()
                            if isinstance(config, StdioBackend) and config.docker_cli is not None)
    backend_status_tool = types.Tool(
        name="gateway_backend_startup_status",
        description="Check configured Docker-backed MCPs. If Docker is unavailable, returns the recovery action and startup status.",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )

    def mcp_surface(backend: str | None = None):
        """Bind one stateless MCP server to the whole profile or one configured backend."""
        async def catalog(cursor, meta):
            """List current tools without allowing a cursor to cross scopes."""
            sort = requested_sort(meta)
            if backend is None:
                published = modules.service.published_tools() if modules is not None else ()
                if docker_backends and all(tool.name != backend_status_tool.name for tool in published):
                    published = (*published, backend_status_tool)
                page = router.visible_page(cursor, published=published, sort=sort)
            else:
                page = router.backend_page(backend, cursor, sort=sort)
            if management.discovery is not None:
                management.discovery.decorate(page.tools, backend=backend)
            return page

        async def call(params):
            """Preserve request metadata and delegate to the shared policy and transport."""
            metadata = params.meta.model_dump(by_alias=True, exclude_none=True) if params.meta is not None else None
            request = server.request_context.request
            if isinstance(request, Request):
                metadata = with_http_trace(metadata, request.headers)
            arguments = params.arguments or {}
            if backend is not None:
                return await router.call_backend(backend, params.name, arguments, metadata=metadata)
            if modules is not None and params.name in modules.service.registry.state.publications:
                return await modules.service.run_published(params.name, arguments, metadata)
            if docker_backends and params.name == backend_status_tool.name:
                if arguments:
                    return tool_error("Invalid backend startup status arguments")
                statuses = []
                for name in docker_backends:
                    status = router.statuses[name]
                    statuses.append(docker_recovery(name, status) or {"backend": name, "status": status})
                return types.CallToolResult(content=[], structuredContent={"connections": statuses})
            return await router.call(params.name, arguments, metadata=metadata)

        server = make_server(catalog, call, forward_catalog_meta=True)
        return server

    servers = {"mcp_manager": mcp_surface()}
    for backend in router.profile.backends:
        servers[f"backend:{backend}"] = mcp_surface(backend)
    if bridge_enabled:
        servers["management_manager"] = management.server()

    @asynccontextmanager
    async def lifespan(_app):
        """Publish a fresh SDK manager for each ASGI lifecycle."""
        events = router.telemetry.technical
        if events is not None:
            events.emit("INFO", "service", "service.starting", "Gateway profile is starting.")
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(router.running())
            enrichment = asyncio.create_task(router.keywords.run())
            stack.push_async_callback(stop_maintenance, enrichment)
            if telemetry_api is not None:
                cleanup = asyncio.create_task(maintain_call_journal(router.telemetry.journal,
                                                                   router.profile.log_retention_days))
                stack.push_async_callback(stop_maintenance, cleanup)
            if bridge_enabled and skill_store is not None:
                maintenance = asyncio.create_task(maintain_skill_cache(skill_store))
                stack.push_async_callback(stop_maintenance, maintenance)
            if modules is not None:
                maintenance = asyncio.create_task(maintain_hooks(router.modules))
                stack.push_async_callback(stop_maintenance, maintenance)
            if test_page is not None:
                maintenance = asyncio.create_task(maintain_mock_reports(mock_tests))
                stack.push_async_callback(stop_maintenance, maintenance)
            state = {}
            for key, server in servers.items():
                manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)
                await stack.enter_async_context(manager.run())
                state[key] = manager
            if events is not None:
                events.emit("INFO", "service", "service.ready", "Gateway profile is ready.")
            try:
                yield state
            finally:
                if events is not None:
                    events.emit("INFO", "service", "service.stopping", "Gateway profile is stopping.")

    class McpEndpoint:
        """Expose a raw ASGI endpoint without Starlette request coercion."""

        def __init__(self, manager_key: str):
            """Choose the fixed lifespan manager for this endpoint."""
            self.manager_key = manager_key

        async def __call__(self, scope, receive, send):
            """Dispatch through the SDK manager owned by this request's lifespan."""
            manager = scope.get("state", {}).get(self.manager_key)
            if manager is None:
                response = PlainTextResponse("MCP gateway is unavailable", status_code=503)
                await response(scope, receive, send)
                return
            await manager.handle_request(scope, receive, send)

    def add_backend_addresses(snapshot):
        """Advertise configured loopback addresses even while a backend reconnects."""
        for item in snapshot["connections"]:
            item["mcp_url"] = origin + mcp_path(router.profile.endpoint_key, item["name"])
        return snapshot

    def dashboard(query: str = "", *, offset: int = 0, backend: str | None = None,
                  notice: str = "", notice_backend: str | None = None, status_code: int = 200,
                  section: str = "/", sort: ToolSort = "usage"):
        """Render a single catalog page and the authorized browser controls."""
        published = modules.service.published_tools() if modules is not None and section == "/tools" else ()
        snapshot = router.dashboard_snapshot(query, offset, backend, page_size=CATALOG_PAGE_SIZE,
                                             sort=sort, published=published)
        page = render(add_backend_addresses(snapshot), query, credentials=credentials.enabled,
                      reconnect_csrf=credentials.csrf if token else None, notice=notice,
                      notice_backend=notice_backend, diagnostics=diagnostics is not None, section=section,
                      protected=token is not None)
        headers = LIVE_SETTINGS_HTML if section == "/tools" else SECURE_HTML
        return HTMLResponse(page, status_code=status_code, headers=headers)

    async def tool_keywords(request):
        """Return keywords for one currently available tool within this profile."""
        name = request.query_params.get("tool", "")
        route = router.routes.get(name)
        if route is not None and name not in router.profile.denied and (route.worker is None or route.worker.live):
            tool = route.tool
        else:
            published = modules.service.published_tools() if modules is not None else ()
            tool = next((item for item in published if item.name == name), None)
        if tool is None:
            return JSONResponse({"error": "Tool is unavailable"}, status_code=404, headers=SECURE_HTML)
        return JSONResponse(router.keywords.describe(name, tool), headers=SECURE_HTML)

    def catalog_filters(request):
        """Keep bounded search and paging across a backend action."""
        value = request.query_params.get("offset", "")
        offset = int(value) if value.isascii() and value.isdecimal() and len(value) <= 6 else 0
        backend = request.query_params.get("backend")
        return {"query": request.query_params.get("q", "")[:200], "offset": offset,
                "backend": backend if backend in router.profile.backends else None,
                "sort": "name" if request.query_params.get("sort") == "name" else "usage"}

    async def overview(request):
        """Show reconnect confirmation only for a successful session replacement."""
        name = request.query_params.get("reconnected")
        receipt = request.query_params.get("confirmation")
        notice = ""
        if receipt and name in reconnect_receipts and reconnect_receipts[name] == receipt:
            notice = f"{name}: {tr('Подключение проверено и заменено.')}"
        return dashboard(**catalog_filters(request), notice=notice, notice_backend=name if notice else None,
                         section=request.url.path)

    async def statistics(request):
        """Show metrics for one configured server or the whole active profile."""
        backends = router.profile.backends
        backend = request.query_params.get("backend") or None
        invalid = backend is not None and backend not in backends
        selected = None if invalid else backend
        page = render_statistics(router.profile.endpoint_key, backends, selected,
                                 router.telemetry.statistics(selected), invalid=invalid)
        return HTMLResponse(page, status_code=400 if invalid else 200, headers=SECURE_HTML)

    async def reconnect(request):
        """Replace one selected MCP after local browser authentication and CSRF checks."""
        name = request.path_params["backend"]
        if not token or name not in router.profile.backends:
            return PlainTextResponse(tr("Не найдено"), status_code=404, headers=SECURE_HTML)
        if request.headers.get("origin") != origin:
            return PlainTextResponse(tr("Недопустимый источник формы"), status_code=403, headers=SECURE_HTML)
        try:
            fields = await read_form(request)
        except ValueError:
            fields = {}
        if set(fields) != {"csrf"} or not compare_digest(fields["csrf"].encode(), credentials.csrf.encode()):
            return PlainTextResponse(tr("Недопустимый токен формы"), status_code=403, headers=SECURE_HTML)
        filters = catalog_filters(request)
        section = "/catalog" if request.query_params.get("return_to") == "catalog" else "/"
        try:
            if await router.connections.reconnect(name):
                receipt = token_urlsafe(16)
                reconnect_receipts[name] = receipt
                previous = catalog_url(**filters, path=section)
                separator = "&" if "?" in previous else "?"
                query = urlencode({"reconnected": name, "confirmation": receipt})
                return RedirectResponse(f"{previous}{separator}{query}#backend-{name}",
                                        status_code=303, headers=SECURE_HTML)
        except ReauthorizationError as error:
            code = str(error)
        else:
            code = "connection-failed"
        if code == "busy":
            return dashboard(**filters, notice=f"{name}: {tr('переподключение уже выполняется.')}", notice_backend=name, status_code=409, section=section)
        title, _tone = FAILURE_LABELS.get(code, ("Новое подключение не подтвердилось", "error"))
        worker = router.connections.active.get(name)
        suffix = " " + tr("Действующее подключение сохранено.") if worker is not None and worker.live else ""
        return dashboard(**filters, notice=f"{name}: {tr(title)}.{suffix}", notice_backend=name, status_code=502, section=section)

    async def status(_request):
        """Return profile status and metrics as machine-readable JSON."""
        return JSONResponse(add_backend_addresses(router.snapshot()), headers={"Cache-Control": "no-store"})

    async def stylesheet(_request):
        """Serve the fixed local dashboard stylesheet."""
        return Response(STYLE, media_type="text/css", headers={"X-Content-Type-Options": "nosniff"})

    async def catalog_asset(request):
        """Serve only the allowlisted browser assets owned by the workspace."""
        asset = request.path_params["asset"]
        if asset not in _WORKSPACE_ASSETS:
            return PlainTextResponse("Not found", status_code=404)
        path = Path(__file__).with_name("assets") / asset
        media_type = "text/css" if path.suffix == ".css" else "text/javascript"
        return FileResponse(path, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})

    async def profiles(_request):
        """Direct profile operations to the local operator application."""
        key = escape(router.profile.endpoint_key)
        content = f"""<section class="ag-section-stack profile-overview" aria-labelledby="active-profile">
<div class="ag-section-stack__items"><h2 id="active-profile">{tr("Активный профиль")} <code>{key}</code></h2>
<p class="muted profile-native-copy">{tr("Выберите или создайте профиль в меню приложения.")}</p>
<p class="muted profile-browser-copy">{tr("Откройте приложение ag-mcp-gateway, чтобы создать, выбрать или запустить профиль.")}</p></div>
<a class="ag-button ag-button--secondary" href="agm-gateway://profiles">{tr("Управление профилями")}</a></section>"""
        page = shell("/profiles", router.profile.endpoint_key, tr("Профили"), content)
        return HTMLResponse(page, headers=SECURE_HTML)

    async def unavailable(request):
        """Keep disabled capabilities discoverable with their current profile context."""
        path = request.url.path
        title = next(tr(title) for route, title, _ in SECTIONS if route == path)
        content = f'<section><p>{tr("Раздел недоступен для активного профиля.")}</p></section>'
        return HTMLResponse(shell(path, router.profile.endpoint_key, title, content), headers=SECURE_HTML)

    async def module_overview(request):
        """Keep the modules section informative when this profile has no active registry."""
        if modules is None:
            return inactive_modules_page(router.profile.endpoint_key)
        return await modules.overview(request)

    async def settings_revisions(_request):
        """Return only lightweight view revisions for the current profile."""
        current = {"policy": management.browser_revision()}
        if modules is not None:
            current["modules"] = str(modules.service.registry.state.revision)
        return JSONResponse(current, headers={"Cache-Control": "no-store"})

    async def workspace_script(_request):
        """Serve the fixed local shell controller under the same-origin CSP."""
        return Response(WORKSPACE_SCRIPT, media_type="text/javascript",
                        headers={"X-Content-Type-Options": "nosniff"})

    async def logo(_request):
        """Return the fixed gateway mark as a local favicon."""
        return Response(LOGO.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1),
                        media_type="image/svg+xml", headers={"X-Content-Type-Options": "nosniff"})

    routes = [
        Route(mcp_path(router.profile.endpoint_key), McpEndpoint("mcp_manager")),
        Route("/", overview),
        Route("/catalog", overview),
        Route("/tools", overview),
        Route("/api/tools/keywords", tool_keywords),
        Route("/statistics", statistics),
        Route("/profiles", profiles),
        Route("/settings", management.overview),
        Route("/modules", module_overview),
        Route("/api/settings/revisions", settings_revisions),
        Route("/reconnect/{backend}", reconnect, methods=["POST"]),
        Route("/credentials", credentials.overview),
        Route("/credentials/{backend}", credentials.update, methods=["POST"]),
        Route(f"/api/status/{router.profile.endpoint_key}", status),
        Route("/dashboard.css", stylesheet),
        Route("/assets/{asset:path}", catalog_asset),
        Route("/workspace.js", workspace_script),
        Route("/gateway.svg", logo),
    ]
    for backend in router.profile.backends:
        routes.append(Route(mcp_path(router.profile.endpoint_key, backend), McpEndpoint(f"backend:{backend}")))
    if management.csrf is not None:
        routes.append(Route("/settings", management.submit, methods=["POST"]))
    if bridge_enabled:
        routes.append(Route(f"/manage/{router.profile.endpoint_key}", McpEndpoint("management_manager")))
    if modules is not None:
        routes.append(Route("/modules", modules.submit, methods=["POST"]))
    if test_page is not None:
        routes.extend([
            Route("/tests", test_page.overview),
            Route("/tests", test_page.submit, methods=["POST"]),
            Route("/tests/groups", test_page.save_group, methods=["POST"]),
            Route("/tests/move", test_page.move_request, methods=["POST"]),
            Route("/tests/runs/{run_id}", test_page.report),
            Route("/tests/runs/{run_id}/json", test_page.report_json),
        ])
    if telemetry_api is not None:
        routes.extend(telemetry_api.routes())
        routes.append(Route("/diagnostics", diagnostics.overview))
    if diagnostics is None:
        routes.append(Route("/diagnostics", unavailable))
    app = Starlette(routes=routes, lifespan=lifespan,
                    middleware=[Middleware(BrowserLocale, mock_tests=test_page is not None)])
    return LocalBoundary(app, port, token)
