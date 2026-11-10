"""Attach stdio MCP clients and the CLI to the running local gateway."""

from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .mcp_server import make_server, serve_stdio
from .router import tool_error


def local_client(token: str | None, timeout: float) -> httpx.AsyncClient:
    """Use a direct connection so a local credential never passes through proxy settings."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(headers=headers, timeout=timeout, trust_env=False, follow_redirects=False)


async def bridge(origin: str, profile: str, token: str | None, timeout: float):
    """Present one stdio client while sharing the daemon's catalog and upstream workers."""
    async with local_client(token, timeout) as http:
        async with streamable_http_client(f"{origin}/mcp/{profile}", http_client=http) as streams:
            async with ClientSession(*streams[:2], read_timeout_seconds=timedelta(seconds=timeout)) as client:
                await client.initialize()

                async def tools(cursor):
                    """Pass pagination through to the gateway without flattening the catalog."""
                    return await client.list_tools(cursor=cursor)

                async def call(params):
                    """Preserve the tool result and prevent sharing for metadata-bearing calls."""
                    try:
                        meta = params.meta.model_dump(by_alias=True, exclude_none=True) if params.meta else None
                        return await client.call_tool(params.name, params.arguments, meta=meta)
                    except Exception:
                        return tool_error("Gateway transport failed")

                await serve_stdio(make_server(tools, call))


async def read_status(origin: str, profile: str, token: str | None, timeout: float) -> dict:
    """Fetch authenticated CLI status without printing connection details on failure."""
    async with local_client(token, timeout) as http:
        response = await http.get(f"{origin}/api/status/{profile}")
        response.raise_for_status()
        return response.json()
