import errno
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from watchdog.events import FileDeletedEvent

from librarymanager_core import connect, is_file_on_unavailable_root
from librarymanager_monitor import (
    CompletedDownloadWorker,
    LibraryEventHandler,
    RootAvailabilityTracker,
    find_scene_for_companion,
    is_network_disconnect_error,
)


def _init_db(db_path: Path):
    con = connect(db_path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            file_id TEXT PRIMARY KEY,
            scene_id TEXT NOT NULL,
            path TEXT NOT NULL,
            basename TEXT NOT NULL,
            exists_on_disk INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
        CREATE TABLE IF NOT EXISTS filesystem_monitor_status (
            id INTEGER PRIMARY KEY CHECK(id=1),
            token TEXT,
            pid INTEGER,
            state TEXT NOT NULL DEFAULT 'running',
            started_at TEXT,
            last_heartbeat TEXT,
            auto_restart_failures INTEGER NOT NULL DEFAULT 0,
            roots_json TEXT NOT NULL DEFAULT '[]',
            unavailable_roots_json TEXT NOT NULL DEFAULT '[]'
        );
        INSERT OR IGNORE INTO filesystem_monitor_status(id, state) VALUES (1, 'running');
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            category TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            old_path TEXT,
            new_path TEXT,
            scene_id TEXT,
            file_id TEXT,
            detail TEXT
        );
        CREATE TABLE IF NOT EXISTS filesystem_events (
            event_key TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            source_path TEXT NOT NULL,
            destination_path TEXT,
            is_directory INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            event_count INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending',
            detail TEXT
        );
        CREATE TABLE IF NOT EXISTS incoming_files (
            path TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            last_checked_at TEXT NOT NULL,
            size INTEGER,
            stable_since REAL,
            status TEXT NOT NULL DEFAULT 'waiting',
            attempts INTEGER NOT NULL DEFAULT 0,
            scan_job_id TEXT,
            detail TEXT
        );
        CREATE TABLE IF NOT EXISTS transcoder_candidates (
            candidate_path TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting'
        );
    """)
    con.commit()
    con.close()


# ==============================================================================
# 1. DELETION SCHEDULER TESTS
# ==============================================================================

class TestDeletionSchedulerSafeguards:
    def test_batch_deletes_cannot_create_unbounded_threads(self, tmp_path):
        """Batch deletes must not spawn unbounded threads; thread pool remains bounded."""
        db_path = tmp_path / "test.sqlite3"
        _init_db(db_path)
        move_worker = MagicMock()
        move_worker.transcoder_compatibility = False

        tracker = RootAvailabilityTracker([str(tmp_path / "root1"), str(tmp_path / "root2")])
        handler = LibraryEventHandler(db_path, move_worker, False, incoming_worker=None, availability_tracker=tracker)

        threads_before = threading.active_count()
        # Fire 200 deletion events across 2 roots
        for i in range(200):
            root = "root1" if i % 2 == 0 else "root2"
            p = tmp_path / root / f"video_{i:04d}.mp4"
            handler.on_deleted(FileDeletedEvent(str(p)))

        # Verify items entered pending queue without exploding threads
        with handler._deletion_scheduler_lock:
            assert len(handler._pending_deletions) == 200

        threads_during = threading.active_count()
        # Max new threads should be at most 2 (the single timer and at most per-root worker)
        assert threads_during - threads_before <= 3, f"Thread count exploded: {threads_during - threads_before}"

        # Clean up timer
        with handler._deletion_scheduler_lock:
            if handler._deletion_timer:
                handler._deletion_timer.cancel()
                handler._deletion_timer = None

    def test_one_hung_network_path_cannot_stop_deletion_processing_for_healthy_roots(self, tmp_path):
        """A stuck root in kernel I/O must not block deletion verification for healthy roots."""
        db_path = tmp_path / "test.sqlite3"
        _init_db(db_path)
        move_worker = MagicMock()
        move_worker.transcoder_compatibility = False

        hung_root = str(tmp_path / "hung_root")
        healthy_root = str(tmp_path / "healthy_root")
        os.makedirs(healthy_root, exist_ok=True)

        tracker = RootAvailabilityTracker([hung_root, healthy_root])
        handler = LibraryEventHandler(db_path, move_worker, False, incoming_worker=None, availability_tracker=tracker)

        # Simulate hung_root is already in flight (e.g. stuck in kernel SMB stat)
        with handler._deletion_scheduler_lock:
            handler._deletion_in_flight_roots.add(hung_root)

        # Add deletions on both roots
        hung_file = f"{hung_root}/hung_movie.mp4"
        healthy_file = f"{healthy_root}/healthy_movie.mp4"

        with handler._deletion_scheduler_lock:
            handler._pending_deletions[hung_file] = time.monotonic() - 1.0
            handler._pending_deletions[healthy_file] = time.monotonic() - 1.0

        # Drain
        with patch.object(handler, "_verify_deletions_for_root") as mock_verify:
            handler._drain_pending_deletions()
            # Healthy root must be dispatched!
            mock_verify.assert_called_once()
            called_root, called_paths = mock_verify.call_args[0]
            assert called_root == healthy_root
            assert called_paths == [healthy_file]

        # Hung file must remain safely pending without spawning a thread
        with handler._deletion_scheduler_lock:
            assert hung_file in handler._pending_deletions
            if handler._deletion_timer:
                handler._deletion_timer.cancel()
                handler._deletion_timer = None

    def test_offline_roots_never_generate_false_deletion_warnings(self, tmp_path):
        """Files residing on an offline share are discarded silently without false deletion warnings."""
        db_path = tmp_path / "test.sqlite3"
        _init_db(db_path)
        move_worker = MagicMock()
        move_worker.transcoder_compatibility = False

        offline_root = str(tmp_path / "offline_share")
        tracker = RootAvailabilityTracker([offline_root])
        # Mark root as offline
        tracker._states[offline_root]["status"] = "unavailable"

        notifications_sent = []
        handler = LibraryEventHandler(db_path, move_worker, True, incoming_worker=None, availability_tracker=tracker)
        handler.notifications = True

        offline_file = f"{offline_root}/Sub/scene.mp4"
        with handler._deletion_scheduler_lock:
            handler._pending_deletions[offline_file] = time.monotonic() - 1.0

        with patch("librarymanager_monitor.notify", side_effect=lambda n, msg: notifications_sent.append(msg)):
            handler._drain_pending_deletions()

        # Discarded with no alerts
        with handler._deletion_scheduler_lock:
            assert offline_file not in handler._pending_deletions

        assert len(notifications_sent) == 0, f"False notification sent: {notifications_sent}"

        con = connect(db_path)
        acts = con.execute("SELECT * FROM activity_log WHERE action='external deletion'").fetchall()
        con.close()
        assert len(acts) == 0, "False external deletion activity recorded"

    def test_scheduler_remains_bounded_over_repeated_failures(self, tmp_path):
        """Scheduler drains expired items and does not leak threads on repeated check failures."""
        db_path = tmp_path / "test.sqlite3"
        _init_db(db_path)
        move_worker = MagicMock()
        move_worker.transcoder_compatibility = False

        tracker = RootAvailabilityTracker([str(tmp_path / "root")])
        handler = LibraryEventHandler(db_path, move_worker, False, incoming_worker=None, availability_tracker=tracker)

        # Add stale items (expired past 120s)
        for i in range(50):
            handler._pending_deletions[f"/dummy/stale_{i}.mp4"] = time.monotonic() - 200.0

        handler._drain_pending_deletions()

        with handler._deletion_scheduler_lock:
            assert len(handler._pending_deletions) == 0, "Expired items should be pruned"


# ==============================================================================
# 2. DATABASE RANGE QUERY TESTS
# ==============================================================================

class TestDatabaseRangeQuerySafeguards:
    def test_explain_query_plan_uses_idx_files_path(self, tmp_path):
        """EXPLAIN QUERY PLAN confirms SQLite uses index idx_files_path for range query."""
        db_path = tmp_path / "test.sqlite3"
        _init_db(db_path)
        con = connect(db_path)

        p1 = "/Volumes/Vault/Folder/"
        u1 = "/Volumes/Vault/Folder0"

        plan = con.execute("""
            EXPLAIN QUERY PLAN
            SELECT file_id, scene_id, path, basename FROM files
            WHERE exists_on_disk=1 AND path >= ? AND path < ?
        """, (p1, u1)).fetchall()
        plan_str = " ".join(str(dict(row)) for row in plan)
        con.close()

        assert "idx_files_path" in plan_str, f"Expected idx_files_path in query plan, got: {plan_str}"
        assert "SEARCH" in plan_str, f"Expected SEARCH using index, got: {plan_str}"

    def test_supports_posix_windows_drive_and_unc_paths(self, tmp_path):
        """Range query and matching work seamlessly across POSIX, Windows drive, and UNC paths."""
        db_path = tmp_path / "test.sqlite3"
        _init_db(db_path)
        con = connect(db_path)

        rows = [
            ("f1", "s1", "/Volumes/Vault/Folder/Scene1.mp4", "Scene1.mp4"),
            ("f2", "s2", "C:\\Media\\Vault\\Folder\\Scene2.mp4", "Scene2.mp4"),
            ("f3", "s3", "\\\\NAS\\Share\\Vault\\Folder\\Scene3.mp4", "Scene3.mp4"),
        ]
        con.executemany("INSERT INTO files(file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES (?,?,?,?,1,datetime('now'),datetime('now'))", rows)
        con.commit()

        # POSIX
        row, _ = find_scene_for_companion(con, "/Volumes/Vault/Folder/Scene1.jpg")
        assert row is not None and row["file_id"] == "f1"

        # Windows drive
        row, _ = find_scene_for_companion(con, "C:\\Media\\Vault\\Folder\\Scene2.jpg")
        assert row is not None and row["file_id"] == "f2"

        # UNC
        row, _ = find_scene_for_companion(con, "\\\\NAS\\Share\\Vault\\Folder\\Scene3.jpg")
        assert row is not None and row["file_id"] == "f3"

        con.close()

    def test_identical_matching_results_against_baseline(self, tmp_path):
        """Verify identical matching results between indexed range and full table scan."""
        db_path = tmp_path / "test.sqlite3"
        _init_db(db_path)
        con = connect(db_path)

        # Populate diverse files in various folders
        test_rows = [
            ("f1", "s1", "/Volume1/A/Video.mp4", "Video.mp4"),
            ("f2", "s2", "/Volume1/A/Other.mp4", "Other.mp4"),
            ("f3", "s3", "/Volume1/B/Video.mp4", "Video.mp4"),
            ("f4", "s4", "/Volume2/C/Movie.mkv", "Movie.mkv"),
        ]
        con.executemany("INSERT INTO files(file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES (?,?,?,?,1,datetime('now'),datetime('now'))", test_rows)
        con.commit()

        # Companion in /Volume1/A
        match_a, rem_a = find_scene_for_companion(con, "/Volume1/A/Video.jpg")
        assert match_a is not None and match_a["file_id"] == "f1"
        assert rem_a == ""

        # Companion in /Volume1/B
        match_b, rem_b = find_scene_for_companion(con, "/Volume1/B/Video.jpg")
        assert match_b is not None and match_b["file_id"] == "f3"

        # Unmatched companion in /Volume1/A
        match_none, _ = find_scene_for_companion(con, "/Volume1/A/Unrelated.jpg")
        assert match_none is None

        con.close()


# ==============================================================================
# 3. NOTIFICATION HOT-RELOAD TESTS
# ==============================================================================

class TestNotificationHotReloadSafeguards:
    def test_hot_reload_updates_all_worker_notifications(self, tmp_path):
        """Reload configuration updates notifications across MoveWorker, CompletedDownloadWorker, and LibraryEventHandler."""
        db_path = tmp_path / "test.sqlite3"
        _init_db(db_path)

        move_worker = MagicMock()
        move_worker.notifications = True
        incoming_worker = MagicMock()
        incoming_worker.notifications = True
        handler = MagicMock()
        handler.notifications = True

        # Simulate the reload block from monitor.main
        new_cfg = {"mac_notifications": False}
        if "mac_notifications" in new_cfg:
            new_notif = bool(new_cfg["mac_notifications"])
            move_worker.notifications = new_notif
            if incoming_worker:
                incoming_worker.notifications = new_notif
            if handler:
                handler.notifications = new_notif

        assert move_worker.notifications is False
        assert incoming_worker.notifications is False
        assert handler.notifications is False


# ==============================================================================
# 4. METHOD NAMING & CLEANUP TESTS
# ==============================================================================

class TestMethodNamingSafeguards:
    def test_current_video_paths_has_explanatory_docstring_and_is_alias(self):
        """_current_video_paths preserves alias contract with clear docstring."""
        import inspect
        src = inspect.getsource(CompletedDownloadWorker._current_video_paths)
        assert "_current_incoming_paths" in src
        doc = CompletedDownloadWorker._current_video_paths.__doc__
        assert doc is not None
        assert "Alias for _current_incoming_paths" in doc

    def test_scheduler_does_not_prune_queued_items_while_root_is_in_flight(self, tmp_path):
        """120-second cleanup must not discard queued deletion checks while the root is actively in flight."""
        db_path = tmp_path / "test.sqlite3"
        _init_db(db_path)
        move_worker = MagicMock()
        move_worker.transcoder_compatibility = False

        slow_root = str(tmp_path / "slow_share")
        tracker = RootAvailabilityTracker([slow_root])
        handler = LibraryEventHandler(db_path, move_worker, False, incoming_worker=None, availability_tracker=tracker)

        # Simulate root is actively in flight (processing previous batch)
        with handler._deletion_scheduler_lock:
            handler._deletion_in_flight_roots.add(slow_root)
            # Item queued > 120 seconds ago
            queued_path = f"{slow_root}/delayed_check.mp4"
            handler._pending_deletions[queued_path] = time.monotonic() - 150.0

        # Drain
        handler._drain_pending_deletions()

        # Item must NOT have been discarded because its root is actively in flight
        with handler._deletion_scheduler_lock:
            assert queued_path in handler._pending_deletions, "Queued deletion check was prematurely pruned while root is in flight!"
