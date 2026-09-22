from __future__ import annotations
import json
import os
import tempfile
import unittest.mock as mock
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import librarymanager_core
import unittest
import unittest.mock
from librarymanager_core import (
    retry_filing_proposal,
    refresh_destination_dir_cache,
    preview_scene_filename,
    preview_safe_filenames,
    apply_scene_filename,
    connect,
    opensubtitles_hash,
    utc_now,
    snapshot_incoming_baseline,
    is_filing_baseline_established,
    is_disqualified_from_filing,
    match_performer_for_filing,
    match_studio_for_filing,
    resolve_filing_destination_folder,
    find_filing_companions,
    evaluate_filing_proposal,
    apply_filing_proposal,
    ignore_filing_proposal,
    recover_filing_proposal,
    get_pending_filing_proposals,
    invalidate_stale_filing_proposals,
    get_configured_filing_destination_roots,
    get_filing_folder_mappings,
    save_filing_folder_mapping,
    delete_filing_folder_mapping,
    resolve_filing_destinations,
    resolve_tag_filing_destinations,
)


@pytest.fixture
def test_env():
    librarymanager_core.invalidate_destination_dir_cache()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir).resolve()
        db_path = tmp_path / "test.db"
        con = connect(db_path)
        con.executescript(librarymanager_core.SCHEMA)
        con.close()

        incoming_dir = tmp_path / "Incoming"
        incoming_dir.mkdir()
        dest_root = tmp_path / "Performers"
        dest_root.mkdir()
        (dest_root / "Jane Doe").mkdir()
        (dest_root / "Mary Smith").mkdir()

        yield {
            "tmp": tmp_path,
            "db": db_path,
            "incoming": incoming_dir,
            "dest_root": dest_root,
        }
    librarymanager_core.invalidate_destination_dir_cache()


# ---------------------------------------------------------------------------
# Point 1: Activation Baseline & Disqualification Tests
# ---------------------------------------------------------------------------

def test_activation_baseline_disqualifies_preexisting_files(test_env):
    db = test_env["db"]
    incoming = test_env["incoming"]

    pre_video = incoming / "pre_existing_video.mp4"
    pre_video.write_bytes(b"V" * 150000)
    pre_sub = incoming / "subdir"
    pre_sub.mkdir()
    pre_nested = pre_sub / "nested_existing.mp4"
    pre_nested.write_bytes(b"N" * 150000)

    # Establish baseline snapshot
    count = snapshot_incoming_baseline(db, [str(incoming)])
    assert count == 2

    # Check baseline presence
    con = connect(db)
    rows = con.execute("SELECT path, size FROM filing_incoming_baseline").fetchall()
    con.close()
    assert len(rows) == 2

    # Pre-existing files must be disqualified
    disq, reason = is_disqualified_from_filing(
        db, str(pre_video), 150000, opensubtitles_hash(pre_video), "f1", "s1"
    )
    assert disq is True
    assert "baseline" in reason

    disq, reason = is_disqualified_from_filing(
        db, str(pre_nested), 150000, opensubtitles_hash(pre_nested), "f2", "s2"
    )
    assert disq is True


def test_relocated_and_rediscovered_library_files_excluded(test_env):
    db = test_env["db"]
    incoming = test_env["incoming"]

    # Establish baseline for incoming
    snapshot_incoming_baseline(db, [str(incoming)])

    # Simulate an existing library file in the database
    orig_path = test_env["tmp"] / "Library" / "video.mp4"
    con = connect(db)
    con.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, fingerprints_json, exists_on_disk, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        ("f100", "s100", str(orig_path), "video.mp4", 150000, json.dumps([{"type": "oshash", "value": "dummyhash123"}]), utc_now(), utc_now())
    )
    con.commit()
    con.close()

    # User moved or copied file back to Incoming
    incoming_copy = incoming / "relocated_video.mp4"
    incoming_copy.write_bytes(b"R" * 150000)

    # Disqualification check with matching file_id or matching oshash
    disq, reason = is_disqualified_from_filing(
        db, str(incoming_copy), 150000, "dummyhash123", "f100", "s100"
    )
    assert disq is True
    assert "relocated" in reason or "match" in reason


def test_previously_evaluated_proposals_excluded(test_env):
    db = test_env["db"]
    incoming = test_env["incoming"]

    snapshot_incoming_baseline(db, [str(incoming)])

    vid = incoming / "tested.mp4"
    vid.write_bytes(b"T" * 150000)

    con = connect(db)
    con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES ('f1', 's1', ?, '/dst/tested.mp4', '/dst', 'tested.mp4', 'performer', '1', 'Jane', NULL, 'filename', 'test', 'ignored', ?, ?)""",
        (str(vid), utc_now(), utc_now())
    )
    con.commit()
    con.close()

    disq, reason = is_disqualified_from_filing(db, str(vid), 150000, opensubtitles_hash(vid), "f1", "s1")
    assert disq is True
    assert "already evaluated" in reason


# ---------------------------------------------------------------------------
# Safe Baseline Failure & Incomplete Baseline Edge Cases (User Prompt 2)
# ---------------------------------------------------------------------------

def test_missing_baseline_fails_safely_and_blocks_proposals(test_env):
    """When the feature is enabled but baseline has never been established,
    Watchtower must fail safely: zero proposals can be generated."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]

    video = incoming / "Jane.Doe.NewArrival.mp4"
    video.write_bytes(b"X" * 150000)

    # Verify baseline is not established
    baseline_ok, reason = is_filing_baseline_established(db, [str(incoming)])
    assert baseline_ok is False
    assert "not been established" in reason

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "allPerformers": [{"id": "1", "name": "Jane Doe", "disambiguation": "", "alias_list": []}]
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingDestinationRoot": str(dest_root),
        "incomingFolders": [str(incoming)],
    }
    scene = {"id": "200", "files": [{"id": "f200"}], "performers": []}

    # evaluate_filing_proposal must refuse to generate a proposal!
    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene, config)
    assert proposal is None, "Missing baseline must block all proposals"

    # Also check is_disqualified_from_filing fails safely
    disq, d_reason = is_disqualified_from_filing(db, str(video), 150000, opensubtitles_hash(video), "f200", "s200")
    assert disq is True
    assert "failing safely" in d_reason


def test_failed_baseline_initialization_fails_safely(test_env):
    """If baseline initialization fails (e.g. disk error or unreadable directory),
    status is recorded as failed and proposals are blocked."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]

    # Record a failed baseline initialization
    con = connect(db)
    con.execute(
        """INSERT INTO filing_baseline_state (established_at, incoming_folders_json, status, last_error)
           VALUES (?, ?, 'failed', 'Permission denied reading incoming folder')""",
        (utc_now(), json.dumps([str(incoming)]))
    )
    con.commit()
    con.close()

    baseline_ok, reason = is_filing_baseline_established(db, [str(incoming)])
    assert baseline_ok is False
    assert "failed" in reason

    video = incoming / "Jane.Doe.ArrivedAfterFail.mp4"
    video.write_bytes(b"X" * 150000)

    mock_stash = MagicMock()
    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingDestinationRoot": str(dest_root),
        "incomingFolders": [str(incoming)],
    }
    scene = {"id": "201", "files": [{"id": "f201"}], "performers": []}

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene, config)
    assert proposal is None, "Failed baseline must block all proposals"


def test_incomplete_baseline_uncovered_folder_fails_safely(test_env):
    """If a new incoming folder is configured after the baseline was established,
    Watchtower must fail safely until the baseline is updated."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    incoming_2 = test_env["tmp"] / "Incoming_2"
    incoming_2.mkdir()
    dest_root = test_env["dest_root"]

    # Snapshot only covers incoming
    snapshot_incoming_baseline(db, [str(incoming)])

    # But config now includes incoming_2
    baseline_ok, reason = is_filing_baseline_established(db, [str(incoming), str(incoming_2)])
    assert baseline_ok is False
    assert "not covered by the current baseline snapshot" in reason

    video = incoming_2 / "Jane.Doe.NewInFolder2.mp4"
    video.write_bytes(b"X" * 150000)

    mock_stash = MagicMock()
    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingDestinationRoot": str(dest_root),
        "incomingFolders": [str(incoming), str(incoming_2)],
    }
    scene = {"id": "202", "files": [{"id": "f202"}], "performers": []}

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene, config)
    assert proposal is None, "Uncovered incoming folder must block proposals"


# ---------------------------------------------------------------------------
# Point 2 & Follow-up: Approved Move, Two-Way Rollback & Recovery Safeguards
# ---------------------------------------------------------------------------

def test_approved_move_success(test_env):
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.Clean.mp4"
    video.write_bytes(b"V" * 150000)
    jpg = incoming / "Jane.Doe.Clean.jpg"
    jpg.write_bytes(b"J" * 2000)

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES ('f10', 's10', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', 'pending', ?, ?)""",
        (str(video), str(dest_folder / video.name), str(dest_folder), video.name, utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    current_vid = [video]
    mock_stash = MagicMock()
    mock_stash.find_plugin_config.return_value = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoot": str(dest_root)
    }
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s10", "files": [{"id": "f10", "path": str(video)}]}
    }
    def mock_move_files(payload):
        target = Path(payload["destination_folder"]) / payload["destination_basename"]
        curr = current_vid[0]
        curr.rename(target)
        current_vid[0] = target
        return True
    mock_stash.move_files.side_effect = mock_move_files

    result = apply_filing_proposal(db, mock_stash, proposal_id)
    assert result["status"] == "completed"
    assert (dest_folder / video.name).exists()
    assert (dest_folder / jpg.name).exists()
    assert not video.exists()
    assert not jpg.exists()

    # Check proposal record in DB
    con = connect(db)
    row = con.execute("SELECT status, last_error FROM filing_proposals WHERE id=?", (proposal_id,)).fetchone()
    con.close()
    assert row["status"] == "completed"
    assert row["last_error"] is None


def test_companion_failure_triggers_verified_two_way_rollback(test_env):
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.Rollback.mp4"
    video.write_bytes(b"V" * 150000)
    jpg = incoming / "Jane.Doe.Rollback.jpg"
    jpg.write_bytes(b"J" * 2000)
    nfo = incoming / "Jane.Doe.Rollback.nfo"
    nfo.write_bytes(b"N" * 500)

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES ('f11', 's11', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', 'pending', ?, ?)""",
        (str(video), str(dest_folder / video.name), str(dest_folder), video.name, utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    current_vid = [video]
    mock_stash = MagicMock()
    mock_stash.find_plugin_config.return_value = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoot": str(dest_root)
    }
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s11", "files": [{"id": "f11", "path": str(video)}]}
    }
    def mock_move_files(payload):
        target = Path(payload["destination_folder"]) / payload["destination_basename"]
        curr = current_vid[0]
        curr.rename(target)
        current_vid[0] = target
        return True
    mock_stash.move_files.side_effect = mock_move_files

    orig_rename = Path.rename
    def faulty_rename(self, target):
        if self.suffix == ".nfo":
            raise OSError("Simulated companion write error")
        return orig_rename(self, target)

    with mock.patch.object(Path, "rename", faulty_rename):
        result = apply_filing_proposal(db, mock_stash, proposal_id)

    assert result["status"] == "failed"
    assert result.get("rolled_back") is True
    assert "safely restored to source" in result["reason"]

    # Verify on-disk state: everything restored to source!
    assert video.exists(), "Video must be back at source!"
    assert jpg.exists(), "Companion JPG must be back at source!"
    assert nfo.exists(), "Companion NFO must be back at source!"
    assert not (dest_folder / video.name).exists()
    assert not (dest_folder / jpg.name).exists()
    assert not (dest_folder / nfo.name).exists()


def test_failed_video_rollback_retains_transaction_and_sets_needs_recovery(test_env):
    """Safeguard requirement: if a companion move fails AND the attempt to roll the video
    back also fails, retain the transaction for recovery and display an actionable warning.
    Never report a successful rollback unless verified."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.Critical.mp4"
    video.write_bytes(b"V" * 150000)
    jpg = incoming / "Jane.Doe.Critical.jpg"
    jpg.write_bytes(b"J" * 2000)

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES ('f12', 's12', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', 'pending', ?, ?)""",
        (str(video), str(dest_folder / video.name), str(dest_folder), video.name, utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    current_vid = [video]
    mock_stash = MagicMock()
    mock_stash.find_plugin_config.return_value = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoot": str(dest_root)
    }
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s12", "files": [{"id": "f12", "path": str(video)}]}
    }
    # First call (move to destination) succeeds; second call (rollback) fails
    move_count = [0]
    def mock_move_files(payload):
        move_count[0] += 1
        if move_count[0] == 1:
            target = Path(payload["destination_folder"]) / payload["destination_basename"]
            curr = current_vid[0]
            curr.rename(target)
            current_vid[0] = target
            return True
        else:
            raise RuntimeError("Stash server crashed during rollback!")
    mock_stash.move_files.side_effect = mock_move_files

    orig_rename = Path.rename
    def faulty_rename(self, target):
        if self.suffix == ".jpg":
            raise OSError("Companion copy error")
        return orig_rename(self, target)

    with mock.patch.object(Path, "rename", faulty_rename):
        result = apply_filing_proposal(db, mock_stash, proposal_id)

    # Must NOT report successful rollback!
    assert result["status"] == "needs_recovery"
    assert result.get("rolled_back") is False
    assert "CRITICAL" in result["reason"]
    assert "Video rollback failed" in result["reason"]

    # Proposal must be preserved in database with status='needs_recovery'
    con = connect(db)
    row = con.execute("SELECT status, last_error FROM filing_proposals WHERE id=?", (proposal_id,)).fetchone()
    con.close()
    assert row["status"] == "needs_recovery"
    assert "CRITICAL" in row["last_error"]

    # Must be returned in get_pending_filing_proposals so it appears in live terminal / Needs Attention
    pending = get_pending_filing_proposals(db)
    assert any(p["id"] == proposal_id and p["status"] == "needs_recovery" for p in pending)

    # Test recovery mechanism: recover_filing_proposal
    def mock_recovery_move(payload):
        target = Path(payload["destination_folder"]) / payload["destination_basename"]
        curr = current_vid[0]
        curr.rename(target)
        current_vid[0] = target
        return True
    mock_stash.move_files.side_effect = mock_recovery_move

    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s12", "files": [{"id": "f12", "path": str(video)}]}
    }
    recovery_res = recover_filing_proposal(db, mock_stash, proposal_id)
    assert recovery_res["status"] == "failed"
    assert recovery_res["recovered"] is True
    assert video.exists(), "Video must now be restored to source"
    assert not (dest_folder / video.name).exists()


# ---------------------------------------------------------------------------
# Point 3: Conservative Matching Rules (Performers, Studios, Aliases, Folders)
# ---------------------------------------------------------------------------

def test_conservative_performer_alias_matching():
    performers = [
        {"id": "1", "name": "Jane Doe", "disambiguation": "", "alias_list": ["JD", "Janie"]},
        {"id": "2", "name": "Mary Smith", "disambiguation": "", "alias_list": ["Mary S"]},
        {"id": "3", "name": "Fox", "disambiguation": "", "alias_list": []},
        {"id": "4", "name": "May", "disambiguation": "", "alias_list": []},
        {"id": "5", "name": "Alice Wonderland", "disambiguation": "", "alias_list": ["Janie"]}, # Janie is ambiguous!
    ]

    # Exact name match
    res = match_performer_for_filing({}, "Jane.Doe.In.Paris.mp4", performers)
    assert res["matched"] is True
    assert res["entity"]["name"] == "Jane Doe"
    assert res["matched_alias"] is None

    # Unambiguous alias match
    res = match_performer_for_filing({}, "Mary.S.Beach.Day.mp4", performers)
    assert res["matched"] is True
    assert res["entity"]["name"] == "Mary Smith"
    assert res["matched_alias"] == "Mary S"

    # Ambiguous alias shared between two performers must be rejected
    res = match_performer_for_filing({}, "Janie.Solo.mp4", performers)
    assert res["matched"] is False

    # Multiple distinct performers in filename are parsed reliably as multiple entities
    res = match_performer_for_filing({}, "Jane.Doe.and.Mary.Smith.mp4", performers)
    assert res["matched"] is True
    assert len(res["entities"]) == 2
    assert {e["name"] for e in res["entities"]} == {"Jane Doe", "Mary Smith"}

    # Substring protection: "Fox" must not match "Foxtrot"
    res = match_performer_for_filing({}, "The.Foxtrot.Dance.mp4", performers)
    assert res["matched"] is False

    # Substring protection: "May" must not match "Maybe"
    res = match_performer_for_filing({}, "Maybe.Someday.mp4", performers)
    assert res["matched"] is False

    # Standalone exact match for "Fox" and "May" works
    res = match_performer_for_filing({}, "Featuring.Fox.Solo.mp4", performers)
    assert res["matched"] is True and res["entity"]["name"] == "Fox"

    res = match_performer_for_filing({}, "May.Returns.mp4", performers)
    assert res["matched"] is True and res["entity"]["name"] == "May"


def test_single_word_alias_does_not_claim_a_different_multiword_name():
    performers = [
        {"id": "98", "name": "Example Performer", "alias_list": ["Sam"]},
    ]

    embedded = match_performer_for_filing(
        {}, "A Release - First Person & Sam Taylor.mp4", performers,
        match_source="filename_only",
    )
    assert embedded["matched"] is False

    standalone = match_performer_for_filing(
        {}, "Sam - Solo Scene.mp4", performers,
        match_source="filename_only",
    )
    assert standalone["matched"] is True
    assert standalone["entity"]["id"] == "98"


def test_verified_tag_folder_matching_is_general_and_preserves_ambiguity(test_env):
    root = test_env["dest_root"]
    exact = root / "Fan Sites"
    plural = root / "Femboys"
    category = root / "BDSM & Fetish"
    descriptor = root / "Blondes"
    second_category = root / "BDSM Collection"
    for folder in (exact, plural, category, descriptor, second_category):
        folder.mkdir(parents=True, exist_ok=True)
    librarymanager_core.invalidate_destination_dir_cache()

    paths, status = resolve_tag_filing_destinations(
        [str(root)], {"id": "1", "name": "fansites", "aliases": []}
    )
    assert paths == [exact]
    assert status == "ok"

    paths, status = resolve_tag_filing_destinations(
        [str(root)], {"id": "2", "name": "femboy", "aliases": []}
    )
    assert paths == [plural]
    assert status == "ok"

    paths, status = resolve_tag_filing_destinations(
        [str(root)], {"id": "3", "name": "Blonde Hair", "aliases": []}
    )
    assert paths == [descriptor]
    assert status == "ok"

    paths, status = resolve_tag_filing_destinations(
        [str(root)], {"id": "4", "name": "BDSM", "aliases": []}
    )
    assert paths == [second_category]
    assert status == "ok"


def test_combined_mode_offers_verified_tag_folder_without_false_alias(test_env):
    db = test_env["db"]
    incoming = test_env["incoming"]
    root = test_env["dest_root"]
    tag_folder = root / "Fan Sites"
    tag_folder.mkdir(parents=True, exist_ok=True)
    librarymanager_core.invalidate_destination_dir_cache()

    snapshot_incoming_baseline(db, [str(incoming)])
    video = incoming / "A Release - First Person & Sam Taylor.mp4"
    video.write_bytes(b"TAG_DESTINATION_TEST" * 1000)

    conn = connect(db)
    now = utc_now()
    conn.execute(
        "INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('tag-file', 'tag-scene', ?, ?, 1, ?, ?)",
        (str(video), video.name, now, now),
    )
    conn.commit()
    conn.close()

    scene = {
        "id": "tag-scene",
        "title": video.stem,
        "files": [{"id": "tag-file", "path": str(video)}],
        "performers": [],
        "studio": None,
        "tags": [{"id": "tag-1", "name": "fansites", "aliases": []}],
    }
    stash = MagicMock()
    stash.call_GQL.side_effect = lambda query, variables=None: (
        {"allPerformers": [{"id": "98", "name": "Example Performer", "alias_list": ["Sam"]}]}
        if "allPerformers" in query else {"allStudios": []}
    )
    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingMatchSource": "metadata_first",
        "autoFilingDestinationRoots": [str(root)],
        "incomingFolders": [str(incoming)],
    }

    proposal = evaluate_filing_proposal(db, stash, str(video), scene, config)
    assert proposal is not None
    assert proposal["destination_folder"] == str(tag_folder.resolve())
    assert proposal["organize_by"] == "tag"
    assert proposal["matched_entity_name"] == "fansites"
    assert proposal["matched_entity_id"] == "tag-1"


def test_conservative_studio_alias_matching():
    studios = [
        {"id": "10", "name": "Evil Angel", "aliases": ["EA", "Evil"]},
        {"id": "20", "name": "Brazzers", "aliases": ["ZZ"]},
        {"id": "30", "name": "Vixen", "aliases": []},
        {"id": "40", "name": "Other Studio", "aliases": ["EA"]}, # EA is ambiguous!
    ]

    # Exact studio name match
    res = match_studio_for_filing({}, "Evil.Angel.Presents.Scene.mp4", studios)
    assert res["matched"] is True
    assert res["entity"]["name"] == "Evil Angel"

    # Ambiguous alias EA must be rejected
    res = match_studio_for_filing({}, "EA.Release.mp4", studios)
    assert res["matched"] is False

    # Multiple studios in filename must be rejected
    res = match_studio_for_filing({}, "Evil.Angel.vs.Brazzers.mp4", studios)
    assert res["matched"] is False
    assert "Multiple studios" in res["reason"]


def test_destination_folder_matching(test_env):
    dest_root = test_env["dest_root"]

    # Exact match
    folder, reason = resolve_filing_destination_folder(str(dest_root), "Jane Doe")
    assert folder == dest_root / "Jane Doe"
    assert reason == "ok"

    # Normalized match (spacing/underscores)
    (dest_root / "Jane_Doe").rmdir() if (dest_root / "Jane_Doe").exists() else None
    folder, reason = resolve_filing_destination_folder(str(dest_root), "Jane_Doe")
    assert folder == dest_root / "Jane Doe"

    # Missing folder must NOT be created silently
    folder, reason = resolve_filing_destination_folder(str(dest_root), "Unknown Performer")
    assert folder is None
    assert "does not exist" in reason
    assert not (dest_root / "Unknown Performer").exists()

    # Ambiguous folder match under root
    (dest_root / "Jane-Doe").mkdir()
    librarymanager_core.invalidate_destination_dir_cache(dest_root)
    folder, reason = resolve_filing_destination_folder(str(dest_root), "Jane Doe")
    assert folder is None
    assert "Ambiguous" in reason
    (dest_root / "Jane-Doe").rmdir()
    librarymanager_core.invalidate_destination_dir_cache(dest_root)


def test_destination_folder_matching_ignores_joined_word_separators(test_env):
    """A Stash entity like 8teenBoy matches an existing 8Teen Boy folder conservatively."""
    dest_root = test_env["dest_root"]
    joined_folder = dest_root / "8Teen Boy"
    joined_folder.mkdir()
    librarymanager_core.invalidate_destination_dir_cache(dest_root)

    paths, status = resolve_filing_destinations([str(dest_root)], "8teenBoy")

    assert status == "ok"
    assert paths == [joined_folder]


def test_joined_word_folder_matching_preserves_ambiguity(test_env):
    """Separator-insensitive collisions are returned for explicit user choice."""
    dest_root = test_env["dest_root"]
    first = dest_root / "8Teen Boy"
    second = dest_root / "8-Teen-Boy"
    first.mkdir()
    second.mkdir()
    librarymanager_core.invalidate_destination_dir_cache(dest_root)

    paths, status = resolve_filing_destinations([str(dest_root)], "8teenBoy")

    assert status == "multiple_destinations"
    assert set(paths) == {first, second}


# ---------------------------------------------------------------------------
# Ignore Action Persistence
# ---------------------------------------------------------------------------

