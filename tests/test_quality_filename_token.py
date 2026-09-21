import json
import tempfile
import unittest
from pathlib import Path

from librarymanager_core import (
    _proposed_stem,
    _row_video_quality,
    _strip_matching_video_quality,
    _validated_video_quality,
    apply_scene_filename,
    flatten_scene_files,
    inventory,
    preview_scene_filename,
    scene_naming_signature,
)


class QualityFilenameTokenTests(unittest.TestCase):
    def test_01_quality_disabled_preserves_existing_output(self):
        self.assertEqual(
            _proposed_stem("Title", "Studio", ["Person"], {}, video_quality=1080),
            "Title - Studio - Person"
        )

    def test_02_quality_defaults_to_end(self):
        options = {"includeVideoQuality": True}
        self.assertEqual(
            _proposed_stem("Title", "Studio", ["Person"], options, video_quality=1080),
            "Title - Studio - Person - [1080p]"
        )

    def test_03_quality_can_be_placed_at_beginning(self):
        options = {"includeVideoQuality": True, "filenameQualityPosition": "beginning"}
        self.assertEqual(
            _proposed_stem("Title", "Studio", ["Person"], options, video_quality=1080),
            "[1080p] - Title - Studio - Person"
        )

    def test_04_missing_or_invalid_quality_is_cleanly_omitted(self):
        options = {"includeVideoQuality": True}
        self.assertEqual(_proposed_stem("Title", None, [], options, video_quality=None), "Title")
        self.assertEqual(_proposed_stem("Title", None, [], options, video_quality=0), "Title")
        self.assertEqual(_proposed_stem("Title", None, [], options, video_quality=-1080), "Title")
        self.assertEqual(_proposed_stem("Title", None, [], options, video_quality="invalid"), "Title")

    def test_05_validated_quality_formatting(self):
        self.assertEqual(_validated_video_quality(1080), "[1080p]")
        self.assertEqual(_validated_video_quality(2160), "[2160p]")
        self.assertEqual(_validated_video_quality(720), "[720p]")
        self.assertEqual(_validated_video_quality("1080"), "[1080p]")
        self.assertEqual(_validated_video_quality("[1080p]"), "[1080p]")
        self.assertEqual(_validated_video_quality("[1080P]"), "[1080p]")
        self.assertEqual(_validated_video_quality(None), "")
        self.assertEqual(_validated_video_quality(0), "")

    def test_06_date_and_quality_together(self):
        # Date beginning, Quality end
        opts_date_beg_qual_end = {"includeSceneDate": True, "filenameDatePosition": "beginning",
                                  "includeVideoQuality": True, "filenameQualityPosition": "end"}
        self.assertEqual(
            _proposed_stem("Title", "Studio", [], opts_date_beg_qual_end, scene_date="2026-09-14", video_quality=1080),
            "2026-09-14 - Title - Studio - [1080p]"
        )
        # Date end, Quality end
        opts_date_end_qual_end = {"includeSceneDate": True, "filenameDatePosition": "end",
                                  "includeVideoQuality": True, "filenameQualityPosition": "end"}
        self.assertEqual(
            _proposed_stem("Title", "Studio", [], opts_date_end_qual_end, scene_date="2026-09-14", video_quality=1080),
            "Title - Studio - 2026-09-14 - [1080p]"
        )
        # Date beginning, Quality beginning
        opts_date_beg_qual_beg = {"includeSceneDate": True, "filenameDatePosition": "beginning",
                                  "includeVideoQuality": True, "filenameQualityPosition": "beginning"}
        self.assertEqual(
            _proposed_stem("Title", "Studio", [], opts_date_beg_qual_beg, scene_date="2026-09-14", video_quality=1080),
            "2026-09-14 - [1080p] - Title - Studio"
        )

    def test_07_matching_quality_token_is_deduplicated(self):
        self.assertEqual(_strip_matching_video_quality("Title - [1080p]", 1080), "Title")
        self.assertEqual(_strip_matching_video_quality("Title - [1080P]", 1080), "Title")
        self.assertEqual(_strip_matching_video_quality("Title - (1080p)", 1080), "Title")
        self.assertEqual(_strip_matching_video_quality("Title - 1080p", 1080), "Title")
        self.assertEqual(
            _proposed_stem("Title - [1080p]", "Studio", [], {"includeVideoQuality": True}, video_quality=1080),
            "Title - Studio - [1080p]"
        )

    def test_08_managed_quality_change_replaces_old_quality(self):
        self.assertEqual(
            _proposed_stem("Title - [720p]", "Studio", [], {"includeVideoQuality": True},
                           video_quality=1080, previous_video_quality=720),
            "Title - Studio - [1080p]"
        )

    def test_09_flatten_scene_files_extracts_height(self):
        scene = {
            "id": "1",
            "title": "Test Scene",
            "files": [
                {"id": "10", "path": "/videos/scene.mp4", "basename": "scene.mp4", "height": 1080}
            ]
        }
        records = list(flatten_scene_files([scene]))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["height"], 1080)

    def test_10_preview_scene_filename_includes_quality(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "test.sqlite"
            video = Path(temp_dir) / "Original Scene.mp4"
            video.write_bytes(b"content")
            scene = {
                "id": "10",
                "title": "Sample Scene",
                "studio": {"name": "Sample Studio"},
                "performers": [{"id": 1, "name": "Performer One"}],
                "files": [{
                    "id": "100",
                    "path": str(video),
                    "basename": video.name,
                    "height": 1080,
                    "size": 7,
                    "fingerprints": []
                }],
            }
            inventory(database, [scene])
            preview = preview_scene_filename(database, "10", {"includeVideoQuality": True})
            self.assertEqual(preview["status"], "ready")
            self.assertEqual(preview["video_quality"], "[1080p]")
            self.assertEqual(Path(preview["proposed_path"]).name, "Sample Scene - Sample Studio - Performer One - [1080p].mp4")

    def test_11_apply_scene_filename_persists_managed_quality(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "test.sqlite"
            video = Path(temp_dir) / "Original Scene.mp4"
            video.write_bytes(b"content")
            scene = {
                "id": "10",
                "title": "Sample Scene",
                "files": [{
                    "id": "100",
                    "path": str(video),
                    "basename": video.name,
                    "height": 2160,
                    "size": 7,
                    "fingerprints": []
                }],
            }
            inventory(database, [scene])
            options = {"includeVideoQuality": True}
            result = apply_scene_filename(
                database, "10",
                lambda file_id, folder, basename: (video.rename(Path(folder) / basename), True)[1],
                options
            )
            self.assertEqual(result["status"], "renamed")
            self.assertEqual(result["video_quality"], "[2160p]")
            from librarymanager_core import connect
            conn = connect(database)
            row = conn.execute("SELECT managed_quality FROM filename_state WHERE file_id='100'").fetchone()
            conn.close()
            self.assertEqual(row["managed_quality"], "[2160p]")

    def test_12_scene_naming_signature_detects_quality_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "test.sqlite"
            video = Path(temp_dir) / "scene.mp4"
            video.write_bytes(b"content")
            scene_720 = {
                "id": "10",
                "title": "Scene",
                "files": [{
                    "id": "100",
                    "path": str(video),
                    "basename": video.name,
                    "height": 720,
                    "size": 7,
                    "fingerprints": []
                }],
            }
            inventory(database, [scene_720])
            sig_without_quality = scene_naming_signature(database, "10", include_quality=False)
            sig_720 = scene_naming_signature(database, "10", include_quality=True)
            self.assertIn("[720p]", sig_720)

            scene_1080 = {
                "id": "10",
                "title": "Scene",
                "files": [{
                    "id": "100",
                    "path": str(video),
                    "basename": video.name,
                    "height": 1080,
                    "size": 7,
                    "fingerprints": []
                }],
            }
            inventory(database, [scene_1080])
            sig_1080 = scene_naming_signature(database, "10", include_quality=True)
            self.assertIn("[1080p]", sig_1080)
            self.assertNotEqual(sig_720, sig_1080)
            self.assertEqual(sig_without_quality, scene_naming_signature(database, "10", include_quality=False))


    def test_13_regression_720p_real_scene_preview_and_protection_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "test.sqlite"
            video = Path(temp_dir) / "Onlyfans Cole Bentley Billy Essex.mp4"
            video.write_bytes(b"content")
            scene = {
                "id": "6393",
                "title": "",
                "files": [{
                    "id": "13154",
                    "path": str(video),
                    "basename": video.name,
                    "width": 404,
                    "height": 720,
                    "size": 192500752,
                    "fingerprints": []
                }],
            }
            inventory(database, [scene])
            preview_scene_filename(database, "6393")
            
            from librarymanager_core import connect
            conn = connect(database)
            conn.execute("UPDATE filename_state SET rename_protected=1 WHERE file_id='13154'")
            conn.commit()
            conn.close()

            options = {"includeVideoQuality": True, "filenameQualityPosition": "end"}

            # Normal background preview respects rename_protected
            preview_protected = preview_scene_filename(database, "6393", options, ignore_protection=False)
            self.assertEqual(preview_protected["status"], "unchanged")
            self.assertEqual(Path(preview_protected["proposed_path"]).name, "Onlyfans Cole Bentley Billy Essex.mp4")

            # UI Real-Scene preview with ignore_protection=True calculates the simulated proposal with [720p]
            preview_ui = preview_scene_filename(database, "6393", options, ignore_protection=True)
            self.assertEqual(preview_ui["status"], "ready")
            self.assertEqual(preview_ui["video_quality"], "[720p]")
            self.assertEqual(Path(preview_ui["proposed_path"]).name, "Onlyfans Cole Bentley Billy Essex - [720p].mp4")


    def test_14_preview_is_strictly_readonly_and_preserves_disk_and_protection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "test.sqlite"
            video = Path(temp_dir) / "Onlyfans Cole Bentley Billy Essex.mp4"
            video.write_bytes(b"sample video content")
            scene = {
                "id": "6393",
                "title": "",
                "files": [{
                    "id": "13154",
                    "path": str(video),
                    "basename": video.name,
                    "width": 404,
                    "height": 720,
                    "size": 20,
                    "fingerprints": []
                }],
            }
            inventory(database, [scene])
            preview_scene_filename(database, "6393")

            # Mark file as rename_protected (Automatic Filing preserve filename)
            from librarymanager_core import connect
            conn = connect(database)
            conn.execute("UPDATE filename_state SET rename_protected=1 WHERE file_id='13154'")
            conn.commit()
            conn.close()

            options = {"includeVideoQuality": True, "filenameQualityPosition": "end"}

            # 1. Preview operation (Test / Search) MUST be strictly read-only
            preview_res = preview_scene_filename(database, "6393", options, ignore_protection=True)
            self.assertEqual(preview_res["action_performed"], False)
            self.assertTrue(video.exists(), "Original file on disk must NOT be renamed by preview")
            proposed_path = Path(preview_res["proposed_path"])
            self.assertEqual(proposed_path.name, "Onlyfans Cole Bentley Billy Essex - [720p].mp4")
            self.assertFalse(proposed_path.exists(), "Proposed path must NOT exist on disk during read-only preview")

            # 2. Background automatic rename MUST NOT touch the protected file
            moves_called = []
            apply_res = apply_scene_filename(
                database, "6393",
                lambda file_id, folder, basename: moves_called.append((file_id, folder, basename)) or True,
                options,
                ignore_protection=False
            )
            self.assertEqual(apply_res["status"], "unchanged")
            self.assertEqual(len(moves_called), 0, "Move callback must NOT be called for protected file")
            self.assertTrue(video.exists(), "Original file remains untouched")


if __name__ == "__main__":
    unittest.main()
