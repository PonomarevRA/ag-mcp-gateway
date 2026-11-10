"""Keep process-local metrics and a bounded metadata-only journal."""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

_LOG = logging.getLogger("gateway")
_SEGMENTS = 4


class Journal:
    """Rotate four NDJSON segments under a shared per-profile byte budget."""

    def __init__(self, folder: Path, max_bytes: int):
        """Trim retained segments to the configured bound before accepting events."""
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.paths = [folder / f"calls.{index}.jsonl" for index in range(_SEGMENTS)]
        self.segment_bytes = max_bytes // _SEGMENTS
        for path in self.paths:
            if path.exists() and path.stat().st_size > self.segment_bytes:
                path.unlink()

    def write(self, event: dict) -> None:
        """Append one event, discarding oldest segments first during rotation."""
        line = json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
        if len(line) > self.segment_bytes:
            return
        current = self.paths[0]
        if current.exists() and current.stat().st_size + len(line) > self.segment_bytes:
            for index in range(_SEGMENTS - 1, 0, -1):
                older, newer = self.paths[index - 1], self.paths[index]
                if older.exists():
                    older.replace(newer)
        with current.open("ab") as sink:
            sink.write(line)


@dataclass
class Counts:
    """Track requests and estimated volume separately at each side of the gateway."""

    received: int = 0
    upstream: int = 0
    merged: int = 0
    denied: int = 0
    errors: int = 0
    input_tokens_estimate: int = 0
    output_tokens_estimate: int = 0
    upstream_output_bytes_avoided_estimate: int = 0
    timed: int = 0
    latency_sum_ms: float = 0
    latency_max_ms: float = 0

    def add_call(self, outcome, elapsed_ms, merged, input_bytes, output_bytes):
        """Record an inbound completion and charge savings only to a joined request."""
        self.received += 1
        self.merged += int(merged)
        self.denied += int(outcome == "denied")
        self.errors += int(outcome not in {"ok", "denied", "cancelled"})
        self.input_tokens_estimate += (input_bytes + 3) // 4
        self.output_tokens_estimate += (output_bytes + 3) // 4
        if merged and outcome == "ok":
            self.upstream_output_bytes_avoided_estimate += output_bytes
        self.timed += 1
        self.latency_sum_ms += elapsed_ms
        self.latency_max_ms = max(self.latency_max_ms, elapsed_ms)

    def snapshot(self) -> dict:
        """Expose a mean and maximum for this process lifetime."""
        data = asdict(self)
        total = data.pop("latency_sum_ms")
        data["latency_mean_ms"] = round(total / self.timed, 2) if self.timed else None
        data["latency_max_ms"] = round(self.latency_max_ms, 2) if self.timed else None
        return data


class Telemetry:
    """Record bounded tool identities and avoid storing arguments or results."""

    def __init__(self, journal: Journal):
        """Start empty counters and append new request completions to the supplied journal."""
        self.journal = journal
        self.total = Counts()
        self.tools: dict[str, Counts] = {}
        self.journal_errors = 0

    def upstream_started(self, tool: str) -> None:
        """Count an actual dispatch, including an upstream that returns an error."""
        self.total.upstream += 1
        self.tools.setdefault(tool, Counts()).upstream += 1

    def completed(self, tool, outcome, elapsed_ms, merged, input_bytes, output_bytes, *, session_tag=None):
        """Update rollups and append one sanitized event per inbound request."""
        for counts in (self.total, self.tools.setdefault(tool, Counts())):
            counts.add_call(outcome, elapsed_ms, merged, input_bytes, output_bytes)
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "outcome": outcome,
            "duration_ms": round(elapsed_ms, 2),
            "joined": merged,
            "request_argument_bytes": input_bytes,
            "result_bytes": output_bytes,
        }
        if session_tag is not None:
            event["session_tag"] = session_tag
        try:
            self.journal.write(event)
        except OSError:
            self.journal_errors += 1
            if self.journal_errors == 1:
                _LOG.warning("Unable to append the gateway call journal")
        _LOG.info("%s %s %.1fms%s", tool, outcome, elapsed_ms, " joined" if merged else "")

    def snapshot(self) -> dict:
        """Return rollups for the CLI and dashboard without reading the journal."""
        return {
            "total": self.total.snapshot(),
            "tools": {name: count.snapshot() for name, count in self.tools.items()},
            "journal_errors": self.journal_errors,
        }
