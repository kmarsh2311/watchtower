"""Regression tests for WatchTower Batch 4:
SQLite Concurrency Audit, Transaction Scoping, and Lifecycle Verification.

Tests cover:
1. Concurrent normal database activity during `clean_deleted_scenes_from_stash`.
2. Pruning of cached diagnostic reports upon clean.
3. Concurrent normal database activity during `reconcile_missing_media_across_roots`.
4. Empty or unavailable roots reporting warning rather than false success.
5. Concurrent normal database activity during `invalidate_stale_filing_proposals`.
"""
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from librarymanager.database.connection import create_connection
from librarymanager.database.schema import SCHEMA
from librarymanager_core import (
    clean_deleted_scenes_from_stash,
    connect,
    invalidate_stale_filing_proposals,
    reconcile_missing_media_across_roots,
    record_filesystem_event,
    utc_now,
)
from librarymanager_monitor import update_status


def test_concurrent_database_activity_during_clean_deleted_scenes(tmp_path):
    """Verify that clean_deleted_scenes_from_stash executes without SQLite lock contention
    even while background monitor threads execute rapid heartbeats and filesystem event writes.
    """
    db_path = tmp_path / "test_concurrency_clean.sqlite3"
    conn = connect(db_path)

    now = utc_now()
    # Insert 2 missing files (exists_on_disk = 0)
    conn.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f_clean_1', 'sc_clean_1', '/missing/1.mp4', '1.mp4', 1000, 0, ?, ?)""",
        (now, now),
    )
    conn.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f_clean_2', 'sc_clean_2', '/missing/2.mp4', '2.mp4', 2000, 0, ?, ?)""",
        (now, now),
    )
    conn.commit()
    conn.close()

    stop_event = threading.Event()
    worker_errors = []

    def heartbeat_loop():
        count = 0
        while not stop_event.is_set():
            try:
                update_status(
                    db_path,
                    token="test_token",
                    pid=99999,
                    state="running",
                    roots=["/monitored/root"],
                    unavailable=[],
                )
                count += 1
                time.sleep(0.01)
            except Exception as e:
                worker_errors.append(f"heartbeat: {e}")

    def event_loop():
        count = 0
        while not stop_event.is_set():
            try:
                record_filesystem_event(
                    db_path,
                    event_type="modified",
                    source_path=f"/monitored/root/file_{count}.mp4",
                )
                count += 1
                time.sleep(0.01)
            except Exception as e:
                worker_errors.append(f"event: {e}")

    t1 = threading.Thread(target=heartbeat_loop, daemon=True)
    t2 = threading.Thread(target=event_loop, daemon=True)
    t1.start()
    t2.start()

    # Mock Stash with simulated network latency on GraphQL check
    mock_stash = MagicMock()
    def slow_call_gql(query, variables=None):
        time.sleep(0.1)  # Simulate network HTTP round-trip
        return {"findScenes": {"scenes": []}}

    mock_stash.call_GQL.side_effect = slow_call_gql

    try:
        res = clean_deleted_scenes_from_stash(db_path, stash=mock_stash)
        assert res["cleaned_count"] == 2
        assert res["remaining_missing"] == 0
    finally:
        stop_event.set()
        t1.join(timeout=3)
        t2.join(timeout=3)

    assert worker_errors == [], f"Lock contention detected in background workers: {worker_errors}"


def test_clean_deleted_scenes_prunes_cached_reports(tmp_path):
    """Verify that clean_deleted_scenes_from_stash prunes dead records from
    reconciliation-report.json and resolution-plan.json so UI diagnostic panels clear immediately.
    """
    db_path = tmp_path / "test_report_pruning.sqlite3"
    conn = connect(db_path)
    now = utc_now()

    conn.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f_rep_1', 'sc_rep_1', '/missing/rep1.mp4', 'rep1.mp4', 5000, 0, ?, ?)""",
        (now, now),
    )
    conn.commit()
    conn.close()

    # Create mock reconciliation-report.json in watchtower root
    core_dir = Path(__file__).parent.parent
    recon_path = core_dir / "reconciliation-report.json"
    plan_path = core_dir / "resolution-plan.json"

    orig_recon = recon_path.read_text(encoding="utf-8") if recon_path.is_file() else None
    orig_plan = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else None

    try:
        recon_path.write_text(
            json.dumps({
                "summary": {"missing": 1, "matched": 0},
                "matches": [{"file_id": "f_rep_1", "scene_id": "sc_rep_1", "path": "/missing/rep1.mp4"}],
            }),
            encoding="utf-8",
        )
        plan_path.write_text(
            json.dumps({
                "summary": {"missing": 1},
                "items": [{"file_id": "f_rep_1", "scene_id": "sc_rep_1"}],
            }),
            encoding="utf-8",
        )

        mock_stash = MagicMock()
        mock_stash.call_GQL.return_value = {"findScenes": {"scenes": []}}

        res = clean_deleted_scenes_from_stash(db_path, stash=mock_stash)
        assert res["cleaned_count"] == 1

        # Check reports on disk
        updated_recon = json.loads(recon_path.read_text(encoding="utf-8"))
        updated_plan = json.loads(plan_path.read_text(encoding="utf-8"))

        assert len(updated_recon["matches"]) == 0
        assert updated_recon["summary"]["missing"] == 0
        assert len(updated_plan["items"]) == 0
    finally:
        # Restore original report files or clean up test files
        if orig_recon is not None:
            recon_path.write_text(orig_recon, encoding="utf-8")
        else:
            recon_path.unlink(missing_ok=True)
        if orig_plan is not None:
            plan_path.write_text(orig_plan, encoding="utf-8")
        else:
            plan_path.unlink(missing_ok=True)


