"""Regression tests for WT-002 and WT-004 resilience findings.

WT-002: Bounded deferred-rescan mechanism for incoming folder changes.
WT-004: Safe candidate relocation preventing candidate loss and concurrency races.
"""

import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from librarymanager_core import SCHEMA
from librarymanager_monitor import CompletedDownloadWorker


def _make_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def _make_worker(db_path, incoming, settle=60, max_attempts=3):
    stash = MagicMock()
    stash.call_GQL.return_value = {"findScenes": {"scenes": []}}
    return CompletedDownloadWorker(
        database_path=db_path,
        stash=stash,
        incoming_folder=str(incoming),
        enabled=True,
        settle_seconds=settle,
        notifications=False,
        fallback_seconds=60,
        max_attempts=max_attempts,
        incoming_folders=[str(incoming)],
    )


# ==============================================================================
# WT-004 REGRESSION TESTS: Safe Candidate Relocation
# ==============================================================================

def test_wt004_relocate_preserves_candidate_when_destination_stat_raises_oserror(tmp_path):
    """
    WT-004: If Path(destination).stat() raises OSError during CompletedDownloadWorker.relocate(),
    the original candidate must remain intact in self.candidates rather than being lost.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    video = incoming / "source_video.mp4"
    video.write_bytes(b"content")
    stat = video.stat()

    worker = _make_worker(db_path, incoming)
    source_resolved = str(video.resolve())
    candidate_data = {
        "size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
        "stable_since": time.time() - 10,
        "attempts": 1,
        "is_companion": False,
        "is_temporary": False,
        "check_after": 0.0,
    }
    with worker.lock:
        worker.candidates[source_resolved] = dict(candidate_data)

    missing_dest = incoming / "non_existent_folder" / "dest.mp4"
    result = worker.relocate(source_resolved, str(missing_dest))

    assert result is False
    with worker.lock:
        assert source_resolved in worker.candidates, "Candidate was lost from self.candidates on failed destination stat!"
        assert worker.candidates[source_resolved]["attempts"] == 1
        assert worker.candidates[source_resolved]["size"] == stat.st_size


def test_wt004_relocate_preserves_concurrent_candidate_updates(tmp_path):
    """
    WT-004: If another thread updates a candidate in self.candidates while destination stat is executing,
    relocate() must not clobber the newer state with a stale snapshot.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    worker = _make_worker(db_path, incoming)

    source = incoming / "source.mp4"
    dest = incoming / "dest.mp4"
    source.write_bytes(b"data")
    dest.write_bytes(b"data")
    source_str = str(source.resolve())
    dest_str = str(dest.resolve())

    with worker.lock:
        worker.candidates[source_str] = {
            "size": 4,
            "modified_ns": 1000,
            "stable_since": 100.0,
            "attempts": 0,
            "is_companion": False,
            "is_temporary": False,
        }

    real_stat = Path.stat

    def slow_dest_stat(path_obj):
        if str(path_obj.resolve()) == dest_str:
            # Simulate concurrent worker update to source candidate while stat is in-flight
            with worker.lock:
                worker.candidates[source_str]["attempts"] = 2
                worker.candidates[source_str]["stable_since"] = 555.0
        return real_stat(path_obj)

    with patch.object(Path, "stat", side_effect=slow_dest_stat, autospec=True):
        res = worker.relocate(source_str, dest_str)

    assert res is True
    with worker.lock:
        assert dest_str in worker.candidates
        # Must retain the newer attempts (2), not the stale snapshot (0)
        assert worker.candidates[dest_str]['attempts'] == 2, (
            f"Expected updated attempts 2, but got stale {worker.candidates[dest_str]['attempts']}"
        )


