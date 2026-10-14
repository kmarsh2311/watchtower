"""Inventory operations and scene record management service for Watchtower.

This module is responsible for:
- Normalizing Stash scene payloads into flat file record dictionaries (flatten_scene_files)
- Performing full library inventory sweeps, recording run lifecycles, detecting missing/restored
  files, offline root awareness, tracking path transitions, and purging stale records (inventory)
- Performing targeted single-scene inventory refreshes for hook events (refresh_scene_inventory)
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Union

from librarymanager.database.connection import create_connection
from librarymanager.database.migrations import ensure_schema
from librarymanager.scanning.filesystem import is_file_on_unavailable_root as _default_is_file_on_unavailable_root

logger = logging.getLogger("librarymanager.services.inventory")


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
) -> dict:
    """Execute a full library inventory sweep across the provided Stash scenes."""
    now = utc_now()
    connect_fn = _get_core_fn("connect", _connect)
    is_offline_fn = _get_core_fn("is_file_on_unavailable_root", _default_is_file_on_unavailable_root)
    connection = connect_fn(database_path)
    try:
        run_id = connection.execute(
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
                    (run_id, record["file_id"], "stash_path_changed", previous["path"], record["path"], now),
                )
            if not is_offline:
                if previous and not previous["exists_on_disk"] and exists:
                    summary["restored"] += 1
                    connection.execute(
                        "INSERT INTO inventory_events(run_id,file_id,event_type,old_path,new_path,recorded_at) VALUES (?,?,?,?,?,?)",
                        (run_id, record["file_id"], "file_restored", previous["path"], record["path"], now),
                    )
                if (not previous or previous["exists_on_disk"]) and not exists:
                    connection.execute(
                        "INSERT INTO inventory_events(run_id,file_id,event_type,old_path,new_path,recorded_at) VALUES (?,?,?,?,?,?)",
                        (run_id, record["file_id"], "file_missing", record["path"], None, now),
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
             summary["changed_paths"], summary["restored"], run_id),
        )
        connection.commit()
        summary["run_id"] = run_id
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
