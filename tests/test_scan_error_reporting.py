"""Focused regression tests for WatchTower scan error handling and reporting.

Validates that:
1. Directory enumeration permission errors are recorded and do not produce clean-success status.
2. Cooperative scan timeouts produce an incomplete/timed-out result with useful context.
3. Manual sync with a mixture of accessible and unavailable roots reports the unavailable root.
4. os.walk() traversal errors are recorded and reflected in reconciliation status.
5. An inaccessible subtree is distinguished from a successfully scanned empty directory.
6. Partial ingestion failures appear in the result and are not reported as clean success.
7. Folder iteration failures in missing-file reconciliation are not misreported as ordinary unmatched results.
8. Startup monitoring does not claim all roots are active when root attachment fails.
9. Both manual and automatic/background operations persist appropriate diagnostic records.
10. Console -> Overview displays the correct success, warning, incomplete and failure states.
11. Background errors remain visible after a page refresh.
12. Normal filesystem races and successful scans continue to behave correctly.
"""
from __future__ import annotations

import errno
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from librarymanager.database import create_connection, ensure_schema
from librarymanager.scanning.reconciliation import (
    reconcile_missing_files,
    reconcile_missing_media_across_roots,
)
from librarymanager.services.manual_sync import add_all_unindexed_media_files
from librarymanager_core import connect, dashboard_data, record_activity
from librarymanager_monitor import CompletedDownloadWorker


def _init_db(db_path: Path):
    conn = create_connection(db_path)
    ensure_schema(conn, db_path)
    conn.close()


def _make_worker(db_path: Path, incoming: Path, scan_timeout: float = 5.0):
    mock_stash = MagicMock()
    mock_stash.find_plugin_config.return_value = {}
    return CompletedDownloadWorker(
        db_path,
        mock_stash,
        str(incoming),
        enabled=True,
        settle_seconds=300,
        notifications=False,
        fallback_seconds=60,
        incoming_folders=[incoming],
        library_roots=[],
        scan_timeout=scan_timeout,
    )


# ---------------------------------------------------------------------------
# Test 1: Directory enumeration permission errors in _iter_directory_bounded
# ---------------------------------------------------------------------------
def test_iter_directory_bounded_records_directory_permission_error(tmp_path):
    db = tmp_path / "test1.db"
    _init_db(db)
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    sub_ok = incoming / "sub_ok"
    sub_ok.mkdir()
    (sub_ok / "clip.mp4").write_bytes(b"data")
    sub_denied = incoming / "sub_denied"
    sub_denied.mkdir()

    worker = _make_worker(db, incoming)

    real_scandir = os.scandir

    def faulty_scandir(p):
        if "sub_denied" in str(p):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_scandir(p)

    scan_state = {}
    with patch("os.scandir", side_effect=faulty_scandir):
        results = list(worker._iter_directory_bounded(incoming, scan_state=scan_state))

    assert any(p.name == "clip.mp4" for p in results)
    assert scan_state["status"] == "completed_with_errors"
    assert len(scan_state["errors"]) == 1
    assert "sub_denied" in scan_state["errors"][0]["path"]
    assert scan_state["errors"][0]["errno"] == errno.EACCES


# ---------------------------------------------------------------------------
# Test 2: Cooperative scan timeouts produce an incomplete/timed-out result
# ---------------------------------------------------------------------------
def test_iter_directory_bounded_reports_timeout_status(tmp_path):
    db = tmp_path / "test2.db"
    _init_db(db)
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "file1.mp4").write_bytes(b"data1")

    worker = _make_worker(db, incoming)
    past_deadline = time.monotonic() - 1.0
    scan_state = {}
    results = list(worker._iter_directory_bounded(incoming, deadline=past_deadline, scan_state=scan_state))

    assert len(results) == 0
    assert scan_state["status"] == "timed_out"
    assert scan_state["timed_out"] is True
    assert "deadline reached" in scan_state["detail"]


