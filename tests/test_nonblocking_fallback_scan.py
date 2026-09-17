"""Regression coverage for non-blocking incoming fallback scans.

A network filesystem can block forever inside a recursive directory walk.  These
checks ensure one hung incoming folder cannot block the CompletedDownloadWorker
or prevent another configured incoming folder from being discovered.
"""

import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from librarymanager_core import SCHEMA
from librarymanager_monitor import CompletedDownloadWorker


def _make_db(path):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def _make_worker(db_path, folders):
    stash = MagicMock()
    stash.call_GQL.return_value = {"findScenes": {"scenes": []}}
    values = [str(folder) for folder in folders]
    return CompletedDownloadWorker(
        database_path=db_path,
        stash=stash,
        incoming_folder=values[0],
        enabled=True,
        settle_seconds=300,
        notifications=False,
        fallback_seconds=15,
        incoming_folders=values,
    )


def test_hung_folder_does_not_block_fallback_or_healthy_folder(tmp_path):
    db_path = tmp_path / "watchtower.sqlite3"
    _make_db(db_path)
    hung = tmp_path / "hung"
    healthy = tmp_path / "healthy"
    hung.mkdir()
    healthy.mkdir()
    video = healthy / "Healthy.mp4"
    video.write_bytes(b"video")

    release_hung_scan = threading.Event()
    real_rglob = Path.rglob

    def selective_rglob(path_obj, pattern):
        if path_obj == hung:
            release_hung_scan.wait(timeout=5.0)
            return iter(())
        return real_rglob(path_obj, pattern)

    with patch.object(Path, "rglob", autospec=True, side_effect=selective_rglob):
        worker = _make_worker(db_path, [hung, healthy])
        try:
            started = time.monotonic()
            worker._fallback_check()
            assert time.monotonic() - started < 0.2, "fallback scheduling must not wait for rglob"

            deadline = time.monotonic() + 2.0
            healthy_path = str(video.resolve())
            while time.monotonic() < deadline:
                with worker.lock:
                    if healthy_path in worker.candidates:
                        break
                time.sleep(0.02)
            with worker.lock:
                assert healthy_path in worker.candidates, "healthy folder must be discovered while another scan is hung"
        finally:
            release_hung_scan.set()
            worker.stop()


def test_repeated_fallback_is_single_flight_for_hung_folder(tmp_path):
    db_path = tmp_path / "watchtower.sqlite3"
    _make_db(db_path)
    hung = tmp_path / "hung"
    hung.mkdir()

    release_hung_scan = threading.Event()
    entered = 0
    entered_lock = threading.Lock()

    def blocking_rglob(path_obj, pattern):
        nonlocal entered
        with entered_lock:
            entered += 1
        release_hung_scan.wait(timeout=5.0)
        return iter(())

    with patch.object(Path, "rglob", autospec=True, side_effect=blocking_rglob):
        worker = _make_worker(db_path, [hung])
        try:
            for _ in range(20):
                worker._fallback_check()
            time.sleep(0.1)
            with entered_lock:
                assert entered == 1, "a hung folder must have at most one in-flight recursive scan"
        finally:
            release_hung_scan.set()
            worker.stop()


def test_hung_folder_does_not_block_submit_tree(tmp_path):
    """A hung folder in submit_tree must not block the caller/watchdog thread on rglob."""
    db_path = tmp_path / "watchtower.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    hung = incoming / "hung"
    hung.mkdir()

    release_hung_scan = threading.Event()
    real_rglob = Path.rglob

    def selective_rglob(path_obj, pattern):
        if path_obj == hung:
            release_hung_scan.wait(timeout=5.0)
            return iter(())
        return real_rglob(path_obj, pattern)

    with patch.object(Path, "rglob", autospec=True, side_effect=selective_rglob):
        worker = _make_worker(db_path, [incoming])
        try:
            started = time.monotonic()
            count = worker.submit_tree(hung)
            elapsed = time.monotonic() - started
            assert elapsed < 0.2, f"submit_tree must not block caller thread on rglob (took {elapsed:.2f}s)"
            assert count == 0
        finally:
            release_hung_scan.set()
            worker.stop()


def test_submit_tree_does_not_call_is_dir_synchronously(tmp_path):
    """submit_tree must do zero network filesystem I/O (including is_dir) before dispatch."""
    db_path = tmp_path / "watchtower.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    hung_dir = incoming / "hung_dir"
    hung_dir.mkdir()

    release_is_dir = threading.Event()
    real_is_dir = Path.is_dir

    def blocking_is_dir(path_obj):
        if path_obj == hung_dir:
            release_is_dir.wait(timeout=5.0)
            return True
        return real_is_dir(path_obj)

    with patch.object(Path, "is_dir", autospec=True, side_effect=blocking_is_dir):
        worker = _make_worker(db_path, [incoming])
        try:
            started = time.monotonic()
            count = worker.submit_tree(hung_dir)
            elapsed = time.monotonic() - started
            assert elapsed < 0.2, f"submit_tree must not block caller thread on is_dir (took {elapsed:.2f}s)"
        finally:
            release_is_dir.set()
            worker.stop()


def test_repeated_submit_tree_is_single_flight_per_owning_incoming_folder(tmp_path):
    """Nested directories under the same incoming folder share the single-flight scan lock."""
    db_path = tmp_path / "watchtower.sqlite3"
    _make_db(db_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    sub1 = incoming / "sub1"
    sub2 = incoming / "sub2"
    sub1.mkdir()
    sub2.mkdir()

    release_hung_scan = threading.Event()
    entered = 0
    entered_lock = threading.Lock()

    def blocking_rglob(path_obj, pattern):
        nonlocal entered
        with entered_lock:
            entered += 1
        release_hung_scan.wait(timeout=5.0)
        return iter(())

    with patch.object(Path, "rglob", autospec=True, side_effect=blocking_rglob):
        worker = _make_worker(db_path, [incoming])
        with entered_lock:
            baseline = entered
        try:
            # First call on sub1 launches scan thread and hangs
            worker.submit_tree(sub1)
            # Subsequent calls on sub1 or sub2 under same incoming folder must not spawn new threads
            for _ in range(10):
                worker.submit_tree(sub1)
                worker.submit_tree(sub2)
            time.sleep(0.1)
            with entered_lock:
                tree_entered = entered - baseline
                assert tree_entered <= 1, f"Expected at most 1 scan under owning incoming folder, entered {tree_entered} times"
        finally:
            release_hung_scan.set()
            worker.stop()
