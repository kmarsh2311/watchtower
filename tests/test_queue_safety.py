"""
Focused safety tests:
1. Abandoned 'processing' jobs cannot be claimed before reconciliation finishes
2. Stash verification — completed requires disk + Stash + Watchtower agreement;
   every unverifiable state must produce needs_recovery, never false completion
3. Monitor startup actually supplies a Stash connection to recovery
4. Queue filing proposal handler — enqueue, reject, no-dest guard
"""
import json
import time
from pathlib import Path
import pytest


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_db(tmp_path):
    from librarymanager_core import connect
    db = tmp_path / "test.db"
    connect(db).close()
    return db


def _insert_proposal(db, pid, src_path, dest_filename, status="pending",
                     dest_folder="", proposed_path=""):
    from librarymanager_core import connect
    conn = connect(db)
    conn.execute("""
        INSERT OR REPLACE INTO filing_proposals
        (id, scene_id, file_id, source_path, destination_filename, proposed_path,
         destination_folder, organize_by, matched_entity_id, matched_entity_name,
         match_source, reason, status, created_at, updated_at)
        VALUES (?, '99', 'f99', ?, ?, ?, ?,
                'performer', 'p1', 'Performer One', 'metadata', 'Test',
                ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
    """, (pid, str(src_path), dest_filename, proposed_path, dest_folder, status))
    conn.commit()
    conn.close()


