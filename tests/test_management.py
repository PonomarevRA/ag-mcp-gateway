"""Protect the management surface, durable tool policies and token source priority."""

import asyncio
import json
import re
from base64 import b64encode

import pytest
from mcp import types
from starlette.testclient import TestClient

from gateway.cli import config_fragment, parser
from gateway.credentials import SecretStoreError
from gateway.http_app import create_app
from gateway.management import InspectQuery, Management
from gateway.policy_settings import PolicyChange, ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Route, Router
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
        assert names == {"gateway_inspect", "gateway_set_tool_policy", "gateway_reconnect_basic", "gateway_reconnect_mcp"}
        proxy = rpc(client, proxy_path, "tools/list").json()["result"]["tools"]
        assert {tool["name"] for tool in proxy} == {"local.read", "local.write"}
        initial = call(client, manage_path, "gateway_inspect", {"limit": 1})["structuredContent"]
        assert initial["views"]["settings"] == ORIGIN + "/settings" and initial["next_offset"] == 1
        assert initial["connections"][0]["last_call_at"] is None
        assert GATEWAY not in json.dumps(initial) and "FIXTURE_GATEWAY" not in json.dumps(initial)
        forbidden = call(client, manage_path, "gateway_set_tool_policy",
                         {"tool": "local.write", "policy": "singleflight", "expected_revision": 0})
        assert forbidden["isError"]
        command = {"tool": "local.read", "policy": "deny", "expected_revision": 0}
        assert call(client, manage_path, "gateway_set_tool_policy", command)["structuredContent"]["revision"] == 1
        assert call(client, manage_path, "gateway_set_tool_policy", command)["isError"]
        assert call(client, proxy_path, "local.read", {})["isError"]
        settings_page = client.get("/settings", headers=HEADERS).text
        assert "Запрещён" in settings_page and "Подключён без входа" in settings_page
        assert "вызовов с запуска пока нет" in settings_page

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


def test_saved_management_singleflight_is_suspended_when_read_only_hint_changes(tmp_path):
    """A changed upstream catalog must not coalesce a tool that can now write."""
    class Slow:
        async def call_tool(self, name, arguments, *, meta=None):
            """Keep concurrent fixture calls in flight together."""
            await asyncio.sleep(0.01)
            return types.CallToolResult(content=[])

    def router_for_hint(read_only):
        """Use a fresh profile and rediscovered tool after each restart."""
        profile = Profile.model_validate({"name": "fixture", "backends": {
            "local": {"transport": "stdio", "command": "fixture"},
        }})
        router = Router(profile, Telemetry(Journal(tmp_path / "logs", 4096)))
        tool = types.Tool(name="read", inputSchema={"type": "object"},
                          annotations=types.ToolAnnotations(readOnlyHint=read_only))
        router.routes["local.read"] = Route(tool, Slow())
        return router

    path = tmp_path / "tool-policy.json"
    original = ToolPolicySettings(router_for_hint(True), path)
    original.change(PolicyChange(tool="local.read", policy="singleflight", expected_revision=0))

    restored = router_for_hint(False)
    settings = ToolPolicySettings(restored, path)
    management = Management(restored, settings, ORIGIN, False)
    tool = management.inspect(InspectQuery())["tools"][0]
    assert tool["policy"] == "singleflight" and tool["effective_policy"] == "allow"
    assert not restored.snapshot()["catalog"][0]["singleflight"]

    async def scenario():
        """Verify two independently dispatched calls after the hint changes."""
        await asyncio.gather(restored.call("local.read", {}), restored.call("local.read", {}))
        assert restored.telemetry.snapshot()["total"]["upstream"] == 2

    asyncio.run(scenario())



def test_management_config_names_are_distinct_from_ordinary_clients(tmp_path):
    """Use a reserved client namespace and separate different named profiles."""
    def fragment(name, management=False):
        """Render a client entry exactly as the config command would."""
        profile = Profile.model_validate({"name": name, "environment": "test", "management_bridge": True,
            "access_token_keychain": "local-gateway",
            "backends": {"local": {"transport": "stdio", "command": "fixture"}},
        })
        argv = ["--profiles", str(tmp_path), "--profile", name, "config", "codex"]
        if management:
            argv.append("--management")
        args = parser().parse_args(argv)
        args.port = profile.resolve_port(None)
        return config_fragment(args, profile)

    managed = fragment("x", True)
    ordinary = fragment("x-manage")
    assert "[mcp_servers.amg-mgmt-test-x]" in managed
    assert "[mcp_servers.amg-test-x-manage]" in ordinary
    assert "mcp-bridge" in managed and "local-gateway" not in managed


