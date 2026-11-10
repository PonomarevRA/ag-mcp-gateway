"""Run the gateway, inspect its state and generate local client configuration."""

import argparse
import asyncio
import getpass
import json
import logging
import os
import sqlite3
import sys
import warnings
from hashlib import sha256
from pathlib import Path

import uvicorn
from pydantic import TypeAdapter, ValidationError
from filelock import FileLock, Timeout

from .bridge import bridge, read_status
from .credentials import SecretStoreError
from .demo import demo_server
from .hook_lifecycle import HOOK_ENV
from .http_app import create_app
from .keychain import MacKeychain
from .mcp_server import VERSION, serve_stdio
from .module_registry import ModuleRegistry
from .policy_settings import ToolPolicySettings
from .profile import HttpBackend, Profile, SecretKey, load_profile
from .router import Router
from .session_context import SessionContext, add_session_options, session_from_cli
from .skill_store import SkillStore
from .skill_tools import SkillReference
from .technical_events import TechnicalEvents
from .technical_journal import TechnicalJournal
from .telemetry import Journal, Telemetry
from .token_secrets import resolve_token, valid_token
from .tool_feedback import ToolFeedback

_LOG = logging.getLogger("gateway")
_LOG_MAX_LINE_BYTES = 64 * 1024
_LOG_READ_CHUNK = _LOG_MAX_LINE_BYTES


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
    stdio = commands.add_parser("stdio", help="attach an MCP stdio client to the running gateway")
    add_session_options(stdio)
    stdio.add_argument("--backend", help="expose only one configured MCP backend")
    commands.add_parser("mcp-bridge", help="attach the authenticated management MCP over stdio")
    token_set = commands.add_parser("token-set", help="save one token in macOS Keychain using hidden input")
    token_set.add_argument("key", help="symbolic Keychain reference")
    tag = commands.add_parser("session-tag", help="derive the journal tag for a declared session")
    add_session_options(tag)
    review = commands.add_parser("skill-review", help="list drafts or read one draft before approval")
    review.add_argument("id", nargs="?")
    approve = commands.add_parser("skill-approve", help="approve one immutable MCP skill draft")
    approve.add_argument("id")
    discard = commands.add_parser("skill-discard", help="remove one pending MCP skill draft")
    discard.add_argument("id")
    commands.add_parser("status", help="print status and metrics as JSON")
    logs = commands.add_parser("logs", help="print the most recent metadata journal events")
    logs.add_argument("--backend", help="filter events by the configured MCP backend name")
    logs.add_argument("--session-tag", help="filter events by a session tag")
    logs.add_argument("--trace-id", help="filter events by a W3C trace ID")
    logs.add_argument("--limit", type=int, default=30, choices=range(1, 10001), metavar="1..10000")
    commands.add_parser("demo-profile", help="write a synthetic demo profile")
    commands.add_parser("demo-backend", help="run the synthetic stdio MCP backend")
    config = commands.add_parser("config", help="print a client configuration fragment")
    add_session_options(config)
    config_scope = config.add_mutually_exclusive_group()
    config_scope.add_argument("--management", action="store_true", help="print a separate management MCP entry")
    config_scope.add_argument("--backend", help="generate an entry for one configured MCP backend")
    config.add_argument("client", choices=["codex", "cursor", "claude", "opencode", "deepseek", "acp"])
    return root


def serve(args, profile: Profile, token: str | None, store: MacKeychain | None):
    """Lock the profile state and run the single local gateway process."""
    if profile.environment == "production" and args.allow_unauthenticated:
        raise ValueError("production profiles cannot use --allow-unauthenticated")
    if not token and not args.allow_unauthenticated:
        raise ValueError("configure a gateway token or opt in with --allow-unauthenticated")
    directory = args.state.expanduser() / profile.state_path
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with FileLock(str(directory / "gateway.lock"), timeout=0):
        journal = Journal(directory / "logs", profile.log_max_bytes)
        journal.prune_expired(profile.log_retention_days)
        profile_file = args.profiles.expanduser() / f"{profile.name}.json"
        discovery_enabled = profile.agent_discovery and bool(token)
        technical = TechnicalJournal(directory / "logs" / "technical.sqlite3", profile.technical_log_max_bytes,
                                     retention_days=profile.log_retention_days)
        events = TechnicalEvents(technical, profile.endpoint_key)
        try:
            telemetry = Telemetry(journal, with_history=discovery_enabled, technical=events)
            router = Router(profile, telemetry, store=store, profile_file=str(profile_file.resolve()))
            settings = ToolPolicySettings(router, directory / "tool-policy.json")
            skill_store = open_skills(args, profile) if profile.skill_router is not None and token else None
            if profile.modules_enabled and token:
                router.modules = ModuleRegistry(router, directory / "modules.json")
            feedback = ToolFeedback(directory / "tool-feedback.sqlite3") if discovery_enabled else None
            app = create_app(router, args.port, token, settings=settings, skill_store=skill_store, feedback=feedback, technical=technical)
            _LOG.info("Profile %s · http://127.0.0.1:%s/", profile.name, args.port)
            uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False,
                        ws="none", log_level="warning", timeout_graceful_shutdown=10)
        finally:
            technical.close()


