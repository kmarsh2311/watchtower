"""
Verification tests for the 12-point Watchtower audit.

Tests are READ-ONLY with respect to the production database and media library.
They use isolated tmp directories and in-memory / temp SQLite databases only.
"""
import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

WATCHTOWER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(WATCHTOWER_DIR))

from librarymanager_monitor import (
    CompletedDownloadWorker,
    VIDEO_EXTENSIONS,
    COMPANION_EXTENSIONS,
    find_scene_for_companion,
    LibraryEventHandler,
)
# Use the real connect() so _ensure_schema runs against real schema
from librarymanager_core import connect, SCHEMA


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _make_db(path):
    """Create a full Watchtower SQLite schema using the real SCHEMA constant."""
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


# ==========================================================================
# FINDING 3 — Orphan companion stuck in 'waiting' forever
# ==========================================================================

class TestFinding3OrphanCompanionLoop:

    def test_orphan_companion_remains_in_candidates_after_process_companion(self, tmp_path):
        """
        CONFIRMED: _process_companion's no-match branch (L1187) saves 'waiting'
        and returns WITHOUT removing the entry from self.candidates.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        jpg = incoming / "SomeScene.jpg"
        jpg.write_bytes(b"fake-image")
        stat = jpg.stat()
        path_str = str(jpg.resolve())

        # Inject directly into candidates with settle already elapsed
        worker.candidates[path_str] = {
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "stable_since": time.time() - 10,
            "attempts": 0,
            "is_companion": True,
            "is_temporary": False,
        }

        assert path_str in worker.candidates, "Precondition"
        worker._process_companion(path_str, worker.candidates[path_str])

        # The bug: companion NOT removed on no-match branch
        assert path_str in worker.candidates, (
            "CONFIRMED: orphan companion remains in self.candidates after "
            "_process_companion finds no match — infinite re-evaluation loop"
        )

    def test_orphan_companion_evaluate_once_repeats_indefinitely(self, tmp_path):
        """
        CONFIRMED: After 5 evaluate_once() calls, the companion is still in
        candidates with status 'waiting'.  The loop is infinite.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        jpg = incoming / "Orphan.jpg"
        jpg.write_bytes(b"x")
        stat = jpg.stat()
        path_str = str(jpg.resolve())

        worker.candidates[path_str] = {
            "size": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "stable_since": time.time() - 10,
            "attempts": 0,
            "is_companion": True,
            "is_temporary": False,
        }

        for _ in range(5):
            worker.evaluate_once()

        con = connect(db)
        try:
            row = con.execute(
                "SELECT status FROM incoming_files WHERE path=?", (path_str,)
            ).fetchone()
        finally:
            con.close()

        assert row is not None
        assert row["status"] == "waiting"
        assert path_str in worker.candidates, (
            "CONFIRMED: candidate persists after 5 evaluate_once() calls — "
            "the loop is infinite"
        )


# ==========================================================================
# FINDING 4 — _fallback_check resubmits companion files
# ==========================================================================

class TestFinding4FallbackResubmitsCompanions:

    def test_current_video_paths_returns_companion_files(self, tmp_path):
        """
        CONFIRMED: _current_video_paths() (alias for _current_incoming_paths())
        returns .jpg / .nfo / .srt companion files, not only videos.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        (incoming / "Movie.mp4").write_bytes(b"v")
        (incoming / "Movie.jpg").write_bytes(b"i")
        (incoming / "Movie.nfo").write_bytes(b"n")
        (incoming / "Movie.srt").write_bytes(b"s")

        paths = worker._current_video_paths()
        companion_paths = {p for p in paths if Path(p).suffix.lower() in COMPANION_EXTENSIONS}
        video_paths    = {p for p in paths if Path(p).suffix.lower() in VIDEO_EXTENSIONS}

        assert len(video_paths) == 1
        assert len(companion_paths) == 3, (
            f"CONFIRMED: _current_video_paths returned {len(companion_paths)} "
            "companion files — method name is misleading; feeds bug in finding 3"
        )

    def test_fallback_check_submits_orphan_companion(self, tmp_path):
        """
        CONFIRMED: _fallback_check() submits an orphan .jpg that has no matching
        video into self.candidates.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)
        worker.started_at = time.time() - 10  # file was created "during this run"

        jpg = incoming / "Orphan.jpg"
        jpg.write_bytes(b"x")
        resolved = str(jpg.resolve())

        assert resolved not in worker.candidates, "Precondition"
        worker._fallback_check()
        assert resolved in worker.candidates, (
            "CONFIRMED: _fallback_check submitted an orphan companion into candidates"
        )


