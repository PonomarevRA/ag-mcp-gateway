"""Check the W3C carrier, request scoping and metadata-only journal."""

import asyncio
import json

import httpx
import pytest
from mcp import types
from starlette.datastructures import Headers

from gateway.cli import show_logs
from gateway.profile import Profile
from gateway.router import Route, Router
from gateway.session_context import SESSION_META, SessionContext
from gateway.telemetry import Journal, Telemetry
from gateway.trace_context import TraceContext, inject_http_trace, with_http_trace

TRACE_ID = "0123456789abcdef0123456789abcdef"
PARENT = f"00-{TRACE_ID}-0123456789abcdef-01"
OTHER_PARENT = "00-fedcba9876543210fedcba9876543210-abcdef0123456789-00"


@pytest.mark.parametrize("parent", [
    "not-a-trace", PARENT.upper(), PARENT + "-extension", PARENT.replace("00-", "ff-", 1),
    f"00-{'0' * 32}-0123456789abcdef-01", f"00-{TRACE_ID}-{'0' * 16}-01",
])
def test_invalid_traceparents_are_ignored(parent):
    """Ignore invalid version, shape, hex or IDs without making tools unavailable."""
    assert TraceContext.from_metadata({"traceparent": parent}) is None


def test_headers_replace_metadata_without_mixing_vendor_state():
    """Header and MCP carriers each keep their own optional state."""
    metadata = {"traceparent": OTHER_PARENT, "tracestate": "other=meta", "custom": "retained"}
    headers = Headers(raw=[(b"traceparent", PARENT.encode()), (b"tracestate", b"vendor=one"),
                           (b"tracestate", b"system=two")])
    merged = with_http_trace(metadata, headers)
    assert merged["custom"] == "retained"
    assert TraceContext.from_metadata(merged).carrier() == {
        "traceparent": PARENT, "tracestate": "vendor=one,system=two",
    }
    invalid = with_http_trace(metadata, Headers({"traceparent": "invalid"}))
    assert TraceContext.from_metadata(invalid) is None
    duplicate = Headers(raw=[(b"traceparent", PARENT.encode()), (b"traceparent", OTHER_PARENT.encode())])
    assert with_http_trace(metadata, duplicate) == {"custom": "retained"}
    assert with_http_trace(None, duplicate) is None
    assert with_http_trace(metadata, Headers({"tracestate": "ignore=unpaired"})) == metadata


@pytest.mark.parametrize("state", ["vendor=one,vendor=two", "vendor=malicious\r\nheader", "vendor=a=b",
                                  "invalid", "vendor=" + "a" * 507])
def test_invalid_tracestate_keeps_valid_traceparent(state):
    """Optional vendor state cannot invalidate a good trace ID."""
    trace = TraceContext.from_metadata({"traceparent": PARENT, "tracestate": state})
    assert trace.carrier() == {"traceparent": PARENT}


def test_http_hook_injects_only_typed_tool_metadata():
    """The outgoing wire carrier belongs only to the current MCP tool message."""
    async def scenario():
        """Inspect outgoing headers without opening external sockets."""
        meta = {"traceparent": PARENT, "tracestate": "vendor=fixture"}
        for method in ("tools/call", "tools/list", "initialize"):
            request = httpx.Request("POST", "https://example.invalid/mcp",
                                    json={"method": method, "params": {"_meta": meta}})
            await inject_http_trace(request)
            if method == "tools/call":
                assert request.headers["traceparent"] == PARENT
                assert request.headers["tracestate"] == "vendor=fixture"
            else:
                assert "traceparent" not in request.headers and "tracestate" not in request.headers
        argument_only = httpx.Request("POST", "https://example.invalid/mcp",
                                      json={"method": "tools/call", "params": {"arguments": meta}})
        await inject_http_trace(argument_only)
        assert "traceparent" not in argument_only.headers

    asyncio.run(scenario())


def test_router_scopes_trace_and_retains_only_ids(tmp_path, capsys):
    """Verify opt-in, ordinary metadata, per-call isolation and composed CLI filters."""
    class RecordingSession:
        async def call_tool(self, name, arguments, *, meta=None):
            """Return the received fixture metadata after allowing concurrent dispatch."""
            await asyncio.sleep(0)
            return types.CallToolResult(content=[], structuredContent=meta or {})

    profile = Profile.model_validate({"name": "test", "backends": {
        "on": {"transport": "stdio", "command": "fixture", "forward_trace_context": True,
               "forward_session_context": True},
        "off": {"transport": "stdio", "command": "fixture"},
    }, "singleflight": ["on.read"]})
    directory = tmp_path / "test"
    router = Router(profile, Telemetry(Journal(directory / "logs", 4096)))
    tool = types.Tool(name="read", inputSchema={"type": "object"})
    router.routes["on.read"] = Route(tool, RecordingSession())
    router.routes["off.read"] = Route(tool, RecordingSession())
    session = {"harness": "fixture", "remoteSessionId": "test-session-id"}

    async def scenario():
        """Make identical calls with different traces and confirm their distinct results."""
        first = {"traceparent": PARENT, "tracestate": "vendor=fixture", SESSION_META: session}
        second = {"traceparent": OTHER_PARENT, "extension": "fixture"}
        results = await asyncio.gather(router.call("on.read", {}, metadata=first),
                                       router.call("on.read", {}, metadata=second))
        assert [result.structuredContent for result in results] == [first, second]
        assert router.telemetry.snapshot()["total"]["upstream"] == 2
        off = await router.call("off.read", {}, metadata=second)
        assert off.structuredContent == {"extension": "fixture"}

    asyncio.run(scenario())
    log = (directory / "logs" / "calls.0.jsonl").read_text()
    assert all(value not in log for value in (PARENT, OTHER_PARENT, "vendor=fixture", "test-session-id"))
    tag = SessionContext.parse(session).tag
    show_logs(directory, 30, "on", tag, TRACE_ID)
    entries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(entries) == 1 and entries[0]["trace_id"] == TRACE_ID and entries[0]["session_tag"] == tag



def test_rejected_trace_only_carriers_leave_singleflight_enabled(tmp_path):
    """Invalid HTTP or MCP trace fields cannot disable ordinary call coalescing."""
    class SlowSession:
        async def call_tool(self, name, arguments, *, meta=None):
            """Hold the flight briefly and expose any forwarded metadata."""
            await asyncio.sleep(0.01)
            return types.CallToolResult(content=[], structuredContent=meta or {})

    profile = Profile.model_validate({"name": "test", "backends": {
        "on": {"transport": "stdio", "command": "fixture", "forward_trace_context": True},
    }, "singleflight": ["on.read"]})
    router = Router(profile, Telemetry(Journal(tmp_path, 4096)))
    router.routes["on.read"] = Route(types.Tool(name="read", inputSchema={"type": "object"}), SlowSession())

    async def scenario():
        """Compare rejected HTTP trace context with an invalid MCP-only carrier."""
        header = with_http_trace({"traceparent": PARENT}, Headers({"traceparent": "invalid"}))
        direct = {"traceparent": "invalid", "tracestate": "vendor=discarded"}
        results = await asyncio.gather(router.call("on.read", {}, metadata=header),
                                       router.call("on.read", {}, metadata=direct))
        assert [result.structuredContent for result in results] == [{}, {}]
        totals = router.telemetry.snapshot()["total"]
        assert totals["upstream"] == 1 and totals["merged"] == 1
        assert "trace_id" not in (tmp_path / "calls.0.jsonl").read_text()

    asyncio.run(scenario())