def test_browser_policy_uses_shared_revision_and_protected_form(tmp_path, monkeypatch):
    """Edit a filtered policy in the browser and recover when Bridge changes its revision."""
    async def run_backend(name, config, timeout, publish, authorization, *, probe):
        """Publish a page and a second-page tool with a known read-only annotation."""
        size = 101 if name == "local" else 1
        tools = {}
        for index in range(size):
            tool = types.Tool(name=f"read{index:03}", inputSchema={"type": "object"},
                              annotations=types.ToolAnnotations(readOnlyHint=index != 0))
            tools[f"{name}.{tool.name}"] = tool
        publish(name, object(), tools, "ready")
        await asyncio.Event().wait()

    monkeypatch.setattr("gateway.connections.run_backend", run_backend)
    profile = Profile.model_validate({
        "name": "fixture", "access_token_env": "FIXTURE_GATEWAY", "management_bridge": True,
        "backends": {
            "local": {"transport": "stdio", "command": "fixture"},
            "other": {"transport": "stdio", "command": "fixture"},
        },
        "denied": ["other.missing"],
    })
    router = Router(profile, Telemetry(Journal(tmp_path / "logs", 4096)))
    settings = ToolPolicySettings(router, tmp_path / "policy.json")
    app = create_app(router, 18793, GATEWAY, settings=settings)
    browser = {"Authorization": "Basic " + b64encode(f"gateway:{GATEWAY}".encode()).decode()}
    same_origin = {**browser, "Origin": ORIGIN}
    manage = "/manage/fixture"

    with TestClient(app, base_url=ORIGIN) as client:
        initial = client.get("/settings?backend=local&lang=en", headers=browser)
        assert "Showing 1–100 of 101" in initial.text
        assert "other.missing" not in initial.text
        assert 'href="/settings?backend=local&amp;offset=100#tool-policy"' in initial.text
        target = "/settings?backend=local&offset=100"
        last = client.get(target + "000", headers=browser)
        assert "Showing 101–101 of 101" in last.text
        assert "Policy for local.read100" in last.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', last.text)[1]
        data = {"csrf": csrf, "tool": "local.read100", "policy": "deny", "expected_revision": "0"}
        assert client.post(target, headers=browser, data=data).status_code == 403
        assert client.post(target, headers={**HEADERS, "Origin": ORIGIN}, data=data).status_code == 401
        assert client.post(target, headers=same_origin, data={**data, "csrf": "wrong"}).status_code == 403
        assert settings.state.revision == 0

        saved = client.post(target, headers=same_origin, data=data, follow_redirects=False)
        assert saved.status_code == 303
        assert saved.headers["location"].startswith(target + "&saved=")
        confirmed = client.get(saved.headers["location"], headers=browser)
        assert "Policy saved." in confirmed.text
        assert "local.read100</strong> · Denied" in confirmed.text
        inspected = call(client, manage, "gateway_inspect", {"backend": "local", "offset": 100})["structuredContent"]
        assert inspected["revision"] == 1 and inspected["tools"][0]["policy"] == "deny"

        change = {"tool": "local.read100", "policy": "allow", "expected_revision": 1}
        assert call(client, manage, "gateway_set_tool_policy", change)["structuredContent"]["revision"] == 2
        stale = client.post(target, headers=same_origin, data={**data, "expected_revision": "1"})
        assert stale.status_code == 409
        assert "The policy has changed." in stale.text
        assert 'name="expected_revision" value="2"' in stale.text
        assert "local.read100</strong> · Allowed" in stale.text

        unsupported = client.post(target, headers=same_origin, data={
            **data, "tool": "local.read000", "policy": "singleflight", "expected_revision": "2",
        })
        assert unsupported.status_code == 400
        assert "This policy is currently unavailable for the tool." in unsupported.text
        assert settings.state.revision == 2

        def fail_write(_state):
            """Reject a browser save without exposing storage errors."""
            raise OSError("fixture storage path")

        monkeypatch.setattr(settings, "_write", fail_write)
        storage_error = client.post(target, headers=same_origin,
                                    data={**data, "expected_revision": "2"})
        assert storage_error.status_code == 503
        assert "Unable to save the policy. Try again later." in storage_error.text
        assert "fixture storage path" not in storage_error.text
        assert settings.state.revision == 2

        other = client.get("/settings?backend=other&lang=ru", headers=browser)
        assert "other.missing</strong> · Запрещён · Нет в каталоге" in other.text
