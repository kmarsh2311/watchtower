"""
Final focused tests before deployment:
A. Startup recovery uses Stash job liveness — not a time threshold
   - RUNNING job → preserved
   - FINISHED/CANCELLED job → reset to queued
   - No job_id stored → uncertain, preserved
   - Stash unreachable → uncertain, preserved
   - stash=None → uncertain, preserved
B. Queued jobs cannot start before reconciliation (recover_stale) finishes
C. End-to-end: move starts, browser "navigates away", backend recovers,
   Stash path / Watchtower records / active_filing_transfers all verified
"""
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta
import pytest


# ─── helpers ─────────────────────────────────────────────────────────────────

def _make_db(tmp_path):
    from librarymanager_core import connect
    db = tmp_path / "test.db"
    connect(db).close()
    return db


def _insert_proposal(db, pid, src, dest_filename, status="pending",
                     dest_folder="", proposed_path=""):
    from librarymanager_core import connect
    conn = connect(db)
    conn.execute("""
        INSERT OR REPLACE INTO filing_proposals
        (id, scene_id, file_id, source_path, destination_filename, proposed_path,
         destination_folder, organize_by, matched_entity_id, matched_entity_name,
         match_source, reason, status, created_at, updated_at)
        VALUES (?, '99', 'f99', ?, ?, ?, ?,
                'performer', 'p1', 'Test Performer', 'metadata', 'Test',
                ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
    """, (pid, str(src), dest_filename, proposed_path, dest_folder, status))
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


def _seed_processing_row(db, proposal_id, dest, job_id=None):
    """Enqueue then manually force to processing with optional stash_job_id."""
    from librarymanager_core import enqueue_filing_transfer, connect
    enqueue_filing_transfer(db, proposal_id, str(dest))
    conn = connect(db)
    conn.execute(
        """UPDATE filing_transfer_queue
           SET status='processing', processing_started_at=datetime('now','-5 minutes'),
               stash_job_id=?
           WHERE proposal_id=?""",
        (job_id, proposal_id),
    )
    conn.commit()
    conn.close()