def test_wt004_relocate_aborts_if_candidate_removed_concurrently(tmp_path):
    """
    WT-004: If another thread removes source from self.candidates (e.g. scanned and imported)
    while destination stat is executing, relocate() must not resurrect destination as a ghost.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    worker = _make_worker(db_path, incoming)

    source = incoming / "source.mp4"
    dest = incoming / "dest.mp4"
    source.write_bytes(b"data")
    dest.write_bytes(b"data")
    source_str = str(source.resolve())
    dest_str = str(dest.resolve())
    with worker.lock:
        worker.candidates.pop(dest_str, None)

    with worker.lock:
        worker.candidates[source_str] = {
            "size": 4,
            "modified_ns": 1000,
            "stable_since": 100.0,
            "attempts": 0,
            "is_companion": False,
            "is_temporary": False,
        }

    real_stat = Path.stat

    def concurrent_remove_stat(path_obj):
        if str(path_obj.resolve()) == dest_str:
            # Simulate another thread removing source while destination is being checked
            with worker.lock:
                worker.candidates.pop(source_str, None)
        return real_stat(path_obj)

    with patch.object(Path, "stat", side_effect=concurrent_remove_stat, autospec=True):
        res = worker.relocate(source_str, dest_str)

    assert res is False, "relocate() should return False if source was removed during stat check"
    with worker.lock:
        assert dest_str not in worker.candidates, "Destination must not be resurrected when source was removed"


def test_wt004_relocate_db_only_aborts_if_status_changed_concurrently(tmp_path):
    """
    WT-004: If a candidate was only in the DB and another thread updates its status
    to "completed" or imports it before destination stat finishes, relocate() must abort.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    worker = _make_worker(db_path, incoming)

    source = incoming / "db_source.mp4"
    dest = incoming / "db_dest.mp4"
    source.write_bytes(b"content")
    dest.write_bytes(b"content")
    s_stat = source.stat()

    source_str = str(source.resolve())
    dest_str = str(dest.resolve())

    con = sqlite3.connect(str(db_path))
    con.execute(
        """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, size, modified_ns, stable_since, settle_seconds, status, attempts, detail)
           VALUES (?, "2026-09-18T00:00:00Z", "2026-09-18T00:00:00Z", ?, ?, 100.0, 60, "waiting", 0, "detail")""",
        (source_str, s_stat.st_size, s_stat.st_mtime_ns)
    )
    con.commit()
    con.close()
    # Ensure neither is in worker.candidates memory
    with worker.lock:
        worker.candidates.pop(source_str, None)
        worker.candidates.pop(dest_str, None)

    real_stat = Path.stat

    def concurrent_db_update(path_obj):
        if str(path_obj.resolve()) == dest_str:
            # Another thread marks source as completed
            con = sqlite3.connect(str(db_path))
            con.execute("UPDATE incoming_files SET status='completed' WHERE path=?", (source_str,))
            con.commit()
            con.close()
        return real_stat(path_obj)

    with patch.object(Path, "stat", side_effect=concurrent_db_update, autospec=True):
        res = worker.relocate(source_str, dest_str)

    assert res is False
    with worker.lock:
        assert dest_str not in worker.candidates


# ==============================================================================
# WT-002 REGRESSION TESTS: Bounded Deferred-Rescan Mechanism
# ==============================================================================

def test_wt002_submit_tree_defers_and_discovers_older_files_after_active_scan(tmp_path):
    """
    WT-002: When submit_tree() is called while an active folder scan is in progress,
    the request must be deferred and executed when the active scan finishes,
    ensuring files with older modification times are discovered.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    worker = _make_worker(db_path, incoming)
    # Ensure worker.started_at is current
    worker.started_at = time.time()

    # Block the initial scan in rglob
    scan_active_event = threading.Event()
    release_scan_event = threading.Event()
    real_rglob = Path.rglob

    subfolder = incoming / "DelayedTree"
    subfolder.mkdir()
    old_video = subfolder / "old_mtime_movie.mp4"
    old_video.write_bytes(b"old video data")
    # Set older mtime prior to worker startup
    old_mtime = worker.started_at - 10000.0
    os.utime(str(old_video), (old_mtime, old_mtime))

    def controlled_rglob(path_obj, pattern):
        if path_obj == incoming:
            scan_active_event.set()
            release_scan_event.wait(timeout=5.0)
            return iter(())
        return real_rglob(path_obj, pattern)

    with patch.object(Path, "rglob", side_effect=controlled_rglob, autospec=True):
        # 1. Dispatch an initial scan that will hold the scan thread
        t = worker._dispatch_folder_scan(incoming, worker.started_at)
        assert scan_active_event.wait(timeout=2.0), "Initial scan thread did not start"

        # 2. While the scan is active, submit_tree() is called on subfolder
        worker.submit_tree(subfolder)

        # 3. Release the initial scan
        release_scan_event.set()

        # Wait for deferred work to complete
        resolved_old = str(old_video.resolve())
        deadline = time.monotonic() + 3.0
        found = False
        while time.monotonic() < deadline:
            with worker.lock:
                if resolved_old in worker.candidates:
                    found = True
                    break
            time.sleep(0.05)

        assert found, f"Older video {resolved_old} was not discovered by deferred scan!"


def test_wt002_continuation_thread_handoff_guarantees_previous_thread_stopped(tmp_path):
    """
    WT-002: If maximum consecutive passes is reached, continuation thread handoff
    must guarantee the previous scan thread has fully terminated before the
    replacement thread begins scanning.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    worker = _make_worker(db_path, incoming)

    thread_execution_log = []
    log_lock = threading.Lock()

    # We will verify that no two scan threads have overlapping active scanning windows
    active_scanning_threads = set()

    real_rglob = Path.rglob

    def monitored_rglob(path_obj, pattern):
        curr = threading.current_thread()
        with log_lock:
            # Verify no other thread is currently in scanning block
            assert len(active_scanning_threads) == 0, (
                f"Concurrency violation: {active_scanning_threads} still active when {curr} started!"
            )
            active_scanning_threads.add(curr)
            thread_execution_log.append((curr.name, "start", time.monotonic()))

        try:
            time.sleep(0.05)
            return real_rglob(path_obj, pattern)
        finally:
            with log_lock:
                active_scanning_threads.remove(curr)
                thread_execution_log.append((curr.name, "end", time.monotonic()))

    # Enqueue work that will trigger continuation
    sub1 = incoming / "sub1"
    sub1.mkdir()
    v1 = sub1 / "v1.mp4"
    v1.write_bytes(b"v1")

    with patch.object(Path, "rglob", side_effect=monitored_rglob, autospec=True):
        worker.submit_tree(sub1)
        # Wait for scan to finish
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with worker._scan_lock:
                if not worker._active_scans:
                    break
            time.sleep(0.05)

    with log_lock:
        assert len(thread_execution_log) >= 2, "Expected at least start and end log entries"


