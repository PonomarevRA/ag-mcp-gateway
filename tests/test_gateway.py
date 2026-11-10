"""Regression checks for isolation, singleflight and the journal byte budget."""

import asyncio
import json

import pytest
from mcp import types

from gateway.profile import Profile
from gateway.router import Route, Router
from gateway.telemetry import Journal, Telemetry


class SlowSession:
    """Control the completion of one simulated upstream tool."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def call_tool(self, name, arguments, *, meta=None):
        """Wait for the test to release concurrent callers."""
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return types.CallToolResult(content=[], structuredContent={"tool": name, "args": arguments, "meta": meta})


def test_singleflight_cancellation_and_fresh_call(tmp_path):
    """One disconnected waiter leaves the shared upstream call running."""
    async def scenario():
        """Exercise overlap, argument order, cancellation and no-cache behavior."""
        profile = Profile.model_validate({
            "name": "test", "backends": {"a": {"transport": "stdio", "command": "unused"}},
            "singleflight": ["a.read"], "denied": ["a.write"],
        })
        telemetry = Telemetry(Journal(tmp_path, 4096))
        router = Router(profile, telemetry)
        session = SlowSession()
        tool = types.Tool(name="read", inputSchema={"type": "object"})
        router.routes["a.read"] = Route(tool, session)
        first = asyncio.create_task(router.call("a.read", {"b": 2, "a": 1}))
        await session.entered.wait()
        second = asyncio.create_task(router.call("a.read", {"a": 1, "b": 2}))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        session.release.set()
        assert (await second).structuredContent["args"] == {"a": 1, "b": 2}
        assert session.calls == 1
        assert not (await router.call("a.read", {"a": 1, "b": 2})).isError
        assert (await router.call("a.write", {"private": "secret"})).isError
        assert session.calls == 2
        counts = telemetry.snapshot()["total"]
        assert counts["merged"] == 1
        assert counts["denied"] == 1
        assert counts["upstream"] == 2
        assert "secret" not in (tmp_path / "calls.0.jsonl").read_text()

    asyncio.run(scenario())


def test_journal_rotation_and_budget_shrink(tmp_path):
    """Retain newest valid events and cap aggregate bytes, including after restart."""
    journal = Journal(tmp_path, 8192)
    for index in range(200):
        journal.write({"seq": index, "event": "x" * 180})
    assert sum(path.stat().st_size for path in tmp_path.glob("*.jsonl")) <= 8192
    current = (tmp_path / "calls.0.jsonl").read_text().splitlines()
    assert json.loads(current[-1])["seq"] == 199
    smaller = Journal(tmp_path, 4096)
    smaller.write({"seq": 200})
    assert sum(path.stat().st_size for path in tmp_path.glob("*.jsonl")) <= 4096


def test_profile_rejects_plain_remote_http_and_conflicting_policy():
    """Unsafe endpoints and overlapping deny/share rules fail during config parsing."""
    with pytest.raises(ValueError, match="upstream requires HTTPS"):
        Profile.model_validate({
            "name": "test", "backends": {"a": {"transport": "http", "url": "http://example.com/mcp"}},
        })
    with pytest.raises(ValueError, match="overlap"):
        Profile.model_validate({
            "name": "test", "backends": {"a": {"transport": "http", "url": "https://example.com/mcp"}},
            "denied": ["a.read"], "singleflight": ["a.read"],
        })


def test_catalog_rejects_stale_cursor():
    """A client can read the next page only against the same catalog revision."""
    from mcp import McpError

    from gateway.mcp_server import catalog_page

    tools = [types.Tool(name=f"tool-{index}", inputSchema={}) for index in range(101)]
    first = catalog_page(tools, None, 7)
    assert len(first.tools) == 100 and first.nextCursor
    second = catalog_page(tools, first.nextCursor, 7)
    assert [tool.name for tool in second.tools] == ["tool-100"]
    assert second.nextCursor is None
    with pytest.raises(McpError, match="Invalid catalog cursor"):
        catalog_page(tools, first.nextCursor, 8)


def test_clean_stdio_exit_withdraws_published_routes(tmp_path):
    """A backend that exits without an exception must disappear from MCP discovery."""
    import sys
    from textwrap import dedent

    program = dedent("""\
        import asyncio
        import os
        from mcp import types
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server

        server = Server("synthetic-exit")
        @server.list_tools()
        async def tools():
            return [types.Tool(name="exit", inputSchema={"type": "object"})]

        @server.call_tool()
        async def call(name, arguments):
            os._exit(0)

        async def main():
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())
        asyncio.run(main())
        """)

    async def scenario():
        """Exercise real process EOF and observe the resulting catalog transition."""
        profile = Profile.model_validate({
            "name": "test", "timeout_seconds": 3,
            "backends": {"fixture": {"transport": "stdio", "command": sys.executable, "args": ["-c", program]}},
        })
        router = Router(profile, Telemetry(Journal(tmp_path, 4096)))
        async with router.running():
            assert [tool.name for tool in router.visible_tools()] == ["fixture.exit"]
            assert (await router.call("fixture.exit", {})).isError
            async with asyncio.timeout(1):
                while router.statuses["fixture"] == "ready":
                    await asyncio.sleep(0.01)
            assert router.statuses["fixture"].startswith("unavailable")
            assert router.visible_tools() == []

    asyncio.run(scenario())
