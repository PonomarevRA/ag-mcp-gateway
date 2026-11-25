"""OS-user profile operations for the desktop app, outside the HTTP surface."""

import json
import sys
from hashlib import sha256
from pathlib import Path
from secrets import token_hex, token_urlsafe
from typing import Literal

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .credentials import SecretStoreError
from .keychain import MacKeychain
from .policy_settings import write_state
from .profile import Backend, Environment, HttpBackend, Identifier, Profile, StdioBackend, load_profile
from .token_secrets import resolve_token, valid_token

_LIMIT = 32_768


class ProfileChange(BaseModel):
    """One profile creation or additive catalog change from a native form."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: Identifier
    environment: Environment | None = None
    port: int | None = Field(default=None, ge=1024, le=65535, strict=True)
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    backend_name: Identifier
    backend: Backend
    bearer_secret: SecretStr | None = None
    management: bool = Field(default=True, strict=True)



class PortChange(BaseModel):
    """A local port edit tied to a specific profile revision."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: Identifier
    port: int = Field(ge=1024, le=65535, strict=True)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")

def profile_summary(profile: Profile) -> dict:
    """Return display metadata without credentials or upstream addresses."""
    values = profile.model_dump(mode="json", round_trip=True)
    for key in ("denied", "singleflight", "module_chain_tools"):
        values[key].sort()
    normalized = json.dumps(values, sort_keys=True, separators=(",", ":"))
    protected = bool(profile.access_token_keychain or profile.access_token_env)
    backend = profile.backends.get("demo")
    fixture_args = ["demo-backend"] if getattr(sys, "frozen", False) else ["-m", "gateway", "demo-backend"]
    demo = (profile.endpoint_key == "demo" and not protected and len(profile.backends) == 1
            and isinstance(backend, StdioBackend) and backend.args == fixture_args
            and Path(backend.command).resolve() == Path(sys.executable).resolve())
    return {"name": profile.name, "key": profile.endpoint_key, "port": profile.resolve_port(None),
            "protected": protected, "demo": demo, "backends": sorted(profile.backends),
            "revision": sha256(normalized.encode()).hexdigest()}


def profile_inventory(folder: Path) -> dict:
    """Read a bounded set of regular profile files and count invalid entries."""
    profiles, unavailable = [], 0
    for path in sorted(folder.glob("*.json"))[:128]:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("not a regular profile")
            profiles.append(profile_summary(load_profile(folder, path.stem)))
        except (OSError, ValueError):
            unavailable += 1
    return {"profiles": profiles, "unavailable": unavailable}


def save_profile(folder: Path, raw: bytes, keychain_factory=MacKeychain) -> dict:
    """Validate before persisting a profile and use a revision to prevent stale edits."""
    if len(raw) > _LIMIT:
        raise ValueError("Profile request exceeds 32 KiB.")
    try:
        change = ProfileChange.model_validate_json(raw)
    except ValidationError:
        raise ValueError("Check the profile name, MCP name, port and transport fields.") from None
    if isinstance(change.backend, StdioBackend) and not Path(change.backend.command).is_absolute():
        raise ValueError("Select a local executable using an absolute path.")
    upstream_key = None
    upstream_token = change.bearer_secret.get_secret_value() if change.bearer_secret else None
    if upstream_token is not None:
        if not isinstance(change.backend, HttpBackend) or change.backend.basic_keychain or not valid_token(upstream_token):
            raise ValueError("Check the HTTP Bearer token.")
        upstream_key = f"backend-{token_hex(16)}"
        change.backend = change.backend.model_copy(update={"bearer_token_keychain": upstream_key})
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / f"{change.name}.json"
    with FileLock(str(folder / ".desktop-profiles.lock"), timeout=2):
        if path.is_symlink():
            raise ValueError("Profile must be a regular file.")
        if change.revision is None:
            if path.exists():
                raise ValueError("A profile with this name already exists.")
            token_key = f"desktop-{change.name}-{token_hex(8)}"
            values = {"name": change.name, "environment": change.environment, "listen_port": change.port,
                      "access_token_keychain": token_key, "management_bridge": change.management,
                      "agent_discovery": change.management, "modules_enabled": change.management,
                      "backends": {}}
        else:
            if not path.is_file():
                raise ValueError("This profile no longer exists. Refresh the profile list.")
            current = load_profile(folder, change.name)
            if profile_summary(current)["revision"] != change.revision:
                raise ValueError("The profile changed. Reopen the form to refresh it.")
            if not (current.access_token_keychain or current.access_token_env):
                raise ValueError("Create a protected profile before adding MCP servers.")
            if change.backend_name in current.backends:
                raise ValueError("An MCP server with this name already exists in the profile.")
            token_key = None
            values = current.model_dump(round_trip=True)
        values["backends"][change.backend_name] = change.backend
        profile = Profile.model_validate(values)
        body = profile.model_dump_json(indent=2, exclude_defaults=True).encode() + b"\n"
        if len(body) > 262_144:
            raise ValueError("The profile exceeds 256 KiB.")
        entries = {}
        if token_key:
            entries[token_key] = token_urlsafe(32)
        if upstream_key and upstream_token:
            entries[upstream_key] = upstream_token
        persist_profile(path, body, entries, keychain_factory)
    return profile_summary(profile)



