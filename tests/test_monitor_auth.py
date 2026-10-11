import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from librarymanager_core import SCHEMA, connect, recent_activity
from librarymanager import start_filesystem_monitor
import librarymanager_monitor


def _init_db(database_path):
    connection = connect(database_path)
    connection.executescript(SCHEMA)
    connection.close()


class MonitorAuthTests(unittest.TestCase):
    def test_start_filesystem_monitor_persists_api_key_from_headers(self):
        """Verify start_filesystem_monitor detects API key from Stash session headers and persists it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.sqlite3"
            _init_db(db)

            stash = MagicMock()
            stash.find_plugin_config.return_value = {}
            stash.s = MagicMock()
            stash.s.headers = {"ApiKey": "stash-secret-token-123"}

            stopped = {"state": "stopped", "token": None}
            running = {"state": "running", "token": "tok123", "roots": [tmpdir], "unavailable_roots": []}
            process = MagicMock(poll=MagicMock(return_value=None))

            with patch("librarymanager.fetch_library_roots", return_value=[tmpdir]), \
                 patch("librarymanager.filesystem_monitor_summary", side_effect=[stopped, running]), \
                 patch("librarymanager.subprocess.Popen", return_value=process), \
                 patch("librarymanager.time.sleep"):
                start_filesystem_monitor(stash, db, {"Host": "127.0.0.1", "Port": 9999})

            runtime_path = Path(start_filesystem_monitor.__code__.co_filename).with_name("monitor-runtime.json")
            try:
                self.assertTrue(runtime_path.exists())
                content = json.loads(runtime_path.read_text(encoding="utf-8"))
                self.assertEqual(content["server_connection"]["ApiKey"], "stash-secret-token-123")
            finally:
                runtime_path.unlink(missing_ok=True)

    def test_start_filesystem_monitor_persists_api_key_from_call_gql(self):
        """Verify start_filesystem_monitor queries getApiKey if not in headers and persists it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.sqlite3"
            _init_db(db)

            stash = MagicMock()
            stash.find_plugin_config.return_value = {}
            stash.s = MagicMock(headers={})
            stash.call_GQL.return_value = {"configuration": {"general": {"apiKey": "gql-api-key-456"}}}

            stopped = {"state": "stopped", "token": None}
            running = {"state": "running", "token": "tok123", "roots": [tmpdir], "unavailable_roots": []}
            process = MagicMock(poll=MagicMock(return_value=None))

            with patch("librarymanager.fetch_library_roots", return_value=[tmpdir]), \
                 patch("librarymanager.filesystem_monitor_summary", side_effect=[stopped, running]), \
                 patch("librarymanager.subprocess.Popen", return_value=process), \
                 patch("librarymanager.time.sleep"):
                start_filesystem_monitor(stash, db, {"Host": "127.0.0.1", "Port": 9999})

            runtime_path = Path(start_filesystem_monitor.__code__.co_filename).with_name("monitor-runtime.json")
            try:
                self.assertTrue(runtime_path.exists())
                content = json.loads(runtime_path.read_text(encoding="utf-8"))
                self.assertEqual(content["server_connection"]["ApiKey"], "gql-api-key-456")
            finally:
                runtime_path.unlink(missing_ok=True)

    def test_move_worker_reconciliation_401_error_guidance(self):
        """Verify that a 401 Unauthorized error during move reconciliation produces clear guidance."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.sqlite3"
            _init_db(db)

            conn = connect(db)
            conn.execute(
                "INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at) "
                "VALUES ('f1', '100', '/data/movie.mp4', 'movie.mp4', 1000, 1, '2026-01-01', '2026-01-01')"
            )
            conn.commit()
            conn.close()

            stash = MagicMock()
            stash.metadata_scan.side_effect = Exception("401 Unauthorized query failed. v0.31.1-0")

            worker = librarymanager_monitor.MoveWorker(
                database_path=db, stash=stash, enabled=True,
                notifications=False, transcoder_compatibility=True
            )

            with patch.object(librarymanager_monitor, "tracked_move",
                              return_value=({"file_id": "f1", "scene_id": "100", "path": "/data/movie.mp4"}, "Transcode replacement")), \
                 patch.object(librarymanager_monitor, "destination_inventory_conflict", return_value=None), \
                 patch.object(librarymanager_monitor, "transcoder_replacement_decision",
                              return_value=("/data/movie-encoded.mp4", "safe")):
                worker._process_move("/data/movie.mp4", "/data/movie-encoded.mp4")

            activities = recent_activity(db)
            failed_act = next((a for a in activities if a["status"] == "failed"), None)
            self.assertIsNotNone(failed_act)
            self.assertIn("401 Unauthorized: Stash session expired or API key missing", failed_act["detail"])
    def test_force_api_key_upgrades_session_cookie_to_header(self):
        """Verify that StashInterface with force_api_key=True replaces SessionCookie with ApiKey header."""
        from stashapi.stashapp import StashInterface

        with patch("stashapi.classes.requests.session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.headers = {}
            mock_session.cookies = MagicMock()

            with patch.object(StashInterface, "stash_version", return_value="v0.31.1"), \
                 patch.object(StashInterface, "call_GQL",
                              return_value={"configuration": {"general": {"apiKey": "perm-key-999"}}}), \
                 patch.object(StashInterface, "_get_fragments_introspection", return_value=[]):
                conn = {
                    "Host": "localhost",
                    "Port": 9999,
                    "SessionCookie": {"Value": "temporary-web-cookie"},
                }

                # 1. Unpatched behavior (force_api_key=False)
                stash_unpatched = StashInterface(conn, force_api_key=False)
                self.assertNotIn("ApiKey", stash_unpatched.s.headers)

                # 2. Patched behavior (force_api_key=True)
                stash_patched = StashInterface(conn, force_api_key=True)
                self.assertEqual(stash_patched.s.headers.get("ApiKey"), "perm-key-999")
                stash_patched.s.cookies.clear.assert_called()

    def test_transcoder_replacement_reconciliation_succeeds_with_authenticated_stash(self):
        """Verify that when authenticated, transcoder replacement reconciliation succeeds end-to-end."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "test.sqlite3"
            _init_db(db)

            conn = connect(db)
            conn.execute(
                "INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at) "
                "VALUES ('f1', '100', '/data/movie.mp4', 'movie.mp4', 1000, 1, '2026-01-01', '2026-01-01')"
            )
            conn.commit()
            conn.close()

            stash = MagicMock()
            stash.metadata_scan.return_value = "job-42"
            stash.wait_for_job.return_value = True
            stash.call_GQL.return_value = {
                "findScene": {
                    "files": [{"id": "f1", "path": "/data/movie-transcoded.mp4", "basename": "movie-transcoded.mp4"}]
                }
            }

            worker = librarymanager_monitor.MoveWorker(
                database_path=db, stash=stash, enabled=True,
                notifications=False, transcoder_compatibility=True
            )

            with patch.object(librarymanager_monitor, "tracked_move",
                              return_value=({"file_id": "f1", "scene_id": "100", "path": "/data/movie.mp4"}, "Transcode replacement")), \
                 patch.object(librarymanager_monitor, "destination_inventory_conflict", return_value=None), \
                 patch.object(librarymanager_monitor, "transcoder_replacement_decision",
                              return_value=("/data/movie-transcoded.mp4", "safe")), \
                 patch.object(librarymanager_monitor, "stash_destination_is_exclusive",
                              return_value=(True, "exclusive")), \
                 patch.object(librarymanager_monitor, "relocate_companions_transactionally", return_value=[]), \
                 patch.object(librarymanager_monitor, "resolve_associated_companion_moves", return_value=None):
                worker._process_move("/data/movie.mp4", "/data/movie-transcoded.mp4")

            # 1. Database file path updated to transcoded file
            conn = connect(db)
            row = conn.execute("SELECT path, basename FROM files WHERE file_id='f1'").fetchone()
            conn.close()
            self.assertEqual(row["path"], "/data/movie-transcoded.mp4")
            self.assertEqual(row["basename"], "movie-transcoded.mp4")

            # 2. Activity log has successful update, no failed records
            activities = recent_activity(db)
            actions = [a["action"] for a in activities]
            self.assertIn("targeted Stash scan", actions)
            failed_acts = [a for a in activities if a["status"] == "failed"]
            self.assertEqual(len(failed_acts), 0)


if __name__ == "__main__":
    unittest.main()