def test_ignore_filing_proposal_persistence(test_env):
    db = test_env["db"]

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES ('f99', 's99', '/in/test.mp4', '/dst/test.mp4', '/dst', 'test.mp4', 'performer', '1', 'Jane', NULL, 'filename', 'test', 'pending', ?, ?)""",
        (utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    res = ignore_filing_proposal(db, proposal_id)
    assert res["status"] == "ignored"

    # Verify no longer in pending list
    pending = get_pending_filing_proposals(db)
    assert not any(p["id"] == proposal_id for p in pending)

    # Verify status in database
    con = connect(db)
    row = con.execute("SELECT status FROM filing_proposals WHERE id=?", (proposal_id,)).fetchone()
    con.close()
    assert row["status"] == "ignored"


# ---------------------------------------------------------------------------
# End-to-End Proposal Generation & Collision Checks
# ---------------------------------------------------------------------------

def test_evaluate_filing_proposal_end_to_end(test_env):
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]

    # Establish complete baseline first
    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Jane.Doe.Hot.Summer.mp4"
    video.write_bytes(b"X" * 150000)
    jpg = incoming / "Jane.Doe.Hot.Summer.jpg"
    jpg.write_bytes(b"J" * 2000)

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "allPerformers": [
            {"id": "1", "name": "Jane Doe", "disambiguation": "", "alias_list": []},
            {"id": "2", "name": "Mary Smith", "disambiguation": "", "alias_list": []},
        ]
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingDestinationRoot": str(dest_root),
        "autoFilingMatchSource": "metadata_first",
        "incomingFolders": [str(incoming)],
    }

    scene = {
        "id": "100",
        "files": [{"id": "f100", "path": str(video)}],
        "title": "Summer Fun",
        "performers": [],
    }

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene, config)
    assert proposal is not None
    assert proposal["matched_entity_name"] == "Jane Doe"
    assert proposal["destination_folder"] == str(dest_root / "Jane Doe")
    assert proposal["companions_count"] == 1

    # Check pending proposals
    pending = get_pending_filing_proposals(db)
    assert len(pending) == 1
    assert pending[0]["file_id"] == "f100"


def test_destination_collision_rejects_proposal(test_env):
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Jane.Doe.Collision.mp4"
    video.write_bytes(b"X" * 150000)

    # Place a collision file at destination
    (dest_folder / video.name).write_bytes(b"COLLISION")

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "allPerformers": [{"id": "1", "name": "Jane Doe", "disambiguation": "", "alias_list": []}]
    }
    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingDestinationRoot": str(dest_root),
        "incomingFolders": [str(incoming)],
    }
    scene = {"id": "101", "files": [{"id": "f101"}], "performers": []}

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene, config)
    assert proposal is None, "Should reject proposal if destination file exists"


def test_companion_collision_rejects_proposal(test_env):
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Jane.Doe.CompCollision.mp4"
    video.write_bytes(b"X" * 150000)
    jpg = incoming / "Jane.Doe.CompCollision.jpg"
    jpg.write_bytes(b"J" * 2000)

    # Place companion collision at destination
    (dest_folder / jpg.name).write_bytes(b"EXISTING_JPG")

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "allPerformers": [{"id": "1", "name": "Jane Doe", "disambiguation": "", "alias_list": []}]
    }
    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingDestinationRoot": str(dest_root),
        "incomingFolders": [str(incoming)],
    }
    scene = {"id": "102", "files": [{"id": "f102"}], "performers": []}

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene, config)
    assert proposal is None, "Should reject proposal if destination companion exists"


def test_multiple_performers_in_metadata_rejects_proposal(test_env):
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Scene.With.Two.Stars.mp4"
    video.write_bytes(b"X" * 150000)

    mock_stash = MagicMock()
    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingDestinationRoot": str(dest_root),
        "autoFilingMatchSource": "metadata_first",
        "incomingFolders": [str(incoming)],
    }
    # Two performers tagged in scene - both Jane Doe and Mary Smith have folders on dest_root
    scene = {
        "id": "103",
        "files": [{"id": "f103"}],
        "performers": [
            {"id": "1", "name": "Jane Doe"},
            {"id": "2", "name": "Mary Smith"},
        ],
    }

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene, config)
    assert proposal is not None
    assert len(proposal["candidate_destinations"]) == 2

    # When neither performer has a folder on disk, proposal is None with diagnostic
    (dest_root / "Jane Doe").rmdir()
    (dest_root / "Mary Smith").rmdir()
    librarymanager_core.invalidate_destination_dir_cache()

    video2 = incoming / "Scene.With.Two.Stars2.mp4"
    video2.write_bytes(b"Y" * 150000)
    scene2 = {"id": "104", "files": [{"id": "f104"}], "performers": scene["performers"]}
    proposal2 = evaluate_filing_proposal(db, mock_stash, str(video2), scene2, config)
    assert proposal2 is None


# ---------------------------------------------------------------------------
# Recovery Safeguards (User Prompt 3: Specific Recovery Corrections)
# ---------------------------------------------------------------------------

def test_recovery_never_touches_unrelated_startswith_files(test_env):
    """Safeguard 1: Never select companions using broad startswith().
    Recover only the exact companion paths recorded for that transaction."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.Exact.mp4"
    exact_jpg = incoming / "Jane.Doe.Exact.jpg"
    # Files are currently at destination awaiting recovery; ensure they do not exist at source

    # Put a file in destination that starts with the same stem but is UNRELATED
    unrelated_file = dest_folder / "Jane.Doe.Exact_unrelated_scene.jpg"
    unrelated_file.write_bytes(b"UNRELATED_DATA")

    # Record proposal with exact companions_json
    dest_video = dest_folder / video.name
    dest_jpg = dest_folder / exact_jpg.name

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, companions_json, status, created_at, updated_at
        ) VALUES ('f50', 's50', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', ?, 'needs_recovery', ?, ?)""",
        (
            str(video), str(dest_video), str(dest_folder), video.name,
            json.dumps([{"source": str(exact_jpg), "target": str(dest_jpg)}]),
            utc_now(), utc_now()
        )
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    # Simulate video and exact companion at destination
    dest_video.write_bytes(b"V" * 150000)
    dest_jpg.write_bytes(b"J" * 2000)

    mock_stash = MagicMock()
    def mock_move(payload):
        target = Path(payload["destination_folder"]) / payload["destination_basename"]
        if dest_video.exists():
            dest_video.rename(target)
        return True
    mock_stash.move_files.side_effect = mock_move
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s50", "files": [{"id": "f50", "path": str(video)}]}
    }

    res = recover_filing_proposal(db, mock_stash, proposal_id)
    assert res["recovered"] is True

    # Verified: video and exact companion restored
    assert video.exists()
    assert exact_jpg.exists()
    assert not dest_video.exists()
    assert not dest_jpg.exists()

    # CRITICAL: unrelated startswith file must NOT be moved or modified!
    assert unrelated_file.exists(), "Unrelated file starting with same stem must NOT be moved!"
    assert unrelated_file.read_bytes() == b"UNRELATED_DATA"
    assert not (incoming / unrelated_file.name).exists()


def test_recovery_preflight_blocks_on_collision_and_never_overwrites(test_env):
    """Safeguard 2: Never overwrite an existing file during recovery.
    Preflight every source and destination, and stop safely on any collision."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.CollisionTest.mp4"
    dest_video = dest_folder / video.name
    dest_video.write_bytes(b"DEST_VIDEO")

    # COLLISION: A file already exists at the source path!
    video.write_bytes(b"SOURCE_COLLISION_DATA")

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, companions_json, status, created_at, updated_at
        ) VALUES ('f51', 's51', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', '[]', 'needs_recovery', ?, ?)""",
        (str(video), str(dest_video), str(dest_folder), video.name, utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    mock_stash = MagicMock()

    # Attempt recovery
    res = recover_filing_proposal(db, mock_stash, proposal_id)
    assert res["recovered"] is False
    assert res["status"] == "needs_recovery"
    assert "Recovery collision" in res["reason"]

    # Both files must remain completely untouched! No overwrite!
    assert video.read_bytes() == b"SOURCE_COLLISION_DATA"
    assert dest_video.read_bytes() == b"DEST_VIDEO"
    assert mock_stash.move_files.call_count == 0, "Stash move_files must NOT be called on collision!"


def test_recovery_requires_video_companions_and_stash_scene_verification(test_env):
    """Safeguard 3: Verify that the video, all companions and the original Stash
    scene's file path are restored before reporting successful recovery."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.ThreePart.mp4"
    dest_video = dest_folder / video.name
    dest_video.write_bytes(b"V" * 150000)

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, companions_json, status, created_at, updated_at
        ) VALUES ('f52', 's52', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', '[]', 'needs_recovery', ?, ?)""",
        (str(video), str(dest_video), str(dest_folder), video.name, utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    mock_stash = MagicMock()
    def mock_move(payload):
        target = Path(payload["destination_folder"]) / payload["destination_basename"]
        if dest_video.exists():
            dest_video.rename(target)
        return True
    mock_stash.move_files.side_effect = mock_move

    # Sub-case A: File moved on disk, but Stash scene path still reports destination!
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s52", "files": [{"id": "f52", "path": str(dest_video)}]}
    }

    res = recover_filing_proposal(db, mock_stash, proposal_id)
    assert res["recovered"] is False
    assert res["status"] == "needs_recovery"
    assert "Stash scene path not restored" in res["reason"]

    # Sub-case B: Stash scene path now reports source!
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s52", "files": [{"id": "f52", "path": str(video)}]}
    }
    res = recover_filing_proposal(db, mock_stash, proposal_id)
    assert res["recovered"] is True
    assert res["status"] == "failed"
    assert "Transaction recovery verified" in res["reason"]


def test_needs_recovery_cannot_be_ignored(test_env):
    """Safeguard 4: Do not allow the UI's Ignore button to dismiss a needs_recovery transaction.
    Keep it visible until recovery is verified."""
    db = test_env["db"]

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, companions_json, status, created_at, updated_at
        ) VALUES ('f53', 's53', '/in/v.mp4', '/dst/v.mp4', '/dst', 'v.mp4', 'performer', '1', 'Jane', NULL, 'filename', 'test', '[]', 'needs_recovery', ?, ?)""",
        (utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    # Attempt to ignore
    res = ignore_filing_proposal(db, proposal_id)
    assert res["status"] == "blocked"
    assert "Cannot ignore a transaction that requires recovery" in res["reason"]

    # Must remain in needs_recovery in the database
    con = connect(db)
    row = con.execute("SELECT status FROM filing_proposals WHERE id=?", (proposal_id,)).fetchone()
    con.close()
    assert row["status"] == "needs_recovery"

    # Must remain visible in pending/actionable proposals list
    pending = get_pending_filing_proposals(db)
    assert any(p["id"] == proposal_id and p["status"] == "needs_recovery" for p in pending)


def test_recovery_safe_to_retry_after_restart(test_env):
    """Safeguard 5: Ensure recovery can safely be retried after a restart without moving unrelated files."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.Restart.mp4"
    jpg = incoming / "Jane.Doe.Restart.jpg"
    dest_video = dest_folder / video.name
    dest_jpg = dest_folder / jpg.name

    dest_video.write_bytes(b"VIDEO_DATA")
    dest_jpg.write_bytes(b"JPG_DATA")

    # Add other unrelated files in both directories
    (incoming / "unrelated_other.mp4").write_bytes(b"OTHER_IN")
    (dest_folder / "unrelated_dest.mp4").write_bytes(b"OTHER_DEST")

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, companions_json, status, created_at, updated_at
        ) VALUES ('f54', 's54', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', ?, 'needs_recovery', ?, ?)""",
        (
            str(video), str(dest_video), str(dest_folder), video.name,
            json.dumps([{"source": str(jpg), "target": str(dest_jpg)}]),
            utc_now(), utc_now()
        )
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    # Simulate restart: fresh connection, fresh mock
    mock_stash = MagicMock()
    def mock_move(payload):
        target = Path(payload["destination_folder"]) / payload["destination_basename"]
        if dest_video.exists():
            dest_video.rename(target)
        return True
    mock_stash.move_files.side_effect = mock_move
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s54", "files": [{"id": "f54", "path": str(video)}]}
    }

    # Execute recovery after restart
    res = recover_filing_proposal(db, mock_stash, proposal_id)
    assert res["recovered"] is True

    # Target files safely restored
    assert video.exists() and video.read_bytes() == b"VIDEO_DATA"
    assert jpg.exists() and jpg.read_bytes() == b"JPG_DATA"
    assert not dest_video.exists()
    assert not dest_jpg.exists()

    # Unrelated files completely untouched
    assert (incoming / "unrelated_other.mp4").read_bytes() == b"OTHER_IN"
    assert (dest_folder / "unrelated_dest.mp4").read_bytes() == b"OTHER_DEST"


def test_approval_fail_closed_on_stash_ownership_query(test_env):
    """Safeguard: Approval must fail closed if Stash scene/file ownership query fails,
    returns incomplete data, or cannot confirm exact file ID and original path."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.FailClosed.mp4"
    video.write_bytes(b"V" * 150000)

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, companions_json, status, created_at, updated_at
        ) VALUES ('f90', 's90', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', '[]', 'pending', ?, ?)""",
        (str(video), str(dest_folder / video.name), str(dest_folder), video.name, utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    valid_config = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoot": str(dest_root)
    }

    def reset_status():
        c = connect(db)
        c.execute("UPDATE filing_proposals SET status='pending' WHERE id=?", (proposal_id,))
        c.commit()
        c.close()

    # Case A: Stash query raises exception (network/server error)
    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = RuntimeError("Stash GraphQL connection refused")
    res = apply_filing_proposal(db, mock_stash, proposal_id, config=valid_config)
    assert res["status"] == "blocked"
    assert "ownership query failed" in res["reason"]

    # Case B: Stash query returns scene as None
    reset_status()
    mock_stash.call_GQL.side_effect = None
    mock_stash.call_GQL.return_value = {"findScene": None}
    res = apply_filing_proposal(db, mock_stash, proposal_id, config=valid_config)
    assert res["status"] == "blocked"
    assert "does not exist or returned incomplete data" in res["reason"]

    # Case C: Stash query returns scene with empty files list
    reset_status()
    mock_stash.call_GQL.return_value = {"findScene": {"id": "s90", "files": []}}
    res = apply_filing_proposal(db, mock_stash, proposal_id, config=valid_config)
    assert res["status"] == "blocked"
    assert "has no file records" in res["reason"]

    # Case D: Stash query returns different file ID
    reset_status()
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s90", "files": [{"id": "wrong_f99", "path": str(video)}]}
    }
    res = apply_filing_proposal(db, mock_stash, proposal_id, config=valid_config)
    assert res["status"] == "blocked"
    assert "file ID f90 not found in Stash scene" in res["reason"]

    # Case E: Stash query returns matching file ID but different path
    reset_status()
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s90", "files": [{"id": "f90", "path": "/other/path/video.mp4"}]}
    }
    res = apply_filing_proposal(db, mock_stash, proposal_id, config=valid_config)
    assert res["status"] == "blocked"
    assert "does not match proposal source path" in res["reason"]


def test_approval_fail_closed_on_missing_or_unverified_config(test_env):
    """Safeguard: Block approval if the current Incoming configuration or destination root
    is missing or cannot be verified. Do not skip those checks."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.ConfigFailClosed.mp4"
    video.write_bytes(b"V" * 150000)

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, companions_json, status, created_at, updated_at
        ) VALUES ('f91', 's91', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', '[]', 'pending', ?, ?)""",
        (str(video), str(dest_folder / video.name), str(dest_folder), video.name, utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s91", "files": [{"id": "f91", "path": str(video)}]}
    }

    def reset_status():
        c = connect(db)
        c.execute("UPDATE filing_proposals SET status='pending' WHERE id=?", (proposal_id,))
        c.commit()
        c.close()

    # Case A: Missing incoming folders in config
    res = apply_filing_proposal(db, mock_stash, proposal_id, config={"incomingFolders": [], "autoFilingDestinationRoot": str(dest_root)})
    assert res["status"] == "blocked"
    assert "Incoming configuration is missing or empty" in res["reason"]

    # Case B: Source file not inside configured incoming folder
    reset_status()
    res = apply_filing_proposal(db, mock_stash, proposal_id, config={"incomingFolders": ["/unrelated/incoming"], "autoFilingDestinationRoot": str(dest_root)})
    assert res["status"] == "blocked"
    assert "not located in any currently configured Incoming folder" in res["reason"]

    # Case C: Missing autoFilingDestinationRoot
    reset_status()
    res = apply_filing_proposal(db, mock_stash, proposal_id, config={"incomingFolders": [str(incoming)], "autoFilingDestinationRoot": ""})
    assert res["status"] == "blocked"
    assert "autoFilingDestinationRoot is not configured" in res["reason"]

    # Case D: Configured destination root does not exist on disk
    reset_status()
    non_existent_root = dest_root / "does_not_exist_99"
    res = apply_filing_proposal(db, mock_stash, proposal_id, config={"incomingFolders": [str(incoming)], "autoFilingDestinationRoot": str(non_existent_root)})
    assert res["status"] == "blocked"
    assert "configured destination root does not exist on disk" in res["reason"]

    # Case E: Destination folder is not under configured destination root
    reset_status()
    other_root = test_env["tmp"] / "OtherRoot"
    other_root.mkdir()
    res = apply_filing_proposal(db, mock_stash, proposal_id, config={"incomingFolders": [str(incoming)], "autoFilingDestinationRoot": str(other_root)})
    assert res["status"] == "blocked"
    assert "not under current destination root" in res["reason"]


def test_stash_move_failure_clean_requires_both_disk_and_stash_record(test_env):
    """Safeguard: After a failed Stash move call, classify the outcome as clean ONLY when
    BOTH disk state AND Stash's exact file record confirm the original location.
    Otherwise retain needs_recovery."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.MoveFailureClean.mp4"
    video.write_bytes(b"V" * 150000)
    dest_video = dest_folder / video.name

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, companions_json, status, created_at, updated_at
        ) VALUES ('f92', 's92', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', '[]', 'pending', ?, ?)""",
        (str(video), str(dest_video), str(dest_folder), video.name, utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    valid_config = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoot": str(dest_root)
    }

    def reset_status():
        c = connect(db)
        c.execute("UPDATE filing_proposals SET status='pending' WHERE id=?", (proposal_id,))
        c.commit()
        c.close()

    # Case A: Clean failure - BOTH disk state confirms source AND Stash file record confirms source
    mock_stash = MagicMock()
    mock_stash.move_files.return_value = False
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s92", "files": [{"id": "f92", "path": str(video)}]}
    }
    res = apply_filing_proposal(db, mock_stash, proposal_id, config=valid_config)
    assert res["status"] == "failed"
    assert "video untouched at source and confirmed in Stash" in res["reason"]
    assert video.exists()
    assert not dest_video.exists()

    # Case B: Disk is at source, BUT Stash query fails on verification -> must retain needs_recovery!
    reset_status()
    # Call 1 (approval revalidation) succeeds; Call 2 (post-move failure verification) fails
    mock_stash.call_GQL.side_effect = [
        {"findScene": {"id": "s92", "files": [{"id": "f92", "path": str(video)}]}},
        RuntimeError("Stash crashed while verifying file record")
    ]
    res = apply_filing_proposal(db, mock_stash, proposal_id, config=valid_config)
    assert res["status"] == "needs_recovery"
    assert "CRITICAL" in res["reason"]
    assert "Stash record unconfirmed" in res["reason"]

    # Case C: Disk is at source, BUT Stash file record shows destination -> must retain needs_recovery!
    reset_status()
    mock_stash.call_GQL.side_effect = [
        {"findScene": {"id": "s92", "files": [{"id": "f92", "path": str(video)}]}},
        {"findScene": {"id": "s92", "files": [{"id": "f92", "path": str(dest_video)}]}}
    ]
    res = apply_filing_proposal(db, mock_stash, proposal_id, config=valid_config)
    assert res["status"] == "needs_recovery"
    assert "CRITICAL" in res["reason"]

    # Case D: Stash claims source, BUT disk video moved to destination -> must retain needs_recovery!
    reset_status()
    def partial_move(payload):
        video.rename(dest_video)
        return False
    mock_stash.move_files.side_effect = partial_move
    mock_stash.call_GQL.side_effect = None
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s92", "files": [{"id": "f92", "path": str(video)}]}
    }
    res = apply_filing_proposal(db, mock_stash, proposal_id, config=valid_config)
    assert res["status"] == "needs_recovery"
    assert "CRITICAL" in res["reason"]
    assert "video exists at destination" in res["reason"]


def test_preflight_blocks_missing_companion_or_destination_collision_without_overwriting(test_env):
    """Safeguard: Recheck every recorded companion's source and destination immediately
    before moving anything. Block missing companions and destination collisions without overwriting files."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.Precheck.mp4"
    video.write_bytes(b"V" * 150000)
    jpg = incoming / "Jane.Doe.Precheck.jpg"
    jpg.write_bytes(b"J" * 2000)
    dest_video = dest_folder / video.name
    dest_jpg = dest_folder / jpg.name

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, companions_json, status, created_at, updated_at
        ) VALUES ('f93', 's93', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', ?, 'pending', ?, ?)""",
        (
            str(video), str(dest_video), str(dest_folder), video.name,
            json.dumps([{"source": str(jpg), "target": str(dest_jpg)}]),
            utc_now(), utc_now()
        )
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    valid_config = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoot": str(dest_root)
    }

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s93", "files": [{"id": "f93", "path": str(video)}]}
    }

    # Case A: Destination companion collision already exists!
    dest_jpg.write_bytes(b"COLLISION_DATA_DO_NOT_OVERWRITE")

    res = apply_filing_proposal(db, mock_stash, proposal_id, config=valid_config)
    assert res["status"] == "blocked"
    assert "Preflight companion check failed: destination collision already exists" in res["reason"]

    # Crucial safety assertion: Zero overwriting, zero moving!
    assert dest_jpg.read_bytes() == b"COLLISION_DATA_DO_NOT_OVERWRITE"
    assert video.exists(), "Video must NOT be moved on companion collision"
    assert jpg.exists(), "Companion must NOT be moved on collision"
    assert not dest_video.exists()
    assert mock_stash.move_files.call_count == 0, "Stash move_files must not be called"

    # Case B: Recorded companion source has gone missing from disk
    dest_jpg.unlink()
    jpg.unlink()

    con = connect(db)
    con.execute("UPDATE filing_proposals SET status='pending' WHERE id=?", (proposal_id,))
    con.commit()
    con.close()

    res = apply_filing_proposal(db, mock_stash, proposal_id, config=valid_config)
    assert res["status"] == "blocked"
    assert "Preflight companion check failed: recorded companion missing from source" in res["reason"]
    assert video.exists(), "Video must NOT be moved if companion is missing"
    assert not dest_video.exists()
    assert mock_stash.move_files.call_count == 0, "Stash move_files must not be called"


def test_longest_match_performer_subsumption_jamie_ray_vs_jamie_sanders():
    """Safeguard: Prefer a complete canonical-name match over a shorter alias
    contained within the same text span. (Jamie Ray vs Jamie Sanders via alias Jamie)"""
    performers = [
        {"id": "227", "name": "Jamie Ray", "disambiguation": "Helix Studios", "alias_list": []},
        {"id": "61", "name": "Jamie Sanders", "disambiguation": "", "alias_list": ["Jamie"]},
    ]

    # Filename: Jamie Ray.mp4
    # "Jamie Ray" is complete canonical name (0..9)
    # "Jamie" is shorter alias of Jamie Sanders (0..5) fully contained in "Jamie Ray"
    res = match_performer_for_filing({}, "Jamie Ray.mp4", performers)
    assert res["matched"] is True
    assert res["entity"]["id"] == "227"
    assert res["entity"]["name"] == "Jamie Ray"
    assert res["matched_alias"] is None


def test_genuine_two_performer_filenames_preserve_ambiguity():
    """Safeguard: Preserve ambiguity when genuinely separate performers match."""
    performers = [
        {"id": "100", "name": "Devin Trez", "disambiguation": "", "alias_list": []},
        {"id": "227", "name": "Jamie Ray", "disambiguation": "", "alias_list": []},
        {"id": "61", "name": "Jamie Sanders", "disambiguation": "", "alias_list": ["Jamie"]},
    ]

    # Filename contains Devin Trez AND Jamie Ray
    # Shorter alias "Jamie" for Jamie Sanders is subsumed under Jamie Ray,
    # but Devin Trez and Jamie Ray are genuinely separate performers in the title.
    res = match_performer_for_filing({}, "Devin Trez, Jamie Ray.mp4", performers)
    assert res["matched"] is True
    assert len(res["entities"]) == 2
    matched_names = {e["name"] for e in res["entities"]}
    assert "Devin Trez" in matched_names
    assert "Jamie Ray" in matched_names
    assert "Jamie Sanders" not in matched_names  # Jamie Sanders was safely subsumed, not a false competitor!


