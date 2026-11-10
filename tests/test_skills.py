"""Check SQLite retention, ranking and the management/approval boundary."""

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from starlette.testclient import TestClient

from gateway.cli import main
from gateway.http_app import create_app
from gateway.policy_settings import ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Router
from gateway.skill_store import SkillSettings, SkillStore, SkillStoreError
from gateway.telemetry import Journal, Telemetry

TOOL = "demo.echo"
STEPS = [TOOL, "demo.blocked", TOOL]
TOKEN = "skill-fixture-token"


def test_approval_ranking_and_feedback(tmp_path):
    """Only approvals are indexed, and useful votes break equal text relevance."""
    store = SkillStore(tmp_path / "skills.sqlite3", SkillSettings())
    first = store.save("Проверка логов", "История инцидентов", STEPS, "First")["id"]
    second = store.save("Проверка логов", "История инцидентов", STEPS, "Second")["id"]
    assert store.find("логов", 5)["skills"] == []
    with pytest.raises(SkillStoreError):
        store.get(first, "approved")
    store.approve(first)
    store.approve(second)
    assert store.approve(second)["kind"] == "approved"
    store.rate(first, False)
    with ThreadPoolExecutor(4) as pool:
        votes = list(pool.map(lambda _: store.rate(second, True), range(12)))
    assert max(vote["votes"] for vote in votes) == 12
    ranked = store.find('логов" OR *', 5)["skills"]
    assert [row["id"] for row in ranked] == [second, first]
    assert ranked[0]["tools"] == STEPS and "instructions" not in ranked[0]
    assert store.get(second, "approved")["instructions"] == "Second"
    assert store.find("unrelated", 5)["skills"] == []
    assert store.find("***", 5) == {"skills": [], "cache": []}
    with pytest.raises(SkillStoreError):
        store.discard(second)


def test_cache_count_expiry_and_page_limit(tmp_path):
    """Evict cache by count, time and SQLite FULL while retaining protected records."""
    path = tmp_path / "skills.sqlite3"
    limited = SkillStore(path, SkillSettings(max_bytes=131072, cache_entries=2, cache_ttl_seconds=60))
    old = limited.save("logs", "first", STEPS)["id"]
    expired = limited.save("logs", "second", STEPS)["id"]
    latest = limited.save("logs", "third", STEPS)["id"]
    assert [row["id"] for row in limited.find("logs", 10)["cache"]] == [latest, expired]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE approaches SET expires=? WHERE id=?", (int(time.time()) - 1, expired))
    assert [row["id"] for row in limited.find("logs", 10)["cache"]] == [latest]
    limited.cleanup()
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM approaches WHERE id IN (?,?)", (old, expired)).fetchone()[0] == 0

    store = SkillStore(path, SkillSettings(max_bytes=131072))
    approved = store.save("logs", "trusted", [TOOL], "reviewed")["id"]
    store.approve(approved)
    draft = store.save("logs", "protected draft", [TOOL], "draft")["id"]
    long_steps = [f"demo.{str(i).zfill(3)}{'x' * 117}" for i in range(32)]
    for index in range(75):
        newest = store.save("logs", f"cache {index}", long_steps)["id"]
    assert path.stat().st_size <= 131072
    with closing(sqlite3.connect(path)) as connection:
        count = connection.execute("SELECT count(*) FROM approaches WHERE kind='cache'").fetchone()[0]
    assert 0 < count < 75
    assert store.find("logs", 10)["cache"][0]["id"] == newest
    assert store.get(approved, "approved")["instructions"] == "reviewed"
    assert store.get(draft, "draft")["instructions"] == "draft"
    assert path.stat().st_mode & 0o077 == 0


def test_full_with_only_protected_records_can_recover(tmp_path):
    """Refuse a write at quota without dropping drafts and accept data after discard."""
    store = SkillStore(tmp_path / "skills.sqlite3", SkillSettings(max_bytes=131072))
    created = []
    with pytest.raises(SkillStoreError, match="full"):
        for index in range(200):
            created.append(store.save("logs", str(index), STEPS, "я" * 6000)["id"])
    assert 1 < len(created) < 200
    assert {row["id"] for row in store.drafts()} == set(created)
    store.discard(created.pop())
    assert store.save("logs", "replacement", STEPS, "я" * 6000)["kind"] == "draft"