def save_port(folder: Path, raw: bytes, *, apply=None) -> dict:
    """Atomically change the listener port while retaining all credentials and backends."""
    if len(raw) > _LIMIT:
        raise ValueError("Profile request exceeds 32 KiB.")
    try:
        change = PortChange.model_validate_json(raw)
    except ValidationError:
        raise ValueError("Check the profile revision and port.") from None
    path = folder / f"{change.name}.json"
    with FileLock(str(folder / ".desktop-profiles.lock"), timeout=2):
        if path.is_symlink() or not path.is_file():
            raise ValueError("The profile is unavailable. Refresh the profile list.")
        current = load_profile(folder, change.name)
        if profile_summary(current)["revision"] != change.revision:
            raise ValueError("The profile changed. Reopen the form to refresh it.")
        profile = current.model_copy(update={"listen_port": change.port})
        body = profile.model_dump_json(indent=2, exclude_defaults=True).encode() + b"\n"
        if len(body) > 262_144:
            raise ValueError("The profile exceeds 256 KiB.")
        if apply is None:
            write_state(path, body)
            return profile_summary(profile)
        original = path.read_bytes()
        restarted = apply(current, profile, lambda: write_state(path, body), lambda: write_state(path, original))
    return {**profile_summary(profile), "restarted": restarted}

def persist_profile(path: Path, body: bytes, entries: dict[str, str], keychain_factory) -> None:
    """Commit the file or compensate for new Keychain entries on a failed write."""
    if not entries:
        write_state(path, body)
        return
    store = keychain_factory()
    created = []
    try:
        for key, value in entries.items():
            store.write_token(key, value)
            created.append(key)
        write_state(path, body)
    except (OSError, SecretStoreError):
        cleanup_failed = False
        for key in reversed(created):
            try:
                store.delete_token(key)
            except SecretStoreError:
                cleanup_failed = True
        if cleanup_failed:
            raise ValueError("The profile was not saved and Keychain cleanup failed.") from None
        raise


def desktop_command(args, stdin, output, token_store, *, apply_port=None) -> None:
    """Serve native commands over local pipes and emit only explicitly requested data."""
    folder = args.profiles.expanduser()
    if args.action == "profiles":
        data = profile_inventory(folder)
    elif args.action == "save":
        data = save_profile(folder, stdin.buffer.read(_LIMIT + 1))
    elif args.action == "port":
        data = save_port(folder, stdin.buffer.read(_LIMIT + 1), apply=apply_port)
    else:
        profile = load_profile(folder, args.profile)
        value = resolve_token(profile.access_token_keychain, profile.access_token_env, token_store(profile))
        if value is None:
            raise ValueError("This profile does not require a gateway token.")
        data = {"token": value}
    print(json.dumps(data), file=output)
