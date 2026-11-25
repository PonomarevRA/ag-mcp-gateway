"""Check MCP tool discovery, durable ratings and the restored latency window."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from mcp import types
from starlette.testclient import TestClient

from gateway.http_app import create_app
from gateway.module_registry import ModuleRegistry
from gateway.policy_settings import ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Router
from gateway.skill_store import SkillStore
from gateway.telemetry import Journal, LatencyHistory, Telemetry
from gateway.tool_discovery import DISCOVERY_META, Discovery, Query
from gateway.tool_feedback import FeedbackError, ToolFeedback

TOKEN = "synthetic-agent-discovery"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}


def event(telemetry, name, elapsed, outcome="ok", joined=False):
    """Feed the same observation shape as a completed gateway call."""
    telemetry.completed(name, outcome, elapsed, joined, 2, 10)


def test_latency_history_restores_only_successful_non_joined_calls(tmp_path, monkeypatch):
    """Keep a bounded window across journal rotations and ignore malformed lines."""
    monkeypatch.setattr(LatencyHistory, "LIMIT", 3)
    journal = Journal(tmp_path, 4096)
    first = Telemetry(journal, with_history=True)
    event(first, "demo.read", 10)
    event(first, "demo.read", 20)
    event(first, "demo.read", 900, "error")
    event(first, "demo.read", 900, joined=True)
    event(first, "demo.read", 30)
    event(first, "demo.other", 40)
    with journal.paths[0].open("ab") as log:
        log.write(b'broken\n[]\n')
    assert first.tool_latency("demo.read") == (25, 2)
    second = Telemetry(Journal(tmp_path, 4096), with_history=True)
    assert second.tool_latency("demo.read") == (25, 2)
    assert second.tool_latency("demo.other") == (40, 1)
    assert second.tool_latency("demo.unknown") == (None, 0)
    event(second, "demo.read", 90)
    assert second.tool_latency("demo.read") == (60, 2)



def test_latency_rounds_the_same_live_and_after_restart(tmp_path):
    """Persist the exact centisecond observation used by the live mean."""
    first = Telemetry(Journal(tmp_path, 4096), with_history=True)
    event(first, "demo.read", 2.675)
    restored = Telemetry(Journal(tmp_path, 4096), with_history=True)
    assert first.tool_latency("demo.read") == restored.tool_latency("demo.read") == (2.67, 1)


def test_search_ranking_yields_and_rechecks_policy(tmp_path, monkeypatch):
    """Keep the event loop free while ranking and reject a changed route snapshot."""
    started, finish = Event(), Event()

    def held_rank(*_args):
        """Signal the start of ranking and wait for the async policy change."""
        started.set()
        assert finish.wait(timeout=3)
        return []

    async def exercise():
        """Change the catalogue before releasing the scoring thread."""
        router = SimpleNamespace(catalog_revision=1, routes={}, profile=SimpleNamespace(denied=set()))
        discovery = Discovery(router, ToolFeedback(tmp_path / "feedback.sqlite3"), None)
        monkeypatch.setattr(discovery, "rank_tools", held_rank)
        task = asyncio.create_task(discovery.search(Query(query="logs")))
        try:
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(0.01)
            router.catalog_revision += 1
        finally:
            finish.set()
        with pytest.raises(FeedbackError, match="Catalog changed"):
            await task

    asyncio.run(exercise())


def test_feedback_is_durable_idempotent_and_profile_local(tmp_path):
    """Deduplicate concurrent retries and refuse a receipt reused for another vote."""
    profile = tmp_path / "test.sqlite3"
    db = ToolFeedback(profile)
    with ThreadPoolExecutor(6) as pool:
        votes = list(pool.map(lambda _: db.rate("demo.read", True, "fixture-vote"), range(12)))
    assert all(value == (1, 1) for value in votes)
    with pytest.raises(FeedbackError, match="different vote"):
        db.rate("demo.read", False, "fixture-vote")
    assert db.rate("demo.read", False, "fixture-next") == (1, 2)
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(lambda index: db.rate("demo.read", True, f"fixture-extra-{index}"), range(8)))
    assert db.current("demo.read") == (9, 10)
    assert ToolFeedback(profile).snapshot() == {"demo.read": (9, 10)}
    assert ToolFeedback(tmp_path / "prod.sqlite3").snapshot() == {}
    assert profile.stat().st_mode & 0o077 == 0


def test_mcp_discovery_hides_denied_and_decorates_allowed_catalog(tmp_path, monkeypatch):
    """Search tools/skills, describe, vote and expose the same card on tools/list."""
    profile = Profile.model_validate({"name": "test", "access_token_env": "FIXTURE_TOKEN",
        "agent_discovery": True, "management_bridge": True, "skill_router": {},
        "backends": {"demo": {"transport": "stdio", "command": "fixture"}},
        "denied": ["demo.blocked"]})
    telemetry = Telemetry(Journal(tmp_path / "logs", 4096), with_history=True)
    event(telemetry, "demo.read", 12)
    event(telemetry, "demo.read", 30)
    router = Router(profile, telemetry)
    settings = ToolPolicySettings(router, tmp_path / "policy.json")
    skill_store = SkillStore(tmp_path / "skills.sqlite3", profile.skill_router)
    for name in ("demo.read", "demo.blocked"):
        draft = skill_store.save("Проверка логов", "Чтение логов", [name], "Apply")["id"]
        skill_store.approve(draft)

    async def fixture(name, config, timeout, publish, authorization, *, probe):
        """Publish a synthetic catalog without external MCP calls."""
        descriptors = {}
        for tool in ("read", "other", "blocked"):
            descriptors[f"demo.{tool}"] = types.Tool(name=tool, description="Поиск логов",
                                                     inputSchema={"type": "object"})
        publish(name, object(), descriptors, "ready")
        await asyncio.Event().wait()

    monkeypatch.setattr("gateway.connections.run_backend", fixture)
    app = create_app(router, 18792, TOKEN, settings=settings, skill_store=skill_store,
                     feedback=ToolFeedback(tmp_path / "tool-feedback.sqlite3"))
    with TestClient(app, base_url="http://127.0.0.1:18792") as client:
        assert client.post("/manage/test").status_code == 401

        def mcp(method, params=None, *, target="/manage/test"):
            """Call the actual stateless MCP endpoint and unwrap its response."""
            request = {"jsonrpc": "2.0", "id": 1, "method": method}
            if params is not None:
                request["params"] = params
            response = client.post(target, headers=HEADERS, json=request)
            assert response.status_code == 200
            return response.json()["result"]

        def call(name, arguments):
            """Invoke one agent discovery command."""
            return mcp("tools/call", {"name": name, "arguments": arguments})

        names = {entry["name"] for entry in mcp("tools/list")["tools"]}
        assert {"gateway_tools_search", "gateway_tool_describe", "gateway_tool_rate"} <= names
        found = call("gateway_tools_search", {"query": "логов"})["structuredContent"]
        assert {tool["name"] for tool in found["tools"]} == {"demo.read", "demo.other"}
        assert [skill["tools"] for skill in found["skills"]] == [["demo.read"]]
        assert found["tools"][0]["name"] == "demo.read" and found["tools"][0]["uses"] == 2
        alphabetical = call("gateway_tools_search", {"query": "логов", "sort": "name"})["structuredContent"]
        assert [tool["name"] for tool in alphabetical["tools"]] == ["demo.other", "demo.read"]
        assert "input_schema" not in found["tools"][0]
        tool = call("gateway_tool_describe", {"tool": "demo.read"})["structuredContent"]
        assert tool["latency"] == {"mean_ms": 21, "samples": 2}
        assert tool["input_schema"] == {"type": "object"} and tool["usefulness"]["score"] is None
        assert call("gateway_tool_describe", {"tool": "demo.blocked"})["isError"]

        args = {"tool": "demo.read", "useful": True, "feedback_id": "fixture-receipt"}
        assert call("gateway_tool_rate", {**args, "tool": "demo.blocked"})["isError"]
        assert call("gateway_tool_rate", args)["structuredContent"]["usefulness"]["votes"] == 1
        assert call("gateway_tool_rate", args)["structuredContent"]["usefulness"]["score"] == 1
        again = call("gateway_tools_search", {"query": "логов", "kind": "tools"})["structuredContent"]
        assert again["tools"][0]["name"] == "demo.read" and again["skills"] == []
        catalog = mcp("tools/list", target="/mcp/test")["tools"]
        read = next(entry for entry in catalog if entry["name"] == "demo.read")
        assert read["_meta"][DISCOVERY_META] == {"latency": tool["latency"],
                                                 "usefulness": {"helpful": 1, "votes": 1, "score": 1}}
        assert [entry["name"] for entry in catalog] == ["demo.read", "demo.other"]
        alphabetical = mcp("tools/list", {"_meta": {"ag-mcp-gateway/sort": "name"}}, target="/mcp/test")
        assert [entry["name"] for entry in alphabetical["tools"]] == ["demo.other", "demo.read"]
        scoped = mcp("tools/list", target="/mcp/test/demo")["tools"]
        assert {entry["name"] for entry in scoped} == {"read", "other"}
        scoped_read = next(entry for entry in scoped if entry["name"] == "read")
        assert scoped_read["_meta"][DISCOVERY_META] == read["_meta"][DISCOVERY_META]


def test_keyword_catalog_observes_shape_and_drafts_allowlisted_module(tmp_path, monkeypatch):
    """Exercise keyword discovery, transient observations and lazy browser access."""
    profile = Profile.model_validate({
        "name": "test", "access_token_env": "FIXTURE_TOKEN",
        "agent_discovery": True, "management_bridge": True, "modules_enabled": True,
        "backends": {name: {"transport": "stdio", "command": "fixture"}
                     for name in ("grafana", "opensearch")},
        "module_chain_tools": ["grafana.search", "opensearch.search", "grafana.blocked"],
        "denied": ["grafana.blocked"],
    })
    router = Router(profile, Telemetry(Journal(tmp_path / "logs", 4096)))
    router.modules = ModuleRegistry(router, tmp_path / "modules.json")
    settings = ToolPolicySettings(router, tmp_path / "policy.json")

    class FixtureSession:
        async def call_tool(self, name, arguments, *, meta=None):
            """Return a technical field and a private scalar that must never be indexed."""
            return types.CallToolResult(content=[], structuredContent={
                "traceId": "private-id-123", "confidentialSecretKey": "private-value",
            })

    async def fixture(name, config, timeout, publish, authorization, *, probe):
        """Advertise different trace fields and one opaque runtime-only tool."""
        field = "traceId" if name == "grafana" else "trace_id"
        search = types.Tool(name="search", inputSchema={
            "type": "object", "properties": {field: {"type": "string"}}, "required": [field],
        }, annotations=types.ToolAnnotations(readOnlyHint=True))
        descriptors = {f"{name}.search": search}
        if name == "grafana":
            descriptors["grafana.opaque"] = types.Tool(name="opaque", inputSchema={"type": "object"})
            descriptors["grafana.blocked"] = search
        publish(name, FixtureSession(), descriptors, "ready")
        await asyncio.Event().wait()

    monkeypatch.setattr("gateway.connections.run_backend", fixture)
    app = create_app(router, 18792, TOKEN, settings=settings, feedback=ToolFeedback(tmp_path / "feedback.sqlite3"))
    with TestClient(app, base_url="http://127.0.0.1:18792") as client:
        def mcp(path, name, arguments):
            """Invoke a real MCP endpoint using the fixture profile token."""
            response = client.post(path, headers=HEADERS, json={"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            return response.json()["result"]

        def find(**arguments):
            """Search live tools through the management MCP."""
            return mcp("/manage/test", "gateway_tools_find_keywords", arguments)["structuredContent"]

        before = find(query="traceId", draft_trace_module=True)
        assert {item["name"] for item in before["tools"]} == {"grafana.search", "opensearch.search"}
        draft = before["module_draft"]
        assert [step["tool"] for step in draft["module"]["steps"] if step["op"] == "call"] == [
            "grafana.search", "opensearch.search"]
        assert draft["module"]["steps"][0]["fields"] == {"traceId": "/input/trace_id"}
        assert not router.modules.state.modules

        result = mcp("/mcp/test", "grafana.opaque", {"privateArgument": "another-private-value"})
        assert not result.get("isError")
        client.portal.call(router.keywords.collect)
        after = find(query="trace_id", backend="grafana")
        assert [item["name"] for item in after["tools"]] == ["grafana.search", "grafana.opaque"]
        assert after["tools"][1]["observed_keywords"] == ["trace"]
        assert find(query="private-value")["tools"] == []
        assert find(query="confidentialSecretKey")["tools"] == []

        url = "/api/tools/keywords?tool=grafana.opaque"
        assert client.get(url).status_code == 401
        auth = ("gateway", TOKEN)
        keywords = client.get(url, auth=auth)
        assert keywords.json()["observed_keywords"] == ["trace"]
        assert "private" not in keywords.text
        assert client.get("/api/tools/keywords?tool=grafana.blocked", auth=auth).status_code == 404
        page = client.get("/tools?lang=en", auth=auth)
        assert 'data-keyword-tool="grafana.opaque"' in page.text and "Keywords</summary>" in page.text
        assert "connect-src 'self'" in page.headers["Content-Security-Policy"]