def _get_proposal(db, pid):
    from librarymanager_core import connect
    conn = connect(db)
    row = conn.execute("SELECT * FROM filing_proposals WHERE id=?", (pid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _get_proposal_status(db, pid):
    r = _get_proposal(db, pid)
    return r["status"] if r else None


def _get_queue_row(db, pid):
    from librarymanager_core import connect
    conn = connect(db)
    row = conn.execute(
        "SELECT * FROM filing_transfer_queue WHERE proposal_id=?", (pid,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _make_stale_transfer(tmp_path, dest_size=300):
    """
    Return (db, src_path, dest_path) with:
    - source GONE (move happened)
    - destination present at dest_size bytes
    - active_filing_transfers row back-dated 20 minutes (stale)
    """
    from librarymanager_core import _update_active_transfer, connect
    db = _make_db(tmp_path)
    src = tmp_path / "video.mp4"
    dest_dir = tmp_path / "Dest"
    dest_dir.mkdir()
    dest = dest_dir / "video.mp4"
    dest.write_bytes(b"x" * dest_size)   # source is gone, dest is present

    _insert_proposal(db, 1, src, "video.mp4", status="pending",
                     dest_folder=str(dest_dir), proposed_path=str(dest))
    _update_active_transfer(
        db, 1, "99", "f99", str(src), str(dest), str(dest_dir),
        "moving_video", "Transferring", "Moving...", total_bytes=dest_size
    )
    conn = connect(db)
    conn.execute(
        "UPDATE active_filing_transfers SET updated_at=datetime('now','-20 minutes') WHERE proposal_id=1"
    )
    conn.commit()
    conn.close()
    return db, src, dest


# ─── Group A: Abandoned job isolation ────────────────────────────────────────

class TestAbandonedJobIsolation:
    """processing rows must not be claimable until recovery runs and clears/verifies them."""

    def test_processing_row_not_claimable_by_claim_next(self, tmp_path):
        """claim_next_filing_transfer must not return a 'processing' row."""
        from librarymanager_core import enqueue_filing_transfer, claim_next_filing_transfer, connect
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"
        src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        enqueue_filing_transfer(db, 1, "/dest/A")
        conn = connect(db)
        conn.execute("UPDATE filing_transfer_queue SET status='processing' WHERE proposal_id=1")
        conn.commit()
        conn.close()

        assert claim_next_filing_transfer(db) is None, \
            "processing row must not be claimable — would cause a duplicate move"

    def test_recover_abandoned_resets_to_queued_before_reclaimable(self, tmp_path):
        """Only after recover_abandoned_processing_queue() can the row be claimed."""
        from librarymanager_core import (
            enqueue_filing_transfer, claim_next_filing_transfer,
            recover_abandoned_processing_queue, connect
        )
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"
        src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        enqueue_filing_transfer(db, 1, "/dest/A")
        conn = connect(db)
        conn.execute("UPDATE filing_transfer_queue SET status='processing' WHERE proposal_id=1")
        conn.commit()
        conn.close()

        assert claim_next_filing_transfer(db) is None   # before recovery: locked
        # seed stash_job_id so recovery can verify liveness
        conn = connect(db)
        conn.execute("UPDATE filing_transfer_queue SET stash_job_id='job-1' WHERE proposal_id=1")
        conn.commit(); conn.close()
        class _StashDone:
            def call_GQL(self, q, v=None): return {"findJob": {"status": "FINISHED"}}
        result = recover_abandoned_processing_queue(db, stash=_StashDone())
        assert result["reset"] == 1
        row = claim_next_filing_transfer(db)             # after recovery: claimable
        assert row is not None
        assert row["proposal_id"] == 1

    def test_recover_abandoned_does_not_touch_queued_or_done_rows(self, tmp_path):
        from librarymanager_core import enqueue_filing_transfer, recover_abandoned_processing_queue, connect
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"
        src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v1.mp4", status="pending")
        enqueue_filing_transfer(db, 1, "/dest/A")   # status='queued'

        conn = connect(db)
        conn.execute("""INSERT INTO filing_transfer_queue
            (proposal_id, target_destination_folder, status, enqueued_at)
            VALUES (2, '/dest/B', 'done', '2026-01-01T00:00:00Z')""")
        conn.commit()
        conn.close()
        _insert_proposal(db, 2, src, "v2.mp4", status="completed")

        result = recover_abandoned_processing_queue(db, stash=None)  # no processing rows to touch
        assert result["reset"] == 0
        assert _get_queue_row(db, 1)["status"] == "queued"
        assert _get_queue_row(db, 2)["status"] == "done"

    def test_stale_recovery_verified_complete_then_apply_blocks_second_execution(self, tmp_path):
        """
        After startup:
          1. recover_abandoned_processing_queue resets processing → queued
          2. recover_stale_active_transfers (with Stash confirming dest) → completed
          3. Worker claims the queued row (it wasn't auto-removed by recovery)
          4. apply_filing_proposal blocks because proposal.status == 'completed'
        No duplicate move is possible.
        """
        from librarymanager_core import (
            enqueue_filing_transfer, recover_abandoned_processing_queue,
            recover_stale_active_transfers, claim_next_filing_transfer,
            apply_filing_proposal, _update_active_transfer, connect
        )
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"
        dest_dir = tmp_path / "Dest"
        dest_dir.mkdir()
        dest = dest_dir / "video.mp4"
        dest.write_bytes(b"x" * 300)   # file moved before crash

        _insert_proposal(db, 1, src, "video.mp4", status="pending",
                         dest_folder=str(dest_dir), proposed_path=str(dest))
        enqueue_filing_transfer(db, 1, str(dest_dir))

        # Simulate worker crash: queue stuck in processing, active_transfer orphaned
        conn = connect(db)
        conn.execute("UPDATE filing_transfer_queue SET status='processing' WHERE proposal_id=1")
        conn.commit()
        conn.close()

        _update_active_transfer(
            db, 1, "99", "f99", str(src), str(dest), str(dest_dir),
            "moving_video", "Transferring", "Moving...", total_bytes=300
        )
        conn = connect(db)
        conn.execute(
            "UPDATE active_filing_transfers SET updated_at=datetime('now','-20 minutes') WHERE proposal_id=1"
        )
        conn.commit()
        conn.close()

        # Step 1: Startup — reset abandoned processing row
        # Seed job_id so recovery can query Stash
        conn = connect(db)
        conn.execute("UPDATE filing_transfer_queue SET stash_job_id='job-dead' WHERE proposal_id=1")
        conn.commit(); conn.close()
        class _StashJobDead:
            def call_GQL(self, q, v=None): return {"findJob": {"status": "FINISHED"}}
        result = recover_abandoned_processing_queue(db, stash=_StashJobDead())
        assert result["reset"] == 1
        assert _get_queue_row(db, 1)["status"] == "queued"

        # Step 2: Stash confirms file is at dest → completed
        class StashAtDest:
            def call_GQL(self, query, variables=None):
                return {"findFile": {"id": "f99", "path": str(dest)}}

        recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10, stash=StashAtDest())
        assert len(recovered) == 1
        assert recovered[0]["outcome"] == "completed"
        assert _get_proposal_status(db, 1) == "completed"

        # Step 3: Worker claims the now-queued row (recovery didn't clean it up)
        row = claim_next_filing_transfer(db)
        assert row is not None

        # Step 4: apply_filing_proposal blocks — proposal already completed
        class NoopStash:
            def find_plugin_config(self, *a, **k): return {}
            def call_GQL(self, *a, **k): return None
            def move_files(self, *a, **k): return False

        res = apply_filing_proposal(db, NoopStash(), 1)
        assert res["status"] == "blocked"
        assert "completed" in res["reason"].lower()


# ─── Group B: Stash verification — completed requires full triple agreement ───

class TestStashVerificationSafety:
    """
    The ONLY path to 'completed' is: disk says moved AND Stash confirms path at dest.
    Every other state must be needs_recovery (not completed, not silently trusted).
    """

    def test_full_agreement_disk_and_stash_at_dest_produces_completed(self, tmp_path):
        """The only correct path to completed: all three agree."""
        from librarymanager_core import recover_stale_active_transfers
        db, src, dest = _make_stale_transfer(tmp_path)

        class StashAtDest:
            def call_GQL(self, q, v=None):
                return {"findFile": {"id": "f99", "path": str(dest)}}

        recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10, stash=StashAtDest())
        assert len(recovered) == 1
        assert recovered[0]["outcome"] == "completed"
        assert _get_proposal_status(db, 1) == "completed"

    def test_stash_path_at_unexpected_location_causes_needs_recovery(self, tmp_path):
        """Stash reports file at a third location → needs_recovery."""
        from librarymanager_core import recover_stale_active_transfers
        db, src, dest = _make_stale_transfer(tmp_path)

        class StashPathElsewhere:
            def call_GQL(self, q, v=None):
                return {"findFile": {"id": "f99", "path": "/Volumes/UnknownDrive/mystery.mp4"}}

        recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10, stash=StashPathElsewhere())
        assert len(recovered) == 1
        assert recovered[0]["outcome"] == "needs_recovery"
        p = _get_proposal(db, 1)
        assert p["status"] == "needs_recovery"
        assert "ambiguous" in (p["last_error"] or "").lower()

    def test_stash_still_at_source_path_causes_needs_recovery(self, tmp_path):
        """
        Disk says moved but Stash path still points to source (not yet rescanned).
        Must be needs_recovery — we cannot mark completed without Stash agreement.
        The last_error must explain how to resolve (run a Stash scan).
        """
        from librarymanager_core import recover_stale_active_transfers
        db, src, dest = _make_stale_transfer(tmp_path)

        class StashAtSource:
            def call_GQL(self, q, v=None):
                return {"findFile": {"id": "f99", "path": str(src)}}

        recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10, stash=StashAtSource())
        assert len(recovered) == 1
        assert recovered[0]["outcome"] == "needs_recovery", \
            "Stash pointing to source must not be treated as verified completion"
        p = _get_proposal(db, 1)
        assert p["status"] == "needs_recovery"
        # Error message must guide the user
        assert "scan" in (p["last_error"] or "").lower()

    def test_stash_gql_exception_causes_needs_recovery_not_completion(self, tmp_path):
        """
        Stash offline / GQL failure during recovery must produce needs_recovery.
        Disk state alone is INSUFFICIENT to confirm completion.
        """
        from librarymanager_core import recover_stale_active_transfers
        db, src, dest = _make_stale_transfer(tmp_path)

        class StashOffline:
            def call_GQL(self, q, v=None):
                raise ConnectionError("Stash unreachable")

        recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10, stash=StashOffline())
        assert len(recovered) == 1
        assert recovered[0]["outcome"] == "needs_recovery", \
            "GQL failure must yield needs_recovery, not completed"
        p = _get_proposal(db, 1)
        assert p["status"] == "needs_recovery"
        assert "stash" in (p["last_error"] or "").lower()

    def test_stash_returns_no_file_record_causes_needs_recovery(self, tmp_path):
        """
        Stash returns None for findFile (file not indexed).
        Cannot confirm location → needs_recovery, never false completion.
        """
        from librarymanager_core import recover_stale_active_transfers
        db, src, dest = _make_stale_transfer(tmp_path)

        class StashNoRecord:
            def call_GQL(self, q, v=None):
                return {"findFile": None}

        recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10, stash=StashNoRecord())
        assert len(recovered) == 1
        assert recovered[0]["outcome"] == "needs_recovery", \
            "Missing Stash record must yield needs_recovery, not completed"
        p = _get_proposal(db, 1)
        assert p["status"] == "needs_recovery"
        assert "scan" in (p["last_error"] or "").lower()

    def test_no_stash_argument_causes_needs_recovery(self, tmp_path):
        """
        Calling recover_stale_active_transfers without stash= must produce needs_recovery.
        This enforces that all callers supply a Stash connection.
        """
        from librarymanager_core import recover_stale_active_transfers
        db, src, dest = _make_stale_transfer(tmp_path)

        recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10)
        # stash=None (default)
        assert len(recovered) == 1
        assert recovered[0]["outcome"] == "needs_recovery", \
            "No stash connection must yield needs_recovery — disk alone is insufficient"
        assert _get_proposal_status(db, 1) == "needs_recovery"

    def test_needs_recovery_clears_active_transfer_so_other_proposals_can_proceed(self, tmp_path):
        """
        Even when outcome is needs_recovery, the active_filing_transfers row must be
        deleted so the serialization guard does not permanently block all other transfers.
        """
        from librarymanager_core import (
            recover_stale_active_transfers, get_active_filing_transfers
        )
        db, src, dest = _make_stale_transfer(tmp_path)

        class StashOffline:
            def call_GQL(self, q, v=None):
                raise ConnectionError("offline")

        assert len(get_active_filing_transfers(db)) == 1

        recover_stale_active_transfers(db, stale_threshold_minutes=10, stash=StashOffline())
        assert len(get_active_filing_transfers(db)) == 0, \
            "Active transfer row must be cleared even for needs_recovery outcome"

    def test_source_present_dest_absent_resets_to_pending_without_stash(self, tmp_path):
        """
        Source still present and dest absent means move never happened.
        This is safe to reset to pending without Stash verification because
        no file was actually moved — no Stash records changed.
        """
        from librarymanager_core import _update_active_transfer, recover_stale_active_transfers, connect
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"
        dest_dir = tmp_path / "Dest"
        dest_dir.mkdir()
        dest = dest_dir / "video.mp4"
        src.write_bytes(b"x" * 300)   # source present, dest absent

        _insert_proposal(db, 1, src, "video.mp4", status="pending",
                         dest_folder=str(dest_dir), proposed_path=str(dest))
        _update_active_transfer(
            db, 1, "99", "f99", str(src), str(dest), str(dest_dir),
            "validating", "Preflight", "...", total_bytes=300
        )
        conn = connect(db)
        conn.execute(
            "UPDATE active_filing_transfers SET updated_at=datetime('now','-20 minutes') WHERE proposal_id=1"
        )
        conn.commit()
        conn.close()

        # No stash needed for source-present case (move didn't happen, Stash unchanged)
        recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10)
        assert len(recovered) == 1
        assert recovered[0]["outcome"] == "reset_to_pending"
        assert _get_proposal_status(db, 1) == "pending"


