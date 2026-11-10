"""Expose profile telemetry through the local technical-log and diagnostics contracts."""

import asyncio
import re
import sqlite3
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import parse_qsl

from starlette.responses import JSONResponse
from starlette.routing import Route

from .service_diagnostics import Diagnostics, SCHEMA as DIAGNOSTICS_SCHEMA
from .technical_journal import APPLICATION, MAX_PAGE_BYTES, MAX_PAGE_EVENTS, STATUS_SCHEMA

_NO_STORE = {"Cache-Control": "no-store"}
_SEQUENCE = re.compile(r"0|[1-9][0-9]{0,18}")
_QUERY_KEYS = {"limit", "journalEpoch", "beforeSequence", "afterSequence"}
_ERRORS = {
    400: ("invalid_query", "Invalid query parameters."),
    403: ("forbidden", "Access forbidden."),
    409: ("cursor_expired", "Cursor has expired."),
    503: ("journal_unavailable", "Journal is unavailable."),
}


def permitted_request(request) -> bool:
    """Require a loopback peer and reject cross-site browser fetches."""
    if any(value.lower().strip() == "cross-site" for value in request.headers.getlist("sec-fetch-site")):
        return False
    try:
        return request.client is not None and ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def json_response(payload: dict, status: int = 200) -> JSONResponse:
    """Prevent a client or intermediary from caching profile evidence."""
    return JSONResponse(payload, status_code=status, headers=_NO_STORE)


def api_error(status: int, reset=None, *, schema=STATUS_SCHEMA) -> JSONResponse:
    """Return a fixed error without including the input or storage exception."""
    code, message = _ERRORS[status]
    return json_response({"schemaVersion": schema, "application": APPLICATION,
                          "error": {"code": code, "message": message, "retryable": status == 503},
                          "resetCursor": reset}, status)


def sequence_number(value: str) -> int:
    """Decode a canonical nonnegative Int64 decimal sequence."""
    if not _SEQUENCE.fullmatch(value) or int(value) > 2**63 - 1:
        raise ValueError("Invalid log sequence")
    return int(value)


@dataclass(frozen=True)
class EventQuery:
    """Hold one count bound and at most one exclusive journal cursor."""

    limit: int
    epoch: str | None
    after: int | None
    before: int | None

    @classmethod
    def parse(cls, raw: bytes):
        """Validate raw query encoding, supported fields and cursor pairing."""
        if len(raw) > 4096:
            raise ValueError("Query is too large")
        query = raw.decode("utf-8")
        if re.search(r"%(?![0-9a-fA-F]{2})", query):
            raise ValueError("Malformed URL encoding")
        values = {}
        for key, value in parse_qsl(query, keep_blank_values=True, errors="strict"):
            if key not in _QUERY_KEYS or key in values:
                raise ValueError("Unknown or repeated query parameter")
            values[key] = value
        limit = sequence_number(values.get("limit", str(MAX_PAGE_EVENTS)))
        if not 1 <= limit <= MAX_PAGE_EVENTS:
            raise ValueError("Invalid log page size")
        after, before = values.get("afterSequence"), values.get("beforeSequence")
        epoch = values.get("journalEpoch")
        if after is not None and before is not None:
            raise ValueError("Conflicting cursors")
        has_sequence = after is not None or before is not None
        if has_sequence != (epoch is not None) or (epoch is not None and not 1 <= len(epoch) <= 128):
            raise ValueError("Missing or invalid cursor component")
        return cls(limit, epoch, sequence_number(after) if after is not None else None,
                   sequence_number(before) if before is not None else None)

    def expired(self, status: dict) -> bool:
        """Reject a cursor from a reset or outside the committed retention window."""
        if self.epoch is None:
            return False
        sequence = self.after if self.after is not None else self.before
        first = int(status["oldestSequence"]) - 1
        last = int(status["latestSequence"])
        return self.epoch != status["journalEpoch"] or not first <= sequence <= last


def log_page(query: EventQuery, status: dict, rows: list[dict]) -> JSONResponse:
    """Return a contiguous, byte-bounded page whose events always ascend."""
    forward = query.after is not None
    chosen = []
    payload = {
        "schemaVersion": STATUS_SCHEMA, "application": APPLICATION,
        "direction": "forward" if forward else "backward", "events": chosen,
        "nextCursor": dict(status["cursor"]), "highWatermark": status["cursor"],
        "oldestSequence": status["oldestSequence"], "health": status["health"],
        "hasMore": len(rows) > query.limit, "truncated": False,
    }
    # Leave room for cursor and boolean changes after candidate rows are added.
    size = len(json_response(payload).body) + 128
    candidates = rows[:query.limit] if forward else reversed(rows[-query.limit:])
    for row in candidates:
        size += len(json_response(row).body) + 1
        if size > MAX_PAGE_BYTES:
            payload.update(truncated=True, hasMore=True)
            break
        chosen.append(row)
    if not forward:
        chosen.reverse()
    if payload["hasMore"]:
        if not chosen:
            return api_error(503)
        payload["nextCursor"]["sequence"] = chosen[-1 if forward else 0]["sequence"]
    elif not forward:
        payload["nextCursor"]["sequence"] = str(int(status["oldestSequence"]) - 1)
    return json_response(payload)


class TelemetryAPI:
    """Serve logs and metrics without recording the collector's polling traffic."""

    def __init__(self, telemetry, journal):
        """Reuse process counters and the one journal owned by the selected profile."""
        self.journal = journal
        self.diagnostics = Diagnostics(telemetry, journal)

    def routes(self) -> list[Route]:
        """Expose the portable read-only endpoints at fixed versioned paths."""
        return [
            Route("/api/technical-logs/v1/status", self.status),
            Route("/api/technical-logs/v1/events", self.events),
            Route("/api/diagnostics/v1/metrics", self.metrics),
        ]

    async def status(self, request):
        """Expose the committed sequence window and journal health."""
        if not permitted_request(request):
            return api_error(403)
        if request.scope["query_string"]:
            return api_error(400)
        try:
            return json_response(await asyncio.to_thread(self.journal.status))
        except (OSError, sqlite3.Error, ValueError):
            return api_error(503)

    async def events(self, request):
        """Read one coherent journal page without blocking the gateway event loop."""
        if not permitted_request(request):
            return api_error(403)
        try:
            query = EventQuery.parse(request.scope["query_string"])
        except ValueError:
            return api_error(400)
        try:
            status, rows = await asyncio.to_thread(self.journal.page, query.after, query.before, query.limit + 1)
        except (OSError, sqlite3.Error, ValueError):
            return api_error(503)
        if query.expired(status):
            return api_error(409, status["cursor"])
        return log_page(query, status, rows)

    async def metrics(self, request):
        """Report process resources and gateway totals without enumerating tools."""
        if not permitted_request(request):
            return api_error(403, schema=DIAGNOSTICS_SCHEMA)
        if request.scope["query_string"]:
            return api_error(400, schema=DIAGNOSTICS_SCHEMA)
        return json_response(self.diagnostics.snapshot())
