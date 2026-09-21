import pytest
from pathlib import Path
from librarymanager_core import (
    connect,
    get_backlog_items,
    snapshot_incoming_baseline,
    is_temporary_download,
    is_actionable_incoming_file,
    utc_now,
)


def test_is_temporary_download_recognizes_all_patterns():
    assert is_temporary_download("movie.mp4.crdownload") is True
    assert is_temporary_download("movie.mp4.part") is True
    assert is_temporary_download("movie.mp4.partial") is True
    assert is_temporary_download("movie.mp4.download") is True
    assert is_temporary_download("movie.mp4.tmp") is True
    assert is_temporary_download("movie.mp4.!qb") is True
    assert is_temporary_download("unconfirmed 12345.crdownload") is True
    assert is_temporary_download(".com.google.chrome.12345") is True
    assert is_temporary_download("movie.mp4") is False
    assert is_temporary_download("movie.jpg") is False


def test_temporary_download_extensions_excluded_from_dynamic_scan(tmp_path):
    db = tmp_path / "test.db"
    inc = tmp_path / "Incoming"
    inc.mkdir()

    # Create temporary/incomplete files
    (inc / "downloading.mp4.crdownload").write_bytes(b"temp crdownload")
    (inc / "torrent.mp4.!qb").write_bytes(b"temp qb")
    (inc / "browser.part").write_bytes(b"temp part")
    (inc / "unconfirmed 9876.crdownload").write_bytes(b"temp unconfirmed")
    (inc / "temp_file.tmp").write_bytes(b"temp tmp")

    # And one real completed video
    real_video = inc / "Completed Video.mp4"
    real_video.write_bytes(b"real completed video")

    config = {"incomingFolders": [str(inc)]}

    res = get_backlog_items(db, None, config=config)

    # Only the completed video should be present in the organiser
    assert res["total_count"] == 1
    assert res["eligible_count"] == 1
    assert res["items"][0]["path"] == str(real_video)
    assert res["items"][0]["status"] == "eligible"


def test_active_incoming_lifecycle_states_excluded_from_organiser(tmp_path):
    db = tmp_path / "test.db"
    inc = tmp_path / "Incoming"
    inc.mkdir()

    settling_video = inc / "Settling Video.mp4"
    settling_video.write_bytes(b"settling video content")

    scanning_video = inc / "Scanning Video.mp4"
    scanning_video.write_bytes(b"scanning video content")

    generating_video = inc / "Generating Sheet Video.mp4"
    generating_video.write_bytes(b"generating video content")

    ready_video = inc / "Ready Video.mp4"
    ready_video.write_bytes(b"ready video content")

    # Record active lifecycle states in incoming_files table
    conn = connect(db)
    conn.execute(
        """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, settle_seconds)
           VALUES (?, ?, ?, 'waiting', 300)""",
        (str(settling_video), utc_now(), utc_now()),
    )
    conn.execute(
        """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, settle_seconds)
           VALUES (?, ?, ?, 'scanning', 300)""",
        (str(scanning_video), utc_now(), utc_now()),
    )
    conn.execute(
        """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, settle_seconds)
           VALUES (?, ?, ?, 'generating_sheet', 300)""",
        (str(generating_video), utc_now(), utc_now()),
    )
    conn.commit()
    conn.close()

    config = {"incomingFolders": [str(inc)]}
    res = get_backlog_items(db, None, config=config)

    # Only Ready Video.mp4 should be discovered as actionable
    assert res["total_count"] == 1
    assert res["eligible_count"] == 1
    assert res["items"][0]["path"] == str(ready_video)


def test_finished_download_settling_appears_without_new_baseline(tmp_path):
    db = tmp_path / "test.db"
    inc = tmp_path / "Incoming"
    inc.mkdir()

    temp_video = inc / "New Video.mp4.crdownload"
    temp_video.write_bytes(b"downloading in progress")

    config = {"incomingFolders": [str(inc)]}

    # Organiser scan while downloading
    res1 = get_backlog_items(db, None, config=config)
    assert res1["total_count"] == 0

    # Download completes: browser renames to final .mp4
    final_video = inc / "New Video.mp4"
    temp_video.rename(final_video)

    # Next refresh immediately shows the finished video as eligible without requiring a new baseline snapshot
    res2 = get_backlog_items(db, None, config=config)
    assert res2["total_count"] == 1
    assert res2["eligible_count"] == 1
    assert res2["items"][0]["path"] == str(final_video)
    assert res2["items"][0]["status"] == "eligible"