# ─── Group C: Monitor startup supplies Stash connection ──────────────────────

class TestMonitorStartupSuppliesStash:
    """
    Verify that the monitor startup path calls recover_stale_active_transfers
    with the stash= argument, not without it. Tested by inspecting the source.
    """

    def test_monitor_startup_passes_stash_to_recovery(self):
        """The monitor must pass stash= so that completed requires Stash verification."""
        import ast, sys
        mon_path = "/Users/keithmarsh/Developer/watchtower/librarymanager_monitor.py"
        with open(mon_path, "r", encoding="utf-8") as f:
            source = f.read()
        # Find all calls to recover_stale_active_transfers
        # Every call that passes database_path must also pass stash=
        import re
        calls = list(re.finditer(r'recover_stale_active_transfers\(([^)]+)\)', source))
        assert calls, "Expected at least one call to recover_stale_active_transfers in monitor"
        for m in calls:
            args_text = m.group(1)
            assert "stash=" in args_text, (
                f"All monitor calls must pass stash=; found: recover_stale_active_transfers({args_text})"
            )

    def test_process_filing_queue_passes_stash_to_recovery(self):
        """The queue worker must also pass stash= to recovery inside its loop."""
        import re
        py_path = "/Users/keithmarsh/Developer/watchtower/librarymanager.py"
        with open(py_path, "r", encoding="utf-8") as f:
            source = f.read()
        calls = list(re.finditer(r'recover_stale_active_transfers\(([^)]+)\)', source))
        assert calls, "Expected at least one call in librarymanager.py"
        for m in calls:
            args_text = m.group(1)
            assert "stash=" in args_text, (
                f"Queue worker must pass stash=; found: recover_stale_active_transfers({args_text})"
            )


