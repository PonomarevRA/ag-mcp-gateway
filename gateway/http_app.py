"""Serve MCP Streamable HTTP and the local dashboard behind one boundary."""

import asyncio
import logging
from base64 import b64decode
from binascii import Error as Base64Error
from contextlib import AsyncExitStack, asynccontextmanager
from hmac import compare_digest
from secrets import token_urlsafe
from urllib.parse import urlencode

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .connection_diagnostics import FAILURE_LABELS
from .connections import ReauthorizationError
from .credential_ui import CredentialsUI, SECURE_HTML, read_form
from .dashboard import CATALOG_PAGE_SIZE, STYLE, catalog_url, render
from .diagnostic_page import DiagnosticPage
from .management import Management
from .mcp_server import make_server
from .module_data import ModuleError
from .module_page import ModulePage
from .skill_tools import maintain_skill_cache
from .telemetry import LOG_RETENTION_INTERVAL_SECONDS
from .telemetry_api import TelemetryAPI
from .trace_context import with_http_trace
from .ui_locale import BrowserLocale, tr


_LOG = logging.getLogger("gateway")


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


def create_app(router, port: int, token: str | None, *, settings=None, skill_store=None, feedback=None, technical=None):
    """Attach MCP, status JSON and credential management to one local profile."""
    origin = f"http://127.0.0.1:{port}"
    credentials = CredentialsUI(router.connections, origin, bool(token))
    management = None
    telemetry_api = TelemetryAPI(router.telemetry, technical) if technical is not None else None
    diagnostics = DiagnosticPage(telemetry_api, router.profile) if telemetry_api is not None else None
    reconnect_receipts: dict[str, str] = {}
    if router.profile.management_bridge and token and settings is not None:
        management = Management(router, settings, origin, credentials.enabled, skill_store,
                                feedback if router.profile.agent_discovery else None, telemetry_api, csrf=credentials.csrf)

    modules = ModulePage(management.modules, origin, credentials.csrf) if management and management.modules else None

    async def catalog(cursor):
        """List one page of the currently visible gateway catalog."""
        page = router.visible_page(cursor)
        if management is not None and management.discovery is not None:
            management.discovery.decorate(page.tools)
        return page

    async def call(params):
        """Pass per-request HTTP trace and MCP metadata to the router."""
        metadata = params.meta.model_dump(by_alias=True, exclude_none=True) if params.meta is not None else None
        request = mcp.request_context.request
        if isinstance(request, Request):
            metadata = with_http_trace(metadata, request.headers)
        return await router.call(params.name, params.arguments or {}, metadata=metadata)

    mcp = make_server(catalog, call)
    servers = {"mcp_manager": mcp}
    if management is not None:
        servers["management_manager"] = management.server()

    @asynccontextmanager
    async def lifespan(_app):
        """Publish a fresh SDK manager for each ASGI lifecycle."""
        events = router.telemetry.technical
        if events is not None:
            events.emit("INFO", "service", "service.starting", "Gateway profile is starting.")
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(router.running())
            if telemetry_api is not None:
                cleanup = asyncio.create_task(maintain_call_journal(router.telemetry.journal,
                                                                   router.profile.log_retention_days))
                stack.push_async_callback(stop_maintenance, cleanup)
            if management is not None and skill_store is not None:
                maintenance = asyncio.create_task(maintain_skill_cache(skill_store))
                stack.push_async_callback(stop_maintenance, maintenance)
            if modules is not None:
                maintenance = asyncio.create_task(maintain_hooks(router.modules))
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

    def dashboard(query: str = "", *, offset: int = 0, backend: str | None = None,
                  notice: str = "", status_code: int = 200):
        """Render a single catalog page and the authorized browser controls."""
        snapshot = router.dashboard_snapshot(query, offset, backend, page_size=CATALOG_PAGE_SIZE)
        page = render(snapshot, query, credentials=credentials.enabled, management=management is not None,
                      reconnect_csrf=credentials.csrf if token else None, notice=notice, modules=modules is not None,
                      diagnostics=diagnostics is not None)
        return HTMLResponse(page, status_code=status_code, headers=SECURE_HTML)

    def catalog_filters(request):
        """Keep bounded search and paging across a backend action."""
        value = request.query_params.get("offset", "")
        offset = int(value) if value.isascii() and value.isdecimal() and len(value) <= 6 else 0
        backend = request.query_params.get("backend")
        return {"query": request.query_params.get("q", "")[:200], "offset": offset,
                "backend": backend if backend in router.profile.backends else None}

    async def overview(request):
        """Show reconnect confirmation only for a successful session replacement."""
        name = request.query_params.get("reconnected")
        receipt = request.query_params.get("confirmation")
        notice = ""
        if receipt and name in reconnect_receipts and reconnect_receipts[name] == receipt:
            notice = f"{name}: {tr('Подключение проверено и заменено.')}"
        return dashboard(**catalog_filters(request), notice=notice)

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
        try:
            if await router.connections.reconnect(name):
                receipt = token_urlsafe(16)
                reconnect_receipts[name] = receipt
                previous = catalog_url(**filters)
                separator = "&" if "?" in previous else "?"
                query = urlencode({"reconnected": name, "confirmation": receipt})
                return RedirectResponse(f"{previous}{separator}{query}#backend-{name}",
                                        status_code=303, headers=SECURE_HTML)
        except ReauthorizationError as error:
            code = str(error)
        else:
            code = "connection-failed"
        if code == "busy":
            return dashboard(**filters, notice=f"{name}: {tr('переподключение уже выполняется.')}", status_code=409)
        title, _tone = FAILURE_LABELS.get(code, ("Новое подключение не подтвердилось", "error"))
        worker = router.connections.active.get(name)
        suffix = " " + tr("Действующее подключение сохранено.") if worker is not None and worker.live else ""
        return dashboard(**filters, notice=f"{name}: {tr(title)}.{suffix}", status_code=502)

    async def status(_request):
        """Return profile status and metrics as machine-readable JSON."""
        return JSONResponse(router.snapshot(), headers={"Cache-Control": "no-store"})

    async def stylesheet(_request):
        """Serve the fixed local dashboard stylesheet."""
        return Response(STYLE, media_type="text/css", headers={"X-Content-Type-Options": "nosniff"})

    routes = [
        Route(f"/mcp/{router.profile.endpoint_key}", McpEndpoint("mcp_manager")),
        Route("/", overview),
        Route("/reconnect/{backend}", reconnect, methods=["POST"]),
        Route("/credentials", credentials.overview),
        Route("/credentials/{backend}", credentials.update, methods=["POST"]),
        Route(f"/api/status/{router.profile.endpoint_key}", status),
        Route("/dashboard.css", stylesheet),
    ]
    if management is not None:
        routes.extend([Route("/settings", management.overview),
                       Route("/settings", management.submit, methods=["POST"]),
                       Route(f"/manage/{router.profile.endpoint_key}", McpEndpoint("management_manager"))])
    if modules is not None:
        routes.extend([Route("/modules", modules.overview), Route("/modules", modules.submit, methods=["POST"])])
    if telemetry_api is not None:
        routes.extend(telemetry_api.routes())
        routes.append(Route("/diagnostics", diagnostics.overview))
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(BrowserLocale)])
    return LocalBoundary(app, port, token)
