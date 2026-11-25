"""Exercise both browser locales without touching corporate MCPs or Keychain."""

from base64 import b64encode
from urllib.parse import urlencode

from starlette.testclient import TestClient

from gateway.http_app import create_app
from gateway.module_registry import ModuleRegistry
from gateway.policy_settings import ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Router
from gateway.telemetry import Journal, Telemetry

ORIGIN = "http://127.0.0.1:18792"


def browser(tmp_path):
    """Construct isolated real HTML routes and the existing Basic boundary."""
    profile = Profile.model_validate({
        "name": "localized", "access_token_env": "FIXTURE", "management_bridge": True,
        "modules_enabled": True, "backends": {
            "demo": {"transport": "stdio", "command": "unused"},
            "basic": {"transport": "http", "url": "https://example.invalid/mcp", "basic_keychain": True},
        },
    })
    router = Router(profile, Telemetry(Journal(tmp_path, 4096)))
    router.modules = ModuleRegistry(router, tmp_path / "modules.json")
    settings = ToolPolicySettings(router, tmp_path / "policy.json")
    app = create_app(router, 18792, "synthetic", settings=settings)
    header = "Basic " + b64encode(b"gateway:synthetic").decode()
    return TestClient(app, base_url=ORIGIN, headers={"Authorization": header})


def test_browser_locales_and_cookie(tmp_path):
    """Cover all four localized pages and leave protocol responses untouched."""
    with browser(tmp_path) as client:
        page = client.get("/?q=test&backend=demo&offset=100&confirmation=do-not-copy",
                          headers={"Accept-Language": "fr, en-US;q=.9, ru;q=.6"})
        assert page.headers["Content-Language"] == "en" and "set-cookie" not in page.headers
        assert 'lang="en"' in page.text and "MCP catalog" in page.text
        assert '?q=test&amp;backend=demo&amp;offset=100&amp;lang=ru' in page.text
        assert "do-not-copy" not in page.text
        page = client.get("/settings?lang=ru")
        assert "Политика инструментов" in page.text
        assert "amg_ui_lang=ru" in page.headers["set-cookie"]
        assert "HttpOnly" in page.headers["set-cookie"]
        assert client.get("/credentials", headers={"Accept-Language": "en"}).headers["Content-Language"] == "ru"
        page = client.get("/modules?lang=en&edit=example&tool=demo.read")
        assert "MCP modules" in page.text and "Manual run" in page.text and "Hooks and sessions" in page.text
        assert "Хуки и сессии" not in page.text and "Выбрать поле" not in page.text
        assert "edit=example&amp;tool=demo.read&amp;lang=ru" in page.text
        page = client.get("/credentials?backend=basic&lang=unsupported")
        assert "MCP credentials" in page.text and "Username" in page.text and "Keychain" in page.text
        assert "set-cookie" not in page.headers
        page = client.get("/settings")
        assert "Policy revision" in page.text and "MCP credential sources" in page.text
        status = client.get("/api/status/localized?lang=ru")
        assert status.status_code == 200 and "Content-Language" not in status.headers
        assert "set-cookie" not in status.headers and status.json()["profile"] == "localized"


def test_browser_language_does_not_grant_access(tmp_path):
    """Language selection stays inside authorization and the form origin checks."""
    with browser(tmp_path) as client:
        protected = client.get("/settings?lang=en", auth=None, headers={"Host": "outside.invalid"})
        assert protected.status_code == 403 and "set-cookie" not in protected.headers
        form = client.post("/modules?lang=ru", headers={"Origin": ORIGIN, "Accept-Language": "en",
                           "Content-Type": "application/x-www-form-urlencoded"},
                           content=urlencode({"csrf": "wrong"}))
        assert form.status_code == 403 and form.text == "Invalid form token"
        assert "set-cookie" not in form.headers
