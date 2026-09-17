"""Tests for Watchtower image companion matching rules.

Covers:
1. Exact filename match (Scene.mp4 + Scene.jpg)
2. One video + cover.jpg
3. One video + multiple generic artwork files (cover.jpg, poster.jpg, fanart.jpg)
4. Two videos + cover.jpg (ambiguous: must NOT pair)
5. Random unrelated image filenames (must NOT attach to video)
6. Image arriving before video
7. Video arriving before image (late-arriving companion)
8. Restart while an unmatched image is present (must NOT re-submit)
9. Fallback recovery of a missed image event
10. Ambiguous images not being retried continuously (no repeated SQLite writes)
"""

import os
import time
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from librarymanager_monitor import (
    CompletedDownloadWorker,
    match_companion_to_video,
    find_scene_for_companion,
    VIDEO_EXTENSIONS,
    COMPANION_EXTENSIONS,
)
from librarymanager_core import connect, SCHEMA


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def _make_worker(db_path, incoming_dir, settle=300, fallback=60):
    stash = MagicMock()
    stash.call_GQL.return_value = {"findScenes": {"scenes": []}}
    stash.find_filepath.side_effect = lambda sc: Path(sc.get("path", ""))
    worker = CompletedDownloadWorker(
        database_path=db_path,
        stash=stash,
        incoming_folder=str(incoming_dir),
        enabled=True,
        settle_seconds=settle,
        notifications=False,
        fallback_seconds=fallback,
    )
    return worker


def _inject_companion(worker, path, settled=True):
    p = Path(path)
    stat = p.stat()
    with worker.lock:
        worker.candidates[str(p.resolve())] = {
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "stable_since": time.time() - (10 if settled else 9999),
            "attempts": 0,
            "is_companion": True,
            "is_temporary": False,
            "last_saved_status": "waiting",
            "last_saved_detail": "Waiting for matching video to arrive",
            "check_after": 0.0,
        }


def _inject_video(worker, path, settled=True):
    p = Path(path)
    stat = p.stat()
    with worker.lock:
        worker.candidates[str(p.resolve())] = {
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "stable_since": time.time() - (10 if settled else 9999),
            "attempts": 0,
            "is_companion": False,
            "is_temporary": False,
            "last_saved_status": "waiting",
            "last_saved_detail": "Waiting for video to settle",
            "check_after": 0.0,
        }


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

