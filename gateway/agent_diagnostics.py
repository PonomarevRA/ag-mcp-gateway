"""Give authenticated management agents a bounded, read-only diagnosis surface."""

import asyncio

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .profile import Identifier, ToolName
from .telemetry_api import EventQuery

TraceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
SessionTag = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
Sequence = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class LogQuery(BaseModel):
    """Select recent warnings and errors by structured gateway metadata."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    backend: Identifier | None = None
    tool: ToolName | None = None
    trace_id: TraceId | None = None
    session_tag: SessionTag | None = None
    limit: int = Field(default=20, strict=True, ge=1, le=40)
    journal_epoch: str | None = Field(default=None, min_length=1, max_length=128)
    before_sequence: Sequence | None = None

    @model_validator(mode="after")
    def cursor_pair(self):
        """Require both exclusive cursor fields when requesting an older page."""
        if (self.journal_epoch is None) != (self.before_sequence is None):
            raise ValueError("Supply both journal_epoch and before_sequence")
        return self

    def matches(self, event: dict) -> bool:
        """Use exact comparisons only and skip routine INFO events."""
        properties = event["properties"]
        return event["level"] in {"WARN", "ERROR", "FATAL"} and all(
            expected is None or actual == expected
            for expected, actual in ((self.backend, properties.get("backend")), (self.tool, properties.get("tool")),
                                     (self.trace_id, event["traceId"]), (self.session_tag, event["sessionId"]))
        )


class MetricsQuery(BaseModel):
    """Allow a metrics read without permitting arbitrary files or routes."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class AgentDiagnostics:
    """Return curated diagnostics without making upstream calls."""

    def __init__(self, telemetry_api):
        """Use the same committed stream and metrics as the external collector."""
        self.api = telemetry_api
        self.commands = {
            "gateway_read_errors": (LogQuery, "Read recent gateway and MCP warnings and errors. Filter by backend, "
                                    "tool, W3C trace ID or hashed session tag. Returns a backwards cursor.",
                                    True, self.errors),
            "gateway_read_metrics": (MetricsQuery, "Read process CPU, memory, uptime, journal size and gateway call totals.",
                                     True, self.metrics),
        }

    async def errors(self, query: LogQuery) -> dict:
        """Filter one bounded newest-first page and return a continuation if needed."""
        page = EventQuery(100, query.journal_epoch, None, query.before_sequence)
        try:
            status, rows = await asyncio.to_thread(self.api.journal.page, before=query.before_sequence, limit=100)
        except ValueError as error:
            raise OSError("Technical journal is unavailable") from error
        if page.expired(status):
            return {"error": "cursor_expired", "message": "Repeat the query with both cursor fields omitted."}
        events = []
        scanned = 0
        before = int(status["oldestSequence"])
        for row in reversed(rows):
            scanned += 1
            before = int(row["sequence"])
            if query.matches(row):
                events.append(row)
                if len(events) == query.limit:
                    break
        cursor = None
        if before > int(status["oldestSequence"]):
            cursor = {"journal_epoch": status["journalEpoch"], "before_sequence": before}
        return {"events": events, "scanned": scanned, "next_cursor": cursor, "health": status["health"]}

    async def metrics(self, _query: MetricsQuery) -> dict:
        """Return one current resource and counter sample."""
        return self.api.diagnostics.snapshot()
