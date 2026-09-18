"""Regression tests for WT-003 and WT-006 audit findings.

WT-003: Remaining blocking NAS operations (startup, event handlers, recovery scans).
WT-006: Root availability probe recovery lifecycle with hard thread limit.
"""

import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from watchdog.events import FileCreatedEvent, FileMovedEvent, FileModifiedEvent

WATCHTOWER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(WATCHTOWER_DIR))

from librarymanager_core import SCHEMA
from librarymanager_monitor import (
    RootAvailabilityTracker,
    CompletedDownloadWorker,
    LibraryEventHandler,
    is_path_available,
    BoundedFSOperation,
    probe_roots_startup,
)


def _make_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def _make_worker(db_path, incoming, settle=60, max_attempts=3):
    stash = MagicMock()
    stash.call_GQL.return_value = {"findScenes": {"scenes": []}}
    return CompletedDownloadWorker(
        database_path=db_path,
        stash=stash,
        incoming_folder=str(incoming),
        enabled=True,
        settle_seconds=settle,
        notifications=False,
        fallback_seconds=60,
        max_attempts=max_attempts,
        incoming_folders=[str(incoming)],
    )


# ==============================================================================
# WT-006 REGRESSION TESTS: Root availability probe recovery lifecycle
# ==============================================================================

def test_wt006_probe_timeout_recovers_when_share_returns_online(tmp_path):
    """WT-006: If a probe times out on a hung share, RootAvailabilityTracker must not
    leave in_flight=True permanently. When the share becomes responsive again, subsequent
    probes must be allowed to run and detect recovery."""
    hung_root = str(tmp_path / "nas_share")
    tracker = RootAvailabilityTracker([hung_root], probe_timeout=0.1)

    hang_event = threading.Event()

    def mock_scandir(p):
        hang_event.wait(timeout=5.0)
        class Dummy:
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return Dummy()

    # Phase 1: Share is hung
    with patch("pathlib.Path.is_dir", return_value=True), patch("os.scandir", side_effect=mock_scandir):
        # First poll launches probe
        avail, unavail, rec, lost = tracker.poll()
        assert hung_root in avail

        # Wait beyond probe_timeout
        time.sleep(0.2)

        # Second poll detects timeout
        avail, unavail, rec, lost = tracker.poll()
        assert hung_root in unavail
        assert hung_root in lost

        # Advance last_probed_at by 20s to satisfy retry cooldown
        with tracker.lock:
            tracker._states[hung_root]["last_probed_at"] = time.monotonic() - 20.0

    # Phase 2: Share recovers! (probe 1 remains hung forever, simulating dead kernel socket)
    # But fresh probe calls succeed immediately on a healthy filesystem
    def mock_scandir_recovering(p):
        class Dummy:
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return Dummy()

    with patch("pathlib.Path.is_dir", return_value=True), patch("os.scandir", side_effect=mock_scandir_recovering):
        # Third poll should launch a new probe and recover!
        avail, unavail, rec, lost = tracker.poll()
        # Wait a moment for worker thread to finish
        time.sleep(0.1)
        avail, unavail, rec, lost = tracker.poll()
        assert hung_root in avail, "Root failed to recover because in_flight remained True!"
        assert hung_root in rec, "Root was not reported in recovered list!"


def test_wt006_stale_probe_result_discarded_by_probe_id(tmp_path):
    """WT-006: If an abandoned probe finally returns after a newer probe has already
    completed, its stale result must be discarded and must not overwrite tracker state."""
    root = str(tmp_path / "flapping_share")
    tracker = RootAvailabilityTracker([root], probe_timeout=0.1)

    probe_1_block = threading.Event()
    probes_started = []

    def mock_scandir(p):
        probes_started.append(time.monotonic())
        if len(probes_started) == 1:
            probe_1_block.wait(timeout=5.0)
            raise OSError("Probe 1 failed late")
        class Dummy:
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return Dummy()

    with patch("pathlib.Path.is_dir", return_value=True), patch("os.scandir", side_effect=mock_scandir):
        tracker.poll()  # launches probe 1
        time.sleep(0.15)  # times out
        tracker.poll()  # marks unavailable

        # Satisfy cooldown
        with tracker.lock:
            tracker._states[root]["last_probed_at"] = time.monotonic() - 20.0

        # Launch probe 2 which succeeds immediately
        tracker.poll()
        time.sleep(0.05)
        avail, unavail, rec, lost = tracker.poll()
        assert root in avail, "Probe 2 should have marked root available"

        # Now probe 1 finally unblocks with failure
        probe_1_block.set()
        time.sleep(0.05)

        # State must STILL be available (probe 1's failure was discarded)
        with tracker.lock:
            assert tracker._states[root]["status"] == "available", "Stale probe 1 failure corrupted available status!"