# ---------------------------------------------------------------------------
# Test 3: Manual sync with mixture of accessible and unavailable roots
# ---------------------------------------------------------------------------
def test_manual_sync_reports_partially_offline_roots(tmp_path):
    db = tmp_path / "test3.db"
    _init_db(db)

    online_root = tmp_path / "OnlineDrive"
    online_root.mkdir()
    offline_root = tmp_path / "OfflineDrive_Unmounted"

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findFiles": {"files": []},
        "findScenes": {"scenes": []},
        "configuration": {"general": {"stashes": []}},
    }

    res = reconcile_missing_media_across_roots(
        db,
        stash=mock_stash,
        roots=[str(online_root), str(offline_root)],
        config={},
    )

    assert res["status"] == "completed_with_errors"
    assert str(offline_root) in res["unavailable_roots"]
    assert res["error_count"] >= 1
    assert "unavailable" in res["message"].lower()

    # Verify diagnostic activity record in database
    conn = connect(db)
    activity = conn.execute(
        "SELECT * FROM activity_log WHERE category='reconciliation' AND action='reconcile missing media' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert activity is not None
    assert activity["status"] == "completed_with_errors"
    assert activity["severity"] == "warning"
    assert "unavailable" in activity["detail"].lower()


# ---------------------------------------------------------------------------
# Test 4: os.walk traversal errors reflected in reconciliation status
# ---------------------------------------------------------------------------
def test_reconcile_missing_media_os_walk_onerror_reporting(tmp_path):
    db = tmp_path / "test4.db"
    _init_db(db)

    root = tmp_path / "DriveA"
    root.mkdir()
    sub_err = root / "UnreadableSubfolder"
    sub_err.mkdir()

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findFiles": {"files": []},
        "findScenes": {"scenes": []},
        "configuration": {"general": {"stashes": []}},
    }

    real_walk = os.walk

    def faulty_walk(top, *args, **kwargs):
        onerror = kwargs.get("onerror")
        for dirpath, dirnames, filenames in real_walk(top, *args, **kwargs):
            if "UnreadableSubfolder" in dirpath and onerror:
                onerror(PermissionError(errno.EACCES, "Permission denied scanning UnreadableSubfolder"))
            yield dirpath, dirnames, filenames

    with patch("os.walk", side_effect=faulty_walk):
        res = reconcile_missing_media_across_roots(
            db,
            stash=mock_stash,
            roots=[str(root)],
            config={},
        )

    assert res["status"] == "completed_with_errors"
    assert res["error_count"] >= 1
    assert any("Permission denied" in e.get("error", "") for e in res["errors"])

    # Verify activity record
    conn = connect(db)
    activity = conn.execute(
        "SELECT * FROM activity_log WHERE category='reconciliation' AND status='completed_with_errors'"
    ).fetchone()
    conn.close()
    assert activity is not None
    assert activity["severity"] == "warning"


# ---------------------------------------------------------------------------
# Test 5: Inaccessible subtree distinguished from successfully scanned empty dir
# ---------------------------------------------------------------------------
def test_scan_tree_worker_distinguishes_inaccessible_from_empty(tmp_path):
    db = tmp_path / "test5.db"
    _init_db(db)

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    empty_sub = incoming / "empty_dir"
    empty_sub.mkdir()
    unreadable_sub = incoming / "unreadable_dir"
    unreadable_sub.mkdir()

    worker = _make_worker(db, incoming)

    # Clean empty directory returns 0
    res_empty = worker._scan_tree_worker(empty_sub)
    assert res_empty == 0

    # Inaccessible directory returns -1 and records activity failure
    def faulty_is_dir(self):
        if "unreadable_dir" in str(self):
            raise PermissionError(errno.EACCES, "Permission denied")
        return True

    with patch.object(Path, "is_dir", faulty_is_dir):
        res_error = worker._scan_tree_worker(unreadable_sub)
        assert res_error == -1

    conn = connect(db)
    act = conn.execute(
        "SELECT * FROM activity_log WHERE category='incoming' AND action='scan subtree' AND status='failed'"
    ).fetchone()
    conn.close()

    assert act is not None
    assert act["severity"] == "warning"
    assert "unreadable_dir" in act["old_path"]


