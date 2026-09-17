"""
Regression tests for the companion file pairing lifecycle.

Covers all 7 scenarios identified in the lifecycle analysis:
  S1. Companion arrives before video
  S2. Video arrives before companion
  S3. Companion arrives during video scanning
  S4. Video arrives several minutes after companion
  S5. Watchtower restarts while a companion is waiting
  S6. Watchdog misses companion event; fallback recovery required
  S7. Companion remains unmatched indefinitely — no repeated DB writes

Tests use isolated tmp directories and in-memory SQLite via the real schema.
Production files and media library are NOT touched.
"""
import sqlite3
import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

WATCHTOWER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(WATCHTOWER_DIR))

from librarymanager_monitor import (
    CompletedDownloadWorker,
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
    """Add a companion to self.candidates, optionally already settled."""
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
        }


def _inject_video(worker, path, settled=True):
    """Add a video to self.candidates, optionally already settled."""
    p = Path(path)
    stat = p.stat()
    settle_secs = worker.settle_seconds
    with worker.lock:
        worker.candidates[str(p.resolve())] = {
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "stable_since": time.time() - (settle_secs + 10 if settled else 9999),
            "attempts": 0,
            "is_companion": False,
            "is_temporary": False,
        }


def _db_status(db, path):
    """Return the current status of a path in incoming_files."""
    con = connect(db)
    try:
        row = con.execute("SELECT status FROM incoming_files WHERE path=?", (path,)).fetchone()
        return row["status"] if row else None
    finally:
        con.close()


def _db_checked_at(db, path):
    """Return last_checked_at for a path."""
    con = connect(db)
    try:
        row = con.execute("SELECT last_checked_at FROM incoming_files WHERE path=?", (path,)).fetchone()
        return row["last_checked_at"] if row else None
    finally:
        con.close()


def _fake_scene(video_path, scene_id="42"):
    return {
        "id": scene_id,
        "files": [{"path": str(video_path)}],
        "performers": [], "tags": [], "studio": None,
        "title": None, "details": None, "date": None,
        "director": None, "code": None, "rating100": None,
        "organized": False, "urls": [], "galleries": [],
        "stash_ids": [], "groups": [],
    }


# ===========================================================================
# S1. Companion arrives BEFORE video
# ===========================================================================

class TestScenario1CompanionBeforeVideo:

    def test_s1_companion_found_in_candidates_when_video_arrives(self, tmp_path):
        """
        Companion is in self.candidates when the video finally arrives.
        _pair_companions_for_video must be able to find and pair it.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        video = incoming / "Scene.mp4"
        companion = incoming / "Scene.jpg"
        video.write_bytes(b"v" * 1000)
        companion.write_bytes(b"i")

        # Companion arrives first, settles, is processed (no match yet), stays in candidates
        _inject_companion(worker, companion, settled=True)
        worker._process_companion(str(companion.resolve()), worker.candidates[str(companion.resolve())])

        # Companion must still be in candidates for pairing to work
        assert str(companion.resolve()) in worker.candidates, (
            "Companion must remain in candidates after no-match to enable later pairing"
        )

        # Video arrives; scan completes; _pair_companions_for_video runs
        worker.stash.metadata_scan.return_value = "job-1"
        worker._pair_companions_for_video(str(video.resolve()), _fake_scene(video))

        assert str(companion.resolve()) not in worker.candidates, (
            "Companion must be removed from candidates after successful pairing"
        )
        assert _db_status(db, str(companion.resolve())) == "paired", (
            "DB status must be 'paired' after pairing"
        )

    def test_s1_companion_not_paired_twice(self, tmp_path):
        """
        After pairing, companion must NOT be in candidates and must not be
        reprocessed on the next evaluate_once cycle.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        video = incoming / "Movie.mp4"
        companion = incoming / "Movie.jpg"
        video.write_bytes(b"v" * 1000)
        companion.write_bytes(b"i")

        _inject_companion(worker, companion, settled=True)
        worker.stash.metadata_scan.return_value = "job-2"
        worker._pair_companions_for_video(str(video.resolve()), _fake_scene(video))

        assert str(companion.resolve()) not in worker.candidates

        # evaluate_once must not re-add or reprocess the companion
        worker.evaluate_once()
        assert str(companion.resolve()) not in worker.candidates, (
            "Paired companion must not reappear in candidates after evaluate_once"
        )


