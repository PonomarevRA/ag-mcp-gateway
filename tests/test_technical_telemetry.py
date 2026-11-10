"""Verify collector paging, agent diagnosis and durable, bounded technical events."""

import asyncio
import json
import sqlite3
from time import monotonic, sleep

from mcp import types
from starlette.testclient import TestClient

from gateway.http_app import create_app
from gateway.policy_settings import ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Router
from gateway.technical_events import TechnicalEvents
from gateway.technical_journal import TechnicalJournal
from gateway.telemetry import Journal, Telemetry

PORT = 18794
ORIGIN = f"http://127.0.0.1:{PORT}"
HEADERS = {"Authorization": "Bearer fixture-gateway", "Accept": "application/json, text/event-stream"}
LOGS = "/api/technical-logs/v1"
METRICS = "/api/diagnostics/v1/metrics"


def wait_head(journal, latest):
    """Wait until the background writer commits exactly the expected sequence."""
    deadline = monotonic() + 5
    while monotonic() < deadline:
        status = journal.status()
        if int(status["latestSequence"]) == latest:
            return status
        sleep(0.01)
    raise AssertionError(f"Technical journal did not reach sequence {latest}")


def test_bounded_journal_retains_cursor_after_restart(tmp_path):
    """Check rotation, bounds and commit continuity with real SQLite storage."""
    path = tmp_path / "technical.sqlite3"
    journal = TechnicalJournal(path, 65_536)
    try:
        for index in range(120):
            journal.emit({"message": "x" * 2048, "number": index})
        status = wait_head(journal, 120)
        assert status["usedBytes"] <= 65_536
        assert int(status["oldestSequence"]) > 1
        assert status["health"]["droppedTotal"] == 0
        _, rows = journal.page()
        assert [int(row["sequence"]) for row in rows] == list(range(int(status["oldestSequence"]), 121))
    finally:
        journal.close()
    reopened = TechnicalJournal(path, 65_536)
    try:
        assert reopened.status()["cursor"] == status["cursor"]
        reopened.emit({"message": "after restart"})
        assert wait_head(reopened, 121)["journalEpoch"] == status["journalEpoch"]
    finally:
        reopened.close()


class FailingTool:
    """Expose deliberate upstream errors containing material forbidden in diagnostics."""

    async def call_tool(self, name, arguments, meta=None):
        """Return a real MCP tool error containing synthetic private data."""
        assert name == "read"
        return types.CallToolResult(isError=True, content=[
            types.TextContent(type="text", text="fixture-private-result")
        ])


