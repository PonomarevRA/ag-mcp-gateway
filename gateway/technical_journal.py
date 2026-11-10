"""Persist bounded technical events with an asynchronous, single-writer journal."""

import json
import sqlite3
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Condition, Lock, Thread
from uuid import uuid4

APPLICATION = "ag-mcp-gateway"
STATUS_SCHEMA = "technical-logs-http/v1"
EVENT_SCHEMA = "technical-log/v1"
MAX_PAGE_EVENTS = 100
MAX_PAGE_BYTES = 262_144
_MAX_EVENT_BYTES = 16_384
_MAX_QUEUE_EVENTS = 256
_MAX_QUEUE_BYTES = 1_048_576
_PAGE_BYTES = 1024


class TechnicalJournal:
    """Keep a contiguous committed suffix and a restart-stable cursor in SQLite."""

    def __init__(self, path: Path, max_bytes: int):
        """Open the local database and start a bounded writer, if storage is available."""
        self.path = path
        self.max_bytes = max_bytes
        self._db = None
        self._db_lock = Lock()
        self._ready = Condition()
        self._queue = deque()
        self._queue_bytes = 0
        self._dropped = 0
        self._error = None
        self._closing = False
        self._worker = None
        self._batch_size = max(1, min(32, max_bytes // (2 * _MAX_EVENT_BYTES)))
        try:
            self._db = self._open()
        except (OSError, sqlite3.Error):
            self._error = "journal_unavailable"
        else:
            self._worker = Thread(target=self._write_loop, name="gateway-technical-journal", daemon=True)
            self._worker.start()

    def _open(self) -> sqlite3.Connection:
        """Initialize the tables and enforce the main file quota before accepting events."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=1, check_same_thread=False)
        try:
            db.execute(f"PRAGMA page_size = {_PAGE_BYTES}")
            db.execute("PRAGMA auto_vacuum = FULL")
            # DELETE mode keeps a temporary rollback journal instead of an accumulating WAL.
            db.execute("PRAGMA journal_mode = DELETE")
            db.execute("PRAGMA cache_size = -512")
            db.execute("CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, payload BLOB NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS journal (epoch TEXT PRIMARY KEY, latest INTEGER NOT NULL) WITHOUT ROWID")
            with db:
                db.execute("INSERT INTO journal SELECT ?, 0 WHERE NOT EXISTS (SELECT 1 FROM journal)", (uuid4().hex,))
            self._apply_quota(db)
        except BaseException:
            db.close()
            raise
        return db

    def _apply_quota(self, db: sqlite3.Connection) -> None:
        """Evict the oldest prefix when the configured main file cap was reduced."""
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        page_limit = self.max_bytes // page_size
        while db.execute("PRAGMA page_count").fetchone()[0] > page_limit:
            oldest, newest = db.execute("SELECT min(sequence), max(sequence) FROM events").fetchone()
            if oldest is None:
                raise OSError("Technical journal quota is too small")
            with db:
                db.execute("DELETE FROM events WHERE sequence <= ?", ((oldest + newest) // 2,))
        applied = db.execute(f"PRAGMA max_page_count = {page_limit}").fetchone()[0]
        if applied != page_limit:
            raise OSError("Technical journal quota could not be applied")

    def emit(self, event: dict) -> None:
        """Queue one already sanitized event without waiting for SQLite or disk."""
        try:
            payload = b""
            if isinstance(event, dict):
                payload = json.dumps(event, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        except (ValueError, TypeError, RecursionError):
            payload = b""
        # Reserve room for the envelope fields assigned at commit time.
        max_payload = min(_MAX_EVENT_BYTES - 512, self.max_bytes // 4)
        with self._ready:
            if (not payload or len(payload) > max_payload or self._closing or self._db is None
                    or len(self._queue) >= _MAX_QUEUE_EVENTS or self._queue_bytes + len(payload) > _MAX_QUEUE_BYTES):
                self._dropped += 1
                return
            self._queue.append(payload)
            self._queue_bytes += len(payload)
            self._ready.notify()

    def _write_loop(self) -> None:
        """Drain queued events in FIFO order, counting failures independently of the gateway."""
        while True:
            with self._ready:
                self._ready.wait_for(lambda: self._closing or self._queue)
                if not self._queue:
                    return
                batch = []
                for _ in range(min(self._batch_size, len(self._queue))):
                    payload = self._queue.popleft()
                    self._queue_bytes -= len(payload)
                    batch.append(payload)
            with self._db_lock:
                lost = self._append(batch)
                with self._ready:
                    self._error = "journal_write_failed" if lost else None
                    if lost:
                        # One storage failure should not hold shutdown up for one timeout per queued batch.
                        self._dropped += lost + len(self._queue)
                        self._queue.clear()
                        self._queue_bytes = 0

    def _append(self, batch: list[bytes]) -> int:
        """Commit a batch, retrying quota pressure after evicting the oldest rows."""
        db = self._db
        while True:
            try:
                self._commit(db, batch)
                return 0
            except sqlite3.Error as error:
                if getattr(error, "sqlite_errorcode", None) != sqlite3.SQLITE_FULL:
                    return len(batch)
                if len(batch) > 1:
                    middle = len(batch) // 2
                    return self._append(batch[:middle]) + self._append(batch[middle:])
                try:
                    with db:
                        removed = db.execute(
                            "DELETE FROM events WHERE sequence IN (SELECT sequence FROM events ORDER BY sequence LIMIT 16)"
                        ).rowcount
                except sqlite3.Error:
                    return 1
                if not removed:
                    return 1

    def _commit(self, db: sqlite3.Connection, batch: list[bytes]) -> None:
        """Assign contiguous sequence numbers and commit events and head together."""
        epoch, latest = db.execute("SELECT epoch, latest FROM journal").fetchone()
        recorded = datetime.now(timezone.utc).isoformat()
        rows = []
        for sequence, payload in enumerate(batch, latest + 1):
            event = json.loads(payload)
            event.update(schemaVersion=EVENT_SCHEMA, application=APPLICATION, eventId=uuid4().hex,
                         recordedAt=recorded, journalEpoch=epoch, sequence=str(sequence))
            encoded = json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode()
            rows.append((sequence, encoded))
        with db:
            db.executemany("INSERT INTO events VALUES (?, ?)", rows)
            db.execute("UPDATE journal SET latest = ?", (rows[-1][0],))

    def _status(self, db: sqlite3.Connection) -> dict:
        """Read the committed window while the caller holds the database lock."""
        epoch, latest = db.execute("SELECT epoch, latest FROM journal").fetchone()
        oldest = db.execute("SELECT min(sequence) FROM events").fetchone()[0]
        with self._ready:
            health = {
                "status": "degraded" if self._error else "ok", "errorCode": self._error,
                "queueItems": len(self._queue), "queueBytes": self._queue_bytes, "droppedTotal": self._dropped,
            }
        return {
            "schemaVersion": STATUS_SCHEMA, "application": APPLICATION, "eventSchemaVersion": EVENT_SCHEMA,
            "journalEpoch": epoch, "oldestSequence": str(latest + 1 if oldest is None else oldest),
            "latestSequence": str(latest), "cursor": {"journalEpoch": epoch, "sequence": str(latest)},
            "maxSizeBytes": self.max_bytes, "usedBytes": self.path.stat().st_size,
            "limits": {"maxPageEvents": MAX_PAGE_EVENTS, "maxPageBytes": MAX_PAGE_BYTES}, "health": health,
        }

    def status(self) -> dict:
        """Expose committed bounds, quota and current producer health."""
        with self._db_lock:
            if self._db is None:
                raise OSError("Technical journal unavailable")
            return self._status(self._db)

    def page(self, after: int | None = None, before: int | None = None, limit: int = 101) -> tuple[dict, list[dict]]:
        """Return a coherent window and a bounded, ascending page of committed events."""
        if not 1 <= limit <= MAX_PAGE_EVENTS + 1 or after is not None and before is not None:
            raise ValueError("Invalid technical journal page")
        with self._db_lock:
            db = self._db
            if db is None:
                raise OSError("Technical journal unavailable")
            status = self._status(db)
            if after is not None:
                rows = db.execute("SELECT payload FROM events WHERE sequence > ? ORDER BY sequence LIMIT ?", (after, limit))
            elif before is not None:
                rows = db.execute("SELECT payload FROM events WHERE sequence < ? ORDER BY sequence DESC LIMIT ?", (before, limit))
            else:
                rows = db.execute("SELECT payload FROM events ORDER BY sequence DESC LIMIT ?", (limit,))
            events = [json.loads(payload) for (payload,) in rows]
            if after is None:
                events.reverse()
            return status, events

    def close(self) -> None:
        """Drain queued events and release SQLite before the profile lock is released."""
        with self._ready:
            self._closing = True
            self._ready.notify()
        if self._worker is not None:
            self._worker.join()
        with self._db_lock:
            if self._db is not None:
                self._db.close()
                self._db = None
