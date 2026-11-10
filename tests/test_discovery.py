"""Check MCP tool discovery, durable ratings and the restored latency window."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from mcp import types
from starlette.testclient import TestClient

from gateway.http_app import create_app
from gateway.policy_settings import ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Router
from gateway.skill_store import SkillStore
from gateway.telemetry import Journal, LatencyHistory, Telemetry
from gateway.tool_discovery import DISCOVERY_META
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
        assert all(entry["name"] != "demo.blocked" for entry in catalog)
