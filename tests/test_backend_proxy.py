"""Exercise each backend proxy through the real MCP HTTP transport."""

import asyncio
from base64 import b64encode
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
from mcp import McpError, types
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from gateway.http_app import create_app
from gateway.mcp_client import CatalogSession
from gateway.module_registry import Bind, Module, ModuleRegistry, Save
from gateway.policy_settings import ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Router
from gateway.session_context import SESSION_META
from gateway.telemetry import Journal, Telemetry

ORIGIN = "http://127.0.0.1:18793"
TOKEN = "synthetic-proxy-token"
PROFILE = "/mcp/test/fixture"
LOCAL = PROFILE + "/local"
OTHER = PROFILE + "/other"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}
TRACE_ID = "0123456789abcdef0123456789abcdef"
PARENT = f"00-{TRACE_ID}-0123456789abcdef-01"


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """Publish two overlapping synthetic catalogs and record real upstream dispatches."""
    starts = Counter()
    calls = []

    class Session:
        """Echo the dispatched identity, arguments and validated forwarding metadata."""

        def __init__(self, backend, generation):
            self.backend = backend
            self.generation = generation

        async def call_tool(self, name, arguments, *, meta=None):
            """Return a predictable result without invoking an external tool."""
            calls.append((self.backend, name, arguments, meta))
            return types.CallToolResult(content=[], structuredContent={
                "backend": self.backend, "generation": self.generation, "tool": name, "arguments": arguments,
                "metadata": meta or {},
            })

    async def run_backend(name, config, timeout, publish, authorization, *, probe):
        """Give each reconnect a fresh session and one changed catalog member."""
        starts[name] += 1
        generation = starts[name]
        generation_tool = "before_reload" if generation == 1 else "after_reload"
        names = ["project.read", "blocked", f"{name}_only", generation_tool]
        if name == "local":
            names.append("other.project.read")
        names.extend(f"fixture_{i:03d}" for i in range(101))
        tools = {f"{name}.{tool}": types.Tool(
            name=tool, inputSchema={"type": "object", "properties": {"caller": {"type": "string"}}},
            description=f"Synthetic {name} {tool}",
        ) for tool in names}
        publish(name, Session(name, generation), tools, "ready")
        await asyncio.Event().wait()

    monkeypatch.setattr("gateway.connections.run_backend", run_backend)
    command = {"transport": "stdio", "command": "fixture"}
    profile = Profile.model_validate({
        "name": "fixture", "environment": "test", "access_token_env": "SYNTHETIC_PROXY_TOKEN",
        "management_bridge": True, "modules_enabled": True, "backends": {
            "local": {**command, "forward_session_context": True, "forward_trace_context": True,
                      "session_args": {"project.read": {"caller": "remoteSessionId"}}},
            "other": command,
        }, "denied": ["local.blocked"], "singleflight": ["local.project.read"],
    })
    router = Router(profile, Telemetry(Journal(tmp_path / "logs", 65_536)))
    settings = ToolPolicySettings(router, tmp_path / "policy.json")
    return create_app(router, 18793, TOKEN, settings=settings), router, calls


def rpc(client, path, method, params=None, *, headers=HEADERS):
    """Send a protocol request to the live stateless session manager."""
    request = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    return client.post(path, headers=headers, json=request)