def test_wt002_rapid_requests_single_flight_and_bounded_threads(tmp_path):
    """
    WT-002: Multiple rapid calls to submit_tree and _dispatch_folder_scan
    must be single-flight per incoming folder and never spawn unlimited threads.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    worker = _make_worker(db_path, incoming)

    sub = incoming / "rapid"
    sub.mkdir()

    entered = 0
    entered_lock = threading.Lock()
    release_scan = threading.Event()

    def slow_rglob(path_obj, pattern):
        nonlocal entered
        with entered_lock:
            entered += 1
        release_scan.wait(timeout=3.0)
        return iter(())

    with patch.object(Path, "rglob", side_effect=slow_rglob, autospec=True):
        # Dispatch first
        worker.submit_tree(sub)

        # Bombard with 20 calls
        for _ in range(20):
            worker.submit_tree(sub)
            worker._dispatch_folder_scan(incoming, time.time())

        time.sleep(0.1)
        with entered_lock:
            assert entered == 1, f"Expected exactly 1 in-flight scan thread, got {entered}"

        release_scan.set()
        worker.stop()


def test_wt002_continuation_thread_joins_previous_thread_before_scanning(tmp_path):
    """
    WT-002: When a continuation thread is spawned after reaching pass limit,
    it must guarantee the previous thread has fully stopped scanning before
    the continuation thread begins scanning.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    worker = _make_worker(db_path, incoming)
    worker._max_consecutive_passes = 1  # Force continuation after 1 pass

    sub1 = incoming / "sub1"
    sub2 = incoming / "sub2"
    sub1.mkdir()
    sub2.mkdir()
    v1 = sub1 / "v1.mp4"
    v2 = sub2 / "v2.mp4"
    v1.write_bytes(b"v1")
    v2.write_bytes(b"v2")

    active_threads = []
    overlap_detected = False
    lock = threading.Lock()

    prev_thread_ref = [None]

    def instrumented_scan_tree(root, *args, **kwargs):
        nonlocal overlap_detected
        current = threading.current_thread()
        with lock:
            if prev_thread_ref[0] is not None and prev_thread_ref[0] != current:
                if prev_thread_ref[0].is_alive():
                    overlap_detected = True
            active_threads.append(current)
            prev_thread_ref[0] = current
        time.sleep(0.05)
        return 1

    worker._scan_tree_worker = instrumented_scan_tree

    # Start first scan
    worker.submit_tree(sub1)
    # Immediately schedule second scan while first is in-flight to trigger continuation
    worker.submit_tree(sub2)

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        with worker._scan_lock:
            if not worker._active_scans:
                break
        time.sleep(0.05)

    assert not overlap_detected, "Continuation thread began scanning while previous thread was still alive!"


