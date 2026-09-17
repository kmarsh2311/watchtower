"""
Tests for browser download lifecycles and safeguards in Watchtower.

Validates:
1. Complete Chrome 3-step transition (.com.google.Chrome.* -> Unconfirmed *.crdownload -> final .mp4)
   - Preserves single logical candidate
   - Never creates false pending problem events in filesystem_events
   - Never creates false 'external companion move' activity records
   - Video settles and scans automatically upon completion
2. Restart during .crdownload:
   - Candidate restored with is_temporary=True from SQLite
   - Renaming to final video after restart transitions candidate cleanly
3. Cancelled downloads:
   - File deletion removes candidate, updates DB to 'gone'
   - Does NOT generate false deletion filesystem_event or review warnings
4. Duplicate final rename events:
   - Second move event is idempotent and handled gracefully
5. Temporary files outside incoming folders:
   - Temp creation is ignored (no problem event)
   - Final rename to video registers as 'created' event (not an uninventoried move!)
6. Firefox (.part) and Safari (.download) behavior:
   - Correctly recognized as temporary downloads and transitioned upon completion
7. True companion classification safeguard:
   - Non-companion files never generate 'external companion move'
"""
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from watchdog.events import FileCreatedEvent, FileMovedEvent, FileDeletedEvent

WATCHTOWER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(WATCHTOWER_DIR))

from librarymanager_monitor import (
    CompletedDownloadWorker,
    LibraryEventHandler,
    is_temporary_download,
    VIDEO_EXTENSIONS,
    COMPANION_EXTENSIONS,
)
from librarymanager_core import connect, SCHEMA


def _make_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def _make_worker(db_path, incoming_dir, settle=2, fallback=60):
    stash = MagicMock()
    stash.call_GQL.return_value = {"findScenes": {"scenes": []}}
    stash.metadata_scan.return_value = "job-scan-123"
    stash.wait_for_job.return_value = True
    worker = CompletedDownloadWorker(
        database_path=db_path,
        stash=stash,
        incoming_folder=str(incoming_dir),
        enabled=True,
        settle_seconds=settle,
        notifications=False,
        fallback_seconds=fallback,
        track_temporary_downloads=True,
    )
    return worker


def _make_handler(db_path, worker=None, incoming_worker=None):
    mock_worker = MagicMock()
    mock_worker.transcoder_compatibility = False
    return LibraryEventHandler(
        database_path=db_path,
        worker=worker or mock_worker,
        notifications=False,
        incoming_worker=incoming_worker,
    )


def _db_rows(db, table):
    con = connect(db)
    try:
        return con.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        con.close()


def test_is_temporary_download_helper():
    """Verify is_temporary_download detects Chrome, Firefox, Safari and downloader temporary artifacts."""
    # Chrome artifacts
    assert is_temporary_download("/incoming/.com.google.Chrome.abc123") is True
    assert is_temporary_download("/incoming/com.google.Chrome.ABC123") is True
    assert is_temporary_download("/incoming/Unconfirmed 123456.crdownload") is True
    assert is_temporary_download("/incoming/download.mp4.crdownload") is True
    assert is_temporary_download(Path("/incoming/.com.google.Chrome.xyz")) is True

    # Firefox, Safari, and other downloaders
    assert is_temporary_download("/incoming/video.mp4.part") is True
    assert is_temporary_download("/incoming/video.mp4.download") is True
    assert is_temporary_download("/incoming/video.mp4.tmp") is True
    assert is_temporary_download("/incoming/video.mp4.temp") is True
    assert is_temporary_download("/incoming/video.mp4.!qb") is True

    # Non-temporary files
    assert is_temporary_download("/incoming/video.mp4") is False
    assert is_temporary_download("/incoming/video.mkv") is False
    assert is_temporary_download("/incoming/video.jpg") is False
    assert is_temporary_download("/incoming/video.nfo") is False
    assert is_temporary_download("") is False
    assert is_temporary_download(None) is False