def _get_queue_row(db, pid):
    from librarymanager_core import connect
    conn = connect(db)
    row = conn.execute(
        "SELECT * FROM filing_transfer_queue WHERE proposal_id=?", (pid,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ─── A. Liveness guard: Stash job status check ───────────────────────────────

class TestStashJobLivenessGuard:
    """
    recover_abandoned_processing_queue must only reset rows whose Stash job
    is confirmed dead. Every unverifiable case must be preserved and reported.
    """

    def test_running_stash_job_is_preserved(self, tmp_path):
        """Stash says RUNNING → worker is alive, row must not be reset."""
        from librarymanager_core import recover_abandoned_processing_queue
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"; src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        _seed_processing_row(db, 1, tmp_path / "Dest", job_id="job-123")

        class StashRunning:
            def call_GQL(self, q, v=None):
                return {"findJob": {"status": "RUNNING"}}

        result = recover_abandoned_processing_queue(db, stash=StashRunning())
        assert result == {"reset": 0, "uncertain": []}
        assert _get_queue_row(db, 1)["status"] == "processing", \
            "RUNNING job must leave row in processing"

    def test_ready_stash_job_is_preserved(self, tmp_path):
        """Stash says READY (queued-in-stash) → treat as alive, do not reset."""
        from librarymanager_core import recover_abandoned_processing_queue
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"; src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        _seed_processing_row(db, 1, tmp_path / "Dest", job_id="job-456")

        class StashReady:
            def call_GQL(self, q, v=None):
                return {"findJob": {"status": "READY"}}

        result = recover_abandoned_processing_queue(db, stash=StashReady())
        assert result["reset"] == 0
        assert _get_queue_row(db, 1)["status"] == "processing"

    def test_finished_stash_job_resets_row_to_queued(self, tmp_path):
        """Stash says FINISHED → worker done, safe to reset."""
        from librarymanager_core import recover_abandoned_processing_queue
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"; src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        _seed_processing_row(db, 1, tmp_path / "Dest", job_id="job-789")

        class StashFinished:
            def call_GQL(self, q, v=None):
                return {"findJob": {"status": "FINISHED"}}

        result = recover_abandoned_processing_queue(db, stash=StashFinished())
        assert result["reset"] == 1
        assert result["uncertain"] == []
        assert _get_queue_row(db, 1)["status"] == "queued"

    def test_cancelled_stash_job_resets_row_to_queued(self, tmp_path):
        """Stash says CANCELLED → reset."""
        from librarymanager_core import recover_abandoned_processing_queue
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"; src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        _seed_processing_row(db, 1, tmp_path / "Dest", job_id="job-abc")

        class StashCancelled:
            def call_GQL(self, q, v=None):
                return {"findJob": {"status": "CANCELLED"}}

        result = recover_abandoned_processing_queue(db, stash=StashCancelled())
        assert result["reset"] == 1

    def test_stash_job_not_found_resets_row(self, tmp_path):
        """Job purged from Stash history (findJob=None) → treat as dead, reset."""
        from librarymanager_core import recover_abandoned_processing_queue
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"; src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        _seed_processing_row(db, 1, tmp_path / "Dest", job_id="job-old")

        class StashJobGone:
            def call_GQL(self, q, v=None):
                return {"findJob": None}

        result = recover_abandoned_processing_queue(db, stash=StashJobGone())
        assert result["reset"] == 1
        assert _get_queue_row(db, 1)["status"] == "queued"

    def test_no_job_id_stored_is_uncertain_not_reset(self, tmp_path):
        """
        No stash_job_id on the row → cannot verify liveness.
        Must be preserved (uncertain), never auto-reset.
        """
        from librarymanager_core import recover_abandoned_processing_queue
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"; src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        _seed_processing_row(db, 1, tmp_path / "Dest", job_id=None)  # no job_id

        class StashOk:
            def call_GQL(self, q, v=None):
                return {"findJob": {"status": "FINISHED"}}

        result = recover_abandoned_processing_queue(db, stash=StashOk())
        assert result["reset"] == 0
        assert 1 in result["uncertain"]
        assert _get_queue_row(db, 1)["status"] == "processing", \
            "No job_id → uncertain → must stay in processing"

    def test_stash_gql_failure_is_uncertain_not_reset(self, tmp_path):
        """Stash GQL error → cannot verify → preserve, report as uncertain."""
        from librarymanager_core import recover_abandoned_processing_queue
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"; src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        _seed_processing_row(db, 1, tmp_path / "Dest", job_id="job-xyz")

        class StashDown:
            def call_GQL(self, q, v=None):
                raise ConnectionError("Stash offline")

        result = recover_abandoned_processing_queue(db, stash=StashDown())
        assert result["reset"] == 0
        assert 1 in result["uncertain"]
        assert _get_queue_row(db, 1)["status"] == "processing"

    def test_stash_none_is_uncertain_not_reset(self, tmp_path):
        """stash=None (no connection available) → uncertain, never reset."""
        from librarymanager_core import recover_abandoned_processing_queue
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"; src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        _seed_processing_row(db, 1, tmp_path / "Dest", job_id="job-zzz")

        result = recover_abandoned_processing_queue(db, stash=None)
        assert result["reset"] == 0
        assert 1 in result["uncertain"]
        assert _get_queue_row(db, 1)["status"] == "processing"

    def test_mixed_rows_handled_independently(self, tmp_path):
        """RUNNING row preserved, FINISHED row reset, no-job-id row uncertain."""
        from librarymanager_core import recover_abandoned_processing_queue
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"; src.write_bytes(b"x")
        dest = tmp_path / "Dest"

        _insert_proposal(db, 1, src, "v1.mp4", status="pending")
        _seed_processing_row(db, 1, dest, job_id="job-running")

        _insert_proposal(db, 2, src, "v2.mp4", status="pending")
        _seed_processing_row(db, 2, dest, job_id="job-finished")

        _insert_proposal(db, 3, src, "v3.mp4", status="pending")
        _seed_processing_row(db, 3, dest, job_id=None)   # no job_id

        job_statuses = {"job-running": "RUNNING", "job-finished": "FINISHED"}

        class StashMixed:
            def call_GQL(self, q, v=None):
                jid = (v or {}).get("id", "")
                status = job_statuses.get(jid)
                if status:
                    return {"findJob": {"status": status}}
                return {"findJob": None}

        result = recover_abandoned_processing_queue(db, stash=StashMixed())
        assert result["reset"] == 1      # only proposal 2
        assert 3 in result["uncertain"]  # proposal 3 (no job_id)
        assert 1 not in result["uncertain"]  # proposal 1 is alive, not uncertain

        assert _get_queue_row(db, 1)["status"] == "processing"  # alive
        assert _get_queue_row(db, 2)["status"] == "queued"       # dead → reset
        assert _get_queue_row(db, 3)["status"] == "processing"   # uncertain → preserved

    def test_monitor_startup_source_passes_stash_to_recover_abandoned(self):
        """Source inspection: monitor passes stash= to recover_abandoned_processing_queue."""
        import re
        mon_path = "/Users/keithmarsh/Developer/watchtower/librarymanager_monitor.py"
        with open(mon_path, "r", encoding="utf-8") as f:
            source = f.read()
        calls = list(re.finditer(r'recover_abandoned_processing_queue\(([^)]+)\)', source))
        assert calls, "Expected at least one call in monitor"
        for m in calls:
            args = m.group(1)
            assert "stash=" in args, \
                f"Monitor must pass stash= to recover_abandoned; found: ({args})"


# ─── B. Reconciliation ordering ──────────────────────────────────────────────

class TestReconciliationBeforeClaim:

    def test_process_filing_queue_calls_recovery_before_claim(self):
        """recover_stale_active_transfers appears before claim_next in source."""
        import re
        py_path = "/Users/keithmarsh/Developer/watchtower/librarymanager.py"
        with open(py_path, "r", encoding="utf-8") as f:
            source = f.read()
        block_start = source.find('elif mode == "process_filing_queue":')
        block_end   = source.find('\n    elif mode == ', block_start + 1)
        block = source[block_start:block_end]
        recover_pos = block.find("recover_stale_active_transfers")
        claim_pos   = block.find("claim_next_filing_transfer")
        assert recover_pos >= 0 and claim_pos >= 0
        assert recover_pos < claim_pos, \
            "recover_stale_active_transfers must be called BEFORE claim_next_filing_transfer"

    def test_queued_job_blocked_while_active_transfer_exists(self, tmp_path):
        """Serialisation guard blocks proposal 2 while proposal 1's active transfer is stale."""
        from librarymanager_core import (
            enqueue_filing_transfer, _update_active_transfer,
            claim_next_filing_transfer, recover_stale_active_transfers,
            apply_filing_proposal, get_active_filing_transfers, connect
        )
        db = _make_db(tmp_path)
        src1 = tmp_path / "video1.mp4"; src1.write_bytes(b"x" * 200)
        src2 = tmp_path / "video2.mp4"; src2.write_bytes(b"y" * 100)
        dest_dir = tmp_path / "Dest"; dest_dir.mkdir()
        # Simulate: proposal 1's move completed — source gone, dest present
        dest1 = dest_dir / "video1.mp4"; dest1.write_bytes(b"x" * 200)
        src1.unlink()  # move completed before crash
        dest2 = dest_dir / "video2.mp4"

        _insert_proposal(db, 1, src1, "video1.mp4", status="pending",
                         dest_folder=str(dest_dir), proposed_path=str(dest1))
        _update_active_transfer(db, 1, "99", "f99", str(src1), str(dest1),
                                str(dest_dir), "moving_video", "Transferring",
                                "...", total_bytes=200)
        conn = connect(db)
        conn.execute("UPDATE active_filing_transfers SET updated_at=datetime('now','-20 minutes') WHERE proposal_id=1")
        conn.commit(); conn.close()

        _insert_proposal(db, 2, src2, "video2.mp4", status="pending",
                         dest_folder=str(dest_dir), proposed_path=str(dest2))
        enqueue_filing_transfer(db, 2, str(dest_dir))

        # Before recovery: proposal 2 is blocked by proposal 1's stale transfer
        class NoopStash:
            def find_plugin_config(self, *a, **k): return {}
            def call_GQL(self, *a, **k): return None
            def move_files(self, *a, **k): return False

        row2 = claim_next_filing_transfer(db)
        assert row2 is not None
        res = apply_filing_proposal(db, NoopStash(), 2)
        assert res["status"] == "blocked"
        assert "serialized" in res["reason"].lower()

        # Reset queue row for retry after recovery
        conn = connect(db)
        conn.execute("UPDATE filing_transfer_queue SET status='queued', processing_started_at=NULL WHERE id=?", (row2["id"],))
        conn.execute("UPDATE filing_proposals SET status='queued' WHERE id=2")
        conn.commit(); conn.close()

        # Recovery resolves proposal 1
        class StashAtDest1:
            def call_GQL(self, q, v=None):
                return {"findFile": {"id": "f99", "path": str(dest1)}}

        recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10, stash=StashAtDest1())
        assert recovered[0]["outcome"] == "completed"
        assert get_active_filing_transfers(db) == []


# ─── C. End-to-end with a real disposable file ───────────────────────────────

class TestEndToEnd:

    def test_e2e_interrupted_move_then_recovery_completes_cleanly(self, tmp_path):
        from librarymanager_core import (
            _update_active_transfer, recover_stale_active_transfers,
            get_active_filing_transfers, connect
        )

        # 1. Create a real disposable file
        src_dir = tmp_path / "Incoming"; src_dir.mkdir()
        dest_dir = tmp_path / "Performer Collection"; dest_dir.mkdir()
        src = src_dir / "Disposable Video - Studio - Performer.mp4"
        src.write_bytes(b"FAKE_VIDEO_CONTENT_" * 100)
        file_size = src.stat().st_size
        dest = dest_dir / src.name
        file_id = "e2e_file_001"
        proposal_id = 9001

        db = _make_db(tmp_path)
        _insert_proposal(db, proposal_id, src, src.name, status="pending",
                         dest_folder=str(dest_dir), proposed_path=str(dest))

        conn = connect(db)
        conn.execute(
            """INSERT OR IGNORE INTO files
               (file_id, scene_id, path, basename, performers_json, fingerprints_json,
                scene_metadata_json, exists_on_disk, first_seen_at, last_seen_at)
               VALUES (?, '99', ?, ?, '[]', '[]', '{}', 1,
                       '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
            (file_id, str(src), src.name)
        )
        conn.commit(); conn.close()

        # 2. Transfer starts — record active row
        _update_active_transfer(db, proposal_id, "99", file_id,
                                str(src), str(dest), str(dest_dir),
                                "moving_video", "Transferring Video",
                                f"Moving to {dest_dir.name}…", total_bytes=file_size)
        assert len(get_active_filing_transfers(db)) == 1

        # 3. OS move executes (apply_filing_proposal does this normally)
        shutil.move(str(src), str(dest))
        assert not src.exists() and dest.exists() and dest.stat().st_size == file_size

        # 3b. Browser navigates away — cleanup never runs; back-date to simulate staleness
        conn = connect(db)
        conn.execute("UPDATE active_filing_transfers SET updated_at=datetime('now','-15 minutes') WHERE proposal_id=?", (proposal_id,))
        conn.commit(); conn.close()
        assert len(get_active_filing_transfers(db)) == 1
        assert _get_proposal_status(db, proposal_id) == "pending"

        # 4. Recovery: Stash confirms dest
        class StashConfirmsDest:
            def call_GQL(self, query, variables=None):
                return {"findFile": {"id": file_id, "path": str(dest)}}

        recovered = recover_stale_active_transfers(db, stale_threshold_minutes=10,
                                                   stash=StashConfirmsDest())

        # 5. Verify all three systems agree
        active_after = get_active_filing_transfers(db)
        assert len(active_after) == 0, f"Stuck transfer remains: {active_after}"

        proposal = _get_proposal(db, proposal_id)
        assert proposal["status"] == "completed", \
            f"Proposal not completed: {proposal['status']} / {proposal.get('last_error')}"
        assert proposal["proposed_path"] == str(dest)

        conn = connect(db)
        wt = conn.execute("SELECT path, exists_on_disk FROM files WHERE file_id=?",
                          (file_id,)).fetchone()
        conn.close()
        assert wt is not None and wt["path"] == str(dest)
        assert wt["exists_on_disk"] == 1

        assert len(recovered) == 1
        assert recovered[0]["outcome"] == "completed"

        # File is exactly at destination — not duplicated, not missing
        assert dest.exists() and not src.exists()
        assert dest.stat().st_size == file_size


# ─── D. Job-ID race condition tolerance ──────────────────────────────────────

class TestJobIdRaceTolerance:
    """
    set_filing_transfer_job_id is called AFTER run_plugin_task returns.
    In the narrow window between those two calls the Stash task may already
    have claimed and completed the row. Verify every outcome of that race is safe.
    """

    def test_race_task_completes_before_job_id_stored_no_harm(self, tmp_path):
        """
        Row moves queued → processing → done before set_filing_transfer_job_id runs.
        The UPDATE affects 0 rows; no data is corrupted; recovery never sees it.
        """
        from librarymanager_core import (
            enqueue_filing_transfer, claim_next_filing_transfer,
            finish_filing_transfer, set_filing_transfer_job_id, connect
        )
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"; src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        enqueue_filing_transfer(db, 1, "/dest/A")

        # Simulate: task claims and finishes before job_id is stored
        row = claim_next_filing_transfer(db)
        finish_filing_transfer(db, row["id"])
        assert _get_queue_row(db, 1)["status"] == "done"

        # Now set_filing_transfer_job_id runs (late) — UPDATE touches 0 rows
        set_filing_transfer_job_id(db, 1, "job-late-123")
        q = _get_queue_row(db, 1)
        assert q["status"] == "done", "Late job_id store must not alter row status"
        # job_id was not stored (row was 'done', not 'queued'/'processing')
        # This is safe — the task already succeeded; no recovery needed

    def test_race_recovery_runs_while_job_id_not_yet_stored(self, tmp_path):
        """
        Recovery fires while a row is 'processing' but stash_job_id is still NULL
        (set_filing_transfer_job_id hasn't run yet). Row must be preserved as
        uncertain — the running worker is never interrupted.
        """
        from librarymanager_core import (
            enqueue_filing_transfer, claim_next_filing_transfer,
            recover_abandoned_processing_queue
        )
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"; src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        enqueue_filing_transfer(db, 1, "/dest/A")
        claim_next_filing_transfer(db)  # processing, job_id still NULL

        # Recovery fires before set_filing_transfer_job_id is called
        class _StashFinished:
            def call_GQL(self, q, v=None):
                # Would answer FINISHED, but we never even reach this call
                # because job_id is NULL — uncertain path is taken instead
                return {"findJob": {"status": "FINISHED"}}

        result = recover_abandoned_processing_queue(db, stash=_StashFinished())
        # NULL job_id → uncertain → preserved, not reset
        assert result["reset"] == 0
        assert 1 in result["uncertain"]
        assert _get_queue_row(db, 1)["status"] == "processing", \
            "Worker must not be interrupted during the job-id race window"

    def test_race_job_id_stored_after_recovery_preserves_row_correctly(self, tmp_path):
        """
        Full race sequence: recovery runs (uncertain, row preserved), then
        set_filing_transfer_job_id stores the job_id, then worker finishes normally.
        Final state must be 'done' — no corruption.
        """
        from librarymanager_core import (
            enqueue_filing_transfer, claim_next_filing_transfer,
            recover_abandoned_processing_queue, set_filing_transfer_job_id,
            finish_filing_transfer
        )
        db = _make_db(tmp_path)
        src = tmp_path / "video.mp4"; src.write_bytes(b"x")
        _insert_proposal(db, 1, src, "v.mp4", status="pending")
        enqueue_filing_transfer(db, 1, "/dest/A")

        # Step 1: Worker claims row (job_id not yet stored)
        row = claim_next_filing_transfer(db)
        assert _get_queue_row(db, 1)["status"] == "processing"

        # Step 2: Recovery fires — uncertain, row preserved
        class AnyStash:
            def call_GQL(self, q, v=None): return {"findJob": {"status": "FINISHED"}}

        result = recover_abandoned_processing_queue(db, stash=AnyStash())
        assert result["reset"] == 0          # preserved
        assert 1 in result["uncertain"]
        assert _get_queue_row(db, 1)["status"] == "processing"  # still running

        # Step 3: job_id stored (late, but row still processing)
        set_filing_transfer_job_id(db, 1, "job-race-456")
        assert _get_queue_row(db, 1)["stash_job_id"] == "job-race-456"

        # Step 4: Worker finishes normally
        finish_filing_transfer(db, row["id"])
        assert _get_queue_row(db, 1)["status"] == "done"
