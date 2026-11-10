"""Portable profile schema and runtime secret resolution."""

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .session_context import SessionField

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$")]
EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
ToolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
Environment = Literal["test", "production"]
_DEFAULT_PORTS = {None: 18792, "test": 18793, "production": 18794}


ArgumentName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]


class BackendOptions(BaseModel):
    """Declare optional, local-only session metadata and argument forwarding."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    forward_session_context: bool = Field(default=False, strict=True)
    session_args: dict[ToolName, dict[ArgumentName, SessionField]] = Field(default_factory=dict, max_length=64)

    @field_validator("session_args")
    @classmethod
    def reject_credential_parameters(cls, mappings):
        """Keep caller provenance separate from conventional credential and lease-key arguments."""
        reserved = {"authorization", "password", "apikey", "secret", "sessionkey", "token",
                    "accesstoken", "bearertoken", "authtoken", "clientsecret"}
        for mapping in mappings.values():
            for parameter in mapping:
                if parameter.replace("_", "").lower() in reserved:
                    raise ValueError("session context cannot populate credentials or lease keys")
        return mappings


class StdioBackend(BackendOptions):
    """Describe a trusted local command and explicit environment forwarding."""

    transport: Literal["stdio"]
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env_from: dict[EnvName, EnvName] = Field(default_factory=dict)


class HttpBackend(BackendOptions):
    """Describe a remote endpoint and an optional bearer-token variable."""

    transport: Literal["http"]
    url: str
    bearer_token_env: EnvName | None = None
    basic_keychain: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_authentication(self):
        """Choose at most one upstream authentication source."""
        if self.basic_keychain and self.bearer_token_env:
            raise ValueError("basic_keychain and bearer_token_env are mutually exclusive")
        if (self.forward_session_context or self.session_args) and not self.loopback:
            raise ValueError("session context forwarding requires a local MCP backend")
        return self

    @property
    def loopback(self) -> bool:
        """Identify an explicitly loopback HTTP endpoint from the validated URL."""
        return urlsplit(self.url).hostname in {"127.0.0.1", "::1", "localhost"}

    @field_validator("url")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        """Require HTTPS except for literal loopback and reject embedded credentials."""
        url = urlsplit(value)
        loopback = url.hostname in {"127.0.0.1", "::1", "localhost"}
        if url.scheme != "https" and not (url.scheme == "http" and loopback):
            raise ValueError("upstream requires HTTPS or loopback HTTP")
        if not url.hostname or url.username is not None or url.password is not None:
            raise ValueError("upstream requires a host and no embedded credentials")
        if url.query or url.fragment:
            raise ValueError("upstream URL must not contain query or fragment")
        return value


Backend = Annotated[StdioBackend | HttpBackend, Field(discriminator="transport")]


class Profile(BaseModel):
    """Bind server identities, policies and journal limits to one local profile."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    name: Identifier
    environment: Environment | None = None
    listen_port: int | None = Field(default=None, ge=1, le=65535, strict=True)
    access_token_env: EnvName | None = None
    log_max_bytes: int = Field(default=100_000_000, ge=4096, le=1_000_000_000, strict=True)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    backends: dict[Identifier, Backend] = Field(min_length=1, max_length=32)
    denied: set[ToolName] = Field(default_factory=set)
    singleflight: set[ToolName] = Field(default_factory=set)

    @property
    def endpoint_key(self) -> str:
        """Return the loopback path suffix, preserving legacy profile URLs."""
        return f"{self.environment}/{self.name}" if self.environment else self.name

    @property
    def state_path(self) -> Path:
        """Return a relative path disjoint from the legacy profile name namespace."""
        return Path(".environments", self.endpoint_key) if self.environment else Path(self.name)

    @property
    def client_name(self) -> str:
        """Give labelled profiles distinct names within the clients' 32-character limit."""
        if self.environment is None:
            return "ag-mcp-gateway"
        label = f"amg-{self.environment}-{self.name}"
        if len(label) <= 32:
            return label
        suffix = sha256(label.encode("ascii")).hexdigest()[:8]
        return f"{label[:23]}-{suffix}"

    def resolve_port(self, override: int | None) -> int:
        """Select the CLI override, profile port or environment default in that order."""
        if override is not None:
            return override
        return self.listen_port or _DEFAULT_PORTS[self.environment]

    @model_validator(mode="after")
    def validate_policy(self):
        """Require production authentication and validate configured tool policies."""
        if self.environment == "production" and self.access_token_env is None:
            raise ValueError("production profile requires access_token_env")
        uses_keychain = any(isinstance(item, HttpBackend) and item.basic_keychain for item in self.backends.values())
        if uses_keychain and self.access_token_env is None:
            raise ValueError("basic_keychain requires a gateway access_token_env")
        if self.denied & self.singleflight:
            raise ValueError("denied and singleflight tool names overlap")
        for name in self.denied | self.singleflight:
            namespace, separator, raw_name = name.partition(".")
            if not separator or not raw_name or namespace not in self.backends:
                raise ValueError("policy tool names require a configured backend prefix")
        return self


def load_profile(folder: Path, name: str) -> Profile:
    """Load one selected JSON profile without accepting a path as its name."""
    if not name or len(name) > 32 or any(c not in _PROFILE_CHARS for c in name):
        raise ValueError("invalid profile name")
    with (folder / f"{name}.json").open("rb") as source:
        body = source.read(262_145)
    if len(body) > 262_144:
        raise ValueError("profile exceeds 256 KiB")
    profile = Profile.model_validate(json.loads(body))
    if profile.name != name:
        raise ValueError("profile name must match the file name")
    return profile


def secret(name: str | None) -> str | None:
    """Resolve an optional environment reference and reject missing or unsafe values."""
    if name is None:
        return None
    value = os.environ.get(name)
    if not value or any(not 33 <= ord(char) <= 126 for char in value):
        raise ValueError(f"missing or invalid environment variable {name}")
    return value


_PROFILE_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
