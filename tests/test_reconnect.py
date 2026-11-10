"""Exercise hot generation replacement and real MCP tool change notifications."""

import asyncio
import re
import sys
import textwrap
from base64 import b64encode

import httpx
from mcp import types
from starlette.testclient import TestClient

from gateway.bridge import bridge
from gateway.http_app import create_app
from gateway.policy_settings import ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Router
from gateway.telemetry import Journal, Telemetry

ORIGIN = "http://127.0.0.1:18792"
TOKEN = "fixture-gateway-reconnect"


def test_browser_and_bridge_replace_only_a_verified_catalog(tmp_path, monkeypatch):
    """Authenticate a browser/agent and retain the original routes after a failed candidate."""
    attempts = {"local": 0, "other": 0}

    async def run_backend(name, config, timeout, publish, authorization, *, probe):
        """Fail one reconnect, then publish a different verified generation."""
        attempts[name] += 1
        attempt = attempts[name]
        if name == "local" and attempt == 2:
            publish(name, None, {}, "auth-rejected")
            return
        suffix = "new" if attempt > 2 else "old"
        tool = types.Tool(name=suffix, inputSchema={"type": "object"})
        publish(name, object(), {f"{name}.{suffix}": tool}, "ready")
        await asyncio.Event().wait()

    def listed(client, path="/mcp/test"):
        """Read tools through the real stateless MCP manager."""
        reply = client.post(path, headers=agent, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {tool["name"] for tool in reply.json()["result"]["tools"]}

    monkeypatch.setattr("gateway.connections.run_backend", run_backend)
    stdio = {"transport": "stdio", "command": "fixture"}
    profile = Profile.model_validate({"name": "test", "access_token_env": "TEST_GATEWAY",
        "management_bridge": True, "backends": {"local": stdio, "other": stdio}})
    router = Router(profile, Telemetry(Journal(tmp_path, 4096)))
    settings = ToolPolicySettings(router, tmp_path / "policy.json")
    app = create_app(router, 18792, TOKEN, settings=settings)
    browser = {"Authorization": "Basic " + b64encode(f"gateway:{TOKEN}".encode()).decode()}
    agent = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}
    with TestClient(app, base_url=ORIGIN) as client:
        assert listed(client) == {"local.old", "other.old"}
        assert "gateway_reconnect_mcp" in listed(client, "/manage/test")
        root = client.get("/", headers=browser)
        assert 'action="/reconnect/local"' in root.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', root.text).group(1)
        url = "/reconnect/local"
        headers = {**browser, "Origin": ORIGIN}
        assert client.post(url, headers=agent, data={"csrf": csrf}).status_code == 401
        assert client.post(url, headers=browser, data={"csrf": csrf}).status_code == 403
        assert client.post(url, headers=headers, data={"csrf": "bad"}).status_code == 403
        before = router.catalog_revision
        failed = client.post(url, headers=headers, data={"csrf": csrf})
        assert failed.status_code == 502 and "HTTP 401" in failed.text
        assert "Действующее подключение сохранено" in failed.text
        assert router.catalog_revision == before and listed(client) == {"local.old", "other.old"}
        ok = client.post(url, headers=headers, data={"csrf": csrf}, follow_redirects=False)
        assert ok.status_code == 303 and ok.headers["location"] == "/#backend-local"
        assert router.catalog_revision > before and listed(client) == {"local.new", "other.old"}
        reply = client.post("/manage/test", headers=agent, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "gateway_reconnect_mcp", "arguments": {"backend": "local"}},
        }).json()["result"]["structuredContent"]
        assert reply["connected"] and reply["catalog_revision"] == router.catalog_revision
        assert attempts == {"local": 4, "other": 1}