def open_skills(args, profile: Profile) -> SkillStore:
    """Open the selected profile's SQLite store for the daemon or local operator."""
    if profile.skill_router is None:
        raise ValueError("skill_router is disabled for this profile")
    path = args.state.expanduser() / profile.state_path / "skills.sqlite3"
    return SkillStore(path, profile.skill_router)


def manage_skill(args, profile: Profile) -> None:
    """Review, approve or discard a draft using local profile-state permissions."""
    identity = SkillReference.model_validate({"id": args.id}).id if args.id is not None else None
    store = open_skills(args, profile)
    if args.command == "skill-approve":
        result = store.approve(identity)
    elif args.command == "skill-discard":
        result = store.discard(identity)
    elif identity is not None:
        result = store.get(identity, "draft")
    else:
        result = store.drafts()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def mac_keychain(profile: Profile) -> MacKeychain | None:
    """Open the native provider only when the selected profile references the OS store."""
    needs_store = profile.access_token_keychain or any(
        isinstance(item, HttpBackend) and (item.basic_keychain or item.bearer_token_keychain)
        for item in profile.backends.values()
    )
    if not needs_store or sys.platform != "darwin":
        return None
    try:
        return MacKeychain()
    except (SecretStoreError, AttributeError):
        _LOG.warning("macOS Keychain is unavailable")
        return None


def save_token(key: str) -> None:
    """Read a token from a real terminal without echo and store it using Security.framework."""
    try:
        TypeAdapter(SecretKey).validate_python(key)
    except ValidationError:
        raise ValueError("invalid Keychain reference") from None
    if sys.platform != "darwin":
        raise ValueError("OS token storage currently requires macOS Keychain")
    if not sys.stdin.isatty():
        raise ValueError("token-set requires a terminal for hidden input")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass("Token: ")
    if not valid_token(value):
        raise ValueError("invalid token format")
    MacKeychain().write_token(key, value)
    print("Token saved in macOS Keychain")


def newest_log_lines(source):
    """Read complete NDJSON lines from the end, without scanning older chunks first."""
    offset = source.seek(0, os.SEEK_END)
    suffix = b""
    found_terminator = False
    while offset:
        size = min(offset, _LOG_READ_CHUNK)
        offset -= size
        source.seek(offset)
        first, *following = source.read(size).split(b"\n")
        if not following:
            if found_terminator and suffix is not None:
                # Generated records fit comfortably in one chunk. Ignore oversized corrupt lines.
                suffix = first + suffix if len(first) + len(suffix) <= _LOG_MAX_LINE_BYTES else None
            continue

        tail = following.pop()
        if found_terminator and suffix is not None and len(tail) + len(suffix) <= _LOG_MAX_LINE_BYTES:
            yield tail + suffix
        # The first chunk with a newline ended with a possibly incomplete record.
        found_terminator = True
        yield from reversed(following)
        suffix = first
    if found_terminator and suffix is not None:
        yield suffix


def show_logs(directory: Path, limit: int, backend: str | None, session_tag: str | None,
              trace_id: str | None = None):
    """Find the requested latest matches and print them in chronological order."""
    if limit <= 0:
        return
    recent = []
    seen = set()
    for index in range(4):
        path = directory / "logs" / f"calls.{index}.jsonl"
        try:
            with path.open("rb") as source:
                stat = os.fstat(source.fileno())
                identity = stat.st_dev, stat.st_ino
                if stat.st_ino and identity in seen:
                    # A segment already read may have moved to this slot during rotation.
                    continue
                seen.add(identity)
                for raw in newest_log_lines(source):
                    try:
                        line = raw.decode("utf-8").removesuffix("\r")
                        event = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    tool = event.get("tool")
                    if backend is not None and (not isinstance(tool, str) or not tool.startswith(f"{backend}.")):
                        continue
                    if session_tag is not None and event.get("session_tag") != session_tag:
                        continue
                    if trace_id is not None and event.get("trace_id") != trace_id:
                        continue
                    recent.append(line + "\n")
                    if len(recent) == limit:
                        break
        except FileNotFoundError:
            continue
        if len(recent) == limit:
            break
    sys.stdout.writelines(reversed(recent))


