"""Unit tests for librarymanager.services.manual_sync and backward-compatible wrappers."""
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
import pytest

import librarymanager.services.manual_sync as ms
from librarymanager.database import create_connection, ensure_schema
import librarymanager_core as core


@pytest.fixture
def test_db(tmp_path: Path) -> Path:
    db_file = tmp_path / "test_manual_sync.db"
    conn = ms.connect(db_file)
    conn.close()
    return db_file


def test_incoming_scan_flags():
    assert ms.INCOMING_SCAN_FLAGS["scanGenerateCovers"] is True
    assert ms.INCOMING_SCAN_FLAGS["scanGeneratePreviews"] is True
    assert ms.INCOMING_SCAN_FLAGS["scanGenerateSprites"] is True
    assert ms.INCOMING_SCAN_FLAGS["scanGeneratePhashes"] is True
    assert ms.INCOMING_SCAN_FLAGS["scanGenerateThumbnails"] is True
    assert core.INCOMING_SCAN_FLAGS == ms.INCOMING_SCAN_FLAGS


def test_get_unindexed_external_media_cleans_missing_files(test_db: Path, tmp_path: Path):
    real_video = tmp_path / "real_video.mp4"
    real_video.write_bytes(b"DATA" * 20)
    missing_video = tmp_path / "missing_video.mp4"

    conn = ms.connect(test_db)
    conn.execute(
        "INSERT INTO unindexed_external_media (path, basename, size, discovered_at, ignored) VALUES (?, ?, ?, ?, 0)",
        (str(real_video), real_video.name, 80, "2026-10-14T12:00:00Z")
    )
    conn.execute(
        "INSERT INTO unindexed_external_media (path, basename, size, discovered_at, ignored) VALUES (?, ?, ?, ?, 0)",
        (str(missing_video), missing_video.name, 100, "2026-10-14T12:00:00Z")
    )
    conn.commit()
    conn.close()

    items = ms.get_unindexed_external_media(test_db)
    assert len(items) == 1
    assert items[0]["path"] == str(real_video)

    # Missing video should have been cleaned up from DB
    conn = ms.connect(test_db)
    rows = conn.execute("SELECT * FROM unindexed_external_media WHERE path = ?", (str(missing_video),)).fetchall()
    conn.close()
    assert len(rows) == 0


def test_ignore_and_reset_unindexed_media(test_db: Path, tmp_path: Path):
    video1 = tmp_path / "clip1.mp4"
    video2 = tmp_path / "clip2.mp4"
    video1.write_bytes(b"clip1")
    video2.write_bytes(b"clip2")

    conn = ms.connect(test_db)
    conn.execute(
        "INSERT INTO unindexed_external_media (path, basename, size, discovered_at, ignored) VALUES (?, ?, ?, ?, 0)",
        (str(video1), "clip1.mp4", 5, "2026-10-14T12:00:00Z")
    )
    conn.execute(
        "INSERT INTO unindexed_external_media (path, basename, size, discovered_at, ignored) VALUES (?, ?, ?, ?, 0)",
        (str(video2), "clip2.mp4", 5, "2026-10-14T12:00:00Z")
    )
    conn.commit()
    conn.close()

    assert len(ms.get_unindexed_external_media(test_db)) == 2

    # Ignore video1
    res = ms.ignore_unindexed_media_file(test_db, str(video1))
    assert res["status"] == "ok"
    assert len(ms.get_unindexed_external_media(test_db)) == 1

    # Ignore all
    res = ms.ignore_all_unindexed_media_files(test_db)
    assert res["status"] == "ok"
    assert len(ms.get_unindexed_external_media(test_db)) == 0

    # Reset ignored
    reset_res = ms.reset_ignored_unindexed_media_files(test_db)
    assert reset_res["status"] == "ok"
    assert reset_res["count"] == 2
    assert len(ms.get_unindexed_external_media(test_db)) == 2


def test_add_unindexed_media_file_not_found(test_db: Path):
    res = ms.add_unindexed_media_file(test_db, None, "/path/does/not/exist.mp4")
    assert res["status"] == "error"
    assert "not found on disk" in res["message"]


def test_add_unindexed_media_file_with_mock_stash(test_db: Path, tmp_path: Path):
    video = tmp_path / "imported_scene.mp4"
    video.write_bytes(b"SAMPLE_VIDEO" * 50)

    # Insert into unindexed
    conn = ms.connect(test_db)
    conn.execute(
        "INSERT INTO unindexed_external_media (path, basename, size, discovered_at, ignored) VALUES (?, ?, ?, ?, 0)",
        (str(video), video.name, 600, "2026-10-14T12:00:00Z")
    )
    conn.commit()
    conn.close()

    mock_stash = MagicMock()
    mock_stash.metadata_scan.return_value = "job-scan-123"
    mock_stash.wait_for_job.return_value = True

    def mock_gql(query, variables=None):
        if "FindSceneByPath" in query or "findScenes" in query:
            return {
                "findScenes": {
                    "scenes": [{
                        "id": "999",
                        "title": "Imported Scene",
                        "files": [{"id": "f999", "path": str(video), "basename": video.name, "size": 600, "duration": 120.0, "fingerprints": []}]
                    }]
                }
            }
        elif "metadataGenerate" in query:
            return {"metadataGenerate": "job-gen-999"}
        return {}

    mock_stash.call_GQL.side_effect = mock_gql

    res = ms.add_unindexed_media_file(test_db, mock_stash, str(video), config={"macNotifications": False, "generateContactSheets": False})
    assert res["status"] == "ok"
    assert res["scene_id"] == "999"

    # Should no longer be in unindexed_external_media
    assert len(ms.get_unindexed_external_media(test_db)) == 0


def test_add_all_unindexed_media_files_empty(test_db: Path):
    res = ms.add_all_unindexed_media_files(test_db, None)
    assert res["status"] == "ok"
    assert res["count"] == 0


def test_notify_desktop_safe_execution():
    # Should not raise exception
    ms.notify_desktop("Test message", title="Test Title")


def test_compatibility_wrappers(test_db: Path, tmp_path: Path):
    video = tmp_path / "compat_clip.mp4"
    video.write_bytes(b"COMPAT")

    conn = ms.connect(test_db)
    conn.execute(
        "INSERT INTO unindexed_external_media (path, basename, size, discovered_at, ignored) VALUES (?, ?, ?, ?, 0)",
        (str(video), video.name, 6, "2026-10-14T12:00:00Z")
    )
    conn.commit()
    conn.close()

    # Test core wrappers match
    assert len(core.get_unindexed_external_media(test_db)) == 1
    assert core.ignore_unindexed_media_file(test_db, str(video))["status"] == "ok"
    assert len(core.get_unindexed_external_media(test_db)) == 0
    assert core.reset_ignored_unindexed_media_files(test_db)["count"] == 1
    assert len(core.get_unindexed_external_media(test_db)) == 1

    # Verify wrapper attribute
    assert getattr(core.notify_desktop, "_is_watchtower_wrapper", False) is True
