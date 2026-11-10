"""Provide a synthetic backend for local setup and end-to-end checks."""

import asyncio

from mcp import types

from .mcp_server import catalog_page, make_server

_ECHO = types.Tool(
    name="echo",
    description="Возвращает переданный текст. Тестовая операция чтения.",
    inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    annotations=types.ToolAnnotations(readOnlyHint=True),
)
_BLOCKED = types.Tool(name="blocked", description="Проверка запрета.",
                      inputSchema={"type": "object"})


def demo_server():
    """Build a local-only backend with one slow echo and one blocked fixture tool."""
    async def tools(cursor):
        """Advertise fixed, synthetic descriptors."""
        return catalog_page([_ECHO, _BLOCKED], cursor, 0)

    async def call(params):
        """Echo fixture input after a short pause that permits overlapping calls."""
        await asyncio.sleep(0.2)
        text = str((params.arguments or {}).get("text", params.name))
        content = [types.TextContent(type="text", text=text)]
        result = {"echo": text}
        if params.meta is not None:
            result["metadata"] = params.meta.model_dump(by_alias=True, exclude_none=True)
        return types.CallToolResult(content=content, structuredContent=result)

    return make_server(tools, call)
