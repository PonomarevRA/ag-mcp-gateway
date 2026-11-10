"""Store explicitly submitted MCP approaches in a bounded profile-local SQLite file."""

import json
import os
import re
import sqlite3
import stat
import time
import unicodedata
from collections.abc import Callable
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class SkillSettings(BaseModel):
    """Limit the main database size and disposable cache retention per profile."""

    model_config = ConfigDict(extra="forbid")
    max_bytes: int = Field(default=8 * 1024 * 1024, ge=128 * 1024, le=256 * 1024 * 1024, strict=True)
    cache_entries: int = Field(default=512, ge=1, le=2048, strict=True)
    cache_ttl_seconds: int = Field(default=7 * 86400, ge=60, le=90 * 86400, strict=True)


class SkillStoreError(ValueError):
    """Report a safe error without exposing database paths or submitted content."""


_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS approaches (
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('draft', 'approved', 'cache')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    steps TEXT NOT NULL,
    instructions TEXT,
    created INTEGER NOT NULL,
    expires INTEGER,
    helpful INTEGER NOT NULL DEFAULT 0,
    votes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS approach_retention ON approaches(kind, created, id);
CREATE VIRTUAL TABLE IF NOT EXISTS approved_search USING fts5(title, summary, content='');
PRAGMA user_version = 1;
COMMIT;
"""


def query_words(value: str) -> set[str]:
    """Normalize text to Unicode search terms without exposing FTS query syntax."""
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFC", value.lower()))
    return {
        "".join(char for char in unicodedata.normalize("NFD", word) if unicodedata.category(char) != "Mn")
        if word.isascii() or all("LATIN" in unicodedata.name(char, "") for char in word)
        else word
        for word in words
    }


def public_approach(row: sqlite3.Row, *, instructions: bool = False) -> dict:
    """Expose one bounded projection while preserving ordered tool names and repeats."""
    result = {
        "id": row["id"], "kind": row["kind"], "title": row["title"], "summary": row["summary"],
        "tools": json.loads(row["steps"]), "helpful": row["helpful"], "votes": row["votes"],
    }
    if row["expires"] is not None:
        result["expires_at"] = row["expires"]
    if instructions:
        result["instructions"] = row["instructions"]
    return result


class SkillStore:
    """Serialize writes across CLI and daemon processes using SQLite transactions."""

    def __init__(self, path: Path, settings: SkillSettings):
        """Create the versioned store in a private file, or open the existing schema."""
        self.path = path
        self.settings = settings
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise SkillStoreError("Skill database requires a private regular file")
        with closing(self._connect()) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                connection.executescript(_SCHEMA)
            elif version != 1:
                raise SkillStoreError("Unsupported skill database version")

    def _connect(self) -> sqlite3.Connection:
        """Enforce the main-file page cap on every connection, including CLI writers."""
        connection = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            pages = self.settings.max_bytes // page_size
            actual = connection.execute(f"PRAGMA max_page_count = {pages}").fetchone()[0]
            if actual > pages:
                raise SkillStoreError("Skill database already exceeds its configured cap")
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise SkillStoreError("Skill database requires DELETE journal mode")
            return connection
        except BaseException:
            connection.close()
            raise

    @contextmanager
    def _write(self):
        """Commit a mutation atomically and roll it back if SQLite rejects it."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _prune(self, connection: sqlite3.Connection, now: int, keep: int):
        """Delete expired rows and keep only the newest disposable cache entries."""
        connection.execute("DELETE FROM approaches WHERE kind='cache' AND expires<=?", (now,))
        connection.execute("""
            DELETE FROM approaches WHERE id IN (
                SELECT id FROM approaches WHERE kind='cache'
                ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET ?
            )
        """, (keep,))

    def cleanup(self) -> None:
        """Expire cached approaches even if no agents are using the gateway."""
        with self._write() as connection:
            self._prune(connection, int(time.time()), self.settings.cache_entries)

    def _retry_with_eviction(self, action: Callable[[sqlite3.Connection], dict]) -> dict:
        """On SQLITE_FULL, free disposable rows in a separate transaction before retrying."""
        # Each recovery halves a cache of at most 2048 rows. Bound retries under concurrent writes.
        for _ in range(13):
            try:
                with self._write() as connection:
                    return action(connection)
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_FULL:
                    raise
            with self._write() as connection:
                before = connection.total_changes
                count = connection.execute("SELECT count(*) FROM approaches WHERE kind='cache'").fetchone()[0]
                if not count:
                    raise SkillStoreError("Skill database is full")
                self._prune(connection, int(time.time()), count // 2)
                if connection.total_changes == before:
                    raise SkillStoreError("Skill database is full")
        raise SkillStoreError("Skill database is full")

    def save(self, title: str, summary: str, tools: list[str], instructions: str | None = None) -> dict:
        """Save an operator-reviewable draft or a disposable cache record."""
        kind = "draft" if instructions is not None else "cache"
        identity = uuid4().hex
        now = int(time.time())
        expires = now + self.settings.cache_ttl_seconds if kind == "cache" else None
        steps = json.dumps(tools, separators=(",", ":"))

        def insert(connection: sqlite3.Connection) -> dict:
            """Reserve a cache slot and insert the validated record in one transaction."""
            keep = self.settings.cache_entries - (kind == "cache")
            self._prune(connection, now, keep)
            connection.execute("""
                INSERT INTO approaches(id,kind,title,summary,steps,instructions,created,expires)
                VALUES (?,?,?,?,?,?,?,?)
            """, (identity, kind, title, summary, steps, instructions, now, expires))
            return {"id": identity, "kind": kind}

        return self._retry_with_eviction(insert)

    def _approved_matches(self, connection: sqlite3.Connection, match: str,
                          limit: int, available: set[str] | None) -> list[sqlite3.Row]:
        """Fill the result with usable matches before returning stale ones for reference."""
        rows = connection.execute("""
            SELECT a.id,a.kind,a.title,a.summary,a.steps,a.expires,a.helpful,a.votes
            FROM approved_search JOIN approaches AS a ON a.rowid=approved_search.rowid
            WHERE approved_search MATCH ?
            ORDER BY bm25(approved_search, 3.0, 1.0)*(1.0+0.5*(a.helpful+1.0)/(a.votes+2)), a.id
        """, (match,))
        if available is None:
            return rows.fetchmany(limit)
        usable, stale = [], []
        for row in rows:
            if set(json.loads(row["steps"])) <= available:
                usable.append(row)
                if len(usable) == limit:
                    return usable
            elif len(stale) < limit:
                stale.append(row)
        return usable + stale[:limit - len(usable)]

    def find(self, query: str, limit: int, available: set[str] | None = None) -> dict:
        """Rank approved skills with FTS5 and cached approaches by token overlap."""
        terms = query_words(query)
        if not terms:
            return {"skills": [], "cache": []}
        match = " OR ".join(f'"{term}"' for term in sorted(terms))
        with closing(self._connect()) as connection:
            approved = self._approved_matches(connection, match, limit, available)
            cached = connection.execute("""
                SELECT * FROM approaches WHERE kind='cache' AND expires>?
                ORDER BY created DESC, rowid DESC LIMIT ?
            """, (int(time.time()), self.settings.cache_entries))
            matches = []
            for row in cached:
                summary_hits = len(terms & query_words(row["summary"]))
                title_hits = len(terms & query_words(row["title"]))
                relevance = title_hits * 3 + summary_hits
                if relevance:
                    matches.append((relevance, row))
            matches.sort(key=lambda hit: hit[0], reverse=True)
        return {"skills": [public_approach(row) for row in approved],
                "cache": [public_approach(row) for _, row in matches[:limit]]}

    def get(self, identity: str, kind: str) -> dict:
        """Return full content only for an exact identity and expected lifecycle state."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM approaches WHERE id=? AND kind=?", (identity, kind)).fetchone()
        if row is None:
            raise SkillStoreError(f"{kind.capitalize()} skill not found")
        return public_approach(row, instructions=True)

    def drafts(self) -> list[dict]:
        """List up to 100 recent drafts for operator review."""
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT * FROM approaches WHERE kind='draft' ORDER BY created DESC, rowid DESC LIMIT 100
            """).fetchall()
        return [public_approach(row) for row in rows]

    def approve(self, identity: str) -> dict:
        """Atomically publish the immutable draft and add it to the approved FTS index."""
        def publish(connection: sqlite3.Connection) -> dict:
            """Update the draft and its index together; repeating approval is harmless."""
            row = connection.execute(
                "SELECT rowid, kind, title, summary FROM approaches WHERE id=? AND kind!='cache'", (identity,),
            ).fetchone()
            if row is None:
                raise SkillStoreError("Draft skill not found")
            if row["kind"] == "draft":
                connection.execute("INSERT INTO approved_search(rowid,title,summary) VALUES (?,?,?)",
                                   (row["rowid"], row["title"], row["summary"]))
                connection.execute("UPDATE approaches SET kind='approved' WHERE id=?", (identity,))
            return {"id": identity, "kind": "approved"}

        return self._retry_with_eviction(publish)

    def discard(self, identity: str) -> dict:
        """Allow an operator to remove a pending draft by its exact ID."""
        with self._write() as connection:
            deleted = connection.execute("DELETE FROM approaches WHERE id=? AND kind='draft'", (identity,))
            if not deleted.rowcount:
                raise SkillStoreError("Draft skill not found")
        return {"id": identity, "discarded": True}

    def rate(self, identity: str, useful: bool) -> dict:
        """Record one usefulness vote against an approved skill."""
        with self._write() as connection:
            row = connection.execute("""
                UPDATE approaches SET helpful=helpful+?, votes=votes+1
                WHERE id=? AND kind='approved' RETURNING helpful,votes
            """, (int(useful), identity)).fetchone()
            if row is None:
                raise SkillStoreError("Approved skill not found")
            return {"id": identity, "helpful": row["helpful"], "votes": row["votes"]}
