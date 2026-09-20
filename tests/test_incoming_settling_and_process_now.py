from __future__ import annotations
import time
from pathlib import Path
import pytest
from unittest.mock import MagicMock

from librarymanager_core import (
    connect,
    utc_now,
    incoming_summary,
    process_incoming_file_now,
    record_activity,
)
from librarymanager_monitor import CompletedDownloadWorker


@pytest.fixture
def test_env(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    conn.close()
    incoming_dir = tmp_path / "Incoming"
    incoming_dir.mkdir(parents=True, exist_ok=True)
    return {
        "db_path": db_path,
        "incoming_dir": incoming_dir,
    }


def test_settling_deadline_and_countdown_accuracy(test_env):
    db_path = test_env["db_path"]
    incoming_dir = test_env["incoming_dir"]
    test_video = incoming_dir / "sample_video.mp4"
    test_video.write_bytes(b"1234567890")

    now = time.time()
    stable_since = now - 20.0  # 20 seconds ago
    settle_seconds = 60        # 60s total wait
    now_str = utc_now()

    stat = test_video.stat()
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, size, modified_ns, stable_since, settle_seconds, detail)
           VALUES (?, ?, ?, 'waiting', ?, ?, ?, ?, ?)""",
        (str(test_video), now_str, now_str, stat.st_size, stat.st_mtime_ns, stable_since, settle_seconds, "Waiting for video to remain unchanged")
    )
    conn.commit()
    conn.close()

    summary = incoming_summary(db_path)
    assert summary.get("waiting") == 1
    active = summary.get("active", [])
    assert len(active) == 1
    item = active[0]

    expected_deadline = stable_since + settle_seconds
    assert item["settling_deadline"] == pytest.approx(expected_deadline, abs=0.5)
    assert item["settle_seconds"] == settle_seconds
    assert item["remaining_seconds"] == pytest.approx(40, abs=1.0)


def test_file_modification_resets_countdown_and_safe_process_now(test_env):
    db_path = test_env["db_path"]
    incoming_dir = test_env["incoming_dir"]
    test_video = incoming_dir / "active_download.mp4"
    test_video.write_bytes(b"initial_chunk")

    now = time.time()
    stable_since = now - 50.0
    settle_seconds = 60
    now_str = utc_now()

    stat = test_video.stat()
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, size, modified_ns, stable_since, settle_seconds, detail)
           VALUES (?, ?, ?, 'waiting', ?, ?, ?, ?, ?)""",
        (str(test_video), now_str, now_str, stat.st_size, stat.st_mtime_ns, stable_since, settle_seconds, "Waiting")
    )
    conn.commit()
    conn.close()

    # Modify the file on disk (simulate ongoing download appending bytes)
    time.sleep(0.01)
    test_video.write_bytes(b"initial_chunk_plus_new_chunk_of_data")

    # Attempt process_incoming_file_now
    result = process_incoming_file_now(db_path, str(test_video))
    assert result["success"] is False
    assert result.get("restarted") is True
    assert "File was modified on disk" in result["error"]

    # Verify DB state was updated with new stat and reset stable_since
    conn = connect(db_path)
    row = conn.execute("SELECT * FROM incoming_files WHERE path=?", (str(test_video),)).fetchone()
    conn.close()

    assert row["size"] == len(b"initial_chunk_plus_new_chunk_of_data")
    assert row["stable_since"] > stable_since
    assert "settling restarted" in row["detail"]

    # Verify incoming_summary now shows reset countdown (close to 60s remaining)
    summary = incoming_summary(db_path)
    assert summary.get("waiting") == 1
    active = summary.get("active", [])
    assert len(active) == 1
    assert active[0]["remaining_seconds"] > 50


