"""Protect page work, schema reuse, shared sizing and event-stream traffic."""

import asyncio
import json

import httpx
import pytest
from jsonschema import Draft202012Validator, ValidationError
from mcp import McpError, types
from mcp.client.streamable_http import streamable_http_client

from gateway.mcp_client import CatalogSession
from gateway.mcp_server import make_server
from gateway.policy_settings import PolicyChange, ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Route, Router
from gateway.schema_checks import SchemaChecks
from gateway.telemetry import Journal, Telemetry
from gateway.tool_discovery import Discovery, Query
from gateway.upstream import EventStreamPacing, McpHttpClient


def fixture_router(tmp_path):
    """Create a synthetic stdio profile without starting an external process."""
    profile = Profile.model_validate({"name": "test", "backends": {
        "demo": {"transport": "stdio", "command": "unused"},
    }, "singleflight": ["demo.same"]})
    return Router(profile, Telemetry(Journal(tmp_path, 65536)))


def test_server_catalog_does_not_retain_old_tool_descriptors():
    """Keep paginated MCP catalog handling independent of the SDK tool cache."""
    async def exercise():
        """List multiple revisions and call through the same server instance."""
        revision = 0

        async def list_tools(cursor):
            """Return a new descriptor for the selected page in each revision."""
            page = "first" if cursor is None else "second"
            tool = types.Tool(name=f"demo.rev_{revision}_{page}", inputSchema={"type": "object"})
            return types.ListToolsResult(tools=[tool], nextCursor="next" if cursor is None else None)

        async def call_tool(params):
            """Return the requested name without consulting an SDK catalog cache."""
            return types.CallToolResult(content=[types.TextContent(type="text", text=params.name)])

        server = make_server(list_tools, call_tool)
        assert server.create_initialization_options().capabilities.tools is not None
        handler = server.request_handlers[types.ListToolsRequest]
        for revision in range(3):
            first = await handler(types.ListToolsRequest())
            assert first.root.tools[0].name == f"demo.rev_{revision}_first"
            params = types.PaginatedRequestParams(cursor=first.root.nextCursor)
            second = await handler(types.ListToolsRequest(params=params))
            assert second.root.tools[0].name == f"demo.rev_{revision}_second"

        call = types.CallToolRequest(params=types.CallToolRequestParams(name="demo.rev_2_second"))
        result = await server.request_handlers[types.CallToolRequest](call)
        assert result.root.content[0].text == "demo.rev_2_second"
        assert not server._tool_cache

    asyncio.run(exercise())


def test_catalog_materializes_only_current_page(tmp_path, monkeypatch):
    """Count descriptor creation and keep metadata and policy scoped to one page."""
    router = fixture_router(tmp_path)
    tool = types.Tool(name="fixture", inputSchema={"type": "object"}, _meta={"upstream": "private"})
    for index in range(305):
        router.routes[f"demo.tool_{index:03}"] = Route(tool, object())
    settings = ToolPolicySettings(router, tmp_path / "policy.json")
    build = router._visible_descriptor
    materialized = []

    def counted(name):
        """Track actual descriptors allocated on the page path."""
        materialized.append(name)
        return build(name)

    monkeypatch.setattr(router, "_visible_descriptor", counted)
    first = router.visible_page(None)
    second = router.visible_page(first.nextCursor)
    assert len(materialized) == 200 and second.tools[0].name == "demo.tool_100"
    assert first.tools[0].meta is None
    first.tools[0].meta = {"request": True}
    assert router.visible_page(None).tools[0].meta is None
    settings.change(PolicyChange(tool="demo.tool_000", policy="deny", expected_revision=0))
    before = len(materialized)
    with pytest.raises(McpError):
        router.visible_page(second.nextCursor)
    assert len(materialized) == before
    assert router.visible_page(None).tools[0].name == "demo.tool_001"



def test_usage_order_stays_stable_while_paginating(tmp_path):
    """New usage changes fresh MCP listings without losing entries in an open listing."""
    router = fixture_router(tmp_path)
    for index in range(102):
        name = f"tool_{index:03}"
        router.routes[f"demo.{name}"] = Route(types.Tool(name=name, inputSchema={}), object())

    router.telemetry.completed("demo.tool_101", "ok", 1, False, 0, 0)
    first = router.visible_page(None)
    assert first.tools[0].name == "demo.tool_101"
    router.telemetry.completed("demo.tool_100", "ok", 1, False, 0, 0)
    router.telemetry.completed("demo.tool_100", "ok", 1, True, 0, 0)
    router.telemetry.completed("demo.tool_099", "denied", 1, False, 0, 0)

    second = router.visible_page(first.nextCursor)
    assert [tool.name for tool in second.tools] == ["demo.tool_099", "demo.tool_100"]
    assert router.visible_page(None).tools[0].name == "demo.tool_100"
    assert router.backend_page("demo", None).tools[0].name == "tool_100"
    alphabetical = router.visible_page(None, sort="name")
    assert alphabetical.tools[0].name == "demo.tool_000"
    assert router.visible_page(alphabetical.nextCursor).tools[0].name == "demo.tool_100"
    with pytest.raises(McpError):
        router.visible_page(first.nextCursor, sort="name")
    with pytest.raises(McpError):
        router.backend_page("demo", first.nextCursor)