def test_wt006_hard_limit_on_hung_probe_threads_prevents_unbounded_spawning(tmp_path):
    """WT-006: If a share remains permanently hung in kernel I/O, RootAvailabilityTracker
    must enforce a genuine hard limit on outstanding probe threads and not spawn unlimited threads."""
    hung_root = str(tmp_path / "dead_nas")
    tracker = RootAvailabilityTracker([hung_root], probe_timeout=0.05)

    hang_forever = threading.Event()

    def mock_scandir_hang(p):
        hang_forever.wait(timeout=10.0)
        return MagicMock()

    created_threads = []
    orig_thread_init = threading.Thread.__init__

    def track_thread_init(self, *args, **kwargs):
        if "probe_" in kwargs.get("name", ""):
            created_threads.append(self)
        orig_thread_init(self, *args, **kwargs)

    try:
        with patch("threading.Thread.__init__", track_thread_init),              patch("pathlib.Path.is_dir", return_value=True),              patch("os.scandir", side_effect=mock_scandir_hang):

            # Poll multiple times, resetting last_probed_at to simulate time passing
            for i in range(10):
                with tracker.lock:
                    if hung_root in tracker._states:
                        tracker._states[hung_root]["last_probed_at"] = 0.0
                tracker.poll()
                time.sleep(0.06)

            # Threads for hung_root must be strictly bounded by hard limit (<= 2)
            active_probe_threads = [t for t in created_threads if t.is_alive()]
            assert len(active_probe_threads) <= 2, f"Spawned {len(active_probe_threads)} threads, exceeding hard limit!"
    finally:
        hang_forever.set()


# ==============================================================================
# WT-003 REGRESSION TESTS: Remaining blocking NAS operations
# ==============================================================================

def test_wt003_startup_is_path_available_timeout_prevents_startup_hang():
    """WT-003: is_path_available() must enforce a bounded timeout so an unresponsive
    mount cannot hang the monitor process at startup."""
    hang_event = threading.Event()

    def mock_scandir_hang(p):
        hang_event.wait(timeout=10.0)
        return MagicMock()

    try:
        with patch("pathlib.Path.is_dir", return_value=True),              patch("os.scandir", side_effect=mock_scandir_hang):
            t0 = time.monotonic()
            result = is_path_available("/Volumes/HungNAS", timeout=0.2)
            elapsed = time.monotonic() - t0
            assert result is False, "Hung path must be reported as unavailable"
            assert elapsed < 1.0, f"is_path_available hung for {elapsed:.2f}s instead of timing out promptly!"
    finally:
        hang_event.set()


def test_wt003_trigger_recovery_scan_asynchronous_and_coalesced(tmp_path):
    """WT-003: trigger_recovery_scan() must not block the calling thread on synchronous
    path resolution (even if resolve() hangs on network mounts) and must coalesce repeated
    recovery scans via _active_scans without spawning unbounded threads."""
    db_path = tmp_path / "test.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    worker = _make_worker(db_path, incoming)
    root = str(incoming)

    hang_resolve = threading.Event()

    def mock_hang_resolve(self):
        hang_resolve.wait(timeout=5.0)
        return self

    try:
        # Patch Path.resolve so that if trigger_recovery_scan calls resolve(), it hangs
        with patch("pathlib.Path.resolve", mock_hang_resolve):
            t0 = time.monotonic()
            # Fire multiple recovery scans in rapid succession
            for _ in range(5):
                worker.trigger_recovery_scan(root)
            elapsed = time.monotonic() - t0

            # Must complete promptly without hanging on resolve()
            assert elapsed < 1.0, f"trigger_recovery_scan blocked calling thread for {elapsed:.2f}s!"

            # Scans must be coalesced under _scan_lock: at most 1 active scan thread per folder
            key = worker._folder_key(incoming)
            with worker._scan_lock:
                active_count = 1 if key in worker._active_scans else 0
                assert active_count <= 1, f"Active scans not coalesced: {active_count}"
    finally:
        hang_resolve.set()


