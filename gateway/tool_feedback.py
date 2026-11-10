"""Persist bounded tool-usefulness feedback separately from the skill registry."""

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from threading import Lock

MAX_TOOLS = 4096
MAX_RECEIPTS = 4096
MAX_PAGES_BYTES = 4 * 1024 * 1024


class FeedbackError(ValueError):
    """Report a safe feedback error without exposing local storage details."""


class ToolFeedback:
    """Keep aggregate votes and a bounded retry receipt window in one SQLite file."""

    def __init__(self, path: Path):
        """Create a private profile-local database and initialize its schema."""
        self.path = path
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise FeedbackError("Tool feedback requires a private regular file")
        with closing(self.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ratings (
                    tool TEXT PRIMARY KEY, helpful INTEGER NOT NULL, votes INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                    tool TEXT NOT NULL, useful INTEGER NOT NULL
                );
            """)
        self._cache_lock = Lock()
        self._ratings = self.snapshot()

    def current(self, tool: str) -> tuple[int, int]:
        """Read the latest committed aggregate without opening the database."""
        with self._cache_lock:
            return self._ratings.get(tool, (0, 0))

    def connect(self):
        """Open an independent connection with the database growth limit applied."""
        db = sqlite3.connect(self.path, timeout=2)
        try:
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            limit = MAX_PAGES_BYTES // page_size
            actual = db.execute(f"PRAGMA max_page_count={limit}").fetchone()[0]
            if actual > limit:
                raise FeedbackError("Tool feedback exceeds its storage limit")
            return db
        except BaseException:
            db.close()
            raise

    def snapshot(self) -> dict[str, tuple[int, int]]:
        """Load existing per-tool aggregates at daemon startup."""
        with closing(self.connect()) as db:
            return {tool: (helpful, votes) for tool, helpful, votes in db.execute(
                "SELECT tool, helpful, votes FROM ratings"
            )}

    def rate(self, tool: str, useful: bool, receipt: str) -> tuple[int, int]:
        """Commit one vote exactly once within the receipt window."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT tool, useful FROM receipts WHERE id=?", (receipt,)).fetchone()
            if previous is not None:
                if previous != (tool, int(useful)):
                    raise FeedbackError("Feedback ID already refers to a different vote")
            else:
                known = db.execute("SELECT 1 FROM ratings WHERE tool=?", (tool,)).fetchone()
                if known is None and db.execute("SELECT count(*) FROM ratings").fetchone()[0] >= MAX_TOOLS:
                    raise FeedbackError("Tool feedback has reached its tool limit")
                db.execute("""
                    INSERT INTO ratings(tool,helpful,votes) VALUES (?,?,1)
                    ON CONFLICT(tool) DO UPDATE SET helpful=helpful+excluded.helpful, votes=votes+1
                """, (tool, int(useful)))
                db.execute("INSERT INTO receipts(id,tool,useful) VALUES (?,?,?)", (receipt, tool, int(useful)))
                db.execute("DELETE FROM receipts WHERE sequence <= (SELECT max(sequence) FROM receipts)-?",
                           (MAX_RECEIPTS,))
            saved = db.execute("SELECT helpful, votes FROM ratings WHERE tool=?", (tool,)).fetchone()
        # Publish in the writer thread even if the requesting coroutine was cancelled.
        with self._cache_lock:
            current = self._ratings.get(tool, (0, 0))
            if saved[1] >= current[1]:
                self._ratings[tool] = saved
                return saved
            return current
