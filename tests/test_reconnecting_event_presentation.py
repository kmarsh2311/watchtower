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
            assert monitor["attention_events"] == 0

            vid_ev = next(e for e in pending if e["source_path"] == str(src_vid))
            assert vid_ev["processing_state"] == "reconnecting"

            jpg_ev = next(e for e in pending if e["source_path"] == str(src_jpg))
            assert jpg_ev["processing_state"] == "reconnecting"
            assert jpg_ev.get("companion_of") == "scene1.mp4"

            # Neither requires manual attention
            assert all(e["processing_state"] == "reconnecting" for e in pending)


def test_companion_first_ordering_and_waiting_video_grace_period():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        src_dir = tmp / "Latinos"
        dst_dir = tmp / "4K"
        src_dir.mkdir(parents=True)
        dst_dir.mkdir(parents=True)

        src_jpg = src_dir / "scene1.mp4.jpg"
        dst_jpg = dst_dir / "scene1.mp4.jpg"
        src_jpg.write_bytes(b"J" * 2000)
        dst_jpg.write_bytes(b"J" * 2000)

        # Step 1: Companion JPG arrives first (e.g. from Finder)
        record_filesystem_event(db, "moved", str(src_jpg), str(dst_jpg))

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

        with patch.object(librarymanager_core, "_is_pid_alive", return_value=True), \
             patch.object(librarymanager_core, "_pid_matches_monitor", return_value=True):
            # Phase A: During the 5-second grace window, shows "WAITING FOR VIDEO"
            data = dashboard_data(db)
            pending = data["pending_events"]
            assert len(pending) == 1
            assert pending[0]["processing_state"] == "waiting_video"
            assert "scene1.mp4" in pending[0]["companion_of"]
            # Excluded from Needs Attention
            assert data["monitor"]["attention_events"] == 0

            # Phase B: Associated video move arrives and is submitted
            src_vid = src_dir / "scene1.mp4"
            dst_vid = dst_dir / "scene1.mp4"
            src_vid.write_bytes(b"V" * 140000)
            dst_vid.write_bytes(b"V" * 140000)
            _insert_file(db, src_vid, file_id="101", scene_id="1")
            record_filesystem_event(db, "moved", str(src_vid), str(dst_vid))

            # Monitor active_moves now has the video
            con = connect(db)
            con.execute(
                """UPDATE filesystem_monitor_status
                   SET active_moves_json=? WHERE id=1""",
                (json.dumps([{
                    "source_path": str(src_vid),
                    "destination_path": str(dst_vid),
                    "status": "reconnecting"
                }]),)
            )
            con.commit()
            con.close()

            data = dashboard_data(db)
            pending = data["pending_events"]
            assert len(pending) == 2

            # Video is reconnecting
            vid_ev = next(e for e in pending if e["source_path"] == str(src_vid))
            assert vid_ev["processing_state"] == "reconnecting"

            # Companion transitioned from "waiting_video" to "reconnecting"
            jpg_ev = next(e for e in pending if e["source_path"] == str(src_jpg))
            assert jpg_ev["processing_state"] == "reconnecting"
            assert jpg_ev["companion_of"] == "scene1.mp4"
            assert data["monitor"]["attention_events"] == 0