def test_conflicting_identities_same_span_preserve_ambiguity():
    """Safeguard: When conflicting identities match the same text span without a
    canonical-name tie-breaker, preserve ambiguity."""
    # Case A: Two different performers sharing the same alias
    performers_alias = [
        {"id": "1", "name": "Jane Walker", "disambiguation": "", "alias_list": ["Star"]},
        {"id": "2", "name": "Mary Runner", "disambiguation": "", "alias_list": ["Star"]},
    ]
    res_a = match_performer_for_filing({}, "Star Solo Scene.mp4", performers_alias)
    assert res_a["matched"] is False

    # Case B: Two different performers with the exact same canonical name
    performers_duplicate_name = [
        {"id": "10", "name": "Alex Smith", "disambiguation": "Studio A", "alias_list": []},
        {"id": "20", "name": "Alex Smith", "disambiguation": "Studio B", "alias_list": []},
    ]
    res_b = match_performer_for_filing({}, "Alex Smith Live.mp4", performers_duplicate_name)
    assert res_b["matched"] is False
    assert "Ambiguous performer match" in res_b["reason"] or "Multiple performers" in res_b["reason"]


def test_longest_match_studio_subsumption_and_ambiguity():
    """Safeguard: Studio matching also implements longest-match subsumption and preserves ambiguity."""
    studios = [
        {"id": "1", "name": "Helix Studios", "aliases": []},
        {"id": "2", "name": "Studio X", "aliases": ["Helix"]},
        {"id": "3", "name": "Evil Angel", "aliases": []},
    ]

    # Subsumption: Helix Studios (0..13) subsumes Studio X's alias "Helix" (0..5)
    res_subsume = match_studio_for_filing({}, "Helix Studios - Big Scene.mp4", studios)
    assert res_subsume["matched"] is True
    assert res_subsume["entity"]["name"] == "Helix Studios"

    # Ambiguity preserved when two distinct studios appear
    res_multi = match_studio_for_filing({}, "Helix Studios and Evil Angel Collab.mp4", studios)
    assert res_multi["matched"] is False
    assert "Multiple studios" in res_multi["reason"]


def test_stale_filing_proposal_automatically_invalidated_on_file_or_scene_deletion(test_env):
    db = test_env["db"]
    incoming = test_env["incoming"]

    # 1. Proposal with source file on disk + existing Stash scene
    vid_a = incoming / "Jane Doe - Video A.mp4"
    vid_a.write_bytes(b"dummy video A content")

    # 2. Proposal with source file on disk, but scene deleted from Stash
    vid_b = incoming / "Jane Doe - Video B.mp4"
    vid_b.write_bytes(b"dummy video B content")

    # 3. Proposal with source file already deleted from disk
    vid_c = incoming / "Jane Doe - Video C.mp4"
    # Not created on disk

    # 4. Proposal with needs_recovery status whose file is absent (must NOT be invalidated)
    vid_rec = incoming / "Jane Doe - Recovery.mp4"

    now = utc_now()
    con = connect(db)
    con.execute(
        """INSERT INTO filing_proposals (
            id, file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES (101, 'f101', 's101', ?, '/dst/a.mp4', '/dst', 'a.mp4', 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', 'pending', ?, ?)""",
        (str(vid_a), now, now)
    )
    con.execute(
        """INSERT INTO filing_proposals (
            id, file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES (102, 'f102', 's102', ?, '/dst/b.mp4', '/dst', 'b.mp4', 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', 'pending', ?, ?)""",
        (str(vid_b), now, now)
    )
    con.execute(
        """INSERT INTO filing_proposals (
            id, file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES (103, 'f103', 's103', ?, '/dst/c.mp4', '/dst', 'c.mp4', 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', 'pending', ?, ?)""",
        (str(vid_c), now, now)
    )
    con.execute(
        """INSERT INTO filing_proposals (
            id, file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES (104, 'f104', 's104', ?, '/dst/rec.mp4', '/dst', 'rec.mp4', 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', 'needs_recovery', ?, ?)""",
        (str(vid_rec), now, now)
    )
    con.commit()
    con.close()

    mock_stash = MagicMock()
    # s101 exists in Stash; s102 does NOT exist (was deleted)
    def mock_call_gql(query, variables=None):
        variables = variables or {}
        sid = str(variables.get("id"))
        if sid == "s101":
            return {"findScene": {"id": "s101"}}
        return {"findScene": None}

    mock_stash.call_GQL.side_effect = mock_call_gql

    # Retrieve pending proposals with stash validation
    pending = get_pending_filing_proposals(db, stash=mock_stash)
    pending_ids = [p["id"] for p in pending]

    # Proposal 101 (valid file + valid scene) must remain
    assert 101 in pending_ids

    # Proposal 102 (scene deleted) must be invalidated and absent
    assert 102 not in pending_ids

    # Proposal 103 (file deleted) must be invalidated and absent
    assert 103 not in pending_ids

    # Proposal 104 (needs_recovery) must remain actionable
    assert 104 in pending_ids

    # Verify database statuses
    con = connect(db)
    cur = con.cursor()
    cur.execute("SELECT id, status, last_error FROM filing_proposals ORDER BY id")
    rows = {r["id"]: (r["status"], r["last_error"]) for r in cur.fetchall()}
    con.close()

    assert rows[101][0] == "pending"
    assert rows[102][0] == "invalid"
    assert "deleted" in str(rows[102][1])
    assert rows[103][0] == "invalid"
    assert "deleted" in str(rows[103][1])
    assert rows[104][0] == "needs_recovery"


# ---------------------------------------------------------------------------
# Phase 2 Feature Tests: Multiple Roots, Custom Mappings, Metadata, Torrents
# ---------------------------------------------------------------------------

def test_filing_roots_default_to_stash_library_roots_and_preserve_legacy_override():
    library_roots = ["/library/one", "/library/two"]

    assert get_configured_filing_destination_roots({"_libraryRoots": library_roots}) == library_roots
    assert get_configured_filing_destination_roots({
        "_libraryRoots": library_roots,
        "autoFilingDestinationRoots": ["/legacy/selected"],
    }) == ["/legacy/selected"]
    assert get_configured_filing_destination_roots({
        "_libraryRoots": library_roots,
        "autoFilingDestinationRoots": ["/legacy/selected"],
        "autoFilingDestinationRootsOverride": False,
    }) == library_roots
    assert get_configured_filing_destination_roots({
        "_libraryRoots": library_roots,
        "autoFilingDestinationRoots": ["/chosen"],
        "autoFilingDestinationRootsOverride": True,
    }) == ["/chosen"]

def test_phase2_multiple_destination_roots_single_match(test_env):
    """Phase 2.1: Search all configured roots. Propose when exactly one matches."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    vault1_performers = test_env["dest_root"]  # Contains "Jane Doe"
    vault2_performers = test_env["tmp"] / "Vault2_Performers"
    vault2_performers.mkdir()
    (vault2_performers / "Sarah Connor").mkdir()

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Jane.Doe.MultiRoot.mp4"
    video.write_bytes(b"V" * 150000)

    config = {
        "autoFilingEnabled": True,
        "autoFilingDestinationRoots": [str(vault1_performers), str(vault2_performers)],
        "incomingFolders": [str(incoming)]
    }

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "allPerformers": [{"id": "10", "name": "Jane Doe", "disambiguation": "", "alias_list": []}]
    }

    prop = evaluate_filing_proposal(db, mock_stash, str(video), {"id": "s201", "files": [{"id": "f201"}]}, config)
    assert prop is not None
    assert prop["status"] == "pending"
    assert prop["destination_folder"] == str(vault1_performers / "Jane Doe")
    assert len(prop["candidate_destinations"]) == 0


def test_phase2_multiple_destination_roots_ambiguous_requires_choice(test_env):
    """Phase 2.1: If multiple destination folders match across roots, present candidates and require choice."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    vault1_performers = test_env["dest_root"]  # Contains "Jane Doe"
    vault2_performers = test_env["tmp"] / "Vault2_Performers"
    vault2_performers.mkdir()
    (vault2_performers / "Jane Doe").mkdir()   # Also contains "Jane Doe"

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Jane.Doe.AmbiguousRoots.mp4"
    video.write_bytes(b"V" * 150000)

    config = {
        "autoFilingEnabled": True,
        "autoFilingDestinationRoots": [str(vault1_performers), str(vault2_performers)],
        "incomingFolders": [str(incoming)]
    }

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "allPerformers": [{"id": "10", "name": "Jane Doe", "disambiguation": "", "alias_list": []}]
    }

    prop = evaluate_filing_proposal(db, mock_stash, str(video), {"id": "s202", "files": [{"id": "f202"}]}, config)
    assert prop is not None
    assert prop["status"] == "pending"
    assert len(prop["candidate_destinations"]) == 2
    cand_paths = [c.get("destination_folder") if isinstance(c, dict) else str(c) for c in prop["candidate_destinations"]]
    assert str(vault1_performers / "Jane Doe") in cand_paths
    assert str(vault2_performers / "Jane Doe") in cand_paths

    # User approves with explicit choice of Vault2
    chosen_dest = str(vault2_performers / "Jane Doe")
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s202", "files": [{"id": "f202", "path": str(video)}]}
    }
    mock_stash.move_files.return_value = True

    res = apply_filing_proposal(
        db, mock_stash, prop["id"],
        config=config,
        target_destination_folder=chosen_dest
    )
    assert res["status"] == "completed"
    assert res["proposed_path"] == str(vault2_performers / "Jane Doe" / video.name)


def test_phase2_custom_folder_mappings_validation_and_usage(test_env):
    """Phase 2.2: Custom folder mappings using stable Stash IDs, path validation, and duplicate detection."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    custom_folder = dest_root / "Jamie Ray Collection"
    custom_folder.mkdir()

    # 1. Validation: Reject non-existent folder
    ok, err = save_filing_folder_mapping(
        db, "performer", "99", "Jamie Ray",
        str(dest_root / "NonExistentFolder"),
        configured_roots=[str(dest_root)]
    )
    assert ok is False
    assert "does not exist on disk" in err

    # 2. Validation: Reject folder outside configured destination roots
    outside_folder = test_env["tmp"] / "UnconfiguredRoot" / "Jamie"
    outside_folder.mkdir(parents=True)
    ok, err = save_filing_folder_mapping(
        db, "performer", "99", "Jamie Ray",
        str(outside_folder),
        configured_roots=[str(dest_root)]
    )
    assert ok is False
    assert "outside configured destination roots" in err

    # 3. Save valid mapping
    ok, msg = save_filing_folder_mapping(
        db, "performer", "99", "Jamie Ray",
        str(custom_folder),
        configured_roots=[str(dest_root)]
    )
    assert ok is True

    # 4. Multiple performers and studios can share the same destination folder
    ok, msg = save_filing_folder_mapping(
        db, "performer", "100", "Another Performer",
        str(custom_folder),
        configured_roots=[str(dest_root)]
    )
    assert ok is True

    # 5. Usage in automatic filing evaluation
    snapshot_incoming_baseline(db, [str(incoming)])
    video = incoming / "Jamie Ray New Video.mp4"
    video.write_bytes(b"V" * 150000)

    config = {
        "autoFilingEnabled": True,
        "autoFilingDestinationRoots": [str(dest_root)],
        "incomingFolders": [str(incoming)]
    }

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "allPerformers": [{"id": "99", "name": "Jamie Ray", "disambiguation": "", "alias_list": []}]
    }

    prop = evaluate_filing_proposal(db, mock_stash, str(video), {"id": "s203", "files": [{"id": "f203"}]}, config)
    assert prop is not None
    assert prop["is_custom_mapped"] is True
    assert prop["destination_folder"] == str(custom_folder)

    # 6. Deletion of mapping
    mappings = get_filing_folder_mappings(db)
    assert len(mappings) == 2
    deleted = delete_filing_folder_mapping(db, mappings[0]["id"])
    assert deleted is True
    assert len(get_filing_folder_mappings(db)) == 1
    delete_filing_folder_mapping(db, mappings[1]["id"])
    assert len(get_filing_folder_mappings(db)) == 0


def test_legacy_name_based_tag_mapping_resolves_current_stash_tag_id(test_env):
    """Mappings saved by the old UI with a name in entity_id remain usable."""
    db = test_env["db"]
    root = test_env["dest_root"]
    mapped = root / "Hot Young Brit"
    mapped.mkdir()

    assert save_filing_folder_mapping(
        db, "tag", "HYB", "HYB", str(mapped), [str(root)]
    )[0]
    assert save_filing_folder_mapping(
        db, "tag", "hyb", "hyb", str(mapped), [str(root)]
    )[0]

    destinations, status = librarymanager_core.resolve_tag_filing_destinations(
        [str(root)], {"id": "588", "name": "hyb", "aliases": []},
        database_path=db,
    )

    assert status == "custom_mapping"
    assert destinations == [mapped.resolve()]


def test_phase2_optional_metadata_updates_default_move_only(test_env):
    """Phase 2.3: By default, approving a filing proposal moves only and leaves metadata untouched."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.MoveOnly.mp4"
    video.write_bytes(b"V" * 150000)

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES ('f204', 's204', ?, ?, ?, ?, 'performer', '10', 'Jane Doe', NULL, 'filename', 'test', 'pending', ?, ?)""",
        (str(video), str(dest_folder / video.name), str(dest_folder), video.name, utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s204", "files": [{"id": "f204", "path": str(video)}]}
    }
    mock_stash.move_files.return_value = True

    config = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoots": [str(dest_root)]
    }

    res = apply_filing_proposal(db, mock_stash, proposal_id, config=config, update_metadata=False)
    assert res["status"] == "completed"
    assert res["metadata_updated"] is False
    # Verify no sceneUpdate mutation was sent
    update_calls = [c for c in mock_stash.call_GQL.call_args_list if "sceneUpdate" in str(c)]
    assert len(update_calls) == 0


def test_phase2_optional_metadata_updates_appends_performer_preserves_existing(test_env):
    """Phase 2.3: When update_metadata=True, append matched performer to scene without replacing existing performers."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.WithMeta.mp4"
    video.write_bytes(b"V" * 150000)

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES ('f205', 's205', ?, ?, ?, ?, 'performer', '10', 'Jane Doe', NULL, 'filename', 'test', 'pending', ?, ?)""",
        (str(video), str(dest_folder / video.name), str(dest_folder), video.name, utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    mock_stash = MagicMock()
    # Scene already has performer 'Existing Performer' (ID 5)
    mock_stash.call_GQL.side_effect = [
        # 1. Preflight scene ownership check
        {"findScene": {"id": "s205", "files": [{"id": "f205", "path": str(video)}]}},
        # 2. Query scene metadata before update
        {"findScene": {"id": "s205", "performers": [{"id": "5", "name": "Existing Performer"}], "studio": None}},
        # 3. Scene update mutation response
        {"sceneUpdate": {"id": "s205"}}
    ]
    mock_stash.move_files.return_value = True

    config = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoots": [str(dest_root)]
    }

    res = apply_filing_proposal(db, mock_stash, proposal_id, config=config, update_metadata=True)
    assert res["status"] == "completed"
    assert res["metadata_updated"] is True

    # Verify sceneUpdate payload contained BOTH performer IDs: 5 (existing) and 10 (new)
    mutation_call = mock_stash.call_GQL.call_args_list[-1]
    variables = mutation_call[0][1]
    assert variables["input"]["performer_ids"] == ["5", "10"]


def test_phase2_metadata_update_failure_does_not_undo_successful_move(test_env):
    """Phase 2.3: Failure in metadata update does NOT rollback or fail the successful file move."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Jane.Doe.MetaFail.mp4"
    video.write_bytes(b"V" * 150000)

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES ('f206', 's206', ?, ?, ?, ?, 'performer', '10', 'Jane Doe', NULL, 'filename', 'test', 'pending', ?, ?)""",
        (str(video), str(dest_folder / video.name), str(dest_folder), video.name, utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = [
        {"findScene": {"id": "s206", "files": [{"id": "f206", "path": str(video)}]}},
        RuntimeError("Stash metadata mutation timeout")
    ]
    mock_stash.move_files.return_value = True

    config = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoots": [str(dest_root)]
    }

    res = apply_filing_proposal(db, mock_stash, proposal_id, config=config, update_metadata=True)
    assert res["status"] == "completed"
    assert res["metadata_updated"] is False
    assert "Stash metadata mutation timeout" in res["metadata_error"]
    # File move remains completed and intact
    assert (dest_folder / video.name).exists() or mock_stash.move_files.called


def test_phase2_multi_video_nested_torrent_folder_independent_evaluation(test_env):
    """Phase 2.4: Evaluate each video in nested folders independently; preserve shared files and directory structure."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_jane = dest_root / "Jane Doe"
    dest_mary = dest_root / "Mary Smith"

    torrent_folder = incoming / "TorrentPack_Nested_2026"
    sub1 = torrent_folder / "Disc1"
    sub2 = torrent_folder / "Disc2"
    sub1.mkdir(parents=True)
    sub2.mkdir(parents=True)

    video1 = sub1 / "Disc1_Jane.Doe.mp4"
    video1.write_bytes(b"V1" * 80000)
    sidecar1 = sub1 / "Disc1_Jane.Doe.mp4.jpg"
    sidecar1.write_bytes(b"J1" * 2000)

    video2 = sub2 / "Disc2_Mary.Smith.mp4"
    video2.write_bytes(b"V2" * 80000)

    # Shared torrent files that must NOT be moved
    shared_nfo = torrent_folder / "release.nfo"
    shared_nfo.write_bytes(b"SHARED NFO")
    readme = sub1 / "README.txt"
    readme.write_bytes(b"README")

    snapshot_incoming_baseline(db, [str(incoming)])

    new_vid1 = sub1 / "Disc1_Jane.Doe_New.mp4"
    new_vid1.write_bytes(b"V1_NEW" * 80000)
    new_sidecar1 = sub1 / "Disc1_Jane.Doe_New.mp4.jpg"
    new_sidecar1.write_bytes(b"J1_NEW" * 2000)

    new_vid2 = sub2 / "Disc2_Mary.Smith_New.mp4"
    new_vid2.write_bytes(b"V2_NEW" * 80000)

    config = {
        "autoFilingEnabled": True,
        "autoFilingDestinationRoots": [str(dest_root)],
        "incomingFolders": [str(incoming)]
    }

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "allPerformers": [
            {"id": "1", "name": "Jane Doe", "disambiguation": "", "alias_list": []},
            {"id": "2", "name": "Mary Smith", "disambiguation": "", "alias_list": []}
        ]
    }

    # Evaluate Video 1
    prop1 = evaluate_filing_proposal(db, mock_stash, str(new_vid1), {"id": "s301", "files": [{"id": "f301"}]}, config)
    assert prop1 is not None
    assert prop1["matched_entity_name"] == "Jane Doe"
    assert prop1["destination_folder"] == str(dest_jane)
    assert prop1["in_nested_folder"] is True
    assert prop1["companions_count"] == 1  # Only new_sidecar1, not shared_nfo or readme

    # Evaluate Video 2
    prop2 = evaluate_filing_proposal(db, mock_stash, str(new_vid2), {"id": "s302", "files": [{"id": "f302"}]}, config)
    assert prop2 is not None
    assert prop2["matched_entity_name"] == "Mary Smith"
    assert prop2["destination_folder"] == str(dest_mary)
    assert prop2["in_nested_folder"] is True

    # Shared files remain untouched in torrent folder
    assert shared_nfo.exists()
    assert readme.exists()


def test_auto_filing_trigger_metadata_mode_waits_for_metadata(test_env):
    """Requirement 1: 'When to suggest filing' -> 'metadata' mode.
    Waits for performer/studio to be assigned in Stash before generating proposal.
    Does not create repeated proposals for the same scene."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Jane.Doe.NewVideo.mp4"
    video.write_bytes(b"V" * 150000)

    config = {
        "autoFilingEnabled": True,
        "autoFilingTrigger": "metadata",
        "autoFilingDestinationRoots": [str(dest_root)],
        "incomingFolders": [str(incoming)]
    }

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "allPerformers": [{"id": "1", "name": "Jane Doe", "disambiguation": "", "alias_list": []}]
    }

    # Step 1: Newly imported scene has NO performer metadata yet -> Proposal skipped
    scene_without_meta = {"id": "s401", "files": [{"id": "f401"}], "performers": [], "studio": None}
    prop = evaluate_filing_proposal(db, mock_stash, str(video), scene_without_meta, config)
    assert prop is None

    # Step 2: Performer assigned in Stash -> Proposal generated
    scene_with_meta = {
        "id": "s401",
        "files": [{"id": "f401"}],
        "performers": [{"id": "1", "name": "Jane Doe"}],
        "studio": None
    }
    prop = evaluate_filing_proposal(db, mock_stash, str(video), scene_with_meta, config)
    assert prop is not None
    assert prop["status"] == "pending"
    assert prop["matched_entity_name"] == "Jane Doe"

    # Step 3: Subsequent metadata edits on the same scene do NOT create duplicate proposals
    prop_dup = evaluate_filing_proposal(db, mock_stash, str(video), scene_with_meta, config)
    assert prop_dup is None


def test_auto_filing_moves_without_renaming_and_leaves_renaming_control_to_setting(test_env):
    """Automatic Filing moves files without changing filenames and without permanent rename protection.
    Automatic Renaming alone governs subsequent metadata-triggered renaming."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "Original_Custom_Name_Jane_Doe.mp4"
    video.write_bytes(b"V" * 150000)
    dest_video = dest_folder / video.name

    con = connect(db)
    # Insert filed scene record and another unrelated scene in files table
    con.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, title, studio, performers_json, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f501', 's501', ?, ?, 150000, 'Original Title', 'Jane Studio', '["Jane Doe"]', 1, ?, ?)""",
        (str(video), video.name, utc_now(), utc_now())
    )
    unrelated_video = dest_root / "Unrelated_Scene.mp4"
    unrelated_video.write_bytes(b"U" * 150000)
    con.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, title, studio, performers_json, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f502', 's502', ?, ?, 150000, 'Unrelated Title', 'StudioX', '["Mary Smith"]', 1, ?, ?)""",
        (str(unrelated_video), unrelated_video.name, utc_now(), utc_now())
    )
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES ('f501', 's501', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', 'pending', ?, ?)""",
        (str(video), str(dest_video), str(dest_folder), video.name, utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s501", "files": [{"id": "f501", "path": str(video)}]}
    }
    mock_stash.move_files.return_value = True

    config = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoots": [str(dest_root)]
    }

    # Apply filing move
    res = apply_filing_proposal(db, mock_stash, proposal_id, config=config)
    assert res["status"] == "completed"

    # 1. Verify file was moved with original filename preserved
    con = connect(db)
    f501_file = con.execute("SELECT path, basename FROM files WHERE file_id='f501'").fetchone()
    assert f501_file["basename"] == "Original_Custom_Name_Jane_Doe.mp4"
    assert f501_file["path"] == str(dest_video)

    # 2. Verify no permanent rename_protected flag was created
    f501_state = con.execute("SELECT rename_protected FROM filename_state WHERE file_id='f501'").fetchone()
    assert f501_state is None or f501_state["rename_protected"] == 0
    con.close()

    # 3. Preview calculates standard safe rename candidate based on metadata
    rename_options = {"filenameStyle": "studio_performers_title"}
    preview = preview_scene_filename(db, "s501", rename_options)
    assert preview["status"] == "ready"
    assert Path(preview["proposed_path"]).name == "Original Title - Jane Studio - Jane Doe.mp4"

    # 4. Explicit manual rename remains possible
    moves = []
    apply_res = apply_scene_filename(
        db, "s501",
        lambda file_id, folder, basename: moves.append((file_id, folder, basename)) or True,
        rename_options
    )
    assert apply_res["status"] == "renamed"
    assert len(moves) == 1
    assert moves[0][2] == "Original Title - Jane Studio - Jane Doe.mp4"


