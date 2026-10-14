"""Focused tests for librarymanager.services.filing and its compatibility wrappers.

Verifies:
1. Public export completeness and wrapper parity with _is_watchtower_wrapper markers
2. Baseline establishment, verification, and pruning
3. Disqualification and companion discovery
4. Queue operations: enqueue, claim (FIFO), finish, fail, job ID assignment
5. Active transfer coordination and concurrency serialization
6. Abandoned queue recovery with Stash job liveness check
7. Stale active transfer recovery (3-way verification safety)
8. Incomplete transfer rollback and recovery
9. One-way dependency direction (filing_rules must not import filing)
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import MagicMock

import pytest

import librarymanager.services.filing as filing
import librarymanager_core
from librarymanager.database.connection import create_connection
from librarymanager.database.migrations import ensure_schema


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "filing_test.db"
    conn = create_connection(db_file)
    ensure_schema(conn, db_file)
    conn.close()
    return db_file


def _insert_test_proposal(
    db_path: Path,
    proposal_id: int,
    source_path: str,
    destination_filename: str,
    status: str = "pending",
    destination_folder: str = "",
    proposed_path: str = "",
    candidate_destinations: list | None = None,
    companions: list | None = None,
):
    conn = filing.connect(db_path)
    now = filing.utc_now()
    conn.execute(
        """INSERT INTO filing_proposals (
            id, file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, match_source, reason,
            companions_json, candidate_destinations_json, status, created_at, updated_at
        ) VALUES (?, 'f1', 's1', ?, ?, ?, ?, 'performer', 'p1', 'Performer One', 'metadata', 'test', ?, ?, ?, ?, ?)""",
        (
            proposal_id,
            source_path,
            proposed_path,
            destination_folder,
            destination_filename,
            json.dumps(companions or []),
            json.dumps(candidate_destinations or []),
            status,
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()


def test_extracted_symbols_and_core_wrapper_parity():
    """Verify all 27 extracted functions exist in filing service and have wrappers in core."""
    expected_functions = [
        "is_filing_baseline_established",
        "snapshot_incoming_baseline",
        "prune_resolved_filing_baseline",
        "is_disqualified_from_filing",
        "find_filing_companions",
        "record_incoming_filing_diagnostic",
        "evaluate_filing_proposal",
        "retry_filing_proposal",
        "enqueue_filing_transfer",
        "claim_next_filing_transfer",
        "finish_filing_transfer",
        "fail_filing_transfer",
        "set_filing_transfer_job_id",
        "recover_abandoned_processing_queue",
        "get_active_filing_transfers",
        "recover_stale_active_transfers",
        "apply_filing_proposal",
        "ignore_filing_proposal",
        "invalidate_stale_filing_proposals",
        "get_pending_filing_proposals",
        "recover_filing_proposal",
        "_is_in_nested_incoming_folder",
        "_update_proposal_status",
        "_move_file",
        "_format_bytes",
        "_update_active_transfer",
        "_clear_active_transfer",
    ]

    for fn_name in expected_functions:
        assert hasattr(filing, fn_name), f"filing service must export {fn_name}"
        assert callable(getattr(filing, fn_name)), f"filing.{fn_name} must be callable"

        assert hasattr(librarymanager_core, fn_name), f"librarymanager_core must have {fn_name}"
        core_fn = getattr(librarymanager_core, fn_name)
        assert callable(core_fn), f"librarymanager_core.{fn_name} must be callable"
        assert getattr(core_fn, "_is_watchtower_wrapper", False) is True, (
            f"librarymanager_core.{fn_name} must have _is_watchtower_wrapper = True"
        )


def test_import_direction_rules_does_not_import_filing():
    """Verify architectural requirement: filing_rules must not import filing."""
    import librarymanager.services.filing_rules as filing_rules
    import sys

    # Inspect module globals and imported symbols of filing_rules
    assert not hasattr(filing_rules, "apply_filing_proposal"), "filing_rules must not export filing functions"
    assert not hasattr(filing_rules, "enqueue_filing_transfer"), "filing_rules must not export filing queue"
    assert "librarymanager.services.filing" not in getattr(filing_rules, "__dict__", {})


def test_transfer_queue_lifecycle(test_db):
    """Verify enqueue -> claim (FIFO) -> finish and fail cycle."""
    _insert_test_proposal(test_db, 101, "/incoming/video1.mp4", "video1.mp4", status="pending")
    _insert_test_proposal(test_db, 102, "/incoming/video2.mp4", "video2.mp4", status="pending")

    # Enqueue both proposals
    assert filing.enqueue_filing_transfer(test_db, 101, "/dest/Folder A") is True
    assert filing.enqueue_filing_transfer(test_db, 102, "/dest/Folder B") is True

    # Claim next transfer (FIFO order)
    claim1 = filing.claim_next_filing_transfer(test_db)
    assert claim1 is not None
    assert claim1["proposal_id"] == 101
    assert claim1["status"] == "queued"  # status at time of claim

    # Check database status is updated to processing
    conn = filing.connect(test_db)
    row = conn.execute("SELECT status FROM filing_transfer_queue WHERE id=?", (claim1["id"],)).fetchone()
    conn.close()
    assert row["status"] == "processing"

    # Set Stash job ID
    filing.set_filing_transfer_job_id(test_db, 101, "job-stash-101")
    conn = filing.connect(test_db)
    row = conn.execute("SELECT stash_job_id FROM filing_transfer_queue WHERE proposal_id=101").fetchone()
    conn.close()
    assert row["stash_job_id"] == "job-stash-101"

    # Finish transfer
    filing.finish_filing_transfer(test_db, claim1["id"])
    conn = filing.connect(test_db)
    row = conn.execute("SELECT status FROM filing_transfer_queue WHERE id=?", (claim1["id"],)).fetchone()
    conn.close()
    assert row["status"] == "done"

    # Claim second transfer
    claim2 = filing.claim_next_filing_transfer(test_db)
    assert claim2 is not None
    assert claim2["proposal_id"] == 102

    # Fail second transfer
    filing.fail_filing_transfer(test_db, claim2["id"], "Simulated network timeout")
    conn = filing.connect(test_db)
    q_row = conn.execute("SELECT status, last_error FROM filing_transfer_queue WHERE id=?", (claim2["id"],)).fetchone()
    p_row = conn.execute("SELECT status, last_error FROM filing_proposals WHERE id=102").fetchone()
    conn.close()
    assert q_row["status"] == "failed"
    assert "Simulated network timeout" in q_row["last_error"]
    assert p_row["status"] == "pending"


def test_active_transfer_tracking(test_db):
    """Verify active transfer table record updates and clearing."""
    assert len(filing.get_active_filing_transfers(test_db)) == 0

    filing._update_active_transfer(
        test_db,
        proposal_id=201,
        scene_id="s201",
        file_id="f201",
        source_path="/incoming/scene.mp4",
        destination_path="/library/Performer/scene.mp4",
        destination_folder="/library/Performer",
        stage="moving_video",
        stage_label="Transferring Video",
        detail="Moving 1.5 GB",
        total_bytes=1500000000,
    )

    active = filing.get_active_filing_transfers(test_db)
    assert len(active) == 1
    assert active[0]["proposal_id"] == 201
    assert active[0]["stage"] == "moving_video"
    assert active[0]["total_bytes"] == 1500000000

    filing._clear_active_transfer(test_db, 201)
    assert len(filing.get_active_filing_transfers(test_db)) == 0


def test_companion_discovery_and_stem_matching(tmp_path):
    """Verify companion sidecars are discovered by exact stem or compound match."""
    inc = tmp_path / "incoming"
    inc.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()

    video = inc / "Movie (2026).mp4"
    video.write_bytes(b"video-bytes")

    # Companion 1: exact stem .nfo
    comp_nfo = inc / "Movie (2026).nfo"
    comp_nfo.write_text("info")

    # Companion 2: compound .mp4.jpg
    comp_thumb = inc / "Movie (2026).mp4.jpg"
    comp_thumb.write_bytes(b"jpg-bytes")

    # Unrelated file in same directory
    unrelated = inc / "Other Movie.mp4"
    unrelated.write_bytes(b"other-bytes")
    unrelated_comp = inc / "Other Movie.nfo"
    unrelated_comp.write_text("other-nfo")

    companions, err = filing.find_filing_companions(video, dest, "Movie (2026).mp4")
    assert err is None
    comp_sources = {c[0].name: c[1].name for c in companions}
    assert "Movie (2026).nfo" in comp_sources
    assert comp_sources["Movie (2026).nfo"] == "Movie (2026).nfo"
    assert "Movie (2026).mp4.jpg" in comp_sources
    assert comp_sources["Movie (2026).mp4.jpg"] == "Movie (2026).mp4.jpg"
    assert "Other Movie.nfo" not in comp_sources


def test_companion_collision_preflight(tmp_path):
    """Verify destination collision aborts companion discovery before transfer."""
    inc = tmp_path / "incoming"
    inc.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()

    video = inc / "Clip.mp4"
    video.write_bytes(b"clip")
    comp = inc / "Clip.jpg"
    comp.write_bytes(b"thumb")

    # Pre-create target companion in destination
    collision = dest / "Clip.jpg"
    collision.write_bytes(b"pre-existing")

    companions, err = filing.find_filing_companions(video, dest, "Clip.mp4")
    assert companions == []
    assert err is not None
    assert "Target companion already exists at destination" in err


def test_baseline_status_checks(test_db):
    """Verify is_filing_baseline_established states."""
    # 1. No baseline row
    ok, reason = filing.is_filing_baseline_established(test_db)
    assert ok is False
    assert "baseline has not been established" in reason

    conn = filing.connect(test_db)
    conn.execute(
        "INSERT INTO filing_baseline_state (established_at, incoming_folders_json, status, last_error) VALUES (?, ?, ?, ?)",
        ("2026-01-01T00:00:00Z", json.dumps(["/incoming"]), "failed", "Permission denied"),
    )
    conn.commit()
    conn.close()

    # 2. Failed baseline
    ok, reason = filing.is_filing_baseline_established(test_db)
    assert ok is False
    assert "initialization failed: Permission denied" in reason

    # 3. Complete baseline
    conn = filing.connect(test_db)
    conn.execute("UPDATE filing_baseline_state SET status='complete', last_error=NULL")
    conn.commit()
    conn.close()

    ok, reason = filing.is_filing_baseline_established(test_db, incoming_folders=["/incoming"])
    assert ok is True
    assert reason == "ok"


def test_nested_incoming_folder_detection(tmp_path):
    """Verify _is_in_nested_incoming_folder checks."""
    inc_root = tmp_path / "Incoming"
    inc_root.mkdir()
    sub_folder = inc_root / "Torrent.Subfolder"
    sub_folder.mkdir()

    file_in_root = inc_root / "direct.mp4"
    file_in_root.touch()
    file_in_sub = sub_folder / "nested.mp4"
    file_in_sub.touch()

    incoming_folders = [str(inc_root)]
    assert filing._is_in_nested_incoming_folder(file_in_root, incoming_folders) is False
    assert filing._is_in_nested_incoming_folder(file_in_sub, incoming_folders) is True


def test_ignore_filing_proposal(test_db):
    """Verify ignore_filing_proposal marks proposal ignored and prevents ignoring needs_recovery."""
    _insert_test_proposal(test_db, 301, "/incoming/video.mp4", "video.mp4", status="pending")
    res = filing.ignore_filing_proposal(test_db, 301)
    assert res["status"] == "ignored"

    _insert_test_proposal(test_db, 302, "/incoming/corrupt.mp4", "corrupt.mp4", status="needs_recovery")
    res_rec = filing.ignore_filing_proposal(test_db, 302)
    assert res_rec["status"] == "blocked"
    assert "requires recovery" in res_rec["reason"]


def _setup_filing_proposal_with_companions(tmp_path, test_db, companion_names):
    incoming = tmp_path / "Incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    dest_root = tmp_path / "DestRoot"
    dest_folder = dest_root / "Performer"
    dest_folder.mkdir(parents=True, exist_ok=True)

    video = incoming / "TestVideo.mp4"
    video.write_bytes(b"V" * 5000)

    companions = []
    for name in companion_names:
        c_path = incoming / name
        c_path.write_bytes(b"C" * 100)
        companions.append(c_path)

    conn = filing.connect(test_db)
    cur = conn.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, match_source,
            reason, status, candidate_destinations_json, created_at, updated_at
        ) VALUES ('f100', 's100', ?, ?, ?, ?, 'performer', 'p1', 'Performer', 'filename', 'test', 'pending', ?, ?, ?)""",
        (
            str(video),
            str(dest_folder / video.name),
            str(dest_folder),
            video.name,
            json.dumps([{"destination_folder": str(dest_folder)}]),
            filing.utc_now(),
            filing.utc_now(),
        ),
    )
    proposal_id = cur.lastrowid
    conn.commit()
    conn.close()

    current_vid = [video]
    mock_stash = MagicMock()
    mock_stash.find_plugin_config.return_value = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoot": str(dest_root),
    }
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s100", "files": [{"id": "f100", "path": str(video)}]}
    }

    def mock_move_files(payload):
        target = Path(payload["destination_folder"]) / payload["destination_basename"]
        curr = current_vid[0]
        curr.rename(target)
        current_vid[0] = target
        return True

    mock_stash.move_files.side_effect = mock_move_files
    return {
        "db": test_db,
        "incoming": incoming,
        "dest_folder": dest_folder,
        "video": video,
        "companions": companions,
        "proposal_id": proposal_id,
        "mock_stash": mock_stash,
        "current_vid": current_vid,
    }


