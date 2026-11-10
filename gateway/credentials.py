"""Build Basic authorization and scope saved credentials to one configured upstream."""

import json
import re
from base64 import b64decode, b64encode
from binascii import Error as Base64Error
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


def pasted_authorization(value: str) -> str:
    """Validate a pasted Basic or raw Base64 pair and return a canonical Basic header."""
    value = value.strip()
    if value[:6].lower() == "basic ":
        value = value[6:].strip()
    if not value or len(value) + len("Basic ") > _MAX_HEADER:
        raise ValueError("invalid Basic value")
    try:
        decoded = b64decode(value, validate=True)
    except (Base64Error, ValueError) as error:
        raise ValueError("invalid Basic value") from error
    login, separator, password = decoded.partition(b":")
    if not separator or not 1 <= len(login) <= 1024 or not 1 <= len(password) <= 4096:
        raise ValueError("invalid Basic value")
    if any(byte < 32 or byte == 127 for byte in decoded):
        raise ValueError("invalid Basic value")
    return "Basic " + b64encode(decoded).decode("ascii")


def valid_authorization(value: str) -> bool:
    """Reject malformed or oversized data returned from external credential storage."""
    return len(value) <= _MAX_HEADER and _HEADER.fullmatch(value) is not None


def credential_account(profile_file: str, endpoint_key: str, backend: str, url: str) -> str:
    """Make a nonsecret Keychain account identifier scoped to profile, backend and URL."""
    identity = json.dumps([profile_file, endpoint_key, backend, url], ensure_ascii=True)
    digest = sha256(identity.encode("ascii")).hexdigest()
    return f"{endpoint_key}/{backend}/{digest}"