# ==========================================================================
# FINDING 5 — find_scene_for_companion full table scan
# ==========================================================================

class TestFinding5FullTableScan:

    def test_full_scan_query_used_for_non_compound_companion(self, tmp_path):
        """
        CONFIRMED: For a non-compound companion (e.g. Scene.jpg, not Scene.mp4.jpg),
        the code reaches the 'weak stem matching' branch which issues a SELECT
        with no path/directory filter.
        """
        db = tmp_path / "db.sqlite3"
        _make_db(db)
        executed_queries = []

        class TracingConn:
            def __init__(self, real):
                self._real = real
            def execute(self, sql, params=()):
                executed_queries.append(sql.strip())
                return self._real.execute(sql, params)
            def close(self):
                self._real.close()

        real_con = sqlite3.connect(str(db))
        real_con.row_factory = sqlite3.Row
        tracing = TracingConn(real_con)

        incoming = tmp_path / "incoming"
        incoming.mkdir()
        companion = incoming / "Scene.jpg"   # non-compound name

        find_scene_for_companion(tracing, companion)
        real_con.close()

        # The full-table query has no 'basename' equality filter
        full_scan_queries = [
            q for q in executed_queries
            if "exists_on_disk=1" in q and "basename = ?" not in q
        ]
        assert len(full_scan_queries) >= 1, (
            f"CONFIRMED: full-table scan issued for non-compound companion.\n"
            f"All queries: {executed_queries}\n"
            f"Full-scan queries: {full_scan_queries}"
        )

    def test_benchmark_find_scene_for_companion(self, tmp_path):
        """
        Benchmark find_scene_for_companion at 100 / 1000 / 5000 rows.
        Determines whether the full table scan is a realistic performance concern.
        """
        results = {}
        for n_rows in (100, 1000, 5000):
            db = tmp_path / f"db_{n_rows}.sqlite3"
            _make_db(db)
            con_insert = sqlite3.connect(str(db))
            # Real schema uses TEXT primary key and NOT NULL fields
            con_insert.executemany(
                """INSERT INTO files(file_id,scene_id,path,basename,size,
                   fingerprints_json,exists_on_disk,first_seen_at,last_seen_at)
                   VALUES (?,?,?,?,?,?,1,datetime('now'),datetime('now'))""",
                [(str(i), str(i),
                  f"/vault/scene_{i:05d}/scene_{i:05d}.mp4",
                  f"scene_{i:05d}.mp4",
                  1000000, "[]")
                 for i in range(n_rows)]
            )
            con_insert.commit()
            con_insert.close()

            incoming = tmp_path / f"inc_{n_rows}"
            incoming.mkdir()
            companion = incoming / "SomeScene.jpg"

            q_con = sqlite3.connect(str(db))
            q_con.row_factory = sqlite3.Row

            t0 = time.perf_counter()
            for _ in range(10):
                find_scene_for_companion(q_con, companion)
            elapsed = (time.perf_counter() - t0) / 10
            results[n_rows] = elapsed
            q_con.close()

        print("\n=== Finding 5 Benchmark (mean per call) ===")
        for n, t in results.items():
            print(f"  {n:>6} rows: {t*1000:.2f} ms")

        # Classify: is this a real risk at typical library sizes?
        # 5000+ rows in the files table is a large-but-plausible library
        at_5k = results[5000]
        if at_5k < 0.020:
            print(f"  At 5000 rows: {at_5k*1000:.2f}ms — LOW risk on local storage")
        elif at_5k < 0.100:
            print(f"  At 5000 rows: {at_5k*1000:.2f}ms — MODERATE risk if many stuck companions")
        else:
            print(f"  At 5000 rows: {at_5k*1000:.2f}ms — HIGH risk")

        # Soft assertion — local SQLite is very fast
        assert at_5k < 0.500, f"Over 500ms at 5k rows is unexpectedly slow: {at_5k*1000:.1f}ms"