# ===========================================================================
# S2. Video arrives BEFORE companion
# ===========================================================================

class TestScenario2VideoBeforeCompanion:

    def test_s2_companion_pairs_via_db_lookup_when_video_already_scanned(self, tmp_path):
        """
        Video already imported (in `files` table). Companion arrives after.
        _process_companion must find the scene via find_scene_for_companion.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        video = incoming / "Scene.mp4"
        companion = incoming / "Scene.jpg"
        video.write_bytes(b"v" * 1000)
        companion.write_bytes(b"i")

        # Insert video into `files` table (simulating a completed scan)
        con = connect(db)
        try:
            con.execute(
                """INSERT INTO files(file_id,scene_id,path,basename,size,fingerprints_json,
                   exists_on_disk,first_seen_at,last_seen_at)
                   VALUES (?,?,?,?,?,?,1,datetime('now'),datetime('now'))""",
                ("file-1", "scene-1", str(video.resolve()), video.name, 1000, "[]"),
            )
            con.commit()
        finally:
            con.close()

        # Companion arrives after video is already in DB
        _inject_companion(worker, companion, settled=True)
        worker.stash.metadata_scan.return_value = "job-3"
        worker._process_companion(str(companion.resolve()), worker.candidates[str(companion.resolve())])

        # Should be paired via DB lookup (find_scene_for_companion found the row)
        assert str(companion.resolve()) not in worker.candidates, (
            "Companion should be removed from candidates after pairing via DB lookup"
        )
        final_status = _db_status(db, str(companion.resolve()))
        assert final_status == "paired", (
            f"Expected status 'paired' after DB-lookup pairing, got: {final_status}"
        )

    def test_s2_companion_not_matched_if_no_video_in_db(self, tmp_path):
        """
        If video is not yet in DB and not in candidates, companion must remain
        in waiting state (not paired, not gone).
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        companion = incoming / "Scene.jpg"
        companion.write_bytes(b"i")

        _inject_companion(worker, companion, settled=True)
        worker._process_companion(str(companion.resolve()), worker.candidates[str(companion.resolve())])

        assert str(companion.resolve()) in worker.candidates, "Must remain in candidates when not matched"
        assert _db_status(db, str(companion.resolve())) == "waiting"


# ===========================================================================
# S3. Companion arrives DURING video scan
# ===========================================================================

class TestScenario3CompanionDuringVideoScan:

    def test_s3_companion_submitted_during_scan_is_paired_by_pair_companions(self, tmp_path):
        """
        Video popped from candidates (as evaluate_once does before _scan).
        Companion arrives and is submitted. _pair_companions_for_video must
        find the companion in self.candidates and pair it.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        video = incoming / "Scene.mp4"
        companion = incoming / "Scene.jpg"
        video.write_bytes(b"v" * 1000)
        companion.write_bytes(b"i")

        # Video popped (as evaluate_once does before _scan)
        with worker.lock:
            worker.candidates.pop(str(video.resolve()), None)

        # Companion arrives DURING scan window
        worker.submit(str(companion))
        assert str(companion.resolve()) in worker.candidates, "Companion must be in candidates"

        # Scan completes; _pair_companions_for_video runs
        worker.stash.metadata_scan.return_value = "job-s3"
        worker._pair_companions_for_video(str(video.resolve()), _fake_scene(video))

        assert str(companion.resolve()) not in worker.candidates, (
            "Companion paired during scan window; must be removed from candidates"
        )

    def test_s3_companion_settled_before_scan_completes_is_still_paired(self, tmp_path):
        """
        Narrow race: _process_companion ran during scan (saved 'waiting') but companion
        stayed in candidates. _pair_companions_for_video must still pair it.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        video = incoming / "Scene.mp4"
        companion = incoming / "Scene.jpg"
        video.write_bytes(b"v" * 1000)
        companion.write_bytes(b"i")

        _inject_companion(worker, companion, settled=True)
        # _process_companion runs; no video in candidates (video popped); saves 'waiting'; stays
        worker._process_companion(str(companion.resolve()), worker.candidates[str(companion.resolve())])
        assert str(companion.resolve()) in worker.candidates

        # Scan completes
        worker.stash.metadata_scan.return_value = "job-s3b"
        worker._pair_companions_for_video(str(video.resolve()), _fake_scene(video))

        assert str(companion.resolve()) not in worker.candidates, (
            "Companion paired by _pair_companions_for_video even after _process_companion ran"
        )


