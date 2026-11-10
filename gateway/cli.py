"""Run the gateway, inspect its state and generate local client configuration."""

import argparse
import asyncio
import json
import logging
import sys
from collections import deque
from pathlib import Path

import uvicorn
from filelock import FileLock, Timeout

from .bridge import bridge, read_status
from .credentials import SecretStoreError
from .demo import demo_server
from .http_app import create_app
from .keychain import MacKeychain
from .mcp_server import VERSION, serve_stdio
from .profile import HttpBackend, Profile, load_profile, secret
from .router import Router
from .telemetry import Journal, Telemetry

_LOG = logging.getLogger("gateway")


def executable() -> list[str]:
    """Return the command for this frozen executable or the installed Python package."""
    args = [str(Path(sys.executable).absolute())]
    if not getattr(sys, "frozen", False):
        args.extend(["-m", "gateway"])
    return args


def demo_profile(folder: Path):
    """Create a fixture-only profile without replacing an existing company profile."""
    folder.mkdir(parents=True, exist_ok=True)
    command, *args = executable()
    profile = {
        "version": 1,
        "name": "demo",
        "backends": {"demo": {"transport": "stdio", "command": command, "args": [*args, "demo-backend"]}},
        "denied": ["demo.blocked"],
        "singleflight": ["demo.echo"],
    }
    path = folder / "demo.json"
    with path.open("x", encoding="utf-8") as sink:
        json.dump(profile, sink, indent=2)
        sink.write("\n")
    print(path)


def parser() -> argparse.ArgumentParser:
    """Declare operator commands and profile storage overrides."""
    root = argparse.ArgumentParser(prog="ag-mcp-gateway")
    base = Path.home() / ".ag-mcp-gateway"
    root.add_argument("--version", action="version", version=VERSION)
    root.add_argument("--profiles", type=Path, default=base / "profiles")
    root.add_argument("--state", type=Path, default=base / "state")
    root.add_argument("--profile", default="demo")
    root.add_argument("--port", type=int, help="override the profile or environment port")
    commands = root.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the loopback gateway and dashboard")
    serve.add_argument("--allow-unauthenticated", action="store_true", help="permit local clients without a token")
    commands.add_parser("stdio", help="attach an MCP stdio client to the running gateway")
    commands.add_parser("status", help="print status and metrics as JSON")
    logs = commands.add_parser("logs", help="print the most recent metadata journal events")
    logs.add_argument("--backend", help="filter events by the configured MCP backend name")
    logs.add_argument("--limit", type=int, default=30, choices=range(1, 10001), metavar="1..10000")
    commands.add_parser("demo-profile", help="write a synthetic demo profile")
    commands.add_parser("demo-backend", help="run the synthetic stdio MCP backend")
    config = commands.add_parser("config", help="print a client configuration fragment")
    config.add_argument("client", choices=["codex", "cursor", "claude", "opencode", "deepseek", "acp"])
    return root


def serve(args, profile: Profile, token: str | None):
    """Lock the profile state and run the single local gateway process."""
    if profile.environment == "production" and args.allow_unauthenticated:
        raise ValueError("production profiles cannot use --allow-unauthenticated")
    if not token and not args.allow_unauthenticated:
        raise ValueError("set access_token_env in the profile or opt in with --allow-unauthenticated")
    directory = args.state.expanduser() / profile.state_path
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with FileLock(str(directory / "gateway.lock"), timeout=0):
        journal = Journal(directory / "logs", profile.log_max_bytes)
        store = mac_keychain(profile)
        profile_file = args.profiles.expanduser() / f"{profile.name}.json"
        router = Router(profile, Telemetry(journal), store=store, profile_file=str(profile_file.resolve()))
        app = create_app(router, args.port, token)
        _LOG.info("Profile %s · http://127.0.0.1:%s/", profile.name, args.port)
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False,
                    ws="none", log_level="warning", timeout_graceful_shutdown=10)


def mac_keychain(profile: Profile) -> MacKeychain | None:
    """Open the native provider only for profiles with opt-in Basic backends."""
    needs_store = any(isinstance(item, HttpBackend) and item.basic_keychain for item in profile.backends.values())
    if not needs_store or sys.platform != "darwin":
        return None
    try:
        return MacKeychain()
    except (SecretStoreError, AttributeError):
        _LOG.warning("macOS Keychain is unavailable")
        return None


