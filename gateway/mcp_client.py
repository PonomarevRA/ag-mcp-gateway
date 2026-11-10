"""Read MCP catalogs and dispatch requests without the SDK's mutable tool cache."""

from mcp import ClientSession, types

from .schema_checks import SchemaChecks


class CatalogSession(ClientSession):
    """Keep discovery and call validation tied to immutable published tool descriptors."""

    async def catalog_page(self, cursor: str | None) -> types.ListToolsResult:
        """Read a page without changing the SDK's per-tool output schema cache."""
        params = types.PaginatedRequestParams(cursor=cursor) if cursor is not None else None
        request = types.ClientRequest(types.ListToolsRequest(params=params))
        return await self.send_request(request, types.ListToolsResult)

    async def request_tool(self, name: str, arguments: dict | None, meta: dict | None) -> types.CallToolResult:
        """Send a tool request without consulting a mutable catalog cache."""
        metadata = types.RequestParams.Meta(**meta) if meta is not None else None
        params = types.CallToolRequestParams(name=name, arguments=arguments, _meta=metadata)
        request = types.ClientRequest(types.CallToolRequest(params=params))
        return await self.send_request(request, types.CallToolResult)

    async def call_published_tool(self, tool: types.Tool, arguments: dict, meta: dict | None,
                                  checks: SchemaChecks) -> types.CallToolResult:
        """Validate a successful response against the schema captured before dispatch."""
        result = await self.request_tool(tool.name, arguments, meta)
        if not result.isError and tool.outputSchema is not None:
            if result.structuredContent is None:
                raise ValueError("tool output schema requires structured content")
            checks.validate(result.structuredContent, tool.outputSchema)
        return result
