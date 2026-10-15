"""Rename queue and rename execution service for Watchtower.

This module is responsible for:
- Serializing file operations with a cross-platform filesystem lock (rename_lock)
- Managing the debounced rename queue (enqueue, claim, cancel, make-due, finish, fail)
- Applying preflighted automatic renames (apply_scene_filename) with companion sidecar
  movement, Stash move callback integration, database updates, and rollback on failure
- Applying manual filename corrections (apply_manual_filename) with companion sidecar
  movement and local database state synchronization
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from librarymanager.database.connection import create_connection
from librarymanager.database.migrations import ensure_schema
from librarymanager.scanning.filesystem import expect_filesystem_move
from librarymanager.scanning.naming_rules import preview_manual_filename, preview_scene_filename

logger = logging.getLogger("librarymanager.services.renaming")

_STALE_PROCESSING_SECONDS = 90


def utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _is_mock(fn: Any) -> bool:
    if fn is None:
        return False
    return (
        hasattr(fn, "mock_calls")
        or hasattr(fn, "assert_called")
        or type(fn).__name__ in ("Mock", "MagicMock", "AsyncMock")
    )


def _get_core_fn(name: str, fallback_fn: Optional[Callable] = None) -> Optional[Callable]:
    """Retrieve an active override or mock, prioritizing direct patches on this module."""
    if _is_mock(fallback_fn):
        return fallback_fn

    core_mod = sys.modules.get("librarymanager_core")
    if core_mod is not None:
        core_fn = getattr(core_mod, name, None)
        if core_fn is not None:
            if _is_mock(core_fn):
                return core_fn
            if not getattr(core_fn, "_is_watchtower_wrapper", False) and name in (
                "connect",
                "_connect_short",
                "record_activity",
            ):
                return core_fn

    return fallback_fn


@contextmanager
def rename_lock(database_path: Path):
    """Serialize file operations from overlapping Stash hooks."""
    lock_file = open(database_path.with_suffix(".rename.lock"), "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            lock_file.close()


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = create_connection(database_path)
    ensure_schema(connection, database_path)
    return connection


def _connect_short(database_path: Path) -> sqlite3.Connection:
    """Open a connection with a short write-lock timeout for hook-critical paths."""
    connection = create_connection(database_path, busy_timeout_ms=5000)
    ensure_schema(connection, database_path)
    return connection


def enqueue_rename(
    database_path: Path, scene_id: str, now_timestamp: float, debounce_seconds: float = 1.0
) -> bool:
    """Coalesce a scene's hooks and return True only when a worker must be scheduled.

    Uses a 5-second write-lock timeout (instead of the default 30 s) so that a
    batch of simultaneous Stash hooks never stalls the UI for half a minute.
    """
    connect_short = _get_core_fn("_connect_short", _connect_short)
    connection = connect_short(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO rename_queue(scene_id,enqueued_at,available_at,status,attempts,processing_started_at,last_error)
               VALUES (?,?,?,'pending',0,NULL,NULL)
               ON CONFLICT(scene_id) DO UPDATE SET enqueued_at=excluded.enqueued_at,
               available_at=excluded.available_at,status='pending',processing_started_at=NULL,last_error=NULL""",
            (str(scene_id), now_timestamp, now_timestamp + debounce_seconds),
        )
        scheduled = connection.execute("SELECT scheduled FROM rename_worker_state WHERE id=1").fetchone()[0]
        should_schedule = not bool(scheduled)
        if should_schedule:
            connection.execute("UPDATE rename_worker_state SET scheduled=1 WHERE id=1")
        connection.commit()
        return should_schedule
    finally:
        connection.close()


def cancel_pending_rename(database_path: Path, scene_id: str) -> bool:
    """Cancel a pending rename for a scene and remove it from the rename queue."""
    connect_fn = _get_core_fn("connect", _connect)
    connection = connect_fn(database_path)
    try:
        connection.execute("DELETE FROM rename_queue WHERE scene_id=?", (str(scene_id),))
        changes = connection.execute("SELECT changes()").fetchone()[0]
        connection.commit()
    finally:
        connection.close()
    if changes > 0:
        record_fn = _get_core_fn("record_activity", None)
        if record_fn:
            record_fn(
                database_path,
                "rename",
                "automatic rename",
                "cancelled",
                scene_id=str(scene_id),
                detail="Pending rename cancelled by user; original filename preserved on disk",
            )
    return bool(changes)


