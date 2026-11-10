"""Protect the management surface, durable tool policies and token source priority."""

import asyncio
import json
from base64 import b64encode

import pytest
from mcp import types
from starlette.testclient import TestClient

from gateway.credentials import SecretStoreError
from gateway.http_app import create_app
from gateway.policy_settings import PolicyChange, ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Router
from gateway.telemetry import Journal, Telemetry
from gateway.token_secrets import resolve_token

ORIGIN = "http://127.0.0.1:18793"
GATEWAY = "fixture-gateway-token"
HEADERS = {"Authorization": f"Bearer {GATEWAY}", "Accept": "application/json, text/event-stream"}


class Tokens:
    """Return a fixture token and simulate access denial without touching the default Keychain."""

    def __init__(self, value):
        self.value = value

    def read_token(self, key):
        """Read only the named fixture item."""
        assert key == "local-gateway"
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def test_os_token_precedes_explicit_environment_fallback(monkeypatch):
    """Use OS first, fallback only on missing item/provider, never on denial or corrupt data."""
    monkeypatch.setenv("GATEWAY_ENV", "fixture-env-value")
    assert resolve_token("local-gateway", "GATEWAY_ENV", Tokens("fixture-os-value")) == "fixture-os-value"
    assert resolve_token("local-gateway", "GATEWAY_ENV", Tokens(None)) == "fixture-env-value"
    assert resolve_token("local-gateway", "GATEWAY_ENV", None) == "fixture-env-value"
    assert resolve_token(None, "GATEWAY_ENV", None) == "fixture-env-value"
    with pytest.raises(SecretStoreError):
        resolve_token("local-gateway", "GATEWAY_ENV", Tokens(SecretStoreError("denied")))
    with pytest.raises(SecretStoreError):
        resolve_token("local-gateway", "GATEWAY_ENV", Tokens("unsafe newline\n"))
    with pytest.raises(ValueError, match="missing"):
        resolve_token("local-gateway", None, Tokens(None))


def test_profile_requires_a_token_for_management_and_accepts_os_only():
    """Reject unauthenticated management and accept a Keychain reference in production."""
    profile = {"name": "fixture", "management_bridge": True, "backends": {
        "local": {"transport": "stdio", "command": "fixture"},
    }}
    with pytest.raises(ValueError, match="requires a gateway token"):
        Profile.model_validate(profile)
    configured = Profile.model_validate({**profile, "access_token_keychain": "local-gateway",
                                         "environment": "production"})
    assert configured.access_token_env is None
    assert configured.access_token_keychain == "local-gateway"


def rpc(client, path, method, params=None, headers=HEADERS):
    """Call the real stateless HTTP MCP surface with a deterministic JSON-RPC envelope."""
    request = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    return client.post(path, headers=headers, json=request)


def call(client, path, name, arguments):
    """Extract the typed tool result from the real SDK server response."""
    response = rpc(client, path, "tools/call", {"name": name, "arguments": arguments})
    return response.json()["result"]


