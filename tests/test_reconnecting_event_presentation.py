import json
import os
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import librarymanager_core
from librarymanager_core import (
    connect,
    dashboard_data,
    filesystem_monitor_summary,
    pending_filesystem_events,
    record_filesystem_event,
    resolve_filesystem_event,
    annotate_pending_events_processing_state,
    utc_now,
    opensubtitles_hash,
)
from librarymanager_monitor import (
    MoveWorker,
    update_status,
)


def _init_db(database_path):
    connection = connect(database_path)
    connection.executescript(librarymanager_core.SCHEMA)
    connection.close()


def _insert_file(database_path, path, file_id="1", scene_id="10", exists=1):
    con = connect(database_path)
    try:
        p = Path(path)
        size = p.stat().st_size if p.is_file() else 140000
        h = opensubtitles_hash(p) if p.is_file() else "abc123hash"
        con.execute(
            """INSERT INTO files(file_id,scene_id,path,basename,title,studio,performers_json,size,duration,
                   fingerprints_json,scene_metadata_json,exists_on_disk,first_seen_at,last_seen_at,missing_since)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (file_id, scene_id, str(path), p.name, "Movie", None, "[]", size, 10.0,
             json.dumps([{"type": "oshash", "value": h}]), "{}", int(exists),
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", None),
        )
        con.commit()
    finally:
        con.close()


def test_active_video_and_companion_moves_appear_as_reconnecting():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        src_dir = tmp / "src"
        dst_dir = tmp / "dst"
        src_dir.mkdir(parents=True)
        dst_dir.mkdir(parents=True)

        src_vid = src_dir / "scene1.mp4"
        dst_vid = dst_dir / "scene1.mp4"
        src_vid.write_bytes(b"V" * 140000)
        dst_vid.write_bytes(b"V" * 140000)

        src_jpg = src_dir / "scene1.jpg"
        dst_jpg = dst_dir / "scene1.jpg"
        src_jpg.write_bytes(b"J" * 2000)
        dst_jpg.write_bytes(b"J" * 2000)

        _insert_file(db, src_vid, file_id="101", scene_id="1")

        record_filesystem_event(db, "moved", str(src_vid), str(dst_vid))
        record_filesystem_event(db, "moved", str(src_jpg), str(dst_jpg))

        # Set monitor running with active move for scene1.mp4
        con = connect(db)
        con.execute(
            """UPDATE filesystem_monitor_status
               SET token='tok1', pid=?, state='running', started_at=?, heartbeat_at=?,
                   roots_json=?, unavailable_roots_json='[]',
                   active_moves_json=?
               WHERE id=1""",
            (
                os.getpid(),
                utc_now(),
                utc_now(),
                json.dumps([str(tmp)]),
                json.dumps([{
                    "source_path": str(src_vid),
                    "destination_path": str(dst_vid),
                    "status": "reconnecting"
                }]),
            )
        )
        con.commit()
        con.close()

        with patch.object(librarymanager_core, "_is_pid_alive", return_value=True), \
             patch.object(librarymanager_core, "_pid_matches_monitor", return_value=True):
            data = dashboard_data(db)
            monitor = data["monitor"]
            pending = data["pending_events"]

            assert monitor["state"] == "running"
            assert not monitor["is_stale"]
            assert len(monitor["active_moves"]) == 1
            assert monitor["pending_events"] == 2

            # Video event is reconnecting
            vid_ev = next(e for e in pending if e["source_path"] == str(src_vid))
            assert vid_ev["processing_state"] == "reconnecting"

            # Companion JPG is automatically matched to the active video move
            jpg_ev = next(e for e in pending if e["source_path"] == str(src_jpg))
            assert jpg_ev["processing_state"] == "reconnecting"
            assert jpg_ev.get("companion_of") == "scene1.mp4"

            # Neither requires manual attention
            assert all(e["processing_state"] == "reconnecting" for e in pending)


def test_successful_reconnection_clears_processing_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        src_dir = tmp / "src"
        dst_dir = tmp / "dst"
        src_dir.mkdir(parents=True)
        dst_dir.mkdir(parents=True)

        src_vid = src_dir / "scene1.mp4"
        dst_vid = dst_dir / "scene1.mp4"
        src_vid.write_bytes(b"V" * 140000)
        dst_vid.write_bytes(b"V" * 140000)

        _insert_file(db, src_vid, file_id="101", scene_id="1")
        record_filesystem_event(db, "moved", str(src_vid), str(dst_vid))

        stash_mock = MagicMock()
        stash_mock.metadata_scan.return_value = "job-42"
        stash_mock.wait_for_job.return_value = True
        stash_mock.call_GQL.return_value = {
            "findScene": {
                "files": [{"id": "101", "path": str(dst_vid), "basename": "scene1.mp4"}]
            }
        }

        worker = MoveWorker(db, stash_mock, enabled=True, notifications=None)

        # Process the move
        worker._process_move(str(src_vid), str(dst_vid))

        # Reconnection completed
        assert worker.active_move is None
        assert worker.active_moves_summary() == []

        # Event was resolved in SQLite
        pending = pending_filesystem_events(db)
        assert len(pending) == 0

        data = dashboard_data(db)
        assert len(data["pending_events"]) == 0
        assert data["monitor"]["active_moves"] == []


def test_failed_stalled_or_abandoned_moves_return_to_needs_attention():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        src_dir = tmp / "src"
        dst_dir = tmp / "dst"
        src_dir.mkdir(parents=True)
        dst_dir.mkdir(parents=True)

        src_vid = src_dir / "scene1.mp4"
        dst_vid = dst_dir / "scene1.mp4"
        # Destination missing or invalid -> verification fails
        record_filesystem_event(db, "moved", str(src_vid), str(dst_vid))

        stash_mock = MagicMock()
        worker = MoveWorker(db, stash_mock, enabled=True, notifications=None)

        # Simulate monitor running with PID
        con = connect(db)
        con.execute(
            """UPDATE filesystem_monitor_status
               SET token='tok1', pid=?, state='running', started_at=?, heartbeat_at=?,
                   roots_json='[]', unavailable_roots_json='[]', active_moves_json='[]'
               WHERE id=1""",
            (os.getpid(), utc_now(), utc_now())
        )
        con.commit()
        con.close()

        # Run process move which fails because destination does not exist
        worker._process_move(str(src_vid), str(dst_vid))

        # Worker is no longer processing it
        assert worker.active_move is None
        assert worker.active_moves_summary() == []

        data = dashboard_data(db)
        pending = data["pending_events"]
        assert len(pending) == 1
        # It must NOT be hidden — processing_state is None
        assert pending[0]["processing_state"] is None
        assert data["monitor"]["attention_events"] == 1


def test_deferred_retry_shows_waiting_state_not_disappeared():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        src_vid = "/media/nas/scene1.mp4"
        dst_vid = "/media/library/scene1.mp4"
        record_filesystem_event(db, "moved", src_vid, dst_vid)

        stash_mock = MagicMock()
        worker = MoveWorker(db, stash_mock, enabled=True, notifications=None)

        # Simulate transient error during verification
        with worker.lock:
            worker.deferred_moves[(src_vid, dst_vid)] = {
                "attempts": 2,
                "next_retry_mono": time.monotonic() + 10.0,
                "last_error": "Permission denied (transient NAS lock)"
            }
        worker._sync_active_moves()

        con = connect(db)
        con.execute(
            """UPDATE filesystem_monitor_status
               SET token='tok1', pid=?, state='running', started_at=?, heartbeat_at=?,
                   roots_json='[]', unavailable_roots_json='[]'
               WHERE id=1""",
            (os.getpid(), utc_now(), utc_now())
        )
        con.commit()
        con.close()

        with patch.object(librarymanager_core, "_is_pid_alive", return_value=True), \
             patch.object(librarymanager_core, "_pid_matches_monitor", return_value=True):
            data = dashboard_data(db)
            pending = data["pending_events"]
            assert len(pending) == 1

            # Deferred move is not hidden, but clearly shows waiting retry state
            ev = pending[0]
            assert ev["processing_state"] == "deferred"
            assert ev["processing_attempts"] == 2
            assert "NAS lock" in ev["processing_error"]


def test_monitor_restart_or_crash_cannot_leave_events_permanently_hidden():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        src_vid = "/media/nas/scene1.mp4"
        dst_vid = "/media/library/scene1.mp4"
        record_filesystem_event(db, "moved", src_vid, dst_vid)

        # Case A: Worker was processing, but process crashed / terminated unexpectedly
        con = connect(db)
        con.execute(
            """UPDATE filesystem_monitor_status
               SET token='dead-tok', pid=999999, state='running', started_at=?, heartbeat_at=?,
                   roots_json='[]', unavailable_roots_json='[]',
                   active_moves_json=?
               WHERE id=1""",
            (
                utc_now(),
                utc_now(),
                json.dumps([{
                    "source_path": src_vid,
                    "destination_path": dst_vid,
                    "status": "reconnecting"
                }]),
            )
        )
        con.commit()
        con.close()

        data = dashboard_data(db)
        monitor = data["monitor"]
        pending = data["pending_events"]

        # Monitor detected stale/dead process
        assert monitor["is_stale"] is True
        assert monitor["active_moves"] == []  # Cleared on stale/dead monitor

        # Event MUST return to Needs Attention (processing_state=None)
        assert len(pending) == 1
        assert pending[0]["processing_state"] is None

        # Case B: Monitor heartbeat is stale (>30s old)
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        con = connect(db)
        con.execute(
            """UPDATE filesystem_monitor_status
               SET token='tok', pid=?, state='running', started_at=?, heartbeat_at=?,
                   active_moves_json=?
               WHERE id=1""",
            (
                os.getpid(),
                old_time,
                old_time,
                json.dumps([{
                    "source_path": src_vid,
                    "destination_path": dst_vid,
                    "status": "reconnecting"
                }]),
            )
        )
        con.commit()
        con.close()

        data = dashboard_data(db)
        assert data["monitor"]["is_stale"] is True
        assert data["monitor"]["active_moves"] == []
        assert data["pending_events"][0]["processing_state"] is None

        # Case C: Monitor is stopped
        con = connect(db)
        con.execute("UPDATE filesystem_monitor_status SET state='stopped' WHERE id=1")
        con.commit()
        con.close()

        data = dashboard_data(db)
        assert data["monitor"]["state"] == "stopped"
        assert data["monitor"]["active_moves"] == []
        assert data["pending_events"][0]["processing_state"] is None
