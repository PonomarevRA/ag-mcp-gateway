"""Check Basic credential rotation, its browser boundary and session draining."""

import asyncio
import re
from base64 import b64decode, b64encode

import pytest
from mcp import types
from starlette.testclient import TestClient

from gateway.credentials import SecretStoreError, basic_authorization, credential_account
from gateway.http_app import create_app
from gateway.profile import Profile
from gateway.router import Router
from gateway.telemetry import Journal, Telemetry

_ORIGIN = "http://127.0.0.1:18794"
_LOCAL_TOKEN = "synthetic-local-token"
_BROWSER = "Basic " + b64encode(f"gateway:{_LOCAL_TOKEN}".encode()).decode()
_OLD = basic_authorization("old-user", "old-password")
_NEW = basic_authorization("new-user", "new-fixture-password")


class MemoryStore:
    """Simulate OS storage without accessing the user's real keychain."""

    def __init__(self):
        """Start with a blank isolated store and writes enabled."""
        self.values = {}
        self.fail = False

    def read(self, account):
        """Return the current value or None for the requested account."""
        return self.values.get(account)

    def write(self, account, value):
        """Write the value unless the fixture simulates an OS failure."""
        if self.fail:
            raise SecretStoreError("fixture store denied")
        self.values[account] = value


class FixtureSession:
    """Return a generation marker and optionally block an in-flight tool call."""

    def __init__(self, label):
        """Give each worker a public fixture label and per-call events."""
        self.label = label
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()

    async def call_tool(self, name, arguments, *, meta=None):
        """Hold a deliberate long-running fixture call until the test releases it."""
        if arguments.get("hold"):
            self.started.set()
            await self.release.wait()
        return types.CallToolResult(content=[], structuredContent={"worker": self.label})


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """Create an authenticated profile and a deterministic upstream transport fixture."""
    sessions = {}
    tool = types.Tool(name="read", inputSchema={"type": "object"})

    async def run_backend(name, config, timeout, publish, authorization=None):
        """Validate the Basic fixture header before publishing the tool catalog."""
        label = {_OLD: "old", _NEW: "new"}.get(authorization)
        if label is None:
            publish(name, None, {}, "unavailable")
            return
        session = FixtureSession(label)
        sessions[label] = session
        publish(name, session, {f"{name}.read": tool}, "ready")
        try:
            await asyncio.Future()
        finally:
            session.closed.set()

    monkeypatch.setattr("gateway.connections.run_backend", run_backend)
    profile = Profile.model_validate({
        "name": "company", "environment": "production", "access_token_env": "LOCAL_TOKEN",
        "backends": {"grafana": {"transport": "http", "url": "https://grafana.example.com/mcp",
                                 "basic_keychain": True}},
    })
    store = MemoryStore()
    router = Router(profile, Telemetry(Journal(tmp_path, 4096)), store=store, profile_file="/synthetic/prod.json")
    account = router.connections.accounts["grafana"]
    store.values[account] = _OLD
    return router, store, account, sessions


