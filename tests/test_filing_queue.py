"""
Focused tests for the filing transfer queue, stale-transfer recovery,
interrupted-move recovery, and duplicate-prevention logic.
"""
import json
import time
import sqlite3
from pathlib import Path

import pytest

# ── helpers ──────────────────────────────────────────────────────────────────

def _make_db(tmp_path):
    from librarymanager_core import connect
    db = tmp_path / "test.db"
    conn = connect(db)
    conn.close()
    return db


def _insert_proposal(db, proposal_id, src_path, dest_filename, status="pending",
                     dest_folder="", proposed_path="", candidates=None):
    from librarymanager_core import connect
    candidates_json = json.dumps(candidates or [])
    conn = connect(db)
    conn.execute("""
        INSERT OR REPLACE INTO filing_proposals
        (id, scene_id, file_id, source_path, destination_filename, proposed_path,
         destination_folder, organize_by, matched_entity_id, matched_entity_name,
         match_source, reason, status, candidate_destinations_json, created_at, updated_at)
        VALUES (?,  '99', 'f99', ?, ?, ?, ?, 'performer', 'p1', 'Performer One',
                'metadata', 'Test', ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
    """, (proposal_id, str(src_path), dest_filename, proposed_path, dest_folder,
          status, candidates_json))
    conn.commit()
    conn.close()


def _get_proposal_status(db, proposal_id):
    from librarymanager_core import connect
    conn = connect(db)
    row = conn.execute("SELECT status FROM filing_proposals WHERE id=?", (proposal_id,)).fetchone()
    conn.close()
    return row["status"] if row else None


def _get_queue_status(db, proposal_id):
    from librarymanager_core import connect
    conn = connect(db)
    row = conn.execute("SELECT status FROM filing_transfer_queue WHERE proposal_id=?",
                       (proposal_id,)).fetchone()
    conn.close()
    return row["status"] if row else None


# ── Test 1: enqueue creates queue row and sets proposal to 'queued' ───────────

def test_enqueue_filing_transfer_creates_row_and_marks_proposal_queued(tmp_path):
    from librarymanager_core import enqueue_filing_transfer, connect
    db = _make_db(tmp_path)
    src = tmp_path / "video.mp4"
    src.write_bytes(b"x" * 100)
    _insert_proposal(db, 1, src, "video.mp4", status="pending")

    result = enqueue_filing_transfer(db, 1, "/dest/Performer", update_metadata=True)
    assert result is True
    assert _get_proposal_status(db, 1) == "queued"

    conn = connect(db)
    row = conn.execute("SELECT * FROM filing_transfer_queue WHERE proposal_id=1").fetchone()
    conn.close()
    assert row is not None
    assert row["status"] == "queued"
    assert row["target_destination_folder"] == "/dest/Performer"
    assert row["update_metadata"] == 1


# ── Test 2: enqueue rejects non-pending proposals ────────────────────────────

def test_enqueue_rejects_non_pending_proposal(tmp_path):
    from librarymanager_core import enqueue_filing_transfer
    db = _make_db(tmp_path)
    src = tmp_path / "video.mp4"
    src.write_bytes(b"x")
    for bad_status in ("completed", "ignored", "needs_recovery", "queued"):
        _insert_proposal(db, 10 + ord(bad_status[0]), src, "v.mp4", status=bad_status)

    for bad_status in ("completed", "ignored", "needs_recovery"):
        pid = 10 + ord(bad_status[0])
        result = enqueue_filing_transfer(db, pid, "/dest/Folder")
        assert result is False, f"Expected False for status={bad_status}"


# ── Test 3: claim_next picks queued in FIFO order ────────────────────────────

def test_claim_next_filing_transfer_fifo(tmp_path):
    from librarymanager_core import enqueue_filing_transfer, claim_next_filing_transfer, connect
    db = _make_db(tmp_path)
    src = tmp_path / "video.mp4"
    src.write_bytes(b"x")
    _insert_proposal(db, 1, src, "v1.mp4", status="pending")
    _insert_proposal(db, 2, src, "v2.mp4", status="pending")

    enqueue_filing_transfer(db, 1, "/dest/A")
    time.sleep(0.01)
    enqueue_filing_transfer(db, 2, "/dest/B")

    row = claim_next_filing_transfer(db)
    assert row is not None
    assert row["proposal_id"] == 1
    assert row["status"] == "queued"  # status at time of claim; now 'processing' in DB

    conn = connect(db)
    db_row = conn.execute("SELECT status FROM filing_transfer_queue WHERE proposal_id=1").fetchone()
    conn.close()
    assert db_row["status"] == "processing"


# ── Test 4: claim returns None when queue is empty ───────────────────────────

def test_claim_returns_none_when_empty(tmp_path):
    from librarymanager_core import claim_next_filing_transfer
    db = _make_db(tmp_path)
    assert claim_next_filing_transfer(db) is None


# ── Test 5: fail_filing_transfer resets proposal to pending ──────────────────

