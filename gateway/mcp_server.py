"""Adapt a catalog and a call handler to the SDK tool-only MCP server."""

from mcp import McpError, types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

VERSION = "0.12.0"
_PAGE_SIZE = 100


def make_server(list_tools, call_tool) -> Server:
    """Preserve full tool results and let the policy handler see blocked calls."""
    server = Server("ag-mcp-gateway", version=VERSION)

    async def catalog(request: types.ListToolsRequest) -> types.ServerResult:
        """Read one catalog page with its opaque cursor."""
        cursor = request.params.cursor if request.params else None
        return types.ServerResult(await list_tools(cursor))

    async def call(request: types.CallToolRequest) -> types.ServerResult:
        """Forward one typed MCP request through the supplied dispatcher."""
        result = await call_tool(request.params)
        return types.ServerResult(result)

    # Both operations use the gateway catalog. The SDK list decorator would
    # retain every descriptor seen across revisions in its unused tool cache.
    server.request_handlers[types.ListToolsRequest] = catalog
    server.request_handlers[types.CallToolRequest] = call
    return server


def catalog_window(total: int, cursor: str | None, revision: int) -> tuple[slice, str | None]:
    """Validate the revision and select a page before materializing its descriptors."""
    start = 0
    if cursor is not None:
        prefix = f"{revision}:"
        offset = cursor[len(prefix):]
        if not cursor.startswith(prefix) or not offset.isascii() or not offset.isdecimal() or len(offset) > 8:
            raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="Invalid catalog cursor"))
        start = int(offset)
        if start % _PAGE_SIZE or not 0 < start < total:
            raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="Invalid catalog cursor"))
    end = start + _PAGE_SIZE
    next_cursor = f"{revision}:{end}" if end < total else None
    return slice(start, end), next_cursor



def catalog_page(tools: list[types.Tool], cursor: str | None, revision: int) -> types.ListToolsResult:
    """Publish a page from an already materialized static catalogue."""
    window, next_cursor = catalog_window(len(tools), cursor, revision)
    return types.ListToolsResult(tools=tools[window], nextCursor=next_cursor)


async def serve_stdio(server: Server) -> None:
    """Run an MCP stdio connection with protocol output reserved for stdout."""
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