def test_chrome_download_full_transition(tmp_path):
    """
    Verify complete 3-step Chrome download lifecycle:
    .com.google.Chrome.* -> Unconfirmed *.crdownload -> final .mp4
    Ensures:
    - Single candidate preserved across all transitions
    - No false filesystem_events or companion records created
    - Transitions to waiting and auto-scans upon completion
    """
    db = tmp_path / "test.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db)

    worker = _make_worker(db, incoming, settle=2)
    handler = _make_handler(db, incoming_worker=worker)

    # Step 1: Initial Chrome temporary file
    chrome_tmp = incoming / ".com.google.Chrome.ABC123"
    chrome_tmp.write_bytes(b"downloading chunk 1")
    chrome_tmp_str = str(chrome_tmp.resolve())

    handler.on_created(FileCreatedEvent(chrome_tmp_str))

    with worker.lock:
        assert chrome_tmp_str in worker.candidates
        cand = worker.candidates[chrome_tmp_str]
        assert cand["is_temporary"] is True

    rows = _db_rows(db, "incoming_files")
    assert len(rows) == 1
    assert rows[0]["path"] == chrome_tmp_str
    assert rows[0]["status"] == "downloading"
    first_seen_initial = rows[0]["first_seen_at"]

    # Verify NO pending filesystem_events created
    assert len(_db_rows(db, "filesystem_events")) == 0
    assert len(_db_rows(db, "activity_log")) == 0

    # Step 2: Rename to Unconfirmed *.crdownload
    crdownload = incoming / "Unconfirmed 987654.crdownload"
    crdownload_str = str(crdownload.resolve())
    chrome_tmp.rename(crdownload)
    crdownload.write_bytes(b"downloading chunk 1 + chunk 2")

    handler.on_moved(FileMovedEvent(chrome_tmp_str, crdownload_str))

    with worker.lock:
        assert chrome_tmp_str not in worker.candidates
        assert crdownload_str in worker.candidates
        cand2 = worker.candidates[crdownload_str]
        assert cand2["is_temporary"] is True

    con = connect(db)
    r_old = con.execute("SELECT status FROM incoming_files WHERE path=?", (chrome_tmp_str,)).fetchone()
    r_new = con.execute("SELECT status, first_seen_at FROM incoming_files WHERE path=?", (crdownload_str,)).fetchone()
    con.close()

    assert r_old["status"] == "moved"
    assert r_new["status"] == "downloading"
    assert r_new["first_seen_at"] == first_seen_initial, "first_seen_at must be preserved across temp renames"

    # Verify NO pending filesystem_events or companion records created
    assert len(_db_rows(db, "filesystem_events")) == 0
    assert len(_db_rows(db, "activity_log")) == 0

    # Step 3: Final rename to video .mp4
    final_video = incoming / "Awesome Movie.mp4"
    final_video_str = str(final_video.resolve())
    crdownload.rename(final_video)

    handler.on_moved(FileMovedEvent(crdownload_str, final_video_str))

    with worker.lock:
        assert crdownload_str not in worker.candidates
        assert final_video_str in worker.candidates
        cand3 = worker.candidates[final_video_str]
        assert cand3["is_temporary"] is False

    con = connect(db)
    r_cr = con.execute("SELECT status FROM incoming_files WHERE path=?", (crdownload_str,)).fetchone()
    r_final = con.execute("SELECT status, first_seen_at FROM incoming_files WHERE path=?", (final_video_str,)).fetchone()
    con.close()

    assert r_cr["status"] == "moved"
    assert r_final["status"] == "waiting"
    assert r_final["first_seen_at"] == first_seen_initial, "first_seen_at preserved for final video"

    # Verify NO problem events or companion moves logged
    assert len(_db_rows(db, "filesystem_events")) == 0
    companion_activities = [a for a in _db_rows(db, "activity_log") if a["category"] == "companion"]
    assert len(companion_activities) == 0

    # Step 4: Settle and auto-scan
    worker.stash.call_GQL.return_value = {
        "findScenes": {
            "scenes": [{
                "id": "101",
                "files": [{"path": final_video_str}],
            }]
        }
    }
    # Simulate settle timeout
    worker.evaluate_once(now=time.time() + 100)

    con = connect(db)
    r_imported = con.execute("SELECT status, scan_job_id FROM incoming_files WHERE path=?", (final_video_str,)).fetchone()
    con.close()
    assert r_imported["status"] == "imported"
    assert r_imported["scan_job_id"] == "job-scan-123"