def test_proxy_isolates_backends_and_shares_policy_and_metadata(gateway):
    """Bind original MCP names to one configured server without bypassing policy."""
    app, router, calls = gateway
    with TestClient(app, base_url=ORIGIN) as client:
        assert rpc(client, LOCAL, "tools/list", headers={"Accept": HEADERS["Accept"]}).status_code == 401
        basic = "Basic " + b64encode(f"gateway:{TOKEN}".encode()).decode()
        assert rpc(client, LOCAL, "tools/list", headers={**HEADERS, "Authorization": basic}).status_code == 401
        assert rpc(client, LOCAL, "tools/list",
                   headers={**HEADERS, "Origin": "https://outside.invalid"}).status_code == 403
        assert rpc(client, PROFILE + "/missing", "tools/list").status_code == 404
        assert rpc(client, "/mcp/fixture/local", "tools/list").status_code == 404
        status = client.get("/api/status/test/fixture", headers=HEADERS).json()
        assert {backend["name"]: backend["mcp_url"] for backend in status["connections"]} == {
            "local": ORIGIN + LOCAL, "other": ORIGIN + OTHER,
        }

        local_list = rpc(client, LOCAL, "tools/list").json()["result"]
        assert "blocked" not in {tool["name"] for tool in local_list["tools"]}
        denied = rpc(client, LOCAL, "tools/call", {"name": "blocked", "arguments": {}})
        assert denied.json()["result"]["isError"]
        for name in ("other_only", "other.other_only"):
            rejected = rpc(client, LOCAL, "tools/call", {"name": name, "arguments": {}})
            assert rejected.json()["result"]["isError"]
        assert calls == []

        session = {"remoteSessionId": "synthetic-session"}
        result = rpc(client, LOCAL, "tools/call",
                     {"name": "project.read", "arguments": {}, "_meta": {SESSION_META: session}},
                     headers={**HEADERS, "traceparent": PARENT}).json()["result"]
        assert result["structuredContent"] == {
            "backend": "local", "generation": 1, "tool": "project.read",
            "arguments": {"caller": "synthetic-session"},
            "metadata": {SESSION_META: session, "traceparent": PARENT},
        }
        result = rpc(client, OTHER, "tools/call",
                     {"name": "project.read", "arguments": {}}).json()["result"]
        assert result["structuredContent"]["backend"] == "other"
        collision = rpc(client, LOCAL, "tools/call",
                        {"name": "other.project.read", "arguments": {}}).json()["result"]
        assert collision["structuredContent"]["backend"] == "local"
        assert collision["structuredContent"]["tool"] == "other.project.read"

        changed = rpc(client, "/manage/test/fixture", "tools/call", {
            "name": "gateway_set_tool_policy",
            "arguments": {"tool": "local.project.read", "policy": "deny", "expected_revision": 0},
        }).json()["result"]
        assert not changed.get("isError", False)
        for path, name in ((LOCAL, "project.read"), (PROFILE, "local.project.read")):
            assert rpc(client, path, "tools/call", {"name": name, "arguments": {}}).json()["result"]["isError"]
        assert len(calls) == 3
        snapshot = router.telemetry.snapshot()
        assert snapshot["total"]["denied"] == 3
        assert snapshot["total"]["upstream"] == 3
        assert snapshot["tools"]["local.project.read"]["upstream"] == 1


@asynccontextmanager
async def sdk_clients(app, *paths):
    """Open initialized MCP client sessions sharing an in-process HTTP server."""

    async def request_app(scope, receive, send):
        """Share the lifespan state that Uvicorn supplies on HTTP requests."""
        await app({**scope, "state": state}, receive, send)

    async with AsyncExitStack() as stack:
        state = await stack.enter_async_context(app.app.router.lifespan_context(app.app))
        transport = httpx.ASGITransport(app=request_app)
        http = await stack.enter_async_context(httpx.AsyncClient(transport=transport, headers=HEADERS, timeout=3))
        clients = []
        for path in paths:
            streams = await stack.enter_async_context(streamable_http_client(ORIGIN + path, http_client=http))
            client = await stack.enter_async_context(CatalogSession(*streams[:2]))
            await client.initialize()
            clients.append(client)
        yield http, clients


