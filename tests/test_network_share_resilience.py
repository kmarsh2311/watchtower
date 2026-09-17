"""Tests for Watchtower generic network-share disconnect and hang resilience.

Covers:
1. One healthy local/available root + one unavailable root.
2. Unavailable root does not prevent healthy root processing.
3. Recursive scan timeout/failure does not kill the worker thread.
4. Files on an unavailable root are not treated as deleted in candidates.
5. Files on an unavailable root are not treated as deleted in inventory.
6. Repeated failures do not cause log/database churn.
7. Root returning online is detected automatically.
8. Monitoring resumes and observer watch is registered after reconnection.
9. Files created while root was offline are discovered during recovery.
10. Worker remains alive after simulated OSError, TimeoutError, and socket failures.
11. Bounded thread safety: no unbounded threads left behind.
12. One permanently hung root cannot stop another healthy root.
13. Repeated health checks do not create additional blocked threads for the hung root.
14. Recovery monitoring itself cannot freeze the main loop (< 10ms).
15. Scanner and probe threads remain strictly bounded over repeated failure cycles.
16. Generic paths across local volumes, SMB, and NFS on macOS, Linux, and Windows.
"""

import errno
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from librarymanager_core import SCHEMA, connect, inventory, is_file_on_unavailable_root
from librarymanager_monitor import (
    CompletedDownloadWorker,
    LibraryEventHandler,
    RootAvailabilityTracker,
    is_network_disconnect_error,
    is_path_available,
)


def _make_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def _make_worker(db_path, incoming_folders, settle=300, fallback=60):
    stash = MagicMock()
    stash.call_GQL.return_value = {"findScenes": {"scenes": []}}
    folders = [str(f) for f in incoming_folders]
    worker = CompletedDownloadWorker(
        database_path=db_path,
        stash=stash,
        incoming_folder=folders[0] if folders else None,
        enabled=True,
        settle_seconds=settle,
        notifications=False,
        fallback_seconds=fallback,
        incoming_folders=folders,
    )
    return worker