def test_restart_during_crdownload(tmp_path):
    """
    Verify watcher restart while a file is in .crdownload state:
    - Restores candidate with is_temporary=True from SQLite
    - Subsequent final rename to video transitions candidate seamlessly
    """
    db = tmp_path / "restart.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db)

    # Worker 1 starts download
    worker1 = _make_worker(db, incoming, settle=2)
    crdownload = incoming / "Unconfirmed 444555.crdownload"
    crdownload.write_bytes(b"partial download data")
    crdownload_str = str(crdownload.resolve())

    worker1.submit(crdownload_str)
    assert crdownload_str in worker1.candidates
    assert worker1.candidates[crdownload_str]["is_temporary"] is True

    # Simulate watcher stopping / restarting:
    # Create Worker 2 against the same DB
    worker2 = _make_worker(db, incoming, settle=2)
    worker2._restore_candidates()

    assert crdownload_str in worker2.candidates
    assert worker2.candidates[crdownload_str]["is_temporary"] is True

    # Chrome finishes downloading while Worker 2 is running
    final_video = incoming / "Completed Scene.mp4"
    final_video_str = str(final_video.resolve())
    crdownload.rename(final_video)

    handler2 = _make_handler(db, incoming_worker=worker2)
    handler2.on_moved(FileMovedEvent(crdownload_str, final_video_str))

    with worker2.lock:
        assert crdownload_str not in worker2.candidates
        assert final_video_str in worker2.candidates
        assert worker2.candidates[final_video_str]["is_temporary"] is False

    con = connect(db)
    row = con.execute("SELECT status FROM incoming_files WHERE path=?", (final_video_str,)).fetchone()
    con.close()
    assert row["status"] == "waiting"


def test_cancelled_download(tmp_path):
    """
    Verify cancelled download (file deleted without renaming to video):
    - Candidate removed from worker
    - SQLite record marked as gone
    - NO false deletion filesystem_events or warnings created
    """
    db = tmp_path / "cancelled.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db)

    worker = _make_worker(db, incoming)
    handler = _make_handler(db, incoming_worker=worker)

    chrome_tmp = incoming / ".com.google.Chrome.cancel123"
    chrome_tmp.write_bytes(b"incomplete")
    chrome_tmp_str = str(chrome_tmp.resolve())

    handler.on_created(FileCreatedEvent(chrome_tmp_str))
    assert chrome_tmp_str in worker.candidates

    # User cancels download -> Chrome unlinks file
    chrome_tmp.unlink()
    handler.on_deleted(FileDeletedEvent(chrome_tmp_str))

    with worker.lock:
        assert chrome_tmp_str not in worker.candidates

    con = connect(db)
    row = con.execute("SELECT status FROM incoming_files WHERE path=?", (chrome_tmp_str,)).fetchone()
    con.close()
    assert row["status"] == "gone"

    # Crucial: NO filesystem_events recorded for deletion of temporary download
    fs_events = _db_rows(db, "filesystem_events")
    assert len(fs_events) == 0


def test_duplicate_final_rename(tmp_path):
    """
    Verify duplicate on_moved events for the final video rename:
    - First rename relocates candidate to waiting
    - Duplicate rename is safely idempotent, returning True without error or duplicate events
    """
    db = tmp_path / "duplicate.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db)

    worker = _make_worker(db, incoming)
    handler = _make_handler(db, incoming_worker=worker)

    crdownload = incoming / "Unconfirmed 111.crdownload"
    crdownload.write_bytes(b"data")
    crdownload_str = str(crdownload.resolve())

    worker.submit(crdownload_str)

    final_video = incoming / "Test Movie.mp4"
    final_video_str = str(final_video.resolve())
    crdownload.rename(final_video)

    # First event
    handler.on_moved(FileMovedEvent(crdownload_str, final_video_str))
    assert final_video_str in worker.candidates
    assert worker.candidates[final_video_str]["is_temporary"] is False

    # Second event (duplicate OS notification)
    handler.on_moved(FileMovedEvent(crdownload_str, final_video_str))
    assert final_video_str in worker.candidates

    con = connect(db)
    row = con.execute("SELECT status FROM incoming_files WHERE path=?", (final_video_str,)).fetchone()
    con.close()
    assert row["status"] == "waiting"
    assert len(_db_rows(db, "filesystem_events")) == 0