def test_manual_sync_empty_roots_reports_warning(tmp_path):
    """When roots=[] is passed, manual sync should report warning and clarify that no roots
    were selected, rather than falsely claiming 'All tracked scene files verified present on disk'.
    """
    db_path = tmp_path / "test_empty_roots.sqlite3"
    conn = connect(db_path)
    conn.close()

    res = reconcile_missing_media_across_roots(db_path, stash=None, roots=[])
    assert res["status"] == "warning"
    assert res["missing_checked"] == 0
    assert "No library roots selected" in res["message"]


def test_manual_sync_offline_roots_reports_warning(tmp_path):
    """When specified roots are all offline/inaccessible, report warning and appropriate message."""
    db_path = tmp_path / "test_offline_roots.sqlite3"
    conn = connect(db_path)
    conn.close()

    res = reconcile_missing_media_across_roots(db_path, stash=None, roots=["/nonexistent/drive/path/123"])
    assert res["status"] == "warning"
    assert res["missing_checked"] == 0
    assert "Selected library drive(s) are currently offline or unavailable" in res["message"]


def test_concurrent_database_activity_during_reconcile_missing_media(tmp_path):
    """Verify that reconcile_missing_media_across_roots reconnects files without locking out
    concurrent background monitor writes during Stash metadata scan and disk checks.
    """
    db_path = tmp_path / "test_reconcile_concurrency.sqlite3"
    conn = connect(db_path)

    now = utc_now()
    media_dir = tmp_path / "library"
    media_dir.mkdir(parents=True, exist_ok=True)
    old_dir = media_dir / "old_folder"
    old_dir.mkdir(parents=True, exist_ok=True)
    new_dir = media_dir / "new_folder"
    new_dir.mkdir(parents=True, exist_ok=True)

    # Reconnected target file on disk in new_dir
    target_file = new_dir / "Scene1_Moved.mp4"
    target_file.write_bytes(b"content-for-scene-1")
    file_size = target_file.stat().st_size

    # Database records old missing path inside media_dir
    old_file_path = old_dir / "Scene1_Moved.mp4"
    conn.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f_rec_1', 'sc_rec_1', ?, 'Scene1_Moved.mp4', ?, 0, ?, ?)""",
        (str(old_file_path), file_size, now, now),
    )
    conn.commit()
    conn.close()

    stop_event = threading.Event()
    worker_errors = []

    def bg_writer():
        count = 0
        while not stop_event.is_set():
            try:
                record_filesystem_event(
                    db_path,
                    event_type="created",
                    source_path=f"/library/unrelated_{count}.mp4",
                )
                count += 1
                time.sleep(0.01)
            except Exception as e:
                worker_errors.append(str(e))

    bg_t = threading.Thread(target=bg_writer, daemon=True)
    bg_t.start()

    mock_stash = MagicMock()
    mock_stash.metadata_scan.return_value = "job-123"
    def slow_wait_for_job(job_id, timeout=60):
        time.sleep(0.1)  # Simulate scan wait
        return True
    mock_stash.wait_for_job.side_effect = slow_wait_for_job

    try:
        res = reconcile_missing_media_across_roots(
            db_path, stash=mock_stash, roots=[str(media_dir)], config={}
        )
        assert res["status"] == "ok"
        assert res["reconciled_count"] == 1
    finally:
        stop_event.set()
        bg_t.join(timeout=3)

    assert worker_errors == [], f"Lock contention in background writer: {worker_errors}"


def test_concurrent_database_activity_during_invalidate_stale_filing_proposals(tmp_path):
    """Verify that invalidate_stale_filing_proposals does not hold write locks while querying Stash."""
    db_path = tmp_path / "test_filing_concurrency.sqlite3"
    conn = connect(db_path)
    now = utc_now()

    conn.execute(
        """INSERT INTO filing_proposals (
               file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
               organize_by, matched_entity_id, matched_entity_name, match_source, reason, status, created_at, updated_at
           ) VALUES (
               'f_stale_1', 'sc_stale_1', '/nonexistent/source.mp4', '/dest/target.mp4', '/dest', 'target.mp4',
               'performer', '1', 'Performer One', 'tag', 'test', 'pending', ?, ?
           )""",
        (now, now),
    )
    conn.commit()
    conn.close()

    stop_event = threading.Event()
    worker_errors = []

    def bg_writer():
        count = 0
        while not stop_event.is_set():
            try:
                record_filesystem_event(
                    db_path,
                    event_type="modified",
                    source_path=f"/bg/file_{count}.mp4",
                )
                count += 1
                time.sleep(0.01)
            except Exception as e:
                worker_errors.append(str(e))

    bg_t = threading.Thread(target=bg_writer, daemon=True)
    bg_t.start()

    mock_stash = MagicMock()
    def slow_gql(query, variables=None):
        time.sleep(0.1)
        return {"findScene": None}
    mock_stash.call_GQL.side_effect = slow_gql

    try:
        invalidated = invalidate_stale_filing_proposals(db_path, stash=mock_stash)
        assert len(invalidated) == 1
    finally:
        stop_event.set()
        bg_t.join(timeout=3)

    assert worker_errors == [], f"Lock contention during filing invalidation: {worker_errors}"