def test_fail_filing_transfer_resets_proposal_to_pending(tmp_path):
    from librarymanager_core import enqueue_filing_transfer, claim_next_filing_transfer, fail_filing_transfer
    db = _make_db(tmp_path)
    src = tmp_path / "video.mp4"
    src.write_bytes(b"x")
    _insert_proposal(db, 1, src, "v.mp4", status="pending")
    enqueue_filing_transfer(db, 1, "/dest/A")
    row = claim_next_filing_transfer(db)
    fail_filing_transfer(db, row["id"], "simulated failure")

    assert _get_proposal_status(db, 1) == "pending"
    assert _get_queue_status(db, 1) == "failed"


# ── Test 6: recover_abandoned_processing_queue resets stuck processing rows ──

def test_recover_abandoned_processing_queue_resets_to_queued(tmp_path):
    from librarymanager_core import (
        enqueue_filing_transfer, claim_next_filing_transfer,
        recover_abandoned_processing_queue, connect
    )
    db = _make_db(tmp_path)
    src = tmp_path / "video.mp4"
    src.write_bytes(b"x")
    _insert_proposal(db, 1, src, "v.mp4", status="pending")
    enqueue_filing_transfer(db, 1, "/dest/A")
    claim_next_filing_transfer(db)  # moves to 'processing'
    # Seed stash_job_id so recovery can query Stash for liveness
    conn = connect(db)
    conn.execute("UPDATE filing_transfer_queue SET stash_job_id='job-001' WHERE proposal_id=1")
    conn.commit(); conn.close()
    assert _get_queue_status(db, 1) == "processing"

    class _StashFinished:
        def call_GQL(self, q, v=None):
            return {"findJob": {"status": "FINISHED"}}

    result = recover_abandoned_processing_queue(db, stash=_StashFinished())
    assert result["reset"] == 1
    assert _get_queue_status(db, 1) == "queued"


# ── Test 7: apply_filing_proposal accepts 'queued' status ────────────────────

def test_apply_filing_proposal_accepts_queued_status(tmp_path):
    """Verify the status guard was widened to include 'queued'."""
    from librarymanager_core import apply_filing_proposal, connect

    db = _make_db(tmp_path)
    dest_folder = tmp_path / "Performer"
    dest_folder.mkdir()
    src = tmp_path / "video.mp4"
    src.write_bytes(b"x" * 200)
    dest_video = dest_folder / "video.mp4"

    _insert_proposal(db, 1, src, "video.mp4",
                     status="queued",
                     dest_folder=str(dest_folder),
                     proposed_path=str(dest_video))

    class FakeStash:
        def find_plugin_config(self, *a, **k): return {}
        def call_GQL(self, query, variables=None):
            # Preflight ownership check
            return {"findScene": {"id": "99", "files": [{"id": "f99", "path": str(src)}]}}
        def move_files(self, args):
            # Simulate successful move
            import shutil
            ids = args.get("ids", [])
            dst_folder = Path(args["destination_folder"])
            dst_name = args["destination_basename"]
            dst_folder.mkdir(exist_ok=True)
            shutil.move(str(src), str(dst_folder / dst_name))
            return True

    res = apply_filing_proposal(db, FakeStash(), 1, target_destination_folder=str(dest_folder))
    # Should not be blocked due to status; may succeed or fail on other preflight
    assert res.get("status") != "blocked" or "already" not in res.get("reason", ""), \
        f"Should not block on 'queued' status; got: {res}"


# ── Test 8: recover_stale_active_transfers — disk complete, no stash ─────────

def test_stale_recovery_disk_complete_marks_completed(tmp_path):
    from librarymanager_core import (
        _update_active_transfer, recover_stale_active_transfers, connect
    )
    db = _make_db(tmp_path)
    src = tmp_path / "video.mp4"
    dest_dir = tmp_path / "Dest"
    dest_dir.mkdir()
    dest = dest_dir / "video.mp4"

    # Simulate: file was moved (src gone, dest present at right size)
    dest.write_bytes(b"x" * 500)

    _insert_proposal(db, 1, src, "video.mp4",
                     status="pending", dest_folder=str(dest_dir), proposed_path=str(dest))

    # Insert stale active transfer (updated_at in the past)
    _update_active_transfer(
        db, 1, "99", "f99", str(src), str(dest), str(dest_dir),
        "moving_video", "Transferring", "Moving...", total_bytes=500
    )
    # Back-date updated_at to 20 minutes ago
    conn = connect(db)
    conn.execute(
        "UPDATE active_filing_transfers SET updated_at=datetime('now', '-20 minutes') WHERE proposal_id=1"
    )
    conn.commit()
    conn.close()

    class _StashAtDest:
        def call_GQL(self, q, v=None):
            return {"findFile": {"id": "f99", "path": str(dest)}}

    recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10, stash=_StashAtDest())
    assert len(recovered) == 1
    assert recovered[0]["outcome"] == "completed"
    assert _get_proposal_status(db, 1) == "completed"


# ── Test 9: stale recovery — source still present → reset to pending ─────────

