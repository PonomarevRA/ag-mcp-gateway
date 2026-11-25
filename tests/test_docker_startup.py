"""Exercise Docker dependency recovery through the real MCP HTTP surface."""

import sys
import textwrap
import time

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from gateway.http_app import create_app
from gateway.policy_settings import ToolPolicySettings
from gateway.profile import Profile
from gateway.router import Router
from gateway.telemetry import Journal, Telemetry


def test_docker_wait_publishes_action_and_starts_only_once(tmp_path, monkeypatch):
    """Start an actual stdio fixture after the configured Docker engine becomes ready."""
    engine = tmp_path / "engine-ready"
    started = tmp_path / "starts"
    docker = tmp_path / "docker"
    docker.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''
        import os
        import sys
        from pathlib import Path

        if sys.argv[1:] == ["--context", "fixture", "info"]:
            print("private diagnostic payload", file=sys.stderr)
            sys.exit(0 if Path(os.environ["DOCKER_HOST"]).exists() else 1)
        if sys.argv[1:] != ["--context", "fixture", "run", "fixture"]:
            sys.exit(2)
        with Path(os.environ["START_LOG"]).open("a") as log:
            log.write("start\\n")

        import asyncio
        from mcp import types
        from mcp.server.lowlevel import Server
        from mcp.server.stdio import stdio_server

        server = Server("fixture")

        @server.list_tools()
        async def tools():
            return [types.Tool(name="echo", inputSchema={"type": "object"})]

        async def main():
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())

        asyncio.run(main())
    '''))
    docker.chmod(0o700)
    monkeypatch.setenv("FIXTURE_ENGINE", str(engine))
    monkeypatch.setenv("FIXTURE_START_LOG", str(started))
    monkeypatch.setattr("gateway.connections._DOCKER_RETRY_SECONDS", 0.02)
    profile = Profile.model_validate({
        "name": "test", "access_token_env": "TEST_TOKEN", "management_bridge": True,
        "backends": {"local": {"transport": "stdio", "command": str(docker),
                               "args": ["--context", "fixture", "run", "fixture"],
                               "env_from": {"DOCKER_HOST": "FIXTURE_ENGINE", "START_LOG": "FIXTURE_START_LOG"}}},
    })
    router = Router(profile, Telemetry(Journal(tmp_path, 4096)))
    settings = ToolPolicySettings(router, tmp_path / "policy.json")
    app = create_app(router, 18792, "fixture-token", settings=settings)
    headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer fixture-token"}

    def request(client, path, method, params=None):
        """Read a JSON-RPC result from a real authenticated MCP endpoint."""
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        return client.post(path, json=message, headers=headers).json()["result"]

    with TestClient(app, base_url="http://127.0.0.1:18792") as client:
        path = "/mcp/test"
        tools = request(client, path, "tools/list")
        assert [tool["name"] for tool in tools["tools"]] == ["gateway_backend_startup_status"]
        status = request(client, path, "tools/call", {"name": "gateway_backend_startup_status"})
        recovery, = status["structuredContent"]["connections"]
        assert recovery["backend"] == "local" and recovery["action"] == "start_docker"
        assert "private diagnostic payload" not in str(tools) + str(status)
        assert tools["_meta"]["ag-mcp-gateway/backend-recovery"] == [recovery]
        inspected = request(client, "/manage/test", "tools/call", {"name": "gateway_inspect"})
        assert inspected["structuredContent"]["connections"][0]["recovery"] == recovery
        scoped = request(client, path + "/local", "tools/call", {"name": "echo"})
        assert scoped["isError"] and scoped["structuredContent"] == recovery
        reconnect = {"name": "gateway_reconnect_mcp", "arguments": {"backend": "local"}}
        blocked = request(client, "/manage/test", "tools/call", reconnect)
        assert blocked["isError"] and blocked["structuredContent"] == recovery
        assert not started.exists()

        engine.touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = request(client, path + "/local", "tools/list")
            if current["tools"]:
                break
            time.sleep(0.02)
        assert [tool["name"] for tool in current["tools"]] == ["echo"]
        assert "_meta" not in current
        assert started.read_text() == "start\n"


def test_wrappers_can_opt_in_and_http_remains_connect_only():
    """Limit process dependencies to trusted commands in stdio profile entries."""
    def profile(backend):
        """Validate one configured backend through the portable profile schema."""
        return Profile.model_validate({"name": "test", "backends": {"local": backend}})

    config = profile({"transport": "stdio", "command": "wrapper", "requires_docker": True})
    assert config.backends["local"].docker_cli == "docker"
    with pytest.raises(ValidationError):
        profile({"transport": "http", "url": "http://127.0.0.1:8080/mcp", "requires_docker": True})