def test_management_and_cli_share_one_profile_store(tmp_path, monkeypatch, capsys):
    """Exercise authorized MCP and CLI approval, isolation and stale-tool reporting."""
    monkeypatch.setenv("SKILL_TEST_TOKEN", TOKEN)
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    profile_data = {"name": "company", "environment": "test", "management_bridge": True,
                    "access_token_env": "SKILL_TEST_TOKEN", "skill_router": {},
                    "backends": {"demo": {"transport": "stdio", "command": "unused", "background_startup": True}},
                    "denied": ["demo.blocked"]}
    profile_file = profile_dir / "company.json"
    profile_file.write_text(json.dumps(profile_data))
    profile = Profile.model_validate(profile_data)

    async def offline(name, config, timeout, publish, authorization, *, probe):
        """Keep fixture tools offline so saved sequences are flagged as unavailable."""
        publish(name, None, {}, "disconnected")

    monkeypatch.setattr("gateway.connections.run_backend", offline)
    state = tmp_path / "state"
    root = state / profile.state_path
    router = Router(profile, Telemetry(Journal(root / "logs", 4096)))
    settings = ToolPolicySettings(router, root / "tool-policy.json")
    store = SkillStore(root / "skills.sqlite3", profile.skill_router)
    app = create_app(router, 18793, TOKEN, settings=settings, skill_store=store)
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}
    operator = ["--profiles", str(profile_dir), "--state", str(state), "--profile", "company"]
    target = "/manage/test/company"

    with TestClient(app, base_url="http://127.0.0.1:18793") as client:
        def call(name, arguments, endpoint=target):
            """Send a management request through the standard MCP surface."""
            message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}
            reply = client.post(endpoint, headers=headers, json=message)
            assert reply.status_code == 200
            return reply.json()["result"]

        assert client.post(target).status_code == 401
        assert call("gateway_skill_approve", {})["isError"]
        fields = {"title": "Inspect logs", "summary": "Look for a burst", "tools": STEPS}
        assert call("gateway_skill_cache_save", {**fields, "arguments": {"secret": "fixture"}})["isError"]
        assert call("gateway_skill_cache_save", {**fields, "tools": ["other.tool"]})["isError"]
        assert call("gateway_skill_cache_save", fields)["structuredContent"]["kind"] == "cache"
        draft = call("gateway_skill_propose", {**fields, "instructions": "Inspect summary first."})
        identity = draft["structuredContent"]["id"]
        assert call("gateway_skill_load", {"id": identity})["isError"]
        search = call("gateway_skill_search", {"query": "logs"})["structuredContent"]
        assert search["suggest_skill_draft"] and search["skills"] == [] and search["cache"][0]["tools"] == STEPS
        assert main([*operator, "skill-review", identity]) == 0
        assert json.loads(capsys.readouterr().out)["instructions"] == "Inspect summary first."
        assert main([*operator, "skill-approve", identity]) == 0
        assert json.loads(capsys.readouterr().out)["kind"] == "approved"
        loaded = call("gateway_skill_load", {"id": identity})["structuredContent"]
        assert loaded["tools"] == STEPS and loaded["blocked_tools"] == ["demo.blocked"]
        assert loaded["missing_tools"] == [TOOL, "demo.blocked"] and not loaded["usable"]
        assert call("gateway_skill_rate", {"id": identity, "useful": True})["structuredContent"]["votes"] == 1
        assert call("gateway_skill_load", {"id": identity}, "/mcp/test/company")["isError"]

    profile_file.write_text(json.dumps({**profile_data, "environment": "production"}))
    assert main([*operator, "skill-review"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    prod = Profile.model_validate({**profile_data, "environment": "production"})
    other = SkillStore(state / prod.state_path / "skills.sqlite3", prod.skill_router)
    with pytest.raises(SkillStoreError):
        other.get(identity, "approved")