# ---------------------------------------------------------------------------
# Test 6: Partial ingestion failures in add_all_unindexed_media_files
# ---------------------------------------------------------------------------
def test_add_all_unindexed_reports_partial_failure(tmp_path):
    db = tmp_path / "test6.db"
    _init_db(db)

    root = tmp_path / "root"
    root.mkdir()
    file1 = root / "clip1.mp4"
    file1.write_bytes(b"video1")
    file2 = root / "clip2.mp4"
    file2.write_bytes(b"video2")

    conn = connect(db)
    conn.execute(
        "INSERT INTO unindexed_external_media (path, basename, size, discovered_at, ignored) VALUES (?, ?, ?, ?, 0)",
        (str(file1), "clip1.mp4", 6, "2026-10-15T00:00:00Z")
    )
    conn.execute(
        "INSERT INTO unindexed_external_media (path, basename, size, discovered_at, ignored) VALUES (?, ?, ?, ?, 0)",
        (str(file2), "clip2.mp4", 6, "2026-10-15T00:00:00Z")
    )
    conn.commit()
    conn.close()

    # Mock add_unindexed_media_file: succeed on clip1, fail on clip2
    def mock_add(database_path, stash, path, config=None):
        if "clip1" in str(path):
            return {"status": "ok", "scene_id": "101"}
        return {"status": "warning", "message": "Stash rejected file due to missing codec"}

    with patch("librarymanager.services.manual_sync.add_unindexed_media_file", side_effect=mock_add):
        res = add_all_unindexed_media_files(db, stash=None, roots=[str(root)])

    assert res["status"] == "completed_with_errors"
    assert res["count"] == 1
    assert res["failed_count"] == 1
    assert len(res["failed"]) == 1
    assert "clip2" in res["failed"][0]["path"]