def test_discovery_matches_punctuation_only_tool_name():
    """Preserve exact qualified names even when they have no searchable words."""
    route = Route(types.Tool(name="-", inputSchema={"type": "object"}), object())
    matches = Discovery.matching(Query(query="_.-"), [("_.-", route)], {})
    assert [(name, item) for _, _, name, item in matches] == [("_.-", route)]


def test_schema_cache_reuses_meta_check_but_checks_each_instance(monkeypatch):
    """Count metaschema work and show bounded eviction and instance rejection."""
    original = Draft202012Validator.check_schema
    checked = []

    def counted(_cls, schema):
        """Record one metaschema check then run the real validator."""
        checked.append(schema)
        original(schema)

    monkeypatch.setattr(Draft202012Validator, "check_schema", classmethod(counted))
    one = {"type": "integer"}
    two = {"type": "string"}
    cache = SchemaChecks(limit=1)
    cache.validate(1, one)
    cache.validate(2, one)
    with pytest.raises(ValidationError):
        cache.validate("invalid", one)
    cache.validate("valid", two)
    cache.validate(3, one)
    assert checked == [one, two, one]
    too_small = SchemaChecks(byte_limit=4)
    too_small.validate(1, one)
    too_small.validate(2, one)
    assert checked[-2:] == [one, one]


def test_joined_callers_size_unchanged_result_once(tmp_path, monkeypatch):
    """Retain per-caller metrics while reusing result bytes on one actual dispatch."""
    router = fixture_router(tmp_path)
    serialized = []
    original = types.CallToolResult.model_dump_json

    def counted(result, **kwargs):
        """Count the journal's output sizing calls."""
        serialized.append(result)
        return original(result, **kwargs)

    class Upstream:
        async def call_tool(self, name, arguments, meta):
            """Give parallel callers time to join the same synthetic result."""
            await asyncio.sleep(0.02)
            return types.CallToolResult(content=[], structuredContent={"value": "x" * 8192})

    monkeypatch.setattr(types.CallToolResult, "model_dump_json", counted)
    tool = types.Tool(name="same", inputSchema={"type": "object"})
    router.routes["demo.same"] = Route(tool, Upstream())

    async def exercise():
        """Send one concurrent burst with identical normalized arguments."""
        return await asyncio.gather(*(router.call("demo.same", {}) for _ in range(24)))

    results = asyncio.run(exercise())
    assert all(not result.isError for result in results)
    assert len(serialized) == 1
    counts = router.telemetry.total
    assert (counts.received, counts.upstream, counts.merged) == (24, 1, 23)


def test_cursorless_sse_reconnects_are_paced_without_delaying_tool_resumption():
    """Use mock HTTP to verify pacing and deadlines for each request type."""
    async def exercise():
        """Queue two idle reads, a normal POST and a resumable tool response."""
        seen = []
        clock = asyncio.get_running_loop().time

        def handle(request):
            """Record the actual transport start and its configured read timeout."""
            seen.append((request.method, clock(), request.extensions["timeout"]["read"],
                         request.headers.get("last-event-id")))
            return httpx.Response(200, content=b"")

        pacing = EventStreamPacing(minimum_interval=0.03)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=2,
                                    event_hooks={"request": [pacing.request]}) as client:
            await client.get("http://fixture")
            again = asyncio.create_task(client.get("http://fixture"))
            await asyncio.sleep(0)
            await client.post("http://fixture")
            await client.get("http://fixture", headers={"Last-Event-ID": "response-cursor"})
            await again
        assert [(method, timeout, cursor) for method, _, timeout, cursor in seen] == [
            ("GET", 300, None), ("POST", 2, None), ("GET", 2, "response-cursor"), ("GET", 300, None),
        ]
        assert seen[3][1] - seen[0][1] >= 0.028

    asyncio.run(exercise())


