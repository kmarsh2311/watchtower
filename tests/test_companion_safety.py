import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from librarymanager_monitor import find_scene_for_companion, match_companion_to_video, CompletedDownloadWorker


class CompanionLookupTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.execute("""
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY,
                scene_id TEXT NOT NULL,
                path TEXT NOT NULL,
                basename TEXT NOT NULL,
                exists_on_disk INTEGER NOT NULL DEFAULT 1
            )
        """)
        self.con.executemany(
            "INSERT INTO files(file_id,scene_id,path,basename,exists_on_disk) VALUES(?,?,?,?,1)",
            [
                ("f1", "s1", "/Library/A/1.m4v", "1.m4v"),
                ("f2", "s2", "/Library/A/SD-123.mp4", "SD-123.mp4"),
                ("f3", "s3", "/Library/A/VeryUniqueSceneName.m4v", "VeryUniqueSceneName.m4v"),
                ("f4", "s4", "/Folder/Scene.m4v", "Scene.m4v"),
                ("f5", "s5", "/Dupe/Scene.mp4", "Scene.mp4"),
                ("f6", "s6", "/Dupe/Scene.m4v", "Scene.m4v"),
                ("f7", "s7", "/Library/DupeA/Movie.m4v", "Movie.m4v"),
                ("f8", "s8", "/Library/DupeB/Movie.m4v", "Movie.m4v"),
            ],
        )

    def tearDown(self):
        self.con.close()

    def test_cross_directory_stem_only_never_matches(self):
        for path in ("/Downloads/1.png", "/Downloads/1.srt", "/Downloads/SD-123.srt", "/Downloads/VeryUniqueSceneName.srt"):
            row, _ = find_scene_for_companion(self.con, path)
            self.assertIsNone(row, path)

    def test_same_directory_exact_matches(self):
        row, _ = find_scene_for_companion(self.con, "/Folder/Scene.jpg")
        self.assertIsNotNone(row)
        self.assertEqual(row["scene_id"], "s4")

    def test_same_directory_duplicate_stem_is_ambiguous(self):
        row, _ = find_scene_for_companion(self.con, "/Dupe/Scene.jpg")
        self.assertIsNone(row)

    def test_cross_directory_compound_unique_matches(self):
        row, _ = find_scene_for_companion(self.con, "/Downloads/SD-123.mp4.jpg")
        self.assertIsNotNone(row)
        self.assertEqual(row["scene_id"], "s2")

    def test_cross_directory_compound_duplicate_is_ambiguous(self):
        row, _ = find_scene_for_companion(self.con, "/Downloads/Movie.m4v.jpg")
        self.assertIsNone(row)

    def test_language_remainder_is_local_only(self):
        row, rem = find_scene_for_companion(self.con, "/Folder/Scene.en.srt")
        self.assertIsNotNone(row)
        self.assertEqual(rem, ".en")
        row, _ = find_scene_for_companion(self.con, "/Downloads/Scene.en.srt")
        self.assertIsNone(row)


class DirectMatcherTests(unittest.TestCase):
    def test_no_fuzzy_prefix_guess(self):
        self.assertEqual(match_companion_to_video(Path("SceneABC.jpg"), Path("Scene.mp4")), (False, None))

    def test_exact_and_language_matches(self):
        self.assertEqual(match_companion_to_video(Path("Scene.jpg"), Path("Scene.mp4")), (True, ""))
        self.assertEqual(match_companion_to_video(Path("Scene.en.srt"), Path("Scene.mp4")), (True, ".en"))


class PairingIntegrationTests(unittest.TestCase):
    def test_pending_companion_in_other_folder_is_not_moved(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            folder_a = root / "A"
            folder_b = root / "B"
            folder_a.mkdir()
            folder_b.mkdir()
            companion = folder_a / "1.png"
            video = folder_b / "1.m4v"
            companion.write_bytes(b"image")
            video.write_bytes(b"video")

            worker = object.__new__(CompletedDownloadWorker)
            worker.lock = __import__("threading").RLock()
            worker.candidates = {str(companion): {}}
            worker.database_path = root / "test.sqlite"
            worker.stash = MagicMock()
            worker.notifications = False
            worker._save_state = MagicMock()

            worker._pair_companions_for_video(str(video), {"id": "s1", "files": [{"path": str(video)}]})
            self.assertTrue(companion.exists())
            self.assertFalse((folder_b / "1.png").exists())


if __name__ == "__main__":
    unittest.main()
