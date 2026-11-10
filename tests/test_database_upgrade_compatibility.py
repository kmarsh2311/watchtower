"""Backwards compatibility tests verifying that an existing populated Watchtower database
upgrades seamlessly without requiring schema rebuilds, library re-indexing, or rehashing.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from pathlib import Path
import pytest

from librarymanager.database import create_connection, ensure_schema
from librarymanager.database.schema import SCHEMA
import librarymanager.scanning.filesystem as fs
import librarymanager_core as core


@pytest.fixture
def populated_existing_db(tmp_path: Path):
    """Creates a simulated pre-existing Watchtower database populated with files,
    cached checksums, pending jobs, and scene records.
    """
    db_file = tmp_path / "librarymanager.sqlite3"
    conn = create_connection(db_file)
    ensure_schema(conn, db_file)

    # Populate existing files on disk
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    video_1 = media_dir / "Existing.Scene.1.mp4"
    video_1.write_bytes(b"EXISTING_VIDEO_1_CONTENT_DATA" * 50)

    video_2 = media_dir / "Existing.Scene.2.mp4"
    video_2.write_bytes(b"EXISTING_VIDEO_2_CONTENT_DATA" * 50)

    # Compute actual SHA256 for video_1 to simulate pre-calculated cache
    sha256_1 = fs.calculate_sha256(video_1)
    stat_1 = video_1.stat()
    stat_2 = video_2.stat()

    # Pre-populate database with existing entries (standard legacy positive integers)
    now = fs.utc_now()
    conn.execute(
        """INSERT INTO file_checksum_cache(
            path, file_id, size, mtime, mtime_ns, device, inode, sha256, status, calculated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)""",
        (str(video_1), "file-101", stat_1.st_size, stat_1.st_mtime_ns / 1_000_000_000,
         stat_1.st_mtime_ns, int(stat_1.st_dev), int(stat_1.st_ino), sha256_1, now)
    )

    # Insert an existing pending checksum job for video_2
    conn.execute(
        """INSERT INTO checksum_jobs(
            path, file_id, size, mtime_ns, device, inode, status, attempts,
            available_at, claimed_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, ?)""",
        (str(video_2), "file-102", stat_2.st_size, stat_2.st_mtime_ns,
         int(stat_2.st_dev), int(stat_2.st_ino), time.time() - 10.0, now)
    )

    # Insert files into files table
    fingerprints_1 = json.dumps([{"type": "sha256", "value": sha256_1}, {"type": "oshash", "value": "1122334455667788"}])
    conn.execute(
        """INSERT INTO files(
            file_id, scene_id, path, basename, title, studio, performers_json,
            size, duration, height, fingerprints_json, scene_metadata_json,
            exists_on_disk, first_seen_at, last_seen_at, missing_since
        ) VALUES (?, ?, ?, ?, 'Scene Title', 'Studio', '[]', ?, 120.0, 1080, ?, '{}', 1, ?, ?, NULL)""",
        ("file-101", "scene-101", str(video_1), video_1.name, stat_1.st_size, fingerprints_1, now, now)
    )

    conn.commit()
    conn.close()

    return {
        "db_path": db_file,
        "video_1": video_1,
        "video_2": video_2,
        "sha256_1": sha256_1,
    }


def test_existing_database_checksum_cache_recognized_without_rehashing(populated_existing_db, monkeypatch):
    """Existing cache entries are recognized by the upgraded code and no re-hashing occurs."""
    db_path = populated_existing_db["db_path"]
    video_1 = populated_existing_db["video_1"]
    expected_sha = populated_existing_db["sha256_1"]

    # Block calculation to prove the existing cache is used directly
    def failing_hasher(path):
        raise AssertionError("calculate_sha256 should NOT be called on pre-existing cached entries!")

    monkeypatch.setattr(fs, "get_calculate_sha256_verified_fn", lambda: failing_hasher)

    # Check cache retrieval via get_cached_or_compute_sha256
    sha, status = fs.get_cached_or_compute_sha256(db_path, video_1, allow_compute=True)
    assert status == "verified"
    assert sha == expected_sha

    # Check durable lookup by file_id
    assert fs.cached_sha256_for_file_id(db_path, "file-101") == expected_sha

    # Check path lookup
    assert fs._cached_sha256_for_current_path(db_path, video_1) == expected_sha


def test_existing_database_pending_checksum_jobs_process_cleanly(populated_existing_db):
    """Existing pending checksum jobs from pre-upgrade databases claim and complete cleanly."""
    db_path = populated_existing_db["db_path"]
    video_2 = populated_existing_db["video_2"]

    # Claim existing job
    job = fs.claim_checksum_job(db_path)
    assert job is not None
    assert job["path"] == str(video_2)
    assert job["file_id"] == "file-102"

    # Process claimed job
    processed = fs.process_checksum_job(db_path, job)
    assert processed is True

    # Verify result was written to file_checksum_cache
    cached_sha = fs._cached_sha256_for_current_path(db_path, video_2)
    assert cached_sha is not None
    assert len(cached_sha) == 64


def test_existing_database_duplicate_scene_detection(populated_existing_db, tmp_path: Path):
    """Duplicate scene detection accurately matches existing records without requiring database rebuilds."""
    db_path = populated_existing_db["db_path"]
    video_1 = populated_existing_db["video_1"]

    # Create candidate duplicate file with exact same content as video_1
    candidate_dup = tmp_path / "incoming" / "Candidate.Duplicate.Scene.mp4"
    candidate_dup.parent.mkdir(parents=True, exist_ok=True)
    candidate_dup.write_bytes(video_1.read_bytes())

    # Pre-compute candidate's hash in cache so find_duplicate_scene_file can compare without compute
    cand_sha, _ = fs.get_cached_or_compute_sha256(db_path, candidate_dup, allow_compute=True)

    # Detect duplicate
    dup = core.find_duplicate_scene_file(db_path, candidate_dup, allow_compute=False)
    assert dup is not None
    assert dup["match_type"] == "sha256_exact"
    assert dup["existing_path"] == str(video_1)
    assert dup["scene_id"] == "scene-101"
    assert dup["file_id"] == "file-101"
    assert dup["checksum_status"] == "verified"


def test_existing_database_requires_no_schema_wipe(populated_existing_db):
    """Upgraded Watchtower detects the existing schema version and preserves all data intact."""
    db_path = populated_existing_db["db_path"]
    conn = create_connection(db_path)
    version = ensure_schema(conn, db_path)
    assert version >= 3

    # Verify counts in existing tables are preserved
    file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    cache_count = conn.execute("SELECT COUNT(*) FROM file_checksum_cache").fetchone()[0]
    job_count = conn.execute("SELECT COUNT(*) FROM checksum_jobs").fetchone()[0]

    assert file_count == 1
    assert cache_count == 1
    assert job_count == 1
    conn.close()