def test_companion_grace_period_expiry_returns_to_needs_attention():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        src_jpg = "/media/Latinos/orphan.jpg"
        dst_jpg = "/media/4K/orphan.jpg"

        # Record companion event with timestamp 6 seconds ago (past 5s grace period)
        past_time = (datetime.now(timezone.utc) - timedelta(seconds=6)).isoformat()
        con = connect(db)
        event_key = json.dumps(["moved", src_jpg, dst_jpg, False], ensure_ascii=False)
        con.execute(
            """INSERT INTO filesystem_events(event_key,event_type,source_path,destination_path,is_directory,
                   first_seen_at,last_seen_at,event_count,status) VALUES (?,?,?,?,0,?,?,1,'pending')""",
            (event_key, "moved", src_jpg, dst_jpg, past_time, past_time)
        )
        con.execute(
            """UPDATE filesystem_monitor_status
               SET token='tok1', pid=?, state='running', started_at=?, heartbeat_at=?,
                   roots_json='[]', unavailable_roots_json='[]', active_moves_json='[]'
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

            # Grace period expired: processing_state is None
            assert pending[0]["processing_state"] is None
            # Must appear in Needs Attention
            assert data["monitor"]["attention_events"] == 1


def test_monitor_failure_or_stop_immediately_exposes_grace_period_companions():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        src_jpg = "/media/Latinos/scene1.jpg"
        dst_jpg = "/media/4K/scene1.jpg"
        record_filesystem_event(db, "moved", src_jpg, dst_jpg)

        # Case A: Monitor stopped
        con = connect(db)
        con.execute("UPDATE filesystem_monitor_status SET state='stopped' WHERE id=1")
        con.commit()
        con.close()

        data = dashboard_data(db)
        assert data["monitor"]["state"] == "stopped"
        assert len(data["pending_events"]) == 1
        # No grace period when monitor is stopped
        assert data["pending_events"][0]["processing_state"] is None
        assert data["monitor"]["attention_events"] == 1

        # Case B: Monitor process crashed / dead PID
        con = connect(db)
        con.execute(
            """UPDATE filesystem_monitor_status
               SET token='tok1', pid=999999, state='running', started_at=?, heartbeat_at=?
               WHERE id=1""",
            (utc_now(), utc_now())
        )
        con.commit()
        con.close()

        data = dashboard_data(db)
        assert data["monitor"]["is_stale"] is True
        assert len(data["pending_events"]) == 1
        # No grace period when monitor is crashed / stale
        assert data["pending_events"][0]["processing_state"] is None
        assert data["monitor"]["attention_events"] == 1


def test_live_status_and_dashboard_data_return_consistent_annotations():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        src_vid = "/media/nas/scene1.mp4"
        dst_vid = "/media/library/scene1.mp4"
        src_jpg = "/media/nas/scene1.jpg"
        dst_jpg = "/media/library/scene1.jpg"

        record_filesystem_event(db, "moved", src_vid, dst_vid)
        record_filesystem_event(db, "moved", src_jpg, dst_jpg)

        active = [{
            "source_path": src_vid,
            "destination_path": dst_vid,
            "status": "reconnecting"
        }]

        con = connect(db)
        con.execute(
            """UPDATE filesystem_monitor_status
               SET token='tok1', pid=?, state='running', started_at=?, heartbeat_at=?,
                   roots_json='[]', unavailable_roots_json='[]', active_moves_json=?
               WHERE id=1""",
            (os.getpid(), utc_now(), utc_now(), json.dumps(active))
        )
        con.commit()
        con.close()

        with patch.object(librarymanager_core, "_is_pid_alive", return_value=True), \
             patch.object(librarymanager_core, "_pid_matches_monitor", return_value=True):
            # 1. From dashboard_data
            data = dashboard_data(db)
            dash_pending = data["pending_events"]
            assert len(dash_pending) == 2
            assert all(e["processing_state"] == "reconnecting" for e in dash_pending)

            # 2. From live_status simulation (as updated in librarymanager.py)
            mon = filesystem_monitor_summary(db)
            live_pending = pending_filesystem_events(db)
            is_running = mon.get("state") == "running" and not mon.get("is_stale") and mon.get("pid_alive")
            annotate_pending_events_processing_state(live_pending, mon.get("active_moves", []), monitor_running=is_running)

            assert len(live_pending) == 2
            assert all(e["processing_state"] == "reconnecting" for e in live_pending)

            # Both return identical processing_state values
            for d_ev, l_ev in zip(dash_pending, live_pending):
                assert d_ev["event_key"] == l_ev["event_key"]
                assert d_ev["processing_state"] == l_ev["processing_state"]


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
        record_filesystem_event(db, "moved", str(src_vid), str(dst_vid))

        stash_mock = MagicMock()
        worker = MoveWorker(db, stash_mock, enabled=True, notifications=None)

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

        assert worker.active_move is None
        assert worker.active_moves_summary() == []

        with patch.object(librarymanager_core, "_is_pid_alive", return_value=True), \
             patch.object(librarymanager_core, "_pid_matches_monitor", return_value=True):
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


def test_standalone_jpg_move_remains_visible_under_needs_attention():
    """Ambiguous or standalone JPGs without any associated video must NEVER receive grace period; they must remain visible under Needs Attention."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        src_dir = tmp / "photos"
        dst_dir = tmp / "library"
        src_dir.mkdir()
        dst_dir.mkdir()

        # Standalone JPG with no video counterpart anywhere
        src_jpg = src_dir / "standalone.jpg"
        dst_jpg = dst_dir / "standalone.jpg"
        src_jpg.write_bytes(b"JPEG_DATA")
        dst_jpg.write_bytes(b"JPEG_DATA")

        record_filesystem_event(db, "moved", str(src_jpg), str(dst_jpg))

        # Monitor is running
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

        with patch.object(librarymanager_core, "_is_pid_alive", return_value=True),              patch.object(librarymanager_core, "_pid_matches_monitor", return_value=True):
            data = dashboard_data(db)
            pending = data["pending_events"]
            assert len(pending) == 1
            # Must NOT receive waiting_video!
            assert pending[0]["processing_state"] is None
            # Must be counted under attention_events
            assert data["monitor"]["attention_events"] == 1


def test_ambiguous_companion_move_remains_visible_under_needs_attention():
    """When a companion matches multiple potential videos in the destination directory, it is ambiguous and must remain under Needs Attention."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        src_dir = tmp / "incoming"
        dst_dir = tmp / "4K"
        src_dir.mkdir()
        dst_dir.mkdir()

        # Two videos in destination folder with same stem -> ambiguous!
        vid1 = dst_dir / "movie.mp4"
        vid2 = dst_dir / "movie.mkv"
        vid1.write_bytes(b"VID1")
        vid2.write_bytes(b"VID2")

        src_jpg = src_dir / "movie.jpg"
        dst_jpg = dst_dir / "movie.jpg"
        src_jpg.write_bytes(b"JPEG")
        dst_jpg.write_bytes(b"JPEG")

        record_filesystem_event(db, "moved", str(src_jpg), str(dst_jpg))

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

        with patch.object(librarymanager_core, "_is_pid_alive", return_value=True),              patch.object(librarymanager_core, "_pid_matches_monitor", return_value=True):
            data = dashboard_data(db)
            pending = data["pending_events"]
            assert len(pending) == 1
            # Ambiguous companion must NOT be marked as waiting_video
            assert pending[0]["processing_state"] is None
            assert data["monitor"]["attention_events"] == 1


def test_monitor_restart_or_crash_cannot_leave_events_permanently_hidden():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = tmp / "inventory.sqlite3"
        _init_db(db)

        src_vid = "/media/nas/scene1.mp4"
        dst_vid = "/media/library/scene1.mp4"
        record_filesystem_event(db, "moved", src_vid, dst_vid)

        # Monitor was running, but process crashed / terminated unexpectedly (stale)
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
        assert data["monitor"]["is_stale"] is True
        assert data["pending_events"][0]["processing_state"] is None
        assert data["monitor"]["attention_events"] == 1
