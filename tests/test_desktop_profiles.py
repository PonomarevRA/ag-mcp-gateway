"""Exercise local profile creation, revision checks and browser discovery."""

import json

import pytest
from starlette.testclient import TestClient

from gateway.desktop_profiles import profile_inventory, save_port, save_profile
from gateway.http_app import create_app
from gateway.profile import load_profile
from gateway.router import Router
from gateway.telemetry import Journal, Telemetry


def test_desktop_saves_token_references_and_adds_with_revision(tmp_path):
    """Create both Keychain items, then add a validated backend without exposing secrets."""
    secrets = {}

    class Keychain:
        def write_token(self, key, value):
            """Use in-memory Keychain fixtures without touching the current user's secrets."""
            secrets[key] = value

    profile = save_profile(tmp_path, json.dumps({
        "name": "sandbox", "environment": "test", "port": 19091,
        "backend_name": "docs", "backend": {"transport": "http", "url": "https://mcp.example.invalid/mcp"},
        "bearer_secret": "fixture-secret-that-must-not-be-serialized",
    }).encode(), Keychain)
    config = load_profile(tmp_path, "sandbox")
    assert profile["key"] == "test/sandbox" and profile["port"] == 19091
    assert profile["protected"] and profile["backends"] == ["docs"]
    assert config.access_token_keychain in secrets
    assert secrets[config.backends["docs"].bearer_token_keychain] == "fixture-secret-that-must-not-be-serialized"
    assert "fixture-secret" not in (tmp_path / "sandbox.json").read_text()
    assert profile_inventory(tmp_path) == {"profiles": [profile], "unavailable": 0}

    addition = json.dumps({
        "name": "sandbox", "revision": profile["revision"], "backend_name": "local",
        "backend": {"transport": "stdio", "command": "/usr/bin/true"},
    }).encode()
    updated = save_profile(tmp_path, addition, Keychain)
    assert updated["backends"] == ["docs", "local"]
    assert profile["revision"] != updated["revision"]
    with pytest.raises(ValueError, match="profile changed"):
        save_profile(tmp_path, addition, Keychain)



def test_desktop_changes_only_port_and_rejects_a_stale_revision(tmp_path):
    """A conflict repair leaves the original backend and gateway credential references intact."""
    original = {
        "name": "sandbox", "listen_port": 19091, "access_token_env": "FIXTURE_TOKEN",
        "backends": {"local": {"transport": "stdio", "command": "/usr/bin/true"}},
    }
    (tmp_path / "sandbox.json").write_text(json.dumps(original))
    before = load_profile(tmp_path, "sandbox")
    revision = profile_inventory(tmp_path)["profiles"][0]["revision"]
    request = json.dumps({"name": "sandbox", "revision": revision, "port": 19092}).encode()
    updated = save_port(tmp_path, request)
    after = load_profile(tmp_path, "sandbox")
    assert updated["port"] == 19092 and updated["revision"] != revision
    assert after == before.model_copy(update={"listen_port": 19092})
    with pytest.raises(ValueError, match="profile changed"):
        save_port(tmp_path, request)

def test_workspace_routes_have_one_current_page_and_local_operator_links(tmp_path):
    """Keep navigation visible and leave native operations outside of the HTTP API."""
    (tmp_path / "demo.json").write_text(json.dumps({
        "name": "demo", "backends": {"local": {"transport": "stdio", "command": "unused"}},
    }))
    profile = load_profile(tmp_path, "demo")
    router = Router(profile, Telemetry(Journal(tmp_path / "logs", 4096)))
    client = TestClient(create_app(router, 18792, None), base_url="http://127.0.0.1:18792")
    for path in ("/", "/catalog", "/tools", "/statistics", "/profiles", "/settings", "/modules", "/credentials"):
        response = client.get(path, headers={"Accept-Language": "en"})
        assert response.status_code == 200
        assert response.text.count('aria-current="page"') == 1
        selected_path = "/" if path == "/catalog" else path
        assert f'href="{selected_path}" aria-current="page"' in response.text
        assert 'href="#main-content"' in response.text
    catalog = client.get("/catalog").text
    assert 'href="agm-gateway://add-mcp/demo"' in catalog and 'id="metrics"' not in catalog
    assert 'href="agm-gateway://profiles"' in client.get("/profiles").text
    assert client.post("/profiles", json={"backend": "ignored"}).status_code == 405
    assert client.get("/gateway.svg").headers["content-type"] == "image/svg+xml"

    draft = json.dumps({
        "name": "demo", "revision": profile_inventory(tmp_path)["profiles"][0]["revision"],
        "backend_name": "new", "backend": {"transport": "stdio", "command": "/usr/bin/true"},
    }).encode()
    with pytest.raises(ValueError, match="protected profile"):
        save_profile(tmp_path, draft)


def test_profile_failure_removes_new_keychain_items(tmp_path, monkeypatch):
    """Leave neither a new profile nor stranded credentials after a failed file commit."""
    secrets = {}

    class Keychain:
        def write_token(self, key, value):
            secrets[key] = value

        def delete_token(self, key):
            del secrets[key]

    def disk_failure(path, body):
        raise OSError("fixture write failed")

    monkeypatch.setattr("gateway.desktop_profiles.write_state", disk_failure)
    request = json.dumps({
        "name": "sandbox", "backend_name": "docs",
        "backend": {"transport": "http", "url": "https://mcp.example.invalid/mcp"},
        "bearer_secret": "fixture-secret-that-must-not-be-serialized",
    }).encode()
    with pytest.raises(OSError, match="fixture write failed"):
        save_profile(tmp_path, request, Keychain)
    assert not secrets and not (tmp_path / "sandbox.json").exists()