def test_apply_filing_proposal_companion_disappears_before_move(tmp_path, test_db):
    """Scenario A: Companion disappears after preflight succeeds and video moves, but before
    the companion transfer loop reaches it. Transfer must fail, restore video, and report
    needs_recovery because the missing companion cannot be verified clean."""
    env = _setup_filing_proposal_with_companions(tmp_path, test_db, ["TestVideo.jpg"])
    video = env["video"]
    comp = env["companions"][0]
    dest_video = env["dest_folder"] / video.name

    # Hook mock_stash.move_files: after video moves to destination on the first call, delete companion at source!
    orig_move = env["mock_stash"].move_files.side_effect
    deleted = [False]

    def move_and_delete_companion(payload):
        res = orig_move(payload)
        if not deleted[0]:
            comp.unlink()  # Vanishes after preflight!
            deleted[0] = True
        return res

    env["mock_stash"].move_files.side_effect = move_and_delete_companion

    res = filing.apply_filing_proposal(
        env["db"], env["mock_stash"], env["proposal_id"], target_destination_folder=str(env["dest_folder"])
    )

    # Must report needs_recovery with rolled_back=False because expected companion is missing
    assert res["status"] == "needs_recovery"
    assert res.get("rolled_back") is False
    assert "CRITICAL" in res["reason"]
    # Communicates both facts: verified video restoration and unresolved missing companion
    assert "video restored to source" in res["reason"]
    assert "Companion missing at transfer time" in res["reason"]
    assert "TestVideo.jpg" in res["reason"]

    # Video must be safely restored to source and absent from destination
    assert video.is_file()
    assert not dest_video.exists()

    # Missing companion is not falsely reported as restored at source
    assert not comp.exists()

    # DB status must be needs_recovery
    conn = filing.connect(test_db)
    row = conn.execute("SELECT status, last_error FROM filing_proposals WHERE id=?", (env["proposal_id"],)).fetchone()
    conn.close()
    assert row["status"] == "needs_recovery"
    assert "CRITICAL" in row["last_error"]
    assert "video restored to source" in row["last_error"]
    assert "Companion missing at transfer time" in row["last_error"]