def show_logs(directory: Path, limit: int, backend: str | None):
    """Read a bounded tail across rotations, tolerating a segment moving during a read."""
    recent = deque(maxlen=limit)
    for index in range(3, -1, -1):
        path = directory / "logs" / f"calls.{index}.jsonl"
        try:
            with path.open("r", encoding="utf-8") as source:
                for line in source:
                    if not line.endswith("\n"):
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if backend is None or event.get("tool", "").startswith(f"{backend}."):
                        recent.append(line)
        except FileNotFoundError:
            continue
    sys.stdout.writelines(recent)


def config_fragment(args, profile: Profile) -> str:
    """Generate a client example that references environment secrets by name."""
    command, *leading = executable()
    argv = [*leading, "--profiles", str(args.profiles.expanduser().resolve()),
            "--profile", args.profile, "--port", str(args.port), "stdio"]
    name = profile.client_name
    token_env = profile.access_token_env
    stdio = {"command": command, "args": argv}
    if args.client == "codex":
        lines = [f"[mcp_servers.{name}]", f"command = {json.dumps(command, ensure_ascii=False)}",
                 f"args = {json.dumps(argv, ensure_ascii=False)}"]
        if token_env:
            lines.append(f"env_vars = {json.dumps([token_env])}")
        return "\n".join(lines)
    if args.client == "deepseek":
        lines = [f"- id: {name}", "  name: '@deepseek-ai/dsh-mcp-client'", "  config:",
                 f"    serverName: {name}", "    transport: stdio",
                 f"    command: {json.dumps(command, ensure_ascii=False)}",
                 f"    args: {json.dumps(argv, ensure_ascii=False)}"]
        if token_env:
            lines.extend(["    env:", f"      {token_env}: !!js process.env.{token_env}"])
        return "\n".join(lines)
    if args.client == "opencode":
        entry = {"type": "local", "command": [command, *argv]}
        if token_env:
            entry["environment"] = {token_env: f"{{env:{token_env}}}"}
        value = {"mcp": {"servers": {name: entry}}}
    elif args.client == "acp":
        value = {"mcpServers": [{"name": name, **stdio, "env": []}]}
    else:
        if token_env:
            prefix = "env:" if args.client == "cursor" else ""
            stdio["env"] = {token_env: "${" + prefix + token_env + "}"}
        value = {"mcpServers": {name: stdio}}
    return json.dumps(value, indent=2, ensure_ascii=False)


def main(argv=None) -> int:
    """Dispatch one command with concise errors on stderr and clean MCP stdout."""
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.CRITICAL, format="%(asctime)s %(levelname)s %(message)s")
    _LOG.setLevel(logging.INFO)
    for library in ("mcp", "httpx", "httpcore"):
        logging.getLogger(library).setLevel(logging.CRITICAL)
    try:
        if args.port is not None and not 1 <= args.port <= 65535:
            raise ValueError("port must be in 1..65535")
        if args.command == "demo-profile":
            demo_profile(args.profiles.expanduser())
            return 0
        if args.command == "demo-backend":
            asyncio.run(serve_stdio(demo_server()))
            return 0
        profile = load_profile(args.profiles.expanduser(), args.profile)
        args.port = profile.resolve_port(args.port)
        origin = f"http://127.0.0.1:{args.port}"
        if args.command == "config":
            print(config_fragment(args, profile))
        elif args.command == "logs":
            if args.backend and args.backend not in profile.backends:
                raise ValueError("unknown backend for this profile")
            show_logs(args.state.expanduser() / profile.state_path, args.limit, args.backend)
        else:
            token = secret(profile.access_token_env)
            if args.command == "serve":
                serve(args, profile, token)
            elif args.command == "status":
                status = asyncio.run(read_status(origin, profile.endpoint_key, token, profile.timeout_seconds))
                print(json.dumps(status, ensure_ascii=False, indent=2))
            else:
                asyncio.run(bridge(origin, profile.endpoint_key, token, profile.timeout_seconds + 5))
        return 0
    except Timeout:
        _LOG.error("Profile state is already owned by another gateway process")
    except (ValueError, FileExistsError) as error:
        _LOG.error("%s", error)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        _LOG.error("Command failed (%s)", type(error).__name__)
    return 1