def test_sdk_scoped_pages_and_reconnect_keep_the_same_proxy_address(gateway):
    """Use the pinned SDK to validate original names, opaque cursors and a live reconnect."""
    app, router, calls = gateway

    async def scenario():
        """Compare page tokens between two scoped endpoints and the aggregate."""
        async with sdk_clients(app, LOCAL) as (http, (client,)):
            first = await client.catalog_page(None)
            assert len(first.tools) == 100 and first.nextCursor
            second = await client.catalog_page(first.nextCursor)
            descriptors = {tool.name: tool for tool in (*first.tools, *second.tools)}
            assert len(descriptors) == 105
            assert {"project.read", "local_only", "before_reload"} <= descriptors.keys()
            assert not {"blocked", "other_only", "local.project.read"} & descriptors.keys()
            assert descriptors["project.read"].description == "Synthetic local project.read"

            for path in (OTHER, PROFILE):
                page = (await http.post(ORIGIN + path, json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/list",
                })).json()["result"]
                assert page["nextCursor"]
                with pytest.raises(McpError) as rejected:
                    await client.catalog_page(page["nextCursor"])
                assert rejected.value.error.code == types.INVALID_PARAMS
                if path == PROFILE:
                    assert all("." in tool["name"] for tool in page["tools"])
                other_cursor = await http.post(ORIGIN + path, json={
                    "jsonrpc": "2.0", "id": 3, "method": "tools/list",
                    "params": {"cursor": first.nextCursor},
                })
                assert other_cursor.json()["error"]["code"] == types.INVALID_PARAMS

            assert await router.connections.reconnect("local")
            with pytest.raises(McpError) as stale:
                await client.catalog_page(first.nextCursor)
            assert stale.value.error.code == types.INVALID_PARAMS
            updated = await client.catalog_page(None)
            assert "after_reload" in {tool.name for tool in updated.tools}
            assert "before_reload" not in {tool.name for tool in updated.tools}
            result = await client.request_tool("after_reload", {}, None)
            assert result.structuredContent["generation"] == 2

    asyncio.run(scenario())
    assert [(backend, name) for backend, name, _, _ in calls] == [("local", "after_reload")]


def test_scoped_sdk_call_shares_persisted_hooks_singleflight_and_statistics(gateway, tmp_path, monkeypatch):
    """Route both public names through the same hooks, upstream flight and counters."""
    app, router, calls = gateway
    tool = "local.project.read"
    registry = ModuleRegistry(router, tmp_path / "proxy-modules.json")
    router.modules = registry

    async def scenario():
        """Reload the hook bindings, then overlap scoped and aggregate calls."""
        async with sdk_clients(app, LOCAL, PROFILE) as (http, (scoped, aggregate)):
            for index, (phase, fields) in enumerate((
                ("before", {"caller": "/input/value"}),
                ("after", {"value": "/input/arguments/caller", "backend": "/input/backend"}),
            )):
                module = Module.model_validate({
                    "id": phase, "title": phase, "steps": [{"op": "object", "fields": fields}],
                })
                saved = registry.save(Save(expected_revision=index * 2, module=module))
                registry.bind(Bind(expected_revision=saved["revision"], tool=tool, phase=phase, module=phase))
            router.modules = ModuleRegistry(router, registry.path)

            both_waiting = asyncio.Event()
            upstream = router.routes[tool].session
            call_tool = upstream.call_tool
            share = router._shared

            def observe_shared_flight(*args):
                """Release the synthetic upstream only once a request joins its flight."""
                task, joined = share(*args)
                if joined:
                    both_waiting.set()
                return task, joined

            async def held_call(name, arguments, *, meta=None):
                """Keep the flight open until both HTTP requests have reached it."""
                await both_waiting.wait()
                return await call_tool(name, arguments, meta=meta)

            monkeypatch.setattr(router, "_shared", observe_shared_flight)
            monkeypatch.setattr(upstream, "call_tool", held_call)
            first, second = await asyncio.gather(
                scoped.request_tool("project.read", {"value": "example", "unused": "local"}, None),
                aggregate.request_tool(tool, {"value": "example", "unused": "aggregate"}, None),
            )
            assert first.structuredContent == second.structuredContent == {"value": "example", "backend": "local"}
            assert calls == [("local", "project.read", {"caller": "example"}, None)]
            status = (await http.get(ORIGIN + "/api/status/test/fixture")).json()
            metrics = status["metrics"]["tools"][tool]
            assert metrics["received"] == 2 and metrics["upstream"] == metrics["merged"] == 1
            assert metrics["timed"] == 2 and metrics["input_tokens_estimate"] > 0

    asyncio.run(scenario())