def test_real_stdio_list_changed_replaces_catalog_and_keeps_old_on_failure(tmp_path, caplog):
    """Receive notifications through MCP 1.30 and swap only after successful list_tools."""
    program = textwrap.dedent('''
        import asyncio
        from mcp import types
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server

        server = Server("fixture")
        state = "old"

        entered = asyncio.Event()
        released = asyncio.Event()

        @server.list_tools()
        async def list_tools(request: types.ListToolsRequest):
            if state == "error" and request.params and request.params.cursor:
                raise ValueError("fixture-private-error")
            value_type = "string" if state == "old" else "integer"
            schema = {"type":"object","properties":{"value":{"type":value_type}},"required":["value"]}
            tools = [
                types.Tool(name="read", inputSchema={"type":"object"}, outputSchema=schema),
                types.Tool(name="switch", inputSchema={"type":"object"}),
                types.Tool(name="old" if state == "error" else state, inputSchema={"type":"object"}),
            ]
            return types.ListToolsResult(tools=tools, nextCursor="broken" if state == "error" else None)

        async def call(request):
            global state
            params = request.params
            arguments = params.arguments or {}
            content = None
            if params.name == "switch":
                if arguments.get("release"):
                    released.set()
                else:
                    if arguments.get("wait_for_read"):
                        await entered.wait()
                    state = arguments["next"]
                    await server.request_context.session.send_tool_list_changed()
            elif params.name == "read":
                value = 7 if state == "new" else "old-result"
                if arguments.get("hold"):
                    entered.set()
                    await released.wait()
                content = {"value": value}
            result = types.CallToolResult(content=[], structuredContent=content)
            return types.ServerResult(result)

        server.request_handlers[types.CallToolRequest] = call

        async def main():
            async with stdio_server() as streams:
                await server.run(*streams, server.create_initialization_options())
        asyncio.run(main())
    ''')

    async def scenario():
        """Trigger a failed re-list followed by a successful refresh without changing the session."""
        profile = Profile.model_validate({"name": "test", "timeout_seconds": 4, "backends": {
            "fixture": {"transport": "stdio", "command": sys.executable, "args": ["-c", program]},
        }})
        router = Router(profile, Telemetry(Journal(tmp_path, 4096)))
        async with router.running():
            assert set(router.routes) == {"fixture.old", "fixture.read", "fixture.switch"}
            worker = router.connections.active["fixture"]
            before = router.catalog_revision
            assert not (await router.call("fixture.switch", {"next": "error"})).isError
            async with asyncio.timeout(3):
                while "Catalog refresh failed for fixture" not in caplog.text:
                    await asyncio.sleep(0.02)
            assert router.catalog_revision == before and "fixture.old" in router.routes
            assert "fixture-private-error" not in caplog.text
            assert (await router.call("fixture.read", {})).structuredContent == {"value": "old-result"}
            in_flight = asyncio.create_task(router.call("fixture.read", {"hold": True}))
            try:
                assert not (await router.call("fixture.switch", {"next": "new", "wait_for_read": True})).isError
                async with asyncio.timeout(3):
                    while "fixture.new" not in router.routes:
                        await asyncio.sleep(0.02)
                assert router.catalog_revision > before and "fixture.old" not in router.routes
                assert router.connections.active["fixture"] is worker
                assert (await router.call("fixture.read", {})).structuredContent == {"value": 7}
            finally:
                await router.call("fixture.switch", {"release": True})
            assert (await in_flight).structuredContent == {"value": "old-result"}

    asyncio.run(scenario())


def test_agent_bridge_relist_keeps_already_dispatched_result(tmp_path, monkeypatch):
    """Keep an old-generation call valid when an agent re-lists through its stdio bridge."""
    async def scenario():
        """Switch the published schema while a Bridge call is waiting for its result."""
        entered, release = asyncio.Event(), asyncio.Event()
        generation = 0

        class Session:
            """Return one generation-specific value and hold a requested call."""

            def __init__(self, value):
                self.value = value

            async def call_tool(self, name, arguments, meta):
                """Wait for the test to complete re-listing before returning the held value."""
                if arguments.get("hold"):
                    entered.set()
                    await release.wait()
                return types.CallToolResult(content=[], structuredContent={"value": self.value})

        async def run_backend(name, config, timeout, publish, authorization, *, probe):
            """Publish a new output schema each time the gateway opens a connection."""
            nonlocal generation
            generation += 1
            value_type = "string" if generation == 1 else "integer"
            schema = {"type": "object", "properties": {"value": {"type": value_type}}, "required": ["value"]}
            tool = types.Tool(name="read", inputSchema={"type": "object"}, outputSchema=schema)
            publish(name, Session("old-result" if generation == 1 else 7), {"local.read": tool}, "ready")
            await asyncio.Event().wait()

        monkeypatch.setattr("gateway.connections.run_backend", run_backend)
        profile = Profile.model_validate({"name": "test", "backends": {
            "local": {"transport": "stdio", "command": "fixture"},
        }})
        router = Router(profile, Telemetry(Journal(tmp_path, 4096)))
        app = create_app(router, 18792, None)

        async def request_app(scope, receive, send):
            """Share the state Uvicorn normally supplies from the ASGI lifespan."""
            await app({**scope, "state": state}, receive, send)

        def local_http(token, timeout):
            """Send Bridge requests to the actual gateway handlers without opening a socket."""
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=request_app), timeout=timeout)

        async def exercise_bridge(server):
            """Run the Bridge MCP handlers over its real HTTP client and gateway endpoint."""
            list_tools = server.request_handlers[types.ListToolsRequest]
            call_tool = server.request_handlers[types.CallToolRequest]

            async def schema_type():
                """Read the current value type advertised through the Bridge."""
                response = await list_tools(types.ListToolsRequest())
                return response.root.tools[0].outputSchema["properties"]["value"]["type"]

            def request(arguments):
                """Construct one call to the same tool name in both generations."""
                return types.CallToolRequest(params=types.CallToolRequestParams(name="local.read", arguments=arguments))

            assert await schema_type() == "string"
            held = asyncio.create_task(call_tool(request({"hold": True})))
            try:
                async with asyncio.timeout(3):
                    await entered.wait()
                    assert await router.connections.reconnect("local")
                    assert await schema_type() == "integer"
                    assert (await call_tool(request({}))).root.structuredContent == {"value": 7}
            finally:
                release.set()
                response = (await held).root
            assert not response.isError and response.structuredContent == {"value": "old-result"}

        monkeypatch.setattr("gateway.bridge.local_client", local_http)
        monkeypatch.setattr("gateway.bridge.serve_stdio", exercise_bridge)
        async with app.app.router.lifespan_context(app.app) as state:
            await bridge(ORIGIN, "test", None, 4)

    asyncio.run(scenario())