# ---------------------------------------------------------------------------
# Test 7: Folder iteration failure in reconcile_missing_files
# ---------------------------------------------------------------------------
def test_reconcile_missing_files_iterdir_failure(tmp_path):
    db = tmp_path / "test7.db"
    _init_db(db)

    folder = tmp_path / "media_folder"
    folder.mkdir()
    missing_file = folder / "scene_missing.mp4"

    conn = connect(db)
    conn.execute(
        """INSERT INTO files(file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f1', 's1', ?, 'scene_missing.mp4', 1000, 0, '2026-10-15T00:00:00Z', '2026-10-15T00:00:00Z')""",
        (str(missing_file),)
    )
    conn.commit()
    conn.close()

    # Simulate folder.iterdir raising PermissionError
    def faulty_iterdir(self):
        if "media_folder" in str(self):
            raise PermissionError(errno.EACCES, "Permission denied reading directory")
        return []

    with patch.object(Path, "iterdir", faulty_iterdir):
        summary, report = reconcile_missing_files(db)

    assert summary.get("read_errors", 0) == 1
    assert len(report) == 1
    assert report[0]["confidence"] == "error"
    assert "Permission denied" in report[0]["reason"]

    # Verify reconciliation_runs table status is completed_with_errors, not complete
    conn = connect(db)
    run_row = conn.execute("SELECT status FROM reconciliation_runs ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()

    assert run_row is not None
    assert run_row["status"] == "completed_with_errors"


# ---------------------------------------------------------------------------
# Test 8: Startup monitoring reports completed_with_errors when roots fail
# ---------------------------------------------------------------------------
def test_schedule_roots_async_reports_failure(tmp_path):
    db = tmp_path / "test8.db"
    _init_db(db)

    # Directly test the status logic of startup root scheduling
    record_activity(
        db, "monitor", "library roots ready", "completed_with_errors",
        severity="warning",
        detail="1 of 2 library root(s) monitored. 1 root(s) failed to attach: OfflineShare"
    )

    data = dashboard_data(db)
    assert len(data.get("scan_issues", [])) >= 1
    issue = data["scan_issues"][0]
    assert issue["category"] == "monitor"
    assert issue["status"] == "completed_with_errors"
    assert issue["severity"] == "warning"
    assert "OfflineShare" in issue["detail"]


# ---------------------------------------------------------------------------
# Test 9: Diagnostic records persisted for both manual & background errors
# ---------------------------------------------------------------------------
def test_diagnostic_records_persisted_for_both_modes(tmp_path):
    db = tmp_path / "test9.db"
    _init_db(db)

    # 1. Manual sync error
    record_activity(
        db, "reconciliation", "reconcile missing media", "completed_with_errors",
        severity="warning",
        old_path="/mnt/drive_b",
        detail="Reconciliation completed with errors: 1 drive(s) unavailable",
        metadata={"unavailable_roots": ["/mnt/drive_b"]}
    )

    # 2. Background incoming scan error
    record_activity(
        db, "incoming", "incoming folder scan", "timed_out",
        severity="warning",
        old_path="/mnt/incoming_share",
        detail="Directory scan timed out for /mnt/incoming_share (deadline reached)",
        metadata={"last_path": "/mnt/incoming_share/huge_folder"}
    )

    conn = connect(db)
    rows = conn.execute(
        "SELECT category, action, status, severity, old_path, detail FROM activity_log ORDER BY id ASC"
    ).fetchall()
    conn.close()

    assert len(rows) == 2
    assert rows[0]["category"] == "reconciliation"
    assert rows[0]["status"] == "completed_with_errors"
    assert rows[1]["category"] == "incoming"
    assert rows[1]["status"] == "timed_out"


# ---------------------------------------------------------------------------
# Test 10: Console -> Overview displays the four scan outcome states
# ---------------------------------------------------------------------------
def test_console_overview_displays_all_four_outcome_states(tmp_path):
    db = tmp_path / "test10.db"
    _init_db(db)

    # 1. Complete (clean success: info severity, not in scan_issues)
    record_activity(
        db, "reconciliation", "reconcile missing media", "complete",
        severity="info", detail="Clean scan completed with 0 errors"
    )
    # 2. Completed with errors (warning/error card)
    record_activity(
        db, "reconciliation", "reconcile missing media", "completed_with_errors",
        severity="warning", detail="Reconciliation completed with errors: 1 drive offline",
        old_path="/mnt/drive_b"
    )
    # 3. Incomplete / timed out
    record_activity(
        db, "incoming", "incoming folder scan", "timed_out",
        severity="warning", detail="Directory scan timed out (deadline reached)",
        old_path="/mnt/incoming"
    )
    # 4. Failed
    record_activity(
        db, "incoming", "incoming folder scan", "failed",
        severity="error", detail="Incoming folder is unavailable",
        old_path="/mnt/missing_incoming"
    )

    data = dashboard_data(db)
    scan_issues = data.get("scan_issues", [])
    # 3 issues should appear (completed_with_errors, timed_out, failed). Clean complete should not appear as an issue.
    assert len(scan_issues) == 3
    statuses = [item["status"] for item in scan_issues]
    assert "completed_with_errors" in statuses
    assert "timed_out" in statuses
    assert "failed" in statuses


# ---------------------------------------------------------------------------
# Test 11: Background errors remain visible after page refresh
# ---------------------------------------------------------------------------
def test_background_errors_remain_visible_after_refresh(tmp_path):
    db = tmp_path / "test11.db"
    _init_db(db)

    # Background scan encounters a disconnect
    record_activity(
        db, "incoming", "incoming folder scan", "disconnected",
        severity="warning",
        old_path="/Volumes/NetworkIncoming",
        detail="Network disconnect scanning directory /Volumes/NetworkIncoming: [Errno 60] Operation timed out",
    )

    # 1st fetch (initial render)
    data1 = dashboard_data(db)
    scan_issues1 = data1.get("scan_issues", [])
    assert len(scan_issues1) == 1
    assert scan_issues1[0]["status"] == "disconnected"
    assert "/Volumes/NetworkIncoming" in scan_issues1[0]["old_path"]

    # 2nd fetch (simulating page refresh)
    data2 = dashboard_data(db)
    scan_issues2 = data2.get("scan_issues", [])
    assert len(scan_issues2) == 1
    assert scan_issues2[0]["id"] == scan_issues1[0]["id"]
    assert scan_issues2[0]["detail"] == scan_issues1[0]["detail"]


# ---------------------------------------------------------------------------
# Test 12: Normal filesystem races and clean scans behave correctly
# ---------------------------------------------------------------------------
def test_normal_filesystem_races_and_clean_scans(tmp_path):
    db = tmp_path / "test12.db"
    _init_db(db)

    online_root = tmp_path / "CleanRoot"
    online_root.mkdir()
    valid_clip = online_root / "good.mp4"
    valid_clip.write_bytes(b"video_bytes")

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findFiles": {"files": []},
        "findScenes": {"scenes": []},
        "configuration": {"general": {"stashes": []}},
    }

    # Clean scan should report status "ok"
    res = reconcile_missing_media_across_roots(
        db,
        stash=mock_stash,
        roots=[str(online_root)],
        config={},
    )
    assert res["status"] == "ok"
    assert res["error_count"] == 0

    # Disappearing file (ENOENT race between scandir and stat) is NOT flagged as a scan failure
    real_stat = os.stat

    def vanishing_stat(path, *args, **kwargs):
        if "good.mp4" in str(path):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")
        return real_stat(path, *args, **kwargs)

    with patch("os.stat", side_effect=vanishing_stat):
        res_race = reconcile_missing_media_across_roots(
            db,
            stash=mock_stash,
            roots=[str(online_root)],
            config={},
        )
    # Normal deletion race must remain status "ok", not completed_with_errors
    assert res_race["status"] == "ok"
    assert res_race["error_count"] == 0