def test_wt002_pass_limit_continuation_executes_pending_folder_scan_with_older_file(tmp_path):
    """
    WT-002: When pass limit is reached, any pending folder scan must be preserved
    and executed by the continuation thread rather than dropped.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    worker = _make_worker(db_path, incoming)
    worker._max_consecutive_passes = 1  # Force pass limit after 1 pass
    worker.started_at = time.time()

    older_video = incoming / "older_folder_scan.mp4"
    older_video.write_bytes(b"older video")
    older_mtime = worker.started_at - 5000.0
    os.utime(str(older_video), (older_mtime, older_mtime))

    scan_active_event = threading.Event()
    release_scan_event = threading.Event()
    real_rglob = Path.rglob

    call_count = 0
    def blocking_rglob(path_obj, pattern):
        nonlocal call_count
        if path_obj == incoming:
            call_count += 1
            if call_count == 1:
                scan_active_event.set()
                release_scan_event.wait(timeout=5.0)
                return iter(())
        return real_rglob(path_obj, pattern)

    with patch.object(Path, "rglob", side_effect=blocking_rglob, autospec=True):
        # 1. Dispatch initial scan
        worker._dispatch_folder_scan(incoming, worker.started_at)
        assert scan_active_event.wait(timeout=2.0), "Initial scan did not start"

        # 2. Queue pending folder scan with older cutoff while initial scan is active
        worker._dispatch_folder_scan(incoming, older_mtime - 10.0)

        # 3. Unblock initial scan
        release_scan_event.set()

        # 4. Wait for continuation thread to process the older file
        resolved_older = str(older_video.resolve())
        deadline = time.monotonic() + 3.0
        found = False
        while time.monotonic() < deadline:
            with worker.lock:
                if resolved_older in worker.candidates:
                    found = True
                    break
            time.sleep(0.05)

        assert found, f"Older video {resolved_older} was dropped when pass limit was reached!"


def test_wt002_pending_request_with_both_subtrees_and_folder_cutoff_executes_both(tmp_path):
    """
    WT-002: When pending work contains BOTH subtrees (from submit_tree) and a folder cutoff
    (from _dispatch_folder_scan), the continuation execution must process both.
    """
    db_path = tmp_path / "watchtower.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db_path)

    worker = _make_worker(db_path, incoming)
    worker._max_consecutive_passes = 1
    worker.started_at = time.time()

    sub = incoming / "SubTreeDir"
    sub.mkdir()
    tree_video = sub / "tree_video.mp4"
    tree_video.write_bytes(b"tree video")
    # Tree video has old mtime
    os.utime(str(tree_video), (worker.started_at - 10000, worker.started_at - 10000))

    cutoff_video = incoming / "cutoff_video.mp4"
    cutoff_video.write_bytes(b"cutoff video")
    cutoff_mtime = worker.started_at - 2000.0
    os.utime(str(cutoff_video), (cutoff_mtime, cutoff_mtime))

    scan_active_event = threading.Event()
    release_scan_event = threading.Event()
    real_rglob = Path.rglob

    call_count = 0
    def blocking_rglob(path_obj, pattern):
        nonlocal call_count
        if path_obj == incoming:
            call_count += 1
            if call_count == 1:
                scan_active_event.set()
                release_scan_event.wait(timeout=5.0)
                return iter(())
        return real_rglob(path_obj, pattern)

    with patch.object(Path, "rglob", side_effect=blocking_rglob, autospec=True):
        worker._dispatch_folder_scan(incoming, worker.started_at)
        assert scan_active_event.wait(timeout=2.0)

        # Queue BOTH submit_tree and _dispatch_folder_scan
        worker.submit_tree(sub)
        worker._dispatch_folder_scan(incoming, cutoff_mtime - 10.0)

        release_scan_event.set()

        tree_res = str(tree_video.resolve())
        cutoff_res = str(cutoff_video.resolve())

        deadline = time.monotonic() + 3.0
        both_found = False
        while time.monotonic() < deadline:
            with worker.lock:
                if tree_res in worker.candidates and cutoff_res in worker.candidates:
                    both_found = True
                    break
            time.sleep(0.05)

        with worker.lock:
            assert tree_res in worker.candidates, "Subtree video was not discovered!"
            assert cutoff_res in worker.candidates, "Folder cutoff video was not discovered!"
