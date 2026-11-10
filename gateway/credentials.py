"""Build Basic authorization and scope saved credentials to one configured upstream."""

import json
import re
from base64 import b64encode
from hashlib import sha256
from typing import Protocol

_MAX_HEADER = 8192
_HEADER = re.compile(r"Basic [A-Za-z0-9+/]+={0,2}")


class SecretStoreError(RuntimeError):
    """Report failure to access the protected credential store."""


class SecretStore(Protocol):
    """Store and retrieve one Basic header without involving profile JSON or logs."""

    def read(self, account: str) -> str | None:
        """Read the saved header for the account, or None when absent."""

    def write(self, account: str, authorization: str) -> None:
        """Replace the saved header for the account, raising on failure."""


def basic_authorization(login: str, password: str) -> str:
    """Encode user input as a Basic header, preserving punctuation and spaces."""
    if not 1 <= len(login) <= 256 or ":" in login or not 1 <= len(password) <= 1024:
        raise ValueError("invalid Basic credentials")
    raw = login + ":" + password
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("invalid Basic credentials")
    return "Basic " + b64encode(raw.encode("utf-8")).decode("ascii")


def valid_authorization(value: str) -> bool:
    """Reject malformed or oversized data returned from external credential storage."""
    return len(value) <= _MAX_HEADER and _HEADER.fullmatch(value) is not None


def credential_account(profile_file: str, endpoint_key: str, backend: str, url: str) -> str:
    """Make a nonsecret Keychain account identifier scoped to profile, backend and URL."""
    identity = json.dumps([profile_file, endpoint_key, backend, url], ensure_ascii=True)
    digest = sha256(identity.encode("ascii")).hexdigest()
    return f"{endpoint_key}/{backend}/{digest}"