# ===========================================================================
# S4. Video arrives SEVERAL MINUTES after companion
# ===========================================================================

class TestScenario4VideoArrivesLate:

    def test_s4_companion_still_in_candidates_after_long_wait(self, tmp_path):
        """
        Key invariant: companion must remain in self.candidates however long
        the wait is, so _pair_companions_for_video can find it.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        companion = incoming / "Scene.jpg"
        companion.write_bytes(b"i")

        _inject_companion(worker, companion, settled=True)

        # Simulate many evaluate_once cycles (companion never paired)
        for _ in range(10):
            worker.evaluate_once()

        assert str(companion.resolve()) in worker.candidates, (
            "REGRESSION: companion must remain in candidates throughout the entire wait "
            "so that _pair_companions_for_video can find it when the video eventually arrives"
        )

    def test_s4_companion_paired_when_video_arrives_after_many_cycles(self, tmp_path):
        """
        After N evaluate_once cycles with no match, video finally arrives.
        Companion must be paired.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        video = incoming / "LateArrival.mp4"
        companion = incoming / "LateArrival.jpg"
        video.write_bytes(b"v" * 1000)
        companion.write_bytes(b"i")

        _inject_companion(worker, companion, settled=True)

        # N cycles with no match
        for _ in range(5):
            worker.evaluate_once()

        assert str(companion.resolve()) in worker.candidates, "Companion must still be present"

        # Video arrives and scan completes
        worker.stash.metadata_scan.return_value = "job-s4"
        worker._pair_companions_for_video(str(video.resolve()), _fake_scene(video, "scene-late"))

        assert str(companion.resolve()) not in worker.candidates, (
            "Companion must be paired by _pair_companions_for_video after long wait"
        )


# ===========================================================================
# S5. Watchtower restart while companion is waiting
# ===========================================================================

class TestScenario5RestartWithWaitingCompanion:

    def test_s5_restore_candidates_reloads_waiting_companion(self, tmp_path):
        """
        After a restart, _restore_candidates() must reload companions
        with status='waiting' from incoming_files.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)

        companion = incoming / "Scene.jpg"
        companion.write_bytes(b"i")
        companion_str = str(companion.resolve())

        # Pre-populate incoming_files as if companion was waiting before restart
        con = connect(db)
        try:
            stat = companion.stat()
            con.execute(
                """INSERT INTO incoming_files(path,first_seen_at,last_checked_at,size,modified_ns,
                   stable_since,settle_seconds,status,attempts,detail)
                   VALUES (?,datetime('now'),datetime('now'),?,?,?,300,'waiting',0,'Waiting for video')""",
                (companion_str, stat.st_size, stat.st_mtime_ns, time.time() - 600),
            )
            con.commit()
        finally:
            con.close()

        # Simulate fresh worker start (what happens after monitor restart)
        worker = _make_worker(db, incoming)

        # _restore_candidates is called in __init__ -> companion should be in candidates
        assert companion_str in worker.candidates, (
            "After restart, _restore_candidates must reload waiting companion into candidates"
        )

    def test_s5_restarted_companion_still_pairs_when_video_arrives(self, tmp_path):
        """
        After restart and restore, companion must be pairable when video arrives.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)

        video = incoming / "Scene.mp4"
        companion = incoming / "Scene.jpg"
        video.write_bytes(b"v" * 1000)
        companion.write_bytes(b"i")
        companion_str = str(companion.resolve())

        # Pre-populate as waiting
        con = connect(db)
        try:
            stat = companion.stat()
            con.execute(
                """INSERT INTO incoming_files(path,first_seen_at,last_checked_at,size,modified_ns,
                   stable_since,settle_seconds,status,attempts,detail)
                   VALUES (?,datetime('now'),datetime('now'),?,?,?,300,'waiting',0,'Waiting')""",
                (companion_str, stat.st_size, stat.st_mtime_ns, time.time() - 600),
            )
            con.commit()
        finally:
            con.close()

        worker = _make_worker(db, incoming)
        assert companion_str in worker.candidates, "Companion must be restored"

        # Video arrives and scan completes
        worker.stash.metadata_scan.return_value = "job-s5"
        worker._pair_companions_for_video(str(video.resolve()), _fake_scene(video))

        assert companion_str not in worker.candidates, (
            "After restart+restore, companion must be paired by _pair_companions_for_video"
        )


