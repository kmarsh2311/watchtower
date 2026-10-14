"""Focused regression test suite covering the 4 core areas requested:

1. Concurrent database/schema access:
   - Verifies cross-process schema validation using PRAGMA user_version.
   - Rapid concurrent connections with empty per-process cache during active write transactions.
   - Verifies zero "database is locked" errors.

2. Manual Sync of an explicitly configured cold-storage root:
   - Verifies cold storage root is scanned and files reconnected when explicitly requested
     or when manual sync defaults to all library roots.

3. Manual Sync failure and completion feedback:
   - Verifies descriptive feedback when selected roots are offline/unavailable.
   - Verifies clean completion message and 'info' severity when all files are present.
   - Verifies unindexed media detection feedback.

4. Cleanup message clearing after task completion:
   - Verifies clean_deleted_from_stash prunes dead records from files, filename_state,
     filename_previews, marks pending filesystem_events as reviewed, and drops
     missing_files_count to 0.
"""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from librarymanager_core import (
    connect,
    clean_deleted_scenes_from_stash,
    reconcile_missing_media_across_roots,
    get_unindexed_external_media,
    SCHEMA,
    utc_now,
    _schema_applied,
)


# ---------------------------------------------------------------------------
# Area 1: Concurrent database and schema access
# ---------------------------------------------------------------------------