def test_management_policy_cas_auth_and_restart(tmp_path, monkeypatch):
    """Use separate authenticated catalogs and recover saved policy after app restart."""
    profile_data = {
        "name": "fixture", "environment": "test", "management_bridge": True,
        "access_token_env": "FIXTURE_GATEWAY",
        "backends": {"local": {"transport": "stdio", "command": "fixture"}},
    }
    manage_path = "/manage/test/fixture"
    proxy_path = "/mcp/test/fixture"

    async def run_backend(name, config, timeout, publish, authorization, *, probe):
        """Publish safe fixture descriptors and wait for normal shutdown."""
        read = types.Tool(name="read", inputSchema={"type": "object"},
                          annotations=types.ToolAnnotations(readOnlyHint=True))
        write = types.Tool(name="write", inputSchema={"type": "object"})
        publish(name, object(), {"local.read": read, "local.write": write}, "ready")
        await asyncio.Event().wait()

    def app_for_profile():
        """Create a fresh router using the original profile and persisted state."""
        router = Router(Profile.model_validate(profile_data), Telemetry(Journal(tmp_path / "logs", 4096)))
        settings = ToolPolicySettings(router, tmp_path / "tool-policy.json")
        return create_app(router, 18793, GATEWAY, settings=settings), settings

    monkeypatch.setattr("gateway.connections.run_backend", run_backend)
    app, settings = app_for_profile()
    with TestClient(app, base_url=ORIGIN) as client:
        assert rpc(client, manage_path, "tools/list", headers={"Accept": HEADERS["Accept"]}).status_code == 401
        basic = "Basic " + b64encode(f"gateway:{GATEWAY}".encode()).decode()
        assert rpc(client, manage_path, "tools/list", headers={**HEADERS, "Authorization": basic}).status_code == 401
        assert rpc(client, manage_path, "tools/list", headers={**HEADERS, "Origin": "https://outside.invalid"}).status_code == 403
        names = {tool["name"] for tool in rpc(client, manage_path, "tools/list").json()["result"]["tools"]}
        assert names == {"gateway_inspect", "gateway_set_tool_policy", "gateway_reconnect_basic"}
        proxy = rpc(client, proxy_path, "tools/list").json()["result"]["tools"]
        assert {tool["name"] for tool in proxy} == {"local.read", "local.write"}
        initial = call(client, manage_path, "gateway_inspect", {"limit": 1})["structuredContent"]
        assert initial["views"]["settings"] == ORIGIN + "/settings" and initial["next_offset"] == 1
        assert GATEWAY not in json.dumps(initial) and "FIXTURE_GATEWAY" not in json.dumps(initial)
        forbidden = call(client, manage_path, "gateway_set_tool_policy",
                         {"tool": "local.write", "policy": "singleflight", "expected_revision": 0})
        assert forbidden["isError"]
        command = {"tool": "local.read", "policy": "deny", "expected_revision": 0}
        assert call(client, manage_path, "gateway_set_tool_policy", command)["structuredContent"]["revision"] == 1
        assert call(client, manage_path, "gateway_set_tool_policy", command)["isError"]
        assert call(client, proxy_path, "local.read", {})["isError"]
        assert "Запрещён" in client.get("/settings", headers=HEADERS).text

        def fail_write(_state):
            """Simulate a disk failure before the replacement."""
            raise OSError("fixture")

        monkeypatch.setattr(settings, "_write", fail_write)
        assert call(client, manage_path, "gateway_set_tool_policy",
                    {**command, "policy": "allow", "expected_revision": 1})["isError"]
        assert settings.state.revision == 1 and "local.read" in settings.router.profile.denied

    app, settings = app_for_profile()
    with TestClient(app, base_url=ORIGIN) as client:
        current = call(client, manage_path, "gateway_inspect", {})["structuredContent"]
        assert current["revision"] == 1 and current["tools"][0]["policy"] == "deny"
        assert call(client, manage_path, "gateway_set_tool_policy",
                    {**command, "policy": "singleflight", "expected_revision": 1})["structuredContent"]["revision"] == 2
        assert settings.router.profile.singleflight == {"local.read"}
        assert settings.router.profile.denied == set()


def test_upstream_bearer_uses_os_item_before_environment(tmp_path, monkeypatch):
    """The resolved OS value is used only by the configured upstream transport."""
    monkeypatch.setenv("UPSTREAM_ENV", "fixture-env-value")
    seen = []

    async def run_backend(name, config, timeout, publish, authorization, *, probe):
        """Record the fixture authorization selected before transport startup."""
        seen.append(authorization)
        publish(name, object(), {}, "ready")
        await asyncio.Event().wait()

    monkeypatch.setattr("gateway.connections.run_backend", run_backend)
    profile = Profile.model_validate({"name": "fixture", "backends": {"remote": {
        "transport": "http", "url": "https://mcp.example.invalid/",
        "bearer_token_env": "UPSTREAM_ENV", "bearer_token_keychain": "local-gateway",
    }}})
    router = Router(profile, Telemetry(Journal(tmp_path, 4096)), store=Tokens("fixture-os-value"))
    with TestClient(create_app(router, 18793, None), base_url=ORIGIN) as client:
        assert seen == ["Bearer fixture-os-value"]
        status = client.get("/api/status/fixture").text
        assert "fixture-os-value" not in status and "UPSTREAM_ENV" not in status
        assert client.post("/manage/fixture").status_code == 404