# ==========================================================================
# FINDING 7 — Companion race during scan
# ==========================================================================

class TestFinding7CompanionRaceDuringScan:

    def test_companion_submitted_after_video_pop_is_paired_correctly(self, tmp_path):
        """
        FALSE POSITIVE (confirmed): A companion submitted AFTER the video is
        popped from candidates is still paired by _pair_companions_for_video,
        because that method reads self.candidates at the time it runs.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        video = incoming / "Scene.mp4"
        video.write_bytes(b"v" * 1000)
        companion = incoming / "Scene.jpg"
        companion.write_bytes(b"i")

        # Simulate video being popped (as evaluate_once does before _scan)
        with worker.lock:
            worker.candidates.pop(str(video.resolve()), None)

        # Companion arrives during the scan window
        worker.submit(str(companion))
        assert str(companion.resolve()) in worker.candidates, "Precondition"

        fake_scene = {
            "id": "42",
            "files": [{"path": str(video.resolve())}],
            "performers": [], "tags": [], "studio": None,
            "title": None, "details": None, "date": None,
            "director": None, "code": None, "rating100": None,
            "organized": False, "urls": [], "galleries": [],
            "stash_ids": [], "groups": [],
        }
        worker.stash.metadata_scan.return_value = "job-1"
        worker._pair_companions_for_video(str(video.resolve()), fake_scene)

        assert str(companion.resolve()) not in worker.candidates, (
            "FALSE POSITIVE confirmed: _pair_companions_for_video correctly finds "
            "and removes the companion submitted after video was popped"
        )

    def test_companion_in_waiting_state_is_still_paired_by_pair_companions(self, tmp_path):
        """
        FALSE POSITIVE (narrow race confirmed): Even if _process_companion runs
        first (saving 'waiting') during the scan window, the companion stays in
        self.candidates, so _pair_companions_for_video still pairs it correctly.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        video = incoming / "Scene.mp4"
        video.write_bytes(b"v" * 1000)
        companion = incoming / "Scene.jpg"
        companion.write_bytes(b"i")

        stat = companion.stat()
        with worker.lock:
            worker.candidates[str(companion.resolve())] = {
                "size": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
                "stable_since": time.time() - 10,
                "attempts": 0,
                "is_companion": True,
                "is_temporary": False,
            }

        # _process_companion runs (no video in candidates, no DB match → 'waiting', stays)
        worker._process_companion(str(companion.resolve()), worker.candidates[str(companion.resolve())])
        assert str(companion.resolve()) in worker.candidates, "Companion still present after _process_companion"

        # _pair_companions_for_video runs afterward
        fake_scene = {
            "id": "99",
            "files": [{"path": str(video.resolve())}],
            "performers": [], "tags": [], "studio": None,
            "title": None, "details": None, "date": None,
            "director": None, "code": None, "rating100": None,
            "organized": False, "urls": [], "galleries": [],
            "stash_ids": [], "groups": [],
        }
        worker.stash.metadata_scan.return_value = "job-2"
        worker._pair_companions_for_video(str(video.resolve()), fake_scene)

        assert str(companion.resolve()) not in worker.candidates, (
            "FALSE POSITIVE (narrow race) confirmed: even after _process_companion "
            "runs during the scan window, _pair_companions_for_video correctly "
            "pairs the companion because it stays in self.candidates with status 'waiting'"
        )


# ==========================================================================
# FINDING 8 — Relocation path variable reuse in _scan
# ==========================================================================

