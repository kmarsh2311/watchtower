"""
Regression tests for Phase 3: Daemon Resiliency & Resource Leaks.

Covers:
1. Stalled network-folder scans:
   - Timeout enforcement without spawning overlapping workers.
   - Preserving active-scan locks without premature release.
   - Preventing abandoned workers from running indefinitely on stalled network mounts.
2. Unmatched companion retries and aging:
   - Bounded retries and retained state without prematurely discarding valid companions.
   - Cleanup from memory (candidates map) and database ('unmatched' status).
   - Traceable failure reporting in activity_log.
3. Companion lookup query plan:
   - EXPLAIN QUERY PLAN verification for compound and range queries.
   - Confirmation of 0 full-table scans with existing indexes (idx_files_basename, idx_files_path).
"""

import errno
import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from librarymanager_core import connect, SCHEMA
from librarymanager_monitor import (
    CompletedDownloadWorker,
    find_scene_for_companion,
    is_network_disconnect_error,
    COMPANION_EXTENSIONS,
    VIDEO_EXTENSIONS,
)


def _init_test_db(db_path: Path):
    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def _make_worker(db_path: Path, incoming_dirs, **kwargs):
    stash = MagicMock()
    stash.call_GQL.return_value = {"findScenes": {"scenes": []}}
    dirs = [Path(d) for d in incoming_dirs] if isinstance(incoming_dirs, list) else [Path(incoming_dirs)]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    return CompletedDownloadWorker(
        database_path=db_path,
        stash=stash,
        incoming_folder=str(dirs[0]),
        enabled=True,
        settle_seconds=300,
        notifications=False,
        incoming_folders=dirs,
        **kwargs
    )


# ==============================================================================
# ITEM 1: STALLED NETWORK-FOLDER SCANS & WORKER CONCURRENCY
# ==============================================================================

class TestNetworkFolderScanResiliency:

    def test_iter_directory_bounded_halts_at_deadline(self, tmp_path):
        """Directory scan stops cooperatively when deadline is reached without hanging indefinitely."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        incoming = tmp_path / "incoming"
        incoming.mkdir()

        # Create nested folders and files
        sub1 = incoming / "sub1"
        sub1.mkdir()
        (sub1 / "file1.mp4").write_bytes(b"data1")
        sub2 = incoming / "sub2"
        sub2.mkdir()
        (sub2 / "file2.mp4").write_bytes(b"data2")

        worker = _make_worker(db, incoming, scan_timeout=0.001)

        # Force a deadline in the past to simulate a timed-out network scan
        past_deadline = time.monotonic() - 1.0
        yielded = list(worker._iter_directory_bounded(incoming, deadline=past_deadline))

        # Must exit immediately and yield nothing when deadline is already passed
        assert len(yielded) == 0

    def test_network_disconnect_error_stops_directory_scan_cleanly(self, tmp_path):
        """Simulated network disconnect / SMB socket timeout aborts scan cleanly without crashing."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        (incoming / "file1.mp4").write_bytes(b"data1")
        sub = incoming / "stalled_share"
        sub.mkdir()

        worker = _make_worker(db, incoming)

        real_scandir = os.scandir

        def faulty_scandir(path):
            if "stalled_share" in str(path):
                raise OSError(errno.ETIMEDOUT, "Operation timed out on SMB mount")
            return real_scandir(path)

        with patch("os.scandir", side_effect=faulty_scandir):
            yielded = list(worker._iter_directory_bounded(incoming))

        # file1 was scanned; stalled_share triggered network error and halted cleanly without unhandled exception
        assert any(p.name == "file1.mp4" for p in yielded)

    def test_continuation_worker_aborts_if_prev_thread_alive(self, tmp_path):
        """Continuation scan worker does NOT run if previous worker is still alive after join timeout."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        incoming = tmp_path / "incoming"
        incoming.mkdir()

        worker = _make_worker(db, incoming)
        worker.worker_join_timeout = 0.05
        key = worker._folder_key(incoming)

        # Simulate a hung previous thread that stays alive
        hang_event = threading.Event()
        prev_thread = threading.Thread(target=hang_event.wait, name="hung-worker-sim", daemon=True)
        prev_thread.start()

        continuation_ran = [False]

        def dummy_scan(*args, **kwargs):
            continuation_ran[0] = True

        try:
            worker._active_scans[key] = threading.current_thread()
            with patch.object(worker, "_scan_incoming_folder", side_effect=dummy_scan):
                # Run continuation with prev_thread still alive
                worker._run_folder_scan(key, incoming, prev_thread=prev_thread)

            # Continuation must have aborted without running scan to prevent overlapping workers
            assert continuation_ran[0] is False, "Continuation must NOT scan while previous worker is still alive!"

            # Active scan lock must NOT be prematurely released; it must be held by prev_thread
            with worker._scan_lock:
                assert worker._active_scans.get(key) is prev_thread, (
                    "Active scan lock was released prematurely instead of pointing to still-running worker!"
                )
        finally:
            hang_event.set()
            prev_thread.join(timeout=1.0)

    def test_dispatch_folder_scan_does_not_spawn_overlapping_workers(self, tmp_path):
        """_dispatch_folder_scan enqueues to _pending_scans and does NOT spawn a thread when worker is active."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        incoming = tmp_path / "incoming"
        incoming.mkdir()

        worker = _make_worker(db, incoming)
        key = worker._folder_key(incoming)

        blocker = threading.Event()
        mock_worker = threading.Thread(target=blocker.wait, daemon=True)
        mock_worker.start()

        try:
            worker._active_scans[key] = mock_worker

            # Attempt to dispatch another scan for the same folder
            res = worker._dispatch_folder_scan(incoming, cutoff=100.0)

            # Must return None (no new thread spawned)
            assert res is None

            # Pending scan must be enqueued
            with worker._scan_lock:
                assert key in worker._pending_scans
                assert worker._pending_scans[key]["cutoff"] == 100.0
                assert worker._active_scans[key] is mock_worker
        finally:
            blocker.set()
            mock_worker.join(timeout=1.0)

    def test_submit_tree_does_not_spawn_overlapping_workers(self, tmp_path):
        """submit_tree enqueues tree to _pending_scans without spawning concurrent worker."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        sub_tree = incoming / "MovieFolder"
        sub_tree.mkdir()

        worker = _make_worker(db, incoming)
        key = worker._folder_key(incoming)

        blocker = threading.Event()
        mock_worker = threading.Thread(target=blocker.wait, daemon=True)
        mock_worker.start()

        try:
            worker._active_scans[key] = mock_worker

            count = worker.submit_tree(sub_tree)
            assert count == 0

            with worker._scan_lock:
                assert key in worker._pending_scans
                assert sub_tree in worker._pending_scans[key]["trees"]
        finally:
            blocker.set()
            mock_worker.join(timeout=1.0)

    def test_dead_worker_thread_cleaned_up_on_dispatch(self, tmp_path):
        """Dead worker thread in _active_scans is cleaned up when next scan is dispatched."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        incoming = tmp_path / "incoming"
        incoming.mkdir()

        worker = _make_worker(db, incoming)
        key = worker._folder_key(incoming)

        # Create a thread that terminates immediately
        dead_thread = threading.Thread(target=lambda: None, daemon=True)
        dead_thread.start()
        dead_thread.join()
        assert not dead_thread.is_alive()

        worker._active_scans[key] = dead_thread

        # Dispatching should prune dead thread and start new thread
        with patch.object(worker, "_run_folder_scan"):
            new_thread = worker._dispatch_folder_scan(incoming, cutoff=None)

        assert new_thread is not None
        assert new_thread is not dead_thread
        assert worker._active_scans[key] is new_thread