def test_wt003_event_handlers_skip_offline_roots_without_filesystem_io(tmp_path):
    """WT-003: Filesystem events arriving for files on an offline root must be dropped
    immediately without performing blocking stat() or resolve() syscalls."""
    db_path = tmp_path / "test.sqlite3"
    _make_db(db_path)
    offline_root = str(tmp_path / "offline_mount")

    tracker = RootAvailabilityTracker([offline_root])
    tracker._states[offline_root]["status"] = "unavailable"

    mock_worker = MagicMock()
    mock_incoming = MagicMock()
    handler = LibraryEventHandler(
        database_path=db_path,
        worker=mock_worker,
        notifications=False,
        incoming_worker=mock_incoming,
        availability_tracker=tracker,
    )

    offline_file = f"{offline_root}/new_video.mp4"

    with patch("pathlib.Path.resolve") as mock_resolve, patch("pathlib.Path.stat") as mock_stat:
        handler.on_created(FileCreatedEvent(offline_file))
        handler.on_modified(FileModifiedEvent(offline_file))
        handler.on_moved(FileMovedEvent(offline_file, f"{offline_root}/renamed.mp4"))

        mock_resolve.assert_not_called()
        mock_stat.assert_not_called()
        mock_incoming.submit.assert_not_called()


def test_wt003_is_inside_incoming_preserves_folder_boundaries_and_symlinks(tmp_path):
    """WT-003: Path matching in _owning_incoming_folder and _is_inside_incoming must
    strictly respect directory boundaries (e.g. /incoming_other vs /incoming) and handle
    macOS /private symlink aliases without invoking blocking network syscalls."""
    db_path = tmp_path / "test.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    worker = _make_worker(db_path, incoming)

    # 1. Exact and child paths match
    assert worker._is_inside_incoming(str(incoming / "video.mp4")) is True
    assert worker._is_inside_incoming(str(incoming / "sub" / "video.mp4")) is True

    # 2. Sibling path with shared prefix must NOT match (boundary check)
    sibling = str(tmp_path / "incoming_other" / "video.mp4")
    assert worker._is_inside_incoming(sibling) is False
    assert worker._owning_incoming_folder(sibling) is None

    # 3. macOS /private alias variant matches
    inc_str = str(incoming / "video.mp4")
    if inc_str.startswith("/private/"):
        alt_path = inc_str[len("/private"):]
    else:
        alt_path = "/private" + inc_str
    assert worker._is_inside_incoming(alt_path) is True


def test_wt003_bounded_filesystem_operations_prevent_watchdog_hang(tmp_path):
    """WT-003: Filesystem operations in event handlers must be bounded so an unexpected
    hang in stat/resolve cannot block the watchdog dispatcher thread indefinitely, and
    exhausted worker allowance marks root unavailable without unbounded thread accumulation."""
    db_path = tmp_path / "test.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    worker = _make_worker(db_path, incoming)
    tracker = RootAvailabilityTracker([str(incoming)])
    handler = LibraryEventHandler(
        database_path=db_path,
        worker=MagicMock(),
        notifications=False,
        incoming_worker=worker,
        availability_tracker=tracker,
    )

    hang_event = threading.Event()
    real_stat = Path.stat

    def mock_stat_hang(self, *args, **kwargs):
        p_str = str(self)
        if "hung_download" in p_str or "incoming" in p_str:
            hang_event.wait(timeout=5.0)
            return MagicMock()
        return real_stat(self, *args, **kwargs)

    try:
        with patch.object(Path, "stat", mock_stat_hang):
            t0 = time.monotonic()
            # Send an event on an incoming candidate
            test_file = str(incoming / "hung_download.mp4")
            handler.on_created(FileCreatedEvent(test_file))
            elapsed = time.monotonic() - t0

            # Watchdog thread must not be blocked indefinitely (bounded to <= 2.0s)
            assert elapsed < 2.5, f"Watchdog thread blocked for {elapsed:.2f}s!"
    finally:
        hang_event.set()