def test_dashboard_verifies_then_saves_and_reuses_same_gateway_token(gateway):
    """Reject wrong origin, CSRF, malformed form and Basic credentials before any write."""
    router, store, account, _sessions = gateway
    with TestClient(create_app(router, 18794, _LOCAL_TOKEN), base_url=_ORIGIN) as client:
        agent = {"Authorization": f"Bearer {_LOCAL_TOKEN}"}
        browser = {"Authorization": _BROWSER}
        assert client.get("/api/status/production/company", headers=agent).is_success
        assert client.get("/credentials", headers=agent).status_code == 401
        assert "/credentials" in client.get("/", headers=browser).text
        form = client.get("/credentials", headers=browser)
        assert form.headers["cache-control"] == "no-store"
        csrf = re.search(r'name="csrf" value="([^"]+)"', form.text).group(1)
        headers = {**browser, "Origin": _ORIGIN}
        url = "/credentials/grafana"
        fields = {"csrf": csrf, "operation": "save", "login": "new-user", "password": "new-fixture-password"}

        assert client.post(url, headers=browser, data=fields).status_code == 403
        assert client.post(url, headers={**browser, "Origin": "https://external.example"}, data=fields).status_code == 403
        assert client.post(url, headers=headers, data={**fields, "csrf": "bad"}).status_code == 403
        assert client.post(url, headers=headers, data={**fields, "password": "x" * 20_000}).status_code == 400
        bad = client.post(url, headers=headers, data={**fields, "password": "wrong-password"})
        assert bad.status_code == 502 and "wrong-password" not in bad.text
        assert store.values[account] == _OLD
        store.fail = True
        assert client.post(url, headers=headers, data=fields).status_code == 503
        assert store.values[account] == _OLD and router.routes["grafana.read"].session.label == "old"
        store.fail = False

        saved = client.post(url, headers=headers, data=fields)
        assert saved.is_success and "сохранены" in saved.text
        assert "new-fixture-password" not in saved.text and "new-user" not in saved.text
        assert store.values[account] == _NEW and router.routes["grafana.read"].session.label == "new"
        assert client.post(url, headers=headers, data={"csrf": csrf, "operation": "reconnect"}).is_success

    with TestClient(create_app(router, 18794, _LOCAL_TOKEN), base_url=_ORIGIN) as client:
        assert client.get("/api/status/production/company", headers=agent).json()["backends"]["grafana"] == "ready"
        assert router.routes["grafana.read"].session.label == "new"


def test_rotation_drains_already_dispatched_tool(gateway):
    """Swap new requests immediately while letting the old generation finish its own call."""
    router, store, account, sessions = gateway

    async def scenario():
        """Check the worker lifetime around a credential swap."""
        async with router.running():
            old = sessions["old"]
            task = asyncio.create_task(router.call("grafana.read", {"hold": True}))
            await asyncio.wait_for(old.started.wait(), 1)
            assert await router.connections.rotate("grafana", _NEW)
            assert not old.closed.is_set() and store.values[account] == _NEW
            new_result = await router.call("grafana.read", {})
            assert new_result.structuredContent == {"worker": "new"}
            old.release.set()
            assert (await task).structuredContent == {"worker": "old"}
            await asyncio.wait_for(old.closed.wait(), 1)

    asyncio.run(scenario())


def test_auth_inputs_and_keychain_scope():
    """Keep colons in passwords and separate profile, backend and endpoint identities."""
    header = basic_authorization("grafana", "p:äss with spaces")
    assert b64decode(header.split()[1]).decode() == "grafana:p:äss with spaces"
    with pytest.raises(ValueError):
        basic_authorization("bad:login", "password")
    scope = "/profiles/company.json"
    url = "https://grafana.example.com/mcp"
    accounts = {
        credential_account(scope, "production/company", "grafana", url),
        credential_account(scope, "test/company", "grafana", url),
        credential_account("/other/company.json", "production/company", "grafana", url),
        credential_account(scope, "production/company", "other", url),
        credential_account(scope, "production/company", "grafana", "https://other.example.com/mcp"),
    }
    assert len(accounts) == 5


def test_basic_requires_gateway_authentication_and_excludes_bearer():
    """Reject an unauthenticated editor or two upstream auth sources in one backend."""
    data = {
        "name": "company",
        "backends": {"grafana": {"transport": "http", "url": "https://example.com/mcp", "basic_keychain": True}},
    }
    with pytest.raises(ValueError, match="requires a gateway access_token_env"):
        Profile.model_validate(data)
    data["access_token_env"] = "LOCAL_TOKEN"
    data["backends"]["grafana"]["bearer_token_env"] = "UPSTREAM_TOKEN"
    with pytest.raises(ValueError, match="mutually exclusive"):
        Profile.model_validate(data)