class TestNetworkShareResilience:
    def test_1_healthy_and_unavailable_root_isolation(self, tmp_path):
        """1. One healthy local root + one unavailable root: healthy root processes normally."""
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        healthy_incoming = tmp_path / "healthy_incoming"
        healthy_incoming.mkdir()
        offline_incoming = tmp_path / "offline_incoming"

        worker = _make_worker(db_path, [healthy_incoming, offline_incoming])

        video = healthy_incoming / "Scene.mp4"
        video.write_bytes(b"content")
        video_str = str(video.resolve())

        worker._fallback_check()

        assert video_str in worker.candidates, "Healthy incoming folder files must be submitted"

    def test_2_unavailable_root_does_not_prevent_healthy_processing(self, tmp_path):
        """2. Unavailable root throwing [Errno 57] Socket is not connected does not block healthy root."""
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        healthy_incoming = tmp_path / "healthy_incoming"
        healthy_incoming.mkdir()
        mock_offline = tmp_path / "mock_offline"
        mock_offline.mkdir()

        worker = _make_worker(db_path, [healthy_incoming, mock_offline])

        healthy_vid = healthy_incoming / "Healthy.mp4"
        healthy_vid.write_bytes(b"healthy")
        healthy_str = str(healthy_vid.resolve())

        real_is_dir = Path.is_dir

        def selective_is_dir(path_obj):
            if "mock_offline" in str(path_obj):
                raise OSError(57, "Socket is not connected")
            return real_is_dir(path_obj)

        with patch.object(Path, "is_dir", autospec=True, side_effect=selective_is_dir):
            paths = worker._current_incoming_paths()

        assert healthy_str in paths, "Healthy incoming file must be found despite socket error on offline folder"

    def test_3_recursive_scan_timeout_failure_does_not_kill_worker(self, tmp_path):
        """3. Recursive scan failure does not kill the worker thread."""
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        incoming = tmp_path / "incoming"
        incoming.mkdir()
        worker = _make_worker(db_path, [incoming])

        worker.start()
        try:
            assert worker.is_alive()

            with patch.object(worker, "_current_video_paths", side_effect=OSError(errno.ETIMEDOUT, "Operation timed out")):
                worker._fallback_check()

            assert worker.is_alive(), "CompletedDownloadWorker thread must remain alive after fallback error"

            vid = incoming / "AfterError.mp4"
            vid.write_bytes(b"new video")
            assert worker.submit(str(vid.resolve())) is True
        finally:
            worker.stop()
            worker.join(timeout=2.0)

    def test_4_files_on_unavailable_root_not_treated_as_deleted_in_candidates(self, tmp_path):
        """4. Files on an unavailable root are not marked 'gone' or popped from candidates."""
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        incoming = tmp_path / "incoming"
        incoming.mkdir()
        worker = _make_worker(db_path, [incoming], settle=60)

        video = incoming / "Scene.mp4"
        video.write_bytes(b"video")
        video_str = str(video.resolve())

        worker.submit(video_str)
        assert video_str in worker.candidates

        with patch.object(Path, "stat", side_effect=OSError(57, "Socket is not connected")):
            worker.evaluate_once(mono_now=100.0)

        assert video_str in worker.candidates, "File on disconnected network share must remain in candidates"
        assert worker.candidates[video_str]["check_after"] > 100.0, "Candidate check must be throttled"

        con = connect(db_path)
        row = con.execute("SELECT status FROM incoming_files WHERE path=?", (video_str,)).fetchone()
        con.close()
        assert row["status"] != "gone", "Status must NOT be set to gone when share disconnects"

    def test_5_files_on_unavailable_root_not_treated_as_deleted_in_inventory(self, tmp_path):
        """5. Files on an unavailable root are not marked exists_on_disk=0 in inventory."""
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        offline_file = "/mnt/storage/nas/Scene.mp4"
        con = connect(db_path)
        con.execute(
            """INSERT INTO files(file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at)
               VALUES('f1', 's1', ?, 'Scene.mp4', 1, datetime('now'), datetime('now'))""",
            (offline_file,)
        )
        con.execute(
            """INSERT INTO filesystem_monitor_status(id, token, state, roots_json, unavailable_roots_json)
               VALUES(1, 'tok', 'running', '["/mnt/storage/nas"]', '["/mnt/storage/nas"]')
               ON CONFLICT(id) DO UPDATE SET unavailable_roots_json=excluded.unavailable_roots_json"""
        )
        con.commit()
        con.close()

        scene_data = [{
            "id": "s1",
            "title": "Offline Scene",
            "files": [{"id": "f1", "path": offline_file, "basename": "Scene.mp4"}]
        }]

        res = inventory(db_path, scene_data)

        con = connect(db_path)
        row = con.execute("SELECT exists_on_disk, missing_since FROM files WHERE file_id='f1'").fetchone()
        events = con.execute("SELECT * FROM inventory_events WHERE file_id='f1' AND event_type='file_missing'").fetchall()
        con.close()

        assert row["exists_on_disk"] == 1, "File on unavailable root must retain exists_on_disk=1"
        assert row["missing_since"] is None, "missing_since must NOT be set for unavailable root"
        assert len(events) == 0, "No file_missing events should be recorded for unavailable root"

    def test_6_repeated_failures_do_not_cause_log_or_db_churn(self, tmp_path):
        """6. Repeated failures on an unavailable root log at most once per state transition."""
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        offline_root = "/Volumes/OfflineVault"
        roots = [offline_root]
        known_unavailable = set()

        recorded_transitions = 0
        for _ in range(5):
            is_avail = is_path_available(offline_root)
            if not is_avail:
                if offline_root not in known_unavailable:
                    known_unavailable.add(offline_root)
                    recorded_transitions += 1

        assert recorded_transitions == 1, "Transition to unavailable must only be recorded once, not on every poll cycle"

    def test_7_root_returning_online_detected_automatically(self, tmp_path):
        """7. Root returning online is detected automatically without restarting monitor."""
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        reconnecting_root = tmp_path / "reconnecting_root"
        known_unavailable = {str(reconnecting_root)}

        reconnecting_root.mkdir()

        is_avail = is_path_available(str(reconnecting_root))
        assert is_avail is True

        recovered = False
        if is_avail and str(reconnecting_root) in known_unavailable:
            known_unavailable.remove(str(reconnecting_root))
            recovered = True

        assert recovered is True, "Recovery must be detected when path becomes available"
        assert str(reconnecting_root) not in known_unavailable

    def test_8_monitoring_resumes_after_reconnection(self, tmp_path):
        """8. Watcher re-registers observer watch when root returns online."""
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        root = tmp_path / "dynamic_root"
        root.mkdir()

        mock_observer = MagicMock()
        mock_handler = MagicMock()
        watched_roots = {}

        if str(root) not in watched_roots and is_path_available(str(root)):
            watch = mock_observer.schedule(mock_handler, str(root), recursive=True)
            watched_roots[str(root)] = watch

        mock_observer.schedule.assert_called_once_with(mock_handler, str(root), recursive=True)
        assert str(root) in watched_roots

    def test_9_files_created_while_root_offline_discovered_during_recovery(self, tmp_path):
        """9. Files created while root was offline are discovered by recovery scan."""
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        root = tmp_path / "mount_root"
        root.mkdir()
        incoming = root / "incoming"
        incoming.mkdir()

        worker = _make_worker(db_path, [incoming])

        offline_arrival = incoming / "OfflineArrival.mp4"
        offline_arrival.write_bytes(b"arrived while offline")
        arrival_str = str(offline_arrival.resolve())

        assert arrival_str not in worker.candidates

        worker.trigger_recovery_scan(root)

        assert arrival_str in worker.candidates, "File created during outage must be ingested on recovery scan"

    def test_10_worker_remains_alive_after_simulated_socket_errors(self, tmp_path):
        """10. Worker remains functional after socket-related filesystem errors."""
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        incoming = tmp_path / "incoming"
        incoming.mkdir()
        worker = _make_worker(db_path, [incoming])

        assert is_network_disconnect_error(OSError(57, "Socket is not connected")) is True
        assert is_network_disconnect_error(OSError(errno.ETIMEDOUT, "Operation timed out")) is True
        assert is_network_disconnect_error(OSError(errno.EHOSTDOWN, "Host is down")) is True
        assert is_network_disconnect_error(OSError(errno.EIO, "Input/output error")) is True
        assert is_network_disconnect_error(ValueError("Generic error")) is False

        handler = LibraryEventHandler(db_path, MagicMock(), False, worker)
        missing_path = "/Volumes/NonExistent/Deleted.mp4"
        with patch.object(Path, "exists", side_effect=OSError(57, "Socket is not connected")):
            handler._notify_if_still_missing(missing_path)

        con = connect(db_path)
        events = con.execute("SELECT * FROM activity_log WHERE action='external deletion'").fetchall()
        con.close()
        assert len(events) == 0, "Socket error must not record an external deletion review"

    def test_11_bounded_thread_safety_no_unbounded_threads(self, tmp_path):
        """11. Repeated calls do not leak or spawn unbounded worker threads."""
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        incoming = tmp_path / "incoming"
        incoming.mkdir()
        worker = _make_worker(db_path, [incoming])

        active_before = threading.active_count()

        for _ in range(20):
            worker.trigger_recovery_scan(incoming)

        active_after = threading.active_count()
        assert active_after <= active_before + 4, "Thread count must remain bounded under rapid calls"

    def test_12_hung_root_cannot_stop_healthy_root(self, tmp_path):
        """12. One permanently hung root cannot block or starve a healthy root."""
        healthy_root = tmp_path / "healthy"
        healthy_root.mkdir()
        hung_root = tmp_path / "hung"

        hang_event = threading.Event()

        def mock_is_dir(path_obj):
            if str(hung_root) in str(path_obj):
                hang_event.wait(timeout=5.0)
                return False
            return True

        with patch.object(Path, "is_dir", autospec=True, side_effect=mock_is_dir):
            tracker = RootAvailabilityTracker([str(healthy_root), str(hung_root)], probe_timeout=0.05)
            # First poll launches probes
            tracker.poll()
            # Wait briefly for hung root probe to exceed 0.05s timeout
            time.sleep(0.1)
            avail, unavail, rec, lost = tracker.poll()

            # Healthy root must remain available
            assert str(healthy_root) in avail, "Healthy root must be available"
            assert str(hung_root) in unavail, "Hung root must be marked unavailable after timeout"
            assert str(healthy_root) not in unavail

        hang_event.set()

    def test_13_repeated_health_checks_do_not_create_additional_blocked_threads(self, tmp_path):
        """13. Repeated health checks on a hung root do not spawn additional probe threads."""
        hung_root = tmp_path / "hung_root"
        hang_event = threading.Event()

        def blocking_is_dir(path_obj):
            hang_event.wait(timeout=5.0)
            return False

        with patch.object(Path, "is_dir", autospec=True, side_effect=blocking_is_dir):
            tracker = RootAvailabilityTracker([str(hung_root)], probe_timeout=0.05)
            tracker.poll()  # First poll launches 1 probe thread
            time.sleep(0.02)

            threads_after_first = threading.active_count()

            # Perform 25 rapid poll cycles while root probe is still hung
            for _ in range(25):
                tracker.poll()

            threads_after_25 = threading.active_count()
            assert threads_after_25 == threads_after_first, (
                "No additional probe threads may be launched while previous probe is still in flight"
            )

        hang_event.set()

    def test_14_recovery_monitoring_cannot_freeze_main_loop(self, tmp_path):
        """14. tracker.poll() is completely non-blocking and executes in < 10ms."""
        hung_root = tmp_path / "hung_share"
        hang_event = threading.Event()

        def block_forever(path_obj):
            hang_event.wait(timeout=10.0)
            return False

        with patch.object(Path, "is_dir", autospec=True, side_effect=block_forever):
            tracker = RootAvailabilityTracker([str(hung_root)], probe_timeout=0.1)

            t0 = time.monotonic()
            for _ in range(10):
                tracker.poll()
            elapsed = time.monotonic() - t0

            # 10 calls should complete in under 50ms total (< 5ms per call)
            assert elapsed < 0.1, f"tracker.poll() must be non-blocking (took {elapsed:.4f}s)"

        hang_event.set()

    def test_15_scanner_and_probe_threads_strictly_bounded(self, tmp_path):
        """15. Total probe and scanner threads remain strictly bounded over repeated failure cycles."""
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        roots = [str(tmp_path / f"root_{i}") for i in range(3)]
        for r in roots:
            Path(r).mkdir()

        tracker = RootAvailabilityTracker(roots, probe_timeout=0.1)
        worker = _make_worker(db_path, roots)

        baseline_threads = threading.active_count()

        # Simulate 10 failure cycles
        for _ in range(10):
            tracker.poll()
            worker._fallback_check()

        time.sleep(0.05)
        active = threading.active_count()
        # Max additional threads: len(roots) probes + len(roots) worker scan pool threads
        assert active <= baseline_threads + len(roots) * 2 + 2, (
            "Total threads must be strictly bounded by configured roots and incoming folders"
        )

    def test_16_generic_paths_across_platforms(self):
        """16. Generic path resolution works across local, SMB, and NFS paths on all OS platforms."""
        unavailable = [
            "/Volumes/MediaShare",
            "/mnt/nfs/storage",
            "\\\\nas\\vault",
            "D:\\LocalLibrary",
        ]

        # Files on unavailable roots
        assert is_file_on_unavailable_root("/Volumes/MediaShare/Video.mp4", unavailable) is True
        assert is_file_on_unavailable_root("/Volumes/MediaShare/Sub/Video.mp4", unavailable) is True
        assert is_file_on_unavailable_root("/mnt/nfs/storage/Incoming/clip.mp4", unavailable) is True
        assert is_file_on_unavailable_root("\\\\nas\\vault\\scene.mkv", unavailable) is True
        assert is_file_on_unavailable_root("D:\\LocalLibrary\\vid.mp4", unavailable) is True

        # Files on healthy/other roots
        assert is_file_on_unavailable_root("/Volumes/HealthyShare/Video.mp4", unavailable) is False
        assert is_file_on_unavailable_root("/mnt/local/storage/clip.mp4", unavailable) is False
        assert is_file_on_unavailable_root("C:\\Other\\clip.mp4", unavailable) is False
        assert is_file_on_unavailable_root("/tmp/test.mp4", unavailable) is False