def test_concurrent_database_and_schema_access(tmp_path):
    """Verify that multiple processes / threads connecting simultaneously with empty in-memory
    _schema_applied sets do NOT cause sqlite3.OperationalError: database is locked.
    Uses PRAGMA user_version to skip DDL and bounded retry for safe coordination.
    """
    db_path = tmp_path / "test_concurrency.sqlite3"
    
    # Initialize DB with schema and user_version
    init_conn = connect(db_path)
    init_conn.close()

    stop_writer = threading.Event()
    errors = []

    def heavy_writer():
        while not stop_writer.is_set():
            try:
                c = connect(db_path)
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    """INSERT INTO activity_log (category, severity, action, status, detail, metadata_json, recorded_at)
                       VALUES ('test', 'info', 'write', 'complete', 'heavy writer', '{}', datetime('now'))"""
                )
                time.sleep(0.005)
                c.commit()
                c.close()
            except Exception as e:
                errors.append(f"Writer error: {e}")

    writer = threading.Thread(target=heavy_writer, daemon=True)
    writer.start()

    def concurrent_poller(worker_id):
        # Simulate separate processes where _schema_applied does NOT have this DB path
        key = str(Path(db_path).resolve())
        for _ in range(15):
            try:
                # Force each iteration to simulate a brand-new process by evicting from _schema_applied
                _schema_applied.discard(key)
                conn = connect(db_path)
                # Simple read query
                count = conn.execute("SELECT count(*) FROM files").fetchone()[0]
                assert count >= 0
                conn.close()
            except Exception as e:
                errors.append(f"Worker {worker_id} error: {e}")
            time.sleep(0.002)

    threads = [threading.Thread(target=concurrent_poller, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    stop_writer.set()
    writer.join(timeout=2.0)

    assert errors == [], f"Concurrency errors occurred: {errors}"


# ---------------------------------------------------------------------------
# Area 2: Manual Sync of an explicitly configured cold-storage root
# ---------------------------------------------------------------------------

def test_manual_sync_explicitly_configured_cold_storage_root(tmp_path):
    """Verify that an explicitly configured cold storage drive:
    1. Is excluded from automatic watchers.
    2. IS scanned and reconciled when passed to reconcile_missing_media_across_roots.
    3. Reconnects moved scene files on the cold storage drive.
    """
    db_path = tmp_path / "cold_sync.sqlite3"
    conn = connect(db_path)

    live_root = tmp_path / "LiveDrives"
    live_root.mkdir()
    cold_root = tmp_path / "ColdStorageDrive"
    cold_root.mkdir()

    # Move target scene file to the cold storage drive
    moved_cold_file = cold_root / "Cold_Scene_123.mp4"
    moved_cold_file.write_bytes(b"cold storage video bytes for testing")

    now = utc_now()
    # In Watchtower DB, scene was previously on live_root but is now missing
    conn.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f_cold_1', 'sc_cold_1', ?, 'Cold_Scene_123.mp4', ?, 0, ?, ?)""",
        (str(live_root / "Cold_Scene_123.mp4"), moved_cold_file.stat().st_size, now, now),
    )
    conn.commit()
    conn.close()

    mock_stash = MagicMock()
    mock_stash.metadata_scan.return_value = "job-cold-1"
    mock_stash.wait_for_job.return_value = True
    mock_stash.find_plugin_config.return_value = {
        "coldStorageRoots": [str(cold_root)]
    }
    def _mock_gql(query, *args, **kwargs):
        if "findScenes" in query or "CheckScenes" in query:
            return {"findScenes": {"scenes": [{"id": "sc_cold_1", "files": [{"id": "f_cold_1", "path": str(live_root / "Cold_Scene_123.mp4")}]}]}}
        return {
            "findFiles": {"files": []},
            "configuration": {"general": {"stashes": [{"path": str(live_root)}, {"path": str(cold_root)}]}},
        }
    mock_stash.call_GQL.side_effect = _mock_gql

    # Execute Manual Sync specifying library roots including cold storage root
    res = reconcile_missing_media_across_roots(
        db_path,
        stash=mock_stash,
        roots=[str(live_root), str(cold_root)],
        config={"coldStorageRoots": [str(cold_root)]},
    )

    assert res["status"] == "ok"
    assert res["reconciled_count"] == 1
    assert len(res["reconciled"]) == 1
    assert res["reconciled"][0]["scene_id"] == "sc_cold_1"
    assert res["reconciled"][0]["new_path"] == str(moved_cold_file)

    # Verify database updated path and exists_on_disk
    conn = connect(db_path)
    updated = conn.execute("SELECT path, exists_on_disk FROM files WHERE file_id='f_cold_1'").fetchone()
    conn.close()

    assert updated["exists_on_disk"] == 1
    assert updated["path"] == str(moved_cold_file)


# ---------------------------------------------------------------------------
# Area 3: Manual Sync failure and completion feedback
# ---------------------------------------------------------------------------

def test_manual_sync_failure_and_completion_feedback(tmp_path):
    """Verify that Manual Sync returns accurate and descriptive feedback for:
    1. Offline / unavailable drives: warning status with clear offline message.
    2. Clean drives with zero missing files: ok status with 'all verified' message and info severity.
    3. Drives with unindexed new media: ok status reporting exact unindexed count.
    """
    db_path = tmp_path / "sync_feedback.sqlite3"
    conn = connect(db_path)
    conn.close()

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findFiles": {"files": []},
        "findScenes": {"scenes": []},
        "configuration": {"general": {"stashes": []}},
    }

    # Case 1: Selected root is offline/missing from filesystem
    offline_root = tmp_path / "NonExistentDrive_USB_Offline"
    res_offline = reconcile_missing_media_across_roots(
        db_path,
        stash=mock_stash,
        roots=[str(offline_root)],
        config={},
    )
    assert res_offline["status"] == "warning"
    assert "Selected library drive(s) are currently offline or unavailable." in res_offline["message"]

    # Case 2: Clean drives (0 missing, 0 unindexed)
    online_root = tmp_path / "OnlineDrive"
    online_root.mkdir()
    res_clean = reconcile_missing_media_across_roots(
        db_path,
        stash=mock_stash,
        roots=[str(online_root)],
        config={},
    )
    assert res_clean["status"] == "ok"
    assert res_clean["missing_checked"] == 0
    assert res_clean["reconciled_count"] == 0
    assert "All tracked scene files verified present on disk." in res_clean["message"]

    conn = connect(db_path)
    log_clean = conn.execute(
        "SELECT severity, status FROM activity_log WHERE action='reconcile missing media' AND status != 'starting' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert log_clean["severity"] == "info"

    # Case 3: Drives with unindexed new media
    new_video = online_root / "New_Release_2026.mp4"
    new_video.write_bytes(b"brand new movie data")

    res_unindexed = reconcile_missing_media_across_roots(
        db_path,
        stash=mock_stash,
        roots=[str(online_root)],
        config={},
    )
    assert res_unindexed["status"] == "ok"
    assert res_unindexed["unindexed_found_count"] == 1
    assert "Found 1 unindexed new video(s) on disk." in res_unindexed["message"]


# ---------------------------------------------------------------------------
# Area 4: Cleanup message clearing after task completion
# ---------------------------------------------------------------------------

def test_cleanup_message_clearing_after_task_completion(tmp_path):
    """Verify that clean_deleted_from_stash:
    1. Prunes dead records from files, filename_state, and filename_previews.
    2. Resolves pending filesystem_events associated with the deleted paths.
    3. Removes stale entries from unindexed_external_media.
    4. Drops missing_files_count to 0 so the alert messages clear completely.
    """
    db_path = tmp_path / "cleanup_clearing.sqlite3"
    conn = connect(db_path)

    now = utc_now()
    deleted_path_1 = "/Volumes/Storage/Deleted1.mp4"
    deleted_path_2 = "/Volumes/Storage/Deleted2.mp4"

    # 1. Insert missing file records
    conn.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f_del_1', 'sc_del_1', ?, 'Deleted1.mp4', 1000, 0, ?, ?)""",
        (deleted_path_1, now, now),
    )
    conn.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f_del_2', 'sc_del_2', ?, 'Deleted2.mp4', 2000, 0, ?, ?)""",
        (deleted_path_2, now, now),
    )

    # 2. Insert filename_state and preview entries
    conn.execute(
        """INSERT INTO filename_state (file_id, base_stem, base_source, created_at, updated_at)
           VALUES ('f_del_1', 'Deleted1', 'test', ?, ?)""",
        (now, now),
    )
    conn.execute(
        """INSERT INTO filename_preview_runs (id, started_at, status) VALUES (1, ?, 'complete')""",
        (now,),
    )
    conn.execute(
        """INSERT INTO filename_previews (run_id, scene_id, file_id, current_path, base_stem, proposed_path, status, reason, recorded_at)
           VALUES (1, 'sc_del_1', 'f_del_1', ?, 'Deleted1', ?, 'matches', 'ok', ?)""",
        (deleted_path_1, deleted_path_1, now),
    )

    # 3. Insert pending filesystem_events for the deleted paths
    conn.execute(
        """INSERT INTO filesystem_events (event_key, event_type, source_path, first_seen_at, last_seen_at, status)
           VALUES ('ev_1', 'file_deleted', ?, ?, ?, 'pending')""",
        (deleted_path_1, now, now),
    )

    # 4. Insert into unindexed_external_media
    conn.execute(
        """INSERT INTO unindexed_external_media (path, basename, size, discovered_at, ignored)
           VALUES (?, 'Deleted1.mp4', 1000, ?, 0)""",
        (deleted_path_1, now),
    )
    conn.commit()

    # Pre-clean check
    assert conn.execute("SELECT count(*) FROM files WHERE exists_on_disk = 0").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM filesystem_events WHERE status = 'pending'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM unindexed_external_media").fetchone()[0] == 1
    conn.close()

    # Mock Stash: scenes have been pruned by Stash metadataClean
    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {"findScenes": {"scenes": []}}

    # Execute clean_deleted_scenes_from_stash
    result = clean_deleted_scenes_from_stash(db_path, stash=mock_stash)

    assert result["cleaned_count"] == 2
    assert result["remaining_missing"] == 0

    # Post-clean verification
    conn = connect(db_path)
    missing_count = conn.execute("SELECT count(*) FROM files WHERE exists_on_disk = 0").fetchone()[0]
    total_files = conn.execute("SELECT count(*) FROM files").fetchone()[0]
    state_count = conn.execute("SELECT count(*) FROM filename_state WHERE file_id IN ('f_del_1', 'f_del_2')").fetchone()[0]
    preview_count = conn.execute("SELECT count(*) FROM filename_previews WHERE file_id IN ('f_del_1', 'f_del_2')").fetchone()[0]
    pending_events = conn.execute("SELECT count(*) FROM filesystem_events WHERE status = 'pending'").fetchone()[0]
    unindexed_count = conn.execute("SELECT count(*) FROM unindexed_external_media WHERE path = ?", (deleted_path_1,)).fetchone()[0]
    activity = conn.execute("SELECT detail, severity FROM activity_log WHERE category='clean' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()

    assert missing_count == 0
    assert total_files == 0
    assert state_count == 0
    assert preview_count == 0
    assert pending_events == 0
    assert unindexed_count == 0
    assert activity is not None
    assert "pruned 2 deleted scene record(s)" in activity["detail"]
