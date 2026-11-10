"""Persist revisioned tool policy overrides for one running gateway profile."""

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .profile import Profile, ToolName

Mode = Literal["allow", "deny", "singleflight"]
_MAX_OVERRIDES = 1024
_MAX_BYTES = 262_144


class PolicyChange(BaseModel):
    """Apply one explicit tool policy against the last observed revision."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    tool: ToolName
    policy: Mode
    expected_revision: int = Field(ge=0, strict=True)


class PolicyState(BaseModel):
    """Store only tool overrides and a monotonically increasing revision."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    revision: int = Field(default=0, ge=0, strict=True)
    overrides: dict[ToolName, Mode] = Field(default_factory=dict, max_length=_MAX_OVERRIDES)


class ToolPolicySettings:
    """Commit durable changes before making them visible to the router."""

    def __init__(self, router, path: Path):
        """Apply persisted overrides over the profile's declarative defaults."""
        self.router = router
        self.path = path
        try:
            with path.open("rb") as source:
                raw = source.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise ValueError("tool policy state exceeds 256 KiB")
            self.state = PolicyState.model_validate_json(raw)
        except FileNotFoundError:
            self.state = PolicyState()
        self.baseline_denied = router.profile.denied.copy()
        self.baseline_singleflight = router.profile.singleflight.copy()
        self._publish(self.state)

    def _publish(self, state: PolicyState) -> None:
        """Update router policy and invalidate previously issued catalog cursors."""
        profile: Profile = self.router.profile
        denied = self.baseline_denied.copy()
        singleflight = self.baseline_singleflight.copy()
        for tool, mode in state.overrides.items():
            backend, separator, name = tool.partition(".")
            if not separator or not name or backend not in profile.backends:
                continue
            denied.discard(tool)
            singleflight.discard(tool)
            if mode == "deny":
                denied.add(tool)
            elif mode == "singleflight":
                singleflight.add(tool)
        if self.router.modules is not None:
            for tool in denied - profile.denied:
                self.router.modules.sessions.invalidate(tool)
        if profile.denied != denied or profile.singleflight != singleflight:
            profile.denied = denied
            profile.singleflight = singleflight
            self.router.catalog_revision += 1
        self.router.management_singleflight = singleflight - self.baseline_singleflight
        self.state = state

    def change(self, change: PolicyChange) -> int:
        """Write one small CAS update atomically, then expose it on the event loop."""
        if change.expected_revision != self.state.revision:
            raise ValueError("settings revision changed; inspect and retry")
        profile: Profile = self.router.profile
        tool = change.tool
        if tool not in self.router.routes and tool not in profile.denied and tool not in profile.singleflight:
            if tool not in self.state.overrides or tool.partition(".")[0] not in profile.backends:
                raise ValueError("tool is not in this profile's catalog or policy")
        default = "allow"
        if tool in self.baseline_denied:
            default = "deny"
        elif tool in self.baseline_singleflight:
            default = "singleflight"
        if change.policy == "singleflight" and default != "singleflight":
            route = self.router.routes.get(tool)
            annotations = route.tool.annotations if route is not None else None
            if annotations is None or annotations.readOnlyHint is not True:
                raise ValueError("singleflight requires a discovered read-only tool")
        overrides = self.state.overrides.copy()
        if change.policy == default:
            overrides.pop(tool, None)
        else:
            overrides[tool] = change.policy
        if overrides == self.state.overrides:
            return self.state.revision
        state = PolicyState(revision=self.state.revision + 1, overrides=overrides)
        self._write(state)
        self._publish(state)
        return state.revision

    def _write(self, state: PolicyState) -> None:
        """Serialize the bounded tool policy snapshot for atomic replacement."""
        data = json.dumps(state.model_dump(), separators=(",", ":")).encode()
        write_state(self.path, data)


def write_state(path: Path, data: bytes) -> None:
    """Replace one private profile document after flushing its new content."""
    descriptor, filename = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as sink:
            sink.write(data)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(filename, path)
    finally:
        Path(filename).unlink(missing_ok=True)
