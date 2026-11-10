"""Validate and carry W3C trace context without an exporter or shared mutable headers."""

import json
import re
from dataclasses import dataclass

import httpx
from starlette.datastructures import Headers

TRACE_FIELDS = frozenset({"traceparent", "tracestate"})
_PARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}")
_STATE_KEY = re.compile(r"(?:[a-z][a-z0-9_*/-]{0,255}|[a-z0-9][a-z0-9_*/-]{0,240}@[a-z][a-z0-9_*/-]{0,13})")
_STATE_VALUE = re.compile(r"[\x20-\x2b\x2d-\x3c\x3e-\x7e]{1,256}")


@dataclass(frozen=True)
class TraceContext:
    """One validated request carrier and its correlation ID."""

    parent: str
    trace_id: str
    state: str | None = None

    @classmethod
    def from_metadata(cls, metadata: dict | None) -> "TraceContext | None":
        """Accept version 00 with nonzero IDs, dropping malformed vendor state independently."""
        if metadata is None:
            return None
        parent = metadata.get("traceparent")
        trace_id = _valid_parent_id(parent) if isinstance(parent, str) else None
        if trace_id is None:
            return None
        return cls(parent, trace_id, _valid_state(metadata.get("tracestate")))

    def carrier(self) -> dict[str, str]:
        """Return only validated W3C fields for this one call."""
        result = {"traceparent": self.parent}
        if self.state is not None:
            result["tracestate"] = self.state
        return result


def _valid_parent_id(parent: str) -> str | None:
    """Extract the trace ID only when both version-00 identifiers are valid."""
    match = _PARENT.fullmatch(parent)
    if match is None or match[1] == "0" * 32 or match[2] == "0" * 16:
        return None
    return match[1]


def _valid_state(value) -> str | None:
    """Normalize at most 32 distinct printable entries within a 512-character budget."""
    if not isinstance(value, str) or len(value) > 512:
        return None
    members = value.split(",")
    if len(members) > 32:
        return None
    entries = {}
    for member in members:
        item = member.strip(" \t")
        if not item:
            continue
        key, _, state = item.partition("=")
        if not _STATE_KEY.fullmatch(key) or key in entries or not _STATE_VALUE.fullmatch(state):
            return None
        entries[key] = state
    return ",".join(f"{key}={state}" for key, state in entries.items()) or None


def without_trace_fields(metadata: dict | None) -> dict | None:
    """Remove the trace carrier and keep the original distinction for explicit empty metadata."""
    if not metadata or not any(key.lower() in TRACE_FIELDS for key in metadata):
        return metadata
    return {key: value for key, value in metadata.items() if key.lower() not in TRACE_FIELDS} or None


def with_http_trace(metadata: dict | None, headers: Headers) -> dict | None:
    """Prefer an explicit HTTP carrier as a pair, never mixing header and MCP state."""
    parents = headers.getlist("traceparent")
    if not parents:
        return metadata
    other = without_trace_fields(metadata)
    if len(parents) != 1:
        return other
    parent = parents[0].strip(" \t")
    if _valid_parent_id(parent) is None:
        return other
    result = dict(other or {})
    result["traceparent"] = parent
    states = headers.getlist("tracestate")
    if states and sum(map(len, states)) + len(states) - 1 <= 512:
        result["tracestate"] = ",".join(states)
    return result


async def inject_http_trace(request: httpx.Request) -> None:
    """Promote the current SDK tool message's MCP metadata to HTTP headers after queueing."""
    if request.method != "POST" or b'"traceparent"' not in request.content:
        return
    # The SDK serializes one JSON-RPC message per POST. The carrier travels with
    # that message even when the send runs on the long-lived transport worker.
    message = json.loads(request.content)
    if message.get("method") != "tools/call":
        return
    trace = TraceContext.from_metadata(message["params"].get("_meta"))
    if trace is not None:
        request.headers.update(trace.carrier())
