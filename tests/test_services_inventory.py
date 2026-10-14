import importlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import librarymanager.services
import librarymanager_core
from librarymanager.database.connection import create_connection
from librarymanager.database.migrations import ensure_schema
from librarymanager.services.inventory import (
    flatten_scene_files,
    inventory,
    refresh_scene_inventory,
)


class TestInventoryService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = create_connection(self.db_path)
        ensure_schema(conn, self.db_path)
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_direct_import_and_package_exports(self):
        self.assertTrue(callable(flatten_scene_files))
        self.assertTrue(callable(inventory))
        self.assertTrue(callable(refresh_scene_inventory))

        self.assertIs(librarymanager.services.flatten_scene_files, flatten_scene_files)
        self.assertIs(librarymanager.services.inventory, inventory)
        self.assertIs(librarymanager.services.refresh_scene_inventory, refresh_scene_inventory)

        # Confirm module resolution via importlib
        inv_mod = importlib.import_module("librarymanager.services.inventory")
        self.assertIs(inv_mod.inventory, inventory)
        self.assertIs(inv_mod.flatten_scene_files, flatten_scene_files)
        self.assertIs(inv_mod.refresh_scene_inventory, refresh_scene_inventory)

    def test_wrapper_delegation_and_markers(self):
        self.assertTrue(getattr(librarymanager_core.flatten_scene_files, "_is_watchtower_wrapper", False))
        self.assertTrue(getattr(librarymanager_core.inventory, "_is_watchtower_wrapper", False))
        self.assertTrue(getattr(librarymanager_core.refresh_scene_inventory, "_is_watchtower_wrapper", False))

        sample_scene = {
            "id": "1",
            "title": "Scene 1",
            "files": [{"id": 10, "path": "/media/scene1.mp4", "height": 1080}],
        }
        res_direct = list(flatten_scene_files([sample_scene]))
        res_wrapper = list(librarymanager_core.flatten_scene_files([sample_scene]))
        self.assertEqual(res_direct, res_wrapper)

    def test_flatten_scene_files_normalization(self):
        scenes = [
            {
                "id": "100",
                "title": "Normalized Scene",
                "performers": [{"name": "Jane Doe", "id": 5}, {"name": "Alex", "id": 6}],
                "studio": {"name": "Studio A"},
                "files": [
                    {
                        "id": 501,
                        "path": "/videos/test1.mp4",
                        "size": 12345,
                        "duration": 60.5,
                        "height": "720",
                        "fingerprints": [{"type": "oshash", "value": "abc"}],
                    }
                ],
            }
        ]
        records = list(flatten_scene_files(scenes))
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["file_id"], "501")
        self.assertEqual(rec["scene_id"], "100")
        self.assertEqual(rec["height"], 720)
        self.assertEqual(rec["studio"], "Studio A")
        self.assertEqual(json.loads(rec["performers_json"]), ["Alex", "Jane Doe"])

    def test_inventory_full_run_and_summary(self):
        video_file = Path(self.tmp.name) / "test_file.mp4"
        video_file.write_bytes(b"data")

        scenes = [
            {
                "id": "10",
                "title": "Inventory Test Scene",
                "files": [{"id": 99, "path": str(video_file), "size": 4}],
            }
        ]

        progress_calls = []

        def on_progress(cur, tot):
            progress_calls.append((cur, tot))

        summary = inventory(self.db_path, scenes, progress_callback=on_progress)
        self.assertEqual(summary["scenes"], 1)
        self.assertEqual(summary["files"], 1)
        self.assertEqual(summary["present"], 1)
        self.assertEqual(summary["missing"], 0)
        self.assertTrue(len(progress_calls) >= 1)

        conn = create_connection(self.db_path)
        file_row = conn.execute("SELECT exists_on_disk, path FROM files WHERE file_id='99'").fetchone()
        self.assertIsNotNone(file_row)
        self.assertEqual(file_row["exists_on_disk"], 1)
        self.assertEqual(file_row["path"], str(video_file))

        run_row = conn.execute("SELECT status, stash_file_count FROM inventory_runs WHERE id=?", (summary["run_id"],)).fetchone()
        self.assertIsNotNone(run_row)
        self.assertEqual(run_row["status"], "complete")
        self.assertEqual(run_row["stash_file_count"], 1)
        conn.close()

    def test_refresh_scene_inventory(self):
        video_file = Path(self.tmp.name) / "refresh_test.mp4"
        video_file.write_bytes(b"data")

        scene = {
            "id": "20",
            "title": "Refresh Scene",
            "files": [{"id": 88, "path": str(video_file), "size": 4}],
        }
        count = refresh_scene_inventory(self.db_path, scene)
        self.assertEqual(count, 1)

        conn = create_connection(self.db_path)
        file_row = conn.execute("SELECT scene_id, title FROM files WHERE file_id='88'").fetchone()
        self.assertIsNotNone(file_row)
        self.assertEqual(file_row["scene_id"], "20")
        self.assertEqual(file_row["title"], "Refresh Scene")
        conn.close()

    def test_monkeypatch_core_connect_override(self):
        fail_called = False

        def patched_connect(path):
            nonlocal fail_called
            fail_called = True
            raise sqlite3.OperationalError("intercepted connection")

        with patch.object(librarymanager_core, "connect", side_effect=patched_connect):
            with self.assertRaises(sqlite3.OperationalError):
                inventory(self.db_path, [])
        self.assertTrue(fail_called)

    def test_monkeypatch_core_is_file_on_unavailable_root_override(self):
        override_called = False

        def mock_is_offline(path_str, roots=None):
            nonlocal override_called
            override_called = True
            return True

        with patch.object(librarymanager_core, "is_file_on_unavailable_root", side_effect=mock_is_offline):
            missing_path = "/nonexistent/path/vid.mp4"
            scenes = [
                {"id": "55", "title": "Offline Scene", "files": [{"id": 77, "path": missing_path}]}
            ]
            summary = inventory(self.db_path, scenes)
        self.assertTrue(override_called)
        self.assertEqual(summary["missing"], 1)
