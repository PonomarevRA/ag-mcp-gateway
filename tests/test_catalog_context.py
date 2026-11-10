"""Verify catalog states and bounded session forwarding independently of UI rendering."""

import asyncio
import json

import httpx
import pytest
from mcp import types

from gateway.connection_diagnostics import ConnectionProbe
from gateway.dashboard import render
from gateway.profile import Profile
from gateway.router import Route, Router
from gateway.session_context import SESSION_META, SessionContext
from gateway.telemetry import Journal, Telemetry

_IDENTITY = {"harness": "fixture", "instanceId": "local-fixture", "remoteSessionId": "fixture-chat-123"}


class RecordingSession:
    """Capture what the selected upstream actually receives."""

    def __init__(self):
        """Start with no dispatched calls."""
        self.calls = []

    async def call_tool(self, name, arguments, *, meta=None):
        """Keep exact arguments and metadata for the synthetic assertion."""
        self.calls.append((name, arguments, meta))
        return types.CallToolResult(content=[])


def test_local_context_maps_missing_args_and_journal_uses_tag(tmp_path):
    """Explicit args win, only configured local tools receive the reserved context."""
    mapping = {"harness": "harness", "instance_id": "instanceId", "remote_session_id": "remoteSessionId"}
    profile = Profile.model_validate({
        "name": "fixture",
        "backends": {
            "tasks": {"transport": "stdio", "command": "unused", "forward_session_context": True,
                      "session_args": {"tasks_resolve_conversation": mapping}},
            "remote": {"transport": "http", "url": "https://remote.example.com/mcp"},
        },
    })
    router = Router(profile, Telemetry(Journal(tmp_path, 4096)))
    local = RecordingSession()
    remote = RecordingSession()
    tool = types.Tool(name="tasks_resolve_conversation", inputSchema={
        "type": "object", "properties": {name: {"type": "string"} for name in mapping},
    })
    router.routes["tasks.tasks_resolve_conversation"] = Route(tool, local)
    router.routes["remote.tasks_resolve_conversation"] = Route(tool, remote)

    async def scenario():
        """Compare local, remote and malformed requests against the same configuration."""
        meta = {SESSION_META: _IDENTITY, "other.example/context": "ordinary extension", "progressToken": 17}
        explicit = {"harness": "manual"}
        assert not (await router.call("tasks.tasks_resolve_conversation", explicit, metadata=meta)).isError
        assert explicit == {"harness": "manual"}
        assert local.calls == [(tool.name, {"harness": "manual", "instance_id": "local-fixture",
                                           "remote_session_id": "fixture-chat-123"},
                                {SESSION_META: _IDENTITY, "other.example/context": "ordinary extension"})]
        assert not (await router.call("remote.tasks_resolve_conversation", {}, metadata=meta)).isError
        assert remote.calls == [(tool.name, {}, {"other.example/context": "ordinary extension"})]
        invalid = {SESSION_META: {"sessionKey": "unsafe"}}
        assert (await router.call("tasks.tasks_resolve_conversation", {}, metadata=invalid)).isError
        assert len(local.calls) == 1
        schema = types.Tool(name=tool.name, inputSchema={"type": "object"})
        router.routes["tasks.tasks_resolve_conversation"] = Route(schema, local)
        assert (await router.call("tasks.tasks_resolve_conversation", {}, metadata=meta)).isError
        assert len(local.calls) == 1

    asyncio.run(scenario())
    contents = (tmp_path / "calls.0.jsonl").read_text()
    assert "fixture-chat-123" not in contents and "local-fixture" not in contents
    events = [json.loads(line) for line in contents.splitlines()]
    tag = SessionContext.parse(_IDENTITY).tag
    assert events[0]["session_tag"] == events[1]["session_tag"] == tag
    assert SessionContext.parse({**_IDENTITY, "conversationId": "fixture-binding"}).tag == tag


@pytest.mark.parametrize("feature", [
    {"forward_session_context": True},
    {"session_args": {"read": {"chat": "remoteSessionId"}}},
])
def test_identity_configuration_requires_local_mcp(feature):
    """Remote HTTP profiles cannot opt in to gateway session provenance forwarding."""
    with pytest.raises(ValueError, match="requires a local MCP"):
        Profile.model_validate({"name": "fixture", "backends": {
            "remote": {"transport": "http", "url": "https://remote.example.com/mcp", **feature},
        }})


@pytest.mark.parametrize(("status", "code"), [
    (401, "auth-rejected"), (403, "access-denied"), (404, "invalid-endpoint"),
    (405, "invalid-endpoint"), (302, "invalid-endpoint"), (500, "upstream-http"),
])
def test_http_status_diagnostic_ignores_response_text(status, code):
    """Use verified status codes without echoing an arbitrary upstream response."""
    async def scenario():
        """A POST response is evidence of the selected transport outcome."""
        probe = ConnectionProbe()
        await probe.response(httpx.Response(status, request=httpx.Request("POST", "http://127.0.0.1/mcp"),
                                            text="fixture-raw-response"))
        assert probe.failure(None) == code

    asyncio.run(scenario())


def test_catalog_distinguishes_local_access_and_authenticated_failure(tmp_path):
    """Show state and actions from observed MCP readiness, not from configured credentials."""
    profile = Profile.model_validate({
        "name": "fixture", "access_token_env": "FIXTURE_TOKEN",
        "backends": {
            "tasks": {"transport": "stdio", "command": "unused"},
            "grafana": {"transport": "http", "url": "https://grafana.example.com/mcp", "basic_keychain": True},
        },
    })
    router = Router(profile, Telemetry(Journal(tmp_path, 4096)))
    router.statuses.update(tasks="ready", grafana="auth-rejected")
    router.routes["tasks.read"] = Route(types.Tool(name="read", description="Task fixture", inputSchema={}),
                                        RecordingSession())
    page = render(router.snapshot(), "", credentials=True)
    assert "Подключён без входа" in page and "HTTP 401" in page
    assert 'href="/credentials?backend=grafana"' in page and 'href="#tools-tasks"' in page
    filtered = render(router.snapshot(), "non-matching", credentials=True)
    assert "Инструменты не найдены" in filtered and 'href="#tools-tasks"' not in filtered


def test_context_cannot_be_used_as_a_lease_key():
    """Reject mappings that would reinterpret session provenance as credentials."""
    with pytest.raises(ValueError, match="cannot populate credentials or lease keys"):
        Profile.model_validate({
            "name": "fixture", "backends": {"tasks": {
                "transport": "stdio", "command": "unused",
                "session_args": {"tasks_work_start": {"session_key": "remoteSessionId"}},
            }},
        })


def test_cli_identity_accepts_dash_prefixed_remote_id():
    """A generated stdio command preserves opaque values starting with a dash."""
    from gateway.cli import parser
    from gateway.session_context import session_from_cli

    context = SessionContext.parse({"remoteSessionId": "-opaque"})
    args = parser().parse_args(["stdio", *context.cli_arguments()])
    assert session_from_cli(args) == context