# ===========================================================================
# S6. Watchdog misses companion creation event; fallback recovery
# ===========================================================================

class TestScenario6FallbackRecovery:

    def test_s6_fallback_rediscovers_missed_companion(self, tmp_path):
        """
        Watchdog missed on_created for a companion. _fallback_check must
        submit it because: not in candidates, not in inventory, not imported.
        This requires _fallback_check to scan companion files (current behaviour).
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)
        worker.started_at = time.time() - 10  # file was created "during this run"

        companion = incoming / "Missed.jpg"
        companion.write_bytes(b"i")
        companion_str = str(companion.resolve())

        # Companion on disk, not in candidates (watchdog missed it)
        assert companion_str not in worker.candidates

        worker._fallback_check()

        assert companion_str in worker.candidates, (
            "REGRESSION: _fallback_check must rediscover companion missed by watchdog. "
            "This requires _current_video_paths() to return companion files. "
            "Fix 2 (filter fallback to videos only) MUST NOT be applied."
        )

    def test_s6_fallback_does_not_resubmit_companion_already_in_candidates(self, tmp_path):
        """
        Companion already in candidates. _fallback_check must skip it
        (the 'pending' guard at L1225-1226 handles this).
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)
        worker.started_at = time.time() - 10

        companion = incoming / "Present.jpg"
        companion.write_bytes(b"i")
        companion_str = str(companion.resolve())

        _inject_companion(worker, companion, settled=False)  # unsettled
        original_candidate = worker.candidates[companion_str].copy()

        worker._fallback_check()

        # Candidate must not be replaced (stable_since would reset)
        assert companion_str in worker.candidates
        assert worker.candidates[companion_str]["stable_since"] == original_candidate["stable_since"], (
            "_fallback_check must not replace an existing candidate (would reset stable_since)"
        )

    def test_s6_fallback_rediscovers_missed_video(self, tmp_path):
        """
        Watchdog missed on_created for a video. _fallback_check must submit it.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)
        worker.started_at = time.time() - 10

        video = incoming / "Missed.mp4"
        video.write_bytes(b"v" * 1000)
        video_str = str(video.resolve())

        assert video_str not in worker.candidates
        worker._fallback_check()
        assert video_str in worker.candidates, (
            "_fallback_check must rediscover videos missed by watchdog"
        )


# ===========================================================================
# S7. Companion unmatched indefinitely — no repeated DB writes
# ===========================================================================

class TestScenario7UnmatchedCompanionNoFlashing:
    """
    THIS IS THE BUG. In current code, evaluate_once calls _process_companion
    every 5s for an unmatched companion, causing a DB write every 5s.

    The correct behaviour: companion stays in candidates (for future pairing),
    but _process_companion is NOT called repeatedly. DB writes should be
    throttled (one on first check, then only when something changes).

    These tests define the EXPECTED CORRECT BEHAVIOUR. They will FAIL
    against current code, demonstrating the bug, and PASS after the fix.
    """

    def test_s7_current_bug_evaluate_once_rewrites_db_every_cycle(self, tmp_path):
        """
        CONFIRMS THE BUG: After the first _process_companion call finds no match,
        subsequent evaluate_once calls must NOT rewrite the DB on every cycle.

        Current behaviour: FAILS (DB is rewritten every cycle).
        After fix: PASSES (DB written once, then throttled).
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        companion = incoming / "Orphan.jpg"
        companion.write_bytes(b"i")
        companion_str = str(companion.resolve())

        _inject_companion(worker, companion, settled=True)

        # First evaluate_once: _process_companion runs, saves 'waiting'
        worker.evaluate_once()
        first_checked_at = _db_checked_at(db, companion_str)
        assert first_checked_at is not None, "First cycle must write to DB"

        # Sleep >1s so timestamps WILL differ if DB is rewritten (utc_now has second precision)
        time.sleep(1.1)

        # Second evaluate_once: should NOT rewrite if fix is applied
        worker.evaluate_once()
        second_checked_at = _db_checked_at(db, companion_str)

        # After fix: second_checked_at == first_checked_at (no rewrite)
        # Before fix: second_checked_at != first_checked_at (DB rewritten = BUG)
        assert second_checked_at == first_checked_at, (
            "BUG CONFIRMED: evaluate_once rewrote the DB on the second cycle for an "
            "unmatched companion. This causes the 0:00/0:01 UI flashing.\n"
            f"First  last_checked_at: {first_checked_at}\n"
            f"Second last_checked_at: {second_checked_at}\n"
            "FIX NEEDED: throttle _process_companion calls using a 'check_after' "
            "timestamp in the candidate dict."
        )

    def test_s7_companion_stays_in_candidates_for_future_pairing(self, tmp_path):
        """
        Even after N evaluate_once cycles find no match, the companion must
        remain in self.candidates for _pair_companions_for_video to use.
        This must hold both before AND after any fix.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        companion = incoming / "LongWait.jpg"
        companion.write_bytes(b"i")
        companion_str = str(companion.resolve())

        _inject_companion(worker, companion, settled=True)

        for _ in range(10):
            worker.evaluate_once()

        assert companion_str in worker.candidates, (
            "REGRESSION: companion must remain in candidates throughout the wait "
            "so _pair_companions_for_video can find it when the video eventually arrives"
        )

    def test_s7_video_submission_triggers_companion_recheck(self, tmp_path):
        """
        When a new video is submitted, waiting companions must be re-evaluated
        promptly (check_after reset), NOT left waiting for their 5-minute timeout.

        This tests the PROPOSED FIX behaviour (check_after reset on video submit).
        After fix: evaluate_once immediately calls _process_companion for companion.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        video = incoming / "Scene.mp4"
        companion = incoming / "Scene.jpg"
        video.write_bytes(b"v" * 1000)
        companion.write_bytes(b"i")
        companion_str = str(companion.resolve())

        _inject_companion(worker, companion, settled=True)

        # Simulate companion already processed (no match): set check_after far in the future
        with worker.lock:
            worker.candidates[companion_str]["check_after"] = time.time() + 9999

        # Video arrives
        worker.submit(str(video))

        # After fix: check_after should be reset to 0 (or near-zero)
        with worker.lock:
            check_after = worker.candidates.get(companion_str, {}).get("check_after", None)

        if check_after is None:
            # check_after not implemented yet — this is the pre-fix state
            pytest.skip("check_after not yet implemented; run after fix is applied")
        elif check_after > time.time() + 10:
            pytest.fail(
                f"BUG: After video submitted, companion check_after={check_after:.0f} is still "
                f"far in the future. Companion will not re-check for ~{(check_after-time.time())/60:.0f} minutes. "
                "FIX: reset check_after to 0 when a new video is submitted."
            )
        else:
            pass  # check_after is near-zero: companion will re-check promptly