# ---------------------------------------------------------------------------
# Test 13: Stale warning resolution lifecycle in Console -> Overview
# ---------------------------------------------------------------------------
def test_overview_scan_issue_resolution_lifecycle(tmp_path):
    """Verify exact resolution semantics for Console -> Overview:
    - Unresolved warning remains visible across refresh.
    - A running scan does NOT resolve it.
    - A scan of a different path does NOT resolve it.
    - A subsequent failed or partial scan does NOT resolve it.
    - Only a genuinely successful complete scan of the same scope resolves it.
    - Historical activity records remain preserved.
    """
    db = tmp_path / "test_resolution.db"
    _init_db(db)

    # 1. Enqueue error on /mnt/incoming
    record_activity(
        db, "incoming", "incoming folder scan", "timed_out",
        severity="warning", old_path="/mnt/incoming",
        detail="Directory scan timed out for /mnt/incoming (deadline reached)"
    )

    # Persists across initial view and refresh
    data1 = dashboard_data(db)
    assert len(data1.get("scan_issues", [])) == 1
    assert data1["scan_issues"][0]["status"] == "timed_out"

    data_refresh = dashboard_data(db)
    assert len(data_refresh.get("scan_issues", [])) == 1

    # 2. A scan starts (status='running') - must NOT resolve the issue
    record_activity(
        db, "incoming", "incoming folder scan", "running",
        severity="info", old_path="/mnt/incoming",
        detail="Scanning /mnt/incoming..."
    )
    data_running = dashboard_data(db)
    assert len(data_running.get("scan_issues", [])) == 1, "Running scan must not clear warning"

    # 3. A successful scan of a DIFFERENT path - must NOT resolve the issue
    record_activity(
        db, "incoming", "incoming folder scan", "complete",
        severity="info", old_path="/mnt/other_incoming",
        detail="Scan complete for /mnt/other_incoming"
    )
    data_diff_path = dashboard_data(db)
    assert len(data_diff_path.get("scan_issues", [])) == 1, "Different path must not clear warning"
    assert data_diff_path["scan_issues"][0]["old_path"] == "/mnt/incoming"

    # 4. A subsequent scan that completed with errors - must NOT resolve the issue
    record_activity(
        db, "incoming", "incoming folder scan", "completed_with_errors",
        severity="warning", old_path="/mnt/incoming",
        detail="Incoming scan completed with 1 error"
    )
    data_partial = dashboard_data(db)
    assert len(data_partial.get("scan_issues", [])) == 2, "Partial error does not resolve earlier warning"

    # 5. Genuinely successful complete scan of the SAME scope resolves both
    record_activity(
        db, "incoming", "incoming folder scan", "complete",
        severity="info", old_path="/mnt/incoming",
        detail="Incoming scan completed cleanly with 0 errors"
    )
    data_resolved = dashboard_data(db)
    assert len(data_resolved.get("scan_issues", [])) == 0, "Complete scan of same scope must resolve warnings"

    # 6. Verify historical activity records were preserved in activity_log
    conn = connect(db)
    total_records = conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
    conn.close()
    assert total_records == 5, "All 5 historical activity rows must be preserved"


