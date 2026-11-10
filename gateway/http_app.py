"""Serve MCP Streamable HTTP and the local dashboard behind one boundary."""

from base64 import b64decode
from binascii import Error as Base64Error
from contextlib import asynccontextmanager
from hmac import compare_digest

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .dashboard import STYLE, render
from .mcp_server import catalog_page, make_server

_CSP = "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"


def authorized(header: str, token: str, *, basic: bool) -> bool:
    """Check bearer credentials or browser Basic credentials for read-only routes."""
    kind, _, value = header.partition(" ")
    if kind.lower() == "bearer":
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
        mcp = request.url.path.startswith("/mcp/")
        header = request.headers.get("authorization", "")
        if self.token and not authorized(header, self.token, basic=not mcp):
            challenge = "Bearer" if mcp else 'Basic realm="ag-mcp-gateway"'
            response = PlainTextResponse("Authorization required", status_code=401,
                                         headers={"WWW-Authenticate": challenge})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(router, port: int, token: str | None):
    """Attach MCP, status JSON and read-only HTML to the same gateway state."""

    async def catalog(cursor):
        """List one page of the currently visible gateway catalog."""
        return catalog_page(router.visible_tools(), cursor, router.catalog_revision)

    async def call(params):
        """Pass extension metadata to the router with the call arguments."""
        metadata = params.meta.model_dump(by_alias=True, exclude_none=True) if params.meta is not None else None
        return await router.call(params.name, params.arguments or {}, metadata=metadata)

    mcp = make_server(catalog, call)
    manager = StreamableHTTPSessionManager(mcp, json_response=True, stateless=True)

    @asynccontextmanager
    async def lifespan(_app):
        """Own router and SDK manager in the same ASGI lifecycle."""
        async with router.running(), manager.run():
            yield

    class McpEndpoint:
        """Expose a raw ASGI endpoint without Starlette request coercion."""

        async def __call__(self, scope, receive, send):
            """Preserve MCP method negotiation and streaming headers."""
            await manager.handle_request(scope, receive, send)

    async def overview(request):
        """Render a current sanitized snapshot and bound the text filter."""
        query = request.query_params.get("q", "")[:200]
        return HTMLResponse(render(router.snapshot(), query), headers={
            "Content-Security-Policy": _CSP,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })

    async def status(_request):
        """Return profile status and metrics as machine-readable JSON."""
        return JSONResponse(router.snapshot(), headers={"Cache-Control": "no-store"})

    async def stylesheet(_request):
        """Serve the fixed local dashboard stylesheet."""
        return Response(STYLE, media_type="text/css", headers={"X-Content-Type-Options": "nosniff"})

    app = Starlette(routes=[
        Route(f"/mcp/{router.profile.name}", McpEndpoint()),
        Route("/", overview),
        Route(f"/api/status/{router.profile.name}", status),
        Route("/dashboard.css", stylesheet),
    ], lifespan=lifespan)
    return LocalBoundary(app, port, token)
