"""Bound the retention and per-session activation of reusable MCP hooks."""

import json
import secrets
import time
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .module_data import ModuleError

HOOK_META = "ag-mcp-gateway/hook-session"
HOOK_ENV = "AG_MCP_HOOK_SESSION"
_SESSION_SECONDS = 3600
_MAX_SESSIONS = 64
_MAX_ACTIVATIONS = 16
MAX_SURVIVED = 16
MAX_SESSION_USES = 64
_RETENTION = {3: 86400, 2: 7 * 86400, 1: 30 * 86400}


class GenerationRules(BaseModel):
    """Set the number of distinct successful sessions needed at each threshold."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    gen2_sessions: int = Field(default=2, ge=2, le=15, strict=True)
    gen1_sessions: int = Field(default=4, ge=3, le=MAX_SURVIVED, strict=True)

    @model_validator(mode="after")
    def check_thresholds(self):
        """Require the older generation to have a strictly higher threshold."""
        if self.gen1_sessions <= self.gen2_sessions:
            raise ValueError("Gen1 threshold must exceed Gen2")
        return self


class SurvivedSession(BaseModel):
    """Retain only a random public identifier and the first successful application time."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    used_at: int = Field(ge=0, strict=True)


class HookLife(BaseModel):
    """Describe an unpinned or pinned local hook independently of live activations."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    created_at: int = Field(ge=0, strict=True)
    sessions: list[SurvivedSession] = Field(default_factory=list, max_length=MAX_SURVIVED)
    last_used_at: int | None = Field(default=None, ge=0, strict=True)
    pinned: bool = Field(default=False, strict=True)

    def generation(self, rules: GenerationRules) -> int:
        """Derive the generation from distinct successful sessions and current settings."""
        count = len(self.sessions)
        if count >= rules.gen1_sessions:
            return 1
        return 2 if count >= rules.gen2_sessions else 3

    def deadline(self, rules: GenerationRules) -> int:
        """Expire idle templates according to their current generation."""
        return (self.last_used_at or self.created_at) + _RETENTION[self.generation(rules)]

    def used(self, identity: str, now: int):
        """Record one success per session and keep only the latest bounded history."""
        if any(session.id == identity for session in self.sessions):
            return self
        survivors = [*self.sessions, SurvivedSession(id=identity, used_at=now)]
        return self.model_copy(update={"sessions": survivors[-MAX_SURVIVED:], "last_used_at": now})


def fingerprint(value) -> str:
    """Hash the content defining an activation without storing or exposing it in the lease."""
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return sha256(data).hexdigest()


@dataclass(frozen=True)
class Activation:
    """Pin a local module to its definition and the bound tool schemas."""

    module: str
    definition: str
    schema: str


@dataclass
class HookSession:
    """Own the active per-tool bindings for one server-issued session."""

    id: str
    expires_at: int
    bindings: dict[tuple[str, str], Activation] = field(default_factory=dict)
    used: set[tuple[str, str]] = field(default_factory=set)


class HookSessions:
    """Store short-lived capabilities in memory and expose only pseudonymous IDs."""

    def __init__(self):
        """Allocate a fresh capability namespace for this profile process."""
        self.sessions: dict[str, HookSession] = {}

    def reap(self, now: int):
        """Drop expired capabilities and their temporary bindings."""
        self.sessions = {key: session for key, session in self.sessions.items() if session.expires_at > now}

    def open(self) -> dict:
        """Create one bounded session and reveal the capability only to its creator."""
        now = int(time.time())
        self.reap(now)
        if len(self.sessions) >= _MAX_SESSIONS:
            raise ModuleError("Hook session limit reached")
        token = secrets.token_urlsafe(32)
        session = HookSession(uuid4().hex, now + _SESSION_SECONDS)
        self.sessions[fingerprint(token)] = session
        return {"session_id": session.id, "capability": token, "expires_at": session.expires_at}

    def get(self, token) -> HookSession:
        """Resolve only an unexpired server-issued capability."""
        if not isinstance(token, str) or len(token) != 43:
            raise ModuleError("Invalid or expired hook session")
        session = self.sessions.get(fingerprint(token))
        if session is None or session.expires_at <= int(time.time()):
            raise ModuleError("Invalid or expired hook session")
        return session

    def bind(self, session: HookSession, tool: str, phase: str, activation: Activation | None):
        """Add or remove one temporary hook without changing profile bindings."""
        key = (tool, phase)
        if activation is None:
            session.bindings.pop(key, None)
            return
        if key not in session.bindings and len(session.bindings) >= _MAX_ACTIVATIONS:
            raise ModuleError("Hook activation limit reached")
        session.bindings[key] = activation

    def close(self, token):
        """Revoke a capability and stop it from authorizing future calls."""
        session = self.get(token)
        self.sessions.pop(fingerprint(token), None)
        return {"session_id": session.id, "closed": True}

    def catalog(self) -> list[dict]:
        """Return active session IDs and hook counts without returning capabilities."""
        now = int(time.time())
        return [{"id": session.id, "expires_at": session.expires_at, "bindings": len(session.bindings)}
                for session in self.sessions.values() if session.expires_at > now]