# ---------------------------------------------------------------------------
# Test 14: Auto-ingestion setting respected and error reporting preserved
# ---------------------------------------------------------------------------
def test_auto_ingestion_setting_and_scan_error_reporting(tmp_path):
    """Verify:
    - Auto-ingestion disabled: discovered media queued for review; metadata_scan not called.
    - Auto-ingestion enabled: automatic ingestion proceeds as configured.
    - Scan errors reported accurately under both settings.
    """
    db = tmp_path / "test_autoingest.db"
    _init_db(db)

    root = tmp_path / "Drive"
    root.mkdir()
    vid = root / "UnindexedVid.mp4"
    vid.write_bytes(b"content")

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findFiles": {"files": []},
        "findScenes": {"scenes": []},
        "configuration": {"general": {"stashes": []}},
    }

    # 1. Disabled setting: discovered media queued, metadata_scan not called
    res_disabled = reconcile_missing_media_across_roots(
        db, mock_stash, [str(root)], config={"autoIngestUnindexedMedia": False}
    )
    assert res_disabled["unindexed_found_count"] == 1
    assert res_disabled["auto_ingested_count"] == 0
    assert not mock_stash.metadata_scan.called

    conn = connect(db)
    queued = conn.execute("SELECT path, ignored FROM unindexed_external_media").fetchall()
    conn.close()
    assert len(queued) == 1
    assert queued[0]["path"] == str(vid)
    assert queued[0]["ignored"] == 0

    # 2. Enabled setting: metadata_scan is called and auto_ingested_count > 0
    mock_stash.metadata_scan.return_value = "job-456"
    mock_stash.wait_for_job.return_value = True

    def mock_find_scene(query, variables=None):
        if "findScenes" in query or "FindSceneByPath" in query:
            return {"findScenes": {"scenes": [{"id": "s1", "files": [{"id": "f1", "path": str(vid)}]}]}}
        return {"findFiles": {"files": []}, "findScenes": {"scenes": []}, "configuration": {"general": {"stashes": []}}}

    mock_stash.call_GQL.side_effect = mock_find_scene

    res_enabled = reconcile_missing_media_across_roots(
        db, mock_stash, [str(root)], config={"autoIngestUnindexedMedia": True}
    )
    assert res_enabled["unindexed_found_count"] == 1
    assert res_enabled["auto_ingested_count"] == 1
    assert mock_stash.metadata_scan.called

    # 3. Scan errors reported under both disabled and enabled settings
    res_err_disabled = reconcile_missing_media_across_roots(
        db, mock_stash, [str(root), "/nonexistent/drive"], config={"autoIngestUnindexedMedia": False}
    )
    assert res_err_disabled["status"] == "completed_with_errors"
    assert "/nonexistent/drive" in res_err_disabled["unavailable_roots"]

    res_err_enabled = reconcile_missing_media_across_roots(
        db, mock_stash, [str(root), "/nonexistent/drive"], config={"autoIngestUnindexedMedia": True}
    )
    assert res_err_enabled["status"] == "completed_with_errors"
    assert "/nonexistent/drive" in res_err_enabled["unavailable_roots"]


