import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import librarymanager.services
import librarymanager_core
from librarymanager.database.connection import create_connection
from librarymanager.database.migrations import ensure_schema
from librarymanager.services import renaming


class TestRenamingService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = create_connection(self.db_path)
        ensure_schema(conn, self.db_path)
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rename_lock(self):
        with renaming.rename_lock(self.db_path):
            lock_path = self.db_path.with_suffix(".rename.lock")
            self.assertTrue(lock_path.exists())

    def test_enqueue_claim_and_finish_rename(self):
        # First enqueue schedules worker
        should_schedule = renaming.enqueue_rename(self.db_path, "scene-101", 1000.0, debounce_seconds=1.0)
        self.assertTrue(should_schedule)

        # Second enqueue within debounce does not schedule again
        should_schedule_2 = renaming.enqueue_rename(self.db_path, "scene-101", 1000.5, debounce_seconds=1.0)
        self.assertFalse(should_schedule_2)

        # Before due time, claim returns None
        claimed_id, next_at, pending = renaming.claim_due_rename(self.db_path, 1000.8)
        self.assertIsNone(claimed_id)
        self.assertEqual(pending, 1)

        # At/after due time, claim returns scene-101
        claimed_id, next_at, pending = renaming.claim_due_rename(self.db_path, 1001.5)
        self.assertEqual(claimed_id, "scene-101")

        # Finish queued rename removes from queue and writes audit record
        renaming.finish_queued_rename(self.db_path, "scene-101", "success", "Renamed cleanly")
        conn = create_connection(self.db_path)
        q_count = conn.execute("SELECT COUNT(*) FROM rename_queue WHERE scene_id='scene-101'").fetchone()[0]
        self.assertEqual(q_count, 0)
        audit_row = conn.execute("SELECT outcome, detail FROM rename_audit WHERE scene_id='scene-101'").fetchone()
        self.assertIsNotNone(audit_row)
        self.assertEqual(audit_row[0], "success")
        self.assertEqual(audit_row[1], "Renamed cleanly")
        conn.close()

    def test_fail_and_make_pending_rename_due(self):
        renaming.enqueue_rename(self.db_path, "scene-202", 1000.0, debounce_seconds=0.0)
        renaming.fail_queued_rename(self.db_path, "scene-202", "Disk write error")

        conn = create_connection(self.db_path)
        status, err = conn.execute("SELECT status, last_error FROM rename_queue WHERE scene_id='scene-202'").fetchone()
        self.assertEqual(status, "failed")
        self.assertEqual(err, "Disk write error")
        conn.close()

        # Retry by making it due
        made_due = renaming.make_pending_rename_due(self.db_path, "scene-202")
        self.assertTrue(made_due)

        claimed, _, _ = renaming.claim_due_rename(self.db_path, time.time())
        self.assertEqual(claimed, "scene-202")

    def test_cancel_pending_rename_and_release_worker(self):
        renaming.enqueue_rename(self.db_path, "scene-303", 1000.0, debounce_seconds=10.0)
        cancelled = renaming.cancel_pending_rename(self.db_path, "scene-303")
        self.assertTrue(cancelled)

        conn = create_connection(self.db_path)
        q_count = conn.execute("SELECT COUNT(*) FROM rename_queue WHERE scene_id='scene-303'").fetchone()[0]
        self.assertEqual(q_count, 0)
        conn.close()

        # Test releasing worker schedule flag
        renaming.release_worker_schedule(self.db_path)
        conn = create_connection(self.db_path)
        sched = conn.execute("SELECT scheduled FROM rename_worker_state WHERE id=1").fetchone()[0]
        self.assertEqual(sched, 0)
        conn.close()

    def test_apply_scene_filename_flow(self):
        conn = create_connection(self.db_path)
        conn.execute(
            "INSERT INTO files(file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) "
            "VALUES ('42', 'sc-1', ?, 'old_name.mp4', 1, '2024-01-01', '2024-01-01')",
            (str(Path(self.tmp.name) / "old_name.mp4"),)
        )
        conn.commit()
        conn.close()

        # Mock preview_scene_filename
        preview_data = {
            "status": "ready",
            "file_id": 42,
            "scene_id": "sc-1",
            "current_path": str(Path(self.tmp.name) / "old_name.mp4"),
            "proposed_path": str(Path(self.tmp.name) / "new_name.mp4"),
            "scene_date": "2024-01-01",
            "video_quality": "1080p",
            "associated_files": [],
        }

        old_file = Path(preview_data["current_path"])
        old_file.write_bytes(b"video-bytes")

        def mock_move(file_id, folder, name):
            old_file.rename(Path(self.tmp.name) / name)
            return True

        with patch("librarymanager.services.renaming.preview_scene_filename", return_value=preview_data):
            result = renaming.apply_scene_filename(self.db_path, "sc-1", mock_move)
            self.assertEqual(result["status"], "renamed")
            self.assertTrue(result["action_performed"])

        new_file = Path(preview_data["proposed_path"])
        self.assertTrue(new_file.exists())

    def test_wrapper_parity_and_markers(self):
        wrappers = [
            librarymanager_core.rename_lock,
            librarymanager_core._connect_short,
            librarymanager_core.enqueue_rename,
            librarymanager_core.cancel_pending_rename,
            librarymanager_core.make_pending_rename_due,
            librarymanager_core.release_worker_schedule,
            librarymanager_core.claim_due_rename,
            librarymanager_core.finish_queued_rename,
            librarymanager_core.fail_queued_rename,
            librarymanager_core.apply_scene_filename,
            librarymanager_core.apply_manual_filename,
        ]
        for w in wrappers:
            self.assertTrue(getattr(w, "_is_watchtower_wrapper", False), f"Wrapper marker missing on {w.__name__}")

    def test_reexports_in_services_package(self):
        self.assertIs(librarymanager.services.rename_lock, renaming.rename_lock)
        self.assertIs(librarymanager.services.enqueue_rename, renaming.enqueue_rename)
        self.assertIs(librarymanager.services.cancel_pending_rename, renaming.cancel_pending_rename)
        self.assertIs(librarymanager.services.make_pending_rename_due, renaming.make_pending_rename_due)
        self.assertIs(librarymanager.services.release_worker_schedule, renaming.release_worker_schedule)
        self.assertIs(librarymanager.services.claim_due_rename, renaming.claim_due_rename)
        self.assertIs(librarymanager.services.finish_queued_rename, renaming.finish_queued_rename)
        self.assertIs(librarymanager.services.fail_queued_rename, renaming.fail_queued_rename)
        self.assertIs(librarymanager.services.apply_scene_filename, renaming.apply_scene_filename)
        self.assertIs(librarymanager.services.apply_manual_filename, renaming.apply_manual_filename)
