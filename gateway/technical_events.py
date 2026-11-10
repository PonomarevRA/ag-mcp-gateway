"""Build technical events exclusively from gateway-owned diagnostic metadata."""

import logging
from datetime import datetime, timezone

from .connection_diagnostics import FAILURE_LABELS
from .technical_journal import APPLICATION, EVENT_SCHEMA

_LOG = logging.getLogger("gateway")
CALL_ERRORS = {
    "invalid_session": "Invalid gateway session context.",
    "policy_denied": "Tool is disabled by gateway policy.",
    "unknown_tool": "Tool is unavailable in the catalog.",
    "read_only_required": "Module step requires a read-only tool.",
    "hooks_disabled": "Hook sessions are disabled.",
    "invalid_session_mapping": "Configured upstream session mapping is invalid.",
    "request_hook_failed": "Request hook failed.",
    "response_hook_failed": "Response hook failed after the upstream call.",
    "invalid_arguments": "Tool arguments are invalid.",
    "connection_changed": "Upstream connection changed before dispatch.",
    "upstream_timeout": "Upstream call timed out.",
    "upstream_transport": "Upstream transport failed.",
    "invalid_upstream_output": "Upstream tool output failed schema validation.",
    "upstream_tool_error": "Upstream returned a tool error.",
    "internal_error": "Gateway tool processing failed.",
}


class TechnicalEvents:
    """Write a safe event stream independent of request bodies and the call journal."""

    def __init__(self, journal, profile: str):
        """Bind all emitted events to the selected gateway profile."""
        self.journal = journal
        self.profile = profile

    def emit(self, level: str, category: str, name: str, message: str, *,
             trace_id=None, session_tag=None, **properties):
        """Record a fixed gateway message and explicitly supplied metadata."""
        event = {
            "schemaVersion": EVENT_SCHEMA, "application": APPLICATION,
            "occurredAt": datetime.now(timezone.utc).isoformat(),
            "level": level, "category": f"gateway.{category}", "eventName": name,
            "messageTemplate": message, "message": message, "exception": None,
            "traceId": trace_id, "sessionId": session_tag,
            "spanId": None, "operationId": None, "taskId": None,
            "properties": {"profile": self.profile, **properties},
        }
        self.journal.emit(event)
        _LOG.log(logging.WARNING if level == "WARN" else getattr(logging, level),
                 "%s %s %s", name, message, event["properties"])

    def connection(self, backend: str, status: str, *, replacement: bool = False):
        """Describe a connection transition or replacement with a classified reason."""
        ready = status == "ready"
        code = status if status in FAILURE_LABELS or ready else "connection-failed"
        name = "backend.replacement" if replacement else "backend.connection"
        message = "MCP connection is ready." if ready else "MCP connection is unavailable."
        self.emit("INFO" if ready else "ERROR", "connection", name, message,
                  backend=backend, status=code)

    def call_error(self, tool: str, outcome: str, code: str, duration: float,
                   trace_id: str | None, session_tag: str | None, *, joined: bool):
        """Log a classified failure without exposing upstream-supplied error text."""
        if code not in CALL_ERRORS:
            code = "internal_error"
        backend = tool.partition(".")[0] if "." in tool else None
        level = "WARN" if outcome in {"unknown", "denied"} else "ERROR"
        self.emit(level, "call", "tool.rejected" if level == "WARN" else "tool.failed", CALL_ERRORS[code],
                  trace_id=trace_id, session_tag=session_tag, tool=tool, backend=backend,
                  outcome=outcome, errorCode=code, durationMs=duration, joined=joined)