def make_pending_rename_due(database_path: Path, scene_id: str) -> bool:
    """Mark a queued or failed rename as pending and immediately due for execution."""
    connect_fn = _get_core_fn("connect", _connect)
    connection = connect_fn(database_path)
    try:
        connection.execute(
            """UPDATE rename_queue
               SET status='pending', available_at=?, last_error=NULL, processing_started_at=NULL
               WHERE scene_id=?""",
            (time.time() - 1.0, str(scene_id)),
        )
        changes = connection.execute("SELECT changes()").fetchone()[0]
        connection.commit()
        return bool(changes)
    finally:
        connection.close()


def release_worker_schedule(database_path: Path):
    """Reset the rename worker scheduled flag so future jobs can be scheduled."""
    connect_fn = _get_core_fn("connect", _connect)
    connection = connect_fn(database_path)
    try:
        connection.execute("UPDATE rename_worker_state SET scheduled=0 WHERE id=1")
        connection.commit()
    finally:
        connection.close()


def claim_due_rename(database_path: Path, now_timestamp: float) -> Tuple[Optional[str], Optional[float], Optional[int]]:
    """Claim the oldest pending rename ready for execution, recovering orphaned renames."""
    connect_fn = _get_core_fn("connect", _connect)
    connection = connect_fn(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Recover any renames stranded in 'processing' by a previously crashed worker.
        stale_cutoff = now_timestamp - _STALE_PROCESSING_SECONDS
        connection.execute(
            """UPDATE rename_queue SET status='pending',processing_started_at=NULL
               WHERE status='processing' AND COALESCE(processing_started_at,enqueued_at) <= ?""",
            (stale_cutoff,),
        )
        row = connection.execute(
            "SELECT scene_id FROM rename_queue WHERE status='pending' AND available_at<=? ORDER BY available_at LIMIT 1",
            (now_timestamp,),
        ).fetchone()
        if not row:
            next_row = connection.execute(
                "SELECT MIN(available_at) AS next_at,COUNT(*) AS pending FROM rename_queue WHERE status='pending'"
            ).fetchone()
            connection.commit()
            return None, next_row["next_at"], next_row["pending"]
        scene_id = row["scene_id"]
        connection.execute(
            "UPDATE rename_queue SET status='processing',processing_started_at=?,attempts=attempts+1 WHERE scene_id=?",
            (now_timestamp, scene_id),
        )
        connection.commit()
        return scene_id, None, None
    finally:
        connection.close()


def finish_queued_rename(database_path: Path, scene_id: str, outcome: str, detail: str):
    """Mark a queued rename as completed, remove it from the queue, and record audit."""
    connect_fn = _get_core_fn("connect", _connect)
    connection = connect_fn(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM rename_queue WHERE scene_id=?", (str(scene_id),))
        connection.execute(
            "INSERT INTO rename_audit(scene_id,outcome,detail,recorded_at) VALUES (?,?,?,?)",
            (str(scene_id), outcome, detail, utc_now()),
        )
        connection.commit()
    finally:
        connection.close()


def fail_queued_rename(database_path: Path, scene_id: str, error: str):
    """Record a failure for a queued rename and log it in the audit table."""
    connect_fn = _get_core_fn("connect", _connect)
    connection = connect_fn(database_path)
    try:
        connection.execute(
            "UPDATE rename_queue SET status='failed',last_error=? WHERE scene_id=?", (str(error), str(scene_id))
        )
        connection.execute(
            "INSERT INTO rename_audit(scene_id,outcome,detail,recorded_at) VALUES (?,?,?,?)",
            (str(scene_id), "failed", str(error), utc_now()),
        )
        connection.commit()
    finally:
        connection.close()


def apply_scene_filename(
    database_path: Path, scene_id: str, move_file: Callable, filename_options: Optional[dict] = None
) -> dict:
    """Apply one preflighted rename through a supplied Stash move callback."""
    lock_fn = _get_core_fn("rename_lock", rename_lock)
    with lock_fn(database_path):
        preview_fn = _get_core_fn("preview_scene_filename", preview_scene_filename)
        preview = preview_fn(database_path, scene_id, filename_options)
        if preview.get("status") != "ready":
            return preview
        moved_sidecars = []
        video_renamed = False
        expect_move_fn = _get_core_fn("expect_filesystem_move", expect_filesystem_move)
        connect_fn = _get_core_fn("connect", _connect)
        record_fn = _get_core_fn("record_activity", None)
        from librarymanager.services.filing import _move_file
        try:
            for item in preview["associated_files"]:
                source, target = Path(item["source"]), Path(item["target"])
                expect_move_fn(database_path, str(source), str(target))
                _move_file(source, target)
                moved_sidecars.append((source, target))
            current = Path(preview["current_path"])
            proposed = Path(preview["proposed_path"])
            expect_move_fn(database_path, str(current), str(proposed))
            result = move_file(preview["file_id"], str(current.parent), proposed.name)
            if result is False or result is None:
                raise RuntimeError("Stash did not confirm the file rename")
            video_renamed = True
            connection = None
            try:
                connection = connect_fn(database_path)
                connection.execute(
                    "UPDATE filename_state SET last_generated_stem=?,managed_date=?,managed_quality=?,updated_at=? WHERE file_id=?",
                    (
                        proposed.stem,
                        preview.get("scene_date") or None,
                        preview.get("video_quality") or None,
                        utc_now(),
                        preview["file_id"],
                    ),
                )
                connection.execute(
                    "UPDATE files SET path=?,basename=?,exists_on_disk=1,last_seen_at=? WHERE file_id=?",
                    (str(proposed), proposed.name, utc_now(), preview["file_id"]),
                )
                for source, target in moved_sidecars:
                    connection.execute(
                        "UPDATE incoming_files SET path=?, last_checked_at=? WHERE path=?",
                        (str(target), utc_now(), str(source)),
                    )
                connection.commit()
            except Exception as database_error:
                if connection is not None:
                    connection.rollback()
                return {
                    **preview,
                    "status": "renamed_with_warning",
                    "reason": f"Stash renamed the video, but Watchtower must refresh its local record: {database_error}",
                    "renamed_sidecars": [{"source": str(source), "target": str(target)} for source, target in moved_sidecars],
                    "action_performed": True,
                    "local_cache_updated": False,
                }
            finally:
                if connection is not None:
                    connection.close()

            # Sidecars are part of the same successful rename transaction, but make
            # them visible in Recent Activity as separate informational entries.
            # Logging is deliberately best-effort so a log write can never turn a
            # completed file rename into a failed/rollback operation.
            for source, target in moved_sidecars:
                if record_fn:
                    try:
                        record_fn(
                            database_path,
                            "filename",
                            "sidecar_renamed",
                            "complete",
                            scene_id=str(scene_id),
                            file_id=preview.get("file_id"),
                            old_path=str(source),
                            new_path=str(target),
                            detail=f"Companion file renamed with video: {source.name} → {target.name}",
                            metadata={"companion_type": target.suffix.lower()},
                        )
                    except Exception:
                        pass
            return {
                **preview,
                "status": "renamed",
                "reason": "Stash confirmed the rename",
                "renamed_sidecars": [{"source": str(source), "target": str(target)} for source, target in moved_sidecars],
                "action_performed": True,
                "local_cache_updated": True,
            }
        except Exception:
            if not video_renamed:
                for source, target in reversed(moved_sidecars):
                    if target.exists() and not source.exists():
                        expect_move_fn(database_path, str(target), str(source))
                        _move_file(target, source)
            raise


def apply_manual_filename(
    database_path: Path, scene_id: str, requested_name: str, move_file: Callable
) -> dict:
    """Apply one exact previewed correction and make it the scene's new filename base."""
    lock_fn = _get_core_fn("rename_lock", rename_lock)
    with lock_fn(database_path):
        preview_fn = _get_core_fn("preview_manual_filename", preview_manual_filename)
        preview = preview_fn(database_path, scene_id, requested_name)
        if preview.get("status") != "ready":
            return preview
        moved_sidecars = []
        video_renamed = False
        expect_move_fn = _get_core_fn("expect_filesystem_move", expect_filesystem_move)
        connect_fn = _get_core_fn("connect", _connect)
        record_fn = _get_core_fn("record_activity", None)
        from librarymanager.services.filing import _move_file
        try:
            for item in preview["associated_files"]:
                source, target = Path(item["source"]), Path(item["target"])
                expect_move_fn(database_path, str(source), str(target))
                _move_file(source, target)
                moved_sidecars.append((source, target))
            current, proposed = Path(preview["current_path"]), Path(preview["proposed_path"])
            expect_move_fn(database_path, str(current), str(proposed))
            result = move_file(preview["file_id"], str(current.parent), proposed.name)
            if result is False or result is None:
                raise RuntimeError("Stash did not confirm the file rename")
            video_renamed = True
            connection = None
            try:
                connection = connect_fn(database_path)
                connection.execute(
                    "UPDATE files SET path=?,basename=?,exists_on_disk=1,last_seen_at=? WHERE file_id=?",
                    (str(proposed), proposed.name, utc_now(), preview["file_id"]),
                )
                connection.execute(
                    """INSERT INTO filename_state(file_id,base_stem,base_source,source_title,last_generated_stem,
                           manual_studio,manual_performers_json,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET base_stem=excluded.base_stem,
                       base_source='manual',source_title=NULL,last_generated_stem=excluded.last_generated_stem,
                       manual_studio=excluded.manual_studio,manual_performers_json=excluded.manual_performers_json,
                       updated_at=excluded.updated_at""",
                    (
                        preview["file_id"],
                        proposed.stem,
                        "manual",
                        None,
                        proposed.stem,
                        preview.get("metadata_studio"),
                        json.dumps(preview.get("metadata_performers") or [], ensure_ascii=False),
                        utc_now(),
                        utc_now(),
                    ),
                )
                connection.commit()
            except Exception as database_error:
                if connection is not None:
                    connection.rollback()
                return {
                    **preview,
                    "status": "renamed_with_warning",
                    "reason": f"Stash renamed the video, but Watchtower must refresh its local record: {database_error}",
                    "renamed_sidecars": [{"source": str(source), "target": str(target)} for source, target in moved_sidecars],
                    "action_performed": True,
                    "local_cache_updated": False,
                }
            finally:
                if connection is not None:
                    connection.close()

            # Log successful companion-file renames separately so manual filename
            # corrections show their JPG/other sidecar updates in Recent Activity.
            # Keep this best-effort: logging must never undo a completed rename.
            for source, target in moved_sidecars:
                if record_fn:
                    try:
                        record_fn(
                            database_path,
                            "filename",
                            "sidecar_renamed",
                            "complete",
                            scene_id=str(scene_id),
                            file_id=preview.get("file_id"),
                            old_path=str(source),
                            new_path=str(target),
                            detail=f"Companion file renamed with video: {source.name} → {target.name}",
                            metadata={"companion_type": target.suffix.lower(), "manual_correction": True},
                        )
                    except Exception:
                        pass
            return {
                **preview,
                "status": "renamed",
                "reason": "Stash confirmed the manual correction",
                "renamed_sidecars": [{"source": str(source), "target": str(target)} for source, target in moved_sidecars],
                "action_performed": True,
                "local_cache_updated": True,
            }
        except Exception:
            if not video_renamed:
                for source, target in reversed(moved_sidecars):
                    if target.exists() and not source.exists():
                        expect_move_fn(database_path, str(target), str(source))
                        _move_file(target, source)
            raise


__all__ = [
    "_STALE_PROCESSING_SECONDS",
    "_connect_short",
    "apply_manual_filename",
    "apply_scene_filename",
    "cancel_pending_rename",
    "claim_due_rename",
    "enqueue_rename",
    "fail_queued_rename",
    "finish_queued_rename",
    "make_pending_rename_due",
    "release_worker_schedule",
    "rename_lock",
]
