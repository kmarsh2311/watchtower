"""
Regression tests for WT-001 and WT-005.

WT-001: A database error in CompletedDownloadWorker._scan() can terminate the
incoming-download worker while the main monitor keeps running.
Safeguards:
1. Candidate must remain recoverable (not lost from candidates).
2. Temporary SQLite failures must not exhaust normal scan attempts.
3. Original settling state must be preserved when the file is unchanged,
   so retries happen after the 10s backoff without waiting 5 minutes again.
4. Persistent database errors must not cause tight retry loops.

WT-005: A file disappearing between is_file() and stat() in _restore_candidates()
can interrupt monitor startup.
"""
import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

WATCHTOWER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(WATCHTOWER_DIR))

from librarymanager_monitor import CompletedDownloadWorker
from librarymanager_core import connect, SCHEMA


def _make_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def _make_worker(db_path, incoming_dir, settle=60, fallback=60, max_attempts=3):
    stash = MagicMock()
    stash.call_GQL.return_value = {"findScenes": {"scenes": []}}
    worker = CompletedDownloadWorker(
        database_path=db_path,
        stash=stash,
        incoming_folder=str(incoming_dir),
        enabled=True,
        settle_seconds=settle,
        notifications=False,
        fallback_seconds=fallback,
        max_attempts=max_attempts,
    )
    return worker


def test_wt001_initial_save_state_db_error_does_not_kill_worker_and_retains_candidate(tmp_path):
    """
    WT-001: When _save_state raises sqlite3.OperationalError at the beginning of _scan,
    the worker must not crash, the candidate must remain recoverable, and attempts must not be exhausted.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    video = incoming / "test_video.mp4"
    video.write_bytes(b"dummy content")

    worker = _make_worker(db_path, incoming, settle=60)
    assert worker.submit(str(video)) is True

    # Age the candidate so it is ready to be scanned
    with worker.lock:
        cand = worker.candidates[str(video.resolve())]
        cand["stable_since"] = time.time() - 100

    real_save_state = worker._save_state

    def failing_save_state(path, status, **kwargs):
        if status == "scanning":
            raise sqlite3.OperationalError("database is locked")
        return real_save_state(path, status, **kwargs)

    worker._save_state = failing_save_state

    # This should NOT raise an unhandled sqlite3.OperationalError
    worker.evaluate_once()

    # Safeguard 1: Candidate must NOT be lost!
    with worker.lock:
        assert str(video.resolve()) in worker.candidates, "Candidate was lost from self.candidates on DB error!"
        saved_cand = worker.candidates[str(video.resolve())]
        # Safeguard 2: Attempts must NOT be exhausted by a transient DB error
        assert saved_cand.get("attempts", 0) == 0, "DB failure must not exhaust scan attempt allowance!"


def test_wt001_worker_thread_survives_db_error(tmp_path):
    """
    WT-001: CompletedDownloadWorker thread must remain alive when evaluate_once hits a DB error.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    video = incoming / "live_video.mp4"
    video.write_bytes(b"sample data")

    worker = _make_worker(db_path, incoming, settle=60)
    assert worker.submit(str(video)) is True

    with worker.lock:
        cand = worker.candidates[str(video.resolve())]
        cand["stable_since"] = time.time() - 100

    def failing_save_state(path, status, **kwargs):
        if status == "scanning":
            raise sqlite3.OperationalError("database is locked")
        return None

    worker._save_state = failing_save_state

    worker.start()
    try:
        time.sleep(0.3)
        assert worker.is_alive(), "CompletedDownloadWorker thread died due to unhandled database error!"
    finally:
        worker.stop()
        worker.join(timeout=2.0)


