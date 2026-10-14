import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import librarymanager_core
import librarymanager.services
from librarymanager.services import contact_sheets


class TestContactSheetsService(unittest.TestCase):
    def test_format_csm_duration(self):
        self.assertEqual(contact_sheets.format_csm_duration(0), "00:00")
        self.assertEqual(contact_sheets.format_csm_duration(45), "00:45")
        self.assertEqual(contact_sheets.format_csm_duration(65), "01:05")
        self.assertEqual(contact_sheets.format_csm_duration(3600), "01:00:00")
        self.assertEqual(contact_sheets.format_csm_duration(3665), "01:01:05")
        self.assertEqual(contact_sheets.format_csm_duration(7200.8), "02:00:00")

    def test_find_csm_font_returns_string(self):
        font = contact_sheets.find_csm_font()
        self.assertIsInstance(font, str)
        self.assertTrue(len(font) > 0)

    def test_bundled_font_exists(self):
        font_path = contact_sheets._get_bundled_font_path()
        self.assertTrue(font_path.is_file(), f"Bundled font missing: {font_path}")
        self.assertEqual(font_path.name, "Roboto-Regular.ttf")

    def test_ffmpeg_supports_drawtext(self):
        with patch("librarymanager.services.contact_sheets._subprocess_run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Filter drawtext", stderr="")
            self.assertTrue(contact_sheets.ffmpeg_supports_drawtext("ffmpeg"))

            mock_run.return_value = MagicMock(returncode=0, stdout="No filters found", stderr="")
            self.assertFalse(contact_sheets.ffmpeg_supports_drawtext("ffmpeg"))

            mock_run.side_effect = OSError("ffmpeg not found")
            self.assertFalse(contact_sheets.ffmpeg_supports_drawtext("ffmpeg"))

    def test_generate_video_contact_sheet_missing_video(self):
        result = contact_sheets.generate_video_contact_sheet("/nonexistent/video.mp4")
        self.assertEqual(result["status"], "error")
        self.assertIn("not found", result["error"])

    def test_generate_video_contact_sheet_existing_dest_no_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "video.mp4"
            video.write_bytes(b"data")
            sheet = root / "video.mp4.jpg"
            sheet.write_bytes(b"image")

            result = contact_sheets.generate_video_contact_sheet(video, overwrite=False)
            self.assertEqual(result["status"], "skipped")
            self.assertIn("already exists", result["message"])

    def test_generate_video_contact_sheet_too_short(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "video.mp4"
            video.write_bytes(b"data")
            probe = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "streams": [{"width": 1920, "height": 1080, "duration": "5.0"}],
                    "format": {"duration": "5.0", "size": "1000"},
                }),
            )

            with patch("librarymanager.services.contact_sheets._shutil_which", return_value="/usr/bin/ffmpeg"), \
                 patch("librarymanager.services.contact_sheets._os_path_exists", return_value=True), \
                 patch("librarymanager.services.contact_sheets._subprocess_run", return_value=probe):
                result = contact_sheets.generate_video_contact_sheet(video, overwrite=True)
                self.assertEqual(result["status"], "skipped")
                self.assertIn("too short", result["message"])

    def test_wrapper_parity_and_markers(self):
        self.assertTrue(getattr(librarymanager_core.find_csm_font, "_is_watchtower_wrapper", False))
        self.assertTrue(getattr(librarymanager_core.format_csm_duration, "_is_watchtower_wrapper", False))
        self.assertTrue(getattr(librarymanager_core.ffmpeg_supports_drawtext, "_is_watchtower_wrapper", False))
        self.assertTrue(getattr(librarymanager_core.generate_ffmpeg_contact_sheet_fallback, "_is_watchtower_wrapper", False))
        self.assertTrue(getattr(librarymanager_core.generate_video_contact_sheet, "_is_watchtower_wrapper", False))

        # Check call-through via wrapper
        self.assertEqual(librarymanager_core.format_csm_duration(120), "02:00")

    def test_reexports_in_services_package(self):
        self.assertIs(librarymanager.services.find_csm_font, contact_sheets.find_csm_font)
        self.assertIs(librarymanager.services.format_csm_duration, contact_sheets.format_csm_duration)
        self.assertIs(librarymanager.services.ffmpeg_supports_drawtext, contact_sheets.ffmpeg_supports_drawtext)
        self.assertIs(librarymanager.services.generate_ffmpeg_contact_sheet_fallback, contact_sheets.generate_ffmpeg_contact_sheet_fallback)
        self.assertIs(librarymanager.services.generate_video_contact_sheet, contact_sheets.generate_video_contact_sheet)