def test_shared_studio_destination_folder_mapping_and_independent_removal(tmp_path: Path):
    from librarymanager_core import (
        save_filing_folder_mapping,
        get_filing_folder_mappings,
        delete_filing_folder_mapping,
        resolve_filing_destinations,
        evaluate_filing_proposal,
        SCHEMA, connect
    )

    db = tmp_path / "watchtower.sqlite3"
    con = connect(db)
    con.executescript(SCHEMA)
    con.close()

    root = tmp_path / "Vault1"
    helix_folder = root / "Helix"
    helix_folder.mkdir(parents=True)
    sep_folder = root / "Separate Studio"
    sep_folder.mkdir(parents=True)
    incoming = tmp_path / "Incoming"
    incoming.mkdir(parents=True)


    # 1. Map multiple subsidiary studios to the same shared 'Helix' folder
    ok1, msg1 = save_filing_folder_mapping(db, "studio", "101", "8teenBoy", str(helix_folder), [str(root)])
    assert ok1, f"Failed mapping 8teenBoy: {msg1}"

    ok2, msg2 = save_filing_folder_mapping(db, "studio", "102", "FratBoy", str(helix_folder), [str(root)])
    assert ok2, f"Failed mapping FratBoy: {msg2}"

    ok3, msg3 = save_filing_folder_mapping(db, "studio", "103", "Helix Latin America", str(helix_folder), [str(root)])
    assert ok3, f"Failed mapping Helix Latin America: {msg3}"

    # 2. Map another studio to a separate folder
    ok4, msg4 = save_filing_folder_mapping(db, "studio", "104", "Separate Studio", str(sep_folder), [str(root)])
    assert ok4, f"Failed mapping Separate Studio: {msg4}"

    # Verify all 4 mappings exist
    mappings = get_filing_folder_mappings(db)
    assert len(mappings) == 4

    # 3. Verify destination resolution for each studio
    dests1, status1 = resolve_filing_destinations([str(root)], "8teenBoy", entity_id="101", entity_type="studio", database_path=db)
    assert status1 == "custom_mapping"
    assert dests1 == [helix_folder]

    dests2, status2 = resolve_filing_destinations([str(root)], "FratBoy", entity_id="102", entity_type="studio", database_path=db)
    assert status2 == "custom_mapping"
    assert dests2 == [helix_folder]

    dests3, status3 = resolve_filing_destinations([str(root)], "Helix Latin America", entity_id="103", entity_type="studio", database_path=db)
    assert status3 == "custom_mapping"
    assert dests3 == [helix_folder]

    dests4, status4 = resolve_filing_destinations([str(root)], "Separate Studio", entity_id="104", entity_type="studio", database_path=db)
    assert status4 == "custom_mapping"
    assert dests4 == [sep_folder]

    # 4. Delete one studio mapping ('FratBoy' ID 102)
    frat_mapping = next(m for m in mappings if m["entity_id"] == "102")
    del_ok = delete_filing_folder_mapping(db, frat_mapping["id"])
    assert del_ok

    # Verify other studios remain mapped to 'Helix'
    remaining = get_filing_folder_mappings(db)
    assert len(remaining) == 3
    assert {m["entity_name"] for m in remaining} == {"8teenBoy", "Helix Latin America", "Separate Studio"}

    dests1_after, _ = resolve_filing_destinations([str(root)], "8teenBoy", entity_id="101", entity_type="studio", database_path=db)
    assert dests1_after == [helix_folder]

    dests3_after, _ = resolve_filing_destinations([str(root)], "Helix Latin America", entity_id="103", entity_type="studio", database_path=db)
    assert dests3_after == [helix_folder]

    # 5. Verify performer mapping duplicate rejection is preserved
    perf_folder = root / "Performer A"
    perf_folder.mkdir(parents=True)
    save_filing_folder_mapping(db, "performer", "201", "Performer A", str(perf_folder), [str(root)])
    
    # Multiple performers can map to the same folder
    dup_ok, dup_msg = save_filing_folder_mapping(db, "performer", "202", "Performer B", str(perf_folder), [str(root)])
    assert dup_ok is True


def test_nested_folders_discovery_custom_mappings_and_cache_refresh(tmp_path: Path):
    from librarymanager_core import (
        resolve_filing_destinations,
        save_filing_folder_mapping,
        get_filing_folder_mappings,
        invalidate_destination_dir_cache,
        _DESTINATION_DIR_CACHE,
        SCHEMA, connect
    )

    db = tmp_path / "watchtower.sqlite3"
    con = connect(db)
    con.executescript(SCHEMA)
    con.close()

    vault1 = tmp_path / "Vault1"
    vault2 = tmp_path / "Vault2"
    
    # Nested folder hierarchy:
    # Vault1/Helix/ (depth 1)
    # Vault2/Studios/Helix/8teenBoy/ (depth 3)
    # Vault2/Collections/Networks/Helix/ (depth 3)
    # Vault2/Performers/Featured/Jamie Ray/ (depth 3)
    
    v1_helix = vault1 / "Helix"
    v1_helix.mkdir(parents=True)
    
    v2_8teen = vault2 / "Studios" / "Helix" / "8teenBoy"
    v2_8teen.mkdir(parents=True)
    
    v2_helix_net = vault2 / "Collections" / "Networks" / "Helix"
    v2_helix_net.mkdir(parents=True)

    v2_jamie = vault2 / "Performers" / "Featured" / "Jamie Ray"
    v2_jamie.mkdir(parents=True)

    roots = [str(vault1), str(vault2)]

    # 1. Test automatic discovery of nested destination folder (8teenBoy at depth 3)
    dests_8teen, status_8teen = resolve_filing_destinations(roots, "8teenBoy")
    assert status_8teen == "ok"
    assert dests_8teen == [v2_8teen.resolve()]

    # 2. Test automatic discovery with multiple matches across roots/depths (Helix in Vault1 and Vault2)
    # Matches: Vault1/Helix, Vault2/Studios/Helix, Vault2/Collections/Networks/Helix
    dests_helix, status_helix = resolve_filing_destinations(roots, "Helix")
    assert status_helix == "multiple_destinations"
    assert len(dests_helix) == 3
    assert set(dests_helix) == {v1_helix.resolve(), (vault2 / "Studios" / "Helix").resolve(), v2_helix_net.resolve()}

    # 3. Test nested performer automatic discovery
    dests_jamie, status_jamie = resolve_filing_destinations(roots, "Jamie Ray")
    assert status_jamie == "ok"
    assert dests_jamie == [v2_jamie.resolve()]

    # 4. Test directory cache: cache was populated during discovery
    assert len(_DESTINATION_DIR_CACHE) > 0

    # 5. Test custom mapping at arbitrary depth (e.g. mapping Studio 'FratBoy' to Vault2/Collections/Networks/Helix)
    ok_map, msg_map = save_filing_folder_mapping(
        db, "studio", "105", "FratBoy", str(v2_helix_net), roots
    )
    assert ok_map, f"Failed saving custom mapping: {msg_map}"

    # Verify custom mapping takes precedence and resolves directly to the nested network folder
    dests_frat, status_frat = resolve_filing_destinations(roots, "FratBoy", entity_id="105", entity_type="studio", database_path=db)
    assert status_frat == "custom_mapping"
    assert dests_frat == [v2_helix_net.resolve()]

    # 6. Test directory cache invalidation and refresh
    invalidate_destination_dir_cache()
    assert len(_DESTINATION_DIR_CACHE) == 0

    # New nested folder created on disk
    v2_new_studio = vault2 / "Studios" / "Indie" / "NewStudio"
    v2_new_studio.mkdir(parents=True)

    # Next resolution automatically discovers newly created nested folder and refreshes cache
    dests_new, status_new = resolve_filing_destinations(roots, "NewStudio")
    assert status_new == "ok"
    assert dests_new == [v2_new_studio.resolve()]
    assert len(_DESTINATION_DIR_CACHE) > 0


def test_nested_folder_cache_ttl_expiry_discovers_changes_with_unchanged_root_mtime(tmp_path: Path):
    from librarymanager_core import (
        resolve_filing_destinations,
        invalidate_destination_dir_cache,
        _get_cached_destination_subdirectories,
        _DESTINATION_DIR_CACHE
    )
    import time

    invalidate_destination_dir_cache()
    vault = tmp_path / "Vault_TTL"
    vault.mkdir()
    initial_sub = vault / "Studios" / "StudioA"
    initial_sub.mkdir(parents=True)

    # Initial resolution populates cache
    roots = [str(vault)]
    dests_a, status_a = resolve_filing_destinations(roots, "StudioA")
    assert status_a == "ok"
    assert dests_a == [initial_sub.resolve()]
    assert len(_DESTINATION_DIR_CACHE) > 0

    # Record root mtime
    root_mtime_before = vault.stat().st_mtime

    # Now create a deep nested folder: Vault_TTL/Studios/SubNetwork/DeepStudioB
    deep_sub = vault / "Studios" / "SubNetwork" / "DeepStudioB"
    deep_sub.mkdir(parents=True)

    # Explicitly ensure root mtime remains identical (simulating real OS behavior where deep directory changes do not touch root)
    os.utime(str(vault), (root_mtime_before, root_mtime_before))
    assert vault.stat().st_mtime == root_mtime_before

    # 1. Immediate call with cache warm (TTL not expired): returns cached result (DeepStudioB not yet found)
    entries_cached = _get_cached_destination_subdirectories(vault, max_depth=4, ttl=60.0)
    found_names = [name for _, name in entries_cached]
    assert "studioa" in found_names
    assert "deepstudiob" not in found_names

    # 2. Call after TTL expires (e.g. ttl=0.0): cache re-scans the tree despite root mtime being unchanged
    entries_fresh = _get_cached_destination_subdirectories(vault, max_depth=4, ttl=0.0)
    fresh_names = [name for _, name in entries_fresh]
    assert "studioa" in fresh_names
    assert "deepstudiob" in fresh_names

    # Resolution now discovers DeepStudioB
    # Invalidate cache and test resolve_filing_destinations
    invalidate_destination_dir_cache()
    dests_b, status_b = resolve_filing_destinations(roots, "DeepStudioB")
    assert status_b == "ok"
    assert dests_b == [deep_sub.resolve()]


def test_explicit_cache_refresh_mechanism(tmp_path: Path):
    from librarymanager_core import (
        resolve_filing_destinations,
        invalidate_destination_dir_cache,
        _DESTINATION_DIR_CACHE
    )

    invalidate_destination_dir_cache()
    vault = tmp_path / "Vault_Refresh"
    vault.mkdir()
    (vault / "Performers" / "InitialArtist").mkdir(parents=True)

    roots = [str(vault)]
    dests1, status1 = resolve_filing_destinations(roots, "InitialArtist")
    assert status1 == "ok"
    assert len(_DESTINATION_DIR_CACHE) > 0

    # Create new nested performer folder
    new_artist_dir = vault / "Performers" / "Featured" / "NewArtist"
    new_artist_dir.mkdir(parents=True)

    # Calling resolve without invalidating or TTL expiry would use warm cache
    # But user clicks 'Refresh Folders' or calls invalidate_destination_dir_cache()
    invalidate_destination_dir_cache()
    assert len(_DESTINATION_DIR_CACHE) == 0

    # Immediate resolution now discovers NewArtist without waiting
    dests2, status2 = resolve_filing_destinations(roots, "NewArtist")
    assert status2 == "ok"
    assert dests2 == [new_artist_dir.resolve()]

    # Test targeted invalidation for specific root
    invalidate_destination_dir_cache(str(vault))
    assert len(_DESTINATION_DIR_CACHE) == 0


def test_configurable_discovery_depth_bounding_and_clamping(tmp_path: Path):
    from librarymanager_core import (
        resolve_filing_destinations,
        invalidate_destination_dir_cache
    )

    invalidate_destination_dir_cache()
    vault = tmp_path / "Vault_Depth"
    vault.mkdir()

    # Create directory tree with distinct depths:
    # Level 1: Vault/L1_Studio (depth 1)
    # Level 2: Vault/D1/L2_Studio (depth 2)
    # Level 3: Vault/D1/D2/L3_Studio (depth 3)
    # Level 4: Vault/D1/D2/D3/L4_Studio (depth 4)
    # Level 5: Vault/D1/D2/D3/D4/L5_Studio (depth 5)
    # Level 6: Vault/D1/D2/D3/D4/D5/L6_Studio (depth 6)
    l1 = vault / "L1_Studio"
    l1.mkdir()
    l2 = vault / "D1" / "L2_Studio"
    l2.mkdir(parents=True)
    l3 = vault / "D1" / "D2" / "L3_Studio"
    l3.mkdir(parents=True)
    l4 = vault / "D1" / "D2" / "D3" / "L4_Studio"
    l4.mkdir(parents=True)
    l5 = vault / "D1" / "D2" / "D3" / "D4" / "L5_Studio"
    l5.mkdir(parents=True)
    l6 = vault / "D1" / "D2" / "D3" / "D4" / "D5" / "L6_Studio"
    l6.mkdir(parents=True)

    roots = [str(vault)]

    # max_depth=1: only depth 1 is searched
    invalidate_destination_dir_cache()
    d1, s1 = resolve_filing_destinations(roots, "L1_Studio", max_depth=1)
    assert s1 == "ok" and d1 == [l1.resolve()]
    d2, s2 = resolve_filing_destinations(roots, "L2_Studio", max_depth=1)
    assert "does not exist" in s2 and d2 == []

    # max_depth=2: depths 1 and 2 are searched
    invalidate_destination_dir_cache()
    d2_ok, s2_ok = resolve_filing_destinations(roots, "L2_Studio", max_depth=2)
    assert s2_ok == "ok" and d2_ok == [l2.resolve()]
    d3_no, s3_no = resolve_filing_destinations(roots, "L3_Studio", max_depth=2)
    assert "does not exist" in s3_no and d3_no == []

    # max_depth=4 (default): depths 1..4 are searched
    invalidate_destination_dir_cache()
    d4_ok, s4_ok = resolve_filing_destinations(roots, "L4_Studio", max_depth=4)
    assert s4_ok == "ok" and d4_ok == [l4.resolve()]
    d5_no, s5_no = resolve_filing_destinations(roots, "L5_Studio", max_depth=4)
    assert "does not exist" in s5_no and d5_no == []

    # max_depth=6: depths 1..6 are searched
    invalidate_destination_dir_cache()
    d6_ok, s6_ok = resolve_filing_destinations(roots, "L6_Studio", max_depth=6)
    assert s6_ok == "ok" and d6_ok == [l6.resolve()]

    # Safe depth clamping: max_depth=0 or negative clamped to 1
    invalidate_destination_dir_cache()
    dc1, sc1 = resolve_filing_destinations(roots, "L1_Studio", max_depth=-5)
    assert sc1 == "ok" and dc1 == [l1.resolve()]
    dc2, sc2 = resolve_filing_destinations(roots, "L2_Studio", max_depth=-5)
    assert "does not exist" in sc2 and dc2 == []

    # Safe depth clamping: max_depth=99 clamped to 8 (finds up to level 6)
    invalidate_destination_dir_cache()
    dc6, sc6 = resolve_filing_destinations(roots, "L6_Studio", max_depth=99)
    assert sc6 == "ok" and dc6 == [l6.resolve()]


def test_custom_folder_mapping_supports_folders_deeper_than_discovery_limit(tmp_path: Path):
    from librarymanager_core import (
        resolve_filing_destinations,
        save_filing_folder_mapping,
        evaluate_filing_proposal,
        snapshot_incoming_baseline,
        invalidate_destination_dir_cache,
        SCHEMA, connect
    )

    db = tmp_path / "custom_deep.sqlite3"
    con = connect(db)
    con.executescript(SCHEMA)
    con.close()

    incoming = tmp_path / "Incoming"
    incoming.mkdir()
    vault = tmp_path / "Vault_Custom"
    vault.mkdir()

    # Very deep directory (depth 7)
    deep_performer_dir = vault / "A" / "B" / "C" / "D" / "E" / "F" / "SuperDeepPerformer"
    deep_performer_dir.mkdir(parents=True)

    roots = [str(vault)]

    # 1. Automatic discovery with max_depth=2 cannot discover SuperDeepPerformer
    invalidate_destination_dir_cache()
    d_auto, s_auto = resolve_filing_destinations(roots, "SuperDeepPerformer", max_depth=2)
    assert "does not exist" in s_auto
    assert d_auto == []

    # 2. Configure a custom folder mapping for performer ID 777 to the deep folder
    ok_map, msg_map = save_filing_folder_mapping(
        db, "performer", "777", "SuperDeepPerformer", str(deep_performer_dir), roots
    )
    assert ok_map, f"Failed saving mapping: {msg_map}"

    # 3. Custom mapping resolution works even with max_depth=2
    d_custom, s_custom = resolve_filing_destinations(
        roots, "SuperDeepPerformer",
        entity_id="777",
        entity_type="performer",
        database_path=db,
        max_depth=2
    )
    assert s_custom == "custom_mapping"
    assert d_custom == [deep_performer_dir.resolve()]

    # 4. End-to-end proposal evaluation with autoFilingMaxDiscoveryDepth=2
    snapshot_incoming_baseline(db, [str(incoming)])

    new_video = incoming / "SuperDeepPerformer - Video 2026.mp4"
    new_video.write_bytes(b"TESTDATA" * 50000)

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "allPerformers": [{"id": "777", "name": "SuperDeepPerformer", "alias_list": []}]
    }

    scene_data = {
        "id": "scene_deep_1",
        "title": "SuperDeepPerformer Scene",
        "performers": [{"id": "777", "name": "SuperDeepPerformer"}],
        "files": [{"id": "file_deep_1", "path": str(new_video)}]
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingDestinationRoots": roots,
        "autoFilingMaxDiscoveryDepth": 2,
        "incomingFolders": [str(incoming)]
    }

    proposal = evaluate_filing_proposal(db, mock_stash, str(new_video), scene_data, config)
    assert proposal is not None
    assert proposal["status"] == "pending"
    assert proposal["is_custom_mapped"] is True
    assert proposal["destination_folder"] == str(deep_performer_dir.resolve())
    assert Path(proposal["proposed_path"]).parent == deep_performer_dir.resolve()


# =====================================================================
# =====================================================================
# Tests for Organize By: "Performer or Studio (Let me choose)" ('both')
# =====================================================================

def test_both_mode_performer_only_match(test_env):
    """When both is selected and only performer matches, propose performer destination."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    vault1 = test_env["dest_root"]

    performer_dir = vault1 / "SoloPerformer"
    performer_dir.mkdir(parents=True, exist_ok=True)

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "SoloPerformer - New Video 2026.mp4"
    video.write_bytes(b"DATA" * 10000)

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: {
        "allPerformers": [{"id": "101", "name": "SoloPerformer", "alias_list": []}],
        "allStudios": [{"id": "201", "name": "UnrelatedStudio", "aliases": []}]
    }

    scene_data = {
        "id": "sc_both_1",
        "title": "SoloPerformer Scene",
        "performers": [{"id": "101", "name": "SoloPerformer"}],
        "studio": None,
        "files": [{"id": "f_both_1", "path": str(video)}]
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingDestinationRoots": [str(vault1)],
        "incomingFolders": [str(incoming)]
    }

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene_data, config)
    assert proposal is not None
    assert proposal["destination_folder"] == str(performer_dir.resolve())
    assert proposal["organize_by"] == "performer"
    assert proposal["matched_entity_name"] == "SoloPerformer"
    assert proposal["status"] == "pending"


def test_both_mode_studio_only_match(test_env):
    """When both is selected and only studio matches, propose studio destination."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    vault1 = test_env["dest_root"]

    studio_dir = vault1 / "SoloStudio"
    studio_dir.mkdir(parents=True, exist_ok=True)

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "SoloStudio - Scene Release.mp4"
    video.write_bytes(b"DATA" * 10000)

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: {
        "allPerformers": [{"id": "101", "name": "UnrelatedPerformer", "alias_list": []}],
        "allStudios": [{"id": "201", "name": "SoloStudio", "aliases": []}]
    }

    scene_data = {
        "id": "sc_both_2",
        "title": "SoloStudio Scene",
        "performers": [],
        "studio": {"id": "201", "name": "SoloStudio"},
        "files": [{"id": "f_both_2", "path": str(video)}]
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingDestinationRoots": [str(vault1)],
        "incomingFolders": [str(incoming)]
    }

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene_data, config)
    assert proposal is not None
    assert proposal["destination_folder"] == str(studio_dir.resolve())
    assert proposal["organize_by"] == "studio"
    assert proposal["matched_entity_name"] == "SoloStudio"
    assert proposal["status"] == "pending"


def test_both_mode_divergent_destinations_requires_explicit_selection(test_env):
    """When both performer and studio match different folders, present both and require selection."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    tmp = test_env["tmp"]
    vault1 = test_env["dest_root"]
    vault2 = tmp / "Vault2"
    vault2.mkdir(parents=True, exist_ok=True)

    performer_dir = vault1 / "Alice"
    performer_dir.mkdir(parents=True, exist_ok=True)

    studio_dir = vault2 / "Helix"
    studio_dir.mkdir(parents=True, exist_ok=True)

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Alice - Helix Feature 2026.mp4"
    video.write_bytes(b"DATA" * 10000)

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: {
        "allPerformers": [{"id": "11", "name": "Alice", "alias_list": []}],
        "allStudios": [{"id": "22", "name": "Helix", "aliases": []}],
        "findScene": {"id": "sc_both_3", "performers": [], "studio": None, "files": [{"id": "f_both_3", "path": str(video)}]},
        "sceneUpdate": {"id": "sc_both_3"}
    }
    mock_stash.move_files.return_value = True

    scene_data = {
        "id": "sc_both_3",
        "title": "Alice Feature",
        "performers": [{"id": "11", "name": "Alice"}],
        "studio": {"id": "22", "name": "Helix"},
        "files": [{"id": "f_both_3", "path": str(video)}]
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingDestinationRoots": [str(vault1), str(vault2)],
        "incomingFolders": [str(incoming)]
    }

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene_data, config)
    assert proposal is not None
    assert proposal["destination_folder"] == ""
    assert proposal["proposed_path"] == ""
    assert len(proposal["candidate_destinations"]) == 2
    assert proposal["status"] == "pending"

    candidates = proposal["candidate_destinations"]
    perf_cands = [c for c in candidates if c["entity_type"] == "performer"]
    stud_cands = [c for c in candidates if c["entity_type"] == "studio"]
    assert len(perf_cands) == 1
    assert len(stud_cands) == 1
    assert perf_cands[0]["destination_folder"] == str(performer_dir.resolve())
    assert stud_cands[0]["destination_folder"] == str(studio_dir.resolve())

    # Approving without target destination must block
    res_no_sel = apply_filing_proposal(db, mock_stash, proposal["id"], config=config)
    assert res_no_sel["status"] == "blocked"
    assert "destination selection is required" in res_no_sel["reason"]

    # Approving with studio target destination must file to studio directory
    res_studio = apply_filing_proposal(
        db, mock_stash, proposal["id"], config=config,
        target_destination_folder=str(studio_dir.resolve()),
        update_metadata=True
    )
    assert res_studio["status"] == "completed"
    assert Path(res_studio["proposed_path"]).parent == studio_dir.resolve()
    assert res_studio["metadata_updated"] is True


def test_both_mode_same_folder_merged_candidate(test_env):
    """When performer and studio match the same destination folder, merge candidate explanations."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    vault1 = test_env["dest_root"]

    shared_dir = vault1 / "Helix"
    shared_dir.mkdir(parents=True, exist_ok=True)

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Helix - Helix - 2026.mp4"
    video.write_bytes(b"DATA" * 10000)

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: {
        "allPerformers": [{"id": "51", "name": "Helix", "alias_list": []}],
        "allStudios": [{"id": "61", "name": "Helix", "aliases": []}],
        "findScene": {"id": "sc_both_4", "performers": [], "studio": None, "files": [{"id": "f_both_4", "path": str(video)}]},
        "sceneUpdate": {"id": "sc_both_4"}
    }
    mock_stash.move_files.return_value = True

    scene_data = {
        "id": "sc_both_4",
        "title": "Shared Scene",
        "performers": [{"id": "51", "name": "Helix"}],
        "studio": {"id": "61", "name": "Helix"},
        "files": [{"id": "f_both_4", "path": str(video)}]
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingDestinationRoots": [str(vault1)],
        "incomingFolders": [str(incoming)]
    }

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene_data, config)
    assert proposal is not None
    assert proposal["destination_folder"] == str(shared_dir.resolve())
    assert len(proposal["candidate_destinations"]) == 1
    assert proposal["status"] == "pending"

    cand = proposal["candidate_destinations"][0]
    assert cand["entity_type"] == "both"
    assert "Helix" in cand["entity_name"]
    assert len(cand.get("matched_entities", [])) == 2

    # Approving with specific entity metadata choice tags only that entity
    res = apply_filing_proposal(
        db, mock_stash, proposal["id"], config=config,
        update_metadata=True,
        target_entity_type="studio",
        target_entity_id="61"
    )
    assert res["status"] == "completed"
    assert res["metadata_updated"] is True