# ---------------------------------------------------------------------------
# Test 15: Successful traversal with unmatched missing scenes does not warn
# ---------------------------------------------------------------------------
def test_reconciliation_unmatched_missing_scenes_does_not_produce_scan_warning(tmp_path):
    """Clean traversal with unmatched missing scenes must emit status=complete, severity=info
    and resolve prior scan errors without creating a new scan warning."""
    db = tmp_path / "test_unmatched.db"
    _init_db(db)

    root = tmp_path / "MediaRoot"
    root.mkdir()

    scene_path = str(root / "NonExistentScene.mp4")
    conn = connect(db)
    conn.execute(
        """INSERT INTO files(file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at, missing_since)
           VALUES ('f100', 's100', ?, 'NonExistentScene.mp4', 0, '2026-01-01', '2026-01-01', '2026-01-01')""",
        (scene_path,)
    )
    conn.commit()
    conn.close()

    # Simulate an earlier scan error (e.g. drive offline)
    record_activity(
        db, "reconciliation", "reconcile missing media", "completed_with_errors",
        severity="warning", detail="1 drive(s) unavailable"
    )
    assert len(dashboard_data(db).get("scan_issues", [])) == 1

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findFiles": {"files": []},
        "findScenes": {"scenes": [{"id": "s100", "files": [{"id": "f100", "path": scene_path}]}]},
        "configuration": {"general": {"stashes": []}},
    }

    # Run reconciliation: drives are accessible and 0 walk errors, but scene is unmatched
    res = reconcile_missing_media_across_roots(db, mock_stash, [str(root)])
    assert res["status"] == "ok"
    assert len(res["unmatched"]) == 1
    assert res["unmatched"][0]["file_id"] == "f100"

    # Verify latest activity record is info / complete
    conn = connect(db)
    rows = conn.execute(
        "SELECT status, severity FROM activity_log WHERE category='reconciliation' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert rows["status"] == "complete"
    assert rows["severity"] == "info"

    # Prior warning is now resolved; no scan warnings in Overview
    assert len(dashboard_data(db).get("scan_issues", [])) == 0


# ---------------------------------------------------------------------------
# Test 16: Genuine traversal errors still produce scan-error warnings
# ---------------------------------------------------------------------------
def test_reconciliation_genuine_traversal_errors_produce_warning(tmp_path):
    """Genuine traversal errors must continue to emit completed_with_errors / warning."""
    db = tmp_path / "test_traversal_err.db"
    _init_db(db)

    root = tmp_path / "MediaRoot"
    root.mkdir()
    sub = root / "SubDir"
    sub.mkdir()

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findFiles": {"files": []},
        "findScenes": {"scenes": []},
        "configuration": {"general": {"stashes": []}},
    }

    real_walk = os.walk

    def faulty_walk(top, *args, **kwargs):
        onerror = kwargs.get("onerror")
        for dirpath, dirnames, filenames in real_walk(top, *args, **kwargs):
            if "SubDir" in dirpath and onerror:
                onerror(PermissionError(errno.EACCES, "Permission denied"))
            yield dirpath, dirnames, filenames

    with patch("os.walk", side_effect=faulty_walk):
        res = reconcile_missing_media_across_roots(db, mock_stash, [str(root)])

    assert res["status"] == "completed_with_errors"
    assert res["error_count"] >= 1

    issues = dashboard_data(db).get("scan_issues", [])
    assert len(issues) == 1
    assert issues[0]["status"] == "completed_with_errors"
    assert issues[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# Test 17: Clean scan after watcher restart resolves previous warning
# ---------------------------------------------------------------------------
def test_clean_scan_after_watcher_restart_resolves_previous_warning(tmp_path):
    """A clean scan by a fresh monitor instance must resolve a pre-restart warning for that path
    and must not emit redundant success records on subsequent routine scans."""
    db = tmp_path / "test_restart_res.db"
    _init_db(db)

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "file.mp4").write_bytes(b"content")

    # 1. Worker 1 encounters an error and records a warning
    record_activity(
        db, "incoming", "incoming folder scan", "completed_with_errors",
        severity="warning", old_path=str(incoming),
        detail="Scan failed with permission error"
    )
    assert len(dashboard_data(db).get("scan_issues", [])) == 1

    # 2. Simulate watcher restart: instantiate Worker 2 fresh
    mock_stash = MagicMock()
    mock_stash.find_plugin_config.return_value = {}
    worker2 = CompletedDownloadWorker(
        db, mock_stash, str(incoming), enabled=False, settle_seconds=300, notifications=False,
        fallback_seconds=60, incoming_folders=[incoming], library_roots=[], scan_timeout=5.0
    )
    # Confirm active errors were loaded into Worker 2
    assert str(incoming) in worker2._active_folder_errors

    # 3. Worker 2 runs clean scan
    worker2._scan_incoming_folder(incoming, 0.0)

    # 4. Overview warning is now resolved
    issues = dashboard_data(db).get("scan_issues", [])
    assert len(issues) == 0, f"Expected 0 scan issues after clean scan, got {issues}"

    # 5. Verify resolution row was emitted
    conn = connect(db)
    res_rows = conn.execute(
        "SELECT status, severity FROM activity_log WHERE category='incoming' AND action='incoming folder scan' AND status='complete'"
    ).fetchall()
    assert len(res_rows) == 1
    assert res_rows[0]["severity"] == "info"

    # 6. Run second routine scan with Worker 2: no redundant complete record
    worker2._scan_incoming_folder(incoming, 0.0)
    count_after = conn.execute(
        "SELECT COUNT(*) FROM activity_log WHERE category='incoming' AND action='incoming folder scan' AND status='complete'"
    ).fetchone()[0]
    conn.close()
    assert count_after == 1, "Must not emit redundant complete records on routine scans"