def test_http_request_deadline_closes_heartbeat_stream_and_keeps_session_alive():
    """Stop a detached SDK POST at its deadline and still resume a later tool response."""
    async def exercise():
        """Run an unfinished and a resumable tool call through the real SDK transport."""
        closed = asyncio.Event()
        ping_replied = asyncio.Event()
        timeout = 0.10
        resumed_id = None
        sse_headers = {"content-type": "text/event-stream"}

        class Heartbeats(httpx.AsyncByteStream):
            """Keep the incomplete SSE response active until the client closes it."""

            async def __aiter__(self):
                """Send comments more often than the client's inactivity timeout."""
                while True:
                    await asyncio.sleep(0.01)
                    yield b": heartbeat\n\n"

            async def aclose(self):
                """Record HTTP response cleanup independently of the tool waiter."""
                closed.set()

        class NotificationStream(httpx.AsyncByteStream):
            """Deliver a server-initiated ping on the persistent notification stream."""

            async def __aiter__(self):
                """Keep the stream open after the ping until normal session shutdown."""
                yield b'data: {"jsonrpc":"2.0","id":"upstream-ping","method":"ping"}\n\n'
                await asyncio.Event().wait()

        async def respond(request):
            """Serve the server ping, incomplete tool response and later resumption."""
            nonlocal resumed_id
            if request.method == "GET":
                if "last-event-id" not in request.headers:
                    return httpx.Response(200, headers=sse_headers, stream=NotificationStream())
                assert request.headers["last-event-id"] == "tool-cursor"
                payload = json.dumps({"jsonrpc": "2.0", "id": resumed_id, "result": {"content": []}})
                return httpx.Response(200, headers=sse_headers, content=f"data: {payload}\n\n".encode())
            if request.method == "DELETE":
                return httpx.Response(204)

            body = json.loads(request.content)
            if "method" not in body:
                assert body == {"jsonrpc": "2.0", "id": "upstream-ping", "result": {}}
                ping_replied.set()
                return httpx.Response(202)
            if body["method"] == "initialize":
                result = {"protocolVersion": body["params"]["protocolVersion"], "capabilities": {},
                          "serverInfo": {"name": "fixture", "version": "1"}}
                return httpx.Response(200, headers={"mcp-session-id": "fixture"},
                                      json={"jsonrpc": "2.0", "id": body["id"], "result": result})
            if body["method"] == "notifications/initialized":
                return httpx.Response(202)
            assert body["method"] == "tools/call"
            if body["params"]["name"] == "heartbeat":
                return httpx.Response(200, headers=sse_headers, stream=Heartbeats())
            resumed_id = body["id"]
            return httpx.Response(200, headers=sse_headers,
                                  content=b"event: message\nid: tool-cursor\nretry: 5\ndata:\n\n")

        pacing = EventStreamPacing()
        async with McpHttpClient(timeout, transport=httpx.MockTransport(respond),
                                 event_hooks={"request": [pacing.request]}) as http:
            async with streamable_http_client("http://fixture/mcp", http_client=http) as streams:
                async with CatalogSession(*streams[:2]) as session:
                    await session.initialize()
                    await asyncio.wait_for(ping_replied.wait(), 0.25)
                    # A server ping response must not arm a timer on the SDK's shared writer.
                    await asyncio.sleep(timeout * 1.5)
                    with pytest.raises(TimeoutError):
                        async with asyncio.timeout(timeout):
                            await session.request_tool("heartbeat", {}, None)
                    await asyncio.wait_for(closed.wait(), 0.25)
                    async with asyncio.timeout(timeout):
                        result = await session.request_tool("resume", {}, None)
                    assert not result.isError

    asyncio.run(exercise())


def test_upstream_output_schema_failure_has_its_own_diagnostic(tmp_path):
    """Distinguish a completed upstream call with invalid output from transport failure."""
    from gateway.mcp_client import CatalogSession

    class Upstream(CatalogSession):
        """Return a successful transport response containing a schema mismatch."""

        def __init__(self):
            """Use the real published-output validator without opening a transport."""

        async def request_tool(self, name, arguments, meta):
            """Return a type that violates the advertised output schema."""
            return types.CallToolResult(content=[], structuredContent={"value": "synthetic-private-output"})

    router = fixture_router(tmp_path)
    tool = types.Tool(name="bad", inputSchema={"type": "object"},
                      outputSchema={"type": "object", "properties": {"value": {"type": "integer"}}})
    router.routes["demo.bad"] = Route(tool, Upstream())
    result = asyncio.run(router.call("demo.bad", {}))
    assert result.isError
    assert result.content[0].text == "Upstream tool output failed schema validation"
    event = json.loads(router.telemetry.journal.paths[0].read_text())
    assert event["error_code"] == "invalid_upstream_output"
    assert "synthetic-private-output" not in str(event)
    assert router.telemetry.total.upstream == router.telemetry.total.errors == 1