def test_both_mode_performer_ambiguity_uses_reliable_studio(test_env):
    """When performer is ambiguous but studio is reliable, use the studio match."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    vault1 = test_env["dest_root"]

    studio_dir = vault1 / "ReliableStudio"
    studio_dir.mkdir(parents=True, exist_ok=True)

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Alex - ReliableStudio Video.mp4"
    video.write_bytes(b"DATA" * 10000)

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: {
        "allPerformers": [
            {"id": "1", "name": "Alex Grey", "alias_list": ["Alex"]},
            {"id": "2", "name": "Alex Jones", "alias_list": ["Alex"]}
        ],
        "allStudios": [
            {"id": "99", "name": "ReliableStudio", "aliases": []}
        ]
    }

    scene_data = {
        "id": "sc_both_5",
        "title": "Ambiguous Performer Scene",
        "performers": [],
        "studio": None,
        "files": [{"id": "f_both_5", "path": str(video)}]
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingDestinationRoots": [str(vault1)],
        "incomingFolders": [str(incoming)],
        "autoFilingMatchSource": "filename_only"
    }

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene_data, config)
    assert proposal is not None
    assert proposal["organize_by"] == "studio"
    assert proposal["matched_entity_name"] == "ReliableStudio"
    assert proposal["destination_folder"] == str(studio_dir.resolve())
    assert proposal["status"] == "pending"


def test_both_mode_studio_ambiguity_uses_reliable_performer(test_env):
    """When studio is ambiguous but performer is reliable, use the performer match."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    vault1 = test_env["dest_root"]

    performer_dir = vault1 / "ReliablePerformer"
    performer_dir.mkdir(parents=True, exist_ok=True)

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "ReliablePerformer - StudioX.mp4"
    video.write_bytes(b"DATA" * 10000)

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: {
        "allPerformers": [
            {"id": "100", "name": "ReliablePerformer", "alias_list": []}
        ],
        "allStudios": [
            {"id": "901", "name": "StudioX Network", "aliases": ["StudioX"]},
            {"id": "902", "name": "StudioX US", "aliases": ["StudioX"]}
        ]
    }

    scene_data = {
        "id": "sc_both_6",
        "title": "Ambiguous Studio Scene",
        "performers": [],
        "studio": None,
        "files": [{"id": "f_both_6", "path": str(video)}]
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingDestinationRoots": [str(vault1)],
        "incomingFolders": [str(incoming)],
        "autoFilingMatchSource": "filename_only"
    }

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene_data, config)
    assert proposal is not None
    assert proposal["organize_by"] == "performer"
    assert proposal["matched_entity_name"] == "ReliablePerformer"
    assert proposal["destination_folder"] == str(performer_dir.resolve())
    assert proposal["status"] == "pending"


def test_both_mode_both_ambiguous_or_neither_returns_none(test_env):
    """When neither matches or both are ambiguous, no proposal is generated."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    vault1 = test_env["dest_root"]

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Random Unknown Video 123.mp4"
    video.write_bytes(b"DATA" * 10000)

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: {
        "allPerformers": [{"id": "1", "name": "Performer Alpha", "alias_list": []}],
        "allStudios": [{"id": "2", "name": "Studio Beta", "aliases": []}]
    }

    scene_data = {
        "id": "sc_both_7",
        "title": "Random Video",
        "performers": [],
        "studio": None,
        "files": [{"id": "f_both_7", "path": str(video)}]
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingDestinationRoots": [str(vault1)],
        "incomingFolders": [str(incoming)]
    }

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene_data, config)
    assert proposal is None


def test_both_mode_rejects_forged_destination_selection(test_env):
    """Approval blocks if client submits a destination not in candidate destinations."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    tmp = test_env["tmp"]
    vault1 = test_env["dest_root"]
    vault2 = tmp / "Vault2"
    vault2.mkdir(parents=True, exist_ok=True)

    performer_dir = vault1 / "Alice"
    performer_dir.mkdir(parents=True, exist_ok=True)
    studio_dir = vault2 / "Helix"
    studio_dir.mkdir(parents=True, exist_ok=True)

    unrelated_dir = vault1 / "Unrelated"
    unrelated_dir.mkdir(parents=True, exist_ok=True)

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Alice - Helix 2026.mp4"
    video.write_bytes(b"DATA" * 10000)

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: {
        "allPerformers": [{"id": "11", "name": "Alice", "alias_list": []}],
        "allStudios": [{"id": "22", "name": "Helix", "aliases": []}]
    }

    scene_data = {
        "id": "sc_both_8",
        "title": "Alice Scene",
        "performers": [{"id": "11", "name": "Alice"}],
        "studio": {"id": "22", "name": "Helix"},
        "files": [{"id": "f_both_8", "path": str(video)}]
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingDestinationRoots": [str(vault1), str(vault2)],
        "incomingFolders": [str(incoming)]
    }

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene_data, config)
    assert proposal is not None

    res = apply_filing_proposal(
        db, mock_stash, proposal["id"], config=config,
        target_destination_folder=str(unrelated_dir.resolve())
    )
    assert res["status"] == "blocked"
    assert "not an eligible candidate" in res["reason"]


def test_both_mode_backward_compatibility_with_legacy_candidates(test_env):
    """Legacy proposals with plain string arrays in candidate_destinations_json approve cleanly."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    vault1 = test_env["dest_root"]

    target_dir = vault1 / "LegacyTarget"
    target_dir.mkdir(parents=True, exist_ok=True)

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Legacy Video.mp4"
    video.write_bytes(b"DATA" * 10000)

    now = utc_now()
    conn = connect(db)
    cur = conn.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, match_source, reason,
            candidate_destinations_json, status, created_at, updated_at
        ) VALUES ('f_legacy_1', 'sc_legacy_1', ?, '', '', 'Legacy Video.mp4',
                  'performer', '10', 'LegacyPerformer', 'filename', 'Matched performer',
                  ?, 'pending', ?, ?)""",
        (str(video), json.dumps([str(target_dir.resolve())]), now, now)
    )
    prop_id = cur.lastrowid
    conn.execute(
        "INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f_legacy_1', 'sc_legacy_1', ?, 'Legacy Video.mp4', 1, ?, ?)",
        (str(video), now, now)
    )
    conn.commit()
    conn.close()

    mock_stash = MagicMock()
    mock_stash.move_files.return_value = True
    mock_stash.call_GQL.return_value = {"findScene": {"id": "sc_legacy_1", "files": [{"id": "f_legacy_1", "path": str(video)}]}}

    config = {
        "autoFilingEnabled": True,
        "autoFilingDestinationRoots": [str(vault1)],
        "incomingFolders": [str(incoming)]
    }

    res = apply_filing_proposal(
        db, mock_stash, prop_id, config=config,
        target_destination_folder=str(target_dir.resolve())
    )
    assert res["status"] == "completed"
    assert Path(res["proposed_path"]).parent == target_dir.resolve()


def test_shared_performer_and_studio_custom_folder_mappings(test_env):
    """Multiple performers and studios can explicitly map to the same existing destination folder.
    Each mapping remains independently identifiable and editable."""
    db = test_env["db"]
    vault1 = test_env["dest_root"]
    shared_folder = vault1 / "Shared_Vault_Folder"
    shared_folder.mkdir(parents=True, exist_ok=True)

    # 1. Map Performer 1 to shared folder
    ok1, msg1 = save_filing_folder_mapping(db, "performer", "101", "Alex Smith", str(shared_folder), [str(vault1)])
    assert ok1 is True

    # 2. Map Studio 1 to same shared folder (cross-type)
    ok2, msg2 = save_filing_folder_mapping(db, "studio", "201", "Helix Media", str(shared_folder), [str(vault1)])
    assert ok2 is True

    # 3. Map Performer 2 to same shared folder
    ok3, msg3 = save_filing_folder_mapping(db, "performer", "102", "Sam Jones", str(shared_folder), [str(vault1)])
    assert ok3 is True

    # 4. Map Studio 2 to same shared folder
    ok4, msg4 = save_filing_folder_mapping(db, "studio", "202", "FratBoy", str(shared_folder), [str(vault1)])
    assert ok4 is True

    # 5. Verify all 4 mappings are stored independently
    mappings = get_filing_folder_mappings(db)
    assert len(mappings) == 4
    for m in mappings:
        assert Path(m["folder_path"]).resolve() == shared_folder.resolve()

    # 6. Resolve destinations for each entity
    d1, _ = resolve_filing_destinations([str(vault1)], "Alex Smith", entity_id="101", entity_type="performer", database_path=db)
    d2, _ = resolve_filing_destinations([str(vault1)], "Helix Media", entity_id="201", entity_type="studio", database_path=db)
    d3, _ = resolve_filing_destinations([str(vault1)], "Sam Jones", entity_id="102", entity_type="performer", database_path=db)
    d4, _ = resolve_filing_destinations([str(vault1)], "FratBoy", entity_id="202", entity_type="studio", database_path=db)
    assert d1 == [shared_folder.resolve()]
    assert d2 == [shared_folder.resolve()]
    assert d3 == [shared_folder.resolve()]
    assert d4 == [shared_folder.resolve()]


def test_independent_deletion_of_shared_folder_mappings(test_env):
    """Deleting one mapping pointing to a shared folder leaves other mappings intact."""
    db = test_env["db"]
    vault1 = test_env["dest_root"]
    shared_folder = vault1 / "Shared_Group"
    shared_folder.mkdir(parents=True, exist_ok=True)

    save_filing_folder_mapping(db, "performer", "301", "Performer One", str(shared_folder), [str(vault1)])
    save_filing_folder_mapping(db, "studio", "401", "Studio One", str(shared_folder), [str(vault1)])

    mappings_before = get_filing_folder_mappings(db)
    assert len(mappings_before) == 2

    # Find Studio One mapping ID and delete it
    studio_map = next(m for m in mappings_before if m["entity_type"] == "studio" and m["entity_id"] == "401")
    del_ok = delete_filing_folder_mapping(db, studio_map["id"])
    assert del_ok is True

    # Performer One mapping must still be present and functional
    mappings_after = get_filing_folder_mappings(db)
    assert len(mappings_after) == 1
    assert mappings_after[0]["entity_type"] == "performer"
    assert mappings_after[0]["entity_id"] == "301"

    d_perf, status_perf = resolve_filing_destinations([str(vault1)], "Performer One", entity_id="301", entity_type="performer", database_path=db)
    assert status_perf == "custom_mapping"
    assert d_perf == [shared_folder.resolve()]

    # Studio One no longer has custom mapping
    d_stud, status_stud = resolve_filing_destinations([str(vault1)], "Studio One", entity_id="401", entity_type="studio", database_path=db)
    assert status_stud != "custom_mapping"


def test_cross_type_custom_mapped_same_folder_proposal_and_targeted_metadata(test_env):
    """When performer and studio are explicitly mapped to the same custom destination folder:
    - In Both mode, destination is deduplicated to 1 candidate with dual explanations.
    - Proposal proposes the folder normally without requiring an extra destination selection.
    - Metadata update allows targeted tagging for the chosen entity only."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    vault1 = test_env["dest_root"]
    custom_shared = vault1 / "CustomSharedTarget"
    custom_shared.mkdir(parents=True, exist_ok=True)

    # Explicit cross-type custom mappings to the same destination
    save_filing_folder_mapping(db, "performer", "501", "MappedPerformer", str(custom_shared), [str(vault1)])
    save_filing_folder_mapping(db, "studio", "601", "MappedStudio", str(custom_shared), [str(vault1)])

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "MappedPerformer - MappedStudio - 2026.mp4"
    video.write_bytes(b"VIDEO" * 5000)

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: {
        "allPerformers": [{"id": "501", "name": "MappedPerformer", "alias_list": []}],
        "allStudios": [{"id": "601", "name": "MappedStudio", "aliases": []}],
        "findScene": {"id": "sc_custom_shared", "performers": [], "studio": None, "files": [{"id": "f_custom_shared", "path": str(video)}]},
        "sceneUpdate": {"id": "sc_custom_shared"}
    }
    mock_stash.move_files.return_value = True

    scene_data = {
        "id": "sc_custom_shared",
        "title": "Custom Shared Feature",
        "performers": [{"id": "501", "name": "MappedPerformer"}],
        "studio": {"id": "601", "name": "MappedStudio"},
        "files": [{"id": "f_custom_shared", "path": str(video)}]
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingDestinationRoots": [str(vault1)],
        "incomingFolders": [str(incoming)]
    }

    proposal = evaluate_filing_proposal(db, mock_stash, str(video), scene_data, config)
    assert proposal is not None
    assert proposal["destination_folder"] == str(custom_shared.resolve())
    assert proposal["status"] == "pending"
    assert len(proposal["candidate_destinations"]) == 1

    cand = proposal["candidate_destinations"][0]
    assert cand["entity_type"] == "both"
    assert cand["is_custom_mapped"] is True
    assert "MappedPerformer" in cand["entity_name"] and "MappedStudio" in cand["entity_name"]
    assert len(cand.get("matched_entities", [])) == 2

    # Case A: Approve with metadata update choosing performer only
    res_perf = apply_filing_proposal(
        db, mock_stash, proposal["id"], config=config,
        update_metadata=True,
        target_entity_type="performer",
        target_entity_id="501"
    )
    assert res_perf["status"] == "completed"
    assert res_perf["metadata_updated"] is True
    assert res_perf["metadata_error"] is None


def test_destination_discovery_safety_excludes_orphaned_covers_and_maintenance_directories(tmp_path: Path):
    """Excludes Watchtower's internal, orphaned-cover, recovery, and maintenance directories from destination discovery."""
    root = tmp_path / "Vault"
    root.mkdir()

    # Legitimate media destination folders
    (root / "Helix Studios").mkdir()
    (root / "Performers" / "John Doe").mkdir(parents=True)

    # Excluded maintenance / internal directories and their descendants
    (root / "_orphaned_covers" / "Helix Studios").mkdir(parents=True)
    (root / "orphaned_covers" / "Other Studio").mkdir(parents=True)
    (root / "deleted_mismatched_covers" / "David").mkdir(parents=True)
    (root / ".stash" / "generated").mkdir(parents=True)
    (root / "_recovery" / "Helix Studios").mkdir(parents=True)
    (root / "_watchtower" / "temp").mkdir(parents=True)
    (root / "lost+found" / "recovered").mkdir(parents=True)
    (root / "$RECYCLE.BIN" / "files").mkdir(parents=True)

    from librarymanager_core import _scan_destination_subdirectories, _normalize_name_for_folder_match

    scanned = _scan_destination_subdirectories(root, max_depth=4)
    scanned_paths = [str(p.resolve()) for p, _ in scanned]
    scanned_names = [norm_name for _, norm_name in scanned]

    # Verify legitimate destinations are discovered
    assert str((root / "Helix Studios").resolve()) in scanned_paths
    assert str((root / "Performers" / "John Doe").resolve()) in scanned_paths
    assert _normalize_name_for_folder_match("Helix Studios") in scanned_names

    # Verify maintenance directories and all their descendants are excluded
    for p_str in scanned_paths:
        assert "_orphaned_covers" not in p_str
        assert "orphaned_covers" not in p_str
        assert "deleted_mismatched_covers" not in p_str
        assert ".stash" not in p_str
        assert "_recovery" not in p_str
        assert "_watchtower" not in p_str
        assert "lost+found" not in p_str
        assert "$recycle.bin" not in p_str.lower()


def test_filing_diagnostic_persistence_and_both_mode_explanations(test_env):
    """Persists structured diagnostics for unmatched, ambiguous, missing-folder, and ineligible files."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    root = test_env["dest_root"]

    snapshot_incoming_baseline(db, [str(incoming)])

    # 1. Setup incoming file in incoming_files table with import status
    v = incoming / "Helix David and John go fishing .mp4"
    v.write_bytes(b"video data 6392")
    conn = connect(db)
    now_str = utc_now()
    conn.execute(
        "INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
        ("f6392", "6392", str(v), v.name, now_str, now_str)
    )
    conn.execute(
        "INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, detail) VALUES (?, '2026-09-19T10:00:00', '2026-09-19T10:05:00', 'imported', 'Added as Stash scene 6392')",
        (str(v),)
    )
    conn.commit()
    conn.close()

    # Scene with no performers and no studio
    scene = {"id": "6392", "title": "David and John go fishing", "files": [{"id": "f6392", "path": str(v)}], "performers": [], "studio": None}

    # Stash performers setup: 'David' matches multiple performers (ambiguous), 'John' matches Gerasim Spartak
    all_p = [
        {"id": "10", "name": "David", "alias_list": []},
        {"id": "11", "name": "Casper Ivarsson", "alias_list": ["David"]},
        {"id": "12", "name": "Gerasim Spartak", "alias_list": ["John"]}
    ]
    # Stash studios: 'Helix Studios' with no alias
    all_s = [
        {"id": "39", "name": "Helix Studios", "aliases": []}
    ]

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: {
        "allPerformers": all_p,
        "allStudios": all_s
    }

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingMatchSource": "filename",
        "autoFilingTrigger": "import",
        "autoFilingDestinationRoots": [str(root)],
        "incomingFolders": [str(incoming)]
    }

    # Evaluate filing proposal -> should fail matching and record structured diagnostic
    prop = evaluate_filing_proposal(db, mock_stash, str(v), scene, config)
    assert prop is None

    conn = connect(db)
    row = conn.execute("SELECT status, detail, filing_diagnostic FROM incoming_files WHERE path=?", (str(v),)).fetchone()
    conn.close()

    assert row["status"] == "imported"
    assert row["detail"] == "Added as Stash scene 6392"  # Preserved!
    # A single-word alias embedded in prose is rejected; the canonical identity remains explicit.
    assert "Performer: Performer 'David' identified" in row["filing_diagnostic"]
    assert "Studio: No matching studio found" in row["filing_diagnostic"]

    # 2. Test when neither matches at all
    v2 = incoming / "Random Unrelated Clip.mp4"
    v2.write_bytes(b"clip data")
    conn = connect(db)
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, 1, ?, ?)", ("f2", "2", str(v2), v2.name, now_str, now_str))
    conn.execute("INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, detail) VALUES (?, '2026-09-19T10:00:00', '2026-09-19T10:05:00', 'imported', 'Added as Stash scene 2')", (str(v2),))
    conn.commit()
    conn.close()

    scene2 = {"id": "2", "title": "Random Unrelated Clip", "files": [{"id": "f2", "path": str(v2)}], "performers": [], "studio": None}
    evaluate_filing_proposal(db, mock_stash, str(v2), scene2, config)

    conn = connect(db)
    row2 = conn.execute("SELECT filing_diagnostic FROM incoming_files WHERE path=?", (str(v2),)).fetchone()
    conn.close()
    assert row2["filing_diagnostic"] == "No identity found: no matching performer, studio, or tag."


def test_retry_filing_proposal_workflow(test_env):
    """Retry filing re-evaluates updated Stash metadata and generates proposal without duplicates or rescanning."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    root = test_env["dest_root"]

    snapshot_incoming_baseline(db, [str(incoming)])

    # 1. Setup destination folder
    helix_dest = root / "Helix Studios"
    helix_dest.mkdir(parents=True)

    # 2. Setup incoming video scene 6392
    v = incoming / "Helix David and John go fishing .mp4"
    v.write_bytes(b"video data 6392")
    now_str = utc_now()
    conn = connect(db)
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, 1, ?, ?)", ("f6392", "6392", str(v), v.name, now_str, now_str))
    conn.execute("INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, detail) VALUES (?, '2026-09-19T10:00:00', '2026-09-19T10:05:00', 'imported', 'Added as Stash scene 6392')", (str(v),))
    conn.commit()
    conn.close()

    # Initial state in Stash: no studio assigned
    initial_scene = {"id": "6392", "title": "David and John go fishing", "files": [{"id": "f6392", "path": str(v)}], "performers": [], "studio": None}
    all_studios = [{"id": "39", "name": "Helix Studios", "aliases": []}]

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: (
        {"findScene": initial_scene} if "findScene(" in query
        else ({"allStudios": all_studios} if "allStudios" in query
        else {"allPerformers": []})
    )

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingMatchSource": "metadata_first",
        "autoFilingTrigger": "import",
        "autoFilingDestinationRoots": [str(root)],
        "incomingFolders": [str(incoming)]
    }

    # Initial retry before metadata is assigned -> fails with diagnostic
    from librarymanager_core import retry_filing_proposal
    res1 = retry_filing_proposal(db, mock_stash, str(v), config=config)
    assert res1["success"] is False
    assert "no matching performer, studio, or tag" in res1["message"].lower()

    # 3. User assigns Helix Studios (ID 39) in Stash
    updated_scene = {
        "id": "6392",
        "title": "David and John go fishing",
        "files": [{"id": "f6392", "path": str(v)}],
        "performers": [],
        "studio": {"id": "39", "name": "Helix Studios", "aliases": []}
    }
    mock_stash.call_GQL.side_effect = lambda query, vars=None: (
        {"findScene": updated_scene} if "findScene(" in query
        else ({"allStudios": all_studios} if "allStudios" in query
        else {"allPerformers": []})
    )

    # 4. User clicks Retry Filing
    res2 = retry_filing_proposal(db, mock_stash, str(v), config=config)
    assert res2["success"] is True
    assert res2["proposal"] is not None
    assert res2["proposal"]["destination_folder"] == str(helix_dest)
    assert res2["proposal"]["matched_entity_name"] == "Helix Studios"

    # Verify exactly 1 proposal exists in database (no duplicates)
    conn = connect(db)
    proposals = conn.execute("SELECT * FROM filing_proposals WHERE source_path=?", (str(v),)).fetchall()
    row = conn.execute("SELECT status, detail, filing_diagnostic FROM incoming_files WHERE path=?", (str(v),)).fetchone()
    conn.close()

    assert len(proposals) == 1
    assert row["status"] == "imported"
    assert row["detail"] == "Added as Stash scene 6392"
    assert "Proposal ready" in row["filing_diagnostic"]

    # 5. Clicking Retry Filing again while proposal is pending is safely rejected to prevent duplicates
    res3 = retry_filing_proposal(db, mock_stash, str(v), config=config)
    assert res3["success"] is False
    assert "active filing proposal already exists" in res3["error"]


def test_already_filed_and_non_incoming_scenes_excluded_from_retry(test_env):
    """Verifies already-filed scenes (e.g. scene 6386) and files outside Incoming are excluded from retry and active list."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    root = test_env["dest_root"]
    performer_dest = root / "Jamie Ray"
    performer_dest.mkdir(parents=True, exist_ok=True)

    snapshot_incoming_baseline(db, [str(incoming)])

    # Setup scene 6386 which has already been moved to performer_dest
    filed_video = performer_dest / "Jamie Ray.mp4"
    filed_video.write_bytes(b"DATA" * 5000)

    now_str = utc_now()
    conn = connect(db)
    conn.execute(
        "INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
        ("f6386", "6386", str(filed_video), filed_video.name, now_str, now_str)
    )
    # Stored in incoming_files under the filed path
    conn.execute(
        "INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, detail) VALUES (?, ?, ?, 'imported', 'Added as Stash scene 6386')",
        (str(filed_video), now_str, now_str)
    )
    # Stored in filing_proposals as completed
    conn.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, status, created_at, updated_at
        ) VALUES ('f6386', '6386', ?, ?, ?, 'Jamie Ray.mp4', 'performer', '227', 'Jamie Ray', NULL, 'filename', 'Matched', 'completed', ?, ?)""",
        (str(incoming / "Jamie Ray.mp4"), str(filed_video), str(performer_dest), now_str, now_str)
    )
    conn.commit()
    conn.close()

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingDestinationRoots": [str(root)],
        "incomingFolders": [str(incoming)]
    }

    # 1. incoming_summary must filter out already-filed scenes outside incoming folder
    summary = librarymanager_core.incoming_summary(db, config=config)
    active_paths = [it["path"] for it in summary.get("active", [])]
    assert str(filed_video) not in active_paths, "Filed video outside incoming folder must not appear in active incoming list"

    # 2. retry_filing_proposal must reject already-filed scenes
    mock_stash = MagicMock()
    mock_stash.find_plugin_config.return_value = config
    mock_stash.call_GQL.return_value = {"findScene": {"id": "6386", "files": [{"id": "f6386", "path": str(filed_video)}]}}

    res = librarymanager_core.retry_filing_proposal(db, mock_stash, str(filed_video), config=config)
    assert res.get("already_filed") is True or res["success"] is False
    assert ("already complete" in res.get("message", "") or "already been successfully filed" in res.get("error", "") or "not located inside any configured Incoming folder" in res.get("error", ""))


