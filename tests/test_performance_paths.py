"""Protect page work, schema reuse, shared sizing and event-stream traffic."""

import asyncio

import httpx
import pytest
from jsonschema import Draft202012Validator, ValidationError
from mcp import McpError, types

from gateway.policy_settings import PolicyChange, ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Route, Router
from gateway.schema_checks import SchemaChecks
from gateway.telemetry import Journal, Telemetry
from gateway.tool_discovery import Discovery, Query
from gateway.upstream import EventStreamPacing


def fixture_router(tmp_path):
    """Create a synthetic stdio profile without starting an external process."""
    profile = Profile.model_validate({"name": "test", "backends": {
        "demo": {"transport": "stdio", "command": "unused"},
    }, "singleflight": ["demo.same"]})
    return Router(profile, Telemetry(Journal(tmp_path, 65536)))


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
