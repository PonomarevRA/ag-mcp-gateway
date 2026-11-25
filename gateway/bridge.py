"""Attach stdio MCP clients and the CLI to the running local gateway."""

from datetime import timedelta
from uuid import uuid4

import httpx
from mcp.client.streamable_http import streamable_http_client

from .hook_lifecycle import HOOK_META
from .mcp_client import CatalogSession
from .mcp_server import make_server, serve_stdio
from .profile import mcp_path
from .router import tool_error
from .session_context import BRIDGE_SESSION_META, SESSION_META, SessionContext


def local_client(token: str | None, timeout: float) -> httpx.AsyncClient:
    """Use a direct connection so a local credential never passes through proxy settings."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(headers=headers, timeout=timeout, trust_env=False, follow_redirects=False)


async def bridge(origin: str, profile: str, token: str | None, timeout: float,
                 session: SessionContext | None = None, *, management: bool = False, hook_session: str | None = None,
                 backend: str | None = None):
    """Present the selected local daemon surface as a standard MCP stdio client."""
    if management and backend is not None:
        raise ValueError("management and backend MCP scopes are mutually exclusive")
    bridge_id = uuid4().hex
    path = f"/manage/{profile}" if management else mcp_path(profile, backend)
    async with local_client(token, timeout) as http:
        async with streamable_http_client(f"{origin}{path}", http_client=http) as streams:
            async with CatalogSession(*streams[:2], read_timeout_seconds=timedelta(seconds=timeout)) as client:
                await client.initialize()

                async def tools(cursor, meta):
                    """Pass pagination through to the gateway without flattening the catalog."""
                    return await client.catalog_page(cursor, meta)

                async def call(params):
                    """Forward metadata and return the result already validated by the gateway."""
                    try:
                        meta = params.meta.model_dump(by_alias=True, exclude_none=True) if params.meta else {}
                        meta[BRIDGE_SESSION_META] = bridge_id
                        if session is not None:
                            meta[SESSION_META] = session.values
                        if hook_session is not None:
                            meta[HOOK_META] = hook_session
                        return await client.request_tool(params.name, params.arguments, meta)
                    except Exception:
                        return tool_error("Gateway transport failed")

                await serve_stdio(make_server(tools, call, forward_catalog_meta=True))


async def read_status(origin: str, profile: str, token: str | None, timeout: float) -> dict:
    """Fetch authenticated CLI status without printing connection details on failure."""
    async with local_client(token, timeout) as http:
        response = await http.get(f"{origin}/api/status/{profile}")
        response.raise_for_status()
        return response.json()