def test_retry_filing_reuses_destination_directory_cache_without_rescanning(test_env):
    """Verifies retry_filing_proposal reuses the cached directory tree without repeatedly traversing destination roots."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    root = test_env["dest_root"]
    helix_dest = root / "Helix Studios"
    helix_dest.mkdir(parents=True, exist_ok=True)

    snapshot_incoming_baseline(db, [str(incoming)])

    # Setup incoming scene
    v = incoming / "David and John go fishing .mp4"
    v.write_bytes(b"video data 6392")
    now_str = utc_now()
    conn = connect(db)
    conn.execute(
        "INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
        ("f6392", "6392", str(v), v.name, now_str, now_str)
    )
    conn.execute(
        "INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, detail) VALUES (?, ?, ?, 'imported', 'Added as Stash scene 6392')",
        (str(v), now_str, now_str)
    )
    conn.commit()
    conn.close()

    scene = {
        "id": "6392",
        "title": "David and John go fishing",
        "files": [{"id": "f6392", "path": str(v)}],
        "performers": [],
        "studio": {"id": "39", "name": "Unmatched Studio", "aliases": []}
    }
    all_studios = [{"id": "39", "name": "Unmatched Studio", "aliases": []}]

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: (
        {"findScene": scene} if "findScene(" in query
        else ({"allStudios": all_studios} if "allStudios" in query
        else {"allPerformers": []})
    )

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingMatchSource": "metadata_first",
        "autoFilingDestinationRoots": [str(root)],
        "incomingFolders": [str(incoming)]
    }

    # Invalidate once to ensure a clean baseline state
    librarymanager_core.invalidate_destination_dir_cache()

    # Track calls to _scan_destination_subdirectories
    original_scan = librarymanager_core._scan_destination_subdirectories
    scan_count = 0

    def counting_scan(root_path, max_depth=4):
        nonlocal scan_count
        scan_count += 1
        return original_scan(root_path, max_depth)

    with mock.patch.object(librarymanager_core, "_scan_destination_subdirectories", side_effect=counting_scan):
        # 1. First retry call: scans destination roots once and caches
        res1 = librarymanager_core.retry_filing_proposal(db, mock_stash, str(v), config=config)
        assert res1["success"] is False
        assert "no destination folder found" in (res1.get("diagnostic") or "").lower()
        assert scan_count == 1, "First evaluation must scan the destination root once to populate cache"

        # 2. Metadata refresh within TTL must reuse the folder cache and NOT rescan
        res2 = librarymanager_core.retry_filing_proposal(
            db, mock_stash, str(v), config=config, allow_refresh=True
        )
        assert res2["success"] is False
        assert scan_count == 1, "Metadata refresh within TTL must reuse the directory cache without rescanning"

        # 3. Explicit cache invalidation (e.g. Refresh Folders) forces a fresh scan on next call
        librarymanager_core.invalidate_destination_dir_cache()
        res3 = librarymanager_core.retry_filing_proposal(db, mock_stash, str(v), config=config)
        assert res3["success"] is False
        assert scan_count == 2, "Evaluation after invalidation must rescan the destination roots"



def test_backlog_items_enumeration_separates_videos_from_companions(test_env):
    """Verifies get_backlog_items counts eligible videos separately from JPGs and companion files, and identifies status."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    root = test_env["dest_root"]

    # 1. Eligible video
    v1 = incoming / "Eligible Video.mp4"
    v1.write_bytes(b"video 1")
    # 2. Already-filed video
    v2 = incoming / "Filed Video.mkv"
    v2.write_bytes(b"video 2")
    # 3. Recovery-state video
    v3 = incoming / "Recovery Video.avi"
    v3.write_bytes(b"video 3")
    # 4. Companion JPG
    c1 = incoming / "Eligible Video.jpg"
    c1.write_bytes(b"artwork 1")
    # 5. Companion NFO
    c2 = incoming / "Eligible Video.nfo"
    c2.write_bytes(b"nfo 1")

    now_str = utc_now()
    conn = connect(db)
    # Register files
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f1', '1', ?, 'Eligible Video.mp4', 1, ?, ?)", (str(v1), now_str, now_str))
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f2', '2', ?, 'Filed Video.mkv', 1, ?, ?)", (str(v2), now_str, now_str))
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f3', '3', ?, 'Recovery Video.avi', 1, ?, ?)", (str(v3), now_str, now_str))

    v2_dest = root / "Studio" / "Filed Video.mkv"
    v2_dest.parent.mkdir(parents=True, exist_ok=True)
    v2_dest.write_bytes(b"filed video 2")
    # Register completed proposal for v2
    conn.execute(
        """INSERT INTO filing_proposals (file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename, organize_by, matched_entity_id, matched_entity_name, match_source, reason, status, created_at, updated_at)
           VALUES ('f2', '2', ?, ?, ?, 'Filed Video.mkv', 'studio', '1', 'Studio', 'metadata', 'ok', 'completed', ?, ?)""",
        (str(v2), str(v2_dest), str(v2_dest.parent), now_str, now_str)
    )
    # Register needs_recovery proposal for v3
    conn.execute(
        """INSERT INTO filing_proposals (file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename, organize_by, matched_entity_id, matched_entity_name, match_source, reason, status, created_at, updated_at)
           VALUES ('f3', '3', ?, '/Volumes/Main/Vault1/Studio/Recovery Video.avi', '/Volumes/Main/Vault1/Studio', 'Recovery Video.avi', 'studio', '1', 'Studio', 'metadata', 'ok', 'needs_recovery', ?, ?)""",
        (str(v3), now_str, now_str)
    )
    conn.commit()
    conn.close()

    # Capture incoming baseline
    snapshot_incoming_baseline(db, [str(incoming)])

    config = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoots": [str(root)]
    }

    res = librarymanager_core.get_backlog_items(db, None, config=config)
    assert res["total_count"] == 5
    assert res["video_count"] == 3
    assert res["companion_count"] == 2
    assert res["eligible_count"] == 1

    # Check individual items
    items_by_name = {i["basename"]: i for i in res["items"]}
    assert items_by_name["Eligible Video.mp4"]["eligible"] is True
    assert items_by_name["Eligible Video.mp4"]["is_video"] is True

    assert items_by_name["Filed Video.mkv"]["eligible"] is False
    assert items_by_name["Filed Video.mkv"]["status"] == "moved"
    assert items_by_name["Filed Video.mkv"]["status_label"] == "Moved out of Incoming"

    assert items_by_name["Recovery Video.avi"]["eligible"] is False
    assert items_by_name["Recovery Video.avi"]["status"] == "needs_recovery"

    assert items_by_name["Eligible Video.jpg"]["is_companion"] is True
    assert items_by_name["Eligible Video.jpg"]["eligible"] is False

    assert items_by_name["Eligible Video.nfo"]["is_companion"] is True
    assert items_by_name["Eligible Video.nfo"]["eligible"] is False


def test_backlog_evaluation_allows_explicit_baseline_selection_without_resetting_baseline(test_env):
    """Verifies selected baseline files can be evaluated with allow_baseline=True while keeping baseline protection intact for everything else."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    root = test_env["dest_root"]
    dest_performer = root / "Michael Vegas"
    dest_performer.mkdir(parents=True, exist_ok=True)

    v1 = incoming / "Michael Vegas Solo.mp4"
    v1.write_bytes(b"video data v1")
    v2 = incoming / "Protected Other Video.mp4"
    v2.write_bytes(b"video data v2")

    now_str = utc_now()
    conn = connect(db)
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f10', '10', ?, 'Michael Vegas Solo.mp4', 1, ?, ?)", (str(v1), now_str, now_str))
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f20', '20', ?, 'Protected Other Video.mp4', 1, ?, ?)", (str(v2), now_str, now_str))
    conn.commit()
    conn.close()

    # Capture baseline containing both files
    snapshotted = snapshot_incoming_baseline(db, [str(incoming)])
    assert snapshotted == 2

    scene10 = {
        "id": "10",
        "title": "Michael Vegas Solo",
        "files": [{"id": "f10", "path": str(v1)}],
        "performers": [{"id": "101", "name": "Michael Vegas", "alias_list": []}],
        "studio": None
    }
    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: (
        {"findScene": scene10} if "findScene(" in query
        else {"allPerformers": [{"id": "101", "name": "Michael Vegas", "alias_list": []}]}
    )

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingMatchSource": "metadata_first",
        "autoFilingDestinationRoots": [str(root)],
        "incomingFolders": [str(incoming)]
    }

    # 1. Normal automatic filing evaluation (allow_baseline=False) is rejected by baseline protection
    eval_normal = librarymanager_core.evaluate_filing_proposal(db, mock_stash, str(v1), scene10, config, allow_baseline=False)
    assert eval_normal is None

    # 2. Backlog batch evaluation for explicitly selected v1 succeeds
    batch_res = librarymanager_core.evaluate_backlog_batch(db, mock_stash, [str(v1)], config=config)
    assert batch_res["tally"]["proposal_ready"] == 1
    assert len(batch_res["results"]) == 1
    assert batch_res["results"][0]["success"] is True
    assert batch_res["results"][0]["outcome"] == "proposal_ready"

    # 3. Verify baseline table remains completely intact and undisturbed
    conn = connect(db)
    baseline_count = conn.execute("SELECT count(*) as c FROM filing_incoming_baseline").fetchone()["c"]
    assert baseline_count == 2, "Baseline table must remain completely intact"
    v2_in_baseline = conn.execute("SELECT 1 FROM filing_incoming_baseline WHERE path=?", (str(v2),)).fetchone()
    assert v2_in_baseline is not None, "Unselected baseline file v2 must remain protected in baseline snapshot"
    conn.close()


def test_backlog_duplicate_prevention_and_unmatched_diagnostics(test_env):
    """Verifies duplicate proposals are prevented and unmatched backlog items record clear diagnostics and tallies."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    root = test_env["dest_root"]
    dest_performer = root / "Known Performer"
    dest_performer.mkdir(parents=True, exist_ok=True)

    v1 = incoming / "Known Performer Video.mp4"
    v1.write_bytes(b"v1")
    v2 = incoming / "Unknown Performer Video.mp4"
    v2.write_bytes(b"v2")
    v3 = incoming / "No Folder Performer Video.mp4"
    v3.write_bytes(b"v3")

    now_str = utc_now()
    conn = connect(db)
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f1', '1', ?, 'Known Performer Video.mp4', 1, ?, ?)", (str(v1), now_str, now_str))
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f2', '2', ?, 'Unknown Performer Video.mp4', 1, ?, ?)", (str(v2), now_str, now_str))
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f3', '3', ?, 'No Folder Performer Video.mp4', 1, ?, ?)", (str(v3), now_str, now_str))
    conn.commit()
    conn.close()

    snapshot_incoming_baseline(db, [str(incoming)])

    scenes = {
        "1": {"id": "1", "title": "Known Performer Video", "files": [{"id": "f1", "path": str(v1)}], "performers": [{"id": "p1", "name": "Known Performer", "alias_list": []}], "studio": None},
        "2": {"id": "2", "title": "Unknown Video", "files": [{"id": "f2", "path": str(v2)}], "performers": [], "studio": None},
        "3": {"id": "3", "title": "No Folder Video", "files": [{"id": "f3", "path": str(v3)}], "performers": [{"id": "p3", "name": "Missing Folder Performer", "alias_list": []}], "studio": None},
    }

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: (
        {"findScene": scenes.get(str(vars.get("id")))} if "findScene(" in query
        else {"allPerformers": [{"id": "p1", "name": "Known Performer", "alias_list": []}, {"id": "p3", "name": "Missing Folder Performer", "alias_list": []}]}
    )

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingMatchSource": "metadata_first",
        "autoFilingDestinationRoots": [str(root)],
        "incomingFolders": [str(incoming)]
    }

    # Evaluate batch of 3 files
    batch_res = librarymanager_core.evaluate_backlog_batch(db, mock_stash, [str(v1), str(v2), str(v3)], config=config)
    assert batch_res["tally"]["proposal_ready"] == 1
    assert batch_res["tally"]["no_identity_found"] == 1
    assert batch_res["tally"]["destination_not_found"] == 1

    # Re-evaluating v1 must reject as duplicate (active proposal already exists)
    retry_v1 = librarymanager_core.retry_filing_proposal(db, mock_stash, str(v1), config=config, allow_baseline=True)
    assert retry_v1["success"] is False
    assert "already exists" in retry_v1["error"].lower()



def test_librarymanager_plugin_operation_backlog_handlers(test_env):
    """Verifies librarymanager module correctly defines, imports and executes get_backlog_items and evaluate_backlog_batch."""
    import librarymanager
    assert hasattr(librarymanager, "get_backlog_items"), "librarymanager.py must import and expose get_backlog_items"
    assert hasattr(librarymanager, "evaluate_backlog_batch"), "librarymanager.py must import and expose evaluate_backlog_batch"

    db = test_env["db"]
    incoming = test_env["incoming"]
    root = test_env["dest_root"]
    dest_performer = root / "Plugin Op Performer"
    dest_performer.mkdir(parents=True, exist_ok=True)

    v1 = incoming / "Plugin Op Performer Scene.mp4"
    v1.write_bytes(b"video 1")
    c1 = incoming / "Plugin Op Performer Scene.jpg"
    c1.write_bytes(b"jpg 1")

    now_str = utc_now()
    conn = connect(db)
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f100', '100', ?, 'Plugin Op Performer Scene.mp4', 1, ?, ?)", (str(v1), now_str, now_str))
    conn.commit()
    conn.close()

    snapshot_incoming_baseline(db, [str(incoming)])

    mock_stash = MagicMock()
    mock_config = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoots": [str(root)],
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingMatchSource": "metadata_first"
    }
    mock_stash.find_plugin_config.return_value = mock_config

    scene100 = {"id": "100", "title": "Plugin Op Performer Scene", "files": [{"id": "f100", "path": str(v1)}], "performers": [{"id": "p100", "name": "Plugin Op Performer", "alias_list": []}], "studio": None}
    mock_stash.call_GQL.side_effect = lambda q, v=None: {"findScene": scene100} if "findScene(" in q else {"allPerformers": [{"id": "p100", "name": "Plugin Op Performer", "alias_list": []}]}

    # 1. Test get_backlog_items via librarymanager module
    res_items = librarymanager.get_backlog_items(db, mock_stash, config=mock_config)
    assert res_items["total_count"] == 2
    assert res_items["video_count"] == 1
    assert res_items["companion_count"] == 1
    assert res_items["eligible_count"] == 1

    # 2. Test evaluate_backlog_batch via librarymanager module
    batch_res = librarymanager.evaluate_backlog_batch(db, mock_stash, [str(v1)], config=mock_config)
    assert batch_res["tally"]["proposal_ready"] == 1
    assert len(batch_res["results"]) == 1
    assert batch_res["results"][0]["success"] is True


# ---------------------------------------------------------------------------
# Point 10: Multi-Performer Evaluation & Intelligent Folder Matching
# ---------------------------------------------------------------------------

def test_intelligent_folder_matching_recognizes_conservative_patterns(test_env):
    """Watchtower recognizes conservative folder patterns (The [Name], [Name] Collection,
    The [Name] Collection) without needing custom mappings."""
    dest_root = test_env["dest_root"]
    
    # Setup test folders
    (dest_root / "The Cole Bentley Collection").mkdir(parents=True, exist_ok=True)
    (dest_root / "Austin Wilde Collection").mkdir(parents=True, exist_ok=True)
    (dest_root / "The Phoenix Cross").mkdir(parents=True, exist_ok=True)
    (dest_root / "Standard Performer").mkdir(parents=True, exist_ok=True)
    librarymanager_core.invalidate_destination_dir_cache()

    # 1. 'The Cole Bentley Collection' -> matches 'Cole Bentley'
    paths, status = resolve_filing_destinations([str(dest_root)], "Cole Bentley")
    assert len(paths) == 1
    assert paths[0].name == "The Cole Bentley Collection"
    assert status == "ok"

    # 2. 'Austin Wilde Collection' -> matches 'Austin Wilde'
    paths, status = resolve_filing_destinations([str(dest_root)], "Austin Wilde")
    assert len(paths) == 1
    assert paths[0].name == "Austin Wilde Collection"
    assert status == "ok"

    # 3. 'The Phoenix Cross' -> matches 'Phoenix Cross'
    paths, status = resolve_filing_destinations([str(dest_root)], "Phoenix Cross")
    assert len(paths) == 1
    assert paths[0].name == "The Phoenix Cross"
    assert status == "ok"

    # 4. 'Standard Performer' -> matches 'Standard Performer'
    paths, status = resolve_filing_destinations([str(dest_root)], "Standard Performer")
    assert len(paths) == 1
    assert paths[0].name == "Standard Performer"
    assert status == "ok"


def test_intelligent_folder_matching_rejects_misleading_partial_and_substring_matches(test_env):
    """Conservative matching strictly forbids arbitrary substring or fuzzy matching."""
    dest_root = test_env["dest_root"]
    (dest_root / "The Cole Bentley Collection").mkdir(parents=True, exist_ok=True)
    (dest_root / "The Collection").mkdir(parents=True, exist_ok=True)
    (dest_root / "The Collective").mkdir(parents=True, exist_ok=True)
    librarymanager_core.invalidate_destination_dir_cache()

    # Partial name 'Cole' must NOT match 'The Cole Bentley Collection'
    paths, _ = resolve_filing_destinations([str(dest_root)], "Cole")
    assert len(paths) == 0

    # Partial name 'Bentley' must NOT match 'The Cole Bentley Collection'
    paths, _ = resolve_filing_destinations([str(dest_root)], "Bentley")
    assert len(paths) == 0

    # Entity 'Cole Bentley' must NOT match folder 'The Collection' or 'The Collective'
    (dest_root / "The Cole Bentley Collection").rmdir()
    librarymanager_core.invalidate_destination_dir_cache()
    paths, _ = resolve_filing_destinations([str(dest_root)], "Cole Bentley")
    assert len(paths) == 0


def test_scene_6393_cole_bentley_and_billy_essex_simulation(test_env):
    """Scene 6393 reproduction: Onlyfans Cole Bentley Billy Essex.mp4
    - Cole Bentley and Billy Essex both recognized in filename.
    - Only Cole Bentley has a destination folder ('The Cole Bentley Collection').
    - Billy Essex has no folder, OnlyFans studio has no folder.
    - Watchtower automatically proposes 'The Cole Bentley Collection' without custom mapping."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Onlyfans Cole Bentley Billy Essex.mp4"
    video.write_bytes(b"SCENE_6393_DATA_" * 1000)

    (dest_root / "The Cole Bentley Collection").mkdir(parents=True, exist_ok=True)
    librarymanager_core.invalidate_destination_dir_cache()

    conn = connect(db)
    now_str = utc_now()
    conn.execute(
        "INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
        ("f6393", "6393", str(video), video.name, now_str, now_str)
    )
    conn.execute(
        "INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, detail) VALUES (?, ?, ?, 'imported', 'Added as Stash scene 6393')",
        (str(video), now_str, now_str)
    )
    conn.commit()
    conn.close()

    scene = {
        "id": "6393",
        "title": "Onlyfans Cole Bentley Billy Essex",
        "files": [{"id": "f6393", "path": str(video)}],
        "performers": [],
        "studio": None
    }

    all_performers = [
        {"id": "2", "name": "Cole Bentley", "disambiguation": "", "alias_list": []},
        {"id": "1636", "name": "Billy Essex", "disambiguation": "", "alias_list": []},
    ]
    all_studios = [
        {"id": "50", "name": "OnlyFans", "aliases": []}
    ]

    mock_stash = MagicMock()
    mock_stash.call_GQL.side_effect = lambda query, vars=None: {
        "allPerformers": all_performers,
        "allStudios": all_studios
    }

    # 1. Test in 'performer' mode
    config_perf = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingMatchSource": "filename",
        "autoFilingDestinationRoots": [str(dest_root)],
        "incomingFolders": [str(incoming)]
    }

    prop_perf = evaluate_filing_proposal(db, mock_stash, str(video), scene, config_perf)
    assert prop_perf is not None
    assert prop_perf["destination_folder"] == str((dest_root / "The Cole Bentley Collection").resolve())
    assert prop_perf["matched_entity_name"] == "Cole Bentley"
    assert prop_perf["matched_entity_id"] == "2"
    assert prop_perf["is_custom_mapped"] is False

    # Clean up proposal for next test
    conn = connect(db)
    conn.execute("DELETE FROM filing_proposals WHERE scene_id='6393'")
    conn.commit()
    conn.close()

    # 2. Test in 'both' mode
    config_both = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "both",
        "autoFilingMatchSource": "filename",
        "autoFilingDestinationRoots": [str(dest_root)],
        "incomingFolders": [str(incoming)]
    }

    prop_both = evaluate_filing_proposal(db, mock_stash, str(video), scene, config_both)
    assert prop_both is not None
    assert prop_both["destination_folder"] == str((dest_root / "The Cole Bentley Collection").resolve())
    assert prop_both["matched_entity_name"] == "Cole Bentley"


def test_multi_performer_multiple_destinations_creates_candidate_selection(test_env):
    """When multiple performers have distinct destination folders on disk,
    Watchtower creates a proposal with candidate_destinations offering a choice."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Cole Bentley Billy Essex CoStar.mp4"
    video.write_bytes(b"COSTAR_VIDEO_DATA" * 1000)

    (dest_root / "The Cole Bentley Collection").mkdir(parents=True, exist_ok=True)
    (dest_root / "Billy Essex").mkdir(parents=True, exist_ok=True)
    librarymanager_core.invalidate_destination_dir_cache()

    scene = {"id": "7001", "files": [{"id": "f7001", "path": str(video)}]}
    all_performers = [
        {"id": "2", "name": "Cole Bentley", "alias_list": []},
        {"id": "1636", "name": "Billy Essex", "alias_list": []},
    ]

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {"allPerformers": all_performers}

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingMatchSource": "filename",
        "autoFilingDestinationRoots": [str(dest_root)],
        "incomingFolders": [str(incoming)]
    }

    conn = connect(db)
    now_str = utc_now()
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f7001', '7001', ?, ?, 1, ?, ?)", (str(video), video.name, now_str, now_str))
    conn.commit()
    conn.close()

    prop = evaluate_filing_proposal(db, mock_stash, str(video), scene, config)
    assert prop is not None
    assert prop["destination_folder"] == ""
    assert len(prop["candidate_destinations"]) == 2
    cand_folders = {c["destination_folder"] for c in prop["candidate_destinations"]}
    assert str((dest_root / "The Cole Bentley Collection").resolve()) in cand_folders
    assert str((dest_root / "Billy Essex").resolve()) in cand_folders


