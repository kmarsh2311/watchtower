"""
Targeted regression tests for WT-007 and WT-008.

WT-007: Unnecessary incoming-files deletion records
- Verify deletion of tracked incoming downloads properly updates incoming_files to 'gone' and pops candidate.
- Verify ordinary file deletions outside incoming folders NEVER insert records into incoming_files.
- Verify library-file deletion detection, notifications, and review events are preserved and not suppressed
  simply because the path is absent from incoming_files.

WT-008: Incomplete GitHub Actions testing
- Verify GitHub Actions configuration runs the full Python suite, the complete UI suite, and existing
  bundled-watchdog smoke checks across all supported platforms.
"""
import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from watchdog.events import FileDeletedEvent, FileCreatedEvent

from librarymanager_core import connect, SCHEMA
from librarymanager_monitor import (
    LibraryEventHandler,
    CompletedDownloadWorker,
    RootAvailabilityTracker,
)




def _make_db(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def _make_worker(db_path, incoming_folder):
    stash = MagicMock()
    stash.call_GQL.return_value = {"findScenes": {"scenes": []}}
    worker = CompletedDownloadWorker(
        database_path=db_path,
        stash=stash,
        incoming_folder=str(incoming_folder),
        enabled=True,
        settle_seconds=10,
        notifications=False,
        fallback_seconds=60,
        incoming_folders=[str(incoming_folder)],
    )
    return worker


def test_tracked_incoming_deletion_updates_incoming_files_to_gone(tmp_path):
    """WT-007: Genuinely tracked incoming downloads must have their candidate popped
    and their incoming_files record updated to 'gone' when deleted."""
    db_path = tmp_path / "test.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    worker = _make_worker(db_path, incoming)
    handler = LibraryEventHandler(
        database_path=db_path,
        worker=MagicMock(),
        notifications=False,
        incoming_worker=worker,
    )

    incoming_file = incoming / "active_download.mp4"
    incoming_file.write_bytes(b"downloading content")
    file_str = str(incoming_file.resolve())

    # Submit as incoming download
    assert worker.submit(file_str) is True
    assert file_str in worker.candidates

    # Verify initial record in incoming_files is 'waiting'
    con = connect(db_path)
    row = con.execute("SELECT status FROM incoming_files WHERE path=?", (file_str,)).fetchone()
    con.close()
    assert row is not None
    assert row["status"] == "waiting"

    # File is deleted on disk
    incoming_file.unlink()
    handler.on_deleted(FileDeletedEvent(file_str))

    # Candidate must be popped from memory
    with worker.lock:
        assert file_str not in worker.candidates

    # incoming_files record must be updated to 'gone'
    con = connect(db_path)
    row_after = con.execute("SELECT status, detail FROM incoming_files WHERE path=?", (file_str,)).fetchone()
    con.close()
    assert row_after is not None
    assert row_after["status"] == "gone"
    assert "File removed from disk" in row_after["detail"]


def test_unrelated_library_file_deletion_creates_no_incoming_files_record(tmp_path):
    """WT-007: Deleting an ordinary file outside incoming folders must NEVER insert
    a record into incoming_files."""
    db_path = tmp_path / "test.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    library_dir = tmp_path / "library_scenes"
    library_dir.mkdir()

    worker = _make_worker(db_path, incoming)
    handler = LibraryEventHandler(
        database_path=db_path,
        worker=MagicMock(),
        notifications=False,
        incoming_worker=worker,
    )

    # File is located outside any incoming folder
    library_file = library_dir / "ordinary_scene.mp4"
    library_file.write_bytes(b"existing scene in library")
    lib_str = str(library_file.resolve())

    # Ensure it is outside incoming
    assert worker._is_inside_incoming(lib_str) is False
    assert lib_str not in worker.candidates

    # Delete the ordinary library file
    library_file.unlink()
    handler.on_deleted(FileDeletedEvent(lib_str))

    # CRITICAL: incoming_files table must remain completely empty!
    con = connect(db_path)
    row = con.execute("SELECT * FROM incoming_files WHERE path=?", (lib_str,)).fetchone()
    total_incoming = con.execute("SELECT COUNT(*) as c FROM incoming_files").fetchone()["c"]
    con.close()

    assert row is None, "Ordinary file deletion must NOT create an incoming_files record!"
    assert total_incoming == 0, "incoming_files must have 0 records!"

    # But filesystem_events MUST record the deletion event for the library
    con = connect(db_path)
    fs_evt = con.execute("SELECT * FROM filesystem_events WHERE source_path=? AND event_type='deleted'", (lib_str,)).fetchone()
    con.close()
    assert fs_evt is not None, "Library deletion event must be recorded in filesystem_events!"


def test_unrelated_library_file_deletion_still_generates_notifications_and_review(tmp_path):
    """WT-007: Ordinary library file deletion must still generate deletion notifications
    and review records if the file remains missing; absence from incoming_files must not
    suppress legitimate library events."""
    db_path = tmp_path / "test.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    library_dir = tmp_path / "library_scenes"
    library_dir.mkdir()

    worker = _make_worker(db_path, incoming)
    handler = LibraryEventHandler(
        database_path=db_path,
        worker=MagicMock(),
        notifications=True,
        incoming_worker=worker,
    )

    scene_file = library_dir / "InventoriedScene.mp4"
    scene_file.write_bytes(b"scene content")
    scene_str = str(scene_file.resolve())

    # Add scene to files inventory
    con = connect(db_path)
    con.execute(
        """INSERT INTO files(file_id, scene_id, path, basename, title, exists_on_disk, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, 1, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")""",
        ("file-101", "scene-101", scene_str, Path(scene_str).name, "Inventoried Scene")
    )
    con.commit()
    con.close()

    # Simulate creation occurred in the past (outside the 5s atomic-swap replacement window)
    with handler._recent_creates_lock:
        handler._recent_creates[scene_str] = time.monotonic() - 10.0

    scene_file.unlink()

    mock_notify = MagicMock()
    with patch("librarymanager_monitor.notify", mock_notify):
        handler.on_deleted(FileDeletedEvent(scene_str))
        # Trigger the missing check
        handler._notify_if_still_missing(scene_str)

        # Deletion notification must have been sent
        assert mock_notify.called
        call_args = mock_notify.call_args[0]
        assert "InventoriedScene.mp4" in call_args[1]

    # Activity log must have recorded external deletion
    con = connect(db_path)
    act = con.execute("SELECT * FROM activity_log WHERE old_path=? AND action='external deletion'", (scene_str,)).fetchone()
    inc_row = con.execute("SELECT * FROM incoming_files WHERE path=?", (scene_str,)).fetchone()
    con.close()

    assert act is not None, "External deletion review activity must be recorded!"
    assert inc_row is None, "incoming_files must NOT have a record for the library deletion!"


def test_save_state_does_not_insert_untracked_gone_record(tmp_path):
    """WT-007: CompletedDownloadWorker._save_state() must not insert a new row
    when marking an untracked path as 'gone'."""
    db_path = tmp_path / "test.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    worker = _make_worker(db_path, incoming)
    untracked_path = str(tmp_path / "untracked.mp4")

    # Call _save_state directly with status='gone'
    worker._save_state(untracked_path, "gone", detail="File removed from disk")

    con = connect(db_path)
    row = con.execute("SELECT * FROM incoming_files WHERE path=?", (untracked_path,)).fetchone()
    con.close()
    assert row is None, "_save_state with status='gone' must not insert a row for untracked paths!"


def test_ci_workflow_runs_full_python_and_ui_suites():
    """WT-008: Verify that GitHub Actions bundled-watchdog.yml runs the full Python suite,
    the complete UI suite, and existing bundled-watchdog smoke checks across platforms."""
    workflow_path = Path(".github/workflows/bundled-watchdog.yml")
    assert workflow_path.is_file(), "bundled-watchdog.yml must exist"

    content = workflow_path.read_text(encoding="utf-8")

    # Matrix must contain cross-platform operating systems
    assert "ubuntu-latest" in content, "Must run on Ubuntu"
    assert "macos-latest" in content, "Must run on macOS"
    assert "windows-latest" in content, "Must run on Windows"

    # Steps must contain required commands
    assert "python -m pytest tests/" in content, "CI must run the full Python suite: python -m pytest tests/"
    assert "node tests/test_ui.mjs" in content, "CI must run the complete UI suite: node tests/test_ui.mjs"
    assert "tests.test_bundled_watchdog" in content, "CI must retain the bundled-watchdog smoke test"
