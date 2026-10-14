"""Unit tests for librarymanager.scanning.filesystem and backward compatibility wrappers."""
import os
import sqlite3
import tempfile
import time
from pathlib import Path
import pytest

import librarymanager.scanning.filesystem as fs
from librarymanager.scanning import (
    associated_file_target,
    calculate_sha256,
    calculate_sha256_verified,
    claim_checksum_job,
    consume_expected_create,
    consume_expected_move,
    consume_expected_move_destination,
    consume_expected_move_source,
    enqueue_checksum_calculation,
    expect_filesystem_create,
    expect_filesystem_delete,
    expect_filesystem_move,
    get_authoritative_unavailable_roots,
    get_calculate_sha256_verified_fn,
    get_cached_or_compute_sha256,
    get_companion_files,
    get_file_stat_snapshot,
    is_actionable_incoming_file,
    is_file_on_unavailable_root,
    is_source_companion_of_moved_video,
    is_verified_companion_destination,
    process_checksum_job,
    reset_checksum_jobs_for_monitor_restart,
    strict_incoming_companions,
)
import librarymanager_core as core


@pytest.fixture
def test_db(tmp_path: Path) -> Path:
    db_file = tmp_path / "test.db"
    conn = fs.connect(db_file)
    conn.close()
    return db_file


def test_companion_constants_and_sidecar_keys():
    assert ".mp4" in fs.VIDEO_EXTENSIONS
    assert ".srt" in fs.ASSOCIATED_EXTENSIONS
    assert ".jpg" in fs.IMAGE_SIDECAR_EXTENSIONS
    assert ".srt" in fs.COMPANION_EXTENSIONS
    assert ".csm.jpg" in fs.COMPANION_EXTENSIONS

    assert fs._sidecar_match_key("Scene.Name [1080p] - Part 1") == "scenename1080ppart1"
    assert fs._sidecar_match_key("Café Münster") == "cafemunster"


def test_is_actionable_incoming_file():
    assert fs.is_actionable_incoming_file("video.mp4") is True
    assert fs.is_actionable_incoming_file("video.srt") is True
    assert fs.is_actionable_incoming_file("video.mp4.crdownload") is False
    assert fs.is_actionable_incoming_file("video.mp4", active_status="downloading") is False
    assert fs.is_actionable_incoming_file("video.mp4", active_status="completed") is True
    assert fs.is_actionable_incoming_file("") is False


def test_associated_file_target(tmp_path: Path):
    video = tmp_path / "Scene 1.mp4"
    proposed = tmp_path / "Studio - 2024-01-01 - Scene 1 [1080p].mp4"

    sub = tmp_path / "Scene 1.srt"
    target = fs.associated_file_target(sub, video, proposed)
    assert target == tmp_path / "Studio - 2024-01-01 - Scene 1 [1080p].srt"

    img = tmp_path / "Scene 1.mp4.jpg"
    target_img = fs.associated_file_target(img, video, proposed)
    assert target_img == tmp_path / "Studio - 2024-01-01 - Scene 1 [1080p].mp4.jpg"


def test_get_companion_files(tmp_path: Path):
    video = tmp_path / "MyScene.mp4"
    video.write_text("dummy video")
    (tmp_path / "MyScene.srt").write_text("sub")
    (tmp_path / "MyScene.jpg").write_text("thumb")
    (tmp_path / "Unrelated.mp4").write_text("other")

    comps = fs.get_companion_files(video)
    assert "MyScene.srt" in comps
    assert "MyScene.jpg" in comps
    assert "Unrelated.mp4" not in comps


def test_strict_incoming_companions(tmp_path: Path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    video = incoming / "video.mp4"
    video.write_bytes(b"data" * 100)
    sidecar = incoming / "video.srt"
    sidecar.write_text("1\n00:00:00 --> 00:00:01\nHi")

    result = fs.strict_incoming_companions(video, [str(incoming)])
    assert len(result) == 1
    assert result[0]["basename"] == "video.srt"
    assert result[0]["size"] > 0


def test_cold_storage_unavailable_roots(test_db: Path, tmp_path: Path):
    available_dir = tmp_path / "online"
    available_dir.mkdir()
    missing_dir = tmp_path / "offline_drive"

    unavail = fs.get_authoritative_unavailable_roots(test_db, extra_roots=[str(available_dir), str(missing_dir)])
    assert str(missing_dir) in unavail
    assert str(available_dir) not in unavail

    assert fs.is_file_on_unavailable_root(str(missing_dir / "folder" / "video.mp4"), unavailable_roots=unavail) is True
    assert fs.is_file_on_unavailable_root(str(available_dir / "folder" / "video.mp4"), unavailable_roots=unavail) is False


def test_checksum_calculation_and_queue(test_db: Path, tmp_path: Path):
    test_file = tmp_path / "sample.mp4"
    test_file.write_bytes(b"WATCHTOWER_TEST_MEDIA_DATA" * 50)

    # Calculate directly
    digest = fs.calculate_sha256(test_file)
    assert digest is not None
    assert len(digest) == 64

    # Enqueue checksum job
    queued = fs.enqueue_checksum_calculation(test_db, test_file, file_id="scene-101", stability_seconds=0.0)
    assert queued is True

    # Claim and process job
    job = fs.claim_checksum_job(test_db)
    assert job is not None
    assert job["path"] == str(test_file)

    processed = fs.process_checksum_job(test_db, job)
    assert processed is True

    # Check cached query
    cached = fs.cached_sha256_for_file_id(test_db, "scene-101")
    assert cached == digest

    current_cached = fs._cached_sha256_for_current_path(test_db, test_file)
    assert current_cached == digest


def test_expected_filesystem_events(test_db: Path, tmp_path: Path):
    src = str(tmp_path / "src.mp4")
    dst = str(tmp_path / "dst.mp4")

    fs.expect_filesystem_move(test_db, src, dst, ttl_seconds=60)
    assert fs.consume_expected_move(test_db, src, dst) is True
    assert fs.consume_expected_move(test_db, src, dst) is False

    del_file = str(tmp_path / "del.mp4")
    fs.expect_filesystem_delete(test_db, del_file, ttl_seconds=60)
    fs.cancel_expected_filesystem_delete(test_db, del_file)

    create_file = str(tmp_path / "new.mp4")
    fs.expect_filesystem_create(test_db, create_file, ttl_seconds=60)
    assert fs.consume_expected_create(test_db, create_file) is True
    assert fs.consume_expected_create(test_db, create_file) is False


def test_compatibility_forwarding_wrappers(test_db: Path, tmp_path: Path):
    test_file = tmp_path / "compat.mp4"
    test_file.write_bytes(b"COMPATIBILITY_WRAPPER_TEST")

    # Verify core forwarding wrappers work identically
    assert core.get_file_stat_snapshot(test_file) == fs.get_file_stat_snapshot(test_file)
    assert core.calculate_sha256(test_file) == fs.calculate_sha256(test_file)
    assert core.calculate_sha256_verified(test_file) == fs.calculate_sha256_verified(test_file)
    assert core.is_actionable_incoming_file(str(test_file)) is True
    assert core.is_temporary_download(str(test_file)) is False

    # Check monkeypatch interop on calculate_sha256_verified
    orig_wrapper = core.calculate_sha256_verified
    assert getattr(orig_wrapper, "_is_watchtower_wrapper", False) is True