def test_temporary_files_outside_incoming_folders(tmp_path):
    """
    Verify temporary files outside incoming folders:
    - Temporary artifact creation is ignored (no problem event)
    - Final rename to video registers as 'created' (new video in library), NOT an uninventoried move
    """
    db = tmp_path / "outside.sqlite3"
    vault = tmp_path / "Vault"
    vault.mkdir()
    incoming = vault / "Incoming"
    incoming.mkdir()
    movies = vault / "Movies"
    movies.mkdir()
    _make_db(db)

    worker = _make_worker(db, incoming)
    mock_main_worker = MagicMock()
    handler = _make_handler(db, worker=mock_main_worker, incoming_worker=worker)

    # Download in movies folder (outside incoming)
    outside_tmp = movies / ".com.google.Chrome.outside123"
    outside_tmp.write_bytes(b"outside download")
    outside_tmp_str = str(outside_tmp.resolve())

    handler.on_created(FileCreatedEvent(outside_tmp_str))

    # Must NOT record a pending event for the temporary file
    assert len(_db_rows(db, "filesystem_events")) == 0

    # Finished download renamed to video
    outside_final = movies / "Outside Video.mp4"
    outside_final_str = str(outside_final.resolve())
    outside_tmp.rename(outside_final)

    handler.on_moved(FileMovedEvent(outside_tmp_str, outside_final_str))

    # Crucial: Must be recorded as 'created' (new video), NOT 'moved'
    events = _db_rows(db, "filesystem_events")
    assert len(events) == 1
    assert events[0]["event_type"] == "created"
    assert events[0]["source_path"] == outside_final_str
    # Main move worker must NOT have been called with missing source
    mock_main_worker.submit.assert_not_called()


def test_firefox_and_safari_lifecycle(tmp_path):
    """
    Verify Firefox (.part) and Safari (.download) download lifecycles:
    - Correctly classified as temporary download
    - Cleanly transferred to final video upon completion
    """
    db = tmp_path / "browsers.sqlite3"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _make_db(db)

    worker = _make_worker(db, incoming)
    handler = _make_handler(db, incoming_worker=worker)

    # 1. Firefox .part
    ff_part = incoming / "FirefoxDownload.mp4.part"
    ff_part.write_bytes(b"firefox stream data")
    ff_part_str = str(ff_part.resolve())

    handler.on_created(FileCreatedEvent(ff_part_str))
    assert worker.candidates[ff_part_str]["is_temporary"] is True

    ff_final = incoming / "FirefoxDownload.mp4"
    ff_final_str = str(ff_final.resolve())
    ff_part.rename(ff_final)

    handler.on_moved(FileMovedEvent(ff_part_str, ff_final_str))
    assert worker.candidates[ff_final_str]["is_temporary"] is False

    # 2. Safari .download
    safari_dl = incoming / "SafariDownload.mp4.download"
    safari_dl.write_bytes(b"safari stream data")
    safari_dl_str = str(safari_dl.resolve())

    handler.on_created(FileCreatedEvent(safari_dl_str))
    assert worker.candidates[safari_dl_str]["is_temporary"] is True

    safari_final = incoming / "SafariDownload.mp4"
    safari_final_str = str(safari_final.resolve())
    safari_dl.rename(safari_final)

    handler.on_moved(FileMovedEvent(safari_dl_str, safari_final_str))
    assert worker.candidates[safari_final_str]["is_temporary"] is False

    # Neither browser created spurious filesystem_events
    assert len(_db_rows(db, "filesystem_events")) == 0


def test_true_companion_classification_safeguard(tmp_path):
    """
    Verify non-companion renames are never classified as 'external companion move'.
    Only true companion extensions (.jpg, .nfo, .vtt, etc.) trigger companion activity.
    """
    db = tmp_path / "companion_safe.sqlite3"
    library = tmp_path / "Library"
    library.mkdir()
    _make_db(db)

    handler = _make_handler(db)

    # Rename random non-companion non-video file
    src_random = library / "notes.xyz"
    dest_random = library / "notes_renamed.xyz"
    src_random.write_text("test")
    src_random.rename(dest_random)

    handler.on_moved(FileMovedEvent(str(src_random), str(dest_random)))

    companion_acts = [a for a in _db_rows(db, "activity_log") if a["category"] == "companion"]
    assert len(companion_acts) == 0, "Non-companion move must NOT be logged as companion move"

    # Rename true companion file (.jpg)
    src_jpg = library / "Scene.jpg"
    dest_jpg = library / "Scene_Cover.jpg"
    src_jpg.write_bytes(b"image")
    src_jpg.rename(dest_jpg)

    handler.on_moved(FileMovedEvent(str(src_jpg), str(dest_jpg)))

    companion_acts_after = [a for a in _db_rows(db, "activity_log") if a["category"] == "companion"]
    assert len(companion_acts_after) == 1
    assert companion_acts_after[0]["action"] == "external companion move"