# ==============================================================================
# Targeted Regression Tests for Reviewed WT-003 / WT-006 Corrections
# ==============================================================================

def test_bounded_fs_operation_atomic_admission_prevents_race():
    """Verify that BoundedFSOperation admission and reservation are atomic so concurrent
    callers cannot slip through and exceed the worker limit."""
    limiter = BoundedFSOperation(max_workers_per_root=1)
    root_key = "/Volumes/SharedMount"

    barrier = threading.Barrier(5)
    unblock = threading.Event()
    results = []

    def caller():
        barrier.wait()
        res, timed_out_or_exhausted = limiter.run(
            root_key,
            lambda: unblock.wait(timeout=2.0),
            timeout=0.2
        )
        results.append((res, timed_out_or_exhausted))

    threads = [threading.Thread(target=caller) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    unblock.set()

    # Under lock, exactly 1 thread was admitted and 4 were immediately rejected as busy
    with limiter._lock:
        active = [w["thread"] for w in limiter._active_workers.get(root_key, []) if w["thread"].is_alive()]
        assert len(active) <= 1, f"Exceeded worker limit: {len(active)} active workers!"

    # All 5 calls completed
    assert len(results) == 5
    assert all(r[1] in ("timeout", "busy", "outage") for r in results)


def test_submit_and_relocate_and_companion_bounded_prevents_dispatcher_hang(tmp_path):
    """Verify that Path.resolve() and is_file() inside submit(), relocate(), and companion
    matching cannot hang the watchdog dispatcher indefinitely."""
    db_path = tmp_path / "test.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    worker = _make_worker(db_path, incoming)
    tracker = RootAvailabilityTracker([str(incoming)])
    handler = LibraryEventHandler(
        database_path=db_path,
        worker=MagicMock(),
        notifications=False,
        incoming_worker=worker,
        availability_tracker=tracker,
    )

    hang_event = threading.Event()

    def mock_resolve_hang(self):
        hang_event.wait(timeout=5.0)
        return self

    try:
        # 1. submit() with hanging resolve()
        with patch("pathlib.Path.resolve", mock_resolve_hang):
            t0 = time.monotonic()
            candidate = str(incoming / "hung_resolve.mp4")
            res = worker.submit(candidate)
            elapsed = time.monotonic() - t0
            assert res is False
            assert elapsed < 2.5, f"submit() hung for {elapsed:.2f}s on resolve()!"

        # 2. relocate() with hanging resolve()
        with patch("pathlib.Path.resolve", mock_resolve_hang):
            t0 = time.monotonic()
            res = worker.relocate(str(incoming / "src.mp4"), str(incoming / "dst.mp4"))
            elapsed = time.monotonic() - t0
            assert res is False
            assert elapsed < 2.5, f"relocate() hung for {elapsed:.2f}s on resolve()!"

        # 3. companion is_file() hanging in on_created
        def mock_is_file_hang(self):
            hang_event.wait(timeout=5.0)
            return True

        with patch("pathlib.Path.is_file", mock_is_file_hang):
            t0 = time.monotonic()
            handler.on_created(FileCreatedEvent(str(incoming / "hung_companion.nfo")))
            elapsed = time.monotonic() - t0
            assert elapsed < 2.5, f"on_created companion check hung for {elapsed:.2f}s!"
    finally:
        hang_event.set()


def test_startup_probe_bounded_workers_without_nested_threads():
    """Verify that startup probe uses a bounded worker pool without nested thread-per-root explosion."""
    roots = [f"/Volumes/Root_{i}" for i in range(6)]
    hang_event = threading.Event()

    def mock_scandir(p):
        if "Root_0" in str(p) or "Root_1" in str(p):
            hang_event.wait(timeout=5.0)
        class Dummy:
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return Dummy()

    created_probe_threads = []
    orig_thread_init = threading.Thread.__init__

    def track_thread_init(self, *args, **kwargs):
        if "startup_probe_" in kwargs.get("name", ""):
            created_probe_threads.append(self)
        orig_thread_init(self, *args, **kwargs)

    try:
        with patch("threading.Thread.__init__", track_thread_init),              patch("pathlib.Path.is_dir", return_value=True),              patch("os.scandir", side_effect=mock_scandir):
            t0 = time.monotonic()
            available, unavailable = probe_roots_startup(roots, timeout=0.3, max_workers=2)
            elapsed = time.monotonic() - t0

            assert elapsed < 1.0, f"probe_roots_startup exceeded deadline: {elapsed:.2f}s"
            # Total worker threads created is strictly bounded (at most len(roots) = 6)
            assert len(created_probe_threads) <= 6
            # No nested inner threads were created
            inner_threads = [t for t in created_probe_threads if "check_" in getattr(t, "name", "")]
            assert len(inner_threads) == 0, "Nested inner threads were created!"
            assert len(available) + len(unavailable) == len(roots)
    finally:
        hang_event.set()


def test_bounded_fs_operation_distinguishes_busy_from_timeout_or_outage(tmp_path):
    """Verify that BoundedFSOperation distinguishes worker busy from an actual timeout or outage.
    A healthy NAS must NOT be marked unavailable merely because two file events arrive together."""
    limiter = BoundedFSOperation(max_workers_per_root=1)
    root = "/Volumes/HealthyNAS"
    tracker = RootAvailabilityTracker([root])
    assert tracker.get_unavailable_roots() == []

    # Scenario 1: First worker is running normally (taking 0.15s on healthy NAS)
    unblock_1 = threading.Event()
    worker_1_started = threading.Event()

    def op1():
        worker_1_started.set()
        unblock_1.wait(timeout=1.0)
        return "op1_done"

    t1 = threading.Thread(target=lambda: limiter.run(root, op1, timeout=0.5))
    t1.start()
    worker_1_started.wait(timeout=1.0)

    # Second worker arrives while worker 1 is running normally
    res2, status2 = limiter.run(root, lambda: "op2", timeout=0.5)
    assert status2 == "busy", f"Expected busy status, got {status2}"
    assert res2 is None

    # Callers MUST NOT mark root unavailable when status is busy!
    if status2 in ("timeout", "outage"):
        tracker.mark_root_unavailable(root)
    assert tracker.get_unavailable_roots() == [], "Healthy root must NOT be marked unavailable on busy!"

    unblock_1.set()
    t1.join(timeout=1.0)

    # Scenario 2: Operation actually times out (hangs > timeout)
    hang_event = threading.Event()
    try:
        t0 = time.monotonic()
        res3, status3 = limiter.run(root, lambda: hang_event.wait(timeout=2.0), timeout=0.1)
        elapsed = time.monotonic() - t0
        assert status3 == "timeout", f"Expected timeout, got {status3}"
        assert elapsed < 0.5

        # Actual timeout DOES mark root unavailable
        if status3 in ("timeout", "outage"):
            tracker.mark_root_unavailable(root)
        assert tracker.get_unavailable_roots() == [root], "Timed-out root must be marked unavailable!"

        # Scenario 3: Another caller arrives while hung thread is still alive -> confirmed outage
        res4, status4 = limiter.run(root, lambda: "op4", timeout=0.1)
        assert status4 == "outage", f"Expected outage status, got {status4}"
        if status4 in ("timeout", "outage"):
            tracker.mark_root_unavailable(root)
        assert tracker.get_unavailable_roots() == [root]
    finally:
        hang_event.set()


def test_trigger_recovery_scan_genuinely_asynchronous_and_coalesced(tmp_path):
    """Verify that trigger_recovery_scan() is genuinely asynchronous (non-blocking for monitor loop)
    and coalesces repeated recovery requests into at most one subsequent pass."""
    db_path = tmp_path / "test.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    worker = _make_worker(db_path, incoming)
    root = str(incoming)

    hang_folder = threading.Event()

    def mock_dispatch(folder, cutoff):
        hang_folder.wait(timeout=5.0)
        return MagicMock()

    try:
        with patch.object(worker, "_dispatch_folder_scan", side_effect=mock_dispatch):
            t0 = time.monotonic()
            # Monitor loop calls with max_wait=0 -> must return IMMEDIATELY without executing scan work
            t = worker.trigger_recovery_scan(root, max_wait=0)
            elapsed = time.monotonic() - t0
            assert elapsed < 0.05, f"trigger_recovery_scan blocked caller for {elapsed:.2f}s!"
            assert t is not None, "Expected background recovery thread"

            # Fire 5 repeated requests for the same root while the first is running
            for _ in range(5):
                t_rep = worker.trigger_recovery_scan(root, max_wait=0)
                assert t_rep == t, "Repeated recovery requests must return existing active thread"

            # Verify coalescing: exactly 1 active recovery scan in _active_recovery_scans
            with worker._recovery_lock:
                assert len(worker._active_recovery_scans) == 1
                assert root in worker._pending_recovery_scans
    finally:
        hang_folder.set()
        if t:
            t.join(timeout=1.0)


def test_deferred_busy_operation_processed_when_capacity_available(tmp_path):
    """Verify that when submit() or relocate() encounters a busy worker, the operation is deferred
    and reliably processed once worker capacity becomes available, even for files with old mtimes
    that fallback folder scanning would miss."""
    from librarymanager_monitor import default_fs_limiter

    db_path = tmp_path / "test.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()

    worker = _make_worker(db_path, incoming)
    root = str(incoming)

    # 1. Create a video file with an ancient mtime (1000.0s, far in the past)
    old_file = incoming / "OldDownload.mp4"
    old_file.write_bytes(b"content of old download")
    old_mtime = 1000.0
    os.utime(old_file, (old_mtime, old_mtime))
    old_file_str = str(old_file.resolve())

    # Fallback scan cutoff would miss this file because old_mtime < worker.started_at
    assert old_mtime < worker.started_at

    # 2. Block worker capacity in default_fs_limiter for this root
    unblock_limiter = threading.Event()
    worker_started = threading.Event()

    def busy_task():
        worker_started.set()
        unblock_limiter.wait(timeout=5.0)

    # default_fs_limiter has max_workers_per_root=2. Occupy both slots:
    t1 = threading.Thread(target=lambda: default_fs_limiter.run(root, busy_task, timeout=1.0))
    t2 = threading.Thread(target=lambda: default_fs_limiter.run(root, busy_task, timeout=1.0))
    t1.start()
    t2.start()
    time.sleep(0.05)

    try:
        # 3. Call submit() while capacity is exhausted
        res = worker.submit(old_file_str)
        # Must return False without marking root offline
        assert res is False
        assert old_file_str not in worker.candidates

        # Must be safely queued in _deferred_busy_submissions
        with worker._deferred_lock:
            assert old_file_str in worker._deferred_busy_submissions
    finally:
        # 4. Release worker capacity
        unblock_limiter.set()
        t1.join(timeout=1.0)
        t2.join(timeout=1.0)

    # 5. When capacity becomes available, evaluate_once() retries deferred submissions
    worker.evaluate_once()

    # The old-mtime download is now reliably ingested into candidates!
    assert old_file_str in worker.candidates, "Old-mtime file must be ingested after worker capacity is restored!"
    with worker._deferred_lock:
        assert old_file_str not in worker._deferred_busy_submissions

    # 6. Now test relocate() under busy conditions
    dst_file = incoming / "RenamedDownload.mp4"
    dst_file_str = str(dst_file.resolve())
    old_file.rename(dst_file)

    unblock_limiter_2 = threading.Event()
    t3 = threading.Thread(target=lambda: default_fs_limiter.run(root, lambda: unblock_limiter_2.wait(timeout=5.0), timeout=1.0))
    t4 = threading.Thread(target=lambda: default_fs_limiter.run(root, lambda: unblock_limiter_2.wait(timeout=5.0), timeout=1.0))
    t3.start()
    t4.start()
    time.sleep(0.05)

    try:
        rel_res = worker.relocate(old_file_str, dst_file_str)
        assert rel_res is False
        with worker._deferred_lock:
            assert any(s == old_file_str and d == dst_file_str for s, d in worker._deferred_busy_relocations)
    finally:
        unblock_limiter_2.set()
        t3.join(timeout=1.0)
        t4.join(timeout=1.0)

    # When capacity returns, evaluate_once() retries and carries over relocation
    worker.evaluate_once()
    assert dst_file_str in worker.candidates
    assert old_file_str not in worker.candidates