def test_wt001_db_error_during_scan_failure_handler_preserves_candidate(tmp_path):
    """
    WT-001: When a scan fails and the secondary _save_state in the except block also raises a DB error,
    the worker must survive and the candidate must be preserved.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    video = incoming / "scan_fail.mp4"
    video.write_bytes(b"sample data")

    worker = _make_worker(db_path, incoming, settle=60, max_attempts=3)
    assert worker.submit(str(video)) is True

    with worker.lock:
        cand = worker.candidates[str(video.resolve())]
        cand["stable_since"] = time.time() - 100

    # Stash metadata scan fails
    worker.stash.metadata_scan.side_effect = RuntimeError("Stash scan timeout")

    real_save_state = worker._save_state

    def save_state_fail_on_retry(path, status, **kwargs):
        if status == "waiting":
            raise sqlite3.OperationalError("database is locked")
        return real_save_state(path, status, **kwargs)

    worker._save_state = save_state_fail_on_retry

    worker.evaluate_once()

    with worker.lock:
        assert str(video.resolve()) in worker.candidates, "Candidate was lost when retry state save failed!"
        assert worker.candidates[str(video.resolve())]["attempts"] == 1


def test_wt001_download_successfully_resumes_after_transient_sqlite_failure_preserving_allowance(tmp_path):
    """
    WT-001: A download must resume after a temporary SQLite failure and succeed,
    preserving the original settling state (no 5-minute re-settle delay) and normal scan attempts.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    video = incoming / "resume_video.mp4"
    video.write_bytes(b"resume video content")

    # 300s settle time (5 minutes)
    worker = _make_worker(db_path, incoming, settle=300, max_attempts=3)
    assert worker.submit(str(video)) is True

    resolved_path = str(video.resolve())
    initial_stable = time.time() - 350  # Settled 350 seconds ago (ready to scan)

    with worker.lock:
        worker.candidates[resolved_path]["stable_since"] = initial_stable

    # Cycle 1: Database is temporarily locked on initial scan attempt
    db_locked = True
    real_save_state = worker._save_state

    def flaky_save_state(path, status, **kwargs):
        if db_locked and status == "scanning":
            raise sqlite3.OperationalError("database is locked")
        return real_save_state(path, status, **kwargs)

    worker._save_state = flaky_save_state

    mono_start = time.monotonic()
    worker.evaluate_once(now=time.time(), mono_now=mono_start)

    # Verify state after temporary failure:
    with worker.lock:
        cand = worker.candidates[resolved_path]
        # Attempt allowance NOT exhausted by the DB lock
        assert cand["attempts"] == 0
        # Original settling state PRESERVED (not reset to current time!)
        assert cand["stable_since"] == initial_stable
        # Backoff check_after is set ~10s ahead
        assert cand["check_after"] >= mono_start + 9.9

    # Cycle 2: Before 10s backoff expires (e.g. at mono_now = 105s), scan is NOT retried
    worker.evaluate_once(now=time.time(), mono_now=mono_start + 5.0)
    with worker.lock:
        assert resolved_path in worker.candidates

    # Cycle 3: After 10s backoff expires (e.g. at mono_now = 111s), DB lock is cleared
    db_locked = False
    worker.stash.metadata_scan.return_value = "job-123"
    worker.stash.wait_for_job.return_value = True
    worker.stash.call_GQL.return_value = {
        "findScenes": {
            "scenes": [{
                "id": "101",
                "title": "Resumed Scene",
                "files": [{"path": resolved_path}]
            }]
        }
    }

    # evaluate_once should now immediately scan and import (without waiting 300s!)
    worker.evaluate_once(now=time.time(), mono_now=mono_start + 11.0)

    # Candidate should now be imported!
    with worker.lock:
        assert resolved_path not in worker.candidates, "Video should have been successfully imported!"

    con = sqlite3.connect(str(db_path))
    row = con.execute("SELECT status, attempts FROM incoming_files WHERE path=?", (resolved_path,)).fetchone()
    con.close()

    assert row is not None
    assert row[0] == "imported"
    # Normal attempt allowance was preserved: only the 1 genuine scan was counted!
    assert row[1] == 1