def test_monitor_restart_preserves_settling_deadline(test_env):
    db_path = test_env["db_path"]
    incoming_dir = test_env["incoming_dir"]
    test_video = incoming_dir / "stable_download.mp4"
    test_video.write_bytes(b"completed_bytes")

    now = time.time()
    original_stable_since = now - 30.0
    settle_seconds = 60
    now_str = utc_now()

    stat = test_video.stat()
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, size, modified_ns, stable_since, settle_seconds, detail)
           VALUES (?, ?, ?, 'waiting', ?, ?, ?, ?, ?)""",
        (str(test_video), now_str, now_str, stat.st_size, stat.st_mtime_ns, original_stable_since, settle_seconds, "Waiting")
    )
    conn.commit()
    conn.close()

    # Simulate monitor startup / restore candidates
    stash_mock = MagicMock()
    worker = CompletedDownloadWorker(
        database_path=db_path,
        stash=stash_mock,
        incoming_folder=str(incoming_dir),
        enabled=True,
        settle_seconds=settle_seconds,
        notifications=False
    )
    worker._restore_candidates()

    # Verify worker restored the exact stable_since from DB
    candidate = worker.candidates.get(str(test_video))
    assert candidate is not None
    assert candidate["stable_since"] == pytest.approx(original_stable_since, abs=0.01)

    # Verify incoming_summary remains accurate across restarts
    summary = incoming_summary(db_path)
    active = summary.get("active", [])
    assert len(active) == 1
    assert active[0]["settling_deadline"] == pytest.approx(original_stable_since + settle_seconds, abs=0.01)


def test_process_incoming_file_now_success_and_monitor_integration(test_env):
    db_path = test_env["db_path"]
    incoming_dir = test_env["incoming_dir"]
    test_video = incoming_dir / "user_approved.mp4"
    test_video.write_bytes(b"ready_for_import")

    now = time.time()
    stable_since = now - 10.0
    settle_seconds = 300
    now_str = utc_now()

    stat = test_video.stat()
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, size, modified_ns, stable_since, settle_seconds, detail)
           VALUES (?, ?, ?, 'waiting', ?, ?, ?, ?, ?)""",
        (str(test_video), now_str, now_str, stat.st_size, stat.st_mtime_ns, stable_since, settle_seconds, "Waiting")
    )
    conn.commit()
    conn.close()

    # Setup worker with in-memory candidate
    stash_mock = MagicMock()
    worker = CompletedDownloadWorker(
        database_path=db_path,
        stash=stash_mock,
        incoming_folder=str(incoming_dir),
        enabled=True,
        settle_seconds=settle_seconds,
        notifications=False
    )
    worker._restore_candidates()
    assert worker.candidates[str(test_video)]["stable_since"] == stable_since

    # User clicks "Process Now"
    result = process_incoming_file_now(db_path, str(test_video))
    assert result["success"] is True
    assert "Settling delay bypassed" in result["message"]

    # Verify DB stable_since is fast-forwarded to before (now - settle_seconds)
    conn = connect(db_path)
    row = conn.execute("SELECT * FROM incoming_files WHERE path=?", (str(test_video),)).fetchone()
    conn.close()
    assert row["stable_since"] < (now - settle_seconds)

    # Mock _scan to verify evaluate_once picks it up immediately
    worker._scan = MagicMock()
    worker.evaluate_once()

    assert worker._scan.called
    assert worker._scan.call_args[0][0] == str(test_video)


def test_process_incoming_file_now_validation_and_duplicate_clicks(test_env):
    db_path = test_env["db_path"]
    incoming_dir = test_env["incoming_dir"]
    now_str = utc_now()

    # 1. Non-existent file
    res = process_incoming_file_now(db_path, "/non/existent/video.mp4")
    assert res["success"] is False
    assert "does not exist on disk" in res["error"]

    # 2. File exists on disk but not in incoming queue
    untracked_file = incoming_dir / "untracked.mp4"
    untracked_file.write_bytes(b"test")
    res = process_incoming_file_now(db_path, str(untracked_file))
    assert res["success"] is False
    assert "not in the incoming queue" in res["error"]

    # 3. File in 'scanning' state
    scanning_file = incoming_dir / "scanning.mp4"
    scanning_file.write_bytes(b"test")
    stat = scanning_file.stat()
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, size, modified_ns, stable_since, settle_seconds, detail)
           VALUES (?, ?, ?, 'scanning', ?, ?, ?, ?, ?)""",
        (str(scanning_file), now_str, now_str, stat.st_size, stat.st_mtime_ns, time.time(), 60, "Scanning in Stash")
    )
    conn.commit()
    conn.close()

    res = process_incoming_file_now(db_path, str(scanning_file))
    assert res["success"] is False
    assert "already being processed (scanning)" in res["error"]

    # 4. File in 'imported' state
    imported_file = incoming_dir / "imported.mp4"
    imported_file.write_bytes(b"test")
    stat = imported_file.stat()
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, size, modified_ns, stable_since, settle_seconds, detail)
           VALUES (?, ?, ?, 'imported', ?, ?, ?, ?, ?)""",
        (str(imported_file), now_str, now_str, stat.st_size, stat.st_mtime_ns, time.time(), 60, "Imported")
    )
    conn.commit()
    conn.close()

    res = process_incoming_file_now(db_path, str(imported_file))
    assert res["success"] is False
    assert "already been imported" in res["error"]

    # 5. Duplicate clicks on waiting file
    waiting_file = incoming_dir / "dup_click.mp4"
    waiting_file.write_bytes(b"test")
    stat = waiting_file.stat()
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, size, modified_ns, stable_since, settle_seconds, detail)
           VALUES (?, ?, ?, 'waiting', ?, ?, ?, ?, ?)""",
        (str(waiting_file), now_str, now_str, stat.st_size, stat.st_mtime_ns, time.time(), 60, "Waiting")
    )
    conn.commit()
    conn.close()

    # First click
    res1 = process_incoming_file_now(db_path, str(waiting_file))
    assert res1["success"] is True

    # Simulate transition to scanning by monitor
    conn = connect(db_path)
    conn.execute("UPDATE incoming_files SET status='scanning' WHERE path=?", (str(waiting_file),))
    conn.commit()
    conn.close()

    # Second click during scanning
    res2 = process_incoming_file_now(db_path, str(waiting_file))
    assert res2["success"] is False
    assert "already being processed" in res2["error"]