# ─── Group D: queue_filing_proposal handler ──────────────────────────────────

class TestQueueFilingProposalHandler:

    def test_queue_proposal_succeeds_for_pending_proposal(self, tmp_path):
        from librarymanager_core import enqueue_filing_transfer, connect
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"
        src.write_bytes(b"x")
        dest_dir = tmp_path / "Dest"
        dest_dir.mkdir()
        _insert_proposal(db, 1, src, "video.mp4", status="pending",
                         dest_folder=str(dest_dir))

        assert enqueue_filing_transfer(db, 1, str(dest_dir)) is True
        q = _get_queue_row(db, 1)
        assert q["status"] == "queued"
        assert _get_proposal_status(db, 1) == "queued"

    def test_queue_proposal_rejected_for_completed_proposal(self, tmp_path):
        from librarymanager_core import enqueue_filing_transfer
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"
        src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="completed")
        assert enqueue_filing_transfer(db, 1, "/dest/A") is False

    def test_queue_proposal_rejected_for_needs_recovery(self, tmp_path):
        from librarymanager_core import enqueue_filing_transfer
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"
        src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="needs_recovery")
        assert enqueue_filing_transfer(db, 1, "/dest/A") is False

    def test_worker_blocks_on_already_completed_proposal(self, tmp_path):
        """
        Race: recovery marks proposal completed between queue reset and worker claim.
        apply_filing_proposal must block; the queue row is then failed without repeating the move.
        """
        from librarymanager_core import (
            enqueue_filing_transfer, claim_next_filing_transfer,
            fail_filing_transfer, apply_filing_proposal, connect
        )
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"
        src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        enqueue_filing_transfer(db, 1, "/dest/A")

        # Recovery already completed the proposal
        conn = connect(db)
        conn.execute("UPDATE filing_proposals SET status='completed' WHERE id=1")
        conn.commit()
        conn.close()

        row = claim_next_filing_transfer(db)
        assert row is not None

        class NoopStash:
            def find_plugin_config(self, *a, **k): return {}
            def call_GQL(self, *a, **k): return None
            def move_files(self, *a, **k): return False

        res = apply_filing_proposal(db, NoopStash(), 1)
        assert res["status"] == "blocked"
        assert "completed" in res["reason"].lower()

        fail_filing_transfer(db, row["id"], res["reason"])
        # Proposal stays completed; queue row is failed
        assert _get_proposal_status(db, 1) == "completed"
        assert _get_queue_row(db, 1)["status"] == "failed"

    def test_cancel_pending_rename_removes_from_queue_and_preserves_filename(self, tmp_path):
        """Cancel removes the pending rename and preserves the current filename."""
        from librarymanager_core import connect, enqueue_rename, cancel_pending_rename, claim_due_rename
        db = _make_db(tmp_path)

        video = tmp_path / "yb_17_sc04.vid-720.mp4"
        video.write_bytes(b"video-content")

        now = time.time()
        conn = connect(db)
        conn.execute(
            "INSERT INTO files(path, basename, scene_id, exists_on_disk, first_seen_at, last_seen_at) VALUES (?, ?, '42', 1, ?, ?)",
            (str(video), video.name, now, now)
        )
        conn.commit()
        conn.close()

        # Enqueue rename with 30s debounce
        enqueue_rename(db, "42", now, debounce_seconds=30.0)

        conn = connect(db)
        row = conn.execute("SELECT status FROM rename_queue WHERE scene_id='42'").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "pending"

        # User clicks CANCEL RENAME
        cancelled = cancel_pending_rename(db, "42")
        assert cancelled is True

        # Verify removed from rename queue and recorded in audit log
        conn = connect(db)
        row_after = conn.execute("SELECT status FROM rename_queue WHERE scene_id='42'").fetchone()
        activity = conn.execute("SELECT status, detail FROM activity_log WHERE scene_id='42' ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()

        assert row_after is None
        assert activity[0] == "cancelled"
        assert "original filename preserved on disk" in activity[1]

        # Verify nothing is claimed even after debounce period expires
        scene_id, next_at, pending = claim_due_rename(db, now + 60.0)
        assert scene_id is None
        assert pending == 0

        # Filename on disk remains untouched
        assert video.exists()
        assert video.name == "yb_17_sc04.vid-720.mp4"
