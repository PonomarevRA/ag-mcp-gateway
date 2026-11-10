"""Serve MCP Streamable HTTP and the local dashboard behind one boundary."""

import asyncio
import logging
from base64 import b64decode
from binascii import Error as Base64Error
from contextlib import AsyncExitStack, asynccontextmanager
from hmac import compare_digest

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .connection_diagnostics import FAILURE_LABELS
from .connections import ReauthorizationError
from .credential_ui import CredentialsUI, SECURE_HTML, read_form
from .dashboard import STYLE, render
from .management import Management
from .mcp_server import catalog_page, make_server
from .module_data import ModuleError
from .module_page import ModulePage
from .skill_tools import maintain_skill_cache
from .trace_context import with_http_trace


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
        browser_action = path == "/credentials" or path.startswith(("/credentials/", "/reconnect/")) or path == "/modules"
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


def create_app(router, port: int, token: str | None, *, settings=None, skill_store=None, feedback=None):
    """Attach MCP, status JSON and credential management to one local profile."""
    origin = f"http://127.0.0.1:{port}"
    credentials = CredentialsUI(router.connections, origin, bool(token))
    management = None
    if router.profile.management_bridge and token and settings is not None:
        management = Management(router, settings, origin, credentials.enabled, skill_store,
                                feedback if router.profile.agent_discovery else None)

    modules = ModulePage(management.modules, origin, credentials.csrf) if management and management.modules else None

    async def catalog(cursor):
        """List one page of the currently visible gateway catalog."""
        page = catalog_page(router.visible_tools(), cursor, router.catalog_revision)
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
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(router.running())
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
            yield state

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

    def dashboard(query: str = "", *, notice: str = "", status_code: int = 200):
        """Render one current, sanitized view and the authorized browser controls."""
        page = render(router.snapshot(), query, credentials=credentials.enabled, management=management is not None,
                      reconnect_csrf=credentials.csrf if token else None, notice=notice, modules=modules is not None)
        return HTMLResponse(page, status_code=status_code, headers=SECURE_HTML)

    async def overview(request):
        """Render the catalog with a bounded search filter."""
        return dashboard(request.query_params.get("q", "")[:200])

    async def reconnect(request):
        """Replace one selected MCP after local browser authentication and CSRF checks."""
        name = request.path_params["backend"]
        if not token or name not in router.profile.backends:
            return PlainTextResponse("Not found", status_code=404, headers=SECURE_HTML)
        if request.headers.get("origin") != origin:
            return PlainTextResponse("Invalid form origin", status_code=403, headers=SECURE_HTML)
        try:
            fields = await read_form(request)
        except ValueError:
            fields = {}
        if set(fields) != {"csrf"} or not compare_digest(fields["csrf"].encode(), credentials.csrf.encode()):
            return PlainTextResponse("Invalid form token", status_code=403, headers=SECURE_HTML)
        try:
            if await router.connections.reconnect(name):
                return RedirectResponse(f"/#backend-{name}", status_code=303, headers=SECURE_HTML)
        except ReauthorizationError as error:
            code = str(error)
        else:
            code = "connection-failed"
        if code == "busy":
            return dashboard(notice=f"{name}: переподключение уже выполняется.", status_code=409)
        title, _tone = FAILURE_LABELS.get(code, ("Новое подключение не подтвердилось", "error"))
        worker = router.connections.active.get(name)
        suffix = " Действующее подключение сохранено." if worker is not None and worker.live else ""
        return dashboard(notice=f"{name}: {title}.{suffix}", status_code=502)

    async def status(_request):
        """Return profile status and metrics as machine-readable JSON."""
        return JSONResponse(router.snapshot(), headers={"Cache-Control": "no-store"})

    async def stylesheet(_request):
        """Serve the fixed local dashboard stylesheet."""
        return Response(STYLE, media_type="text/css", headers={"X-Content-Type-Options": "nosniff"})

    async def settings_page(request):
        """Render the same policy state exposed through the management MCP."""
        value = request.query_params.get("offset", "")
        offset = int(value) if value.isascii() and value.isdecimal() and len(value) <= 6 else 0
        return HTMLResponse(management.page(offset), headers=SECURE_HTML)

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
        routes.extend([Route("/settings", settings_page),
                       Route(f"/manage/{router.profile.endpoint_key}", McpEndpoint("management_manager"))])
    if modules is not None:
        routes.extend([Route("/modules", modules.overview), Route("/modules", modules.submit, methods=["POST"])])
    app = Starlette(routes=routes, lifespan=lifespan)
    return LocalBoundary(app, port, token)
