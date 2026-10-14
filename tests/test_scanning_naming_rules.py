"""Unit and regression tests for librarymanager.scanning.naming_rules (Batch 4)."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import librarymanager.scanning.naming_rules as nr
import librarymanager_core as core_mod


class ScanningNamingRulesTests(unittest.TestCase):
    def test_01_imports_from_scanning_subsystem(self):
        """Verify module can be imported both directly and through librarymanager.scanning."""
        import librarymanager.scanning as scanning
        self.assertTrue(hasattr(scanning, "_proposed_stem"))
        self.assertTrue(hasattr(scanning, "preview_scene_filename"))
        self.assertTrue(hasattr(scanning, "filename_format_options"))
        self.assertTrue(hasattr(scanning, "_sanitize_filename_stem"))

    def test_02_pure_normalization_and_matching(self):
        """Verify accent, space, punctuation and case normalization."""
        self.assertEqual(nr._normalized_filename_text("  Studio - 01!  "), "studio 01")
        self.assertEqual(nr._compact_filename_text("  Studio - 01!  "), "studio01")
        self.assertTrue(nr._same_filename_name("Evil Angel", "Evil-Angel"))
        self.assertTrue(nr._same_filename_name("Studio 2000", "Studio2000"))
        self.assertFalse(nr._same_filename_name("Studio 1", "Studio 2"))
        self.assertTrue(nr._contains_name("A Scene with Jane Doe Today", "Jane Doe"))
        self.assertTrue(nr._contains_name("Scene_JaneDoe_HD", "Jane Doe"))

    def test_03_stem_sanitization_removes_illegal_filesystem_characters(self):
        """Verify illegal path characters (slash, colon, quotes, null bytes) are replaced or stripped."""
        raw = 'Scene: Subtitle / Part 1 *special* ? "quote" <tag> | pipe \x00.'
        cleaned = nr._sanitize_filename_stem(raw)
        self.assertNotIn(":", cleaned)
        self.assertNotIn("/", cleaned)
        self.assertNotIn("*", cleaned)
        self.assertNotIn("?", cleaned)
        self.assertNotIn('"', cleaned)
        self.assertNotIn("<", cleaned)
        self.assertNotIn(">", cleaned)
        self.assertNotIn("|", cleaned)
        self.assertNotIn("\x00", cleaned)
        self.assertFalse(cleaned.endswith("."))

    def test_04_metadata_stripping_and_pairing_protection(self):
        """Verify studio/performer stripping and creative pairing title protection."""
        # Unscraped or regular title with hyphens
        options = {"stripMetadataFromTitle": True}
        row_unscraped = {"scene_metadata_json": "{}"}
        self.assertTrue(nr._should_strip_metadata_from_title(options, row_unscraped, "Brazzers - Scene 1"))

        # Officially scraped creative pairing without release delimiters should NOT be stripped
        row_scraped = {"scene_metadata_json": json.dumps({"stash_ids": ["test-id"]})}
        self.assertFalse(nr._should_strip_metadata_from_title(options, row_scraped, "Jade and Xander"))
        self.assertFalse(nr._should_strip_metadata_from_title(options, row_scraped, "Alice with Bob"))
        # But if it has hyphens/release delimiters, it IS stripped
        self.assertTrue(nr._should_strip_metadata_from_title(options, row_scraped, "Jade and Xander - Scene 1"))

    def test_05_quality_and_date_variants_stripping(self):
        """Verify exact date and resolution boundary deduplication."""
        text = "2026-09-14 - Big Scene - [1080p]"
        stripped_date = nr._strip_matching_scene_date(text, "2026-09-14")
        self.assertEqual(stripped_date, "Big Scene - [1080p]")

        stripped_qual = nr._strip_matching_video_quality(stripped_date, 1080)
        self.assertEqual(stripped_qual, "Big Scene")

        # Tolerates alternate date formats
        variants = nr._matching_scene_date_variants("2026-09-14")
        self.assertIn("2026-09-14", variants)
        self.assertIn("14-09-2026", variants)
        self.assertIn("2026.09.14", variants)

    def test_06_proposed_stem_formatting_options(self):
        """Verify proposed stem ordering, date positions, and separator permutations."""
        options = {
            "includeSceneDate": True,
            "filenameDatePosition": "beginning",
            "includeVideoQuality": True,
            "filenameQualityPosition": "end",
            "filenameOrder": "studio,title,performers",
            "filenameSectionSeparator": "dash",
            "filenamePerformerSeparator": "ampersand",
        }
        stem = nr._proposed_stem(
            base="My Title",
            studio="Acme",
            performers=["Alice", "Bob"],
            options=options,
            scene_date="2026-10-14",
            video_quality=1080,
        )
        self.assertEqual(stem, "2026-10-14 - Acme - My Title - Alice & Bob - [1080p]")

    def test_07_filename_state_lifecycle_in_database(self):
        """Verify _create_filename_state and _sync_filename_state database persistence."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test_naming.sqlite3"
            conn = nr.connect(db_path)
            now = nr.utc_now()
            file_id = "f_name_1"
            scene_id = "s_name_1"
            vpath = Path(td) / "Original_Video.mp4"
            vpath.write_bytes(b"DATA")

            conn.execute(
                """INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk,
                                      title, studio, performers_json, scene_metadata_json, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, 'Original_Video.mp4', 4, 1, 'My Special Scene', 'MyStudio',
                           '[\"Performer A\"]', '{}', ?, ?)""",
                (file_id, scene_id, str(vpath), now, now),
            )
            conn.commit()

            row = conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
            state = nr._create_filename_state(conn, row, vpath, ["Performer A"])
            conn.commit()

            self.assertIsNotNone(state)
            self.assertEqual(state["file_id"], file_id)
            self.assertEqual(state["base_stem"], "My Special Scene")

            # Preview scene filename
            preview = nr.preview_scene_filename(db_path, scene_id)
            self.assertEqual(preview["status"], "ready")
            self.assertIn("MyStudio", preview["proposed_path"])
            self.assertIn("Performer A", preview["proposed_path"])

            # Sync filename state with updated metadata
            conn.execute("UPDATE files SET title='Updated Scene Title' WHERE file_id=?", (file_id,))
            conn.commit()
            updated_row = conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
            synced_state = nr._sync_filename_state(conn, state, updated_row, vpath, ["Performer A"])
            conn.commit()

            self.assertEqual(synced_state["base_stem"], "Updated Scene Title")
            conn.close()

    def test_08_core_forwarding_shims_match_canonical(self):
        """Verify librarymanager_core functions delegate cleanly to librarymanager.scanning.naming_rules."""
        self.assertEqual(
            core_mod._normalized_filename_text("Test_Title"),
            nr._normalized_filename_text("Test_Title"),
        )
        self.assertEqual(
            core_mod._validated_video_quality(2160),
            nr._validated_video_quality(2160),
        )
        self.assertEqual(
            core_mod._proposed_stem("Base", "Studio", ["Perf"], {}),
            nr._proposed_stem("Base", "Studio", ["Perf"], {}),
        )