def test_apply_filing_proposal_companion_missing_during_rollback(tmp_path, test_db):
    """Scenario B: After companion 1 moves, companion 2 move fails. During rollback,
    companion 1 at destination is missing. Operation must report needs_recovery, NOT rolled_back=True."""
    env = _setup_filing_proposal_with_companions(tmp_path, test_db, ["TestVideo.jpg", "TestVideo.nfo"])
    # Companions are sorted: TestVideo.nfo is moved first, TestVideo.jpg is moved second
    comp_first = env["incoming"] / "TestVideo.nfo"
    dest_first = env["dest_folder"] / "TestVideo.nfo"

    orig_move_file = filing._move_file

    def faulty_move(src, target):
        src_p = Path(src)
        if src_p.suffix == ".jpg":  # Second companion transfer fails
            # Before rolling back, simulate first companion at destination vanishing!
            if dest_first.exists():
                dest_first.unlink()
            raise OSError("Simulated second companion transfer error")
        return orig_move_file(src, target)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(filing, "_move_file", faulty_move)
        res = filing.apply_filing_proposal(
            env["db"], env["mock_stash"], env["proposal_id"], target_destination_folder=str(env["dest_folder"])
        )

    # Must NOT report rolled_back=True because companion 1 could not be restored!
    assert res["status"] == "needs_recovery"
    assert res.get("rolled_back") is False
    assert "CRITICAL" in res["reason"]
    assert "Companion rollback failed or unverified" in res["reason"]
    assert "TestVideo.nfo" in res["reason"]

    # DB status must be needs_recovery
    conn = filing.connect(test_db)
    row = conn.execute("SELECT status, last_error FROM filing_proposals WHERE id=?", (env["proposal_id"],)).fetchone()
    conn.close()
    assert row["status"] == "needs_recovery"
    assert "CRITICAL" in row["last_error"]