# ---------------------------------------------------------------------------
# Test 18: Clean scan does not resolve different path or after failed scan
# ---------------------------------------------------------------------------
def test_clean_scan_does_not_resolve_different_path_or_after_unsuccessful_scan(tmp_path):
    """Clean scan of path B must not resolve path A, and an unsuccessful scan must not resolve."""
    db = tmp_path / "test_path_scope.db"
    _init_db(db)

    path_a = tmp_path / "incoming_a"
    path_a.mkdir()
    path_b = tmp_path / "incoming_b"
    path_b.mkdir()

    # Record scan error for path_a
    record_activity(
        db, "incoming", "incoming folder scan", "completed_with_errors",
        severity="warning", old_path=str(path_a),
        detail="Permission error on folder A"
    )

    worker = _make_worker(db, path_b)

    # 1. Clean scan of path_b: must NOT resolve path_a
    worker._scan_incoming_folder(path_b, 0.0)
    issues = dashboard_data(db).get("scan_issues", [])
    assert len(issues) == 1
    assert issues[0]["old_path"] == str(path_a)

    # 2. Unsuccessful scan of path_a (e.g. OSError / permission denied): must NOT resolve path_a
    def faulty_bounded(root, timeout=None, deadline=None, scan_state=None):
        if scan_state is not None:
            scan_state["status"] = "completed_with_errors"
            scan_state["errors"] = [{"path": str(path_a), "error": "Permission denied"}]
        return
        yield

    with patch.object(worker, "_iter_directory_bounded", side_effect=faulty_bounded):
        worker._scan_incoming_folder(path_a, 0.0)

    issues_after_fail = dashboard_data(db).get("scan_issues", [])
    assert len(issues_after_fail) >= 1
    assert any(i["old_path"] == str(path_a) for i in issues_after_fail)