# ===========================================================================
# Interaction tests: Fix 1-proposed vs Fix 2-proposed regression check
# ===========================================================================

class TestProposedFixRegression:

    def test_fix2_would_break_missed_companion_fallback_recovery(self, tmp_path):
        """
        This test PROVES that Fix 2 (filter _fallback_check to VIDEO_EXTENSIONS only)
        MUST NOT be applied.

        Scenario: companion missed by watchdog. With Fix 2 applied, _fallback_check
        returns early for companion files -> companion is never rediscovered.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)
        worker.started_at = time.time() - 10

        companion = incoming / "Missed.jpg"
        companion.write_bytes(b"i")
        companion_str = str(companion.resolve())

        # Apply Fix 2 test setup: filter scannable extensions to videos only
        import librarymanager_monitor as mod
        worker._scannable_extensions = lambda: VIDEO_EXTENSIONS

        # Companion is on disk, not in candidates, not in inventory
        assert companion_str not in worker.candidates
        worker._fallback_check()

        # With Fix 2: companion NOT discovered (REGRESSION)
        with_fix2 = companion_str not in worker.candidates
        assert with_fix2, (
            "Fix 2 test setup: companion not submitted when video-only filter applied"
        )

        # Restore and verify without Fix 2 it works
        del worker._scannable_extensions  # remove monkey-patch
        worker._fallback_check()
        without_fix2 = companion_str in worker.candidates
        assert without_fix2, "Without Fix 2: companion IS discovered by fallback"

        # Document the regression
        assert with_fix2 and without_fix2, (
            "REGRESSION CONFIRMED: Fix 2 breaks watchdog-missed companion recovery. "
            "Fix 2 must NOT be applied."
        )

    def test_fix1_original_breaks_fallback_pairing_chain(self, tmp_path):
        """
        This test PROVES that Fix 1-original (pop companion on no-match) combined
        with Fix 2 causes a companion to be permanently lost.

        Chain: pop -> fallback can't find (Fix 2) -> companion never re-enters candidates
        -> _pair_companions_for_video can never find it -> NOT paired.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)
        worker.started_at = time.time() - 10

        video = incoming / "Scene.mp4"
        companion = incoming / "Scene.jpg"
        video.write_bytes(b"v" * 1000)
        companion.write_bytes(b"i")
        companion_str = str(companion.resolve())

        # Inject companion into candidates
        _inject_companion(worker, companion, settled=True)

        # Apply Fix 1-original: pop companion on no-match
        # (simulate what Fix 1-original does)
        worker._process_companion(companion_str, worker.candidates[companion_str])
        # In current code, companion stays. With Fix 1-original it would be popped:
        with worker.lock:
            worker.candidates.pop(companion_str, None)  # Fix 1-original effect

        # Apply Fix 2: filter fallback to videos only
        import librarymanager_monitor as mod
        worker._scannable_extensions = lambda: VIDEO_EXTENSIONS

        # Fallback runs but CANNOT rediscover companion (Fix 2)
        worker._fallback_check()
        assert companion_str not in worker.candidates, "Fix 2 blocks fallback rediscovery"

        # Video arrives and scan completes
        worker.stash.metadata_scan.return_value = "job-fix1fix2"
        worker._pair_companions_for_video(str(video.resolve()), _fake_scene(video))

        # Companion is NOT paired (it was never in candidates)
        final_status = _db_status(db, companion_str)
        assert final_status != "paired", (
            "REGRESSION: With Fix 1-original + Fix 2, companion is permanently lost "
            "and never paired. This proves both fixes together are NOT safe."
        )


    def test_selective_wake_up_only_matching_companions(self, tmp_path):
        """
        Requirement 5: Avoid waking every waiting companion unnecessarily.
        When VideoA arrives, only SceneA.jpg wakes up; SceneB.jpg remains sleeping.
        """
        db = tmp_path / 'db.sqlite3'
        incoming = tmp_path / 'incoming'
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        comp_a = incoming / 'SceneA.jpg'
        comp_b = incoming / 'SceneB.jpg'
        comp_a.write_bytes(b'a')
        comp_b.write_bytes(b'b')
        comp_a_str = str(comp_a.resolve())
        comp_b_str = str(comp_b.resolve())

        worker.submit(str(comp_a))
        worker.submit(str(comp_b))

        worker.candidates[comp_a_str]['stable_since'] = time.time() - 10
        worker.candidates[comp_b_str]['stable_since'] = time.time() - 10

        worker.evaluate_once()

        with worker.lock:
            assert worker.candidates[comp_a_str]['check_after'] > time.monotonic() + 100
            assert worker.candidates[comp_b_str]['check_after'] > time.monotonic() + 100
            b_deadline = worker.candidates[comp_b_str]['check_after']

        video_a = incoming / 'SceneA.mp4'
        video_a.write_bytes(b'video')
        worker.submit(str(video_a))

        with worker.lock:
            assert worker.candidates[comp_a_str]['check_after'] == 0.0, 'SceneA.jpg should be woken up'
            assert worker.candidates[comp_b_str]['check_after'] == b_deadline, 'SceneB.jpg should NOT be woken up'

    def test_database_write_failure_does_not_suppress_future_writes(self, tmp_path):
        """
        Safety check: If _save_state raises an exception (e.g. database locked):
        1. The worker thread does not crash and remains alive.
        2. The failed write does not mark the state as saved, and schedules a short 5s retry.
        3. When the lock clears, the retry persists 'waiting' to SQLite.
        4. When the matching video arrives, companion pairing completes successfully.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        companion = incoming / "Scene.jpg"
        companion.write_bytes(b"image")
        companion_str = str(companion.resolve())

        video = incoming / "Scene.mp4"
        video.write_bytes(b"video" * 100)
        video_str = str(video.resolve())

        _inject_companion(worker, companion, settled=True)

        original_save_state = worker._save_state
        lock_attempts = [0]

        def flaky_save_state(*args, **kwargs):
            if lock_attempts[0] < 1:
                lock_attempts[0] += 1
                raise sqlite3.OperationalError("database is locked")
            return original_save_state(*args, **kwargs)

        worker._save_state = flaky_save_state

        worker.start()
        try:
            worker.wake.set()
            time.sleep(0.3)

            # 1. Verify worker thread remained alive despite sqlite3.OperationalError
            assert worker.is_alive(), "Worker thread must NOT crash on database OperationalError"

            # Verify in-memory state: candidate not prematurely marked saved, short retry scheduled
            with worker.lock:
                cand = worker.candidates[companion_str]
                assert cand.get("last_saved_status") != "waiting", (
                    "Failed write must NOT set last_saved_status"
                )
                assert cand.get("check_after", 0.0) <= time.monotonic() + 10.0, (
                    "Failed write must schedule short 5s retry, not 300s"
                )
                # Reset check_after to 0 so the next cycle retries immediately
                cand["check_after"] = 0.0

            # 2. Trigger second evaluate cycle (DB lock cleared)
            worker.wake.set()
            time.sleep(0.3)

            # 3. Verify state was persisted to database
            assert _db_status(db, companion_str) == "waiting", (
                "Failed write must be retried and persisted once DB is accessible"
            )
            with worker.lock:
                cand = worker.candidates[companion_str]
                assert cand.get("last_saved_status") == "waiting"
                assert cand.get("check_after", 0.0) > time.monotonic() + 100.0, (
                    "Successful write must set full throttle interval"
                )

            # 4. Now submit matching video and complete scan
            worker.stash.metadata_scan.return_value = "job-retry"
            worker._pair_companions_for_video(video_str, _fake_scene(video))

            # Verify companion pairing continues working normally
            assert _db_status(db, companion_str) == "paired", (
                "Companion pairing must succeed after database recovery"
            )
            assert companion_str not in worker.candidates
        finally:
            worker.stop()
            worker.join(timeout=2.0)
