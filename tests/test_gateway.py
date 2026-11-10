"""Regression checks for isolation, singleflight and the journal byte budget."""

import asyncio
import json
from datetime import datetime, timezone
from threading import Event

import pytest
from mcp import types
from starlette.testclient import TestClient

from gateway.http_app import create_app
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
        first_dispatch = telemetry.last_upstream_at("a")
        assert first_dispatch is not None
        second = asyncio.create_task(router.call("a.read", {"a": 1, "b": 2}))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        session.release.set()
        assert (await second).structuredContent["args"] == {"a": 1, "b": 2}
        assert session.calls == 1
        assert telemetry.last_upstream_at("a") == first_dispatch
        assert not (await router.call("a.read", {"a": 1, "b": 2})).isError
        latest_dispatch = telemetry.last_upstream_at("a")
        assert (await router.call("a.write", {"private": "secret"})).isError
        assert telemetry.last_upstream_at("a") == latest_dispatch
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


def test_environment_defaults_and_identifiers():
    """Same-named profiles get disjoint endpoints, state and client names."""
    data = {
        "name": "company-" + "x" * 24,
        "backends": {"docs": {"transport": "stdio", "command": "unused"}},
    }
    legacy = Profile.model_validate(data)
    test = Profile.model_validate({**data, "environment": "test"})
    prod = Profile.model_validate({
        **data, "environment": "production", "access_token_env": "PROD_TOKEN",
    })
    assert [profile.resolve_port(None) for profile in (legacy, test, prod)] == [18792, 18793, 18794]
    assert prod.model_copy(update={"listen_port": 19042}).resolve_port(None) == 19042
    assert prod.resolve_port(19222) == 19222
    assert legacy.client_name == "ag-mcp-gateway"
    assert len({profile.client_name for profile in (legacy, test, prod)}) == 3
    assert len(test.client_name) <= 32 and len(prod.client_name) <= 32
    assert len({profile.state_path for profile in (legacy, test, prod)}) == 3
    assert len({profile.endpoint_key for profile in (legacy, test, prod)}) == 3


def test_production_requires_authentication(tmp_path, monkeypatch):
    """Production needs a token reference and refuses the unauthenticated switch."""
    from gateway.cli import main

    data = {
        "name": "company", "environment": "production",
        "backends": {"docs": {"transport": "stdio", "command": "unused"}},
    }
    with pytest.raises(ValueError, match="production profile requires access_token_env"):
        Profile.model_validate(data)
    data["access_token_env"] = "GATEWAY_PROD_TEST_TOKEN"
    (tmp_path / "company.json").write_text(json.dumps(data))
    state = tmp_path / "state"
    command = ["--profiles", str(tmp_path), "--state", str(state), "--profile", "company", "serve"]
    monkeypatch.delenv(data["access_token_env"], raising=False)
    assert main(command) == 1
    monkeypatch.setenv(data["access_token_env"], "synthetic-value")
    assert main([*command, "--allow-unauthenticated"]) == 1
    assert not state.exists()


@pytest.mark.parametrize("port", [0, 65536, True, "18793"])
def test_profile_rejects_invalid_ports(port):
    """Profile ports are integers in the bindable port range."""
    with pytest.raises(ValueError):
        Profile.model_validate({
            "name": "company", "environment": "test", "listen_port": port,
            "backends": {"docs": {"transport": "stdio", "command": "unused"}},
        })


@pytest.mark.parametrize("setting", [None, False, True])
def test_startup_waits_only_for_required_backends(tmp_path, monkeypatch, setting):
    """Expose the ready MCP while opt-in discovery runs and clean up all workers."""
    async def scenario():
        """Control discovery and route publication without relying on sleep durations."""
        backend = {"transport": "stdio", "command": "unused"}
        optional = backend.copy()
        if setting is not None:
            optional["background_startup"] = setting
        profile = Profile.model_validate({"name": "test", "backends": {"fast": backend, "slow": optional}})
        router = Router(profile, Telemetry(Journal(tmp_path, 4096)))
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()
        slow_published = asyncio.Event()
        serving = asyncio.Event()
        stop = asyncio.Event()
        stopped = set()
        session = SlowSession()
        session.release.set()
        tool = types.Tool(name="read", inputSchema={"type": "object"})

        async def run_backend(name, config, timeout, publish, authorization, *, probe):
            """Publish a real-looking catalog only when the test releases discovery."""
            try:
                if name == "slow":
                    slow_started.set()
                    await release_slow.wait()
                publish(name, session, {f"{name}.read": tool}, "ready")
                if name == "slow":
                    slow_published.set()
                await stop.wait()
            finally:
                stopped.add(name)

        async def serve():
            """Signal completion of the exact lifespan barrier used by the HTTP server."""
            async with router.running():
                serving.set()
                await stop.wait()

        monkeypatch.setattr("gateway.connections.run_backend", run_backend)
        task = asyncio.create_task(serve())
        try:
            await asyncio.wait_for(slow_started.wait(), 1)
            if setting:
                await asyncio.wait_for(serving.wait(), 1)
                assert router.snapshot()["backends"] == {"fast": "ready", "slow": "starting"}
                assert [tool.name for tool in router.visible_tools()] == ["fast.read"]
                assert not (await router.call("fast.read", {})).isError
                assert (await router.call("slow.read", {})).isError
            else:
                assert not serving.is_set()
            release_slow.set()
            await asyncio.wait_for(slow_published.wait(), 1)
            await asyncio.wait_for(serving.wait(), 1)
            assert [tool.name for tool in router.visible_tools()] == ["fast.read", "slow.read"]
        finally:
            stop.set()
            await asyncio.wait_for(task, 1)
        assert stopped == {"fast", "slow"}

    asyncio.run(scenario())