def test_wt001_persistent_database_errors_cannot_cause_tight_retry_loop(tmp_path):
    """
    WT-001: Persistent database errors must respect the 10-second backoff and not cause a tight retry loop.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    video = incoming / "loop_test.mp4"
    video.write_bytes(b"loop test content")

    worker = _make_worker(db_path, incoming, settle=60)
    assert worker.submit(str(video)) is True

    resolved_path = str(video.resolve())
    with worker.lock:
        worker.candidates[resolved_path]["stable_since"] = time.time() - 100

    scan_attempts = []
    real_scan = worker._scan

    def instrumented_scan(path, candidate):
        scan_attempts.append(time.monotonic())
        # Always fail with SQLite error
        raise sqlite3.OperationalError("database is locked permanently")

    worker._scan = instrumented_scan

    mono_now = time.monotonic()

    # First evaluation: triggers scan, which encounters DB error and backs off 10s
    worker.evaluate_once(now=time.time(), mono_now=mono_now)
    assert len(scan_attempts) == 1

    # Rapid successive evaluation cycles during the 10-second backoff window (1000.1 to 1009.9)
    for delta in [0.1, 0.5, 1.0, 2.5, 5.0, 7.5, 9.0, 9.9]:
        worker.evaluate_once(now=time.time(), mono_now=mono_now + delta)
        # MUST NOT execute scan while mono_now < check_after!
        assert len(scan_attempts) == 1, f"Tight retry loop detected at delta {delta}!"

    # Once 10 seconds have elapsed (mono_now >= 1010.0), it may retry once
    worker.evaluate_once(now=time.time(), mono_now=mono_now + 10.1)
    assert len(scan_attempts) == 2

    # And again backs off for another 10 seconds:
    for delta in [10.2, 11.0, 15.0, 19.9]:
        worker.evaluate_once(now=time.time(), mono_now=mono_now + delta)
        assert len(scan_attempts) == 2, f"Premature retry detected at delta {delta}!"

    # At 20.2s, exactly 3rd attempt:
    worker.evaluate_once(now=time.time(), mono_now=mono_now + 20.2)
    assert len(scan_attempts) == 3


def test_wt005_file_disappearing_between_is_file_and_stat_does_not_crash_startup(tmp_path):
    """
    WT-005: If a tracked file disappears between is_file() and stat() during _restore_candidates,
    monitor startup must continue without raising FileNotFoundError and restore remaining files.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    file1 = incoming / "file1.mp4"
    file1.write_bytes(b"file1 content")
    stat1 = file1.stat()

    file2 = incoming / "file2.mp4"
    file2.write_bytes(b"file2 content")
    stat2 = file2.stat()

    con = sqlite3.connect(str(db_path))
    con.execute(
        """INSERT INTO incoming_files(path, first_seen_at, last_checked_at, size, modified_ns, stable_since, settle_seconds, status, attempts, detail)
           VALUES (?, '2026-09-18T00:00:00Z', '2026-09-18T00:00:00Z', ?, ?, 100.0, 60, 'waiting', 0, 'waiting')""",
        (str(file1.resolve()), stat1.st_size, stat1.st_mtime_ns)
    )
    con.execute(
        """INSERT INTO incoming_files(path, first_seen_at, last_checked_at, size, modified_ns, stable_since, settle_seconds, status, attempts, detail)
           VALUES (?, '2026-09-18T00:00:00Z', '2026-09-18T00:00:00Z', ?, ?, 100.0, 60, 'waiting', 0, 'waiting')""",
        (str(file2.resolve()), stat2.st_size, stat2.st_mtime_ns)
    )
    con.commit()
    con.close()

    # Simulate race condition: file2 unlinks right after is_file() returns True
    original_is_file = Path.is_file

    def race_is_file(self, *args, **kwargs):
        res = original_is_file(self, *args, **kwargs)
        if str(self.resolve()) == str(file2.resolve()) and file2.exists():
            file2.unlink()
        return res

    with patch.object(Path, "is_file", side_effect=race_is_file, autospec=True):
        worker = _make_worker(db_path, incoming, settle=60)

    # Startup should succeed and file1 must be restored
    with worker.lock:
        assert str(file1.resolve()) in worker.candidates
        assert str(file2.resolve()) not in worker.candidates


def test_wt001_emergency_handler_survives_oserror_on_is_file_and_preserves_candidate(tmp_path):
    """
    WT-001: In evaluate_once's emergency scan exception handler, an OSError during Path(path).is_file()
    (e.g. NAS disconnection) must not cause an unhandled exception or discard the candidate.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    video = incoming / "nas_test.mp4"
    video.write_bytes(b"nas test content")

    worker = _make_worker(db_path, incoming, settle=60)
    assert worker.submit(str(video)) is True

    resolved_path = str(video.resolve())
    with worker.lock:
        worker.candidates[resolved_path]["stable_since"] = time.time() - 100

    # Scan raises an unhandled error
    def crashing_scan(path, candidate):
        raise RuntimeError("Scan thread aborted")

    worker._scan = crashing_scan

    # Path.is_file raises OSError simulating NAS disconnection during recovery check
    with patch.object(Path, "is_file", side_effect=OSError(112, "Host is down")):
        # Must not raise OSError
        worker.evaluate_once()

    # Candidate must be safely retained in candidates for retry!
    with worker.lock:
        assert resolved_path in worker.candidates, "Candidate was lost when Path.is_file() raised OSError!"
        assert worker.candidates[resolved_path]["check_after"] > time.monotonic()
