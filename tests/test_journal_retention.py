"""Verify scheduled technical event expiration and the hard file-size ceiling."""

from datetime import datetime, timedelta, timezone
from time import monotonic, sleep

import pytest

from gateway import technical_journal
from gateway.telemetry import LOG_FILE_MAX_BYTES
from gateway.telemetry_api import EventQuery
from gateway.technical_journal import TechnicalJournal


@pytest.fixture
def journal_clock(monkeypatch):
    """Control producer timestamps while preserving a real periodic worker wakeup."""
    class Clock(datetime):
        now_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            """Return the fixed wall time in the requested timezone."""
            return cls.now_at.astimezone(tz)

    monkeypatch.setattr(technical_journal, "datetime", Clock)
    monkeypatch.setattr(technical_journal, "_RETENTION_INTERVAL_SECONDS", 0.01)
    return Clock


def wait_for_status(journal, *, oldest=None, latest=None):
    """Wait for durable bounds without signalling the producer or retention worker."""
    deadline = monotonic() + 5
    while monotonic() < deadline:
        status = journal.status()
        actual_oldest = int(status["oldestSequence"])
        actual_latest = int(status["latestSequence"])
        if (oldest is None or oldest == actual_oldest) and (latest is None or latest == actual_latest):
            return status
        sleep(0.01)
    pytest.fail(f"Technical journal did not reach window [{oldest}, {latest}]")


def test_idle_cleanup_preserves_recent_events_and_expires_old_cursors(tmp_path, journal_clock):
    """Periodic retention works on an idle profile and only removes the expired prefix."""
    journal = TechnicalJournal(tmp_path / "technical.sqlite3", 65_536, retention_days=1)
    try:
        for _ in range(2):
            journal.emit({"message": "old"})
        initial = wait_for_status(journal, oldest=1, latest=2)
        journal_clock.now_at += timedelta(hours=12)
        journal.emit({"message": "recent"})
        wait_for_status(journal, oldest=1, latest=3)
        journal_clock.now_at += timedelta(hours=13)
        status = wait_for_status(journal, oldest=3, latest=3)
        assert status["journalEpoch"] == initial["journalEpoch"]
        assert status["health"]["droppedTotal"] == 0
        assert EventQuery(100, initial["journalEpoch"], 1, None).expired(status)
        assert not EventQuery(100, initial["journalEpoch"], 2, None).expired(status)
        _, events = journal.page()
        assert [event["message"] for event in events] == ["recent"]
    finally:
        journal.close()


def test_startup_cleanup_reclaims_disk_and_preserves_the_head(tmp_path, journal_clock, monkeypatch):
    """An expired database shrinks at restart without changing epoch or sequence."""
    path = tmp_path / "technical.sqlite3"
    journal = TechnicalJournal(path, 1_000_000_000)
    try:
        for _ in range(200):
            journal.emit({"message": "x" * 1024})
        initial = wait_for_status(journal, oldest=1, latest=200)
        assert initial["maxSizeBytes"] == LOG_FILE_MAX_BYTES
    finally:
        journal.close()

    journal_clock.now_at += timedelta(days=8)
    monkeypatch.setattr(technical_journal, "_RETENTION_INTERVAL_SECONDS", 60)
    reopened = TechnicalJournal(path, LOG_FILE_MAX_BYTES)
    try:
        status = reopened.status()
        assert status["oldestSequence"] == "201"
        assert status["latestSequence"] == "200"
        assert status["cursor"] == initial["cursor"]
        assert status["usedBytes"] < initial["usedBytes"]
        assert not reopened.page()[1]
        reopened.emit({"message": "fresh"})
        assert wait_for_status(reopened, oldest=201, latest=201)["journalEpoch"] == status["journalEpoch"]
    finally:
        reopened.close()


def test_existing_file_above_ceiling_is_trimmed_at_open(tmp_path, monkeypatch):
    """Simulate an older, oversized database while retaining its newest records."""
    path = tmp_path / "technical.sqlite3"
    old = TechnicalJournal(path, 128 * 1024)
    try:
        for _ in range(80):
            old.emit({"message": "x" * 1024})
        initial = wait_for_status(old, oldest=1, latest=80)
        assert initial["usedBytes"] > 65_536
    finally:
        old.close()

    monkeypatch.setattr(technical_journal, "LOG_FILE_MAX_BYTES", 65_536)
    reopened = TechnicalJournal(path, 1_000_000_000)
    try:
        status, events = reopened.page()
        assert status["maxSizeBytes"] == 65_536
        assert status["usedBytes"] <= 65_536
        assert status["cursor"] == initial["cursor"]
        assert 1 < int(status["oldestSequence"]) < 80
        assert events[-1]["sequence"] == "80"
    finally:
        reopened.close()
