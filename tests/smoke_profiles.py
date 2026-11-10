"""Verify two simultaneous test and production profiles against synthetic MCP servers."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from contextlib import ExitStack
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from smoke import free_port, stop, wait_ready


def launch_profile(folder: Path, binary: str, environment: str, port: int, stack: ExitStack):
    """Start an isolated daemon and wait for its same-named fixture backend."""
    profiles = folder / environment
    profiles.mkdir()
    token_env = f"AG_SMOKE_{environment.upper()}_TOKEN"
    token = f"synthetic-{environment}-token"
    profile = {
        "name": "company", "environment": environment, "listen_port": port,
        "access_token_env": token_env, "timeout_seconds": 8,
        "backends": {"demo": {"transport": "stdio", "command": binary, "args": ["demo-backend"]}},
    }
    (profiles / "company.json").write_text(json.dumps(profile))
    command = [binary, "--profiles", str(profiles), "--state", str(folder / "state"), "--profile", "company"]
    process = subprocess.Popen([*command, "serve"], env={**os.environ, token_env: token})
    stack.callback(stop, process)
    origin = f"http://127.0.0.1:{port}"
    status = wait_ready(origin, f"{environment}/company", process, token)
    assert status["environment"] == environment and status["backends"]["demo"] == "ready", status
    return command, token_env, token, origin


async def call_through_bridge(command: list[str], token_env: str, token: str, environment: str):
    """Call one fixture through the stdio bridge without overriding the configured port."""
    server = StdioServerParameters(command=command[0], args=[*command[1:], "stdio"], env={token_env: token})
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            assert [tool.name for tool in (await client.list_tools()).tools] == ["demo.blocked", "demo.echo"]
            reply = await client.call_tool("demo.echo", {"text": environment})
            assert reply.structuredContent == {"echo": environment}, reply


def verify_boundary(command, token_env, token, origin, environment, other_token):
    """Verify profile-scoped URLs, authorization, status, labels and client configuration."""
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(trust_env=False, headers=headers, base_url=origin) as client:
        other_environment = "test" if environment == "production" else "production"
        assert client.get(f"/api/status/{other_environment}/company").status_code == 404
        assert client.post(f"/mcp/{other_environment}/company", json={}).status_code == 404
        assert client.get("/api/status/company").status_code == 404
        status = client.get(f"/api/status/{environment}/company")
        assert status.json()["metrics"]["total"]["upstream"] == 1
        assert client.get(f"/api/status/{environment}/company",
                          headers={"Authorization": f"Bearer {other_token}"}).status_code == 401
        page = client.get("/")
        label = "Рабочая среда" if environment == "production" else "Тестовая среда"
        assert page.is_success and label in page.text
    with httpx.Client(trust_env=False, base_url=origin) as guest:
        assert guest.get("/").status_code == 401
    env = {**os.environ, token_env: token}
    output = subprocess.check_output([*command, "config", "codex"], env=env, text=True)
    assert token not in output and token_env in output
    status = subprocess.check_output([*command, "status"], env=env, text=True)
    assert json.loads(status)["environment"] == environment
    logs = subprocess.check_output([*command, "logs"], env=env, text=True)
    assert len(logs.splitlines()) == 1 and json.loads(logs)["tool"] == "demo.echo"
    return next(iter(tomllib.loads(output)["mcp_servers"]))


def main() -> None:
    """Run the same company name in two environments sharing a state root."""
    binary = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        folder = Path(directory)
        test = launch_profile(folder, binary, "test", free_port(), stack)
        prod = launch_profile(folder, binary, "production", free_port(), stack)

        async def exercise():
            """Send requests concurrently from the two independent MCP clients."""
            await asyncio.gather(call_through_bridge(*test[:3], "test"),
                                 call_through_bridge(*prod[:3], "production"))

        asyncio.run(exercise())
        test_name = verify_boundary(*test, "test", prod[2])
        prod_name = verify_boundary(*prod, "production", test[2])
        assert test_name != prod_name
        for environment in ("test", "production"):
            assert (folder / "state" / ".environments" / environment /
                    "company" / "logs" / "calls.0.jsonl").is_file()
        print("PASS: simultaneous test and production daemons, distinct routes, tokens, journals and configs")


if __name__ == "__main__":
    main()