class TestFinding8RelocationPathReuse:

    def test_relocate_pops_source_path_correctly(self, tmp_path):
        """
        FALSE POSITIVE confirmed: worker.relocate(source, dest) pops `source`
        from candidates and adds `dest`.  No ghost entry for original path.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        orig = incoming / "downloading.part"
        dest = incoming / "complete.mp4"
        orig.write_bytes(b"data")
        dest.write_bytes(b"data")

        orig_str = str(orig.resolve())
        dest_str = str(dest.resolve())
        stat = orig.stat()

        with worker.lock:
            worker.candidates[orig_str] = {
                "size": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
                "stable_since": time.time(),
                "attempts": 0,
                "is_companion": False,
                "is_temporary": True,
            }
        worker._save_state(orig_str, "downloading", stat=stat)

        worker.relocate(orig_str, dest_str)

        assert orig_str not in worker.candidates, (
            "FALSE POSITIVE confirmed: relocate() correctly pops the original "
            "path — no ghost entry"
        )
        assert dest_str in worker.candidates

    def test_scan_pop_of_new_path_is_harmless(self, tmp_path):
        """
        FALSE POSITIVE confirmed: _scan line 976 pops the new path (dest),
        which is correct.  The original (orig) was already popped by relocate().
        Result: no ghost entries in candidates.
        """
        db = tmp_path / "db.sqlite3"
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        _make_db(db)
        worker = _make_worker(db, incoming)

        orig = incoming / "movie.partial"
        dest = incoming / "movie.mp4"
        orig.write_bytes(b"data" * 100)
        dest.write_bytes(b"data" * 100)

        orig_str = str(orig.resolve())
        dest_str = str(dest.resolve())

        stat = orig.stat()
        with worker.lock:
            worker.candidates[orig_str] = {
                "size": stat.st_size, "modified_ns": stat.st_mtime_ns,
                "stable_since": time.time(), "attempts": 0,
                "is_companion": False, "is_temporary": True,
            }

        # relocate() pops orig, adds dest
        worker.relocate(orig_str, dest_str)
        # _scan's inner loop then does: path = current_path (=dest_str); pop(path)
        with worker.lock:
            worker.candidates.pop(dest_str, None)

        assert orig_str not in worker.candidates, "No ghost for original path"
        assert dest_str not in worker.candidates, "dest correctly removed"


# ==========================================================================
# FINDING 1 — CODE-CONFIRMED: No timeout on rglob
# ==========================================================================

class TestFinding1NoRglobTimeout:
    def test_no_timeout_around_rglob_in_current_incoming_paths(self):
        """
        CODE-CONFIRMED: _current_incoming_paths uses rglob with no timeout.
        On an unresponsive NFS/SMB mount the IncomingWorker thread will block.
        Cannot be reproduced safely in an automated test without a real mount.
        """
        import inspect
        src = inspect.getsource(CompletedDownloadWorker._current_incoming_paths)
        assert "rglob" in src, "rglob must be present"
        has_timeout = any(t in src.lower() for t in ["timeout", "signal.alarm", "threading.timer", "concurrent.futures"])
        assert not has_timeout, (
            "Timeout guard found — finding 1 would be FALSE POSITIVE"
        )


# ==========================================================================
# FINDING 2 — Unbounded timer threads on deletion
# ==========================================================================

class TestFinding2UnboundedTimerThreads:

    def test_no_limit_on_deletion_timer_count(self):
        """CODE-CONFIRMED: on_deleted spawns Timer threads without any count limit."""
        import inspect
        src = inspect.getsource(LibraryEventHandler.on_deleted)
        assert "threading.Timer" in src
        has_limit = any(tok in src for tok in [
            "Semaphore", "BoundedSemaphore", "ThreadPoolExecutor",
            "len(self._notification_timers", "maxsize"
        ])
        assert not has_limit, "Limit already exists — finding 2 FALSE POSITIVE"

    def test_deletion_events_create_new_threads(self, tmp_path):
        """
        CONFIRMED: 10 file-deletion events for .nfo files each spawn a timer thread.
        """
        from watchdog.events import FileDeletedEvent

        db = tmp_path / "db.sqlite3"
        _make_db(db)
        move_worker = MagicMock()
        move_worker.transcoder_compatibility = False
        handler = LibraryEventHandler(db, move_worker, False, incoming_worker=None)

        active_before = threading.active_count()

        for i in range(10):
            f = tmp_path / f"file_{i:03d}.nfo"
            f.write_text("x")
            evt = FileDeletedEvent(str(f))
            handler.on_deleted(evt)

        time.sleep(0.05)
        new_threads = threading.active_count() - active_before

        assert new_threads >= 1, (
            f"Expected timer threads to start, got only {new_threads} new threads"
        )


# ==========================================================================
# FINDING 6 — DB read inside pending-video loop (Minor, not a bug)
# ==========================================================================

class TestFinding6DBReadInLoop:
    def test_process_companion_opens_db_connection_per_call(self, tmp_path):
        """
        CODE-CONFIRMED (minor): _process_companion opens one DB connection per call
        via connect(). This is not a bug but is noted as a performance characteristic.
        For N stuck companions, N DB connections are opened per evaluate_once() cycle.
        """
        import inspect
        src = inspect.getsource(CompletedDownloadWorker._process_companion)
        # The DB is accessed via find_scene_for_companion which uses an already-open connection
        # The connection is opened by _process_companion itself
        assert "connect(self.database_path)" in src
        # One connection per _process_companion call
        connection_opens = src.count("connect(self.database_path)")
        assert connection_opens == 1, (
            f"_process_companion opens {connection_opens} connections per call"
        )


# ==========================================================================
# FINDING 9 — Hot-reload missing incoming_worker.notifications
# ==========================================================================

class TestFinding9HotReloadMissesNotifications:
    def test_incoming_worker_notifications_not_updated_in_reload_block(self):
        """
        CODE-CONFIRMED: hot-reload updates worker.notifications but NOT
        incoming_worker.notifications. Toggling notifications in the UI has
        no effect on incoming import notifications until monitor restart.
        """
        import inspect
        import librarymanager_monitor as mod
        src = inspect.getsource(mod.main)
        reload_idx = src.find('"reload"')
        assert reload_idx != -1
        reload_block = src[reload_idx:reload_idx + 1500]
        assert "worker.notifications" in reload_block, (
            "worker.notifications must be updated"
        )
        assert "incoming_worker.notifications" not in reload_block, (
            "FALSE POSITIVE: incoming_worker.notifications IS updated — finding 9 wrong"
        )


# ==========================================================================
# FINDING 11 — Misleading method name
# ==========================================================================

class TestFinding11MisleadingName:
    def test_current_video_paths_is_alias_for_current_incoming_paths(self):
        """CODE-CONFIRMED: name is misleading but the behaviour is confirmed by F4 tests."""
        import inspect
        src = inspect.getsource(CompletedDownloadWorker._current_video_paths)
        assert "_current_incoming_paths" in src


# ==========================================================================
# FINDING 12 — Timer window vs suppress window mismatch
# ==========================================================================

class TestFinding12TimerWindowMismatch:
    def test_timer_3s_suppress_5s_is_conservative_design_choice(self):
        """
        CODE-CONFIRMED (reclassified as design choice, not a bug):
        Timer fires at t+3 but suppress window is 5s, meaning any recreate
        between t=0 and t=notify+5s is suppressed.  This is a wider safety
        margin that avoids false 'file deleted' warnings during atomic operations.
        """
        import inspect
        src_deleted = inspect.getsource(LibraryEventHandler.on_deleted)
        src_notify  = inspect.getsource(LibraryEventHandler._notify_if_still_missing)
        assert "Timer(3," in src_deleted
        assert "< 5.0" in src_notify
        # Document: this is intentionally conservative, not a data-loss bug