def test_multi_performer_shared_folder_deduplicates_and_retains_identities(test_env):
    """When multiple performers resolve to the same destination folder,
    the proposal deduplicates the folder and records all matched identities."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Cole Bentley Billy Essex Collab.mp4"
    video.write_bytes(b"COLLAB_VIDEO_DATA" * 1000)

    shared_folder = dest_root / "Shared Duos"
    shared_folder.mkdir(parents=True, exist_ok=True)
    librarymanager_core.invalidate_destination_dir_cache()

    # Custom mapping for both performers to the same shared folder
    save_filing_folder_mapping(db, "performer", "2", "Cole Bentley", str(shared_folder), [str(dest_root)])
    save_filing_folder_mapping(db, "performer", "1636", "Billy Essex", str(shared_folder), [str(dest_root)])

    scene = {"id": "7002", "files": [{"id": "f7002", "path": str(video)}]}
    all_performers = [
        {"id": "2", "name": "Cole Bentley", "alias_list": []},
        {"id": "1636", "name": "Billy Essex", "alias_list": []},
    ]

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {"allPerformers": all_performers}

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingMatchSource": "filename",
        "autoFilingDestinationRoots": [str(dest_root)],
        "incomingFolders": [str(incoming)]
    }

    conn = connect(db)
    now_str = utc_now()
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f7002', '7002', ?, ?, 1, ?, ?)", (str(video), video.name, now_str, now_str))
    conn.commit()
    conn.close()

    prop = evaluate_filing_proposal(db, mock_stash, str(video), scene, config)
    assert prop is not None
    assert prop["destination_folder"] == str(shared_folder.resolve())
    assert "Cole Bentley" in prop["matched_entity_name"]
    assert "Billy Essex" in prop["matched_entity_name"]
    assert prop["is_custom_mapped"] is True


def test_multi_performer_no_destinations_explains_in_diagnostic(test_env):
    """When multiple performers are identified but none have folders,
    Watchtower explains the reason clearly in incoming diagnostics."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]

    # Remove all folders
    for d in dest_root.iterdir():
        if d.is_dir():
            d.rmdir()
    librarymanager_core.invalidate_destination_dir_cache()

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "Cole Bentley Billy Essex NoFolder.mp4"
    video.write_bytes(b"NOFOLDER_DATA" * 1000)

    scene = {"id": "7003", "files": [{"id": "f7003", "path": str(video)}]}
    all_performers = [
        {"id": "2", "name": "Cole Bentley", "alias_list": []},
        {"id": "1636", "name": "Billy Essex", "alias_list": []},
    ]

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {"allPerformers": all_performers}

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingMatchSource": "filename",
        "autoFilingDestinationRoots": [str(dest_root)],
        "incomingFolders": [str(incoming)]
    }

    conn = connect(db)
    now_str = utc_now()
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f7003', '7003', ?, ?, 1, ?, ?)", (str(video), video.name, now_str, now_str))
    conn.execute("INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, detail) VALUES (?, ?, ?, 'imported', 'Scene 7003')", (str(video), now_str, now_str))
    conn.commit()
    conn.close()

    prop = evaluate_filing_proposal(db, mock_stash, str(video), scene, config)
    assert prop is None

    conn = connect(db)
    row = conn.execute("SELECT filing_diagnostic FROM incoming_files WHERE path=?", (str(video),)).fetchone()
    conn.close()

    assert "Multiple performers identified (Cole Bentley, Billy Essex), but no destination folders found." in row["filing_diagnostic"]


# ---------------------------------------------------------------------------
# Point 11: Destination Directory Persistent Cache & Proposal Candidates Refresh
# ---------------------------------------------------------------------------

def test_persistent_destination_cache_survives_process_memory_clear(test_env):
    """Destination directory cache persisted in SQLite survives clearing process memory,
    preventing repeated NAS filesystem scans."""
    db = test_env["db"]
    dest_root = test_env["dest_root"]
    
    (dest_root / "The Jamie Ray Collection").mkdir(parents=True, exist_ok=True)
    
    # 1. First resolution: populates SQLite cache
    paths1, status1 = resolve_filing_destinations([str(dest_root)], "Jamie Ray", database_path=db)
    assert len(paths1) == 1
    assert paths1[0].name == "The Jamie Ray Collection"
    
    # Check that SQLite cache has records
    conn = connect(db)
    count = conn.execute("SELECT count(*) FROM filing_destination_dir_cache").fetchone()[0]
    meta = conn.execute("SELECT * FROM filing_destination_cache_meta WHERE root_path=?", (str(dest_root),)).fetchone()
    conn.close()
    assert count > 0
    assert meta is not None
    assert meta["entry_count"] == count

    # 2. Clear process in-memory cache completely (simulating fresh Python subprocess)
    librarymanager_core._DESTINATION_DIR_CACHE.clear()

    # 3. Second resolution: reuses SQLite cache without walking filesystem
    with unittest.mock.patch("librarymanager_core._scan_destination_subdirectories") as mock_scan:
        paths2, status2 = resolve_filing_destinations([str(dest_root)], "Jamie Ray", database_path=db)
        assert len(paths2) == 1
        assert paths2[0].name == "The Jamie Ray Collection"
        mock_scan.assert_not_called()  # Disk I/O was completely avoided!


def test_refresh_destination_dir_cache_explicitly_updates_snapshot(test_env):
    """Explicit 'Refresh Folders' action updates the SQLite cache when folders change on disk."""
    db = test_env["db"]
    dest_root = test_env["dest_root"]

    # Populate initial cache
    refresh_destination_dir_cache(db, [str(dest_root)], max_depth=4)
    
    # Add new folder on disk after cache was created
    (dest_root / "New Performer Folder").mkdir(parents=True, exist_ok=True)

    # Without refresh, cache does not see the new folder yet (cached snapshot)
    librarymanager_core._DESTINATION_DIR_CACHE.clear()
    paths, _ = resolve_filing_destinations([str(dest_root)], "New Performer Folder", database_path=db)
    assert len(paths) == 0

    # Explicit refresh action
    res = refresh_destination_dir_cache(db, [str(dest_root)], max_depth=4)
    assert res["success"] is True
    assert res["total_folders"] > 0

    # Now finds the newly added folder immediately
    paths_after, _ = resolve_filing_destinations([str(dest_root)], "New Performer Folder", database_path=db)
    assert len(paths_after) == 1
    assert paths_after[0].name == "New Performer Folder"


def test_retry_filing_proposal_refreshes_pending_proposal_candidates(test_env):
    """Retrying or refreshing a pending proposal safely updates candidate choices in-place
    without creating duplicate proposals or moving files."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]

    snapshot_incoming_baseline(db, [str(incoming)])

    video = incoming / "8Teenboy-Jamie Ray & Milo Harper.mp4"
    video.write_bytes(b"JAMIE_RAY_MILO_HARPER_DATA" * 1000)

    conn = connect(db)
    now_str = utc_now()
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f888', '888', ?, ?, 1, ?, ?)", (str(video), video.name, now_str, now_str))
    conn.execute("INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, detail) VALUES (?, ?, ?, 'imported', 'Scene 888')", (str(video), now_str, now_str))
    conn.commit()
    conn.close()

    scene = {
        "id": "888",
        "files": [{"id": "f888", "path": str(video)}],
        "performers": [{"id": "227", "name": "Jamie Ray", "alias_list": []}],
        "studio": None
    }

    # Only 1 folder initially exists
    (dest_root / "Jamie Ray").mkdir(parents=True, exist_ok=True)
    refresh_destination_dir_cache(db, [str(dest_root)], max_depth=4)

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {"findScene": scene, "allPerformers": scene["performers"]}

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingMatchSource": "metadata_first",
        "autoFilingDestinationRoots": [str(dest_root)],
        "incomingFolders": [str(incoming)]
    }

    # Initial proposal created with single destination
    prop1 = evaluate_filing_proposal(db, mock_stash, str(video), scene, config)
    assert prop1 is not None
    assert prop1["destination_folder"] == str((dest_root / "Jamie Ray").resolve())
    initial_prop_id = prop1["id"]

    # Later, a second collection folder is created on disk
    (dest_root / "The Jamie Ray Collection").mkdir(parents=True, exist_ok=True)
    refresh_destination_dir_cache(db, [str(dest_root)], max_depth=4)

    # Retry/refresh the pending proposal
    retry_res = retry_filing_proposal(db, mock_stash, str(video), config=config, allow_refresh=True)
    assert retry_res["success"] is True
    prop2 = retry_res["proposal"]
    assert prop2["id"] == initial_prop_id  # Proposal updated in-place!
    assert len(prop2["candidate_destinations"]) == 2  # Both folders now available as choices!

    # Verify no duplicate proposals in database
    conn = connect(db)
    rows = conn.execute("SELECT * FROM filing_proposals WHERE source_path=?", (str(video),)).fetchall()
    conn.close()
    assert len(rows) == 1
    assert len(json.loads(rows[0]["candidate_destinations_json"])) == 2




def test_apply_filing_proposal_allows_baseline_backlog_proposals(test_env):
    """Verify apply_filing_proposal does not block when source file is in baseline snapshot."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    (dest_root / "Jamie Ray").mkdir(parents=True, exist_ok=True)

    video = incoming / "Jamie Ray - Scene 1.mp4"
    video.write_bytes(b"fake video content")

    # Take baseline snapshot including video
    snapshot_incoming_baseline(db, [str(incoming)])

    conn = connect(db)
    conn.execute("INSERT INTO files(file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at) VALUES ('100', '200', ?, ?, ?, 1, ?, ?)",
                 (str(video), video.name, len(b"fake video content"), "2026-09-19T00:00:00Z", "2026-09-19T00:00:00Z"))
    conn.commit()
    conn.close()

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findScene": {
            "id": "200",
            "files": [{"id": "100", "path": str(video)}]
        }
    }
    mock_stash.find_scene.return_value = {
        "id": "200",
        "files": [{"id": "100", "path": str(video)}]
    }
    mock_stash.find_performers.return_value = [{"id": "1", "name": "Jamie Ray"}]
    mock_stash.find_studios.return_value = []

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingDestinationRoots": [str(dest_root)],
        "incomingFolders": [str(incoming)]
    }

    # Evaluate proposal with allow_baseline=True (as Backlog Organiser does)
    scene = {"id": "200", "title": "Scene 1", "files": [{"id": "100", "path": str(video)}], "performers": [{"id": "1", "name": "Jamie Ray"}]}
    prop = evaluate_filing_proposal(db, mock_stash, str(video), scene, config, allow_baseline=True)
    assert prop is not None
    assert prop["status"] == "pending"

    # Simulate filesystem move done by stash.move_files
    def fake_move(args):
        target = dest_root / "Jamie Ray" / video.name
        video.rename(target)
        return True
    mock_stash.move_files.side_effect = fake_move

    # Approve proposal
    res = apply_filing_proposal(db, mock_stash, prop["id"], config=config)
    assert res["status"] == "completed"
    assert (dest_root / "Jamie Ray" / video.name).exists()
    assert not video.exists()


def test_retry_filing_proposal_restores_blocked_proposal_in_place(test_env):
    """Verify retry_filing_proposal with allow_refresh=True safely restores a blocked proposal."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    (dest_root / "Jamie Ray").mkdir(parents=True, exist_ok=True)

    video = incoming / "Jamie Ray - Scene 2.mp4"
    video.write_bytes(b"fake content 2")

    snapshot_incoming_baseline(db, [str(incoming)])

    conn = connect(db)
    conn.execute("INSERT INTO files(file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at) VALUES ('101', '201', ?, ?, ?, 1, ?, ?)",
                 (str(video), video.name, len(b"fake content 2"), "2026-09-19T00:00:00Z", "2026-09-19T00:00:00Z"))
    # Proposal marked blocked previously
    conn.execute("""INSERT INTO filing_proposals(
        id, file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
        organize_by, matched_entity_id, matched_entity_name, match_source, reason,
        status, last_error, created_at, updated_at
    ) VALUES (42, '101', '201', ?, '', '', ?, 'performer', '1', 'Jamie Ray', 'filename', 'reason', 'blocked', 'previous error', '2026-09-19', '2026-09-19')""",
    (str(video), video.name))
    conn.commit()
    conn.close()

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {
        "findScene": {
            "id": "201",
            "title": "Scene 2",
            "files": [{"id": "101", "path": str(video)}],
            "performers": [{"id": "1", "name": "Jamie Ray"}]
        }
    }
    mock_stash.find_scene.return_value = {
        "id": "201",
        "files": [{"id": "101", "path": str(video)}]
    }
    mock_stash.find_performers.return_value = [{"id": "1", "name": "Jamie Ray"}]
    mock_stash.find_studios.return_value = []

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingDestinationRoots": [str(dest_root)],
        "incomingFolders": [str(incoming)]
    }

    res = retry_filing_proposal(db, mock_stash, str(video), config=config, allow_refresh=True, proposal_id=42)
    assert res["success"] is True
    assert res["proposal"]["id"] == 42
    assert res["proposal"]["status"] == "pending"
    assert res["proposal"]["destination_folder"] == str((dest_root / "Jamie Ray").resolve())


def test_cross_device_companion_move_succeeds(test_env):
    """Verify cross-device companion move succeeds when Path.rename raises Errno 18."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]
    dest_folder = dest_root / "Jane Doe"

    video = incoming / "CrossDevice.mp4"
    video.write_bytes(b"V" * 1000)
    jpg = incoming / "CrossDevice.jpg"
    jpg.write_bytes(b"J" * 500)

    con = connect(db)
    cur = con.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
            reason, companions_json, status, created_at, updated_at
        ) VALUES ('f100', 's100', ?, ?, ?, ?, 'performer', '1', 'Jane Doe', NULL, 'filename', 'test', ?, 'pending', ?, ?)""",
        (str(video), str(dest_folder / video.name), str(dest_folder), video.name,
         json.dumps([{"source": str(jpg), "target": str(dest_folder / jpg.name)}]),
         utc_now(), utc_now())
    )
    proposal_id = cur.lastrowid
    con.commit()
    con.close()

    mock_stash = MagicMock()
    mock_stash.find_plugin_config.return_value = {
        "incomingFolders": [str(incoming)],
        "autoFilingDestinationRoots": [str(dest_root)]
    }
    mock_stash.call_GQL.return_value = {
        "findScene": {"id": "s100", "files": [{"id": "f100", "path": str(video)}]}
    }
    def mock_move(payload):
        target = Path(payload["destination_folder"]) / payload["destination_basename"]
        video.rename(target)
        return True
    mock_stash.move_files.side_effect = mock_move

    orig_rename = Path.rename
    def cross_device_rename(self, target):
        if self.suffix == ".jpg":
            err = OSError("Cross-device link")
            err.errno = 18
            raise err
        return orig_rename(self, target)

    with mock.patch.object(Path, "rename", cross_device_rename):
        res = apply_filing_proposal(db, mock_stash, proposal_id)

    assert res["status"] == "completed"
    assert (dest_folder / video.name).exists()
    assert (dest_folder / jpg.name).exists()
    assert not video.exists()
    assert not jpg.exists()


def test_cross_device_expected_move_suppression(test_env):
    """Verify expect_filesystem_move registers expected create and consume_expected_move_source/dest."""
    db = test_env["db"]
    src = "/Volumes/Main/Vault1/New/test.mp4"
    dst = "/Volumes/Vault2/Vault2/test.mp4"

    from librarymanager_core import (
        expect_filesystem_move,
        consume_expected_move_source,
        consume_expected_move_destination,
        consume_expected_create
    )

    expect_filesystem_move(db, src, dst, ttl_seconds=60)

    # Source deletion must be consumed
    assert consume_expected_move_source(db, src) is True
    # Destination creation must be consumed
    assert consume_expected_move_destination(db, dst) is True

    # Unknown paths must not be consumed
    assert consume_expected_move_source(db, "/Volumes/Main/Vault1/New/other.mp4") is False
    assert consume_expected_move_destination(db, "/Volumes/Vault2/Vault2/other.mp4") is False


def test_active_filing_transfer_stages_and_concurrency_serialization(tmp_path):
    from librarymanager_core import (
        _update_active_transfer, _clear_active_transfer, get_active_filing_transfers,
        apply_filing_proposal, _format_bytes, dashboard_data, connect
    )

    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    conn.close()

    # 1. Test helper and formatting
    assert _format_bytes(1024 * 1024 * 500) == "500.0 MB"
    assert _format_bytes(1024 * 1024 * 1024 * 2) == "2.0 GB"

    _update_active_transfer(
        db_path, proposal_id=101, scene_id="1", file_id="f1",
        source_path="/inc/video1.mp4", destination_path="/dest/performer/video1.mp4", destination_folder="/dest/performer",
        stage="validating", stage_label="Preflight Verification",
        detail="Validating paths...", total_bytes=1024*1024*500
    )

    transfers = get_active_filing_transfers(db_path)
    assert len(transfers) == 1
    assert transfers[0]["proposal_id"] == 101
    assert transfers[0]["stage"] == "validating"
    assert transfers[0]["stage_label"] == "Preflight Verification"
    assert transfers[0]["total_bytes"] == 1024*1024*500

    # Test dashboard_data includes active_filing_transfers
    class DummyStash:
        def find_plugin_config(self, *a, **k): return {}
    
    d_data = dashboard_data(db_path, stash=DummyStash())
    assert "active_filing_transfers" in d_data
    assert len(d_data["active_filing_transfers"]) == 1
    assert d_data["active_filing_transfers"][0]["proposal_id"] == 101

    # 2. Test concurrency guard: applying proposal 102 while 101 is active is blocked
    conn = connect(db_path)
    conn.execute(
        """INSERT INTO filing_proposals (
            id, scene_id, file_id, source_path, destination_filename, proposed_path, destination_folder,
            organize_by, matched_entity_id, matched_entity_name, match_source, reason, status, created_at, updated_at
        ) VALUES (
            102, '2', 'f2', '/inc/video2.mp4', 'video2.mp4', '/dest/video2.mp4', '/dest',
            'performer', 'p1', 'Performer One', 'scene_performer', 'Test reason', 'pending', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        )"""
    )
    conn.commit()
    conn.close()

    res = apply_filing_proposal(db_path, DummyStash(), 102)
    assert res["status"] == "blocked"
    assert "serialized" in res["reason"].lower()

    # 3. Clear active transfer and verify
    _clear_active_transfer(db_path, 101)
    transfers_after = get_active_filing_transfers(db_path)
    assert len(transfers_after) == 0



def test_backlog_granular_baseline_counts_and_selection_sanitization(tmp_path):
    """Verifies get_backlog_items calculates accurate granular counts:
    - Baseline snapshot total (immutable original baseline count)
    - Remaining in Incoming count (files still physically present in incoming folders)
    - Eligible video count (unfiled, present videos)
    - Remaining companion count (JPG/NFO still in incoming)
    - Already filed / ineligible count
    """
    db = tmp_path / "test.db"
    inc = tmp_path / "Incoming"
    inc.mkdir()
    root = tmp_path / "DestRoot"
    root.mkdir()

    # Create test files
    v1 = inc / "Video 1.mp4"
    v1.write_bytes(b"video 1 content")

    v2 = inc / "Video 2.mp4"
    v2.write_bytes(b"video 2 content")

    v3_moved = inc / "Video 3 Moved.mp4"
    v3_moved.write_bytes(b"video 3 content")

    c1 = inc / "Video 1.jpg"
    c1.write_bytes(b"image 1")

    c2_missing = inc / "Video 3.jpg"
    c2_missing.write_bytes(b"image 2")

    # Capture baseline with 5 files
    snapshot_incoming_baseline(db, [str(inc)])

    # Simulate Video 3 being moved to destination and its companion deleted from incoming
    v3_dest = root / "Performer" / "Video 3 Moved.mp4"
    v3_dest.parent.mkdir()
    v3_moved.unlink() # remove from incoming
    v3_dest.write_bytes(b"video 3 content")
    c2_missing.unlink() # remove companion from incoming

    now_str = utc_now()
    conn = connect(db)
    # Register filing proposal completed for Video 3
    conn.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, match_source, reason, status, created_at, updated_at
        ) VALUES (
            'f3', '3', ?, ?, ?, 'Video 3 Moved.mp4',
            'performer', 'p1', 'Performer', 'scene_performer', 'ok', 'completed', ?, ?
        )""",
        (str(v3_moved), str(v3_dest), str(v3_dest.parent), now_str, now_str)
    )
    conn.commit()
    conn.close()

    config = {
        "incomingFolders": [str(inc)],
        "autoFilingDestinationRoots": [str(root)]
    }

    res = librarymanager_core.get_backlog_items(db, None, config=config)

    # 1. Original snapshot total remains 5 (immutable)
    assert res["total_count"] == 4
    assert res["baseline_total"] == 5
    assert res["video_count"] == 2
    assert res["companion_count"] == 2

    # 2. Remaining in incoming: 2 videos (v1, v2) + 1 companion (c1) = 3 files
    assert res["eligible_count"] == 2
    assert res["remaining_companion_count"] == 1
    assert res["remaining_incoming_count"] == 3

    # 3. Already filed and ineligible counts
    assert res["already_filed_count"] == 0
    assert res["verified_moved_count"] == 1
    assert res["ineligible_count"] == 0

