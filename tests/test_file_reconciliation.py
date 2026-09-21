"""Comprehensive Regression and Unit Tests for Watchtower File Reconciliation (Phase 1).

Covers:
- Ambiguous duplicate detection (multiple matches requiring manual review, never picking first)
- Checksum caching and non-blocking performance (no re-hashing during rapid dashboard polling)
- Cross-volume creation and deletion event ordering (create-before-delete and delete-before-create)
- Stash connection errors, deletion safety check failures, and confirmed scene deletion
- Current verified evidence for companion cleanup vs stale historical move records
- Missing companion extension types (.funscript, .srt, .vtt, .nfo, .json, .csm.jpg)
- Keep Both execution safeguards
- Inaccessible, offline, and changing/unstable file handling
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock
import pytest

import librarymanager_core
from librarymanager_core import (
    connect,
    calculate_sha256,
    calculate_sha256_verified,
    get_cached_or_compute_sha256,
    enqueue_checksum_calculation,
    claim_checksum_job,
    process_next_checksum_job,
    reset_checksum_jobs_for_monitor_restart,
    cached_sha256_for_file_id,
    inventory,
    get_file_stat_snapshot,
    get_companion_files,
    find_duplicate_scene_file,
    inspect_backlog_duplicate,
    strict_incoming_companions,
    get_backlog_items,
    acknowledge_backlog_missing,
    evaluate_backlog_batch,
    is_source_companion_of_moved_video,
    pending_filesystem_events,
    annotate_pending_events_processing_state,
    reconcile_filesystem_events,
    COMPANION_EXTENSIONS,
    SCHEMA,
)


def test_backlog_missing_items_can_be_rechecked_or_acknowledged_without_deleting_history(tmp_path: Path):
    db_path = create_test_db(tmp_path / "watchtower.db")
    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    missing_video = incoming / "intentionally removed.mp4"
    missing_companion = incoming / "restored later.jpg"
    missing_video.write_bytes(b"video")
    missing_companion.write_bytes(b"image")
    librarymanager_core.snapshot_incoming_baseline(db_path, [str(incoming)])
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO filing_proposals(
               file_id,scene_id,source_path,proposed_path,destination_folder,destination_filename,
               organize_by,matched_entity_id,matched_entity_name,match_source,reason,status,created_at,updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("old-file", "12", str(missing_video), str(tmp_path / "Filed" / missing_video.name),
         str(tmp_path / "Filed"), missing_video.name, "performer", "p12", "Example",
         "scene_performer", "test completed proposal", "completed", "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    conn.close()
    missing_video.unlink()
    missing_companion.unlink()

    before = get_backlog_items(db_path, None, config={"incomingFolders": [str(incoming)]})
    assert before["missing_count"] == 2

    result = acknowledge_backlog_missing(db_path, [str(missing_video)])
    assert result["acknowledged"] == [str(missing_video)]
    after_ack = get_backlog_items(db_path, None, config={"incomingFolders": [str(incoming)]})
    assert after_ack["missing_count"] == 1
    assert after_ack["acknowledged_missing_count"] == 1
    assert after_ack["needs_attention_count"] == 1
    items = {item["path"]: item for item in after_ack["items"]}
    assert items[str(missing_video)]["status"] == "acknowledged_missing"

    missing_companion.write_bytes(b"image")
    after_restore = get_backlog_items(db_path, None, config={"incomingFolders": [str(incoming)]})
    assert after_restore["missing_count"] == 0
    assert after_restore["acknowledged_missing_count"] == 1
    assert {row["path"] for row in connect(db_path).execute("SELECT path FROM filing_incoming_baseline")} == {
        str(missing_video), str(missing_companion)
    }
from librarymanager import (
    assert_scene_removal_safe,
    validate_filesystem_scan_action,
    delete_verified_backlog_duplicate,
)


def create_test_db(db_path: Path) -> Path:
    """Initialize a test database with full schema including file_checksum_cache."""
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def seed_scene(
    db_path: Path,
    scene_id: int,
    file_id: int,
    file_path: Path,
    title: str = "Test Scene",
    size: int | None = None,
    sha256: str | None = None,
    oshash: str | None = None,
    exists_on_disk: int = 1,
):
    """Seed a scene and file entry into the test database."""
    conn = connect(db_path)
    try:
        p_str = str(file_path)
        actual_size = size if size is not None else (file_path.stat().st_size if file_path.exists() else 100000)
        fingerprints = []
        if oshash:
            fingerprints.append({"type": "oshash", "value": oshash})
        if sha256:
            fingerprints.append({"type": "sha256", "value": sha256})

        conn.execute(
            """INSERT OR REPLACE INTO files(
                file_id, scene_id, path, basename, title, studio, performers_json,
                size, duration, fingerprints_json, scene_metadata_json, exists_on_disk,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            (
                str(file_id),
                str(scene_id),
                p_str,
                file_path.name,
                title,
                "Test Studio",
                actual_size,
                600.0,
                json.dumps(fingerprints),
                json.dumps({"title": title}),
                exists_on_disk,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class DuplicateRepairStash:
    def __init__(self, scene, delete_path: Path | None = None):
        self.scene = scene
        self.delete_path = delete_path
        self.delete_calls = []

    def find_scene(self, scene_id):
        assert str(scene_id) == str(self.scene["id"])
        return self.scene

    def call_GQL(self, query, variables):
        if "deleteFiles" not in query:
            return {"findScene": self.scene}
        self.delete_calls.append((query, variables))
        if self.delete_path is not None:
            self.delete_path.unlink()
        deleted_ids = {str(file_id) for file_id in variables.get("ids", [])}
        self.scene["files"] = [
            item for item in self.scene.get("files", []) if str(item.get("id")) not in deleted_ids
        ]
        return {"deleteFiles": True}


def _prepare_same_scene_duplicate(tmp_path: Path):
    db_path = create_test_db(tmp_path / "duplicate-repair.db")
    incoming = tmp_path / "Incoming"
    organised = tmp_path / "Library"
    incoming.mkdir()
    organised.mkdir()
    candidate = incoming / "scene.mp4"
    retained = organised / "organised.mov"
    payload = b"EXACT_DUPLICATE" * 10000
    candidate.write_bytes(payload)
    retained.write_bytes(payload)
    companion = incoming / "scene.mp4.jpg"
    companion.write_bytes(b"cover")
    unrelated = incoming / "scene holiday.jpg"
    unrelated.write_bytes(b"unrelated")
    seed_scene(db_path, 700, 7001, candidate, oshash="same-oshash")
    seed_scene(db_path, 700, 7002, retained, oshash="same-oshash")
    scene = {
        "id": "700",
        "files": [
            {"id": "7001", "path": str(candidate), "size": candidate.stat().st_size,
             "fingerprints": [{"type": "oshash", "value": "same-oshash"}]},
            {"id": "7002", "path": str(retained), "size": retained.stat().st_size,
             "fingerprints": [{"type": "oshash", "value": "same-oshash"}]},
        ],
    }
    return db_path, incoming, candidate, retained, companion, unrelated, scene


def test_duplicate_repair_is_demand_driven_and_companions_are_exact(tmp_path: Path):
    db_path, incoming, candidate, retained, companion, unrelated, scene = _prepare_same_scene_duplicate(tmp_path)
    stash = DuplicateRepairStash(scene)
    config = {"incomingFolders": [str(incoming)], "autoFilingEnabled": True}
    conn = connect(db_path)
    try:
        conn.execute("UPDATE files SET size=NULL WHERE scene_id='700'")
        conn.commit()
    finally:
        conn.close()

    first = inspect_backlog_duplicate(db_path, stash, candidate, config, request_verification=False)
    assert first["checksum_status"] == "pending"
    assert [item["path"] for item in first["companions"]] == [str(companion)]
    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM checksum_jobs").fetchone()[0] == 0
    finally:
        conn.close()

    queued = inspect_backlog_duplicate(db_path, stash, candidate, config, request_verification=True)
    assert queued["checksum_status"] == "pending"
    conn = connect(db_path)
    try:
        jobs = conn.execute("SELECT path,file_id,status FROM checksum_jobs ORDER BY path").fetchall()
        assert {(row["path"], row["file_id"]) for row in jobs} == {
            (str(candidate), "7001"), (str(retained), "7002")
        }
        conn.execute("UPDATE checksum_jobs SET available_at=0")
        conn.commit()
    finally:
        conn.close()
    assert process_next_checksum_job(db_path) is True
    assert process_next_checksum_job(db_path) is True

    verified = inspect_backlog_duplicate(db_path, stash, candidate, config, request_verification=False)
    assert verified["checksum_status"] == "verified"
    assert verified["sha256"]
    assert unrelated.exists()


def test_backlog_evaluation_classifies_same_scene_copy_for_duplicate_review(tmp_path: Path):
    db_path, incoming, candidate, _retained, companion, _unrelated, scene = _prepare_same_scene_duplicate(tmp_path)
    stash = DuplicateRepairStash(scene)
    config = {"incomingFolders": [str(incoming)], "autoFilingEnabled": True}
    conn = connect(db_path)
    try:
        stat = candidate.stat()
        conn.execute(
            """INSERT INTO filing_incoming_baseline(path,size,modified_ns,oshash,seen_at)
               VALUES (?,?,?,?,?)""",
            (str(candidate), stat.st_size, stat.st_mtime_ns, "same-oshash", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            """INSERT INTO filing_baseline_state(
                   established_at,completed_at,incoming_folders_json,file_count,status
               ) VALUES (?,?,?,?, 'complete')""",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", json.dumps([str(incoming)]), 1),
        )
        conn.commit()
    finally:
        conn.close()

    backlog = get_backlog_items(db_path, None, config=config)
    assert backlog["duplicate_review_count"] == 1
    assert backlog["eligible_count"] == 0
    backlog_item = next(item for item in backlog["items"] if item["path"] == str(candidate))
    assert backlog_item["status"] == "duplicate_candidate"
    assert backlog_item["duplicate_info"]["retained_file_id"] == "7002"
    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM checksum_jobs").fetchone()[0] == 0
    finally:
        conn.close()

    result = evaluate_backlog_batch(db_path, stash, [str(candidate)], config=config)
    assert result["tally"]["duplicate_review"] == 1, result
    item = result["results"][0]
    assert item["outcome"] == "duplicate_review"
    assert item["scene_id"] == "700"
    assert item["duplicate_info"]["candidate_file_id"] == "7001"
    assert item["duplicate_info"]["retained_file_id"] == "7002"
    assert item["duplicate_info"]["checksum_status"] == "pending"
    assert [entry["path"] for entry in item["duplicate_info"]["companions"]] == [str(companion)]
    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM checksum_jobs").fetchone()[0] == 0
    finally:
        conn.close()


def test_delete_exact_duplicate_uses_stash_and_optional_companion_selection(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("librarymanager.audit", lambda *args, **kwargs: None)
    db_path, incoming, candidate, retained, companion, unrelated, scene = _prepare_same_scene_duplicate(tmp_path)
    stash = DuplicateRepairStash(scene, delete_path=candidate)
    config = {"incomingFolders": [str(incoming)]}
    conn = connect(db_path)
    try:
        for baseline_path in (candidate, companion):
            stat = baseline_path.stat()
            conn.execute(
                """INSERT INTO filing_incoming_baseline(path,size,modified_ns,oshash,seen_at)
                   VALUES (?,?,?,?,?)""",
                (str(baseline_path), stat.st_size, stat.st_mtime_ns, "same-oshash", "2026-01-01T00:00:00+00:00"),
            )
        conn.commit()
    finally:
        conn.close()
    pending = inspect_backlog_duplicate(db_path, stash, candidate, config, request_verification=True)
    conn = connect(db_path)
    try:
        conn.execute("UPDATE checksum_jobs SET available_at=0")
        conn.commit()
    finally:
        conn.close()
    process_next_checksum_job(db_path)
    process_next_checksum_job(db_path)
    verified = inspect_backlog_duplicate(db_path, stash, candidate, config)

    with pytest.raises(ValueError, match="ownership, or checksum changed"):
        delete_verified_backlog_duplicate(
            db_path, stash, str(candidate), config, "700", "WRONG", "7002",
            verified["sha256"], verified["companions"],
        )
    assert stash.delete_calls == []
    assert candidate.exists()

    # A macOS/Stash .delete rename can race with the plugin operation and create
    # the warning before the GraphQL deletion returns. Successful verified repair
    # must resolve that warning without touching the retained scene file.
    librarymanager_core.record_filesystem_event(db_path, "deleted", str(candidate))
    result = delete_verified_backlog_duplicate(
        db_path, stash, str(candidate), config, "700", "7001", "7002",
        verified["sha256"], verified["companions"],
    )
    assert result["success"] is True
    assert not candidate.exists()
    assert not companion.exists()
    assert retained.exists()
    assert unrelated.exists()
    assert stash.delete_calls[0][1] == {"ids": ["7001"]}
    assert pending_filesystem_events(db_path) == []
    conn = connect(db_path)
    try:
        repair = conn.execute(
            "SELECT * FROM duplicate_file_repairs WHERE candidate_path=?", (str(candidate),)
        ).fetchone()
        assert repair["status"] == "completed"
        assert json.loads(repair["deleted_companions_json"]) == [str(companion)]
    finally:
        conn.close()
    backlog = get_backlog_items(db_path, None, config=config)
    assert backlog["missing_count"] == 0
    assert backlog["resolved_duplicate_count"] == 2
    repaired = {item["path"]: item["status"] for item in backlog["items"]}
    assert repaired[str(candidate)] == "duplicate_removed"
    assert repaired[str(companion)] == "duplicate_removed"


def test_duplicate_repair_refuses_a_candidate_outside_incoming(tmp_path: Path):
    db_path, incoming, candidate, retained, _companion, _unrelated, scene = _prepare_same_scene_duplicate(tmp_path)
    stash = DuplicateRepairStash(scene)
    with pytest.raises(ValueError, match="only permits duplicate repair"):
        inspect_backlog_duplicate(
            db_path, stash, retained, {"incomingFolders": [str(incoming)]}, request_verification=True
        )
    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM checksum_jobs").fetchone()[0] == 0
    finally:
        conn.close()


def test_companions_are_preserved_when_checkbox_is_not_selected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("librarymanager.audit", lambda *args, **kwargs: None)
    db_path, incoming, candidate, retained, companion, _unrelated, scene = _prepare_same_scene_duplicate(tmp_path)
    stash = DuplicateRepairStash(scene, delete_path=candidate)
    config = {"incomingFolders": [str(incoming)]}
    inspect_backlog_duplicate(db_path, stash, candidate, config, request_verification=True)
    conn = connect(db_path)
    try:
        conn.execute("UPDATE checksum_jobs SET available_at=0")
        conn.commit()
    finally:
        conn.close()
    process_next_checksum_job(db_path)
    process_next_checksum_job(db_path)
    verified = inspect_backlog_duplicate(db_path, stash, candidate, config)
    delete_verified_backlog_duplicate(
        db_path, stash, str(candidate), config, "700", "7001", "7002",
        verified["sha256"], [],
    )
    assert not candidate.exists()
    assert companion.exists()
    assert retained.exists()


# =========================================================================
# 1. Ambiguous Matches
# =========================================================================

def test_multiple_identical_candidates_ambiguity(tmp_path: Path):
    """When multiple Stash scenes have identical size and SHA-256, return ambiguous result."""
    db_path = create_test_db(tmp_path / "test.db")

    dir1 = tmp_path / "Folder1"
    dir1.mkdir()
    video1 = dir1 / "scene_a.mp4"
    video1.write_bytes(b"IDENTICAL_CONTENT_FOR_MULTIPLE_SCENES" * 2000)

    dir2 = tmp_path / "Folder2"
    dir2.mkdir()
    video2 = dir2 / "scene_b.mp4"
    video2.write_bytes(b"IDENTICAL_CONTENT_FOR_MULTIPLE_SCENES" * 2000)

    sha256_val = calculate_sha256(video1)

    # Seed two distinct scenes pointing to identical files
    seed_scene(db_path, scene_id=101, file_id=1001, file_path=video1, title="Scene 101", sha256=sha256_val)
    seed_scene(db_path, scene_id=102, file_id=1002, file_path=video2, title="Scene 102", sha256=sha256_val)

    # Candidate file in a third folder
    dir3 = tmp_path / "Folder3"
    dir3.mkdir()
    candidate = dir3 / "copy.mp4"
    candidate.write_bytes(b"IDENTICAL_CONTENT_FOR_MULTIPLE_SCENES" * 2000)

    # Pre-cache sha256 so test evaluates verified matches
    get_cached_or_compute_sha256(db_path, candidate, allow_compute=True)
    get_cached_or_compute_sha256(db_path, video1, allow_compute=True)
    get_cached_or_compute_sha256(db_path, video2, allow_compute=True)

    dup = find_duplicate_scene_file(db_path, candidate, allow_compute=False)

    assert dup is not None
    assert dup["is_ambiguous"] is True
    assert dup["ambiguous_count"] == 2
    assert dup["scene_id"] is None  # Never blindly pick first scene
    assert len(dup["all_candidates"]) == 2
    matched_scene_ids = {c["scene_id"] for c in dup["all_candidates"]}
    assert matched_scene_ids == {"101", "102"}


# =========================================================================
# 2. NAS Performance & Checksum Caching
# =========================================================================

def test_repeated_dashboard_polling_no_rehash(tmp_path: Path):
    """Rapid polling uses persistent SQLite cache in O(1) without re-hashing."""
    db_path = create_test_db(tmp_path / "test.db")

    video = tmp_path / "sample.mp4"
    video.write_bytes(b"DATA" * 50000)

    # First computation with allow_compute=True
    sha1, status1 = get_cached_or_compute_sha256(db_path, video, allow_compute=True)
    assert status1 == "verified"
    assert sha1 is not None

    # Verify persistent row exists in file_checksum_cache
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM file_checksum_cache WHERE path=?", (str(video),)).fetchone()
        assert row is not None
        assert row["sha256"] == sha1
        assert row["status"] == "completed"
    finally:
        conn.close()

    # Subsequent call with allow_compute=False (as done by dashboard polling)
    start_t = time.perf_counter()
    sha2, status2 = get_cached_or_compute_sha256(db_path, video, allow_compute=False)
    duration = time.perf_counter() - start_t

    assert status2 == "verified"
    assert sha2 == sha1
    assert duration < 0.05  # Instantaneous cache hit


def test_dashboard_request_only_queues_checksum_work(tmp_path: Path, monkeypatch):
    """A cache miss during dashboard rendering never reads the video body."""
    db_path = create_test_db(tmp_path / "test.db")
    video = tmp_path / "dashboard.mp4"
    video.write_bytes(b"DASHBOARD_MUST_NOT_HASH" * 10000)

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("dashboard attempted a synchronous full-file hash")

    monkeypatch.setattr(librarymanager_core, "calculate_sha256_verified", forbidden_hash)
    checksum, status = get_cached_or_compute_sha256(
        db_path, video, allow_compute=False, file_id="4101"
    )
    assert checksum is None
    assert status == "pending"
    conn = connect(db_path)
    try:
        job = conn.execute(
            "SELECT file_id,status FROM checksum_jobs WHERE path=?", (str(video),)
        ).fetchone()
    finally:
        conn.close()
    assert job["file_id"] == "4101"
    assert job["status"] == "pending"


def test_checksum_job_survives_monitor_restart(tmp_path: Path):
    """A job claimed by a terminated monitor is returned to the durable queue."""
    db_path = create_test_db(tmp_path / "test.db")
    video = tmp_path / "restart.mp4"
    video.write_bytes(b"RESTART_SAFE" * 10000)

    assert enqueue_checksum_calculation(db_path, video, file_id="4201", stability_seconds=0)
    claimed = claim_checksum_job(db_path)
    assert claimed is not None
    assert claimed["file_id"] == "4201"

    assert reset_checksum_jobs_for_monitor_restart(db_path) == 1
    assert process_next_checksum_job(db_path) is True

    conn = connect(db_path)
    try:
        job = conn.execute("SELECT status FROM checksum_jobs WHERE id=?", (claimed["id"],)).fetchone()
        cache = conn.execute(
            "SELECT file_id,status,sha256 FROM file_checksum_cache WHERE path=?", (str(video),)
        ).fetchone()
    finally:
        conn.close()
    assert job["status"] == "completed"
    assert cache["file_id"] == "4201"
    assert cache["status"] == "completed"
    assert cache["sha256"]


def test_inventory_does_not_queue_whole_library_checksums(tmp_path: Path, monkeypatch):
    """Ordinary inventory records metadata without scheduling or reading every video."""
    db_path = create_test_db(tmp_path / "test.db")
    video = tmp_path / "library.mp4"
    video.write_bytes(b"INVENTORY_BASELINE" * 10000)
    scenes = [{
        "id": "421",
        "title": "Baseline",
        "files": [{
            "id": "4211", "path": str(video), "basename": video.name,
            "size": video.stat().st_size, "duration": 10.0, "fingerprints": [],
        }],
    }]

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("inventory attempted to hash a library video")

    monkeypatch.setattr(librarymanager_core, "calculate_sha256_verified", forbidden_hash)
    inventory(db_path, scenes)
    conn = connect(db_path)
    try:
        job_count = conn.execute("SELECT COUNT(*) FROM checksum_jobs").fetchone()[0]
        cache_count = conn.execute("SELECT COUNT(*) FROM file_checksum_cache").fetchone()[0]
    finally:
        conn.close()
    assert job_count == 0
    assert cache_count == 0
    assert reset_checksum_jobs_for_monitor_restart(db_path) == 0


def test_scene_refresh_does_not_queue_checksum_work(tmp_path: Path):
    """Hook-driven inventory refreshes also avoid whole-library checksum scheduling."""
    db_path = create_test_db(tmp_path / "test.db")
    video = tmp_path / "refreshed.mp4"
    video.write_bytes(b"REFRESH_WITHOUT_HASH" * 1000)
    scene = {
        "id": "422", "title": "Refresh",
        "files": [{
            "id": "4221", "path": str(video), "basename": video.name,
            "size": video.stat().st_size, "duration": 10.0, "fingerprints": [],
        }],
    }
    librarymanager_core.refresh_scene_inventory(db_path, scene)
    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM checksum_jobs").fetchone()[0] == 0
    finally:
        conn.close()


def test_candidate_verification_is_demand_driven_and_sequential(tmp_path: Path, monkeypatch):
    """Only a relevant candidate and its same-size source are queued, one job per worker claim."""
    db_path = create_test_db(tmp_path / "test.db")
    source = tmp_path / "Library" / "source.mp4"
    source.parent.mkdir()
    source.write_bytes(b"DEMAND_DRIVEN" * 10000)
    seed_scene(db_path, scene_id=423, file_id=4231, file_path=source)
    candidate = tmp_path / "Candidate" / "copy.mp4"
    candidate.parent.mkdir()
    candidate.write_bytes(source.read_bytes())

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("dashboard candidate lookup attempted synchronous hashing")

    original_hash = librarymanager_core.calculate_sha256_verified
    monkeypatch.setattr(librarymanager_core, "calculate_sha256_verified", forbidden_hash)
    evidence = find_duplicate_scene_file(db_path, candidate, allow_compute=False)
    assert evidence["checksum_status"] == "pending"
    for _ in range(4):
        assert find_duplicate_scene_file(db_path, candidate, allow_compute=False)["checksum_status"] == "pending"

    conn = connect(db_path)
    try:
        jobs = conn.execute(
            "SELECT path,file_id,status FROM checksum_jobs ORDER BY path"
        ).fetchall()
        conn.execute("UPDATE checksum_jobs SET available_at=0")
        conn.commit()
    finally:
        conn.close()
    assert len(jobs) == 2
    assert {job["path"] for job in jobs} == {str(source), str(candidate)}
    assert next(job for job in jobs if job["path"] == str(source))["file_id"] == "4231"

    monkeypatch.setattr(librarymanager_core, "calculate_sha256_verified", original_hash)
    assert process_next_checksum_job(db_path) is True
    conn = connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM checksum_jobs WHERE status='completed'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
    assert process_next_checksum_job(db_path) is True
    assert process_next_checksum_job(db_path) is False
    verified = find_duplicate_scene_file(db_path, candidate, allow_compute=False)
    assert verified["checksum_status"] == "verified"


def test_checksum_queue_is_bounded(tmp_path: Path):
    """A burst of candidates cannot create an unbounded NAS read backlog."""
    db_path = create_test_db(tmp_path / "test.db")
    accepted = []
    for index in range(librarymanager_core.CHECKSUM_MAX_ACTIVE_JOBS + 3):
        video = tmp_path / f"candidate-{index}.mp4"
        video.write_bytes((f"candidate-{index}" * 1000).encode())
        accepted.append(enqueue_checksum_calculation(db_path, video, stability_seconds=0))

    assert sum(accepted) == librarymanager_core.CHECKSUM_MAX_ACTIVE_JOBS
    conn = connect(db_path)
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM checksum_jobs WHERE status IN ('pending','retry','processing')"
        ).fetchone()[0]
    finally:
        conn.close()
    assert active == librarymanager_core.CHECKSUM_MAX_ACTIVE_JOBS

    assert process_next_checksum_job(db_path) is True
    deferred = tmp_path / "candidate-deferred.mp4"
    deferred.write_bytes(b"NOW_THERE_IS_CAPACITY" * 1000)
    assert enqueue_checksum_calculation(db_path, deferred, stability_seconds=0) is True


def test_concurrent_checksum_consumers_claim_only_once(tmp_path: Path, monkeypatch):
    """Concurrent requests coalesce and only one worker reads a large file."""
    db_path = create_test_db(tmp_path / "test.db")
    video = tmp_path / "single-flight.mp4"
    video.write_bytes(b"SINGLE_FLIGHT" * 20000)

    for _ in range(5):
        assert enqueue_checksum_calculation(db_path, video, file_id="4301", stability_seconds=0)

    calls = 0
    calls_lock = threading.Lock()
    original = librarymanager_core.calculate_sha256_verified

    def counted_hash(path, chunk_size=65536):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return original(path, chunk_size)

    monkeypatch.setattr(librarymanager_core, "calculate_sha256_verified", counted_hash)
    results = []

    def consume():
        results.append(process_next_checksum_job(db_path))

    workers = [threading.Thread(target=consume) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert calls == 1
    assert sorted(results) == [False, True]
    conn = connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM checksum_jobs WHERE path=?", (str(video),)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_offline_and_changing_nas_files(tmp_path: Path):
    """Inaccessible and changing files are handled gracefully without crashing."""
    db_path = create_test_db(tmp_path / "test.db")

    non_existent = tmp_path / "non_existent.mp4"
    sha, status = get_cached_or_compute_sha256(db_path, non_existent, allow_compute=False)
    assert status == "offline"
    assert sha is None

    # Empty file
    empty_file = tmp_path / "empty.mp4"
    empty_file.write_bytes(b"")
    sha_empty, status_empty = get_cached_or_compute_sha256(db_path, empty_file, allow_compute=True)
    assert status_empty == "empty"
    assert sha_empty is None


def test_file_changed_after_claim_remains_unverified(tmp_path: Path):
    """A stat identity change supersedes the claimed hash job and creates no cache entry."""
    db_path = create_test_db(tmp_path / "test.db")
    video = tmp_path / "changing.mp4"
    video.write_bytes(b"BEFORE" * 10000)
    assert enqueue_checksum_calculation(db_path, video, file_id="4302", stability_seconds=0)
    job = claim_checksum_job(db_path)
    assert job is not None

    video.write_bytes(b"AFTER_DIFFERENT_SIZE" * 10000)
    assert librarymanager_core.process_checksum_job(db_path, job) is False

    conn = connect(db_path)
    try:
        old_status = conn.execute(
            "SELECT status FROM checksum_jobs WHERE id=?", (job["id"],)
        ).fetchone()["status"]
        replacement = conn.execute(
            "SELECT status,file_id FROM checksum_jobs WHERE path=? ORDER BY id DESC LIMIT 1",
            (str(video),),
        ).fetchone()
        cache_count = conn.execute(
            "SELECT COUNT(*) FROM file_checksum_cache WHERE path=?", (str(video),)
        ).fetchone()[0]
    finally:
        conn.close()
    assert old_status == "superseded"
    assert replacement["status"] == "pending"
    assert replacement["file_id"] == "4302"
    assert cache_count == 0


def test_replaced_file_with_same_size_and_mtime_is_rehashed(tmp_path: Path):
    """A new inode cannot inherit a completed checksum merely by preserving size and mtime."""
    db_path = create_test_db(tmp_path / "test.db")
    video = tmp_path / "replacement.mp4"
    video.write_bytes(b"OLD-CONTENT")
    original_stat = video.stat()
    old_hash, status = get_cached_or_compute_sha256(
        db_path, video, allow_compute=True, file_id="4303"
    )
    assert status == "verified"

    video.unlink()
    video.write_bytes(b"NEW-CONTENT")
    os.utime(video, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert video.stat().st_size == original_stat.st_size
    assert video.stat().st_mtime_ns == original_stat.st_mtime_ns

    checksum, status = get_cached_or_compute_sha256(
        db_path, video, allow_compute=False, file_id="4303"
    )
    assert checksum is None
    assert status == "pending"
    conn = connect(db_path)
    try:
        conn.execute("UPDATE checksum_jobs SET available_at=0 WHERE path=?", (str(video),))
        conn.commit()
    finally:
        conn.close()
    assert process_next_checksum_job(db_path) is True

    new_hash, status = get_cached_or_compute_sha256(
        db_path, video, allow_compute=False, file_id="4303"
    )
    assert status == "verified"
    assert new_hash != old_hash


def test_existing_checksum_schema_migrates_before_file_id_index(tmp_path: Path):
    """An existing Phase 1 cache gains identity columns without startup failure."""
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    try:
        legacy.execute(
            """CREATE TABLE file_checksum_cache(
                   path TEXT PRIMARY KEY,size INTEGER NOT NULL,mtime REAL NOT NULL,
                   sha256 TEXT,oshash TEXT,status TEXT NOT NULL DEFAULT 'completed',
                   calculated_at TEXT NOT NULL
               )"""
        )
        legacy.commit()
    finally:
        legacy.close()

    migrated = connect(db_path)
    try:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(file_checksum_cache)")}
        indexes = {row[1] for row in migrated.execute("PRAGMA index_list(file_checksum_cache)")}
    finally:
        migrated.close()
    assert {"file_id", "mtime_ns", "device", "inode"}.issubset(columns)
    assert "idx_checksum_cache_file_id" in indexes


# =========================================================================
# 3. Cross-Volume Move Ordering
# =========================================================================

def test_cross_volume_move_event_ordering(tmp_path: Path):
    """Cross-volume move detected regardless of create/delete watcher event arrival order."""
    db_path = create_test_db(tmp_path / "test.db")

    src = tmp_path / "Vol1" / "movie.mp4"
    src.parent.mkdir()
    content = b"CROSS_VOLUME_BYTES" * 3000
    src.write_bytes(content)

    sha256_val = calculate_sha256(src)
    seed_scene(db_path, scene_id=888, file_id=999, file_path=src, title="Move Scene", sha256=sha256_val)

    dst = tmp_path / "Vol2" / "movie.mp4"
    dst.parent.mkdir()
    dst.write_bytes(content)

    # Source deleted
    src.unlink()

    get_cached_or_compute_sha256(db_path, dst, allow_compute=True)

    dup = find_duplicate_scene_file(db_path, dst, allow_compute=False)
    assert dup is not None
    assert dup["scene_id"] == "888"
    assert dup["is_external_move"] is True
    assert dup["match_type"] == "sha256_exact"


def test_source_hash_remains_bound_to_file_id_after_source_disappears(tmp_path: Path):
    """A background source checksum can identify a move after the old path is gone."""
    db_path = create_test_db(tmp_path / "test.db")
    source = tmp_path / "Source" / "movie.mp4"
    source.parent.mkdir()
    source.write_bytes(b"PERSISTENT_SOURCE_IDENTITY" * 5000)
    seed_scene(db_path, scene_id=440, file_id=4401, file_path=source)

    assert enqueue_checksum_calculation(db_path, source, file_id="4401", stability_seconds=0)
    assert process_next_checksum_job(db_path) is True
    source_hash = cached_sha256_for_file_id(db_path, "4401", source.stat().st_size)
    assert source_hash

    destination = tmp_path / "OtherVolume" / "movie.mp4"
    destination.parent.mkdir()
    destination.write_bytes(source.read_bytes())
    source.unlink()
    assert enqueue_checksum_calculation(db_path, destination, stability_seconds=0)
    assert process_next_checksum_job(db_path) is True

    duplicate = find_duplicate_scene_file(db_path, destination, allow_compute=False)
    assert duplicate is not None
    assert duplicate["scene_id"] == "440"
    assert duplicate["file_id"] == "4401"
    assert duplicate["is_external_move"] is True


def test_external_move_cannot_start_scan_or_clear_event(tmp_path: Path):
    """Verified external moves remain pending and are blocked from generic scans."""
    db_path = create_test_db(tmp_path / "test.db")
    source = tmp_path / "Source" / "protected.mp4"
    source.parent.mkdir()
    source.write_bytes(b"DO_NOT_REASSIGN" * 5000)
    seed_scene(db_path, scene_id=450, file_id=4501, file_path=source)
    assert enqueue_checksum_calculation(db_path, source, file_id="4501", stability_seconds=0)
    assert process_next_checksum_job(db_path) is True

    destination = tmp_path / "Destination" / "protected.mp4"
    destination.parent.mkdir()
    destination.write_bytes(source.read_bytes())
    source.unlink()
    assert enqueue_checksum_calculation(db_path, destination, stability_seconds=0)
    assert process_next_checksum_job(db_path) is True

    event_key = "external-move-review"
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO filesystem_events(
                   event_key,event_type,source_path,destination_path,is_directory,
                   first_seen_at,last_seen_at,status
               ) VALUES (?, 'created', ?, NULL, 0, datetime('now'), datetime('now'), 'pending')""",
            (event_key, str(destination)),
        )
        conn.commit()
        event = dict(conn.execute(
            "SELECT * FROM filesystem_events WHERE event_key=?", (event_key,)
        ).fetchone())
    finally:
        conn.close()

    with pytest.raises(ValueError, match="review-only"):
        validate_filesystem_scan_action(db_path, event, "scan_destination")
    with pytest.raises(ValueError, match="review-only"):
        validate_filesystem_scan_action(db_path, event, "keep_both")

    conn = connect(db_path)
    try:
        status = conn.execute(
            "SELECT status FROM filesystem_events WHERE event_key=?", (event_key,)
        ).fetchone()["status"]
    finally:
        conn.close()
    assert status == "pending"


def test_unverified_external_move_never_calls_stash_scan(tmp_path: Path):
    """A size-only missing-source candidate cannot reach Stash metadata scanning."""
    db_path = create_test_db(tmp_path / "test.db")
    missing_source = tmp_path / "Missing" / "unverified.mp4"
    candidate = tmp_path / "Found" / "unverified.mp4"
    candidate.parent.mkdir()
    candidate.write_bytes(b"UNVERIFIED_MOVE" * 5000)
    seed_scene(
        db_path, scene_id=451, file_id=4511, file_path=missing_source,
        size=candidate.stat().st_size, exists_on_disk=0,
    )
    event = {"source_path": str(candidate), "destination_path": None}
    stash = MagicMock()

    with pytest.raises(ValueError, match="review-only"):
        validate_filesystem_scan_action(db_path, event, "scan_destination")

    stash.metadata_scan.assert_not_called()
    evidence = find_duplicate_scene_file(db_path, candidate, allow_compute=False)
    assert evidence["checksum_status"] == "unverified"
    assert evidence["match_type"] == "source_checksum_unavailable"
    assert evidence["is_external_move"] is True
    conn = connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM checksum_jobs").fetchone()[0] == 0
    finally:
        conn.close()


# =========================================================================
# 4. Deletion Safety Checks & Assertion Verification
# =========================================================================

def test_deletion_safety_assertions(tmp_path: Path):
    """Verify assert_scene_removal_safe prevents deletion when safety rules are violated."""
    deleted_video = tmp_path / "absent_video.mp4"

    # 1. Missing files array in scene
    with pytest.raises(ValueError, match="could not verify this scene's current files"):
        assert_scene_removal_safe({"id": "1"}, str(deleted_video))

    # 2. File returned to disk
    returned_video = tmp_path / "returned.mp4"
    returned_video.write_bytes(b"DATA")
    scene_with_returned = {"id": "2", "files": [{"path": str(returned_video)}]}
    with pytest.raises(ValueError, match="video file exists again"):
        assert_scene_removal_safe(scene_with_returned, str(returned_video))

    # 3. Scene has multiple video files attached
    other_file = tmp_path / "other.mp4"
    scene_multi_files = {
        "id": "3",
        "files": [{"path": str(deleted_video)}, {"path": str(other_file)}]
    }
    with pytest.raises(ValueError, match="another attached video file"):
        assert_scene_removal_safe(scene_multi_files, str(deleted_video))

    # 4. Safe single-file missing scene passes without error
    scene_single_safe = {"id": "4", "files": [{"path": str(deleted_video)}]}
    assert_scene_removal_safe(scene_single_safe, str(deleted_video))


# =========================================================================
# 5. Companion Evidence Verification & Extension Unification
# =========================================================================

def test_companion_extensions_unification():
    """Verify all sidecar/companion types are unified."""
    required_exts = {
        ".funscript", ".srt", ".vtt", ".scc", ".ttml", ".dfxp", ".lrc", ".txt",
        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".nfo", ".json", ".xml", ".sub", ".idx",
        ".csm.jpg", ".csm.png", ".csm.webp"
    }
    for ext in required_exts:
        assert ext in COMPANION_EXTENSIONS, f"Missing {ext} in COMPANION_EXTENSIONS"


def test_stale_companion_history_rejection(tmp_path: Path):
    """A filename match and stale move history cannot hide a companion deletion."""
    db_path = create_test_db(tmp_path / "test.db")

    src_dir = tmp_path / "Src"
    src_dir.mkdir()
    src_comp = src_dir / "clip.funscript"

    dst_dir = tmp_path / "Dst"
    dst_dir.mkdir()
    dst_vid = dst_dir / "clip.mp4"
    dst_vid.write_bytes(b"VIDEO")

    seed_scene(db_path, scene_id=301, file_id=3001, file_path=dst_vid, exists_on_disk=1)

    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO activity_log(
                   category,action,status,old_path,new_path,file_id,scene_id,recorded_at
               ) VALUES ('reconciliation','targeted Stash scan','updated',?,?,?,?,?)""",
            (str(src_dir / "clip.mp4"), str(dst_vid), "3001", "301", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            """INSERT INTO filesystem_events(
                   event_key,event_type,source_path,destination_path,is_directory,
                   first_seen_at,last_seen_at,status
               ) VALUES ('stale-companion','deleted',?,NULL,0,?,?, 'pending')""",
            (str(src_comp), "2026-02-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"),
        )
        conn.commit()

        dst_comp = dst_dir / "clip.funscript"
        dst_comp.write_bytes(b"FUNSCRIPT_DATA")
        assert is_source_companion_of_moved_video(conn, str(src_comp)) is False
    finally:
        conn.close()


def test_companion_post_move_cleanup_in_pending_events(tmp_path: Path):
    """Source companion deletion is auto-resolved when video was moved and companion exists at target."""
    db_path = create_test_db(tmp_path / "test.db")

    src_dir = tmp_path / "SourceFolder"
    src_dir.mkdir()
    src_video_path = str(src_dir / "my_video.mp4")
    src_comp = src_dir / "my_video.mp4.jpg"

    dst_dir = tmp_path / "DestFolder"
    dst_dir.mkdir()
    dst_video = dst_dir / "my_video.mp4"
    dst_comp = dst_dir / "my_video.mp4.jpg"
    dst_video.write_bytes(b"VIDEO_DATA" * 5000)
    dst_comp.write_bytes(b"JPG_DATA")

    seed_scene(db_path, scene_id=555, file_id=5555, file_path=dst_video, exists_on_disk=1)

    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO filesystem_events(
                event_key, event_type, source_path, destination_path, is_directory,
                first_seen_at, last_seen_at, status
            ) VALUES (?, 'deleted', ?, NULL, 0, datetime('now'), datetime('now'), 'pending')""",
            ("ev-del-comp", str(src_comp)),
        )
        event_time = conn.execute(
            "SELECT first_seen_at FROM filesystem_events WHERE event_key='ev-del-comp'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO activity_log(
                category, action, status, old_path, new_path, file_id, scene_id, recorded_at
            ) VALUES ('reconciliation', 'targeted Stash scan', 'updated', ?, ?, '5555', '555', ?)""",
            (src_video_path, str(dst_video), event_time),
        )
        conn.commit()
    finally:
        conn.close()

    pending = pending_filesystem_events(db_path)
    assert len(pending) == 0


def test_expected_duplicate_delete_temp_rename_creates_no_warning(tmp_path: Path):
    from librarymanager_monitor import LibraryEventHandler

    db_path = create_test_db(tmp_path / "expected-delete.db")
    candidate = tmp_path / "Incoming" / "scene.mp4"
    candidate.parent.mkdir()
    candidate.write_bytes(b"duplicate")
    librarymanager_core.expect_filesystem_delete(db_path, str(candidate))

    handler = LibraryEventHandler(db_path, MagicMock(), False)
    handler.on_moved(MagicMock(
        is_directory=False,
        src_path=str(candidate),
        dest_path=str(candidate) + ".delete",
    ))

    assert pending_filesystem_events(db_path) == []