def rpc(client, path, method, params=None, headers=HEADERS):
    """Send one authenticated request to the real MCP SDK surface."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(path, json=body, headers=headers)


def test_external_collection_and_agent_diagnosis(tmp_path, monkeypatch):
    """Poll with a real Host/token boundary and correlate a classified MCP failure."""
    async def run_backend(name, config, timeout, publish, authorization, *, probe):
        """Publish one deterministic synthetic backend and wait for shutdown."""
        tool = types.Tool(name="read", inputSchema={"type": "object"})
        publish(name, FailingTool(), {"local.read": tool}, "ready")
        await asyncio.Event().wait()

    monkeypatch.setattr("gateway.connections.run_backend", run_backend)
    profile = Profile.model_validate({
        "name": "fixture", "management_bridge": True, "access_token_env": "FIXTURE_GATEWAY",
        "backends": {"local": {"transport": "stdio", "command": "fixture"}},
    })
    journal = TechnicalJournal(tmp_path / "technical.sqlite3", 65_536)
    events = TechnicalEvents(journal, profile.endpoint_key)
    router = Router(profile, Telemetry(Journal(tmp_path, 4096), technical=events))
    settings = ToolPolicySettings(router, tmp_path / "tool-policy.json")
    app = create_app(router, PORT, "fixture-gateway", settings=settings, technical=journal)
    try:
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 51200)) as client:
            assert client.get(METRICS).status_code == 401
            assert client.get(METRICS, headers={**HEADERS, "Origin": "https://outside.invalid"}).status_code == 403
            assert client.get(METRICS, headers={**HEADERS, "Host": "outside.invalid"}).status_code == 403
            for path in (METRICS, LOGS + "/status", LOGS + "/events"):
                assert client.get(path, headers={**HEADERS, "Sec-Fetch-Site": "cross-site"}).status_code == 403
            assert client.get(METRICS + "?q=1", headers=HEADERS).status_code == 400
            assert client.get(LOGS + "/events?limit=%FF", headers=HEADERS).status_code == 400
            assert client.get(LOGS + "/events?limit=1&limit=2", headers=HEADERS).status_code == 400
            baseline = wait_head(journal, 3)
            trace = "123456789abcdef0123456789abcdef0"
            request = {"name": "local.read", "arguments": {"fixture-private-argument": "private"}}
            call_headers = {**HEADERS, "traceparent": f"00-{trace}-0123456789abcdef-01"}
            result = rpc(client, "/mcp/fixture", "tools/call", request, call_headers)
            assert result.json()["result"]["isError"]
            final = wait_head(journal, 4)

            response = client.get(LOGS + "/status", headers=HEADERS)
            assert response.headers["Cache-Control"] == "no-store"
            assert response.json()["cursor"] == final["cursor"]
            params = {"journalEpoch": baseline["journalEpoch"], "afterSequence": baseline["latestSequence"]}
            page = client.get(LOGS + "/events", headers=HEADERS, params=params).json()
            assert page["schemaVersion"] == "technical-logs-http/v1"
            assert page["nextCursor"] == page["highWatermark"] == final["cursor"]
            event, = page["events"]
            assert event["traceId"] == trace and event["level"] == "ERROR"
            assert event["properties"]["errorCode"] == "upstream_tool_error"
            serialized = json.dumps(page)
            assert all(secret not in serialized for secret in (
                "fixture-private-argument", "fixture-private-result", "fixture-gateway", "traceparent"
            ))

            params = {"journalEpoch": "expired", "afterSequence": "0"}
            stale = client.get(LOGS + "/events", headers=HEADERS, params=params)
            assert stale.status_code == 409 and stale.json()["resetCursor"] == final["cursor"]
            latest = client.get(LOGS + "/events?limit=1", headers=HEADERS).json()
            assert latest["hasMore"] and latest["nextCursor"] == final["cursor"]
            params = {"journalEpoch": latest["nextCursor"]["journalEpoch"],
                      "beforeSequence": latest["nextCursor"]["sequence"]}
            older = client.get(LOGS + "/events", headers=HEADERS, params=params).json()
            assert [row["sequence"] for row in older["events"]] == ["1", "2", "3"]

            metrics = client.get(METRICS, headers=HEADERS).json()
            assert metrics["schemaVersion"] == "service-diagnostics/v1"
            assert metrics["gateway"]["received"] == metrics["gateway"]["upstream"] == metrics["gateway"]["errors"] == 1
            assert metrics["process"]["uptimeSeconds"] > 0
            assert "cpuUserSeconds" in metrics["process"] and metrics["storage"]["journalBytes"] > 0
            tools = rpc(client, "/manage/fixture", "tools/list").json()["result"]["tools"]
            assert {item["name"] for item in tools if item["annotations"]["readOnlyHint"]} >= {
                "gateway_read_errors", "gateway_read_metrics"
            }
            args = {"name": "gateway_read_errors", "arguments": {"trace_id": trace}}
            diagnosis = rpc(client, "/manage/fixture", "tools/call", args).json()
            assert diagnosis["result"]["structuredContent"]["events"] == [event]
            stale_args = {"trace_id": trace, "journal_epoch": "expired", "before_sequence": 0}
            stale = rpc(client, "/manage/fixture", "tools/call",
                        {"name": "gateway_read_errors", "arguments": stale_args}).json()
            assert stale["result"]["structuredContent"]["error"] == "cursor_expired"
            assert "omitted" in stale["result"]["structuredContent"]["message"]
            assert rpc(client, "/manage/fixture", "tools/call", args).json()["result"]["structuredContent"]["events"] == [event]
            args = {"name": "gateway_read_metrics", "arguments": {}}
            metric_tool = rpc(client, "/manage/fixture", "tools/call", args).json()
            assert metric_tool["result"]["structuredContent"]["gateway"] == metrics["gateway"]
            assert journal.status()["cursor"] == final["cursor"]

            with sqlite3.connect(journal.path) as connection:
                connection.execute("UPDATE events SET payload=? WHERE sequence=4", (b"{}",))
            assert client.get(LOGS + "/events", headers=HEADERS).status_code == 503
            bad = rpc(client, "/manage/fixture", "tools/call", {"name": "gateway_read_errors", "arguments": {}})
            assert bad.json()["result"]["isError"]

        with TestClient(app, base_url=ORIGIN, client=("192.0.2.42", 51200)) as remote:
            assert remote.get(METRICS, headers=HEADERS).status_code == 403
    finally:
        journal.close()


def test_collector_byte_limit_preserves_continuation():
    """Check byte-bound forward and backwards pages with heterogeneous row sizes."""
    from gateway.telemetry_api import EventQuery, log_page

    cursor = {"journalEpoch": "epoch", "sequence": "100"}
    status = {"cursor": cursor, "oldestSequence": "1", "health": {"status": "ok"}}
    rows = [{"sequence": str(index), "message": "Ж" * 5000} for index in range(1, 101)]
    forward = log_page(EventQuery(100, "epoch", 0, None), status, rows)
    backward = log_page(EventQuery(100, None, None, None), status, rows)
    for response, edge in ((forward, -1), (backward, 0)):
        payload = json.loads(response.body)
        assert len(response.body) <= 262_144 and payload["truncated"] and payload["hasMore"]
        assert payload["nextCursor"]["sequence"] == payload["events"][edge]["sequence"]
    assert json.loads(forward.body)["events"][0]["sequence"] == "1"
    assert json.loads(backward.body)["events"][-1]["sequence"] == "100"


def test_call_journal_file_cap_and_idle_expiry(tmp_path, monkeypatch):
    """Apply the 50 MB file ceiling to legacy quotas and expire an idle segment."""
    import os

    from gateway.http_app import maintain_call_journal
    from gateway.telemetry import LOG_FILE_MAX_BYTES

    old = tmp_path / "calls.2.jsonl"
    with old.open("wb") as source:
        source.truncate(LOG_FILE_MAX_BYTES + 1)
    journal = Journal(tmp_path, 1_000_000_000)
    assert journal.segment_bytes == LOG_FILE_MAX_BYTES
    assert not old.exists()
    journal.write({"tool": "fixture.read"})
    old = journal.paths[1]
    journal.paths[0].rename(old)
    os.utime(old, (100, 100))
    journal.write({"tool": "fixture.read"})
    monkeypatch.setattr("gateway.http_app.LOG_RETENTION_INTERVAL_SECONDS", 0.005)

    async def expire():
        """Run the same cleanup task the idle daemon starts."""
        cleanup = asyncio.create_task(maintain_call_journal(journal, 7))
        try:
            async with asyncio.timeout(2):
                while old.exists():
                    await asyncio.sleep(0.005)
        finally:
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)

    asyncio.run(expire())
    assert journal.paths[0].exists()


def test_cli_logs_reads_recent_matches_before_scanning_retained_history(tmp_path, monkeypatch, capsys):
    """Keep chronological filtered output across chunks and rotations, skipping incomplete records."""
    from gateway import cli

    logs = tmp_path / "logs"
    logs.mkdir()
    matching = {"tool": "demo.lookup", "session_tag": "tag", "trace_id": "trace"}

    def row(number):
        """Encode one complete matching record for the test journal."""
        return json.dumps({**matching, "n": number}).encode() + b"\n"

    (logs / "calls.2.jsonl").write_bytes(row(0) * 1_000)
    (logs / "calls.1.jsonl").write_bytes(row(1) + row(2))
    (logs / "calls.0.jsonl").write_bytes(
        row(3) + b'{"tool":"other.lookup"}\n' + b"invalid \xff\n" + b"x" * 256 + b"\n" + row(4) + row(5).rstrip()
    )
    monkeypatch.setattr(cli, "_LOG_READ_CHUNK", 17)
    monkeypatch.setattr(cli, "_LOG_MAX_LINE_BYTES", 128)
    original_loads = json.loads
    parsed = []

    def counted_loads(value):
        """Observe which rows reached JSON parsing without altering the decoder."""
        parsed.append(value)
        return original_loads(value)

    monkeypatch.setattr(cli.json, "loads", counted_loads)
    cli.show_logs(tmp_path, 3, "demo", "tag", "trace")
    assert capsys.readouterr().out == (row(2) + row(3) + row(4)).decode()
    assert len(parsed) == 4


def test_telemetry_bounds_retired_stats_and_preserves_global_counts(tmp_path, monkeypatch):
    """Keep live and denied counters, with a stable bound for departed names."""
    telemetry = Telemetry(Journal(tmp_path, 65_536))
    events = []
    monkeypatch.setattr(telemetry.journal, "write", events.append)
    live = {"demo.live"}
    denied = {"demo.denied"}
    for name in ("demo.live", "demo.denied", "(unknown)", *(f"demo.old{n}" for n in range(300))):
        telemetry.completed(name, "ok", 1, False, 0, 0)
    telemetry.prune_tools(live, denied)

    retained = list(telemetry.tools)
    assert retained == ["demo.live", "demo.denied", "(unknown)", *(f"demo.old{n}" for n in range(44, 300))]
    telemetry.completed("demo.old0", "ok", 1, False, 0, 0, track_tool=False)
    telemetry.completed("demo.old44", "ok", 1, False, 0, 0, track_tool=False)
    assert list(telemetry.tools) == retained
    assert telemetry.tools["demo.old44"].received == 2
    assert telemetry.total.received == 305
    assert len(events) == 305
