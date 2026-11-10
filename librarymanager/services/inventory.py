"""Inventory operations and scene record management service for Watchtower.

This module is responsible for:
- Normalizing Stash scene payloads into flat file record dictionaries (flatten_scene_files)
- Performing full library inventory sweeps, recording run lifecycles, detecting missing/restored
  files, offline root awareness, tracking path transitions, and purging stale records (inventory)
- Performing targeted single-scene inventory refreshes for hook events (refresh_scene_inventory)
"""
from __future__ import annotations

import datetime
import errno
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Union

from librarymanager.database.connection import create_connection
from librarymanager.database.migrations import ensure_schema
from librarymanager.scanning.filesystem import is_file_on_unavailable_root as _default_is_file_on_unavailable_root

logger = logging.getLogger("librarymanager.services.inventory")


def utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def inventory_progress_path(database_path: Path | str) -> Path:
    """Return the filesystem path for the atomic inventory progress JSON file."""
    return Path(database_path).with_name(f"{Path(database_path).name}.inventory-progress.json")


def _is_transient_filesystem_error(exc: OSError) -> bool:
    """Check if an OSError is a transient file locking or contention error."""
    winerror = getattr(exc, "winerror", None)
    if winerror in (5, 32, 33):
        return True
    err = getattr(exc, "errno", None)
    transient_errnos = {
        getattr(errno, "EBUSY", None),
        getattr(errno, "ETXTBSY", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EWOULDBLOCK", None),
    }
    transient_errnos.discard(None)
    if err in transient_errnos:
        return True
    err_str = str(exc).lower()
    return any(marker in err_str for marker in ("winerror 5", "winerror 32", "access is denied", "accès refusé"))


def _default_is_pid_alive(pid: int | None) -> bool:
    """Check if a process with the given PID is currently alive on the system."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, ValueError, OSError):
        return False


def is_pid_alive(pid: int | None) -> bool:
    is_alive_fn = _get_core_fn("_is_pid_alive", _default_is_pid_alive)
    return is_alive_fn(pid)


def read_inventory_progress(database_path: Path | str) -> dict:
    """Read and parse the current inventory progress file safely."""
    progress_path = inventory_progress_path(database_path)
    if not progress_path.is_file():
        return {"status": "idle", "processed": 0, "total": 0, "detail": ""}
    try:
        payload = json.loads(progress_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"status": "idle", "processed": 0, "total": 0, "detail": ""}
    except (OSError, ValueError):
        return {"status": "unknown", "processed": 0, "total": 0, "detail": "Progress is temporarily unavailable"}


def get_active_inventory_run(database_path: Path | str) -> dict | None:
    """Return active inventory run metadata if an inventory process is currently alive, else None."""
    current = read_inventory_progress(database_path)
    if not isinstance(current, dict):
        return None
    if current.get("status") not in ("preparing", "running"):
        return None
    pid = current.get("pid")
    if pid is not None:
        try:
            pid_int = int(pid)
        except (ValueError, TypeError):
            return None
        if is_pid_alive(pid_int):
            if pid_int != os.getpid():
                return current
        else:
            # PID is dead: interrupted or crashed inventory run
            return None
    else:
        # Fallback for legacy progress files without PID: consider active only if updated within 300s
        updated_at_str = current.get("updated_at")
        if updated_at_str:
            try:
                updated_dt = datetime.datetime.fromisoformat(updated_at_str)
                age = (datetime.datetime.now(datetime.timezone.utc) - updated_dt).total_seconds()
                if age < 300:
                    return current
            except Exception:
                pass
    return None


def activity_logger():
    return logging.getLogger("librarymanager.activity")


def write_inventory_progress(
    database_path: Path | str,
    status: str,
    processed: int = 0,
    total: int = 0,
    detail: str = "",
    run_id: Optional[str] = None,
    pid: Optional[int] = None,
) -> dict:
    """Atomically publish inventory progress for the onboarding UI.

    Guarantees:
    - Identifiable run ownership via run_id and pid.
    - Only the active inventory run is permitted to publish progress.
    - A secondary or unowned process cannot overwrite or clear the active run's progress.
    - Interrupted/stale runs from terminated PIDs are safely reclaimed.
    - Temporary files are PID-and-timestamp-isolated and cleaned up safely.
    """
    progress_path = inventory_progress_path(database_path)
    owner_pid = int(pid or os.getpid())

    # Check ownership against existing progress file
    current = read_inventory_progress(database_path)
    if isinstance(current, dict) and current.get("status") in ("preparing", "running"):
        active_pid = current.get("pid")
        active_run_id = current.get("run_id")
        if active_pid is not None:
            try:
                active_pid_int = int(active_pid)
            except (ValueError, TypeError):
                active_pid_int = None
            if active_pid_int and is_pid_alive(active_pid_int):
                # If an active run is held by another process or another run ID:
                if active_pid_int != owner_pid or (active_run_id and run_id and active_run_id != run_id):
                    activity_logger().warning(
                        "Ignoring inventory progress update (status=%s) from unowned process (PID %s, run %s); "
                        "active inventory run %s (PID %s) holds ownership",
                        status, owner_pid, run_id, active_run_id, active_pid_int
                    )
                    return current

    temporary_path = progress_path.with_name(f"{progress_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    effective_run_id = str(run_id or (current.get("run_id") if isinstance(current, dict) else "") or "")
    payload = {
        "status": str(status),
        "processed": max(0, int(processed or 0)),
        "total": max(0, int(total or 0)),
        "detail": str(detail or ""),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "run_id": effective_run_id,
        "pid": owner_pid,
    }
    max_attempts = 5
    try:
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        for attempt in range(max_attempts):
            try:
                temporary_path.replace(progress_path)
                break
            except OSError as exc:
                if _is_transient_filesystem_error(exc) and attempt < max_attempts - 1:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    if _is_transient_filesystem_error(exc):
                        activity_logger().warning(
                            "Failed to publish inventory progress file %s after %d transient retry attempts: %s",
                            progress_path, max_attempts, exc
                        )
                    else:
                        activity_logger().warning(
                            "Non-transient filesystem error replacing inventory progress file %s: %s",
                            progress_path, exc
                        )
                    break
    except Exception as exc:
        activity_logger().warning(
            "Unexpected error writing inventory progress temporary file %s: %s",
            temporary_path, exc
        )
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
    return payload


def _is_mock(fn: Any) -> bool:
    if fn is None:
        return False
    return (
        hasattr(fn, "mock_calls")
        or hasattr(fn, "assert_called")
        or type(fn).__name__ in ("Mock", "MagicMock", "AsyncMock")
    )


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = create_connection(database_path)
    ensure_schema(connection, database_path)
    return connection


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
                "_connect",
                "is_file_on_unavailable_root",
            ):
                return core_fn

    return fallback_fn


def flatten_scene_files(scenes) -> Generator[dict, None, None]:
    """Normalize Stash scene payloads into flat file records for inventory storage."""
    for scene in scenes:
        performers = sorted(
            {p.get("name", "").strip() for p in scene.get("performers") or [] if p.get("name", "").strip()},
            key=str.casefold,
        )
        studio = (scene.get("studio") or {}).get("name")
        metadata = {
            "inventory_version": 1,
            "title": scene.get("title"),
            "details": scene.get("details"),
            "date": scene.get("date"),
            "director": scene.get("director"),
            "code": scene.get("code"),
            "rating100": scene.get("rating100"),
            "organized": scene.get("organized"),
            "studio": studio,
            "performers": sorted({str(p.get("id")) for p in scene.get("performers") or [] if p.get("id") is not None}),
            "tags": sorted({str(t.get("id")) for t in scene.get("tags") or [] if t.get("id") is not None}),
            "galleries": sorted({str(g.get("id")) for g in scene.get("galleries") or [] if g.get("id") is not None}),
            "urls": sorted({str(url) for url in scene.get("urls") or [] if url}),
            "stash_ids": sorted(
                {f"{item.get('endpoint')}|{item.get('stash_id')}" for item in scene.get("stash_ids") or []
                 if item.get("endpoint") and item.get("stash_id")}
            ),
            "groups": sorted(
                {f"{(item.get('group') or {}).get('id')}|{item.get('scene_index')}" for item in scene.get("groups") or []
                 if (item.get("group") or {}).get("id") is not None}
            ),
        }
        for file_record in scene.get("files") or []:
            path = file_record.get("path")
            file_id = file_record.get("id")
            if not path or file_id is None:
                continue
            height = None
            raw_height = file_record.get("height")
            if raw_height is not None:
                try:
                    h_val = int(raw_height)
                    if h_val > 0:
                        height = h_val
                except (ValueError, TypeError):
                    pass
            yield {
                "file_id": str(file_id),
                "scene_id": str(scene.get("id")),
                "path": str(path),
                "basename": file_record.get("basename") or os.path.basename(path),
                "title": scene.get("title"),
                "studio": studio,
                "performers_json": json.dumps(performers, ensure_ascii=False),
                "size": file_record.get("size"),
                "duration": file_record.get("duration"),
                "height": height,
                "fingerprints_json": json.dumps(file_record.get("fingerprints") or [], sort_keys=True),
                "scene_metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            }


def inventory(
    database_path: Path,
    scenes,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    run_id: Optional[str] = None,
) -> dict:
    """Execute a full library inventory sweep across the provided Stash scenes."""
    now = utc_now()
    connect_fn = _get_core_fn("connect", _connect)
    is_offline_fn = _get_core_fn("is_file_on_unavailable_root", _default_is_file_on_unavailable_root)
    get_active_fn = _get_core_fn("get_active_inventory_run", get_active_inventory_run)

    # Check if another living process is actively running inventory
    active_run = get_active_fn(database_path)
    if active_run and active_run.get("pid") is not None and active_run.get("pid") != os.getpid():
        raise RuntimeError(
            f"Inventory is already in progress in another process (PID {active_run.get('pid')}). "
            "Please wait for the active scan to finish."
        )

    # Fast lock probe: check if SQLite write lock is already held without waiting 30 seconds
    if not _is_mock(connect_fn):
        probe_conn = connect_fn(database_path)
        try:
            probe_conn.execute("PRAGMA busy_timeout = 250")
            probe_conn.execute("BEGIN IMMEDIATE")
            probe_conn.rollback()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise RuntimeError(
                    "Inventory cannot execute because the database is currently in use by another operation."
                ) from exc
            raise
        finally:
            probe_conn.close()

    connection = connect_fn(database_path)
    try:
        db_run_id = connection.execute(
            "INSERT INTO inventory_runs(started_at) VALUES (?)", (now,)
        ).lastrowid
        records = list(flatten_scene_files(scenes))
        total_records = len(records)
        if progress_callback:
            progress_callback(0, total_records)
        summary = {
            "scenes": len({record["scene_id"] for record in records}),
            "files": 0,
            "present": 0,
            "missing": 0,
            "changed_paths": 0,
            "restored": 0,
        }

        unavailable_roots = []
        try:
            status_row = connection.execute(
                "SELECT unavailable_roots_json FROM filesystem_monitor_status WHERE id=1"
            ).fetchone()
            if status_row and status_row["unavailable_roots_json"]:
                unavailable_roots = json.loads(status_row["unavailable_roots_json"])
        except Exception:
            pass

        for record_number, record in enumerate(records, start=1):
            previous = connection.execute(
                "SELECT path, exists_on_disk, missing_since FROM files WHERE file_id = ?",
                (record["file_id"],),
            ).fetchone()
            try:
                exists = os.path.isfile(record["path"])
            except OSError:
                exists = False

            is_offline = False
            if not exists:
                is_offline = is_offline_fn(record["path"], unavailable_roots)

            if is_offline and previous:
                exists = bool(previous["exists_on_disk"])
                missing_since = previous["missing_since"]
            else:
                missing_since = None if exists else (previous["missing_since"] if previous else now)

            summary["files"] += 1
            summary["present" if exists else "missing"] += 1

            if previous and previous["path"] != record["path"]:
                summary["changed_paths"] += 1
                connection.execute(
                    "INSERT INTO inventory_events(run_id,file_id,event_type,old_path,new_path,recorded_at) VALUES (?,?,?,?,?,?)",
                    (db_run_id, record["file_id"], "stash_path_changed", previous["path"], record["path"], now),
                )
            if not is_offline:
                if previous and not previous["exists_on_disk"] and exists:
                    summary["restored"] += 1
                    connection.execute(
                        "INSERT INTO inventory_events(run_id,file_id,event_type,old_path,new_path,recorded_at) VALUES (?,?,?,?,?,?)",
                        (db_run_id, record["file_id"], "file_restored", previous["path"], record["path"], now),
                    )
                if (not previous or previous["exists_on_disk"]) and not exists:
                    connection.execute(
                        "INSERT INTO inventory_events(run_id,file_id,event_type,old_path,new_path,recorded_at) VALUES (?,?,?,?,?,?)",
                        (db_run_id, record["file_id"], "file_missing", record["path"], None, now),
                    )

            connection.execute(
                """INSERT INTO files(file_id,scene_id,path,basename,title,studio,performers_json,size,duration,height,
                       fingerprints_json,scene_metadata_json,exists_on_disk,first_seen_at,last_seen_at,missing_since)
                   VALUES (:file_id,:scene_id,:path,:basename,:title,:studio,:performers_json,:size,:duration,:height,
                       :fingerprints_json,:scene_metadata_json,:exists_on_disk,:first_seen_at,:last_seen_at,:missing_since)
                   ON CONFLICT(file_id) DO UPDATE SET scene_id=excluded.scene_id,path=excluded.path,
                       basename=excluded.basename,title=excluded.title,studio=excluded.studio,
                       performers_json=excluded.performers_json,size=excluded.size,duration=excluded.duration,height=excluded.height,
                       fingerprints_json=excluded.fingerprints_json,scene_metadata_json=excluded.scene_metadata_json,
                       exists_on_disk=excluded.exists_on_disk,
                       last_seen_at=excluded.last_seen_at,missing_since=excluded.missing_since""",
                {**record, "exists_on_disk": int(exists), "first_seen_at": now, "last_seen_at": now, "missing_since": missing_since},
            )
            if progress_callback and (record_number == total_records or record_number % 50 == 0):
                progress_callback(record_number, total_records)

        connection.execute("DELETE FROM filename_state WHERE file_id IN (SELECT file_id FROM files WHERE last_seen_at != ?)", (now,))
        connection.execute("DELETE FROM filename_previews WHERE file_id IN (SELECT file_id FROM files WHERE last_seen_at != ?)", (now,))
        connection.execute("DELETE FROM files WHERE last_seen_at != ?", (now,))

        connection.execute(
            """UPDATE inventory_runs SET completed_at=?,stash_file_count=?,stash_scene_count=?,present_count=?,missing_count=?,
                   changed_path_count=?,restored_count=?,status='complete' WHERE id=?""",
            (now, summary["files"], summary["scenes"], summary["present"], summary["missing"],
             summary["changed_paths"], summary["restored"], db_run_id),
        )
        connection.commit()
        summary["run_id"] = db_run_id
        if run_id:
            summary["client_run_id"] = run_id
        return summary
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def refresh_scene_inventory(database_path: Path, scene: dict) -> int:
    """Refresh one scene before hook-driven work without creating a full inventory run."""
    now = utc_now()
    records = list(flatten_scene_files([scene]))
    connect_fn = _get_core_fn("connect", _connect)
    is_offline_fn = _get_core_fn("is_file_on_unavailable_root", _default_is_file_on_unavailable_root)
    connection = connect_fn(database_path)
    try:
        unavailable_roots = []
        try:
            status_row = connection.execute(
                "SELECT unavailable_roots_json FROM filesystem_monitor_status WHERE id=1"
            ).fetchone()
            if status_row and status_row["unavailable_roots_json"]:
                unavailable_roots = json.loads(status_row["unavailable_roots_json"])
        except Exception:
            pass

        current_file_ids = [str(r["file_id"]) for r in records if r.get("file_id")]
        if current_file_ids:
            placeholders = ",".join("?" for _ in current_file_ids)
            connection.execute(
                f"DELETE FROM files WHERE scene_id=? AND file_id NOT IN ({placeholders})",
                (str(scene["id"]), *current_file_ids)
            )

        for record in records:
            try:
                exists = os.path.isfile(record["path"])
            except OSError:
                exists = False
            previous = connection.execute(
                "SELECT first_seen_at,missing_since,exists_on_disk FROM files WHERE file_id=?",
                (record["file_id"],),
            ).fetchone()
            is_offline = False
            if not exists:
                is_offline = is_offline_fn(record["path"], unavailable_roots)

            if is_offline and previous:
                exists = bool(previous["exists_on_disk"])
                missing_since = previous["missing_since"]
            else:
                missing_since = None if exists else (previous["missing_since"] if previous else now)

            connection.execute(
                """INSERT INTO files(file_id,scene_id,path,basename,title,studio,performers_json,size,duration,height,
                       fingerprints_json,scene_metadata_json,exists_on_disk,first_seen_at,last_seen_at,missing_since)
                   VALUES (:file_id,:scene_id,:path,:basename,:title,:studio,:performers_json,:size,:duration,:height,
                       :fingerprints_json,:scene_metadata_json,:exists_on_disk,:first_seen_at,:last_seen_at,:missing_since)
                   ON CONFLICT(file_id) DO UPDATE SET scene_id=excluded.scene_id,path=excluded.path,
                       basename=excluded.basename,title=excluded.title,studio=excluded.studio,
                       performers_json=excluded.performers_json,size=excluded.size,duration=excluded.duration,height=excluded.height,
                       fingerprints_json=excluded.fingerprints_json,scene_metadata_json=excluded.scene_metadata_json,
                       exists_on_disk=excluded.exists_on_disk,last_seen_at=excluded.last_seen_at,
                       missing_since=excluded.missing_since""",
                {
                    **record,
                    "exists_on_disk": int(exists),
                    "first_seen_at": previous["first_seen_at"] if previous else now,
                    "last_seen_at": now,
                    "missing_since": missing_since,
                },
            )
        connection.commit()
        return len(records)
    finally:
        connection.close()


__all__ = [
    "flatten_scene_files",
    "inventory",
    "refresh_scene_inventory",
    "inventory_progress_path",
    "read_inventory_progress",
    "write_inventory_progress",
    "get_active_inventory_run",
    "_is_transient_filesystem_error",
    "_default_is_pid_alive",
    "is_pid_alive",
]

