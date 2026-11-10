"""Read and update generic passwords in a single macOS Keychain."""

import ctypes as c
import os
from contextlib import contextmanager
from pathlib import Path

from .credentials import SecretStoreError, valid_authorization
from .token_secrets import valid_token

_SERVICE = b"ag-mcp-gateway"
_NOT_FOUND = -25300
_POINTER = c.c_void_p
_SIZE = c.c_uint32
_TEXT = c.c_char_p


class MacKeychain:
    """Use the OS default keychain, or an explicit keychain for isolated tests."""

    def __init__(self, path: Path | None = None):
        """Bind Security.framework without loading any platform library on import."""
        self.path = path
        try:
            security = c.CDLL("/System/Library/Frameworks/Security.framework/Security")
            foundation = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        except OSError as error:
            raise SecretStoreError("macOS Keychain is unavailable") from error

        self.default = _bind(security, "SecKeychainCopyDefault", [c.POINTER(_POINTER)])
        self.open = _bind(security, "SecKeychainOpen", [_TEXT, c.POINTER(_POINTER)])
        self.find = _bind(security, "SecKeychainFindGenericPassword", [
            _POINTER, _SIZE, _TEXT, _SIZE, _TEXT, c.POINTER(_SIZE), c.POINTER(_POINTER), c.POINTER(_POINTER),
        ])
        self.add = _bind(security, "SecKeychainAddGenericPassword",
                         [_POINTER, _SIZE, _TEXT, _SIZE, _TEXT, _SIZE, _TEXT, c.POINTER(_POINTER)])
        self.modify = _bind(security, "SecKeychainItemModifyAttributesAndData",
                            [_POINTER, _POINTER, _SIZE, _TEXT])
        self.free_data = _bind(security, "SecKeychainItemFreeContent", [_POINTER, _POINTER])
        self.release = _bind(foundation, "CFRelease", [_POINTER], result=None)

    @contextmanager
    def _keychain(self):
        """Pin only the selected keychain and release the native reference after use."""
        ref = _POINTER()
        status = self.open(os.fsencode(self.path), c.byref(ref)) if self.path else self.default(c.byref(ref))
        _check(status)
        try:
            yield ref
        finally:
            self.release(ref)

    def read(self, account: str) -> str | None:
        """Read an existing Basic item using its original account namespace."""
        return self._read(account, valid_authorization)

    def read_token(self, key: str) -> str | None:
        """Read a token in the namespace distinct from Basic accounts."""
        return self._read(f"token/{key}", valid_token)

    def _read(self, account: str, validate) -> str | None:
        """Read and validate an ASCII value, then free the native password buffer."""
        name = account.encode("ascii")
        data = _POINTER()
        length = _SIZE()
        with self._keychain() as keychain:
            status = self.find(keychain, len(_SERVICE), _SERVICE, len(name), name,
                               c.byref(length), c.byref(data), None)
            if status == _NOT_FOUND:
                return None
            _check(status)
            try:
                if not data or not 1 <= length.value <= 8192:
                    raise SecretStoreError("invalid Keychain item")
                value = c.string_at(data, length.value).decode("ascii")
                if not validate(value):
                    raise SecretStoreError("invalid Keychain item")
                return value
            except UnicodeError as error:
                raise SecretStoreError("invalid Keychain item") from error
            finally:
                if data:
                    self.free_data(None, data)

    def write(self, account: str, authorization: str) -> None:
        """Store Basic credentials under their existing account namespace."""
        self._write(account, authorization, valid_authorization)

    def write_token(self, key: str, value: str) -> None:
        """Store a token without passing it through a subprocess."""
        self._write(f"token/{key}", value, valid_token)

    def _write(self, account: str, value: str, validate) -> None:
        """Add or replace a validated generic password through Security.framework."""
        if not validate(value):
            raise SecretStoreError("invalid credential value")
        name = account.encode("ascii")
        data = value.encode("ascii")
        item = _POINTER()
        with self._keychain() as keychain:
            status = self.find(keychain, len(_SERVICE), _SERVICE, len(name), name, None, None, c.byref(item))
            if status == _NOT_FOUND:
                _check(self.add(keychain, len(_SERVICE), _SERVICE, len(name), name, len(data), data, None))
                return
            _check(status)
            try:
                _check(self.modify(item, None, len(data), data))
            finally:
                self.release(item)


def _bind(library, name: str, args: list, result=c.c_int32):
    """Declare the C argument and return types of one framework symbol."""
    method = getattr(library, name)
    method.argtypes = args
    method.restype = result
    return method


def _check(status: int) -> None:
    """Map a Security framework error code to a credential-store failure."""
    if status:
        raise SecretStoreError(f"Keychain operation failed ({status})")