# ==============================================================================
# ITEM 2: UNMATCHED COMPANION RETRIES AND AGING
# ==============================================================================

class TestUnmatchedCompanionAging:

    def test_companion_waiting_increments_attempts_and_persists(self, tmp_path):
        """Each waiting retry increments candidate attempts and persists to database."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        incoming = tmp_path / "incoming"
        incoming.mkdir()

        worker = _make_worker(db, incoming, max_companion_attempts=24)

        jpg = incoming / "Orphan.jpg"
        jpg.write_bytes(b"image-data")
        path_str = str(jpg.resolve())

        worker.candidates[path_str] = {
            "size": len(b"image-data"),
            "modified_ns": jpg.stat().st_mtime_ns,
            "stable_since": time.time() - 10,
            "attempts": 0,
            "is_companion": True,
            "is_temporary": False,
            "first_seen_time": time.time(),
        }

        # First evaluation
        worker._process_companion(path_str, worker.candidates[path_str])
        assert worker.candidates[path_str]["attempts"] == 1
        assert worker.candidates[path_str]["last_saved_status"] == "waiting"

        con = connect(db)
        row = con.execute("SELECT status, attempts FROM incoming_files WHERE path=?", (path_str,)).fetchone()
        con.close()
        assert row is not None
        assert row["status"] == "waiting"
        assert row["attempts"] == 1

    def test_unmatched_companion_retires_after_max_attempts(self, tmp_path):
        """Companion transitions to 'unmatched', pops from candidates, and logs activity after max attempts."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        incoming = tmp_path / "incoming"
        incoming.mkdir()

        worker = _make_worker(db, incoming, max_companion_attempts=3)

        jpg = incoming / "Orphan.jpg"
        jpg.write_bytes(b"image-data")
        path_str = str(jpg.resolve())

        worker.candidates[path_str] = {
            "size": len(b"image-data"),
            "modified_ns": jpg.stat().st_mtime_ns,
            "stable_since": time.time() - 10,
            "attempts": 2,  # Next attempt reaches max_companion_attempts (3)
            "is_companion": True,
            "is_temporary": False,
            "first_seen_time": time.time(),
        }

        # Process companion: reaches 3 attempts
        worker._process_companion(path_str, worker.candidates[path_str])

        # Must be removed from candidates (no memory leak)
        assert path_str not in worker.candidates, "Aged-out companion must be removed from candidates"

        # Database status must be 'unmatched'
        con = connect(db)
        row = con.execute("SELECT status, attempts, detail FROM incoming_files WHERE path=?", (path_str,)).fetchone()
        act = con.execute(
            "SELECT action, status, detail FROM activity_log WHERE category='companion' AND old_path=?", (path_str,)
        ).fetchone()
        con.close()

        assert row is not None
        assert row["status"] == "unmatched"
        assert "Max attempts" in row["detail"]
        assert act is not None
        assert act["action"] == "unmatched companion aged out"
        assert act["status"] == "recorded"

    def test_unmatched_companion_retires_after_max_age_seconds(self, tmp_path):
        """Companion transitions to 'unmatched' and pops from candidates after exceeding max age."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        incoming = tmp_path / "incoming"
        incoming.mkdir()

        worker = _make_worker(db, incoming, max_companion_attempts=100, max_companion_age_seconds=60.0)

        jpg = incoming / "OldOrphan.jpg"
        jpg.write_bytes(b"image-data")
        path_str = str(jpg.resolve())

        # First seen 120 seconds ago (> 60s limit)
        worker.candidates[path_str] = {
            "size": len(b"image-data"),
            "modified_ns": jpg.stat().st_mtime_ns,
            "stable_since": time.time() - 10,
            "attempts": 1,
            "is_companion": True,
            "is_temporary": False,
            "first_seen_time": time.time() - 120.0,
        }

        worker._process_companion(path_str, worker.candidates[path_str])

        assert path_str not in worker.candidates
        con = connect(db)
        row = con.execute("SELECT status, detail FROM incoming_files WHERE path=?", (path_str,)).fetchone()
        con.close()
        assert row is not None
        assert row["status"] == "unmatched"
        assert "Max age" in row["detail"]

    def test_companion_waiting_for_specific_downloading_video_not_prematurely_discarded(self, tmp_path):
        """Companion matched to an active incoming video in candidates is NOT discarded despite exceeding attempts."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        incoming = tmp_path / "incoming"
        incoming.mkdir()

        worker = _make_worker(db, incoming, max_companion_attempts=2)

        # Video is currently downloading in incoming
        video = incoming / "BigMovie.mp4"
        video.write_bytes(b"video-chunk")
        vid_str = str(video.resolve())

        jpg = incoming / "BigMovie.jpg"
        jpg.write_bytes(b"image-data")
        jpg_str = str(jpg.resolve())

        worker.candidates[vid_str] = {
            "size": len(b"video-chunk"),
            "modified_ns": video.stat().st_mtime_ns,
            "stable_since": time.time() - 5,
            "attempts": 0,
            "is_companion": False,
            "is_temporary": False,
        }
        worker.candidates[jpg_str] = {
            "size": len(b"image-data"),
            "modified_ns": jpg.stat().st_mtime_ns,
            "stable_since": time.time() - 10,
            "attempts": 5,  # Exceeds max_companion_attempts (2)
            "is_companion": True,
            "is_temporary": False,
            "first_seen_time": time.time() - 9999,  # Old
        }

        worker._process_companion(jpg_str, worker.candidates[jpg_str])

        # Companion must NOT be discarded because the video is actively downloading
        assert jpg_str in worker.candidates
        assert "finish downloading" in worker.candidates[jpg_str]["last_saved_detail"]

    def test_late_companion_pairs_successfully_before_aging(self, tmp_path):
        """Companion that matches an existing scene in the library pairs and is relocated."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        library = tmp_path / "library"
        library.mkdir()

        # Existing video in library
        lib_video = library / "Scene1.mp4"
        lib_video.write_bytes(b"video-data")

        con = connect(db)
        con.execute(
            "INSERT INTO files(file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES (?,?,?,?,1,datetime('now'),datetime('now'))",
            ("f1", "101", str(lib_video), "Scene1.mp4")
        )
        con.commit()
        con.close()

        worker = _make_worker(db, incoming)

        # Compound companion arrives in incoming for cross-directory scene
        comp = incoming / "Scene1.mp4.jpg"
        comp.write_bytes(b"poster-data")
        comp_str = str(comp.resolve())

        worker.candidates[comp_str] = {
            "size": len(b"poster-data"),
            "modified_ns": comp.stat().st_mtime_ns,
            "stable_since": time.time() - 10,
            "attempts": 0,
            "is_companion": True,
            "is_temporary": False,
            "first_seen_time": time.time(),
        }

        worker._process_companion(comp_str, worker.candidates[comp_str])

        # Successfully paired and removed from candidates
        assert comp_str not in worker.candidates
        target_dest = library / "Scene1.mp4.jpg"
        assert target_dest.exists(), "Companion must be relocated next to scene"
        assert not comp.exists(), "Companion source must be moved"

        con = connect(db)
        row = con.execute("SELECT status FROM incoming_files WHERE path=?", (comp_str,)).fetchone()
        con.close()
        assert row is not None
        assert row["status"] == "paired"


# ==============================================================================
# ITEM 3: COMPANION LOOKUP QUERY PLAN (EXPLAIN QUERY PLAN)
# ==============================================================================

class TestCompanionLookupQueryPlan:

    def test_explain_query_plan_compound_lookup_uses_basename_index(self, tmp_path):
        """EXPLAIN QUERY PLAN confirms compound companion lookup uses idx_files_basename without SCAN."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        con = connect(db)

        plan = con.execute("""
            EXPLAIN QUERY PLAN
            SELECT file_id, scene_id, path, basename FROM files
            WHERE exists_on_disk=1 AND basename = ?
        """, ("Movie.m4v",)).fetchall()
        con.close()

        plan_str = " ".join(str(tuple(row)) for row in plan)
        assert "idx_files_basename" in plan_str
        assert "SEARCH" in plan_str
        assert "SCAN" not in plan_str, f"Full table scan detected: {plan_str}"

    def test_explain_query_plan_range_lookup_uses_path_index(self, tmp_path):
        """EXPLAIN QUERY PLAN confirms directory range lookup uses idx_files_path without SCAN."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        con = connect(db)

        p1 = "/Volumes/Vault/Folder/"
        u1 = "/Volumes/Vault/Folder0"

        plan = con.execute("""
            EXPLAIN QUERY PLAN
            SELECT file_id, scene_id, path, basename FROM files
            WHERE exists_on_disk=1 AND path >= ? AND path < ?
        """, (p1, u1)).fetchall()
        con.close()

        plan_str = " ".join(str(tuple(row)) for row in plan)
        assert "idx_files_path" in plan_str
        assert "SEARCH" in plan_str
        assert "SCAN" not in plan_str, f"Full table scan detected: {plan_str}"

    def test_explain_query_plan_or_range_lookup_uses_multi_index_path(self, tmp_path):
        """EXPLAIN QUERY PLAN confirms multi-separator OR query uses idx_files_path without SCAN."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        con = connect(db)

        p1 = "/Volumes/Vault/Folder/"
        u1 = "/Volumes/Vault/Folder0"
        p2 = "\\Volumes\\Vault\\Folder\\"
        u2 = "\\Volumes\\Vault\\Folder]"

        plan = con.execute("""
            EXPLAIN QUERY PLAN
            SELECT file_id, scene_id, path, basename FROM files
            WHERE exists_on_disk=1 AND ((path >= ? AND path < ?) OR (path >= ? AND path < ?))
        """, (p1, u1, p2, u2)).fetchall()
        con.close()

        plan_str = " ".join(str(tuple(row)) for row in plan)
        assert "idx_files_path" in plan_str
        assert "SEARCH" in plan_str
        assert "SCAN" not in plan_str, f"Full table scan detected in OR query: {plan_str}"

    def test_no_full_table_scan_during_find_scene_for_companion(self, tmp_path):
        """All queries executed by find_scene_for_companion are verified via EXPLAIN to avoid full-table scans."""
        db = tmp_path / "test.sqlite3"
        _init_test_db(db)
        con = connect(db)

        executed_queries = []

        class QueryTracingConnection:
            def __init__(self, real):
                self._real = real
            def execute(self, sql, params=()):
                executed_queries.append((sql.strip(), params))
                return self._real.execute(sql, params)
            def fetchall(self):
                return self._real.fetchall()
            def close(self):
                self._real.close()

        tracing_con = QueryTracingConnection(con)

        # 1. Non-compound companion query
        find_scene_for_companion(tracing_con, "/Volumes/Vault/Folder/Scene1.jpg")

        # 2. Compound companion query
        find_scene_for_companion(tracing_con, "/Volumes/Vault/Folder/Movie.m4v.jpg")

        assert len(executed_queries) >= 2

        # Verify EXPLAIN QUERY PLAN for every executed query
        for sql, params in executed_queries:
            plan = con.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            plan_str = " ".join(str(tuple(row)) for row in plan)
            assert "SCAN files" not in plan_str, f"Query '{sql}' caused full-table scan: {plan_str}"
            assert "SEARCH files USING INDEX" in plan_str, f"Query '{sql}' did not use index: {plan_str}"

        con.close()