def test_stale_recovery_source_present_resets_to_pending(tmp_path):
    from librarymanager_core import (
        _update_active_transfer, recover_stale_active_transfers, connect
    )
    db = _make_db(tmp_path)
    src = tmp_path / "video.mp4"
    dest_dir = tmp_path / "Dest"
    dest_dir.mkdir()
    dest = dest_dir / "video.mp4"
    src.write_bytes(b"x" * 500)  # source still exists, dest does not

    _insert_proposal(db, 1, src, "video.mp4",
                     status="pending", dest_folder=str(dest_dir), proposed_path=str(dest))
    _update_active_transfer(
        db, 1, "99", "f99", str(src), str(dest), str(dest_dir),
        "moving_video", "Transferring", "Moving...", total_bytes=500
    )
    conn = connect(db)
    conn.execute(
        "UPDATE active_filing_transfers SET updated_at=datetime('now', '-20 minutes') WHERE proposal_id=1"
    )
    conn.commit()
    conn.close()

    recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10)
    assert len(recovered) == 1
    assert recovered[0]["outcome"] == "reset_to_pending"
    assert _get_proposal_status(db, 1) == "pending"


# ── Test 10: stale recovery — both present → needs_recovery ──────────────────

def test_stale_recovery_both_present_marks_needs_recovery(tmp_path):
    from librarymanager_core import (
        _update_active_transfer, recover_stale_active_transfers, connect
    )
    db = _make_db(tmp_path)
    src = tmp_path / "video.mp4"
    dest_dir = tmp_path / "Dest"
    dest_dir.mkdir()
    dest = dest_dir / "video.mp4"
    src.write_bytes(b"x" * 500)   # both exist (copy scenario)
    dest.write_bytes(b"x" * 500)

    _insert_proposal(db, 1, src, "video.mp4",
                     status="pending", dest_folder=str(dest_dir), proposed_path=str(dest))
    _update_active_transfer(
        db, 1, "99", "f99", str(src), str(dest), str(dest_dir),
        "moving_video", "Transferring", "Moving...", total_bytes=500
    )
    conn = connect(db)
    conn.execute(
        "UPDATE active_filing_transfers SET updated_at=datetime('now', '-20 minutes') WHERE proposal_id=1"
    )
    conn.commit()
    conn.close()

    recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10)
    assert len(recovered) == 1
    assert recovered[0]["outcome"] == "needs_recovery"
    assert _get_proposal_status(db, 1) == "needs_recovery"


# ── Test 11: stale recovery is idempotent (safe to repeat) ───────────────────

def test_stale_recovery_idempotent(tmp_path):
    """Running recovery twice must not double-process or corrupt state."""
    from librarymanager_core import (
        _update_active_transfer, recover_stale_active_transfers, connect
    )
    db = _make_db(tmp_path)
    src = tmp_path / "video.mp4"
    dest_dir = tmp_path / "Dest"
    dest_dir.mkdir()
    dest = dest_dir / "video.mp4"
    dest.write_bytes(b"x" * 100)  # moved already

    _insert_proposal(db, 1, src, "video.mp4",
                     status="pending", dest_folder=str(dest_dir), proposed_path=str(dest))
    _update_active_transfer(
        db, 1, "99", "f99", str(src), str(dest), str(dest_dir),
        "moving_video", "Transferring", "Moving...", total_bytes=100
    )
    conn = connect(db)
    conn.execute(
        "UPDATE active_filing_transfers SET updated_at=datetime('now', '-20 minutes') WHERE proposal_id=1"
    )
    conn.commit()
    conn.close()

    class _StashAtDest:
        def call_GQL(self, q, v=None):
            return {"findFile": {"id": "f99", "path": str(dest)}}

    r1 = recover_stale_active_transfers(db, stale_threshold_minutes=10, stash=_StashAtDest())
    r2 = recover_stale_active_transfers(db, stale_threshold_minutes=10, stash=_StashAtDest())
    assert len(r1) == 1
    assert len(r2) == 0  # Second pass: no active transfer row to process
    assert _get_proposal_status(db, 1) == "completed"


# ── Test 12: duplicate enqueue is idempotent ─────────────────────────────────

def test_enqueue_duplicate_is_idempotent(tmp_path):
    """Enqueuing the same pending proposal twice must not create duplicate rows."""
    from librarymanager_core import enqueue_filing_transfer, connect
    db = _make_db(tmp_path)
    src = tmp_path / "video.mp4"
    src.write_bytes(b"x")
    _insert_proposal(db, 1, src, "v.mp4", status="pending")

    r1 = enqueue_filing_transfer(db, 1, "/dest/A")
    # proposal is now 'queued'; second call must fail (not pending)
    r2 = enqueue_filing_transfer(db, 1, "/dest/B")
    assert r1 is True
    assert r2 is False  # Already queued, not pending

    conn = connect(db)
    count = conn.execute("SELECT COUNT(*) FROM filing_transfer_queue WHERE proposal_id=1").fetchone()[0]
    conn.close()
    assert count == 1  # Still only one row


