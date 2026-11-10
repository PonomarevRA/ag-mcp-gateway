"""Keep process-local metrics and a bounded metadata-only journal."""

import json
import logging
import math
import re
from collections import deque
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


    def recent_successes(self, max_bytes: int, limit: int) -> list[tuple[str, float]]:
        """Read a bounded tail of retained successful non-joined journal events."""
        samples = []
        remaining = max_bytes
        for path in self.paths:
            if remaining <= 0 or len(samples) >= limit:
                break
            try:
                with path.open("rb") as source:
                    size = source.seek(0, 2)
                    offset = max(0, size - remaining)
                    source.seek(offset)
                    data = source.read(remaining)
            except FileNotFoundError:
                continue
            except OSError:
                _LOG.warning("Unable to read tool latency history")
                break
            remaining -= len(data)
            if offset:
                data = data.partition(b"\n")[2]
            for line in reversed(data.splitlines()):
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if not isinstance(event, dict) or event.get("outcome") != "ok" or event.get("joined") is not False:
                    continue
                name, elapsed = event.get("tool"), event.get("duration_ms")
                if (isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) and "." in name and
                        type(elapsed) in (int, float) and 0 <= elapsed < 86_400_000 and math.isfinite(elapsed)):
                    samples.append((name, elapsed))
                    if len(samples) == limit:
                        break
        samples.reverse()
        return samples


class LatencyHistory:
    """Maintain per-tool means over a bounded profile-wide sample window."""

    LIMIT = 10_000
    STARTUP_BYTES = 4 * 1024 * 1024

    def __init__(self, journal: Journal):
        """Seed from the newest retained journal data, oldest samples first."""
        self.samples = deque()
        self.totals: dict[str, tuple[int, int]] = {}
        for name, elapsed in journal.recent_successes(self.STARTUP_BYTES, self.LIMIT):
            self.add(name, elapsed)

    def add(self, name: str, elapsed: float):
        """Insert one successful observation and remove the oldest one at capacity."""
        centi_ms = round(elapsed * 100)
        if len(self.samples) == self.LIMIT:
            old_name, old_duration = self.samples.popleft()
            total, count = self.totals[old_name]
            if count == 1:
                del self.totals[old_name]
            else:
                self.totals[old_name] = total - old_duration, count - 1
        total, count = self.totals.get(name, (0, 0))
        self.totals[name] = total + centi_ms, count + 1
        self.samples.append((name, centi_ms))

    def mean(self, name: str) -> tuple[float | None, int]:
        """Return average gateway duration in milliseconds and the number of samples."""
        total, count = self.totals.get(name, (0, 0))
        return (round(total / (100 * count), 2), count) if count else (None, 0)


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

    def __init__(self, journal: Journal, *, with_history: bool = False):
        """Start empty counters and append new request completions to the supplied journal."""
        self.journal = journal
        self.total = Counts()
        self.tools: dict[str, Counts] = {}
        self.journal_errors = 0
        self._last_upstream: dict[str, str] = {}
        self.history = LatencyHistory(journal) if with_history else None

    def upstream_started(self, tool: str) -> None:
        """Count and timestamp an actual dispatch, including calls that return errors."""
        self.total.upstream += 1
        self.tools.setdefault(tool, Counts()).upstream += 1
        self._last_upstream[tool.partition(".")[0]] = datetime.now(timezone.utc).isoformat()

    def last_upstream_at(self, backend: str) -> str | None:
        """Return the latest upstream dispatch time for this backend in UTC."""
        return self._last_upstream.get(backend)

    def tool_latency(self, tool: str) -> tuple[float | None, int]:
        """Expose observed successful latency when agent discovery is enabled."""
        return self.history.mean(tool) if self.history is not None else (None, 0)

    def completed(self, tool, outcome, elapsed_ms, merged, input_bytes, output_bytes, *,
                  session_tag=None, trace_id=None):
        """Update rollups and append one sanitized event per inbound request."""
        duration = round(elapsed_ms, 2)
        if self.history is not None and outcome == "ok" and not merged:
            self.history.add(tool, duration)
        for counts in (self.total, self.tools.setdefault(tool, Counts())):
            counts.add_call(outcome, elapsed_ms, merged, input_bytes, output_bytes)
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "outcome": outcome,
            "duration_ms": duration,
            "joined": merged,
            "request_argument_bytes": input_bytes,
            "result_bytes": output_bytes,
        }
        if session_tag is not None:
            event["session_tag"] = session_tag
        if trace_id is not None:
            event["trace_id"] = trace_id
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