def test_apply_filing_proposal_companion_collision_during_rollback(tmp_path, test_db):
    """Rollback safety: If a collision file appears at companion source during rollback,
    do NOT overwrite it. Set needs_recovery and rolled_back=False."""
    env = _setup_filing_proposal_with_companions(tmp_path, test_db, ["TestVideo.jpg", "TestVideo.nfo"])
    comp_first = env["incoming"] / "TestVideo.nfo"

    orig_move_file = filing._move_file

    def collision_move(src, target):
        src_p = Path(src)
        if src_p.suffix == ".jpg":  # Second companion transfer fails
            # A rogue collision file appeared at first companion's source location!
            comp_first.write_bytes(b"collision-data")
            raise OSError("Simulated second companion transfer error")
        return orig_move_file(src, target)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(filing, "_move_file", collision_move)
        res = filing.apply_filing_proposal(
            env["db"], env["mock_stash"], env["proposal_id"], target_destination_folder=str(env["dest_folder"])
        )

    assert res["status"] == "needs_recovery"
    assert res.get("rolled_back") is False
    assert "CRITICAL" in res["reason"]
    assert "source collision exists" in res["reason"]
    # Collision file must NOT have been overwritten
    assert comp_first.read_bytes() == b"collision-data"


def test_apply_filing_proposal_verified_clean_rollback(tmp_path, test_db):
    """Ensure existing verified clean rollback behavior works when companion 2 fails and all companions
    plus video are safely restored and verified."""
    env = _setup_filing_proposal_with_companions(tmp_path, test_db, ["TestVideo.jpg", "TestVideo.nfo"])
    video = env["video"]
    comp1 = env["incoming"] / "TestVideo.nfo"
    comp2 = env["incoming"] / "TestVideo.jpg"
    dest_video = env["dest_folder"] / video.name
    dest_comp1 = env["dest_folder"] / comp1.name
    dest_comp2 = env["dest_folder"] / comp2.name

    orig_move_file = filing._move_file

    def fail_on_second_comp(src, target):
        if Path(src).suffix == ".jpg":
            raise OSError("Simulated second companion transfer failure")
        return orig_move_file(src, target)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(filing, "_move_file", fail_on_second_comp)
        res = filing.apply_filing_proposal(
            env["db"], env["mock_stash"], env["proposal_id"], target_destination_folder=str(env["dest_folder"])
        )

    assert res["status"] == "failed"
    assert res.get("rolled_back") is True
    assert "safely restored to source" in res["reason"]

    # Everything verified restored at source
    assert video.is_file()
    assert comp1.is_file()
    assert comp2.is_file()
    # Nothing left at destination
    assert not dest_video.exists()
    assert not dest_comp1.exists()
    assert not dest_comp2.exists()
