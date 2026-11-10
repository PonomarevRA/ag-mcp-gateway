"""Resolve named authentication tokens with the OS store taking precedence."""

from typing import Protocol

from .credentials import SecretStoreError
from .profile import secret


class TokenStore(Protocol):
    """Keep printable authentication tokens under a typed OS account."""

    def read_token(self, key: str) -> str | None:
        """Return the named token, or None for a missing item."""

    def write_token(self, key: str, value: str) -> None:
        """Write one named token without exposing it through process arguments."""


def valid_token(value: str) -> bool:
    """Accept bounded printable ASCII suitable for an HTTP authorization value."""
    return 1 <= len(value) <= 8192 and all(33 <= ord(char) <= 126 for char in value)


def resolve_token(key: str | None, env: str | None, store: TokenStore | None) -> str | None:
    """Try an explicitly named OS item first, then an explicitly configured environment reference."""
    if key is None:
        return secret(env)
    if store is not None:
        value = store.read_token(key)
        if value is not None:
            if not valid_token(value):
                raise SecretStoreError("invalid OS token")
            return value
    if env is not None:
        return secret(env)
    if store is None:
        raise SecretStoreError("OS secret store is unavailable")
    raise ValueError("configured OS token is missing")
