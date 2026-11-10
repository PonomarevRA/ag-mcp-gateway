"""Own one MCP session per configured upstream and discover its tool catalog."""

import asyncio
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta

import httpx
from anyio import ClosedResourceError, EndOfStream
from anyio.abc import ObjectReceiveStream
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .profile import HttpBackend, StdioBackend, secret

_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]+")


class ObservedReceiveStream(ObjectReceiveStream):
    """Expose SDK transport EOF without consuming messages outside ClientSession."""

    def __init__(self, source: ObjectReceiveStream):
        """Pair the receive channel with an event owned by the backend worker."""
        self.source = source
        self.closed = asyncio.Event()

    async def receive(self):
        """Read one message or signal that the underlying transport can no longer deliver."""
        try:
            return await self.source.receive()
        except (EndOfStream, ClosedResourceError):
            self.closed.set()
            raise

    async def aclose(self):
        """Release the receive channel when the SDK closes its session."""
        await self.source.aclose()


@asynccontextmanager
async def open_session(config, timeout: float):
    """Open the selected SDK transport and keep its contexts on the owning task."""
    async with AsyncExitStack() as stack:
        if isinstance(config, StdioBackend):
            environment = {}
            for target, source in config.env_from.items():
                if source not in os.environ:
                    raise ValueError("missing upstream environment variable")
                environment[target] = os.environ[source]
            params = StdioServerParameters(command=config.command, args=config.args, env=environment)
            # Child stderr may contain credentials or payloads. Keep the gateway journal metadata-only.
            sink = stack.enter_context(open(os.devnull, "w"))
            streams = await stack.enter_async_context(stdio_client(params, errlog=sink))
        else:
            assert isinstance(config, HttpBackend)
            token = secret(config.bearer_token_env)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            loopback = httpx.URL(config.url).host in {"127.0.0.1", "::1", "localhost"}
            client = await stack.enter_async_context(
                httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False, trust_env=not loopback)
            )
            streams = await stack.enter_async_context(streamable_http_client(config.url, http_client=client))
        incoming = ObservedReceiveStream(streams[0])
        session = ClientSession(incoming, streams[1], read_timeout_seconds=timedelta(seconds=timeout))
        await stack.enter_async_context(session)
        yield session, incoming.closed


async def discover(session: ClientSession, prefix: str):
    """Read paginated tools and reject ambiguous names or an unbounded catalog."""
    tools = {}
    cursor = None
    seen_cursors = set()
    for _ in range(64):
        page = await session.list_tools(cursor=cursor)
        for tool in page.tools:
            name = f"{prefix}.{tool.name}"
            if len(name) > 128 or not _TOOL_NAME.fullmatch(tool.name):
                raise ValueError("invalid upstream tool name")
            if name in tools:
                raise ValueError("duplicate upstream tool name")
            tools[name] = tool
            if len(tools) > 2000:
                raise ValueError("upstream catalog exceeds 2000 tools")
        cursor = page.nextCursor
        if cursor is None:
            return tools
        if cursor in seen_cursors:
            raise ValueError("repeated upstream cursor")
        seen_cursors.add(cursor)
    raise ValueError("upstream catalog exceeds 64 pages")


async def run_backend(prefix, config, timeout, publish):
    """Keep the discovered session alive and publish a sanitized failure on exit."""
    try:
        async with open_session(config, timeout) as (session, closed):
            async with asyncio.timeout(timeout):
                await session.initialize()
                tools = await discover(session, prefix)
            publish(prefix, session, tools, "ready")
            await closed.wait()
            publish(prefix, None, {}, "unavailable (connection closed)")
    except Exception as error:
        publish(prefix, None, {}, f"unavailable ({type(error).__name__})")