class TestImageCompanionMatching:

    def test_1_exact_filename_match(self, tmp_path):
        """Rule 1: If an image has the same base filename as a video, pair it automatically."""
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        video = incoming / "Scene.mp4"
        video.write_bytes(b"video content")
        companion = incoming / "Scene.jpg"
        companion.write_bytes(b"image content")

        worker = _make_worker(db_path, incoming)
        worker.stash.metadata_scan = MagicMock()

        _inject_companion(worker, companion)
        _inject_video(worker, video)

        scene = {"id": "101", "path": str(video)}
        worker._pair_companions_for_video(video, scene)

        # Companion must be paired and removed from candidates
        assert str(companion.resolve()) not in worker.candidates
        con = connect(db_path)
        row = con.execute("SELECT status FROM incoming_files WHERE path=?", (str(companion.resolve()),)).fetchone()
        con.close()
        assert row is not None
        assert row["status"] == "paired"

    def test_2_one_video_plus_cover_jpg(self, tmp_path):
        """Rule 2: Recognise generic artwork (cover.jpg) when exactly one video is in that folder."""
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        video = incoming / "Scene.mp4"
        video.write_bytes(b"video content")
        cover = incoming / "cover.jpg"
        cover.write_bytes(b"cover image")

        worker = _make_worker(db_path, incoming)
        worker.stash.metadata_scan = MagicMock()

        _inject_companion(worker, cover)
        _inject_video(worker, video)

        scene = {"id": "102", "path": str(video)}
        worker._pair_companions_for_video(video, scene)

        # cover.jpg should be paired with Scene.mp4
        assert str(cover.resolve()) not in worker.candidates
        con = connect(db_path)
        row = con.execute("SELECT status, detail FROM incoming_files WHERE path=?", (str(cover.resolve()),)).fetchone()
        con.close()
        assert row is not None
        assert row["status"] == "paired"

    def test_3_one_video_plus_multiple_generic_artwork(self, tmp_path):
        """Rule 2: Recognise multiple generic artwork files (cover.jpg, poster.jpg, fanart.jpg) with one video without collision."""
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        video = incoming / "Scene.mp4"
        video.write_bytes(b"video content")
        cover = incoming / "cover.jpg"
        cover.write_bytes(b"cover image")
        poster = incoming / "poster.jpg"
        poster.write_bytes(b"poster image")
        fanart = incoming / "fanart.jpg"
        fanart.write_bytes(b"fanart image")

        worker = _make_worker(db_path, incoming)
        worker.stash.metadata_scan = MagicMock()

        _inject_companion(worker, cover)
        _inject_companion(worker, poster)
        _inject_companion(worker, fanart)
        _inject_video(worker, video)

        scene = {"id": "103", "path": str(video)}
        worker._pair_companions_for_video(video, scene)

        # All 3 generic artwork files must be paired and distinct (no file overwrite collision)
        for art in [cover, poster, fanart]:
            assert str(art.resolve()) not in worker.candidates
            con = connect(db_path)
            row = con.execute("SELECT status FROM incoming_files WHERE path=?", (str(art.resolve()),)).fetchone()
            con.close()
            assert row is not None
            assert row["status"] == "paired"

    def test_4_two_videos_plus_cover_jpg(self, tmp_path):
        """Rule 5: If a folder contains multiple videos, generic artwork must NOT be assigned automatically."""
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        video1 = incoming / "Scene1.mp4"
        video1.write_bytes(b"video 1 content")
        video2 = incoming / "Scene2.mp4"
        video2.write_bytes(b"video 2 content")
        cover = incoming / "cover.jpg"
        cover.write_bytes(b"cover image")

        worker = _make_worker(db_path, incoming)
        worker.stash.metadata_scan = MagicMock()

        _inject_companion(worker, cover)
        _inject_video(worker, video1)
        _inject_video(worker, video2)

        scene1 = {"id": "104", "path": str(video1)}
        worker._pair_companions_for_video(video1, scene1)

        # cover.jpg must NOT pair with Scene1
        assert cover.exists(), "Physical cover.jpg file must be untouched"
        con = connect(db_path)
        row = con.execute("SELECT status, detail FROM incoming_files WHERE path=?", (str(cover.resolve()),)).fetchone()
        con.close()
        assert row is None or row["status"] != "paired"

        # When evaluated, cover.jpg should be marked unmatched/ambiguous
        candidate = worker.candidates.get(str(cover.resolve()))
        if candidate:
            worker._process_companion(str(cover.resolve()), candidate)

        con = connect(db_path)
        row = con.execute("SELECT status, detail FROM incoming_files WHERE path=?", (str(cover.resolve()),)).fetchone()
        con.close()
        assert row is not None
        assert row["status"] == "unmatched"
        assert "multiple" in row["detail"].lower() or "ambiguous" in row["detail"].lower()
        assert str(cover.resolve()) not in worker.candidates, "Ambiguous image must be popped from candidates"

    def test_5_random_unrelated_image_filenames(self, tmp_path):
        """Rule 3: Images with unrelated/random names must not attach to a video merely for being in same folder."""
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        video = incoming / "Scene.mp4"
        video.write_bytes(b"video content")
        random_img = incoming / "IMG_4920.jpg"
        random_img.write_bytes(b"unrelated photo")

        worker = _make_worker(db_path, incoming)
        worker.stash.metadata_scan = MagicMock()

        _inject_companion(worker, random_img)
        _inject_video(worker, video)

        scene = {"id": "105", "path": str(video)}
        worker._pair_companions_for_video(video, scene)

        # random_img must NOT pair with Scene
        assert random_img.exists(), "Physical image must be untouched"
        con = connect(db_path)
        row = con.execute("SELECT status FROM incoming_files WHERE path=?", (str(random_img.resolve()),)).fetchone()
        con.close()
        assert row is None or row["status"] != "paired"

        # When evaluated, random_img must be marked unmatched
        cand = worker.candidates.get(str(random_img.resolve()))
        if cand:
            worker._process_companion(str(random_img.resolve()), cand)

        con = connect(db_path)
        row = con.execute("SELECT status, detail FROM incoming_files WHERE path=?", (str(random_img.resolve()),)).fetchone()
        con.close()
        assert row is not None
        assert row["status"] == "unmatched"
        assert str(random_img.resolve()) not in worker.candidates

    def test_6_image_arriving_before_video(self, tmp_path):
        """Rule 6: Preserve companion-before-video pairing for both exact match and single-video cover.jpg."""
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        # 1. Image arrives first (folder has 0 videos)
        cover = incoming / "cover.jpg"
        cover.write_bytes(b"cover image")

        worker = _make_worker(db_path, incoming)
        worker.submit(cover)

        # While 0 videos exist, cover.jpg enters waiting
        worker.evaluate_once()
        con = connect(db_path)
        row = con.execute("SELECT status FROM incoming_files WHERE path=?", (str(cover.resolve()),)).fetchone()
        con.close()
        assert row["status"] == "waiting"
        assert str(cover.resolve()) in worker.candidates

        # 2. Later, exactly one video arrives
        video = incoming / "Scene.mp4"
        video.write_bytes(b"video content")
        worker.submit(video)

        # Video scan completes and pairs with waiting cover.jpg
        scene = {"id": "106", "path": str(video)}
        worker._pair_companions_for_video(video, scene)

        assert str(cover.resolve()) not in worker.candidates
        con = connect(db_path)
        row = con.execute("SELECT status FROM incoming_files WHERE path=?", (str(cover.resolve()),)).fetchone()
        con.close()
        assert row["status"] == "paired"

    def test_7_video_arriving_before_image(self, tmp_path):
        """Rule 6: Preserve video-before-companion pairing (late companion arriving after scene imported in DB)."""
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        video = incoming / "Scene.mp4"
        video.write_bytes(b"video content")

        # Insert scene into inventory DB as already imported
        con = connect(db_path)
        con.execute(
            "INSERT INTO files(file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at) VALUES(?, ?, ?, ?, ?, 1, datetime('now'), datetime('now'))",
            ("f1", "201", str(video), "Scene.mp4", 100)
        )
        con.commit()
        con.close()

        worker = _make_worker(db_path, incoming)
        worker.stash.metadata_scan = MagicMock()

        # Late-arriving generic image: cover.jpg in single-video folder
        cover = incoming / "cover.jpg"
        cover.write_bytes(b"image")
        worker.submit(cover)
        with worker.lock:
            worker.candidates[str(cover.resolve())]["stable_since"] = time.time() - 10

        worker.evaluate_once()

        assert str(cover.resolve()) not in worker.candidates
        con = connect(db_path)
        row = con.execute("SELECT status FROM incoming_files WHERE path=?", (str(cover.resolve()),)).fetchone()
        con.close()
        assert row is not None
        assert row["status"] == "paired"

    def test_8_restart_while_unmatched_image_present(self, tmp_path):
        """Rule 6: Unmatched image must NOT be re-submitted or restored into candidates on restart."""
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        unrelated = incoming / "random_pic.jpg"
        unrelated.write_bytes(b"random")

        # Pre-record as unmatched in DB
        con = connect(db_path)
        con.execute(
            """INSERT INTO incoming_files(path, first_seen_at, last_checked_at, size, modified_ns, status, detail)
               VALUES(?, datetime('now'), datetime('now'), 10, 100, 'unmatched', 'Unrelated image')""",
            (str(unrelated.resolve()),)
        )
        con.commit()
        con.close()

        worker = _make_worker(db_path, incoming)
        worker._restore_candidates()

        # Must NOT be loaded into candidates
        assert str(unrelated.resolve()) not in worker.candidates

        # Must NOT be re-submitted by fallback check
        worker._fallback_check()
        assert str(unrelated.resolve()) not in worker.candidates

        # Physical file must still exist untouched
        assert unrelated.exists()

    def test_9_fallback_recovery_of_missed_image_event(self, tmp_path):
        """Rule 6: Fallback recovery still rediscovers an unrecorded image companion missed by watchdog."""
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        video = incoming / "Scene.mp4"
        video.write_bytes(b"video")
        companion = incoming / "Scene.jpg"
        companion.write_bytes(b"companion")

        worker = _make_worker(db_path, incoming)
        # Missed watchdog event: companion exists on disk with recent mtime
        worker.started_at = time.time() - 10

        worker._fallback_check()

        # Fallback check should discover companion
        assert str(companion.resolve()) in worker.candidates

    def test_10_ambiguous_images_not_retried_continuously(self, tmp_path):
        """Rule 4: Stop repeatedly retrying ambiguous images; no repeated SQLite writes."""
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        video1 = incoming / "Scene1.mp4"
        video1.write_bytes(b"video 1")
        video2 = incoming / "Scene2.mp4"
        video2.write_bytes(b"video 2")
        cover = incoming / "cover.jpg"
        cover.write_bytes(b"cover")

        worker = _make_worker(db_path, incoming)
        worker.submit(cover)
        worker.submit(video1)
        worker.submit(video2)
        with worker.lock:
            worker.candidates[str(cover.resolve())]["stable_since"] = time.time() - 10

        # Run evaluate_once to process cover.jpg with multiple videos present
        worker.evaluate_once()

        con = connect(db_path)
        row = con.execute("SELECT status, last_checked_at FROM incoming_files WHERE path=?", (str(cover.resolve()),)).fetchone()
        con.close()
        assert row is not None
        assert row["status"] == "unmatched"
        saved_timestamp = row["last_checked_at"]

        # Multiple subsequent evaluate_once cycles must NOT re-touch or update SQLite for this file
        for _ in range(5):
            worker.evaluate_once()

        con = connect(db_path)
        row2 = con.execute("SELECT status, last_checked_at FROM incoming_files WHERE path=?", (str(cover.resolve()),)).fetchone()
        con.close()
        assert row2["status"] == "unmatched"
        assert row2["last_checked_at"] == saved_timestamp, "SQLite must not be rewritten for unmatched image"

    def test_11_dismiss_ignored_preserves_physical_file_and_stops_retries(self, tmp_path):
        """Rule 2: Dismissing an item marks it ignored, does not delete/modify file, and removes from retries."""
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        video = incoming / "Scene.mp4"
        video.write_bytes(b"video")
        unmatched_img = incoming / "unrelated.jpg"
        unmatched_img.write_bytes(b"original image bytes")

        worker = _make_worker(db_path, incoming)
        worker.submit(unmatched_img)
        worker.submit(video)
        with worker.lock:
            worker.candidates[str(unmatched_img.resolve())]["stable_since"] = time.time() - 10

        worker.evaluate_once()

        # Unmatched image is processed
        con = connect(db_path)
        row = con.execute("SELECT status FROM incoming_files WHERE path=?", (str(unmatched_img.resolve()),)).fetchone()
        assert row["status"] == "unmatched"

        # Dismiss / Ignore action occurs (simulating handleDismissIncoming)
        con.execute("UPDATE incoming_files SET status='ignored', detail='Ignored by user' WHERE path=?", (str(unmatched_img.resolve()),))
        con.commit()
        con.close()

        # 1. Physical file must NOT be deleted, moved, or renamed
        assert unmatched_img.exists(), "Physical file must NOT be deleted"
        assert unmatched_img.read_bytes() == b"original image bytes"

        # 2. Worker must not retry or process it
        worker.evaluate_once()
        assert str(unmatched_img.resolve()) not in worker.candidates

        # 3. Fallback check must not re-submit it
        worker._fallback_check()
        assert str(unmatched_img.resolve()) not in worker.candidates

        con = connect(db_path)
        row2 = con.execute("SELECT status FROM incoming_files WHERE path=?", (str(unmatched_img.resolve()),)).fetchone()
        con.close()
        assert row2["status"] == "ignored"

    def test_12_retry_recovers_ignored_item_to_waiting(self, tmp_path):
        """Rule 3: A dismissed/ignored item can be retried and re-evaluated against folder contents."""
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        db_path = tmp_path / "test.sqlite3"
        _make_db(db_path)

        cover = incoming / "cover.jpg"
        cover.write_bytes(b"cover artwork")

        # Pre-record as ignored in DB
        con = connect(db_path)
        con.execute(
            """INSERT INTO incoming_files(path, first_seen_at, last_checked_at, size, modified_ns, status, detail)
               VALUES(?, datetime('now'), datetime('now'), 10, 100, 'ignored', 'Ignored by user')""",
            (str(cover.resolve()),)
        )
        con.commit()
        con.close()

        worker = _make_worker(db_path, incoming)
        assert str(cover.resolve()) not in worker.candidates

        # Later, a matching single video arrives
        video = incoming / "Scene.mp4"
        video.write_bytes(b"video")

        # User chooses Retry in UI: status is reset to 'waiting' and file is touched
        con = connect(db_path)
        con.execute("UPDATE incoming_files SET status='waiting', attempts=0, stable_since=?, detail='User requested re-scan' WHERE path=?",
                    (time.time(), str(cover.resolve())))
        con.commit()
        con.close()
        os.utime(str(cover.resolve()), None)

        # Worker picks up retried file
        worker._fallback_check()
        assert str(cover.resolve()) in worker.candidates, "Retried item must be re-admitted to candidates"

        # Video scan completes; cover.jpg is now paired
        scene = {"id": "202", "path": str(video)}
        worker._pair_companions_for_video(video, scene)

        assert str(cover.resolve()) not in worker.candidates
        con = connect(db_path)
        row = con.execute("SELECT status FROM incoming_files WHERE path=?", (str(cover.resolve()),)).fetchone()
        con.close()
        assert row["status"] == "paired"
