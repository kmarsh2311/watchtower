import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from librarymanager_core import parse_cold_storage_roots
from librarymanager_monitor import RootAvailabilityTracker


class ColdStorageDrivesTests(unittest.TestCase):
    def test_parse_cold_storage_roots_various_formats(self):
        # Empty
        self.assertEqual(parse_cold_storage_roots(None), [])
        self.assertEqual(parse_cold_storage_roots(""), [])
        self.assertEqual(parse_cold_storage_roots([]), [])

        # Python list
        self.assertEqual(parse_cold_storage_roots(["/Volumes/Vault2/Vault2"]), ["/Volumes/Vault2/Vault2"])

        # JSON array string
        self.assertEqual(parse_cold_storage_roots('["/Volumes/Vault2/Vault2", "/Volumes/Archive"]'),
                         ["/Volumes/Vault2/Vault2", "/Volumes/Archive"])

        # Comma-separated
        self.assertEqual(parse_cold_storage_roots("/Volumes/Vault2/Vault2, /Volumes/Archive"),
                         ["/Volumes/Vault2/Vault2", "/Volumes/Archive"])

        # Newline-separated
        self.assertEqual(parse_cold_storage_roots("/Volumes/Vault2/Vault2\n/Volumes/Archive"),
                         ["/Volumes/Vault2/Vault2", "/Volumes/Archive"])

    def test_cold_storage_roots_excluded_from_tracker_probes(self):
        all_roots = ["/Volumes/Main/Vault1", "/Volumes/Vault2/Vault2"]
        cold_roots = ["/Volumes/Vault2/Vault2"]
        tracker = RootAvailabilityTracker(all_roots, probe_timeout=30.0, cold_storage_roots=cold_roots)
        self.assertIn("/Volumes/Main/Vault1", tracker.roots)
        self.assertNotIn("/Volumes/Vault2/Vault2", tracker.roots)

        # Polling tracker should only check active roots
        with patch.object(tracker, "_launch_probe_unlocked") as mock_launch:
            tracker.poll()
            for r in tracker.roots:
                self.assertNotEqual(r, "/Volumes/Vault2/Vault2")

        # Explicit launch probe call on cold root must be rejected immediately without launching thread
        with patch("threading.Thread") as mock_thread:
            tracker._launch_probe_unlocked("/Volumes/Vault2/Vault2", 100.0)
            mock_thread.assert_not_called()

    def test_probe_roots_startup_skips_cold_storage(self):
        from librarymanager_monitor import probe_roots_startup
        all_roots = ["/Volumes/Main/Vault1", "/Volumes/Vault2/Vault2"]
        cold_roots = ["/Volumes/Vault2/Vault2"]
        with patch("pathlib.Path.is_dir") as mock_is_dir, patch("os.scandir") as mock_scandir:
            mock_is_dir.return_value = True
            available, unavailable = probe_roots_startup(all_roots, cold_storage_roots=cold_roots)
            self.assertIn("/Volumes/Main/Vault1", available)
            self.assertNotIn("/Volumes/Vault2/Vault2", available)
            self.assertNotIn("/Volumes/Vault2/Vault2", unavailable)
            for call_arg in mock_is_dir.call_args_list:
                self.assertNotIn("Vault2", str(call_arg))

    def test_grouped_reconciliation_worker_blocks_cold_storage_probe(self):
        from librarymanager_monitor import GroupedReconciliationWorker
        worker = GroupedReconciliationWorker(
            coordinator=MagicMock(),
            roots=["/Volumes/Main/Vault1"],
            cold_storage_roots=["/Volumes/Vault2/Vault2"],
        )
        self.assertNotIn("/Volumes/Vault2/Vault2", worker.roots)
        with patch("pathlib.Path.stat") as mock_stat:
            res = worker._probe("/Volumes/Vault2/Vault2/subfolder/video.mp4")
            self.assertEqual(res["status"], "unavailable")
            self.assertEqual(res["error"], "cold_storage")
            mock_stat.assert_not_called()

    def test_fsevents_emitter_blocks_cold_storage_stat(self):
        import os
        from watchdog.observers.fsevents import FSEventsEmitter
        FSEventsEmitter.cold_roots = {"/Volumes/Vault2/Vault2"}
        emitter = FSEventsEmitter.__new__(FSEventsEmitter)
        emitter._watch = MagicMock()
        emitter._watch.path = "/Volumes/Vault2/Vault2"
        emitter._absolute_watch_path = "/Volumes/Vault2/Vault2"
        emitter._start_time = 0.0

        mock_event = MagicMock()
        mock_event.path = "/Volumes/Vault2/Vault2"
        with patch("os.stat") as mock_stat:
            emitter.queue_events(1.0, [mock_event])
            mock_stat.assert_not_called()

    def test_get_configured_filing_destination_roots_excludes_cold_storage(self):
        from librarymanager_core import get_configured_filing_destination_roots
        config = {
            "autoFilingDestinationRoots": [
                "/Volumes/Main/Vault1",
                "/Volumes/Vault2/Vault2",
            ],
            "coldStorageRoots": [
                "/Volumes/Vault2/Vault2",
            ],
        }
        roots = get_configured_filing_destination_roots(config)
        self.assertEqual(roots, ["/Volumes/Main/Vault1"])

    def test_scan_destination_subdirectories_skips_cold_storage_without_os_walk(self):
        from librarymanager_core import _scan_destination_subdirectories, _get_cached_destination_subdirectories
        cold_roots = ["/Volumes/Vault2/Vault2"]
        target = Path("/Volumes/Vault2/Vault2/Studio")

        with patch("os.walk") as mock_walk, patch("pathlib.Path.resolve") as mock_resolve:
            res = _scan_destination_subdirectories(target, max_depth=4, cold_storage_roots=cold_roots)
            self.assertEqual(res, [])
            mock_walk.assert_not_called()

            res_cached = _get_cached_destination_subdirectories(target, max_depth=4, cold_storage_roots=cold_roots)
            self.assertEqual(res_cached, [])
            mock_walk.assert_not_called()

    def test_find_eligible_videos_in_folder_ignores_unrelated_drives_without_resolve(self):
        from librarymanager_monitor import find_eligible_videos_in_folder
        folder = Path("/Volumes/Main/Vault1/4K")

        # Mock candidates with a file on Vault2 and a file in folder
        candidates = {
            "/Volumes/Vault2/Vault2/4K/Other.mp4": {
                "is_companion": False,
                "is_temporary": False,
            },
            "/Volumes/Main/Vault1/4K/Local.mp4": {
                "is_companion": False,
                "is_temporary": False,
            },
        }

        with patch("pathlib.Path.is_dir", return_value=False):
            with patch("pathlib.Path.resolve") as mock_resolve:
                mock_resolve.side_effect = lambda: folder if "4K" in str(self) else Path(str(self))
                # Call find_eligible_videos_in_folder
                eligible = find_eligible_videos_in_folder(folder, candidates=candidates)
                # Verify Vault2 was never resolved
                for call_arg in mock_resolve.call_args_list:
                    self.assertNotIn("Vault2", str(call_arg))

    def test_register_transcoder_candidate_never_probes_unrelated_drives(self):
        import tempfile
        from librarymanager_core import connect
        from librarymanager_monitor import register_transcoder_candidate

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.sqlite3"
            conn = connect(db_path)
            conn.execute(
                "INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) "
                "VALUES ('1', '1', '/Volumes/Vault2/Vault2/Amateur/video.mp4', 'video.mp4', 1, '2026-01-01', '2026-01-01')"
            )
            conn.commit()
            conn.close()

            candidate = "/Volumes/Main/Vault1/Amateur/new_video.mp4"
            with patch("pathlib.Path.is_file") as mock_is_file, patch("pathlib.Path.resolve") as mock_resolve:
                register_transcoder_candidate(db_path, candidate, cold_storage_roots=["/Volumes/Vault2/Vault2"])
                for call_arg in mock_is_file.call_args_list:
                    self.assertNotIn("Vault2", str(call_arg))
                for call_arg in mock_resolve.call_args_list:
                    self.assertNotIn("Vault2", str(call_arg))

            cold_candidate = "/Volumes/Vault2/Vault2/Amateur/encoded.mp4"
            with patch("librarymanager_monitor.connect") as mock_connect:
                res = register_transcoder_candidate(db_path, cold_candidate, cold_storage_roots=["/Volumes/Vault2/Vault2"])
                self.assertIsNone(res)
                mock_connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()

