"""Accept explicit session provenance and bind it to selected local tool arguments."""

import argparse
import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

SESSION_META = "ag-mcp-gateway/session"
SessionField = Literal["harness", "instanceId", "remoteSessionId", "conversationId"]
_OPTIONS = {
    "harness": "--harness",
    "instanceId": "--instance-id",
    "remoteSessionId": "--session-id",
    "conversationId": "--conversation-id",
}
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True)
class SessionContext:
    """Carry caller-declared identities without granting task or credential authority."""

    values: dict[str, str]

    @classmethod
    def parse(cls, value) -> "SessionContext":
        """Copy supported nonsecret fields and reject malformed or ambiguous values."""
        if not isinstance(value, dict) or value.keys() - _OPTIONS.keys():
            raise ValueError("invalid gateway session context")
        if not {"remoteSessionId", "conversationId"} & value.keys():
            raise ValueError("provide a remote session ID or a conversation ID")
        fields = {}
        for key, item in value.items():
            if not isinstance(item, str):
                raise ValueError("invalid gateway session identity")
            if key == "remoteSessionId":
                valid = 1 <= len(item) <= 512 and item.isprintable() and not any(c.isspace() for c in item)
            else:
                valid = bool(_OPAQUE_ID.fullmatch(item))
            if not valid:
                raise ValueError("invalid gateway session identity")
            fields[key] = item
        return cls(fields)

    @property
    def tag(self) -> str:
        """Derive the same journal tag for equivalent identifiers regardless of order."""
        if "remoteSessionId" in self.values:
            identity = {key: value for key, value in self.values.items() if key != "conversationId"}
        else:
            identity = {"conversationId": self.values["conversationId"]}
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("ascii")
        return sha256(canonical).hexdigest()[:16]

    def bind(self, arguments: dict, mapping: dict, schema: dict) -> dict:
        """Supply missing configured arguments only when the upstream schema declares them."""
        result = arguments.copy()
        properties = schema.get("properties")
        for argument, field in mapping.items():
            if argument in result or field not in self.values:
                continue
            if not isinstance(properties, dict) or argument not in properties:
                raise ValueError("session parameter is missing from the upstream schema")
            result[argument] = self.values[field]
        return result

    def cli_arguments(self) -> list[str]:
        """Serialize the selected identity fields for a generated stdio bridge command."""
        result = []
        for field, value in self.values.items():
            result.append(f"{_OPTIONS[field]}={value}")
        return result


def add_session_options(parser: argparse.ArgumentParser):
    """Declare the same explicit identity switches on stdio, config and session-tag."""
    for field, flag in _OPTIONS.items():
        parser.add_argument(flag, dest=field, help=f"caller-declared {field}")


def session_from_cli(args) -> SessionContext | None:
    """Parse the identity options present on this command, if any."""
    values = {}
    for field in _OPTIONS:
        value = getattr(args, field, None)
        if value is not None:
            values[field] = value
    return SessionContext.parse(values) if values else None
