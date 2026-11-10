"""Verify a gateway executable through real stdio and Streamable HTTP transports."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import tomllib
from contextlib import ExitStack
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

_TOKEN = "synthetic-local-smoke-token"
_PRIVATE = "synthetic-payload-DO-NOT-LOG"


def free_port():
    """Pick an ephemeral loopback port for a short-lived fixture process."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def stop(process):
    """Terminate only the fixture process and wait for its owned child to exit."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def wait_ready(origin, profile, process, token=None):
    """Poll authenticated status and surface fixture startup errors promptly."""
    import time

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(timeout=1, trust_env=False, headers=headers) as http:
        for _ in range(150):
            if process.poll() is not None:
                raise AssertionError(f"{profile} exited: {process.returncode}")
            try:
                reply = http.get(f"{origin}/api/status/{profile}")
                if reply.is_success:
                    return reply.json()
            except httpx.TransportError:
                pass
            time.sleep(0.1)
    raise AssertionError(f"{profile} did not become ready")


async def exercise(command, origin):
    """Confirm two sessions share exactly one upstream call, while errors stay isolated."""
    async with httpx.AsyncClient(trust_env=False, headers={"Authorization": f"Bearer {_TOKEN}"}) as http:
        async with streamable_http_client(f"{origin}/mcp/company", http_client=http) as streams:
            async with ClientSession(*streams[:2]) as direct:
                await direct.initialize()
                parameters = StdioServerParameters(command=command[0], args=[*command[1:], "stdio"],
                                                   env={"AG_SMOKE_TOKEN": _TOKEN})
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as stdio:
                        await stdio.initialize()
                        tools = await stdio.list_tools()
                        names = {tool.name for tool in tools.tools}
                        expected = {"local.echo", "remote.demo.echo"}
                        if names != expected:
                            status = (await http.get(f"{origin}/api/status/company")).json()
                            raise AssertionError((names, status["backends"], status["catalog"]))
                        one, two = await asyncio.gather(
                            direct.call_tool("local.echo", {"text": _PRIVATE}),
                            stdio.call_tool("local.echo", {"text": _PRIVATE}),
                        )
                        assert one.structuredContent == two.structuredContent == {"echo": _PRIVATE}
                        again = await stdio.call_tool("local.echo", {"text": _PRIVATE})
                        assert again.structuredContent == one.structuredContent
                        extension = {"example.com/context": "synthetic metadata"}
                        meta = {**extension, "progressToken": "client-local"}
                        tagged = await asyncio.gather(
                            direct.call_tool("local.echo", {"text": _PRIVATE}, meta=meta),
                            stdio.call_tool("local.echo", {"text": _PRIVATE}, meta=meta),
                        )
                        assert all(item.structuredContent == {"echo": _PRIVATE, "metadata": extension}
                                   for item in tagged), tagged
                        denied = await direct.call_tool("local.blocked", {})
                        assert denied.isError
                        remote = await direct.call_tool("remote.demo.echo", {"text": "upstream HTTP"})
                        assert remote.structuredContent == {"echo": "upstream HTTP"}
        response = await http.get(f"{origin}/api/status/company")
        total = response.json()["metrics"]["total"]
        assert total["received"] == 7 and total["upstream"] == 5, total
        assert total["merged"] == total["denied"] == 1, total
        assert total["latency_mean_ms"] > 0 and total["input_tokens_estimate"] > 0, total
        assert (await http.get(f"{origin}/api/status/demo")).status_code == 404
        assert (await http.get(f"{origin}/", headers={"Origin": "https://example.com"})).status_code == 403
        assert (await http.get(f"{origin}/", headers={"Host": "example.com"})).status_code == 403
        page = await http.get(f"{origin}/", params={"q": '"><script>fixture</script>'})
        assert page.is_success and "<script>" not in page.text
        filtered = await http.get(f"{origin}/", params={"q": "local.echo"})
        assert "local.echo" in filtered.text and "remote.demo.echo" not in filtered.text
    async with httpx.AsyncClient(trust_env=False) as guest:
        assert (await guest.get(f"{origin}/")).status_code == 401


async def exercise_scoped(command, origin, environment):
    """Attach a real SDK client and an independently generated scoped stdio entry."""
    path = f"{origin}/mcp/company/local"
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    async with httpx.AsyncClient(trust_env=False, headers=headers) as http:
        async with streamable_http_client(path, http_client=http) as streams:
            async with ClientSession(*streams[:2]) as client:
                await client.initialize()
                assert [tool.name for tool in (await client.list_tools()).tools] == ["echo"]
                assert (await client.call_tool("blocked", {})).isError
        async with streamable_http_client(f"{origin}/mcp/company/remote", http_client=http) as streams:
            async with ClientSession(*streams[:2]) as client:
                await client.initialize()
                assert [tool.name for tool in (await client.list_tools()).tools] == ["demo.echo"]
                result = await client.call_tool("demo.echo", {"text": "scoped HTTP"})
                assert result.structuredContent == {"echo": "scoped HTTP"}
        page = await http.get(f"{origin}/")
        assert page.is_success and path in page.text

    config = subprocess.check_output([*command, "config", "--backend", "local", "codex"], env=environment)
    entry, = tomllib.loads(config.decode())["mcp_servers"].values()
    parameters = StdioServerParameters(command=entry["command"], args=entry["args"], env={"AG_SMOKE_TOKEN": _TOKEN})
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            assert [tool.name for tool in (await client.list_tools()).tools] == ["echo"]
            result = await client.call_tool("echo", {"text": "scoped stdio"})
            assert result.structuredContent == {"echo": "scoped stdio"}


def main():
    """Run two isolated daemons and validate CLI JSON and payload-free logs."""
    binary = str(Path(sys.argv[1]).resolve())
    with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
        folder = Path(temp)
        base = [binary, "--profiles", str(folder), "--state", str(folder)]
        environment = {**os.environ, "AG_SMOKE_TOKEN": _TOKEN}
        subprocess.run([*base, "demo-profile"], check=True, stdout=subprocess.DEVNULL)
        upstream_port = free_port()
        upstream = f"http://127.0.0.1:{upstream_port}"
        fixture = subprocess.Popen([*base, "--port", str(upstream_port), "serve", "--allow-unauthenticated"])
        stack.callback(stop, fixture)
        demo_status = wait_ready(upstream, "demo", fixture)
        assert demo_status["backends"]["demo"] == "ready", demo_status
        profile = {
            "name": "company", "access_token_env": "AG_SMOKE_TOKEN", "timeout_seconds": 5,
            "backends": {
                "local": {"transport": "stdio", "command": binary, "args": ["demo-backend"]},
                "remote": {"transport": "http", "url": f"{upstream}/mcp/demo"},
                "bad": {"transport": "stdio", "command": str(folder / "missing")},
            },
            "denied": ["local.blocked"], "singleflight": ["local.echo"], "log_max_bytes": 4096,
        }
        (folder / "company.json").write_text(json.dumps(profile))
        gateway_port = free_port()
        gateway = f"http://127.0.0.1:{gateway_port}"
        command = [*base, "--port", str(gateway_port), "--profile", "company"]
        daemon = subprocess.Popen([*command, "serve"], env=environment)
        stack.callback(stop, daemon)
        status = wait_ready(gateway, "company", daemon, _TOKEN)
        assert status["backends"]["bad"].startswith("unavailable"), status
        assert status["backends"]["local"] == status["backends"]["remote"] == "ready", status
        asyncio.run(exercise(command, gateway))
        cli_status = json.loads(subprocess.check_output([*command, "status"], env=environment))
        assert cli_status["metrics"]["total"]["received"] == 7
        logs = subprocess.check_output([*command, "logs"], env=environment).decode()
        assert len(logs.splitlines()) == 7 and _PRIVATE not in logs and _TOKEN not in logs
        assert "synthetic metadata" not in logs
        filtered_logs = subprocess.check_output([*command, "logs", "--backend", "remote"], env=environment)
        assert [json.loads(line)["tool"] for line in filtered_logs.splitlines()] == ["remote.demo.echo"]
        asyncio.run(exercise_scoped(command, gateway, environment))
        print("PASS: executable, aggregate and scoped MCP, upstream HTTP/stdio, policy, singleflight, dashboard and CLI")


if __name__ == "__main__":
    main()