def test_backlog_statuses_and_reconciled_statistics(tmp_path):
    """Verifies get_backlog_items accurately distinguishes:
    - Verified moved files (status: 'moved', 'Moved out of Incoming' with destination path)
    - Pending proposals (status: 'has_proposal', 'Active Proposal Pending')
    - Genuinely missing files (status: 'missing_on_disk', 'File Missing on Disk')
    - Moved companion files (status: 'moved', destination path)
    - Ineligible files (e.g. outside incoming)
    - Mathematical reconciliation of all categories with immutable baseline total
    """
    db = tmp_path / "backlog_test.db"
    inc = tmp_path / "Incoming"
    inc.mkdir()
    root = tmp_path / "DestRoot"
    root.mkdir()

    # 1. Eligible video in incoming
    v_eligible = inc / "Eligible.mp4"
    v_eligible.write_bytes(b"eligible content")

    # 2. Pending proposal video in incoming
    v_pending = inc / "Pending.mp4"
    v_pending.write_bytes(b"pending content")

    # 3. Verified moved video (source missing in incoming, destination exists)
    v_moved = inc / "Moved.mp4"
    v_moved.write_bytes(b"moved content")

    # 4. Genuinely missing video (source missing, no proposal/move)
    v_missing = inc / "Missing.mp4"
    v_missing.write_bytes(b"missing content")

    # 5. Companion in incoming
    c_incoming = inc / "Eligible.jpg"
    c_incoming.write_bytes(b"jpg content")

    # 6. Companion moved with video
    c_moved = inc / "Moved.jpg"
    c_moved.write_bytes(b"moved jpg")

    # 7. Genuinely missing companion
    c_missing = inc / "Missing.jpg"
    c_missing.write_bytes(b"missing jpg")

    # Take baseline snapshot of all 7 files
    snapshot_incoming_baseline(db, [str(inc)])

    # Setup moved video & companion at destination
    v_dest = root / "Performer" / "Moved.mp4"
    c_dest = root / "Performer" / "Moved.jpg"
    v_dest.parent.mkdir()
    v_dest.write_bytes(b"moved content")
    c_dest.write_bytes(b"moved jpg")

    # Remove moved and missing files from incoming
    v_moved.unlink()
    c_moved.unlink()
    v_missing.unlink()
    c_missing.unlink()

    now_str = utc_now()
    conn = connect(db)
    # Insert completed proposal for v_moved
    conn.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, match_source, reason, status, created_at, updated_at
        ) VALUES (
            'f_moved', '10', ?, ?, ?, 'Moved.mp4',
            'performer', 'p1', 'Performer', 'scene_performer', 'ok', 'completed', ?, ?
        )""",
        (str(v_moved), str(v_dest), str(v_dest.parent), now_str, now_str)
    )
    # Insert pending proposal for v_pending
    conn.execute(
        """INSERT INTO filing_proposals (
            file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
            organize_by, matched_entity_id, matched_entity_name, match_source, reason, status, created_at, updated_at
        ) VALUES (
            'f_pending', '20', ?, ?, ?, 'Pending.mp4',
            'performer', 'p2', 'Performer 2', 'scene_performer', 'ok', 'pending', ?, ?
        )""",
        (str(v_pending), str(root / "Performer 2" / "Pending.mp4"), str(root / "Performer 2"), now_str, now_str)
    )
    conn.commit()
    conn.close()

    config = {
        "incomingFolders": [str(inc)],
        "autoFilingDestinationRoots": [str(root)]
    }

    res = librarymanager_core.get_backlog_items(db, None, config=config)

    # Verify Counts
    assert res["baseline_total"] == 7
    assert res["total_count"] == 5
    assert res["video_count"] == 3
    assert res["companion_count"] == 2

    # Physically in incoming: 1 eligible video + 1 pending video + 1 companion = 3
    assert res["remaining_incoming_count"] == 3
    assert res["eligible_count"] == 1
    assert res["pending_proposal_count"] == 1
    assert res["remaining_companion_count"] == 1

    # Verified moved: 1 video + 1 companion = 2
    assert res["verified_moved_video_count"] == 0
    assert res["verified_moved_companion_count"] == 0
    assert res["verified_moved_count"] == 2

    # Missing / unaccounted: 1 missing video + 1 missing companion = 2
    assert res["missing_count"] == 2

    # Ineligible videos: 4 total videos - 1 eligible = 3
    assert res["ineligible_count"] == 2

    # Mathematical Reconciliation Check:
    assert res["remaining_incoming_count"] + res["missing_count"] == res["total_count"]

    # Verify Status Details for Items
    items_by_path = {it["path"]: it for it in res["items"]}

    # Eligible video
    it_el = items_by_path[str(v_eligible)]
    assert it_el["status"] == "eligible"
    assert it_el["status_label"] == "Eligible"
    assert it_el["eligible"] is True
    assert it_el["exists_on_disk"] is True

    # Pending proposal video
    it_pend = items_by_path[str(v_pending)]
    assert it_pend["status"] == "has_proposal"
    assert it_pend["status_label"] == "Active Proposal Pending"
    assert it_pend["eligible"] is False
    assert it_pend["exists_on_disk"] is True

    # Verified moved records are retired from the detailed working set.
    assert str(v_moved) not in items_by_path

    # Missing video
    it_mis = items_by_path[str(v_missing)]
    assert it_mis["status"] == "missing_on_disk"
    assert it_mis["status_label"] == "File Missing on Disk"
    assert it_mis["eligible"] is False
    assert it_mis["exists_on_disk"] is False

    assert str(c_moved) not in items_by_path

    # Missing companion
    it_cmis = items_by_path[str(c_missing)]
    assert it_cmis["status"] == "missing_on_disk"
    assert "Missing" in it_cmis["status_label"]
    assert it_cmis["exists_on_disk"] is False


def test_successful_filing_clears_incoming_diagnostic(test_env):
    db, incoming, root = test_env["db"], test_env["incoming"], test_env["dest_root"]
    destination = root / "Alex Drake"
    destination.mkdir()
    snapshot_incoming_baseline(db, [str(incoming)])
    refresh_destination_dir_cache(db, [str(root)], max_depth=4)
    video = incoming / "scene-clear.mp4"
    video.write_bytes(b"video")
    conn = connect(db)
    conn.execute(
        """INSERT INTO files(file_id,scene_id,path,basename,performers_json,size,
                              fingerprints_json,scene_metadata_json,exists_on_disk,first_seen_at,last_seen_at)
           VALUES('clear-file','clear-scene',?,'scene-clear.mp4','[\"Alex Drake\"]',5,'[]','{}',1,'before','before')""",
        (str(video),),
    )
    conn.execute(
        """INSERT INTO incoming_files(path,first_seen_at,last_checked_at,status,filing_diagnostic)
           VALUES(?,'before','before','imported','Proposal ready: performer Alex Drake')""",
        (str(video),),
    )
    conn.commit()
    conn.close()
    stash = MagicMock()
    stash.call_GQL.side_effect = lambda query, variables=None: (
        {"findScene": {"id": "clear-scene", "files": [
            {"id": "clear-file", "path": str(video)}]}}
        if "findScene" in query else
        {"allPerformers": [{"id": "p1", "name": "Alex Drake", "alias_list": []}]}
    )
    stash.move_files.return_value = True
    scene = {"id": "clear-scene", "files": [{"id": "clear-file", "path": str(video)}],
             "performers": [{"id": "p1", "name": "Alex Drake"}], "studio": None, "tags": []}
    config = {"autoFilingEnabled": True, "autoFilingOrganizeBy": "performer",
              "autoFilingDestinationRoots": [str(root)], "incomingFolders": [str(incoming)]}
    proposal = evaluate_filing_proposal(db, stash, str(video), scene, config, allow_baseline=True)
    assert proposal
    result = apply_filing_proposal(db, stash, proposal["id"], config=config)
    assert result["status"] == "completed"
    conn = connect(db)
    row = conn.execute("SELECT last_error,status FROM filing_proposals WHERE id=?", (proposal["id"],)).fetchone()
    incoming_row = conn.execute("SELECT filing_diagnostic FROM incoming_files").fetchone()
    conn.close()
    assert row["status"] == "completed" and row["last_error"] is None
    assert incoming_row["filing_diagnostic"] is None


def test_stale_pending_proposal_recognises_verified_destination(test_env):
    db, incoming, root = test_env["db"], test_env["incoming"], test_env["dest_root"]
    source = incoming / "already-moved.mp4"
    destination = root / "Jane Doe" / source.name
    destination.write_bytes(b"moved")
    conn = connect(db)
    conn.execute(
        """INSERT INTO filing_proposals(id,file_id,scene_id,source_path,proposed_path,
            destination_folder,destination_filename,organize_by,matched_entity_id,matched_entity_name,match_source,
            reason,status,created_at,updated_at)
            VALUES(901,'verified-file','verified-scene',?,?,?,'already-moved.mp4','performer','p1',
                   'Jane Doe','metadata','Matched performer','pending','before','before')""",
        (str(source), str(destination), str(destination.parent)),
    )
    conn.execute(
        """INSERT INTO incoming_files(path,first_seen_at,last_checked_at,status,filing_diagnostic)
           VALUES(?,'before','before','imported','Proposal ready: performer Jane Doe')""",
        (str(destination),),
    )
    conn.commit()
    conn.close()
    stash = MagicMock()
    stash.call_GQL.return_value = {"findScene": {"id": "verified-scene", "files": [
        {"id": "verified-file", "path": str(destination)}]}}
    assert invalidate_stale_filing_proposals(db, stash=stash) == []
    conn = connect(db)
    proposal = conn.execute("SELECT status,last_error FROM filing_proposals WHERE id=901").fetchone()
    diagnostic = conn.execute("SELECT filing_diagnostic FROM incoming_files").fetchone()[0]
    conn.close()
    assert proposal["status"] == "completed" and proposal["last_error"] is None
    assert diagnostic is None


def test_retry_recognises_already_completed_destination(test_env):
    db, incoming, root = test_env["db"], test_env["incoming"], test_env["dest_root"]
    source = incoming / "retry-complete.mp4"
    destination = root / "Jane Doe" / source.name
    destination.write_bytes(b"complete")
    conn = connect(db)
    conn.execute(
        """INSERT INTO filing_proposals(id,file_id,scene_id,source_path,proposed_path,
            destination_folder,destination_filename,organize_by,matched_entity_id,matched_entity_name,match_source,
            reason,status,created_at,updated_at)
            VALUES(902,'retry-file','retry-scene',?,?,?,'retry-complete.mp4','performer','p1',
                   'Jane Doe','metadata','Matched performer','invalid','before','before')""",
        (str(source), str(destination), str(destination.parent)),
    )
    conn.execute(
        """INSERT INTO files(file_id,scene_id,path,basename,fingerprints_json,scene_metadata_json,
                              exists_on_disk,first_seen_at,last_seen_at)
           VALUES('retry-file','retry-scene',?,'retry-complete.mp4','[]','{}',1,'before','before')""",
        (str(destination),),
    )
    conn.execute(
        """INSERT INTO incoming_files(path,first_seen_at,last_checked_at,status,filing_diagnostic)
           VALUES(?,'before','before','imported','Proposal ready: performer Jane Doe')""",
        (str(destination),),
    )
    conn.commit()
    conn.close()
    stash = MagicMock()
    stash.call_GQL.return_value = {"findScene": {"id": "retry-scene", "files": [
        {"id": "retry-file", "path": str(destination)}]}}
    config = {"autoFilingEnabled": True, "incomingFolders": [str(incoming)]}
    result = retry_filing_proposal(db, stash, str(destination), config=config)
    assert result["success"] is True and result["already_filed"] is True


def test_retry_pending_proposal_is_review_state_not_filing_error(test_env):
    db, incoming = test_env["db"], test_env["incoming"]
    video = incoming / "pending-review.mp4"
    video.write_bytes(b"pending")
    conn = connect(db)
    conn.execute(
        """INSERT INTO filing_proposals(id,file_id,scene_id,source_path,proposed_path,
            destination_folder,destination_filename,organize_by,matched_entity_id,matched_entity_name,match_source,
            reason,status,created_at,updated_at)
            VALUES(903,'pending-file','pending-scene',?,'','','pending-review.mp4','performer','p1',
                   'Jane Doe','metadata','Matched performer','pending','before','before')""",
        (str(video),),
    )
    conn.commit()
    conn.close()
    result = retry_filing_proposal(
        db, MagicMock(), str(video),
        config={"autoFilingEnabled": True, "incomingFolders": [str(incoming)]},
    )
    assert result["success"] is False
    assert result["proposal_pending"] is True
    assert "ready for review" in result["message"]


def test_parenthetical_collection_folder_matches_primary_performer(test_env):
    folder = test_env["dest_root"] / "The Kyle Polaski (Michal Stranik, Damien Porch) Collection"
    folder.mkdir()
    paths, status = resolve_filing_destinations(
        [str(test_env["dest_root"])], "Kyle Polaski", database_path=test_env["db"]
    )
    assert status == "ok"
    assert paths == [folder]


def test_combined_filing_offers_performer_and_tag_destinations(test_env):
    db, incoming, root = test_env["db"], test_env["incoming"], test_env["dest_root"]
    snapshot_incoming_baseline(db, [str(incoming)])
    performer_folder = root / "The Kyle Polaski (Michal Stranik) Collection"
    tag_folder = root / "Blondes"
    performer_folder.mkdir()
    tag_folder.mkdir()
    video = incoming / "Kyle Polaski Blonde.mp4"
    video.write_bytes(b"combined")
    stash = MagicMock()
    stash.call_GQL.side_effect = lambda query, variables=None: {
        "{ allPerformers { id name disambiguation alias_list } }": {
            "allPerformers": [{"id": "p84", "name": "Kyle Polaski", "alias_list": []}]},
        "{ allStudios { id name aliases } }": {"allStudios": []},
    }.get(query, {})
    scene = {"id": "combined-scene", "files": [{"id": "combined-file", "path": str(video)}],
             "performers": [{"id": "p84", "name": "Kyle Polaski"}], "studio": None,
             "tags": [{"id": "t1", "name": "Blonde Hair"}]}
    config = {"autoFilingEnabled": True, "autoFilingOrganizeBy": "both",
              "autoFilingDestinationRoots": [str(root)], "incomingFolders": [str(incoming)]}
    proposal = evaluate_filing_proposal(db, stash, str(video), scene, config, allow_baseline=True)
    destinations = {item["destination_folder"] for item in proposal["candidate_destinations"]}
    assert destinations == {str(performer_folder), str(tag_folder)}


def test_refresh_choices_fetches_current_metadata_and_invalidates_empty_match(test_env):
    db, incoming, root = test_env["db"], test_env["incoming"], test_env["dest_root"]
    snapshot_incoming_baseline(db, [str(incoming)])
    video = incoming / "refresh-current.mp4"
    video.write_bytes(b"refresh")
    conn = connect(db)
    conn.execute(
        """INSERT INTO filing_proposals(id,file_id,scene_id,source_path,proposed_path,
            destination_folder,destination_filename,organize_by,matched_entity_id,matched_entity_name,match_source,
            reason,status,created_at,updated_at)
            VALUES(904,'refresh-file','refresh-scene',?,'/old/path','/old','refresh-current.mp4',
                   'performer','p1','Old Name','metadata','Matched performer','pending','before','before')""",
        (str(video),),
    )
    conn.commit()
    conn.close()
    stash = MagicMock()
    stash.call_GQL.return_value = {"findScene": {"id": "refresh-scene", "title": "Cleared",
        "files": [{"id": "refresh-file", "path": str(video)}], "performers": [],
        "studio": None, "tags": []}}
    config = {"autoFilingEnabled": True, "autoFilingOrganizeBy": "both",
              "autoFilingDestinationRoots": [str(root)], "incomingFolders": [str(incoming)]}
    result = retry_filing_proposal(
        db, stash, str(video), config=config, allow_refresh=True, proposal_id=904
    )
    assert result["success"] is False
    conn = connect(db)
    row = conn.execute("SELECT status,last_error FROM filing_proposals WHERE id=904").fetchone()
    conn.close()
    assert row["status"] == "invalid" and row["last_error"]


def test_dashboard_refresh_does_not_flash_completed_destination_as_unresolved(test_env):
    """Manual dashboard Refresh must apply configured Incoming roots just like live polling."""
    db, incoming, root = test_env["db"], test_env["incoming"], test_env["dest_root"]
    destination = root / "Jane Doe" / "scene-5541.mp4"
    destination.write_bytes(b"already filed")
    conn = connect(db)
    conn.execute(
        """INSERT INTO files(file_id,scene_id,path,basename,fingerprints_json,scene_metadata_json,
                              exists_on_disk,first_seen_at,last_seen_at)
           VALUES('11218','5541',?,'scene-5541.mp4','[]','{}',1,'before','before')""",
        (str(destination),),
    )
    conn.execute(
        """INSERT INTO incoming_files(path,first_seen_at,last_checked_at,status,detail,filing_diagnostic)
           VALUES(?,'before','before','imported','Stash scene',NULL)""",
        (str(destination),),
    )
    conn.execute(
        """INSERT INTO filing_proposals(id,file_id,scene_id,source_path,proposed_path,
            destination_folder,destination_filename,organize_by,matched_entity_id,matched_entity_name,
            match_source,reason,status,last_error,created_at,updated_at)
            VALUES(95541,'11218','5541',?,?,?,'scene-5541.mp4','performer','p1','Jane Doe',
                   'metadata','Matched performer','invalid','Historical source missing','before','before')""",
        (str(incoming / "scene-5541.mp4"), str(destination), str(destination.parent)),
    )
    conn.commit()
    conn.close()
    config = {"autoFilingEnabled": True, "incomingFolders": [str(incoming)]}
    stash = MagicMock()
    stash.find_plugin_config.return_value = config

    snapshot = librarymanager_core.dashboard_data(db, stash=stash)

    assert all(item["path"] != str(destination) for item in snapshot["incoming"]["active"])
    assert snapshot["filing_proposals"] == []


def test_incoming_snapshot_prioritises_pending_approval_over_invalid_history(test_env):
    """A pending proposal belongs under Filing Proposals even if a newer invalid row exists."""
    db, incoming, root = test_env["db"], test_env["incoming"], test_env["dest_root"]
    video = incoming / "awaiting-approval.mp4"
    video.write_bytes(b"pending")
    conn = connect(db)
    conn.execute(
        """INSERT INTO files(file_id,scene_id,path,basename,fingerprints_json,scene_metadata_json,
                              exists_on_disk,first_seen_at,last_seen_at)
           VALUES('approval-file','approval-scene',?,'awaiting-approval.mp4','[]','{}',1,'before','before')""",
        (str(video),),
    )
    conn.execute(
        """INSERT INTO incoming_files(path,first_seen_at,last_checked_at,status,detail,filing_diagnostic)
           VALUES(?,'before','before','imported','Stash scene','Proposal ready: performer Jane Doe')""",
        (str(video),),
    )
    proposal_values = (str(video), str(root / "Jane Doe" / video.name), str(root / "Jane Doe"))
    conn.execute(
        """INSERT INTO filing_proposals(id,file_id,scene_id,source_path,proposed_path,
            destination_folder,destination_filename,organize_by,matched_entity_id,matched_entity_name,
            match_source,reason,status,created_at,updated_at)
            VALUES(95542,'approval-file','approval-scene',?,?,?,'awaiting-approval.mp4',
                   'performer','p1','Jane Doe','metadata','Matched performer','pending','before','before')""",
        proposal_values,
    )
    conn.execute(
        """INSERT INTO filing_proposals(id,file_id,scene_id,source_path,proposed_path,
            destination_folder,destination_filename,organize_by,matched_entity_id,matched_entity_name,
            match_source,reason,status,last_error,created_at,updated_at)
            VALUES(95543,'approval-file','approval-scene',?,?,?,'awaiting-approval.mp4',
                   'performer','p1','Jane Doe','metadata','Matched performer','invalid',
                   'Historical failure','after','after')""",
        proposal_values,
    )
    conn.commit()
    conn.close()

    summary = librarymanager_core.incoming_summary(
        db, config={"autoFilingEnabled": True, "incomingFolders": [str(incoming)]}
    )
    item = next(entry for entry in summary["active"] if entry["path"] == str(video))
    assert item["has_pending_proposal"] is True
    assert item["filing_status"] == "pending"


def test_backlog_counts_identity_verified_stale_proposal_as_moved(test_env):
    """A stale proposal status must not make a verified destination video and JPG look missing."""
    db, incoming, root = test_env["db"], test_env["incoming"], test_env["dest_root"]
    source = incoming / "verified-move.mp4"
    source_sheet = incoming / "verified-move.mp4.jpg"
    source.write_bytes(b"verified video")
    source_sheet.write_bytes(b"verified sheet")
    snapshot_incoming_baseline(db, [str(incoming)])
    destination_dir = root / "Jane Doe"
    destination = destination_dir / source.name
    destination_sheet = destination_dir / source_sheet.name
    source.replace(destination)
    source_sheet.replace(destination_sheet)
    conn = connect(db)
    conn.execute(
        """INSERT INTO files(file_id,scene_id,path,basename,title,fingerprints_json,
                              scene_metadata_json,exists_on_disk,first_seen_at,last_seen_at)
           VALUES('verified-id','verified-scene',?,'verified-move.mp4','Verified move',
                  '[]','{}',1,'before','before')""",
        (str(destination),),
    )
    conn.execute(
        """INSERT INTO filing_proposals(file_id,scene_id,source_path,proposed_path,
            destination_folder,destination_filename,organize_by,matched_entity_id,matched_entity_name,
            match_source,reason,status,last_error,created_at,updated_at)
            VALUES('verified-id','verified-scene',?,?,?,'verified-move.mp4','performer','p1',
                   'Jane Doe','metadata','Matched performer','invalid','Old source missing','before','after')""",
        (str(source), str(destination), str(destination_dir)),
    )
    conn.commit()
    conn.close()

    backlog = librarymanager_core.get_backlog_items(
        db, config={"incomingFolders": [str(incoming)]}
    )
    by_path = {item["path"]: item for item in backlog["items"]}
    assert backlog["missing_count"] == 0
    assert backlog["verified_moved_count"] == 2
    assert by_path[str(source)]["status"] == "moved"
    assert by_path[str(source)]["scene_id"] == "verified-scene"
    assert by_path[str(source_sheet)]["status"] == "moved"


def test_backlog_metadata_refresh_preserves_scene_identity(test_env, monkeypatch):
    db, incoming = test_env["db"], test_env["incoming"]
    video = incoming / "metadata-refresh.mp4"
    video.write_bytes(b"refresh metadata")
    conn = connect(db)
    conn.execute(
        """INSERT INTO files(file_id,scene_id,path,basename,fingerprints_json,scene_metadata_json,
                              exists_on_disk,first_seen_at,last_seen_at)
           VALUES('metadata-file','metadata-scene',?,'metadata-refresh.mp4','[]','{}',1,'before','before')""",
        (str(video),),
    )
    conn.commit()
    conn.close()
    received = {}

    def fake_retry(database_path, stash, file_path, **kwargs):
        received.update(kwargs)
        return {"success": False, "error": "No destination folder found"}

    monkeypatch.setattr(librarymanager_core, "retry_filing_proposal", fake_retry)
    result = librarymanager_core.evaluate_backlog_batch(
        db, MagicMock(), [str(video)], config={}, allow_refresh=True
    )
    assert received["allow_refresh"] is True
    assert result["results"][0]["file_id"] == "metadata-file"
    assert result["results"][0]["scene_id"] == "metadata-scene"


def test_rename_protected_schema_migration_and_auto_rename_independence(tmp_path):
    """Verify schema migration safely resets legacy rename_protected locks to 0
    without queuing renames or bulk renaming, and Automatic Renaming alone controls renaming."""
    from librarymanager_core import connect, _ensure_schema, preview_safe_filenames, preview_scene_filename, apply_scene_filename, utc_now
    db = tmp_path / "migration_test.sqlite3"
    video = tmp_path / "Sample Video.mp4"
    video.write_bytes(b"content")

    # Connect to initialize schema
    conn = connect(db)
    conn.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, title, studio, performers_json, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f100', 's100', ?, 'Sample Video.mp4', 100, 'Super Title', 'Super Studio', '["Performer One"]', 1, ?, ?)""",
        (str(video), utc_now(), utc_now())
    )
    # Manually simulate legacy rename_protected = 1
    conn.execute(
        """INSERT INTO filename_state (file_id, base_stem, base_source, rename_protected, created_at, updated_at)
           VALUES ('f100', 'Sample Video', 'automatic_filing', 1, ?, ?)""",
        (utc_now(), utc_now())
    )
    conn.commit()
    conn.close()

    # 1. Run schema migration
    # 1. Run schema migration (clearing in-memory cache to simulate fresh connection)
    librarymanager_core._schema_applied.clear()
    conn = connect(db)
    conn.close()

    # 2. Verify rename_protected is safely reset to 0
    conn = connect(db)
    st = conn.execute("SELECT rename_protected FROM filename_state WHERE file_id='f100'").fetchone()
    assert st["rename_protected"] == 0

    # 3. Verify rename_queue has no items automatically enqueued
    queue_items = conn.execute("SELECT * FROM rename_queue").fetchall()
    assert len(queue_items) == 0
    conn.close()

    # 4. Preview generates normal proposal without being blocked by protection
    opts = {"filenameStyle": "studio_performers_title"}
    preview = preview_scene_filename(db, "s100", opts)
    assert preview["status"] == "ready"
    assert Path(preview["proposed_path"]).name == "Super Title - Super Studio - Performer One.mp4"
    assert video.exists(), "Original file must remain untouched"

    # 5. Safe batch preview also proposes normal rename
    summary, report = preview_safe_filenames(db, opts)
    assert summary["proposed"] == 1
    assert summary["conflicts"] == 0
    assert video.exists(), "Batch preview is strictly read-only"

    # 6. Explicit manual rename works when called
    moves = []
    apply_res = apply_scene_filename(
        db, "s100",
        lambda file_id, folder, basename: moves.append((file_id, folder, basename)) or True,
        opts
    )
    assert apply_res["status"] == "renamed"
    assert len(moves) == 1
    assert moves[0][2] == "Super Title - Super Studio - Performer One.mp4"

def test_retry_filing_proposal_force_rescan_invalidates_and_discovers_new_disk_folders(test_env):
    """Verify retry_filing_proposal with force_rescan=True invalidates cache and rescans live disk folders."""
    db = test_env["db"]
    incoming = test_env["incoming"]
    dest_root = test_env["dest_root"]

    snapshot_incoming_baseline(db, [str(incoming)])

    (dest_root / "Initial Performer").mkdir(parents=True, exist_ok=True)
    video = incoming / "Test Video - Initial Performer.mp4"
    video.write_bytes(b"dummy video data")

    conn = connect(db)
    now_str = utc_now()
    conn.execute("INSERT INTO files (file_id, scene_id, path, basename, exists_on_disk, first_seen_at, last_seen_at) VALUES ('f1', 's1', ?, ?, 1, ?, ?)", (str(video), video.name, now_str, now_str))
    conn.execute("INSERT INTO incoming_files (path, first_seen_at, last_checked_at, status, detail) VALUES (?, ?, ?, 'imported', 'Scene s1')", (str(video), now_str, now_str))
    conn.commit()
    conn.close()

    scene = {
        "id": "s1",
        "title": "Test Scene",
        "files": [{"id": "f1", "path": str(video)}],
        "performers": [{"id": "p1", "name": "Initial Performer", "alias_list": []}],
        "studio": None,
        "tags": []
    }

    mock_stash = MagicMock()
    mock_stash.call_GQL.return_value = {"findScene": scene, "allPerformers": scene["performers"]}

    config = {
        "autoFilingEnabled": True,
        "autoFilingOrganizeBy": "performer",
        "autoFilingMatchSource": "metadata_first",
        "autoFilingDestinationRoots": [str(dest_root)],
        "incomingFolders": [str(incoming)]
    }

    prop = evaluate_filing_proposal(db, mock_stash, str(video), scene, config)
    assert prop is not None
    assert prop["destination_folder"] == str((dest_root / "Initial Performer").resolve())

    # Now remove Initial Performer and create New Performer on disk without waiting 1 hour
    (dest_root / "Initial Performer").rmdir()
    (dest_root / "New Performer").mkdir(parents=True, exist_ok=True)

    scene["performers"] = [{"id": "p2", "name": "New Performer", "alias_list": []}]
    mock_stash.call_GQL.return_value = {"findScene": scene, "allPerformers": scene["performers"]}

    # Calling retry with force_rescan=True forces an immediate disk rescan and invalidation
    rescan_res = retry_filing_proposal(db, mock_stash, str(video), config=config, allow_refresh=True, force_rescan=True)
    assert rescan_res["success"] is True
    assert rescan_res["proposal"]["destination_folder"] == str((dest_root / "New Performer").resolve())
