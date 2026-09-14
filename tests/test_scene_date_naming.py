import tempfile
import unittest
from pathlib import Path

from librarymanager_core import (
    _metadata_name_pattern,
    _proposed_stem,
    _strip_matching_scene_date,
    _validated_scene_date,
    apply_scene_filename,
    inventory,
    preview_scene_filename,
    scene_naming_signature,
)


class SceneDateNamingTests(unittest.TestCase):
    def test_01_connector_regex_uses_real_word_boundaries(self):
        pattern = _metadata_name_pattern("Jane Doe", include_connectors=True)
        self.assertIsNotNone(pattern)
        self.assertNotIn("\x08", pattern)

    def test_02_connector_regex_does_not_match_inside_words(self):
        import re
        pattern = _metadata_name_pattern("Jane", include_connectors=True)
        self.assertIsNone(re.search(pattern, "candyJane", flags=re.IGNORECASE))

    def test_03_date_disabled_preserves_existing_output(self):
        self.assertEqual(_proposed_stem("Title", "Studio", ["Person"], {}, "2026-09-14"),
                         "Title - Studio - Person")

    def test_04_date_defaults_to_beginning(self):
        self.assertEqual(_proposed_stem("Title", None, [], {"includeSceneDate": True}, "2026-09-14"),
                         "2026-09-14 - Title")

    def test_05_date_can_be_placed_at_end(self):
        options = {"includeSceneDate": True, "filenameDatePosition": "end"}
        self.assertEqual(_proposed_stem("Title", None, [], options, "2026-09-14"),
                         "Title - 2026-09-14")

    def test_06_missing_date_is_cleanly_omitted(self):
        self.assertEqual(_proposed_stem("Title", None, [], {"includeSceneDate": True}, None), "Title")

    def test_07_invalid_date_is_cleanly_omitted(self):
        self.assertEqual(_proposed_stem("Title", None, [], {"includeSceneDate": True}, "2026-99-99"), "Title")

    def test_08_valid_leap_day_is_accepted(self):
        self.assertEqual(_validated_scene_date("2024-02-29"), "2024-02-29")

    def test_09_invalid_leap_day_is_rejected(self):
        self.assertEqual(_validated_scene_date("2025-02-29"), "")

    def test_10_matching_iso_prefix_is_deduplicated(self):
        self.assertEqual(_strip_matching_scene_date("2026-09-14 - Title", "2026-09-14"), "Title")

    def test_11_matching_iso_suffix_is_deduplicated(self):
        self.assertEqual(_strip_matching_scene_date("Title - 2026-09-14", "2026-09-14"), "Title")

    def test_12_matching_day_month_year_is_recognised(self):
        self.assertEqual(_strip_matching_scene_date("14-09-2026 Title", "2026-09-14"), "Title")

    def test_13_matching_month_day_year_is_recognised(self):
        self.assertEqual(_strip_matching_scene_date("09-14-2026 Title", "2026-09-14"), "Title")

    def test_14_matching_year_day_month_is_recognised(self):
        self.assertEqual(_strip_matching_scene_date("2026-14-09 Title", "2026-09-14"), "Title")

    def test_15_matching_dot_separators_are_recognised(self):
        self.assertEqual(_strip_matching_scene_date("14.09.2026 - Title", "2026-09-14"), "Title")

    def test_16_matching_underscore_separators_are_recognised(self):
        self.assertEqual(_strip_matching_scene_date("Title_2026_09_14", "2026-09-14"), "Title")

    def test_17_different_date_is_preserved(self):
        self.assertEqual(_strip_matching_scene_date("2026-09-13 - Title", "2026-09-14"),
                         "2026-09-13 - Title")

    def test_18_matching_date_in_middle_is_preserved(self):
        self.assertEqual(_strip_matching_scene_date("Part 2026-09-14 Two", "2026-09-14"),
                         "Part 2026-09-14 Two")
        self.assertEqual(_strip_matching_scene_date("2026-09-14Title", "2026-09-14"), "2026-09-14Title")
        self.assertEqual(_strip_matching_scene_date("Title2026-09-14", "2026-09-14"), "Title2026-09-14")

    def test_19_standalone_year_is_preserved(self):
        self.assertEqual(_strip_matching_scene_date("2026 - Title", "2026-09-14"), "2026 - Title")

    def test_20_invalid_position_fails_to_beginning(self):
        options = {"includeSceneDate": True, "filenameDatePosition": "middle"}
        self.assertEqual(_proposed_stem("Title", None, [], options, "2026-09-14"),
                         "2026-09-14 - Title")

    def test_21_existing_component_order_is_preserved(self):
        options = {"includeSceneDate": True, "filenameOrder": "performers,title,studio"}
        self.assertEqual(_proposed_stem("Title", "Studio", ["Person"], options, "2026-09-14"),
                         "2026-09-14 - Person - Title - Studio")

    def test_22_selected_section_separator_applies_to_date(self):
        options = {"includeSceneDate": True, "filenameSectionSeparator": "underscore"}
        self.assertEqual(_proposed_stem("Title", None, [], options, "2026-09-14"), "2026-09-14_Title")

    def test_23_preview_uses_inventory_scene_date_and_avoids_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Original.mp4"
            video.write_bytes(b"video")
            database = root / "inventory.sqlite3"
            inventory(database, [{"id": "10", "title": "14-09-2026 - Title", "date": "2026-09-14",
                                  "studio": None, "performers": [],
                                  "files": [{"id": "20", "path": str(video), "size": 5}]}])
            preview = preview_scene_filename(database, "10", {"includeSceneDate": True})
            self.assertTrue(preview["proposed_path"].endswith("2026-09-14 - Title.mp4"))
            self.assertEqual(
                _proposed_stem("14-09-2026 - Title", None, [], {"includeSceneDate": True},
                               "2026-09-15", "2026-09-14"),
                "2026-09-15 - Title",
            )

    def test_24_date_participates_in_naming_signature_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Original.mp4"
            video.write_bytes(b"video")
            database = root / "inventory.sqlite3"
            base = {"id": "10", "title": "Title", "studio": None, "performers": [],
                    "files": [{"id": "20", "path": str(video), "size": 5}]}
            inventory(database, [{**base, "date": "2026-09-14"}])
            without_date = scene_naming_signature(database, "10")
            with_date = scene_naming_signature(database, "10", True)
            self.assertEqual(len(without_date), 3)
            self.assertEqual(with_date[-1], "2026-09-14")

    def test_25_date_rename_moves_companion_and_collision_still_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "Title.mp4"
            companion = root / "Title.srt"
            video.write_bytes(b"video")
            companion.write_text("subtitle")
            database = root / "inventory.sqlite3"
            inventory(database, [{"id": "10", "title": "Title", "date": "2026-09-14",
                                  "studio": None, "performers": [],
                                  "files": [{"id": "20", "path": str(video), "size": 5}]}])
            options = {"includeSceneDate": True}
            blocked_target = root / "2026-09-14 - Title.srt"
            blocked_target.write_text("occupied")
            self.assertEqual(preview_scene_filename(database, "10", options)["status"], "blocked")
            blocked_target.unlink()

            def move_file(_file_id, _folder, basename):
                video.rename(root / basename)
                return True

            result = apply_scene_filename(database, "10", move_file, options)
            self.assertEqual(result["status"], "renamed")
            self.assertTrue((root / "2026-09-14 - Title.mp4").exists())
            self.assertTrue((root / "2026-09-14 - Title.srt").exists())


if __name__ == "__main__":
    unittest.main()
