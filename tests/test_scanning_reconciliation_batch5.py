"""Focused tests for Batch 5:
Extraction of Filesystem Scanning and Reconciliation Logic into librarymanager.scanning.reconciliation.
"""
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import librarymanager.scanning.reconciliation as recon_mod
from librarymanager.database.connection import create_connection
from librarymanager.database.schema import SCHEMA
from librarymanager.scanning import (
    _prune_cached_reports as pkg_prune,
    clean_deleted_scenes_from_stash as pkg_clean,
    parse_cold_storage_roots as pkg_parse,
    reconcile_missing_files as pkg_recon_files,
    reconcile_missing_media_across_roots as pkg_recon_media,
)
import librarymanager_core as core_mod


def test_public_interface_and_compatibility():
    """Verify all public symbols are exposed both in the extracted module and in librarymanager_core."""
    funcs = [
        "reconcile_missing_files",
        "clean_deleted_scenes_from_stash",
        "reconcile_missing_media_across_roots",
        "_prune_cached_reports",
        "parse_cold_storage_roots",
    ]
    for fn in funcs:
        assert hasattr(recon_mod, fn), f"recon_mod missing {fn}"
        assert hasattr(core_mod, fn), f"core_mod missing {fn}"
        assert callable(getattr(recon_mod, fn))
        assert callable(getattr(core_mod, fn))

    # Package __init__ re-exports
    assert callable(pkg_recon_files)
    assert callable(pkg_clean)
    assert callable(pkg_recon_media)
    assert callable(pkg_prune)
    assert callable(pkg_parse)


def test_parse_cold_storage_roots():
    """Test parse_cold_storage_roots parsing logic across various formats."""
    for fn in (recon_mod.parse_cold_storage_roots, core_mod.parse_cold_storage_roots):
        assert fn(None) == []
        assert fn("") == []
        assert fn([]) == []
        assert fn(["/drive/a", "/drive/b"]) == ["/drive/a", "/drive/b"]
        assert fn('["/drive/a", "/drive/b"]') == ["/drive/a", "/drive/b"]
        assert fn("/drive/a, /drive/b") == ["/drive/a", "/drive/b"]
        assert fn("/drive/a\n/drive/b") == ["/drive/a", "/drive/b"]


def test_reconcile_missing_files_functional(tmp_path):
    """Test read-only candidate reconciliation via reconcile_missing_files."""
    db_path = tmp_path / "test_missing_files.sqlite3"
    conn = core_mod.connect(db_path)
    now = core_mod.utc_now()

    library_dir = tmp_path / "lib"
    library_dir.mkdir(parents=True, exist_ok=True)

    # Candidate file on disk
    cand = library_dir / "Scene1_Renamed.mp4"
    cand.write_bytes(b"A" * 200000)
    cand_size = cand.stat().st_size

    # Original missing record
    missing_path = library_dir / "Scene1_Original.mp4"
    conn.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f1', 's1', ?, 'Scene1_Original.mp4', ?, 0, ?, ?)""",
        (str(missing_path), cand_size, now, now),
    )
    conn.commit()
    conn.close()

    summary, report = recon_mod.reconcile_missing_files(db_path)
    assert summary["missing"] == 1
    assert summary["matched"] == 1
    assert len(report) == 1
    assert report[0]["file_id"] == "f1"
    assert report[0]["candidate_path"] == str(cand)


def test_clean_deleted_scenes_and_report_pruning(tmp_path):
    """Test clean_deleted_scenes_from_stash with custom root_dir report pruning."""
    db_path = tmp_path / "test_clean.sqlite3"
    conn = core_mod.connect(db_path)
    now = core_mod.utc_now()

    conn.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f_clean_dead', 's_clean_dead', '/nonexistent/file.mp4', 'file.mp4', 1000, 0, ?, ?)""",
        (now, now),
    )
    conn.commit()
    conn.close()

    # Create dummy reports in a custom root dir
    custom_root = tmp_path / "plugin_root"
    custom_root.mkdir()
    recon_json = custom_root / "reconciliation-report.json"
    plan_json = custom_root / "resolution-plan.json"

    recon_json.write_text(json.dumps({
        "summary": {"missing": 2},
        "matches": [
            {"file_id": "f_clean_dead", "scene_id": "s_clean_dead"},
            {"file_id": "f_other", "scene_id": "s_other"},
        ],
    }))
    plan_json.write_text(json.dumps({
        "items": [
            {"stale_file_id": "f_clean_dead", "stale_scene_id": "s_clean_dead"},
            {"stale_file_id": "f_other", "stale_scene_id": "s_other"},
        ],
    }))

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {"findScenes": {"scenes": []}}

    res = core_mod.clean_deleted_scenes_from_stash(db_path, stash=mock_stash, root_dir=custom_root)
    assert res["cleaned_count"] == 1
    assert res["remaining_missing"] == 0

    updated_recon = json.loads(recon_json.read_text())
    updated_plan = json.loads(plan_json.read_text())

    assert len(updated_recon["matches"]) == 1
    assert updated_recon["matches"][0]["file_id"] == "f_other"
    assert len(updated_plan["items"]) == 1
    assert updated_plan["items"][0]["stale_file_id"] == "f_other"


def test_reconcile_missing_media_across_roots_reconnection(tmp_path):
    """Test reconcile_missing_media_across_roots moves companion and reconnects moved media."""
    db_path = tmp_path / "test_reconcile_media.sqlite3"
    conn = core_mod.connect(db_path)
    now = core_mod.utc_now()

    lib_root = tmp_path / "library"
    folder_a = lib_root / "FolderA"
    folder_b = lib_root / "FolderB"
    folder_a.mkdir(parents=True)
    folder_b.mkdir(parents=True)

    # Moved video in folder_b
    video_file = folder_b / "Video1.mp4"
    video_file.write_bytes(b"content-of-video-1" * 100)
    v_size = video_file.stat().st_size

    # Companion left in folder_a
    companion = folder_a / "Video1.srt"
    companion.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello")

    # Record in db pointing to old path in folder_a
    old_video_path = folder_a / "Video1.mp4"
    conn.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f_media_1', 's_media_1', ?, 'Video1.mp4', ?, 0, ?, ?)""",
        (str(old_video_path), v_size, now, now),
    )
    conn.commit()
    conn.close()

    mock_stash = MagicMock()
    mock_stash.metadata_scan.return_value = "scan_job_99"
    mock_stash.wait_for_job.return_value = True

    res = core_mod.reconcile_missing_media_across_roots(
        db_path, stash=mock_stash, roots=[str(lib_root)], config={}
    )
    assert res["status"] == "ok"
    assert res["reconciled_count"] == 1
    assert res["reconciled"][0]["new_path"] == str(video_file)

    # Verify companion was relocated alongside the moved video
    expected_companion = folder_b / "Video1.srt"
    assert expected_companion.is_file(), "Companion file was not moved alongside video"