def test_background_startup_rejects_string_booleans():
    """Require explicit JSON booleans so a typo cannot change startup semantics."""
    with pytest.raises(ValueError, match="background_startup"):
        Profile.model_validate({"name": "test", "backends": {
            "slow": {"transport": "http", "url": "https://example.com/mcp", "background_startup": "false"},
        }})


def test_background_lifespan_clears_stale_routes_and_cancels_pending_startup(tmp_path, monkeypatch):
    """Restart the same HTTP app while keeping the second discovery pending."""
    profile = Profile.model_validate({"name": "test", "backends": {
        "slow": {"transport": "stdio", "command": "unused", "background_startup": True},
    }})
    router = Router(profile, Telemetry(Journal(tmp_path, 4096)))
    first_ready = Event()
    second_started = Event()
    second_stopped = Event()

    async def run_backend(name, config, timeout, publish, authorization, *, probe):
        """Publish only the first generation and record cancellation of the second."""
        pending = asyncio.Event()
        if not first_ready.is_set():
            tool = types.Tool(name="read", inputSchema={"type": "object"})
            publish(name, SlowSession(), {f"{name}.read": tool}, "ready")
            first_ready.set()
            await pending.wait()
            return
        try:
            second_started.set()
            await pending.wait()
        finally:
            second_stopped.set()

    def list_tools(client):
        """Initialize and read the catalog over the actual stateless MCP endpoint."""
        headers = {"Accept": "application/json, text/event-stream"}
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        }}
        initialized = client.post("/mcp/test", json=request, headers=headers).json()["result"]
        headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
        reply = client.post("/mcp/test", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                            headers=headers)
        return [tool["name"] for tool in reply.json()["result"]["tools"]]

    monkeypatch.setattr("gateway.connections.run_backend", run_backend)
    app = create_app(router, 18792, None)
    with TestClient(app, base_url="http://127.0.0.1:18792") as client:
        assert first_ready.wait(2)
        assert list_tools(client) == ["slow.read"]

    with TestClient(app, base_url="http://127.0.0.1:18792") as client:
        assert second_started.wait(2)
        assert client.get("/api/status/test").json()["backends"] == {"slow": "starting"}
        assert list_tools(client) == []
    assert second_stopped.is_set()



def test_last_call_is_per_backend_and_independent_of_connection_state(tmp_path):
    """Publish a UTC dispatch time only for the attempted MCP, even on tool failure."""
    class FailureSession:
        async def call_tool(self, name, arguments, *, meta=None):
            """Simulate a tool failure while keeping the connection available."""
            return types.CallToolResult(content=[], isError=True)

    profile = Profile.model_validate({"name": "test", "backends": {
        "active": {"transport": "stdio", "command": "unused"},
        "idle": {"transport": "stdio", "command": "unused"},
    }})
    router = Router(profile, Telemetry(Journal(tmp_path, 4096)))
    router.statuses.update(active="ready", idle="auth-rejected")
    router.routes["active.read"] = Route(types.Tool(name="read", inputSchema={}), FailureSession())
    assert all(row["last_call_at"] is None for row in router.snapshot()["connections"])

    assert asyncio.run(router.call("active.read", {})).isError
    assert asyncio.run(router.call("idle.missing", {})).isError
    active, idle = router.snapshot()["connections"]
    assert active["status"] == "ready"
    assert datetime.fromisoformat(active["last_call_at"]).tzinfo == timezone.utc
    assert idle["status"] == "auth-rejected" and idle["last_call_at"] is None