def config_fragment(args, profile: Profile, session: SessionContext | None = None) -> str:
    """Generate a client example that references environment secrets by name."""
    command, *leading = executable()
    management = getattr(args, "management", False)
    backend = getattr(args, "backend", None)
    if backend is not None and backend not in profile.backends:
        raise ValueError("unknown backend for this profile")
    if backend is not None and management:
        raise ValueError("management and backend MCP scopes are mutually exclusive")
    if management and not profile.management_bridge:
        raise ValueError("management_bridge is disabled for this profile")
    if management and session is not None:
        raise ValueError("management configuration does not accept caller session identity")
    transport = "mcp-bridge" if management else "stdio"
    argv = [*leading, "--profiles", str(args.profiles.expanduser().resolve()),
            "--profile", args.profile, "--port", str(args.port), transport]
    if session is not None:
        argv.extend(session.cli_arguments())
    name = profile.client_name
    if backend is not None:
        argv.extend(["--backend", backend])
        identity = f"{profile.endpoint_key}/{backend}"
        suffix = sha256(identity.encode("ascii")).hexdigest()[:8]
        name = f"amg-proxy-{backend[:13]}-{suffix}"
    if management:
        environment = profile.environment or "legacy"
        name = f"amg-mgmt-{environment}-{profile.name}"
        if len(name) > 32:
            suffix = sha256(profile.endpoint_key.encode("ascii")).hexdigest()[:8]
            name = f"{name[:23]}-{suffix}"
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
        if args.command == "token-set":
            save_token(args.key)
            return 0
        if args.command == "demo-profile":
            demo_profile(args.profiles.expanduser())
            return 0
        if args.command == "demo-backend":
            asyncio.run(serve_stdio(demo_server()))
            return 0
        session = session_from_cli(args)
        if args.command == "session-tag":
            if session is None:
                raise ValueError("provide at least one session identity")
            print(session.tag)
            return 0
        profile = load_profile(args.profiles.expanduser(), args.profile)
        args.port = profile.resolve_port(args.port)
        origin = f"http://127.0.0.1:{args.port}"
        if args.command == "config":
            print(config_fragment(args, profile, session))
        elif args.command in {"skill-review", "skill-approve", "skill-discard"}:
            manage_skill(args, profile)
        elif args.command == "logs":
            if args.backend and args.backend not in profile.backends:
                raise ValueError("unknown backend for this profile")
            show_logs(args.state.expanduser() / profile.state_path, args.limit, args.backend,
                      args.session_tag, args.trace_id)
        else:
            if args.command == "mcp-bridge" and not profile.management_bridge:
                raise ValueError("management_bridge is disabled for this profile")
            store = mac_keychain(profile)
            token = resolve_token(profile.access_token_keychain, profile.access_token_env, store)
            if args.command == "serve":
                serve(args, profile, token, store)
            elif args.command == "status":
                status = asyncio.run(read_status(origin, profile.endpoint_key, token, profile.timeout_seconds))
                print(json.dumps(status, ensure_ascii=False, indent=2))
            else:
                backend = getattr(args, "backend", None)
                if backend is not None and backend not in profile.backends:
                    raise ValueError("unknown backend for this profile")
                asyncio.run(bridge(origin, profile.endpoint_key, token, profile.timeout_seconds + 5, session,
                                   backend=backend, management=args.command == "mcp-bridge",
                                   hook_session=os.environ.get(HOOK_ENV) if args.command == "stdio" else None))
        return 0
    except Timeout:
        _LOG.error("Profile state is already owned by another gateway process")
    except (ValueError, FileExistsError, SecretStoreError) as error:
        _LOG.error("%s", error)
    except sqlite3.Error:
        _LOG.error("SQLite storage is unavailable")
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        _LOG.error("Command failed (%s)", type(error).__name__)
    return 1
