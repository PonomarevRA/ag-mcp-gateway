"""Keep process-local metrics and a bounded metadata-only journal."""

import json
import logging
import math
import os
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import time

_LOG = logging.getLogger("gateway")
_SEGMENTS = 4
LOG_FILE_MAX_BYTES = 50_000_000
DEFAULT_LOG_RETENTION_DAYS = 7
LOG_RETENTION_INTERVAL_SECONDS = 60
_RETIRED_TOOL_LIMIT = 256


class Journal:
    """Rotate four NDJSON segments under a shared per-profile byte budget."""

    def __init__(self, folder: Path, max_bytes: int):
        """Trim retained segments to the configured bound before accepting events."""
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.paths = [folder / f"calls.{index}.jsonl" for index in range(_SEGMENTS)]
        self.segment_bytes = min(max_bytes // _SEGMENTS, LOG_FILE_MAX_BYTES)
        for path in self.paths:
            if path.exists() and path.stat().st_size > self.segment_bytes:
                path.unlink()

    def prune_expired(self, retention_days: int) -> None:
        """Remove segments whose last append predates the configured retention window."""
        cutoff = time() - retention_days * 86400
        for path in self.paths:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                _LOG.warning("Unable to expire a gateway call journal segment")

    def write(self, event: dict) -> None:
        """Append one event, discarding oldest segments first during rotation."""
        line = json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
        if len(line) > self.segment_bytes:
            return
        current = self.paths[0]
        with current.open("ab") as sink:
            if sink.tell() + len(line) <= self.segment_bytes:
                sink.write(line)
                return
        for index in range(_SEGMENTS - 1, 0, -1):
            older, newer = self.paths[index - 1], self.paths[index]
            try:
                older.replace(newer)
            except FileNotFoundError:
                continue
        with current.open("ab") as sink:
            sink.write(line)

    def recent_successes(self, max_bytes: int, limit: int) -> list[tuple[str, float]]:
        """Read a bounded tail of retained successful non-joined journal events."""
        samples = []
        for event in self.newest_events(max_bytes):
            if event.get("outcome") != "ok" or event.get("joined") is not False:
                continue
            name, elapsed = event.get("tool"), event.get("duration_ms")
            if (isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) and "." in name and
                    type(elapsed) in (int, float) and 0 <= elapsed < 86_400_000 and math.isfinite(elapsed)):
                samples.append((name, elapsed))
                if len(samples) == limit:
                    break
        samples.reverse()
        return samples

    def newest_events(self, max_bytes: int):
        """Yield complete journal events newest first within a total read budget."""
        seen = set()
        remaining = max_bytes
        for path in self.paths:
            if remaining <= 0:
                break
            try:
                with path.open("rb") as source:
                    stat = os.fstat(source.fileno())
                    identity = stat.st_dev, stat.st_ino
                    if stat.st_ino and identity in seen:
                        continue
                    if stat.st_ino:
                        seen.add(identity)
                    size = source.seek(0, 2)
                    offset = max(0, size - remaining)
                    source.seek(offset)
                    data = source.read(remaining)
            except FileNotFoundError:
                continue
            except OSError:
                _LOG.warning("Unable to read gateway call history")
                break
            remaining -= len(data)
            if offset:
                data = data.partition(b"\n")[2]
            for line in reversed(data.split(b"\n")[:-1]):
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if isinstance(event, dict):
                    yield event


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

    def __init__(self, journal: Journal, *, with_history: bool = False, technical=None):
        """Start empty counters and append new request completions to the supplied journal."""
        self.journal = journal
        self.technical = technical
        self.total = Counts()
        self.tools: dict[str, Counts] = {}
        self._backends: dict[str, Counts] = {}
        self.journal_errors = 0
        self._last_upstream: dict[str, str] = {}
        self.history = LatencyHistory(journal) if with_history else None

    def register_backends(self, names: Iterable[str]) -> None:
        """Limit process-lifetime rollups to the configured backend identities."""
        self._backends = {name: Counts() for name in names}

    def statistics(self, backend: str | None = None) -> dict:
        """Summarize the gateway or one configured backend independently of the catalog page."""
        counts = self.total if backend is None else self._backends[backend]
        return {"total": counts.snapshot(), "journal_errors": self.journal_errors}

    def upstream_started(self, tool: str) -> None:
        """Count and timestamp an actual dispatch, including calls that return errors."""
        self.total.upstream += 1
        self.tools.setdefault(tool, Counts()).upstream += 1
        backend = tool.partition(".")[0]
        backend_counts = self._backends.get(backend)
        if backend_counts is not None:
            backend_counts.upstream += 1
        self._last_upstream[backend] = datetime.now(timezone.utc).isoformat()

    def last_upstream_at(self, backend: str) -> str | None:
        """Return the latest upstream dispatch time for this backend in UTC."""
        return self._last_upstream.get(backend)

    def tool_uses(self, name: str) -> int:
        """Count completed tool requests other than policy denials in this process."""
        counts = self.tools.get(name)
        return counts.received - counts.denied if counts is not None else 0

    def tool_latency(self, tool: str) -> tuple[float | None, int]:
        """Expose observed successful latency when agent discovery is enabled."""
        return self.history.mean(tool) if self.history is not None else (None, 0)

    def prune_tools(self, live_names, denied, *, publications=()) -> None:
        """Keep live counters and the 256 most recently introduced retired identities."""
        retired = 0
        remove = []
        for name in reversed(self.tools):
            if name == "(unknown)" or name in live_names or name in denied or name in publications:
                continue
            retired += 1
            if retired > _RETIRED_TOOL_LIMIT:
                remove.append(name)
        for name in remove:
            del self.tools[name]

    def completed(self, tool, outcome, elapsed_ms, merged, input_bytes, output_bytes, *,
                  session_tag=None, task_tag=None, trace_id=None, error_code=None, track_tool=True, module_step=False):
        """Update rollups and append one sanitized event per inbound request."""
        duration = round(elapsed_ms, 2)
        if self.history is not None and outcome == "ok" and not merged:
            self.history.add(tool, duration)
        self.total.add_call(outcome, elapsed_ms, merged, input_bytes, output_bytes)
        backend_counts = self._backends.get(tool.partition(".")[0])
        if backend_counts is not None:
            backend_counts.add_call(outcome, elapsed_ms, merged, input_bytes, output_bytes)
        counts = self.tools.get(tool)
        if counts is None and track_tool:
            counts = self.tools[tool] = Counts()
        if counts is not None:
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
        if error_code is not None:
            event["error_code"] = error_code
        if session_tag is not None:
            event["session_tag"] = session_tag
        if task_tag is not None:
            event["task_tag"] = task_tag
        if trace_id is not None:
            event["trace_id"] = trace_id
        if module_step:
            event["module_step"] = True
        try:
            self.journal.write(event)
        except OSError:
            self.journal_errors += 1
            if self.journal_errors == 1:
                _LOG.warning("Unable to append the gateway call journal")
        if self.technical is not None and error_code is not None:
            self.technical.call_error(tool, outcome, error_code, duration, trace_id, session_tag, joined=merged)
        else:
            _LOG.info("%s %s %.1fms%s", tool, outcome, elapsed_ms, " joined" if merged else "")

    def snapshot(self, tool_names: list[str] | None = None) -> dict:
        """Return all rollups or just those on the selected dashboard page."""
        names = self.tools if tool_names is None else tool_names
        tools = {}
        for name in names:
            counts = self.tools.get(name)
            if counts is not None:
                tools[name] = counts.snapshot()
        return {"total": self.total.snapshot(), "tools": tools, "journal_errors": self.journal_errors}
