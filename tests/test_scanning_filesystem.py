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


def test_to_sqlite_int_conversion():
    assert fs.to_sqlite_int(None) is None
    assert fs.to_sqlite_int(0) == 0
    assert fs.to_sqlite_int(12345) == 12345
    assert fs.to_sqlite_int(-1) == -1
    # 64-bit signed max (2**63 - 1)
    max_signed = (1 << 63) - 1
    assert fs.to_sqlite_int(max_signed) == max_signed
    # 2**63 maps to -2**63
    assert fs.to_sqlite_int(1 << 63) == -(1 << 63)
    # 2**64 - 1 maps to -1
    assert fs.to_sqlite_int((1 << 64) - 1) == -1
    # Large Docker / ZFS unsigned inode (> 2**63 - 1)
    large_ino = 18446744073709551610  # 2**64 - 6
    assert fs.to_sqlite_int(large_ino) == -6
    # Semantics: identity bijection
    a = (1 << 63) + 9999
    b = (1 << 63) + 9999
    c = (1 << 63) + 10000
    assert fs.to_sqlite_int(a) == fs.to_sqlite_int(b)
    assert fs.to_sqlite_int(a) != fs.to_sqlite_int(c)


def test_sqlite_large_inode_checksum_and_cache(test_db: Path, tmp_path: Path, monkeypatch):
    test_file = tmp_path / "large_inode_video.mp4"
    test_file.write_bytes(b"TEST_LARGE_INODE_DATA" * 50)

    # Simulate 64-bit unsigned inode and device (e.g. from Docker overlayfs or ZFS)
    large_inode = (1 << 64) - 42  # > 2**63 - 1
    large_device = (1 << 63) + 123456  # > 2**63 - 1
    real_stat = test_file.stat()

    class FakeStat:
        st_size = real_stat.st_size
        st_mtime_ns = real_stat.st_mtime_ns
        st_dev = large_device
        st_ino = large_inode

    monkeypatch.setattr(Path, "stat", lambda self: FakeStat())

    # Verify get_file_stat_snapshot safely normalizes without overflow
    snapshot = fs.get_file_stat_snapshot(test_file)
    assert snapshot is not None
    size, mtime_ns, dev, ino = snapshot
    assert dev == fs.to_sqlite_int(large_device)
    assert ino == fs.to_sqlite_int(large_inode)

    # 1. Direct get_cached_or_compute_sha256 calculation and SQLite cache insert
    sha256, status = fs.get_cached_or_compute_sha256(test_db, test_file, allow_compute=True, file_id="scene-large-1")
    assert status == "verified"
    assert sha256 is not None
    assert len(sha256) == 64

    # Verify subsequent lookup hits cache and matches verified status without OverflowError
    cached_sha, cached_status = fs.get_cached_or_compute_sha256(test_db, test_file, allow_compute=False)
    assert cached_status == "verified"
    assert cached_sha == sha256

    # 2. Enqueue checksum calculation into checksum_jobs with large inode
    queued = fs.enqueue_checksum_calculation(test_db, test_file, file_id="scene-large-2", stability_seconds=0.0)
    assert queued is True

    # 3. Claim and process the queued job
    job = fs.claim_checksum_job(test_db)
    assert job is not None
    processed = fs.process_checksum_job(test_db, job)
    assert processed is True

    # 4. Cached lookup for current path
    assert fs._cached_sha256_for_current_path(test_db, test_file) == sha256


def test_sqlite_large_mtime_ns_and_size(test_db: Path, tmp_path: Path, monkeypatch):
    test_file = tmp_path / "future_mtime_video.mp4"
    test_file.write_bytes(b"TEST_FUTURE_MTIME_DATA" * 20)

    # Simulate timestamps or corrupted sizes > 2**63 - 1 (e.g. year 3000 or unsigned wrapped off_t)
    large_mtime_ns = (1 << 63) + 987654321
    large_size = 1024

    class FakeStat:
        st_size = large_size
        st_mtime_ns = large_mtime_ns
        st_dev = 1234
        st_ino = 5678

    monkeypatch.setattr(Path, "stat", lambda self: FakeStat())

    snapshot = fs.get_file_stat_snapshot(test_file)
    assert snapshot is not None
    assert snapshot[1] == fs.to_sqlite_int(large_mtime_ns)

    # Verify no OverflowError occurs during hash cache insertion and query
    sha, status = fs.get_cached_or_compute_sha256(test_db, test_file, allow_compute=True)
    assert status == "verified"
    assert sha is not None

    cached_sha, cached_status = fs.get_cached_or_compute_sha256(test_db, test_file, allow_compute=False)
    assert cached_status == "verified"
    assert cached_sha == sha


def test_existing_cache_entries_reused_without_rehashing(test_db: Path, tmp_path: Path, monkeypatch):
    test_file = tmp_path / "cached_video.mp4"
    test_file.write_bytes(b"REUSE_CACHE_TEST_DATA" * 20)

    # First computation populates the cache
    sha1, status1 = fs.get_cached_or_compute_sha256(test_db, test_file, allow_compute=True, file_id="scene-reuse")
    assert status1 == "verified"
    assert sha1 is not None

    # Monkeypatch calculate function to raise an error if called
    def failing_hasher(path):
        raise AssertionError("calculate_sha256 should NOT be called when cache entry exists!")

    monkeypatch.setattr(fs, "get_calculate_sha256_verified_fn", lambda: failing_hasher)

    # Verify cache hit reuses the entry without recalculating
    sha2, status2 = fs.get_cached_or_compute_sha256(test_db, test_file, allow_compute=True)
    assert status2 == "verified"
    assert sha2 == sha1


def test_negative_and_excessive_file_sizes_rejected(tmp_path: Path, monkeypatch):
    test_file = tmp_path / "invalid_size.mp4"
    test_file.write_bytes(b"DATA")

    for bad_size in (-1, -100, -(1 << 30), 1 << 63, 1 << 64):
        class BadSizeStat:
            st_size = bad_size
            st_mtime_ns = 1_000_000_000
            st_dev = 1
            st_ino = 1

        monkeypatch.setattr(Path, "stat", lambda self: BadSizeStat())
        assert fs.get_file_stat_snapshot(test_file) is None
        assert fs.calculate_sha256_verified(test_file) is None


def test_pre_1970_timestamps_handled_correctly(test_db: Path, tmp_path: Path, monkeypatch):
    test_file = tmp_path / "pre_1970_video.mp4"
    test_file.write_bytes(b"PRE_1970_TEST_DATA" * 10)

    # 1969-12-31 23:59:59 UTC = -1 second = -1,000,000,000 nanoseconds
    pre_1970_ns = -1_000_000_000
    real_stat = test_file.stat()

    class Pre1970Stat:
        st_size = real_stat.st_size
        st_mtime_ns = pre_1970_ns
        st_dev = 200
        st_ino = 300

    monkeypatch.setattr(Path, "stat", lambda self: Pre1970Stat())

    snapshot = fs.get_file_stat_snapshot(test_file)
    assert snapshot is not None
    assert snapshot[1] == pre_1970_ns  # Legitimately preserved negative timestamp

    # Verify SQLite caching and lookup work with negative timestamp
    sha, status = fs.get_cached_or_compute_sha256(test_db, test_file, allow_compute=True)
    assert status == "verified"
    assert sha is not None

    cached_sha, cached_status = fs.get_cached_or_compute_sha256(test_db, test_file, allow_compute=False)
    assert cached_status == "verified"
    assert cached_sha == sha
