"""Read-only inventory storage for Stash Library Manager."""

from __future__ import annotations

import logging
logger = logging.getLogger("librarymanager.core")

import json
import hashlib
import os
import sqlite3
import struct
import re
import unicodedata
import tempfile
import subprocess
import shutil
import glob
import sys
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".3gp", ".asf", ".avi", ".divx", ".flv", ".m2ts", ".m4v", ".mkv",
    ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ogm", ".ogv", ".rm",
    ".rmvb", ".ts", ".vob", ".webm", ".wmv",
}
ASSOCIATED_EXTENSIONS = {".funscript", ".srt", ".vtt", ".scc", ".ttml", ".dfxp", ".lrc", ".txt"}
IMAGE_SIDECAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
COMPANION_EXTENSIONS = {
    ".funscript", ".srt", ".vtt", ".scc", ".ttml", ".dfxp", ".lrc", ".txt",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".nfo", ".json", ".xml", ".sub", ".idx",
    ".csm.jpg", ".csm.png", ".csm.webp",
}
TEMPORARY_DOWNLOAD_EXTENSIONS = {
    ".part", ".partial", ".crdownload", ".download", ".tmp", ".temp", ".!qb", ".mega", ".aria2",
}
SYSTEM_JUNK_FILENAMES = {".ds_store", "thumbs.db", "desktop.ini"}
ACTIVE_INCOMING_LIFECYCLE_STATUSES = {
    "waiting", "downloading", "scanning", "generating_sheet", "renaming",
}


def is_temporary_download(path) -> bool:
    """Return True if the file path represents a temporary or partial browser/downloader file."""
    from librarymanager.scanning.filesystem import is_temporary_download as _impl
    return _impl(path)


def is_actionable_incoming_file(path, active_status: str | None = None) -> bool:
    """Return True if path is a completed, settled video or companion file ready for organiser action."""
    from librarymanager.scanning.filesystem import is_actionable_incoming_file as _impl
    return _impl(path, active_status=active_status)


def is_verified_companion_destination(connection, destination_path: str, source_path: str | None = None) -> bool:
    """Check if a moved companion file exists at destination and pairs with an active scene video."""
    from librarymanager.scanning.filesystem import is_verified_companion_destination as _impl
    return _impl(connection, destination_path, source_path=source_path)


def _sidecar_match_key(name: str) -> str:
    """Normalize text by stripping combining accents and non-alphanumeric punctuation (brackets, dashes, etc.)."""
    from librarymanager.scanning.filesystem import _sidecar_match_key as _impl
    return _impl(name)


def is_source_companion_of_moved_video(connection, source_path: str) -> bool:
    """Resolve a companion deletion only from the current verified video reconnection."""
    from librarymanager.scanning.filesystem import is_source_companion_of_moved_video as _impl
    return _impl(connection, source_path)


def get_companion_files(video_path: Path | str) -> list[str]:
    """Return a list of companion filenames associated with a video file on disk."""
    from librarymanager.scanning.filesystem import get_companion_files as _impl
    return _impl(video_path)


def associated_file_target(candidate: Path, current: Path, proposed: Path) -> Path | None:
    """Return a companion's target while preserving its existing naming convention (case-insensitively)."""
    from librarymanager.scanning.filesystem import associated_file_target as _impl
    return _impl(candidate, current, proposed)

associated_file_target._is_watchtower_wrapper = True


@contextmanager
def rename_lock(database_path: Path):
    """Serialize file operations from overlapping Stash hooks."""
    from librarymanager.services.renaming import rename_lock as _impl
    with _impl(database_path):
        yield

rename_lock._is_watchtower_wrapper = True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


CURRENT_SCHEMA_VERSION = 3


def _safe_alter(connection: "sqlite3.Connection", table: str, column: str, ddl: str) -> None:
    from librarymanager.database.migrations import _safe_alter as _impl_safe_alter
    _impl_safe_alter(connection, table, column, ddl)


def _ensure_schema(connection: "sqlite3.Connection", database_path: Path | str) -> int:
    from librarymanager.database.migrations import ensure_schema
    return ensure_schema(connection, database_path)


def connect(database_path: Path) -> sqlite3.Connection:
    from librarymanager.database.connection import create_connection
    connection = create_connection(database_path)
    _ensure_schema(connection, database_path)
    return connection


def prune_operational_records(database_path: Path) -> dict:
    """Safely bound historical operational tables without pruning pending/actionable records.
    - Preserves all pending proposals, active incoming files, and unreviewed events indefinitely.
    - Limits historical completed/resolved logs to recent bounded counts for diagnosis/audit.
    - Runs incrementally without expensive full-table scans on every refresh.
    """
    connection = connect(database_path)
    pruned = {}
    try:
        # 1. Resolved filesystem events (keep pending indefinitely, bound resolved to 2,000)
        cur = connection.execute(
            """DELETE FROM filesystem_events
               WHERE status != 'pending'
                 AND rowid NOT IN (
                     SELECT rowid FROM filesystem_events
                     WHERE status != 'pending'
                     ORDER BY rowid DESC LIMIT 2000
                 )"""
        )
        pruned["filesystem_events"] = cur.rowcount

        # 2. Resolved filing proposals (keep pending and needs_recovery indefinitely, bound resolved to 1,000)
        cur = connection.execute(
            """DELETE FROM filing_proposals
               WHERE status NOT IN ('pending', 'needs_recovery')
                 AND id NOT IN (
                     SELECT id FROM filing_proposals
                     WHERE status NOT IN ('pending', 'needs_recovery')
                     ORDER BY id DESC LIMIT 1000
                 )"""
        )
        pruned["filing_proposals"] = cur.rowcount

        # 3. Reconciliation runs and proposals (keep active/running, bound completed runs to 50)
        connection.execute(
            """DELETE FROM filesystem_reconciliation_proposals
               WHERE run_id IN (
                   SELECT id FROM filesystem_reconciliation_runs
                   WHERE status != 'running'
                     AND id NOT IN (
                         SELECT id FROM filesystem_reconciliation_runs
                         ORDER BY id DESC LIMIT 50
                     )
               )"""
        )
        cur = connection.execute(
            """DELETE FROM filesystem_reconciliation_runs
               WHERE status != 'running'
                 AND id NOT IN (
                     SELECT id FROM filesystem_reconciliation_runs
                     ORDER BY id DESC LIMIT 50
                 )"""
        )
        pruned["reconciliation_runs"] = cur.rowcount

        # 4. Duplicate repairs (bound completed to 1,000)
        cur = connection.execute(
            """DELETE FROM duplicate_file_repairs
               WHERE status != 'processing'
                 AND candidate_path NOT IN (
                     SELECT candidate_path FROM duplicate_file_repairs
                     WHERE status != 'processing'
                     ORDER BY rowid DESC LIMIT 1000
                 )"""
        )
        pruned["duplicate_repairs"] = cur.rowcount

        # 5. Stale incoming files (keep active states indefinitely, bound imported/ignored to 1,000)
        cur = connection.execute(
            """DELETE FROM incoming_files
               WHERE status IN ('imported', 'ignored')
                 AND path NOT IN (
                     SELECT path FROM incoming_files
                     WHERE status IN ('imported', 'ignored')
                     ORDER BY rowid DESC LIMIT 1000
                 )"""
        )
        pruned["incoming_files"] = cur.rowcount

        connection.commit()
        return pruned
    finally:
        connection.close()


def record_activity(database_path: Path, category: str, action: str, status: str, *, severity: str = "info",
                    scene_id: str | None = None, file_id: str | None = None, old_path: str | None = None,
                    new_path: str | None = None, detail: str = "", metadata: dict | None = None):
    """Append one durable, human-readable audit event.
    
    After inserting, prunes the table to the most recent 5,000 rows so the
    database doesn't grow unboundedly during long-running sessions.
    """
    connection = connect(database_path)
    try:
        connection.execute(
            """INSERT INTO activity_log(category,severity,action,status,scene_id,file_id,old_path,new_path,
                   detail,metadata_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (category, severity, action, status, str(scene_id) if scene_id is not None else None,
             str(file_id) if file_id is not None else None, old_path, new_path, detail,
             json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True), utc_now()),
        )
        # Prune oldest rows beyond the keep limit — runs in the same transaction,
        # so it only fires when there is actually something to delete.
        connection.execute(
            """DELETE FROM activity_log WHERE id NOT IN (
                   SELECT id FROM activity_log ORDER BY id DESC LIMIT 5000
               )"""
        )
        connection.commit()
    finally:
        connection.close()


def record_monitor_lifecycle(database_path: Path, action: str, status: str, detail: str = "", metadata: dict | None = None) -> bool:
    """Append a monitor lifecycle audit event (e.g. MONITOR STARTED, MONITOR STOPPED).

    Relies on process ownership and lifecycle transitions to prevent duplicates.
    """
    record_activity(
        database_path, "monitor", action, status,
        severity="info", detail=detail, metadata=metadata or {}
    )
    return True


def recent_activity(database_path: Path, limit: int = 250) -> list[dict]:
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 2000)),)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            result.append(item)
        return result
    finally:
        connection.close()


def _is_subpath_of(path: Path | str, parent: Path | str) -> bool:
    """Return True if path is equal to or inside parent directory without touching the disk."""
    from librarymanager.scanning.filesystem import _is_subpath_of as _impl
    return _impl(path, parent)



def get_configured_incoming_folders(config: dict | None) -> list[str]:
    """Return a cleaned list of configured incoming folder paths."""
    if not config or not isinstance(config, dict):
        return []
    raw = config.get("incomingFolders") or ([config.get("incomingFolder")] if config.get("incomingFolder") else [])
    folders = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, (str, Path)) and str(item).strip():
                folders.append(str(item).strip())
    elif isinstance(raw, str) and raw.strip():
        folders.append(raw.strip())
    return folders


def incoming_summary(database_path: Path, config: dict = None) -> dict:
    connection = connect(database_path)
    try:
        counts = {row["status"]: row["count"] for row in connection.execute(
            "SELECT status,COUNT(*) AS count FROM incoming_files GROUP BY status"
        )}
        latest = connection.execute(
            "SELECT path,status,detail,last_checked_at FROM incoming_files ORDER BY last_checked_at DESC LIMIT 1"
        ).fetchone()
        active = []
        now = datetime.now().timestamp()
        baseline_count_row = connection.execute("SELECT COUNT(*) AS count FROM filing_incoming_baseline").fetchone()
        baseline_count = baseline_count_row["count"] if baseline_count_row else 0
        baseline_state = connection.execute(
            "SELECT status FROM filing_baseline_state ORDER BY id DESC LIMIT 1"
        ).fetchone()
        baseline_summary = connection.execute(
            "SELECT initial_count,completed_at FROM filing_baseline_summary WHERE id=1"
        ).fetchone()
        baseline_established = bool(baseline_state and baseline_state["status"] == "complete")
        incoming_folders = get_configured_incoming_folders(config)
        backlog_eligible_count = 0
        b_paths_set = set()
        active_props = {
            p["source_path"]: p["status"]
            for p in connection.execute("SELECT source_path, status FROM filing_proposals").fetchall()
        }

        renamed_paths = {}
        for rename_row in connection.execute(
            """SELECT old_path,new_path FROM activity_log
               WHERE old_path IS NOT NULL AND new_path IS NOT NULL
                 AND status IN ('renamed','complete')
               ORDER BY id ASC"""
        ).fetchall():
            renamed_paths[str(rename_row["old_path"])] = str(rename_row["new_path"])

        def current_recorded_path(original_path: str) -> str:
            candidate = original_path
            visited = set()
            while candidate in renamed_paths and candidate not in visited:
                visited.add(candidate)
                candidate = renamed_paths[candidate]
            return candidate if Path(candidate).is_file() else original_path

        cold_roots = [
            os.path.normpath(str(c))
            for c in parse_cold_storage_roots((config or {}).get("coldStorageRoots"))
            if str(c).strip()
        ]

        if baseline_count > 0:
            b_rows = connection.execute("SELECT path FROM filing_incoming_baseline").fetchall()
            for b_row in b_rows:
                b_path_str = b_row["path"]
                cur_b_path = current_recorded_path(b_path_str)
                b_paths_set.add(cur_b_path)
                b_paths_set.add(b_path_str)
                b_p = Path(cur_b_path)
                if b_p.suffix.lower() in VIDEO_EXTENSIONS:
                    p_st = active_props.get(cur_b_path) or active_props.get(b_path_str)
                    if p_st in ("completed", "pending", "needs_recovery"):
                        continue
                    try:
                        is_inside = any(_is_subpath_of(b_p, f) for f in incoming_folders) if incoming_folders else False
                    except Exception:
                        is_inside = False
                    if is_inside and b_p.is_file():
                        backlog_eligible_count += 1

        for ign_row in connection.execute("SELECT path FROM incoming_files WHERE status='ignored'").fetchall():
            ign_p_str = ign_row["path"]
            if ign_p_str and ign_p_str not in b_paths_set:
                p_st = active_props.get(ign_p_str)
                if p_st in ("completed", "pending", "needs_recovery"):
                    continue
                ign_p = Path(ign_p_str)
                if ign_p.suffix.lower() in VIDEO_EXTENSIONS:
                    try:
                        is_inside = any(_is_subpath_of(ign_p, f) for f in incoming_folders) if incoming_folders else False
                    except Exception:
                        is_inside = False
                    if is_inside and ign_p.is_file():
                        backlog_eligible_count += 1
                        b_paths_set.add(ign_p_str)

        if incoming_folders:
            active_incoming_states = {
                r["path"]: r["status"] for r in connection.execute("SELECT path, status FROM incoming_files").fetchall()
            }
            discovered_files = discover_incoming_files_cached(
                incoming_folders,
                active_incoming_states=active_incoming_states,
                force_refresh=False
            )
            for disc in discovered_files:
                disc_p_str = disc.get("path")
                if disc_p_str and disc_p_str not in b_paths_set:
                    p_st = active_props.get(disc_p_str)
                    if p_st in ("completed", "pending", "needs_recovery"):
                        continue
                    disc_p = Path(disc_p_str)
                    if disc_p.suffix.lower() in VIDEO_EXTENSIONS and disc_p.is_file():
                        backlog_eligible_count += 1
                        b_paths_set.add(disc_p_str)

        for row in connection.execute(
            """SELECT path,status,size,stable_since,settle_seconds,attempts,scan_job_id,detail,filing_diagnostic,last_checked_at
               FROM incoming_files WHERE status IN ('waiting','scanning','failed','downloading','generating_sheet','unmatched','ignored','imported')
               ORDER BY CASE status
                   WHEN 'failed' THEN 0
                   WHEN 'unmatched' THEN 1
                   WHEN 'waiting' THEN 2
                   WHEN 'downloading' THEN 3
                   WHEN 'generating_sheet' THEN 4
                   WHEN 'scanning' THEN 5
                   WHEN 'ignored' THEN 6
                   WHEN 'imported' THEN 7
                   ELSE 8
               END, last_checked_at DESC LIMIT 50"""
        ):
            item = dict(row)
            p_obj = Path(item["path"])
            p_norm = os.path.normpath(str(p_obj))
            is_cold = bool(cold_roots and any(
                p_norm == c or p_norm.startswith(c + os.sep) or c.startswith(p_norm + os.sep)
                for c in cold_roots
            ))

            # Check if physically located inside a configured incoming folder
            is_inside_incoming = True
            if incoming_folders:
                is_inside_incoming = any(
                    _is_subpath_of(p_obj, f)
                    for f in incoming_folders
                )
            item["is_in_incoming_folder"] = is_inside_incoming

            if is_cold or not is_inside_incoming:
                is_file = False
            else:
                try:
                    is_file = p_obj.is_file()
                except OSError:
                    is_file = False
            item["exists_on_disk"] = is_file

            f_row = connection.execute(
                "SELECT scene_id, file_id, path FROM files WHERE path=? OR basename=?",
                (item["path"], p_obj.name)
            ).fetchone()
            item_scene_id = str(f_row["scene_id"]) if f_row and f_row["scene_id"] else None
            if not item_scene_id and item.get("detail"):
                m_sc = re.search(r"scene\s+(\d+)", str(item["detail"]), re.I)
                if m_sc:
                    item_scene_id = m_sc.group(1)
            item["scene_id"] = item_scene_id

            prop_row = connection.execute(
                """SELECT status, proposed_path, destination_folder FROM filing_proposals
                   WHERE source_path=? OR proposed_path=? OR (scene_id IS NOT NULL AND scene_id=?)
                   ORDER BY CASE status
                       WHEN 'needs_recovery' THEN 0
                       WHEN 'pending' THEN 1
                       ELSE 2
                   END, id DESC LIMIT 1""",
                (item["path"], item["path"], item_scene_id or "")
            ).fetchone()
            if prop_row:
                item["filing_status"] = prop_row["status"]
                item["filed"] = (prop_row["status"] == "completed")
                item["needs_recovery"] = (prop_row["status"] == "needs_recovery")
                item["has_pending_proposal"] = (prop_row["status"] == "pending")
            else:
                item["filing_status"] = None
                item["filed"] = False
                item["needs_recovery"] = False
                item["has_pending_proposal"] = False

            is_bl = connection.execute("SELECT 1 FROM filing_incoming_baseline WHERE path=?", (item["path"],)).fetchone() is not None
            item["is_baseline"] = is_bl

            ext = p_obj.suffix.lower()
            item["is_video"] = ext in {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".webm", ".flv", ".ts", ".m2ts"}

            # If imported and already filed, or outside incoming folders, or not on disk, exclude from active pending list
            if item["status"] == "imported":
                if item["filed"] or not is_inside_incoming or not is_file:
                    continue

            stable_ts = float(item["stable_since"] or now)
            settle_dur = int(item["settle_seconds"] or 300)
            deadline = stable_ts + settle_dur
            item["settling_deadline"] = deadline if item["status"] == "waiting" else None
            item["remaining_seconds"] = max(0, int(deadline - now)) if item["status"] == "waiting" else 0
            active.append(item)

        for r_row in connection.execute(
            """SELECT scene_id,enqueued_at,available_at,status FROM rename_queue WHERE status IN ('pending','processing')"""
        ).fetchall():
            sc_id = r_row["scene_id"]
            f_row = connection.execute("SELECT basename,path,title,studio,performers_json FROM files WHERE scene_id=? AND exists_on_disk=1 LIMIT 1", (sc_id,)).fetchone()
            bname = f_row["basename"] if f_row else f"Scene {sc_id}"
            is_proc = r_row["status"] == "processing"
            rem = 0 if is_proc else max(0, int(r_row["available_at"] - now))
            
            proposed_name = None
            if f_row:
                try:
                    title = f_row["title"] or Path(f_row["path"]).stem
                    studio = f_row["studio"]
                    perfs = json.loads(f_row["performers_json"] or "[]")
                    ext = Path(f_row["path"]).suffix
                    stem = _proposed_stem(title, studio, perfs)
                    if stem:
                        proposed_name = stem + ext
                except Exception:
                    proposed_name = None

            # If not actively processing and proposed filename is already identical to current filename,
            # auto-clear it from the queue so we do not show a pointless countdown.
            if not is_proc and proposed_name and proposed_name == bname:
                try:
                    connection.execute("DELETE FROM rename_queue WHERE scene_id=? AND status='pending'", (str(sc_id),))
                    connection.commit()
                except Exception:
                    pass
                continue

            active.append({
                "path": f"scene://{sc_id}/{bname}",
                "scene_id": str(sc_id),
                "current_name": bname,
                "proposed_name": proposed_name or bname,
                "status": "renaming" if is_proc else "pending_rename",
                "remaining_seconds": rem,
                "stable_since": r_row["enqueued_at"],
                "settle_seconds": int(r_row["available_at"] - r_row["enqueued_at"]),
                "detail": "Applying filename in Stash" if is_proc else (f"Proposed: {proposed_name}" if proposed_name else "Waiting for metadata edits to settle"),
                "last_checked_at": r_row["enqueued_at"],
            })
        return {
            "waiting": counts.get("waiting", 0),
            "scanning": counts.get("scanning", 0),
            "imported": counts.get("imported", 0),
            "failed": counts.get("failed", 0),
            "downloading": counts.get("downloading", 0),
            "baseline_count": baseline_count,
            "baseline_established": baseline_established,
            "baseline_completed": bool(baseline_summary and baseline_summary["completed_at"]),
            "baseline_initial_count": int(baseline_summary["initial_count"] or 0) if baseline_summary else baseline_count,
            "backlog_eligible_count": backlog_eligible_count,
            "latest": dict(latest) if latest else None,
            "active": active,
        }
    finally:
        connection.close()


def expect_filesystem_move(database_path: Path, source: str, destination: str, ttl_seconds: float = 60):
    from librarymanager.scanning.filesystem import expect_filesystem_move as _impl
    return _impl(database_path, source, destination, ttl_seconds=ttl_seconds)


def consume_expected_move(database_path: Path, source: str, destination: str) -> bool:
    from librarymanager.scanning.filesystem import consume_expected_move as _impl
    return _impl(database_path, source, destination)


def consume_expected_move_source(database_path: Path, source: str) -> bool:
    from librarymanager.scanning.filesystem import consume_expected_move_source as _impl
    return _impl(database_path, source)


def expect_filesystem_delete(database_path: Path, path: str, ttl_seconds: float = 60):
    """Mark one explicit deletion so the monitor does not report it as external."""
    from librarymanager.scanning.filesystem import expect_filesystem_delete as _impl
    return _impl(database_path, path, ttl_seconds=ttl_seconds)


def cancel_expected_filesystem_delete(database_path: Path, path: str):
    """Remove an expected-deletion marker when the deletion itself failed."""
    from librarymanager.scanning.filesystem import cancel_expected_filesystem_delete as _impl
    return _impl(database_path, path)


def consume_expected_move_destination(database_path: Path, destination: str) -> bool:
    from librarymanager.scanning.filesystem import consume_expected_move_destination as _impl
    return _impl(database_path, destination)


def expect_filesystem_create(database_path: Path, path: str, ttl_seconds: float = 120):
    from librarymanager.scanning.filesystem import expect_filesystem_create as _impl
    return _impl(database_path, path, ttl_seconds=ttl_seconds)


def consume_expected_create(database_path: Path, path: str) -> bool:
    from librarymanager.scanning.filesystem import consume_expected_create as _impl
    return _impl(database_path, path)


def annotate_pending_events_processing_state(
    pending_events: list[dict],
    active_moves: list[dict],
    monitor_running: bool = True,
    grace_seconds: float = 5.0,
    current_time: float | None = None,
    connection=None,
    database_path: Path | None = None,
) -> list[dict]:
    """Annotate pending filesystem events with in-flight worker processing states.

    - Moves actively being reconnected by MoveWorker get processing_state='reconnecting'.
    - Queued moves get processing_state='queued' (presented as reconnecting).
    - Moves deferred for retry get processing_state='deferred'.
    - Companion files colocated with active/queued/pending video moves get processing_state='reconnecting'.
    - Companion files arriving before their video within a bounded 5-second grace period
      get processing_state='waiting_video' ('WAITING FOR VIDEO').
    - Ambiguous companions (multiple candidate videos) and standalone JPGs (no associated video)
      never enter the grace period and remain visible under Needs Attention (processing_state=None).
    - If the grace period expires without a video move, or if the monitor stops/crashes,
      processing_state is None so the event surfaces in Needs Attention.
    """
    if not monitor_running:
        for ev in pending_events:
            ev["processing_state"] = None
        return pending_events

    move_map = {}
    video_destinations = []
    for m in (active_moves or []):
        src = m.get("source_path")
        dst = m.get("destination_path")
        if src and dst:
            move_map[(src, dst)] = m
        if dst:
            move_map[dst] = m
            video_destinations.append(m)
        if src:
            move_map[src] = m

    # Also index video moves present in pending_events (in case both moved simultaneously)
    pending_video_destinations = []
    for ev in pending_events:
        dst = ev.get("destination_path")
        ev_type = ev.get("event_type")
        if ev_type == "moved" and dst and Path(dst).suffix.lower() in VIDEO_EXTENSIONS:
            pending_video_destinations.append(ev)

    now_dt = datetime.fromtimestamp(current_time, tz=timezone.utc) if current_time else datetime.now(timezone.utc)

    for ev in pending_events:
        ev["processing_state"] = None
        src = ev.get("source_path")
        dst = ev.get("destination_path")

        # 1. Direct move match against active moves
        matched = None
        if src and dst and (src, dst) in move_map:
            matched = move_map[(src, dst)]
        elif dst and dst in move_map:
            matched = move_map[dst]
        elif src and src in move_map:
            matched = move_map[src]

        if matched:
            ev["processing_state"] = matched.get("status", "reconnecting")
            if "attempts" in matched:
                ev["processing_attempts"] = matched["attempts"]
            if "last_error" in matched:
                ev["processing_error"] = matched["last_error"]
            continue

        # 2. Check if this is a companion file
        if dst:
            try:
                cand = Path(dst)
                if cand.suffix.lower() in COMPANION_EXTENSIONS:
                    # 2a. Check if associated video is in active_moves
                    companion_video_found = False
                    for vm in video_destinations:
                        vid_dst = Path(vm["destination_path"])
                        if cand.parent == vid_dst.parent:
                            c_stem = cand.stem.lower()
                            v_stem = vid_dst.stem.lower()
                            c_name = cand.name.lower()
                            v_name = vid_dst.name.lower()
                            if c_stem == v_stem or c_name == v_name + cand.suffix.lower():
                                ev["processing_state"] = vm.get("status", "reconnecting")
                                ev["companion_of"] = vid_dst.name
                                companion_video_found = True
                                break
                    if companion_video_found:
                        continue

                    # 2b. Check if associated video is in pending_events (moved concurrently)
                    for pvm in pending_video_destinations:
                        pvid_dst = Path(pvm["destination_path"])
                        if cand.parent == pvid_dst.parent:
                            c_stem = cand.stem.lower()
                            pv_stem = pvid_dst.stem.lower()
                            c_name = cand.name.lower()
                            pv_name = pvid_dst.name.lower()
                            if c_stem == pv_stem or c_name == pv_name + cand.suffix.lower():
                                ev["processing_state"] = "reconnecting"
                                ev["companion_of"] = pvid_dst.name
                                companion_video_found = True
                                break
                    if companion_video_found:
                        continue

                    # 2c. Associated video hasn't been detected yet: evaluate if this is an early companion vs ambiguous/standalone
                    # Check if this is explicitly a compound companion (e.g. video.mp4.jpg)
                    is_compound = False
                    target_video = None
                    try:
                        stem_p = Path(cand.stem)
                        if stem_p.suffix.lower() in VIDEO_EXTENSIONS:
                            is_compound = True
                            target_video = cand.stem
                        elif src:
                            src_stem_p = Path(Path(src).stem)
                            if src_stem_p.suffix.lower() in VIDEO_EXTENSIONS:
                                is_compound = True
                                target_video = src_stem_p.name
                    except Exception:
                        pass

                    # Check for ambiguity in destination directory (multiple video files with matching stem)
                    is_ambiguous = False
                    dest_parent = cand.parent
                    try:
                        if dest_parent.is_dir():
                            dest_stem = cand.stem.lower()
                            matching_dest_vids = []
                            for vext in VIDEO_EXTENSIONS:
                                vp = dest_parent / (cand.stem + vext)
                                if vp.is_file():
                                    matching_dest_vids.append(vp)
                            if len(matching_dest_vids) > 1:
                                is_ambiguous = True
                    except Exception:
                        pass

                    # If ambiguous in destination, it can never be safely paired; keep in Needs Attention
                    if is_ambiguous:
                        continue

                    # Check if there is an associated video in source directory or database
                    has_video_association = is_compound
                    if not has_video_association:
                        # Check source directory on disk
                        try:
                            if src:
                                src_parent = Path(src).parent
                                if src_parent.is_dir():
                                    for vext in VIDEO_EXTENSIONS:
                                        if (src_parent / (Path(src).stem + vext)).is_file():
                                            has_video_association = True
                                            target_video = Path(src).stem + vext
                                            break
                        except Exception:
                            pass

                    if not has_video_association and (connection or database_path):
                        # Check database files table to see if a video with this stem was inventoried
                        try:
                            con = connection
                            should_close = False
                            if con is None and database_path:
                                con = connect(database_path)
                                should_close = True
                            try:
                                stem_query = cand.stem
                                rows = con.execute(
                                    """SELECT path FROM files
                                       WHERE (path LIKE ? OR path LIKE ?)
                                         AND exists_on_disk=1
                                       LIMIT 3""",
                                    (f"%/{stem_query}.%", f"%/{stem_query}")
                                ).fetchall()
                                video_rows = [r["path"] for r in rows if Path(r["path"]).suffix.lower() in VIDEO_EXTENSIONS]
                                if len(video_rows) == 1:
                                    has_video_association = True
                                    target_video = Path(video_rows[0]).name
                                elif len(video_rows) > 1:
                                    is_ambiguous = True
                            finally:
                                if should_close and con:
                                    con.close()
                        except Exception:
                            pass

                    # If ambiguous, or if it is a standalone JPG with no video association:
                    # Do not treat as waiting for video; leave processing_state = None (Needs Attention)
                    if is_ambiguous or not has_video_association:
                        continue

                    # Bounded grace window for verified early companion JPG waiting for its video
                    seen_str = ev.get("first_seen_at") or ev.get("last_seen_at")
                    age = None
                    if seen_str:
                        try:
                            dt = datetime.fromisoformat(seen_str)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            age = max(0.0, (now_dt - dt).total_seconds())
                        except Exception:
                            age = None

                    if age is not None and age <= grace_seconds:
                        ev["processing_state"] = "waiting_video"
                        ev["companion_of"] = target_video or cand.stem
                        continue
                    else:
                        # Grace period expired: leaves processing_state = None (Needs Attention)
                        pass
            except Exception:
                pass

        if (database_path or connection) and ev.get("event_type") == "created" and ev.get("source_path") and ev.get("processing_state") is None:
            cand_p = Path(ev["source_path"])
            if cand_p.suffix.lower() in VIDEO_EXTENSIONS:
                db_p = database_path
                if not db_p and connection:
                    try:
                        db_list = connection.execute("PRAGMA database_list").fetchall()
                        if db_list and db_list[0]["file"]:
                            db_p = Path(db_list[0]["file"])
                    except Exception:
                        pass
                if db_p:
                    dup = find_duplicate_scene_file(db_p, cand_p, allow_compute=False)
                    if dup:
                        ev["duplicate_info"] = dup
                        if dup.get("is_ambiguous"):
                            ev["event_subtype"] = "ambiguous_duplicate_detected"
                        elif dup.get("is_external_move"):
                            ev["event_subtype"] = "external_move_detected"
                        else:
                            ev["event_subtype"] = "duplicate_detected"

    return pending_events



def dashboard_data(database_path: Path, activity_limit: int = 100, stash=None, config: dict = None) -> dict:
    """Return the small read-only snapshot used by the central dashboard."""
    if config is None and stash is not None and hasattr(stash, "find_plugin_config"):
        try:
            config = stash.find_plugin_config("librarymanager") or {}
        except Exception:
            config = {}

    # Reconcile proposal state before reading Incoming so both sections describe
    # the same authoritative filing state within this response.
    filing_proposals = get_pending_filing_proposals(database_path, stash=stash)
    from librarymanager_reconciliation import list_review_batches
    grouped_reconciliation = list_review_batches(database_path)
    incoming = incoming_summary(database_path, config=config)
    connection = connect(database_path)
    try:
        inventory_row = connection.execute(
            "SELECT * FROM inventory_runs WHERE status='complete' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        queue_counts = {row["status"]: row["count"] for row in connection.execute(
            "SELECT status,COUNT(*) AS count FROM rename_queue GROUP BY status"
        )}
        failed_renames = []
        if queue_counts.get("failed", 0) > 0:
            failed_renames = [
                dict(row) for row in connection.execute(
                    """SELECT q.scene_id, q.enqueued_at, q.available_at, q.attempts, q.last_error,
                              COALESCE(f.path, '') AS path,
                              COALESCE(f.basename, '') AS basename,
                              COALESCE(f.title, '') AS title
                       FROM rename_queue q
                       LEFT JOIN (
                           SELECT scene_id, path, basename, title,
                                  ROW_NUMBER() OVER (PARTITION BY scene_id ORDER BY exists_on_disk DESC, last_seen_at DESC, CAST(file_id AS INTEGER) DESC) AS rn
                           FROM files
                       ) f ON f.scene_id = q.scene_id AND f.rn = 1
                       WHERE q.status = 'failed'
                       ORDER BY q.enqueued_at DESC LIMIT 50"""
                ).fetchall()
            ]
        preview_run = connection.execute(
            "SELECT * FROM filename_preview_runs WHERE status='complete' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        preview_rows = []
        if preview_run:
            preview_rows = [dict(row) for row in connection.execute(
                """SELECT file_id,scene_id,current_path,proposed_path,status,reason
                   FROM filename_previews WHERE run_id=? AND status!='unchanged'
                   ORDER BY CASE status WHEN 'conflict' THEN 0 ELSE 1 END,id LIMIT 200""",
                (preview_run["id"],),
            )]
        monitor = filesystem_monitor_summary(database_path)
        pending_events = pending_filesystem_events(database_path)
        is_running = monitor.get("state") == "running" and not monitor.get("is_stale") and monitor.get("pid_alive")
        annotate_pending_events_processing_state(pending_events, monitor.get("active_moves", []), monitor_running=is_running, database_path=database_path)
        missing_count_row = connection.execute(
            "SELECT COUNT(*) FROM files WHERE exists_on_disk = 0"
        ).fetchone()
        missing_files_count = int(missing_count_row[0]) if missing_count_row else 0
        unindexed_rows = connection.execute(
            "SELECT path, basename, size, discovered_at FROM unindexed_external_media WHERE ignored = 0 ORDER BY discovered_at DESC"
        ).fetchall()
        unindexed_media = [dict(r) for r in unindexed_rows if os.path.isfile(r["path"])]
        ignored_count_row = connection.execute(
            "SELECT count(*) FROM unindexed_external_media WHERE ignored = 1"
        ).fetchone()
        ignored_unindexed_count = int(ignored_count_row[0]) if ignored_count_row else 0
        scan_issues = [
            dict(row) for row in connection.execute(
                """SELECT a.id, a.category, a.severity, a.action, a.status, a.old_path, a.new_path, a.detail, a.metadata_json, a.recorded_at
                   FROM activity_log a
                   WHERE a.category IN ('reconciliation', 'incoming', 'scan', 'monitor')
                     AND (a.severity IN ('warning', 'error') OR a.status IN ('completed_with_errors', 'timed_out', 'incomplete', 'failed'))
                     AND NOT EXISTS (
                         SELECT 1 FROM activity_log s
                         WHERE s.id > a.id
                           AND s.category = a.category
                           AND s.action = a.action
                           AND (
                               (a.old_path IS NOT NULL AND a.old_path != '' AND s.old_path = a.old_path)
                               OR
                               ((a.old_path IS NULL OR a.old_path = '') AND (s.old_path IS NULL OR s.old_path = ''))
                           )
                           AND s.status IN ('complete', 'ok')
                           AND s.severity = 'info'
                     )
                   ORDER BY a.recorded_at DESC LIMIT 10"""
            ).fetchall()
        ]
        return {
            "inventory": dict(inventory_row) if inventory_row else None,
            "rename_queue": queue_counts,
            "failed_renames": failed_renames,
            "monitor": monitor,
            "activity": recent_activity(database_path, activity_limit),
            "filename_preview": {"run": dict(preview_run), "rows": preview_rows} if preview_run else None,
            "pending_events": pending_events,
            "grouped_reconciliation": grouped_reconciliation,
            "transcoder_candidates": pending_transcoder_candidates(database_path),
            "incoming": incoming,
            "filing_proposals": filing_proposals,
            "active_filing_transfers": get_active_filing_transfers(database_path),
            "filing_folder_mappings": get_filing_folder_mappings(database_path),
            "missing_files_count": missing_files_count,
            "unindexed_media": unindexed_media,
            "ignored_unindexed_count": ignored_unindexed_count,
            "scan_issues": scan_issues,
        }
    finally:
        connection.close()


def pending_filesystem_events(database_path: Path, limit: int = 50) -> list[dict]:
    """Return the exact unresolved events shown by the live dashboard."""
    connection = connect(database_path)
    try:
        rows = [dict(row) for row in connection.execute(
            """SELECT e.event_key,e.event_type,e.source_path,e.destination_path,e.is_directory,
                      e.first_seen_at,e.last_seen_at,e.event_count,f.scene_id,f.file_id
               FROM filesystem_events e
               LEFT JOIN files f ON f.path=e.source_path
               LEFT JOIN grouped_reconciliation_event_links grl ON grl.event_key=e.event_key
               LEFT JOIN grouped_reconciliation_batches grb ON grb.id=grl.batch_id
               WHERE e.status='pending'
                 AND (grl.event_key IS NULL OR grb.state IN ('resolved','dismissed'))
               ORDER BY e.last_seen_at DESC LIMIT ?""",
            (int(limit),),
        )]
        valid_rows = []
        to_resolve = []
        for r in rows:
            ev_type = r.get("event_type")
            src = r.get("source_path")
            dest = r.get("destination_path")
            resolved = False

            if ev_type == "deleted" and src:
                # 1. File returned to original location on disk (transient replacement/swap)
                try:
                    if Path(src).exists():
                        resolved = True
                except OSError:
                    pass

                if not resolved:
                    # 2. Reconnected to another location in Stash
                    fid = r.get("file_id")
                    if not fid:
                        act = connection.execute(
                            "SELECT file_id FROM activity_log WHERE old_path=? AND file_id IS NOT NULL ORDER BY recorded_at DESC LIMIT 1",
                            (src,)
                        ).fetchone()
                        if act:
                            fid = act["file_id"]
                    if not fid:
                        inv = connection.execute(
                            "SELECT file_id FROM inventory_events WHERE old_path=? ORDER BY id DESC LIMIT 1",
                            (src,)
                        ).fetchone()
                        if inv:
                            fid = inv["file_id"]
                    if fid:
                        # Ensure ONLY this specific file_id is reconnected (a different file in a multi-file scene will NOT resolve this)
                        f_row = connection.execute(
                            "SELECT path, exists_on_disk FROM files WHERE file_id=?",
                            (fid,)
                        ).fetchone()
                        if f_row and f_row["path"] != src and f_row["exists_on_disk"]:
                            try:
                                if Path(f_row["path"]).is_file():
                                    resolved = True
                            except OSError:
                                pass

                if not resolved:
                    try:
                        if is_source_companion_of_moved_video(connection, src):
                            resolved = True
                    except OSError:
                        pass

            elif ev_type == "created" and src:
                # Created file exists on disk and is cataloged in Stash's files inventory
                try:
                    if Path(src).is_file():
                        known = connection.execute(
                            "SELECT 1 FROM files WHERE path=? AND exists_on_disk=1",
                            (src,)
                        ).fetchone()
                        if known:
                            resolved = True
                except OSError:
                    pass

            elif ev_type == "moved" and dest:
                try:
                    if Path(dest).is_file():
                        dest_row = connection.execute(
                            "SELECT file_id FROM files WHERE path=? AND exists_on_disk=1",
                            (dest,)
                        ).fetchone()
                        if dest_row:
                            fid = r.get("file_id")
                            if not fid and src:
                                src_row = connection.execute("SELECT file_id FROM files WHERE path=?", (src,)).fetchone()
                                if src_row:
                                    fid = src_row["file_id"]
                            if not fid and src:
                                act = connection.execute(
                                    "SELECT file_id FROM activity_log WHERE old_path=? AND file_id IS NOT NULL ORDER BY recorded_at DESC LIMIT 1",
                                    (src,)
                                ).fetchone()
                                if act:
                                    fid = act["file_id"]
                            if not fid and src:
                                inv = connection.execute(
                                    "SELECT file_id FROM inventory_events WHERE old_path=? ORDER BY id DESC LIMIT 1",
                                    (src,)
                                ).fetchone()
                                if inv:
                                    fid = inv["file_id"]
                            if fid:
                                if dest_row["file_id"] == fid:
                                    resolved = True
                            else:
                                resolved = True
                        elif is_verified_companion_destination(connection, dest, src):
                            resolved = True
                except OSError:
                    pass

            if resolved:
                to_resolve.append(r["event_key"])
            else:
                valid_rows.append(r)

        if to_resolve:
            for k in to_resolve:
                connection.execute("UPDATE filesystem_events SET status='resolved' WHERE event_key=?", (k,))
            connection.commit()
        return valid_rows
    finally:
        connection.close()


def pending_transcoder_candidates(database_path: Path, limit: int = 250) -> list[dict]:
    """Return persistent neutral replacement candidates shown as work in progress."""
    connection = connect(database_path)
    try:
        return [dict(row) for row in connection.execute(
            """SELECT candidate_path,source_path,detected_at,updated_at,status,detail
               FROM transcoder_candidates WHERE status='waiting'
               ORDER BY detected_at LIMIT ?""", (int(limit),)
        )]
    finally:
        connection.close()


def promote_transcoder_candidate(database_path: Path, candidate_path: str):
    """Stop treating a candidate as a replacement and expose its created event for review."""
    connection = connect(database_path)
    try:
        connection.execute(
            """UPDATE transcoder_candidates SET status='independent',updated_at=?,
                      detail='User chose to treat this as an independent new file'
               WHERE candidate_path=?""",
            (utc_now(), str(candidate_path)),
        )
        connection.execute(
            "UPDATE filesystem_events SET status='pending' WHERE event_type='created' AND source_path=?",
            (str(candidate_path),),
        )
        connection.commit()
    finally:
        connection.close()


def flatten_scene_files(scenes):
    from librarymanager.services.inventory import flatten_scene_files as _impl
    return _impl(scenes)


flatten_scene_files._is_watchtower_wrapper = True


def parse_cold_storage_roots(raw) -> list[str]:
    """Parse configured cold storage root paths from JSON list, Python list, comma-separated, or newline-separated string."""
    from librarymanager.scanning.reconciliation import parse_cold_storage_roots as _impl
    return _impl(raw)


def is_file_on_unavailable_root(path_str: str, unavailable_roots=None) -> bool:
    """Check if a file's root or mount point is currently offline or unreachable.
    Uses normalized path prefix resolution against the configured unavailable roots.
    """
    from librarymanager.scanning.filesystem import is_file_on_unavailable_root as _impl
    return _impl(path_str, unavailable_roots=unavailable_roots)


def get_authoritative_unavailable_roots(database_path: Path = None, extra_roots: list = None) -> list[str]:
    """Return the list of configured or monitored roots that are currently offline or unreachable.
    Probes filesystem directly; discards stale monitor status entries if the root is now accessible.
    """
    from librarymanager.scanning.filesystem import get_authoritative_unavailable_roots as _impl
    return _impl(database_path=database_path, extra_roots=extra_roots)


def inventory(database_path: Path, scenes, progress_callback=None) -> dict:
    from librarymanager.services.inventory import inventory as _impl
    return _impl(database_path, scenes, progress_callback=progress_callback)


inventory._is_watchtower_wrapper = True


def refresh_scene_inventory(database_path: Path, scene: dict) -> int:
    """Refresh one scene before hook-driven work without creating a full inventory run."""
    from librarymanager.services.inventory import refresh_scene_inventory as _impl
    return _impl(database_path, scene)


refresh_scene_inventory._is_watchtower_wrapper = True


def _row_scene_date(row) -> str:
    """Return a validated ISO Stash scene date from an inventoried file row."""
    from librarymanager.scanning.naming_rules import _row_scene_date as _impl
    return _impl(row)


def scene_naming_signature(database_path: Path, scene_id: str, include_date: bool = False, include_quality: bool = False):
    """Return only metadata that is allowed to influence a filename."""
    from librarymanager.scanning.naming_rules import scene_naming_signature as _impl
    return _impl(database_path, scene_id, include_date, include_quality)



def _connect_short(database_path: Path) -> sqlite3.Connection:
    """Open a connection with a short write-lock timeout for hook-critical paths."""
    from librarymanager.services.renaming import _connect_short as _impl
    return _impl(database_path)

_connect_short._is_watchtower_wrapper = True


def enqueue_rename(database_path: Path, scene_id: str, now_timestamp: float, debounce_seconds: float = 1.0) -> bool:
    """Coalesce a scene's hooks and return True only when a worker must be scheduled."""
    from librarymanager.services.renaming import enqueue_rename as _impl
    return _impl(database_path, scene_id, now_timestamp, debounce_seconds=debounce_seconds)

enqueue_rename._is_watchtower_wrapper = True


def cancel_pending_rename(database_path: Path, scene_id: str) -> bool:
    """Cancel a pending rename for a scene and remove it from the rename queue."""
    from librarymanager.services.renaming import cancel_pending_rename as _impl
    return _impl(database_path, scene_id)

cancel_pending_rename._is_watchtower_wrapper = True


def make_pending_rename_due(database_path: Path, scene_id: str) -> bool:
    """Mark a queued or failed rename as pending and immediately due for execution."""
    from librarymanager.services.renaming import make_pending_rename_due as _impl
    return _impl(database_path, scene_id)

make_pending_rename_due._is_watchtower_wrapper = True


def release_worker_schedule(database_path: Path):
    """Reset the rename worker scheduled flag so future jobs can be scheduled."""
    from librarymanager.services.renaming import release_worker_schedule as _impl
    return _impl(database_path)

release_worker_schedule._is_watchtower_wrapper = True


def claim_due_rename(database_path: Path, now_timestamp: float):
    """Claim the oldest pending rename ready for execution, recovering orphaned renames."""
    from librarymanager.services.renaming import claim_due_rename as _impl
    return _impl(database_path, now_timestamp)

claim_due_rename._is_watchtower_wrapper = True


def finish_queued_rename(database_path: Path, scene_id: str, outcome: str, detail: str):
    """Mark a queued rename as completed, remove it from the queue, and record audit."""
    from librarymanager.services.renaming import finish_queued_rename as _impl
    return _impl(database_path, scene_id, outcome, detail)

finish_queued_rename._is_watchtower_wrapper = True


def fail_queued_rename(database_path: Path, scene_id: str, error: str):
    """Record a failure for a queued rename and log it in the audit table."""
    from librarymanager.services.renaming import fail_queued_rename as _impl
    return _impl(database_path, scene_id, error)

fail_queued_rename._is_watchtower_wrapper = True


def record_filesystem_event(database_path: Path, event_type: str, source_path: str,
                            destination_path: str | None = None, is_directory: bool = False,
                            initial_status: str = 'pending'):
    """Coalesce identical watcher events without taking any filesystem action."""
    now = utc_now()
    event_key = json.dumps([event_type, os.path.normpath(source_path),
                            os.path.normpath(destination_path) if destination_path else None,
                            bool(is_directory)], ensure_ascii=False)
    connection = connect(database_path)
    try:
        connection.execute(
            """INSERT INTO filesystem_events(event_key,event_type,source_path,destination_path,is_directory,
                   first_seen_at,last_seen_at,event_count,status) VALUES (?,?,?,?,?,?,?,1,?)
               ON CONFLICT(event_key) DO UPDATE SET last_seen_at=excluded.last_seen_at,
                   first_seen_at=CASE WHEN filesystem_events.status='reviewed'
                                      THEN excluded.first_seen_at ELSE filesystem_events.first_seen_at END,
                   event_count=filesystem_events.event_count+1,
                   status=excluded.status""",
            (event_key, event_type, source_path, destination_path, int(is_directory), now, now, initial_status),
        )
        connection.commit()
    finally:
        connection.close()


def resolve_filesystem_event(database_path: Path, event_type: str, source_path: str,
                             destination_path: str | None = None, is_directory: bool = False):
    """Remove a successfully handled event from the attention count while retaining its history."""
    event_key = json.dumps([event_type, os.path.normpath(source_path),
                            os.path.normpath(destination_path) if destination_path else None,
                            bool(is_directory)], ensure_ascii=False)
    connection = connect(database_path)
    try:
        connection.execute("UPDATE filesystem_events SET status='reviewed' WHERE event_key=?", (event_key,))
        connection.commit()
    finally:
        connection.close()


def _windows_pid_alive(pid: int) -> bool:
    """Probe a Windows PID without sending a signal to the target process."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        # Access denied still proves that the process exists.
        return ctypes.get_last_error() == 5
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            return _windows_pid_alive(int(pid))
        except (OSError, ValueError):
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        # A protected process still exists; macOS may deny signal probes from
        # the Stash plugin host even when both processes belong to the user.
        return True
    except (ProcessLookupError, ValueError):
        return False
    except OSError:
        return False


def _terminate_pid(pid: int | None) -> bool:
    """Forcefully terminate a known stale or hung process."""
    if not pid or int(pid) <= 0 or not _is_pid_alive(int(pid)):
        return False
    pid_int = int(pid)
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid_int)], capture_output=True, check=False)
            return True
        import signal
        os.kill(pid_int, signal.SIGTERM)
        time.sleep(0.2)
        if _is_pid_alive(pid_int):
            os.kill(pid_int, signal.SIGKILL)
        return True
    except (OSError, Exception):
        return False


def _pid_matches_monitor(pid: int | None, token: str | None):
    """Return True/False when process identity can be checked, otherwise None."""
    if not pid or not token:
        return False
    try:
        proc_cmdline = Path(f"/proc/{int(pid)}/cmdline")
        if proc_cmdline.is_file():
            command = proc_cmdline.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        elif sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            if result.returncode:
                return None
            command = result.stdout
        else:
            result = subprocess.run(["ps", "-ww", "-p", str(int(pid)), "-o", "command="],
                                    capture_output=True, text=True, timeout=3, check=False)
            if result.returncode:
                return None
            command = result.stdout
        return "librarymanager_monitor.py" in command and str(token) in command
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
def filesystem_monitor_summary(database_path: Path) -> dict:
    connection = connect(database_path)
    try:
        status = connection.execute("SELECT * FROM filesystem_monitor_status WHERE id=1").fetchone()
        if not status:
            return {"state": "stopped", "raw_state": "stopped", "is_stale": False, "heartbeat_age_seconds": None,
                    "pid": None, "pid_alive": False, "pid_matches_monitor": False,
                    "started_at": None, "token": None, "heartbeat_at": None,
                    "roots": [], "unavailable_roots": [], "pending_events": 0, "event_types": {}}
        counts = {row["event_type"]: row["count"] for row in connection.execute(
            "SELECT event_type,COUNT(*) AS count FROM filesystem_events WHERE status='pending' GROUP BY event_type"
        )}
        
        heartbeat_at = status["heartbeat_at"]
        heartbeat_age = None
        if heartbeat_at:
            try:
                hb_dt = datetime.fromisoformat(heartbeat_at)
                if hb_dt.tzinfo is None:
                    hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                heartbeat_age = max(0.0, (datetime.now(timezone.utc) - hb_dt).total_seconds())
            except Exception:
                heartbeat_age = None

        pid = status["pid"]
        pid_alive = _is_pid_alive(pid) if pid else False
        pid_matches_monitor = _pid_matches_monitor(pid, status["token"]) if pid_alive else False
        raw_state = status["state"] or "stopped"
        effective_state = raw_state
        is_stale = False
        stale_reason = None

        if raw_state == "running":
            if pid and not pid_alive:
                is_stale = True
                effective_state = "stale"
                stale_reason = f"Process (PID {pid}) terminated unexpectedly"
            elif pid_matches_monitor is False:
                is_stale = True
                effective_state = "stale"
                stale_reason = f"Process (PID {pid}) is not this Watchtower monitor"
            elif heartbeat_age is not None and heartbeat_age > 30.0:
                is_stale = True
                effective_state = "stale"
                stale_reason = f"No heartbeat for {int(heartbeat_age)}s (expected every 2s)"
            elif not heartbeat_at:
                is_stale = True
                effective_state = "stale"
                stale_reason = "No heartbeat recorded since startup"

        active_moves = []
        if effective_state == "running" and not is_stale and pid_alive:
            try:
                active_moves = json.loads(status["active_moves_json"] or "[]")
            except Exception:
                active_moves = []

        total_pending = sum(counts.values())
        attention_events = total_pending
        if effective_state == "running" and not is_stale and pid_alive and total_pending > 0:
            pending_rows = [dict(row) for row in connection.execute(
                "SELECT event_key, event_type, source_path, destination_path, first_seen_at, last_seen_at FROM filesystem_events WHERE status='pending'"
            )]
            annotate_pending_events_processing_state(pending_rows, active_moves, monitor_running=True, connection=connection, database_path=database_path)
            attention_events = sum(1 for r in pending_rows if not r.get("processing_state"))
        elif effective_state == "running" and not is_stale and pid_alive:
            attention_events = 0

        return {
            "state": effective_state,
            "raw_state": raw_state,
            "is_stale": is_stale,
            "stale_reason": stale_reason,
            "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None,
            "pid": pid,
            "pid_alive": pid_alive,
            "pid_matches_monitor": pid_matches_monitor,
            "started_at": status["started_at"],
            "token": status["token"],
            "heartbeat_at": heartbeat_at,
            "roots": json.loads(status["roots_json"] or "[]"),
            "unavailable_roots": json.loads(status["unavailable_roots_json"] or "[]"),
            "active_moves": active_moves,
            "pending_events": total_pending,
            "attention_events": attention_events,
            "event_types": counts,
        }
    finally:
        connection.close()


def _path_key(path: str) -> str:
    return unicodedata.normalize("NFC", os.path.normcase(os.path.normpath(str(path))))


def reconcile_filesystem_events(database_path: Path) -> tuple[dict, list[dict]]:
    """Turn pending watcher events into read-only Stash reconciliation proposals."""
    now = utc_now()
    connection = connect(database_path)
    try:
        run_id = connection.execute(
            "INSERT INTO filesystem_reconciliation_runs(started_at) VALUES (?)", (now,)
        ).lastrowid
        events = connection.execute(
            "SELECT * FROM filesystem_events WHERE status='pending' ORDER BY first_seen_at,event_key"
        ).fetchall()
        files = connection.execute("SELECT * FROM files").fetchall()
        files_by_path = {_path_key(row["path"]): row for row in files}
        missing_by_size = {}
        for row in files:
            if not row["exists_on_disk"] and row["size"] is not None:
                missing_by_size.setdefault(int(row["size"]), []).append(row)
        summary = {"events": len(events), "verified": 0, "review": 0, "informational": 0}
        proposals = []

        for event in events:
            source = event["source_path"]
            destination = event["destination_path"]
            tracked_source = files_by_path.get(_path_key(source))
            tracked_destination = files_by_path.get(_path_key(destination)) if destination else None
            item = {"event_key": event["event_key"], "event_type": event["event_type"],
                    "file_id": None, "scene_id": None, "old_path": source, "new_path": destination,
                    "confidence": "informational", "recommendation": "none",
                    "reason": "Event does not require Stash path reconciliation", "action_performed": False}

            if event["is_directory"]:
                pass
            elif event["event_type"] == "moved" and tracked_destination:
                item.update(file_id=tracked_destination["file_id"], scene_id=tracked_destination["scene_id"],
                            confidence="informational", recommendation="none",
                            reason="Destination already matches the current Stash inventory")
            elif event["event_type"] == "moved" and tracked_source and destination and Path(destination).is_file():
                candidate = Path(destination)
                size_match = tracked_source["size"] is None or candidate.stat().st_size == tracked_source["size"]
                expected_hash = fingerprint_value(tracked_source["fingerprints_json"], "oshash")
                hash_match = opensubtitles_hash(candidate) == expected_hash if expected_hash and size_match else None
                if size_match and hash_match is not False:
                    confidence = "verified" if hash_match else "strong"
                    reason = "Exact watched move from the inventoried path"
                    if hash_match:
                        reason += "; size and oshash match"
                    item.update(file_id=tracked_source["file_id"], scene_id=tracked_source["scene_id"],
                                confidence=confidence, recommendation="targeted_stash_reconciliation", reason=reason)
                else:
                    item.update(file_id=tracked_source["file_id"], scene_id=tracked_source["scene_id"],
                                confidence="review", recommendation="manual_review",
                                reason="Move destination does not match the inventoried size or oshash")
            elif event["event_type"] == "deleted" and tracked_source:
                item.update(file_id=tracked_source["file_id"], scene_id=tracked_source["scene_id"],
                            confidence="review", recommendation="wait_for_matching_create_or_move",
                            reason="Tracked path was deleted without a paired destination")
            elif event["event_type"] == "created" and not tracked_source and Path(source).is_file():
                candidate = Path(source)
                dup = find_duplicate_scene_file(database_path, candidate)
                if dup and not dup["is_external_move"]:
                    item.update(file_id=dup["file_id"], scene_id=dup["scene_id"],
                                confidence="review", recommendation="manual_review",
                                reason=f"Created file is byte-for-byte identical (SHA-256) to active scene {dup['scene_id']} at {dup['existing_path']}")
                elif dup and dup["is_external_move"]:
                    item.update(file_id=dup["file_id"], scene_id=dup["scene_id"],
                                confidence="verified" if dup["match_type"] == "sha256_exact" else "strong",
                                recommendation="targeted_stash_reconciliation",
                                reason=f"Created file matches missing scene {dup['scene_id']} by {dup['match_type']}")
                else:
                    same_size = missing_by_size.get(candidate.stat().st_size, [])
                    hash_matches = []
                    for missing in same_size:
                        expected_hash = fingerprint_value(missing["fingerprints_json"], "oshash")
                        if expected_hash and opensubtitles_hash(candidate) == expected_hash:
                            hash_matches.append(missing)
                    if len(hash_matches) == 1:
                        match = hash_matches[0]
                        item.update(file_id=match["file_id"], scene_id=match["scene_id"],
                                    confidence="verified", recommendation="targeted_stash_reconciliation",
                                    reason="Untracked created file matches one missing file by size and oshash")
                    elif len(same_size) == 1:
                        match = same_size[0]
                        item.update(file_id=match["file_id"], scene_id=match["scene_id"], confidence="review",
                                    recommendation="manual_review",
                                    reason="Unique size match but no verified oshash match")
                    elif same_size:
                        item.update(confidence="review", recommendation="manual_review",
                                    reason=f"Created file has {len(same_size)} possible missing-file size matches")

            bucket = "verified" if item["confidence"] == "verified" else (
                "review" if item["confidence"] in ("review", "strong") else "informational")
            summary[bucket] += 1
            connection.execute(
                """INSERT INTO filesystem_reconciliation_proposals(run_id,event_key,event_type,file_id,scene_id,
                       old_path,new_path,confidence,recommendation,reason,action_performed,recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0,?)""",
                (run_id, item["event_key"], item["event_type"], item["file_id"], item["scene_id"],
                 item["old_path"], item["new_path"], item["confidence"], item["recommendation"], item["reason"], now),
            )
            proposals.append(item)

        connection.execute(
            """UPDATE filesystem_reconciliation_runs SET completed_at=?,event_count=?,verified_count=?,
                   review_count=?,informational_count=?,status='complete' WHERE id=?""",
            (now, summary["events"], summary["verified"], summary["review"], summary["informational"], run_id),
        )
        connection.commit()
        summary["run_id"] = run_id
        return summary, proposals
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def fingerprint_value(fingerprints_json: str, fingerprint_type: str):
    """Extract a specific fingerprint value from fingerprints JSON string."""
    from librarymanager.scanning.reconciliation import fingerprint_value as _impl
    return _impl(fingerprints_json, fingerprint_type)


def opensubtitles_hash(path: Path):
    """Calculate the OpenSubtitles hash without reading the complete video."""
    from librarymanager.scanning.reconciliation import opensubtitles_hash as _impl
    return _impl(path)

opensubtitles_hash._is_watchtower_wrapper = True



CHECKSUM_STABILITY_SECONDS = 5.0
CHECKSUM_RETRY_BASE_SECONDS = 15.0
CHECKSUM_MAX_ACTIVE_JOBS = 8


def get_file_stat_snapshot(path: Path | str) -> tuple[int, int, int, int] | None:
    """Return a high-resolution (size, mtime_ns, device, inode) identity snapshot."""
    from librarymanager.scanning.filesystem import get_file_stat_snapshot as _impl
    return _impl(path)


def enqueue_checksum_calculation(
    database_path: Path,
    path: Path | str,
    file_id: str | None = None,
    stability_seconds: float = CHECKSUM_STABILITY_SECONDS,
) -> bool:
    """Persist checksum work for the long-running filesystem monitor.

    Identical jobs coalesce in SQLite, so dashboard processes and monitor restarts
    cannot create parallel full-file reads.
    """
    from librarymanager.scanning.filesystem import enqueue_checksum_calculation as _impl
    return _impl(database_path, path, file_id=file_id, stability_seconds=stability_seconds)


def reset_checksum_jobs_for_monitor_restart(database_path: Path) -> int:
    """Return jobs abandoned by a previous monitor process to the durable queue."""
    from librarymanager.scanning.filesystem import reset_checksum_jobs_for_monitor_restart as _impl
    return _impl(database_path)


def claim_checksum_job(database_path: Path) -> dict | None:
    """Atomically claim one due checksum job; concurrent workers cannot share it."""
    from librarymanager.scanning.filesystem import claim_checksum_job as _impl
    return _impl(database_path)


def _retry_checksum_job(database_path: Path, job: dict, reason: str) -> None:
    from librarymanager.scanning.filesystem import _retry_checksum_job as _impl
    return _impl(database_path, job, reason)


def process_checksum_job(database_path: Path, job: dict) -> bool:
    """Hash one claimed job and persist its verified file identity."""
    from librarymanager.scanning.filesystem import process_checksum_job as _impl
    return _impl(database_path, job)


def process_next_checksum_job(database_path: Path) -> bool:
    """Process at most one durable checksum job."""
    from librarymanager.scanning.filesystem import process_next_checksum_job as _impl
    return _impl(database_path)


def calculate_sha256_verified(path: Path | str, chunk_size: int = 65536) -> str | None:
    """Calculate complete SHA-256 checksum with pre- and post-read stability checks."""
    from librarymanager.scanning.filesystem import calculate_sha256_verified as _impl
    return _impl(path, chunk_size=chunk_size)

calculate_sha256_verified._is_watchtower_wrapper = True


def calculate_sha256(path: Path | str, chunk_size: int = 65536) -> str | None:
    """Calculate the complete SHA-256 checksum for a file on disk."""
    from librarymanager.scanning.filesystem import calculate_sha256 as _impl
    return _impl(path, chunk_size=chunk_size)

calculate_sha256._is_watchtower_wrapper = True


def get_cached_or_compute_sha256(
    database_path: Path,
    path: Path | str,
    allow_compute: bool = False,
    file_id: str | None = None,
) -> tuple[str | None, str]:
    """Retrieve SHA-256 from persistent cache or compute with stability verification.

    Returns: (sha256_or_none, status)
    status can be: 'verified', 'pending', 'unstable', 'offline', 'empty'
    """
    from librarymanager.scanning.filesystem import get_cached_or_compute_sha256 as _impl
    return _impl(database_path, path, allow_compute=allow_compute, file_id=file_id)


def cached_sha256_for_file_id(database_path: Path, file_id: str, size: int | None = None) -> str | None:
    """Return durable verified source identity even after its original path disappears."""
    from librarymanager.scanning.filesystem import cached_sha256_for_file_id as _impl
    return _impl(database_path, file_id, size=size)


def find_duplicate_scene_file(
    database_path: Path,
    candidate_path: Path | str,
    allow_compute: bool = False,
) -> dict | None:
    """Check if candidate_path matches any existing Stash scene file by size and strong checksum.

    Returns a dict with scene and duplicate details, or None if no duplicate is found.
    Handles ambiguous matches (multiple matching scenes) by requiring manual review without
    blindly selecting the first record.
    """
    cand = Path(candidate_path)
    stat = get_file_stat_snapshot(cand)
    if stat is None or stat[0] == 0 or cand.suffix.lower() not in VIDEO_EXTENSIONS:
        return None
    cand_size = stat[0]
    norm_cand = os.path.normcase(os.path.abspath(cand))

    conn = connect(database_path)
    try:
        rows = conn.execute(
            """SELECT file_id, scene_id, path, basename, title, studio, size,
                      duration, fingerprints_json, exists_on_disk
               FROM files WHERE size=?""",
            (cand_size,)
        ).fetchall()

        if not rows:
            return None

        candidate_rows = [
            r for r in rows
            if os.path.normcase(os.path.abspath(r["path"])) != norm_cand
        ]
        if not candidate_rows:
            return None

        source_candidates = []
        for row in candidate_rows:
            existing_path = Path(row["path"])
            existing_stat = get_file_stat_snapshot(existing_path)
            existing_on_disk = existing_stat is not None and existing_stat[0] > 0
            source_sha256 = fingerprint_value(row["fingerprints_json"], "sha256")
            if not existing_on_disk and not source_sha256:
                cached = conn.execute(
                    """SELECT sha256 FROM file_checksum_cache
                       WHERE file_id=? AND size=? AND status='completed' AND sha256 IS NOT NULL
                       ORDER BY calculated_at DESC LIMIT 1""",
                    (str(row["file_id"]), cand_size),
                ).fetchone()
                source_sha256 = str(cached["sha256"]).lower() if cached and cached["sha256"] else None
            source_candidates.append((row, existing_path, existing_on_disk, source_sha256))

        # A missing source with no recorded checksum cannot be verified by hashing
        # the destination alone. Keep it visible as unverified without reading the
        # candidate video or scheduling unrelated library files.
        has_verifiable_source = any(
            existing_on_disk or source_sha256
            for _, _, existing_on_disk, source_sha256 in source_candidates
        )
        if has_verifiable_source:
            cand_sha256, _ = get_cached_or_compute_sha256(
                database_path, cand, allow_compute=allow_compute
            )
        else:
            cand_sha256 = None

        cand_companions = get_companion_files(cand)

        verified_matches = []
        pending_matches = []

        for r, existing_path, existing_on_disk, recorded_sha256 in source_candidates:
            existing_path_str = r["path"]
            if existing_on_disk:
                orig_sha256, _ = get_cached_or_compute_sha256(
                    database_path, existing_path, allow_compute=allow_compute,
                    file_id=str(r["file_id"]),
                )
            else:
                orig_sha256 = recorded_sha256

            if cand_sha256 and orig_sha256:
                if cand_sha256.lower() == orig_sha256.lower():
                    existing_comps = get_companion_files(existing_path) if existing_on_disk else []
                    is_external_move = not existing_on_disk
                    if not is_external_move:
                        del_ev = conn.execute(
                            "SELECT 1 FROM filesystem_events WHERE event_type='deleted' AND source_path=? AND status='pending'",
                            (existing_path_str,)
                        ).fetchone()
                        if del_ev:
                            is_external_move = True

                    verified_matches.append({
                        "scene_id": str(r["scene_id"]),
                        "file_id": str(r["file_id"]),
                        "title": r["title"] or r["basename"],
                        "existing_path": existing_path_str,
                        "candidate_path": str(cand),
                        "size": cand_size,
                        "checksum_value": cand_sha256,
                        "checksum_status": "verified",
                        "match_type": "sha256_exact",
                        "is_external_move": is_external_move,
                        "existing_exists_on_disk": existing_on_disk,
                        "candidate_exists_on_disk": True,
                        "existing_companions": existing_comps,
                        "candidate_companions": cand_companions,
                    })
            elif cand_sha256 is None or orig_sha256 is None:
                existing_comps = get_companion_files(existing_path) if existing_on_disk else []
                is_external_move = not existing_on_disk
                if not is_external_move:
                    del_ev = conn.execute(
                        "SELECT 1 FROM filesystem_events WHERE event_type='deleted' AND source_path=? AND status='pending'",
                        (existing_path_str,)
                    ).fetchone()
                    if del_ev:
                        is_external_move = True

                verification_unavailable = not existing_on_disk and orig_sha256 is None
                pending_matches.append({
                    "scene_id": str(r["scene_id"]),
                    "file_id": str(r["file_id"]),
                    "title": r["title"] or r["basename"],
                    "existing_path": existing_path_str,
                    "candidate_path": str(cand),
                    "size": cand_size,
                    "checksum_value": None,
                    "checksum_status": "unverified" if verification_unavailable else "pending",
                    "match_type": "source_checksum_unavailable" if verification_unavailable else "pending_verification",
                    "is_external_move": is_external_move,
                    "existing_exists_on_disk": existing_on_disk,
                    "candidate_exists_on_disk": True,
                    "existing_companions": existing_comps,
                    "candidate_companions": cand_companions,
                })

        if len(verified_matches) == 1:
            res = dict(verified_matches[0])
            res["is_ambiguous"] = False
            res["all_candidates"] = verified_matches
            return res
        elif len(verified_matches) > 1:
            return {
                "is_ambiguous": True,
                "ambiguous_count": len(verified_matches),
                "scene_id": None,
                "file_id": None,
                "title": f"Ambiguous ({len(verified_matches)} identical scenes found)",
                "existing_path": None,
                "candidate_path": str(cand),
                "size": cand_size,
                "checksum_value": cand_sha256,
                "checksum_status": "verified",
                "match_type": "sha256_exact",
                "is_external_move": False,
                "existing_exists_on_disk": True,
                "candidate_exists_on_disk": True,
                "existing_companions": [],
                "candidate_companions": cand_companions,
                "all_candidates": verified_matches,
                "reason": f"Found {len(verified_matches)} matching Stash scenes with identical SHA-256 checksums",
            }
        elif len(pending_matches) == 1:
            res = dict(pending_matches[0])
            res["is_ambiguous"] = False
            res["all_candidates"] = pending_matches
            return res
        elif len(pending_matches) > 1:
            all_unverified = all(match["checksum_status"] == "unverified" for match in pending_matches)
            return {
                "is_ambiguous": True,
                "ambiguous_count": len(pending_matches),
                "scene_id": None,
                "file_id": None,
                "title": f"Ambiguous ({len(pending_matches)} candidate scenes found)",
                "existing_path": None,
                "candidate_path": str(cand),
                "size": cand_size,
                "checksum_value": None,
                "checksum_status": "unverified" if all_unverified else "pending",
                "match_type": "source_checksum_unavailable" if all_unverified else "pending_verification",
                "is_external_move": False,
                "existing_exists_on_disk": True,
                "candidate_exists_on_disk": True,
                "existing_companions": [],
                "candidate_companions": cand_companions,
                "all_candidates": pending_matches,
                "reason": (
                    f"Found {len(pending_matches)} candidate scenes matching file size; source checksums are unavailable"
                    if all_unverified else
                    f"Found {len(pending_matches)} candidate scenes matching file size; verification pending"
                ),
            }

        return None
    finally:
        conn.close()


def _cached_sha256_for_current_path(database_path: Path, path: Path | str) -> str | None:
    """Return a checksum only when it belongs to the file's current stat identity."""
    from librarymanager.scanning.filesystem import _cached_sha256_for_current_path as _impl
    return _impl(database_path, path)


def strict_incoming_companions(video_path: Path | str, incoming_folders: list[str]) -> list[dict]:
    """List exact-name companions beside an incoming video without fuzzy matching."""
    from librarymanager.scanning.filesystem import strict_incoming_companions as _impl
    return _impl(video_path, incoming_folders)


def inspect_backlog_duplicate(
    database_path: Path,
    stash,
    candidate_path: Path | str,
    config: dict,
    request_verification: bool = False,
) -> dict | None:
    """Inspect one user-selected incoming file as a possible same-scene duplicate.

    Full-file reads are queued only after an explicit verification request.  Cached
    checksums are accepted only while size, nanosecond mtime, device and inode still
    match, so dashboard polling remains read-only and cheap.
    """
    candidate = Path(candidate_path)
    incoming_folders = get_configured_incoming_folders(config)
    if not incoming_folders:
        raise ValueError("Configure an Incoming Downloads folder before reviewing duplicates")
    if not candidate.is_file():
        raise ValueError("The selected incoming file is no longer available")
    if candidate.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("Only video files can be reviewed as duplicate videos")
    if not any(_is_subpath_of(candidate, Path(root).expanduser()) for root in incoming_folders):
        raise ValueError("Watchtower only permits duplicate repair inside a configured incoming folder")

    connection = connect(database_path)
    try:
        candidate_row = connection.execute(
            """SELECT file_id,scene_id,path,size,duration,fingerprints_json
               FROM files WHERE path=? ORDER BY exists_on_disk DESC LIMIT 1""",
            (str(candidate),),
        ).fetchone()
        if not candidate_row or not candidate_row["file_id"] or not candidate_row["scene_id"]:
            return None
        candidate_size = int(candidate_row["size"] or candidate.stat().st_size)
        candidate_oshash = fingerprint_value(candidate_row["fingerprints_json"], "oshash")
        rows = connection.execute(
            """SELECT file_id,scene_id,path,size,duration,fingerprints_json
               FROM files WHERE scene_id=? AND file_id!=?""",
            (str(candidate_row["scene_id"]), str(candidate_row["file_id"])),
        ).fetchall()
    finally:
        connection.close()

    retained_candidates = []
    for row in rows:
        retained = Path(row["path"])
        if not retained.is_file():
            continue
        if any(_is_subpath_of(retained, Path(root).expanduser()) for root in incoming_folders):
            continue
        retained_snapshot = get_file_stat_snapshot(retained)
        retained_size = int(row["size"] or (retained_snapshot[0] if retained_snapshot else 0))
        if retained_size != candidate_size:
            continue
        retained_oshash = fingerprint_value(row["fingerprints_json"], "oshash")
        if candidate_oshash and retained_oshash and candidate_oshash.lower() != retained_oshash.lower():
            continue
        retained_candidates.append(row)

    if not retained_candidates:
        return None
    if len(retained_candidates) != 1:
        return {
            "status": "ambiguous",
            "checksum_status": "unverified",
            "scene_id": str(candidate_row["scene_id"]),
            "candidate_file_id": str(candidate_row["file_id"]),
            "candidate_path": str(candidate),
            "reason": f"This scene has {len(retained_candidates)} possible retained files; choose in Stash before deleting anything",
            "companions": strict_incoming_companions(candidate, incoming_folders),
        }

    retained_row = retained_candidates[0]
    retained = Path(retained_row["path"])

    # A destructive action must use Stash's current scene/file ownership, not
    # merely the last inventory snapshot.
    if stash is not None:
        try:
            scene = stash.find_scene(int(candidate_row["scene_id"]))
        except Exception as exc:
            raise RuntimeError(f"Could not verify Stash Scene {candidate_row['scene_id']}: {exc}") from exc
        scene_files = (scene or {}).get("files") or []
        current_by_id = {str(item.get("id")): item for item in scene_files if item and item.get("id") is not None}
        current_candidate = current_by_id.get(str(candidate_row["file_id"]))
        current_retained = current_by_id.get(str(retained_row["file_id"]))
        if not current_candidate or os.path.normcase(os.path.realpath(current_candidate.get("path") or "")) != os.path.normcase(os.path.realpath(candidate)):
            raise ValueError("The incoming file is no longer attached to the expected Stash scene")
        if not current_retained or os.path.normcase(os.path.realpath(current_retained.get("path") or "")) != os.path.normcase(os.path.realpath(retained)):
            raise ValueError("The organised file is no longer attached to the expected Stash scene")
        if len(scene_files) < 2:
            raise ValueError("The scene no longer has a retained file, so deletion was blocked")

    candidate_sha = _cached_sha256_for_current_path(database_path, candidate)
    retained_sha = _cached_sha256_for_current_path(database_path, retained)
    checksum_status = "pending"
    reason = "Full SHA-256 verification has not completed"
    if candidate_sha and retained_sha:
        if candidate_sha == retained_sha:
            checksum_status = "verified"
            reason = "Both current files have identical full SHA-256 checksums"
        else:
            checksum_status = "mismatch"
            reason = "The full SHA-256 checksums differ; deletion is not allowed"
    elif request_verification:
        queued_candidate = enqueue_checksum_calculation(
            database_path, candidate, str(candidate_row["file_id"])
        )
        queued_retained = enqueue_checksum_calculation(
            database_path, retained, str(retained_row["file_id"])
        )
        if not (queued_candidate and queued_retained):
            reason = "Checksum work is already pending or the bounded verification queue is currently full"
        else:
            reason = "Full SHA-256 verification was queued and will run sequentially"

    return {
        "status": "exact_duplicate" if checksum_status == "verified" else "duplicate_candidate",
        "checksum_status": checksum_status,
        "reason": reason,
        "scene_id": str(candidate_row["scene_id"]),
        "candidate_file_id": str(candidate_row["file_id"]),
        "candidate_path": str(candidate),
        "retained_file_id": str(retained_row["file_id"]),
        "retained_path": str(retained),
        "size": candidate_size,
        "sha256": candidate_sha if checksum_status == "verified" else None,
        "companions": strict_incoming_companions(candidate, incoming_folders),
    }


def reconcile_missing_files(database_path: Path) -> tuple[dict, list[dict]]:
    """Find rename candidates in the missing file's original directory; never mutate files."""
    from librarymanager.scanning.reconciliation import reconcile_missing_files as _impl
    return _impl(database_path)


def _prune_cached_reports(target_fids_set: set[str], root_dir: Path | None = None) -> None:
    """Safely remove entries matching target_fids_set from reconciliation-report.json and resolution-plan.json."""
    from librarymanager.scanning.reconciliation import _prune_cached_reports as _impl
    return _impl(target_fids_set, root_dir=root_dir)


def clean_deleted_scenes_from_stash(database_path: Path, stash: Any = None, root_dir: Path | None = None) -> dict:
    """Safely prune dead records from Watchtower's files table after Stash clean or file deletion.
    - Removes files where exists_on_disk = 0 and the scene/file no longer exists in Stash or disk.
    - Also verifies files on disk; if a missing file was restored/reconnected, updates exists_on_disk = 1.
    - Records an activity audit event in the live feed.
    """
    from librarymanager.scanning.reconciliation import clean_deleted_scenes_from_stash as _impl
    return _impl(database_path, stash=stash, root_dir=root_dir)


def reconcile_missing_media_across_roots(
    database_path: Path, stash, roots: list, config: dict = None, root_dir: Path | None = None
) -> dict:
    """Find and reconnect moved scene media across monitored library roots without full Stash scans.
    Designed for files moved via phones, tablets, or external file managers where filesystem
    events were bypassed.
    """
    from librarymanager.scanning.reconciliation import reconcile_missing_media_across_roots as _impl
    return _impl(database_path, stash, roots, config=config, root_dir=root_dir)


INCOMING_SCAN_FLAGS = {
    "scanGenerateCovers": True,
    "scanGeneratePreviews": True,
    "scanGenerateSprites": True,
    "scanGeneratePhashes": True,
    "scanGenerateThumbnails": True,
}


def get_unindexed_external_media(database_path: Path) -> list[dict]:
    """Return all non-ignored unindexed video files found on disk."""
    from librarymanager.services.manual_sync import get_unindexed_external_media as _impl
    return _impl(database_path)


def _ensure_stash_supports_extension(stash, path: str):
    """Ensure Stash's configured videoExtensions include this file's extension so the scanner accepts it."""
    from librarymanager.services.manual_sync import _ensure_stash_supports_extension as _impl
    return _impl(stash, path)


def notify_desktop(message: str, title: str = "Stash Library Manager"):
    """Send native desktop notification across macOS, Windows, Linux."""
    from librarymanager.services.manual_sync import notify_desktop as _impl
    return _impl(message, title=title)

notify_desktop._is_watchtower_wrapper = True


def add_unindexed_media_file(database_path: Path, stash, path: str, config: dict = None) -> dict:
    """Trigger a full Stash ingestion scan for an unindexed video file, including previews, sprites, companions, and contact sheets."""
    from librarymanager.services.manual_sync import add_unindexed_media_file as _impl
    return _impl(database_path, stash, path, config=config)


def add_all_unindexed_media_files(database_path: Path, stash, config: dict = None, roots: list = None) -> dict:
    """Trigger full Stash ingestion for all discovered unindexed video files."""
    from librarymanager.services.manual_sync import add_all_unindexed_media_files as _impl
    return _impl(database_path, stash, config=config, roots=roots)


def ignore_unindexed_media_file(database_path: Path, path: str) -> dict:
    """Mark an unindexed video file as ignored so it does not prompt again."""
    from librarymanager.services.manual_sync import ignore_unindexed_media_file as _impl
    return _impl(database_path, path)


def ignore_all_unindexed_media_files(database_path: Path) -> dict:
    """Mark all currently discovered unindexed video files as ignored."""
    from librarymanager.services.manual_sync import ignore_all_unindexed_media_files as _impl
    return _impl(database_path)


def reset_ignored_unindexed_media_files(database_path: Path) -> dict:
    """Un-ignore all previously ignored external media files so they immediately reappear in active review."""
    from librarymanager.services.manual_sync import reset_ignored_unindexed_media_files as _impl
    return _impl(database_path)


def _metadata_differences(stale_metadata: dict, live_metadata: dict) -> list[str]:
    differences = []
    collection_fields = ("performers", "tags", "galleries", "urls", "stash_ids", "groups")
    scalar_fields = ("title", "details", "date", "director", "code", "rating100", "organized", "studio")
    for field in collection_fields:
        stale_values = set(stale_metadata.get(field) or [])
        live_values = set(live_metadata.get(field) or [])
        if not stale_values.issubset(live_values):
            differences.append(field)
    for field in scalar_fields:
        stale_value = stale_metadata.get(field)
        live_value = live_metadata.get(field)
        if stale_value not in (None, "", False) and stale_value != live_value:
            differences.append(field)
    return differences


def build_resolution_plan(database_path: Path) -> dict:
    """Plan safe conflict resolution from the latest reconciliation; never mutate Stash."""
    connection = connect(database_path)
    try:
        latest_run = connection.execute("SELECT MAX(id) FROM reconciliation_runs").fetchone()[0]
        items = []
        if latest_run is None:
            return {"reconciliation_run_id": None, "items": [], "message": "Run reconciliation first"}
        conflicts = connection.execute(
            "SELECT * FROM reconciliation_candidates WHERE run_id=? AND confidence='conflict' ORDER BY scene_id",
            (latest_run,),
        ).fetchall()
        for conflict in conflicts:
            stale = connection.execute("SELECT * FROM files WHERE file_id=?", (conflict["file_id"],)).fetchone()
            live = connection.execute("SELECT * FROM files WHERE path=? AND exists_on_disk=1", (conflict["candidate_path"],)).fetchone()
            if not stale or not live:
                items.append({"stale_scene_id": conflict["scene_id"], "recommendation": "manual_review",
                              "reason": "The stale or live inventory record is no longer available"})
                continue
            stale_metadata = json.loads(stale["scene_metadata_json"] or "{}")
            live_metadata = json.loads(live["scene_metadata_json"] or "{}")
            if stale_metadata.get("inventory_version") != 1 or live_metadata.get("inventory_version") != 1:
                items.append({
                    "stale_scene_id": stale["scene_id"], "stale_file_id": stale["file_id"],
                    "live_scene_id": live["scene_id"], "live_file_id": live["file_id"],
                    "recommendation": "refresh_inventory_first",
                    "reason": "Run Build Read-Only Inventory again to capture the full Step 3 metadata set",
                    "action_performed": False,
                })
                continue
            differences = _metadata_differences(stale_metadata, live_metadata)
            recommendation = "merge_metadata_first" if differences else "stale_record_redundant"
            reason = (f"Stale scene contains metadata not preserved on the live scene: {', '.join(differences)}"
                      if differences else "All inventoried stale-scene metadata is already preserved on the live scene")
            items.append({
                "stale_scene_id": stale["scene_id"], "stale_file_id": stale["file_id"],
                "live_scene_id": live["scene_id"], "live_file_id": live["file_id"],
                "live_path": live["path"], "recommendation": recommendation,
                "metadata_differences": differences, "reason": reason,
                "action_performed": False,
            })
        return {"reconciliation_run_id": latest_run, "items": items,
                "safe_redundant": sum(item.get("recommendation") == "stale_record_redundant" for item in items),
                "merge_required": sum(item.get("recommendation") == "merge_metadata_first" for item in items)}
    finally:
        connection.close()


def build_merge_preview(database_path: Path) -> dict:
    """Create an additive, non-mutating metadata merge preview from the latest plan."""
    plan = build_resolution_plan(database_path)
    connection = connect(database_path)
    try:
        previews = []
        for item in plan.get("items", []):
            if item.get("recommendation") not in ("merge_metadata_first", "stale_record_redundant"):
                continue
            stale = connection.execute("SELECT * FROM files WHERE file_id=?", (item["stale_file_id"],)).fetchone()
            live = connection.execute("SELECT * FROM files WHERE file_id=?", (item["live_file_id"],)).fetchone()
            if not stale or not live:
                continue
            stale_metadata = json.loads(stale["scene_metadata_json"] or "{}")
            live_metadata = json.loads(live["scene_metadata_json"] or "{}")
            changes = []
            conflicts = []
            for field in ("performers", "tags", "galleries", "urls", "stash_ids", "groups"):
                stale_values = set(stale_metadata.get(field) or [])
                live_values = set(live_metadata.get(field) or [])
                additions = sorted(stale_values - live_values)
                if additions:
                    changes.append({"field": field, "operation": "add", "values": additions,
                                    "result": sorted(live_values | stale_values)})
            for field in ("title", "details", "date", "director", "code", "rating100", "studio"):
                stale_value = stale_metadata.get(field)
                live_value = live_metadata.get(field)
                if stale_value in (None, "") or stale_value == live_value:
                    continue
                if live_value in (None, ""):
                    changes.append({"field": field, "operation": "fill_empty",
                                    "old_value": live_value, "new_value": stale_value})
                else:
                    conflicts.append({"field": field, "live_value": live_value,
                                      "stale_value": stale_value, "resolution": "manual_choice_required"})
            previews.append({
                "stale_scene_id": item["stale_scene_id"], "live_scene_id": item["live_scene_id"],
                "changes": changes, "conflicts": conflicts,
                "ready_to_apply": bool(changes) and not conflicts,
                "action_performed": False,
            })
        return {
            "resolution_run_id": plan.get("reconciliation_run_id"),
            "previews": previews,
            "ready_to_apply": sum(bool(item["ready_to_apply"]) for item in previews),
            "manual_review": sum(bool(item["conflicts"]) for item in previews),
            "action_performed": False,
        }
    finally:
        connection.close()


def _normalized_filename_text(value: str) -> str:
    from librarymanager.scanning.naming_rules import _normalized_filename_text as _impl
    return _impl(value)


def _compact_filename_text(value: str) -> str:
    from librarymanager.scanning.naming_rules import _compact_filename_text as _impl
    return _impl(value)


def _same_filename_name(left: str, right: str) -> bool:
    """Treat spaced, compact and punctuation variants of a metadata name as equal."""
    from librarymanager.scanning.naming_rules import _same_filename_name as _impl
    return _impl(left, right)


def _contains_name(text: str, name: str) -> bool:
    from librarymanager.scanning.naming_rules import _contains_name as _impl
    return _impl(text, name)


def _remove_legacy_studio(base: str, studio: str | None) -> str:
    """Remove an equivalent legacy [studio] or {studio} marker from a stable base."""
    from librarymanager.scanning.naming_rules import _remove_legacy_studio as _impl
    return _impl(base, studio)


def _metadata_name_pattern(name: str, include_connectors: bool = False) -> str | None:
    """Return an exact-name regex that tolerates spaces/punctuation but not partial words."""
    from librarymanager.scanning.naming_rules import _metadata_name_pattern as _impl
    return _impl(name, include_connectors)


def _strip_managed_metadata(base: str, studios, performers, options: dict | None = None) -> str:
    """Remove only studio/performer names that Stash currently or previously identified as metadata."""
    from librarymanager.scanning.naming_rules import _strip_managed_metadata as _impl
    return _impl(base, studios, performers, options)


def _remove_manual_metadata(base: str, studio: str | None, performers: list[str]) -> str:
    """Compatibility wrapper for older callers; may return an empty clean base."""
    from librarymanager.scanning.naming_rules import _remove_manual_metadata as _impl
    return _impl(base, studio, performers)


def _is_officially_scraped(row) -> bool:
    """Return True if the scene has been matched to an external metadata provider (e.g. StashDB, The PornDB)."""
    from librarymanager.scanning.naming_rules import _is_officially_scraped as _impl
    return _impl(row)


def _should_strip_metadata_from_title(filename_options: dict | None, row=None, title: str | None = None) -> bool:
    from librarymanager.scanning.naming_rules import _should_strip_metadata_from_title as _impl
    return _impl(filename_options, row, title)


def _sync_filename_state(connection, state, row, current: Path, performers: list[str], filename_options: dict | None = None):
    """Synchronize the stable base with current Stash metadata and the physical filename."""
    from librarymanager.scanning.naming_rules import _sync_filename_state as _impl
    return _impl(connection, state, row, current, performers, filename_options)


def _create_filename_state(connection, row, current: Path, performers: list[str], filename_options: dict | None = None):
    """Create filename state from Stash title when known, otherwise the existing physical stem."""
    from librarymanager.scanning.naming_rules import _create_filename_state as _impl
    return _impl(connection, row, current, performers, filename_options)


def _refresh_manual_filename_state(connection, state, row, performers: list[str]):
    """Compatibility entry point: normalize any stored base using previous/current metadata."""
    from librarymanager.scanning.naming_rules import _refresh_manual_filename_state as _impl
    return _impl(connection, state, row, performers)


def _derive_blank_title_base(stem: str, studio: str | None, performers: list[str]) -> str:
    """Remove only exact trailing metadata produced by supported filename formats."""
    from librarymanager.scanning.naming_rules import _derive_blank_title_base as _impl
    return _impl(stem, studio, performers)


def filename_format_options(config: dict | None = None) -> dict:
    """Turn the UI's friendly filename choices into validated formatting values."""
    from librarymanager.scanning.naming_rules import filename_format_options as _impl
    return _impl(config)


def _validated_video_quality(quality: int | str | None) -> str:
    """Return canonical quality token like '[1080p]' or empty string."""
    from librarymanager.scanning.naming_rules import _validated_video_quality as _impl
    return _impl(quality)


def _strip_matching_video_quality(text: str, quality: str | int | None) -> str:
    """Remove matching resolution token at a clear title boundary without guessing at other resolutions."""
    from librarymanager.scanning.naming_rules import _strip_matching_video_quality as _impl
    return _impl(text, quality)


def _row_video_quality(row) -> str:
    """Extract validated video quality token e.g. '[1080p]' from a files table row or record."""
    from librarymanager.scanning.naming_rules import _row_video_quality as _impl
    return _impl(row)


def _validated_scene_date(scene_date: str | None) -> str:
    from librarymanager.scanning.naming_rules import _validated_scene_date as _impl
    return _impl(scene_date)


def _matching_scene_date_variants(scene_date: str) -> list[str]:
    """Return supported textual forms of a known Stash date for exact deduplication only."""
    from librarymanager.scanning.naming_rules import _matching_scene_date_variants as _impl
    return _impl(scene_date)


def _strip_matching_scene_date(text: str, scene_date: str) -> str:
    """Remove the known scene date at a clear title boundary without guessing at other dates."""
    from librarymanager.scanning.naming_rules import _strip_matching_scene_date as _impl
    return _impl(text, scene_date)


def _sanitize_filename_stem(stem: str, options: dict | None = None) -> str:
    """Remove or replace illegal filesystem characters with clean punctuation."""
    from librarymanager.scanning.naming_rules import _sanitize_filename_stem as _impl
    return _impl(stem, options)


def _is_only_metadata_or_connectors(text: str, studio: str | None, performers: list[str], options: dict | None = None) -> bool:
    """Check if text consists exclusively of studio, performers, and connective words/punctuation."""
    from librarymanager.scanning.naming_rules import _is_only_metadata_or_connectors as _impl
    return _impl(text, studio, performers, options)


def _proposed_stem(base: str, studio: str | None, performers: list[str], options: dict | None = None,
                   scene_date: str | None = None, previous_scene_date: str | None = None,
                   video_quality: str | int | None = None, previous_video_quality: str | int | None = None) -> str:
    """Build one canonical filename from the stored base + current Stash metadata."""
    from librarymanager.scanning.naming_rules import _proposed_stem as _impl
    return _impl(base, studio, performers, options, scene_date, previous_scene_date, video_quality, previous_video_quality)


def preview_safe_filenames(database_path: Path, filename_options: dict | None = None) -> tuple[dict, list[dict]]:
    """Persist stable clean bases and calculate proposed paths; never rename a file."""
    from librarymanager.scanning.naming_rules import preview_safe_filenames as _impl
    return _impl(database_path, filename_options)

preview_safe_filenames._is_watchtower_wrapper = True


def preview_scene_filename(database_path: Path, scene_id: str, filename_options: dict | None = None) -> dict:
    """Return the latest calculated filename proposal for one scene."""
    from librarymanager.scanning.naming_rules import preview_scene_filename as _impl
    return _impl(database_path, scene_id, filename_options, associated_file_target_fn=associated_file_target)

preview_scene_filename._is_watchtower_wrapper = True


def apply_scene_filename(database_path: Path, scene_id: str, move_file, filename_options: dict | None = None) -> dict:
    """Apply one preflighted rename through a supplied Stash move callback."""
    from librarymanager.services.renaming import apply_scene_filename as _impl
    return _impl(database_path, scene_id, move_file, filename_options=filename_options)

apply_scene_filename._is_watchtower_wrapper = True


def preview_manual_filename(database_path: Path, scene_id: str, requested_name: str) -> dict:
    """Preflight one exact user-supplied basename without changing anything."""
    from librarymanager.scanning.naming_rules import preview_manual_filename as _impl
    return _impl(database_path, scene_id, requested_name, associated_file_target_fn=associated_file_target)

preview_manual_filename._is_watchtower_wrapper = True



def apply_manual_filename(database_path: Path, scene_id: str, requested_name: str, move_file) -> dict:
    """Apply one exact previewed correction and make it the scene's new filename base."""
    from librarymanager.services.renaming import apply_manual_filename as _impl
    return _impl(database_path, scene_id, requested_name, move_file)

apply_manual_filename._is_watchtower_wrapper = True


def find_csm_font():
    """Locate a suitable font for contact sheet text annotation across supported platforms."""
    from librarymanager.services.contact_sheets import find_csm_font as _impl
    return _impl()

find_csm_font._is_watchtower_wrapper = True


def format_csm_duration(seconds):
    """Format duration in seconds to MM:SS or HH:MM:SS format."""
    from librarymanager.services.contact_sheets import format_csm_duration as _impl
    return _impl(seconds)

format_csm_duration._is_watchtower_wrapper = True


def ffmpeg_supports_drawtext(ffmpeg_bin):
    """Check whether the given FFmpeg binary has drawtext filter support."""
    from librarymanager.services.contact_sheets import ffmpeg_supports_drawtext as _impl
    return _impl(ffmpeg_bin)

ffmpeg_supports_drawtext._is_watchtower_wrapper = True


def generate_ffmpeg_contact_sheet_fallback(
    ffmpeg_bin, video_file, dest_path, tmp_dir, timestamps, cols, rows,
    scale_w, include_banner, width, height, file_size_str, formatted_duration,
    taskpolicy_prefix,
):
    """Render a contact sheet without ImageMagick using bundled font assets."""
    from librarymanager.services.contact_sheets import generate_ffmpeg_contact_sheet_fallback as _impl
    return _impl(
        ffmpeg_bin, video_file, dest_path, tmp_dir, timestamps, cols, rows,
        scale_w, include_banner, width, height, file_size_str, formatted_duration,
        taskpolicy_prefix,
    )

generate_ffmpeg_contact_sheet_fallback._is_watchtower_wrapper = True


def generate_video_contact_sheet(
    video_path,
    output_path=None,
    grid="4x4",
    include_banner=True,
    adjust_vertical=True,
    custom_script=None,
    allow_custom_script=False,
    overwrite=False,
    logger=None,
):
    """Generate a high-quality visual contact sheet for a video file."""
    from librarymanager.services.contact_sheets import generate_video_contact_sheet as _impl
    return _impl(
        video_path,
        output_path=output_path,
        grid=grid,
        include_banner=include_banner,
        adjust_vertical=adjust_vertical,
        custom_script=custom_script,
        allow_custom_script=allow_custom_script,
        overwrite=overwrite,
        logger=logger,
    )

generate_video_contact_sheet._is_watchtower_wrapper = True


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Automatic Filing (Phase 1)
# ---------------------------------------------------------------------------

def match_performer_for_filing(scene: dict, filename: str, all_performers: list[dict], match_source: str = "metadata_first") -> dict:
    from librarymanager.services.filing_rules import match_performer_for_filing as _impl
    return _impl(scene, filename, all_performers, match_source=match_source)


match_performer_for_filing._is_watchtower_wrapper = True


def match_studio_for_filing(scene: dict, filename: str, all_studios: list[dict], match_source: str = "metadata_first") -> dict:
    from librarymanager.services.filing_rules import match_studio_for_filing as _impl
    return _impl(scene, filename, all_studios, match_source=match_source)


match_studio_for_filing._is_watchtower_wrapper = True


def get_configured_filing_destination_roots(config: dict | None) -> list[str]:
    from librarymanager.services.filing_rules import get_configured_filing_destination_roots as _impl
    return _impl(config)


get_configured_filing_destination_roots._is_watchtower_wrapper = True


def get_filing_folder_mappings(database_path: Path) -> list[dict]:
    from librarymanager.services.filing_rules import get_filing_folder_mappings as _impl
    return _impl(database_path)


get_filing_folder_mappings._is_watchtower_wrapper = True


def save_filing_folder_mapping(
    database_path: Path,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    folder_path: str,
    configured_roots: list[str] | None = None
) -> tuple[bool, str]:
    from librarymanager.services.filing_rules import save_filing_folder_mapping as _impl
    return _impl(
        database_path,
        entity_type,
        entity_id,
        entity_name,
        folder_path,
        configured_roots=configured_roots,
    )


save_filing_folder_mapping._is_watchtower_wrapper = True


def delete_filing_folder_mapping(database_path: Path, mapping_id: int) -> bool:
    from librarymanager.services.filing_rules import delete_filing_folder_mapping as _impl
    return _impl(database_path, mapping_id)


delete_filing_folder_mapping._is_watchtower_wrapper = True


def invalidate_incoming_discovery_cache(root_path: str | Path | None = None) -> None:
    from librarymanager.services.filing_rules import invalidate_incoming_discovery_cache as _impl
    return _impl(root_path=root_path)


invalidate_incoming_discovery_cache._is_watchtower_wrapper = True


def discover_incoming_files_cached(
    incoming_folders: list[str],
    active_incoming_states: dict[str, str] | None = None,
    force_refresh: bool = False,
    ttl: float = 3600.0
) -> list[dict]:
    from librarymanager.services.filing_rules import discover_incoming_files_cached as _impl
    return _impl(
        incoming_folders,
        active_incoming_states=active_incoming_states,
        force_refresh=force_refresh,
        ttl=ttl,
    )


discover_incoming_files_cached._is_watchtower_wrapper = True


def invalidate_destination_dir_cache(root_path: str | Path | None = None, database_path: Path | None = None) -> None:
    from librarymanager.services.filing_rules import invalidate_destination_dir_cache as _impl
    return _impl(root_path=root_path, database_path=database_path)


invalidate_destination_dir_cache._is_watchtower_wrapper = True


def refresh_destination_dir_cache(
    database_path: Path,
    roots: list[str],
    max_depth: int = 4,
    cold_storage_roots: list[str] | set[str] | None = None
) -> dict:
    from librarymanager.services.filing_rules import refresh_destination_dir_cache as _impl
    return _impl(
        database_path,
        roots,
        max_depth=max_depth,
        cold_storage_roots=cold_storage_roots,
    )


refresh_destination_dir_cache._is_watchtower_wrapper = True


def _is_excluded_destination_discovery_dir(dirname: str) -> bool:
    from librarymanager.services.filing_rules import _is_excluded_destination_discovery_dir as _impl
    return _impl(dirname)


_is_excluded_destination_discovery_dir._is_watchtower_wrapper = True


def _scan_destination_subdirectories(
    root_path: Path,
    max_depth: int = 4,
    cold_storage_roots: list[str] | set[str] | None = None
) -> list[tuple[Path, str]]:
    from librarymanager.services.filing_rules import _scan_destination_subdirectories as _impl
    return _impl(root_path, max_depth=max_depth, cold_storage_roots=cold_storage_roots)


_scan_destination_subdirectories._is_watchtower_wrapper = True


def _get_cached_destination_subdirectories(
    root_path: str | Path,
    max_depth: int = 4,
    ttl: float = 3600.0,
    database_path: Path | None = None,
    cold_storage_roots: list[str] | set[str] | None = None
) -> list[tuple[Path, str]]:
    from librarymanager.services.filing_rules import _get_cached_destination_subdirectories as _impl
    return _impl(
        root_path,
        max_depth=max_depth,
        ttl=ttl,
        database_path=database_path,
        cold_storage_roots=cold_storage_roots,
    )


_get_cached_destination_subdirectories._is_watchtower_wrapper = True


def resolve_filing_destinations(
    destination_roots: list[str],
    entity_name: str,
    entity_id: str | None = None,
    entity_type: str | None = None,
    database_path: Path | None = None,
    max_depth: int = 4
) -> tuple[list[Path], str]:
    from librarymanager.services.filing_rules import resolve_filing_destinations as _impl
    return _impl(
        destination_roots,
        entity_name,
        entity_id=entity_id,
        entity_type=entity_type,
        database_path=database_path,
        max_depth=max_depth,
    )


resolve_filing_destinations._is_watchtower_wrapper = True


def resolve_tag_filing_destinations(
    destination_roots: list[str],
    tag: dict,
    database_path: Path | None = None,
    max_depth: int = 4,
) -> tuple[list[Path], str]:
    from librarymanager.services.filing_rules import resolve_tag_filing_destinations as _impl
    return _impl(
        destination_roots,
        tag,
        database_path=database_path,
        max_depth=max_depth,
    )


resolve_tag_filing_destinations._is_watchtower_wrapper = True


def resolve_filing_destination_folder(destination_root: str, entity_name: str) -> tuple[Path | None, str]:
    """Legacy single-root compatibility wrapper for resolve_filing_destinations."""
    from librarymanager.services.filing_rules import resolve_filing_destination_folder as _impl
    return _impl(destination_root, entity_name)


resolve_filing_destination_folder._is_watchtower_wrapper = True



def _is_in_nested_incoming_folder(file_path: Path, incoming_folders: list[str]) -> bool:
    from librarymanager.services.filing import _is_in_nested_incoming_folder as _impl
    return _impl(file_path, incoming_folders)


_is_in_nested_incoming_folder._is_watchtower_wrapper = True


def is_filing_baseline_established(database_path: Path, incoming_folders: list[str] | None = None) -> tuple[bool, str]:
    from librarymanager.services.filing import is_filing_baseline_established as _impl
    return _impl(database_path, incoming_folders)


is_filing_baseline_established._is_watchtower_wrapper = True


def snapshot_incoming_baseline(database_path: Path, incoming_folders: list[str]) -> int:
    from librarymanager.services.filing import snapshot_incoming_baseline as _impl
    return _impl(database_path, incoming_folders)


snapshot_incoming_baseline._is_watchtower_wrapper = True


def prune_resolved_filing_baseline(database_path: Path, config: dict | None = None) -> dict:
    from librarymanager.services.filing import prune_resolved_filing_baseline as _impl
    return _impl(database_path, config)


prune_resolved_filing_baseline._is_watchtower_wrapper = True


def is_disqualified_from_filing(
    database_path: Path,
    file_path: str,
    file_size: int,
    oshash: str | None,
    file_id: str | None,
    scene_id: str | None,
    allow_baseline: bool = False,
    allow_refresh: bool = False,
) -> tuple[bool, str]:
    from librarymanager.services.filing import is_disqualified_from_filing as _impl
    return _impl(
        database_path,
        file_path,
        file_size,
        oshash,
        file_id,
        scene_id,
        allow_baseline=allow_baseline,
        allow_refresh=allow_refresh,
    )


is_disqualified_from_filing._is_watchtower_wrapper = True


def find_filing_companions(
    source_video: Path, dest_folder: Path, dest_video_name: str
) -> tuple[list[tuple[Path, Path]], str | None]:
    from librarymanager.services.filing import find_filing_companions as _impl
    return _impl(source_video, dest_folder, dest_video_name)


find_filing_companions._is_watchtower_wrapper = True


def _update_proposal_status(
    database_path: Path, proposal_id: int, status: str, last_error: str | None = None
) -> None:
    from librarymanager.services.filing import _update_proposal_status as _impl
    return _impl(database_path, proposal_id, status, last_error)


_update_proposal_status._is_watchtower_wrapper = True


def record_incoming_filing_diagnostic(database_path: Path, file_path: str, diagnostic: str):
    from librarymanager.services.filing import record_incoming_filing_diagnostic as _impl
    return _impl(database_path, file_path, diagnostic)


record_incoming_filing_diagnostic._is_watchtower_wrapper = True


def evaluate_filing_proposal(
    database_path: Path,
    stash,
    file_path: str,
    scene: dict,
    config: dict,
    allow_baseline: bool = False,
    allow_refresh: bool = False,
) -> dict | None:
    from librarymanager.services.filing import evaluate_filing_proposal as _impl
    return _impl(
        database_path,
        stash,
        file_path,
        scene,
        config,
        allow_baseline=allow_baseline,
        allow_refresh=allow_refresh,
    )


evaluate_filing_proposal._is_watchtower_wrapper = True


def retry_filing_proposal(
    database_path: Path,
    stash,
    file_path: str,
    config: dict = None,
    allow_baseline: bool = False,
    allow_refresh: bool = False,
    proposal_id: int | None = None,
    force_rescan: bool = False,
) -> dict:
    from librarymanager.services.filing import retry_filing_proposal as _impl
    return _impl(
        database_path,
        stash,
        file_path,
        config=config,
        allow_baseline=allow_baseline,
        allow_refresh=allow_refresh,
        proposal_id=proposal_id,
        force_rescan=force_rescan,
    )


retry_filing_proposal._is_watchtower_wrapper = True


def _move_file(source: Path | str, target: Path | str):
    from librarymanager.services.filing import _move_file as _impl
    return _impl(source, target)


_move_file._is_watchtower_wrapper = True


def _format_bytes(num_bytes: int | float) -> str:
    from librarymanager.services.filing import _format_bytes as _impl
    return _impl(num_bytes)


_format_bytes._is_watchtower_wrapper = True


def _update_active_transfer(
    database_path: Path,
    proposal_id: int,
    scene_id: str,
    file_id: str,
    source_path: str,
    destination_path: str,
    destination_folder: str,
    stage: str,
    stage_label: str,
    detail: str,
    total_bytes: int = 0,
):
    from librarymanager.services.filing import _update_active_transfer as _impl
    return _impl(
        database_path,
        proposal_id,
        scene_id,
        file_id,
        source_path,
        destination_path,
        destination_folder,
        stage,
        stage_label,
        detail,
        total_bytes=total_bytes,
    )


_update_active_transfer._is_watchtower_wrapper = True


def _clear_active_transfer(database_path: Path, proposal_id: int):
    from librarymanager.services.filing import _clear_active_transfer as _impl
    return _impl(database_path, proposal_id)


_clear_active_transfer._is_watchtower_wrapper = True


def enqueue_filing_transfer(
    database_path: Path,
    proposal_id: int,
    target_destination_folder: str,
    target_entity_type: str = None,
    target_entity_id: str = None,
    update_metadata: bool = False,
) -> bool:
    from librarymanager.services.filing import enqueue_filing_transfer as _impl
    return _impl(
        database_path,
        proposal_id,
        target_destination_folder,
        target_entity_type=target_entity_type,
        target_entity_id=target_entity_id,
        update_metadata=update_metadata,
    )


enqueue_filing_transfer._is_watchtower_wrapper = True


def claim_next_filing_transfer(database_path: Path):
    from librarymanager.services.filing import claim_next_filing_transfer as _impl
    return _impl(database_path)


claim_next_filing_transfer._is_watchtower_wrapper = True


def finish_filing_transfer(database_path: Path, queue_id: int) -> None:
    from librarymanager.services.filing import finish_filing_transfer as _impl
    return _impl(database_path, queue_id)


finish_filing_transfer._is_watchtower_wrapper = True


def fail_filing_transfer(database_path: Path, queue_id: int, error: str) -> None:
    from librarymanager.services.filing import fail_filing_transfer as _impl
    return _impl(database_path, queue_id, error)


fail_filing_transfer._is_watchtower_wrapper = True


def set_filing_transfer_job_id(database_path: Path, proposal_id: int, job_id: str) -> None:
    from librarymanager.services.filing import set_filing_transfer_job_id as _impl
    return _impl(database_path, proposal_id, job_id)


set_filing_transfer_job_id._is_watchtower_wrapper = True


def recover_abandoned_processing_queue(database_path: Path, stash=None) -> dict:
    from librarymanager.services.filing import recover_abandoned_processing_queue as _impl
    return _impl(database_path, stash=stash)


recover_abandoned_processing_queue._is_watchtower_wrapper = True


def get_active_filing_transfers(database_path: Path) -> list[dict]:
    from librarymanager.services.filing import get_active_filing_transfers as _impl
    return _impl(database_path)


get_active_filing_transfers._is_watchtower_wrapper = True


def recover_stale_active_transfers(
    database_path: Path, stale_threshold_minutes: int = 10, stash=None
) -> list[dict]:
    from librarymanager.services.filing import recover_stale_active_transfers as _impl
    return _impl(database_path, stale_threshold_minutes=stale_threshold_minutes, stash=stash)


recover_stale_active_transfers._is_watchtower_wrapper = True


def apply_filing_proposal(
    database_path: Path,
    stash,
    proposal_id: int,
    config: dict = None,
    update_metadata: bool = False,
    target_destination_folder: str = None,
    target_entity_type: str = None,
    target_entity_id: str = None,
) -> dict:
    from librarymanager.services.filing import apply_filing_proposal as _impl
    return _impl(
        database_path,
        stash,
        proposal_id,
        config=config,
        update_metadata=update_metadata,
        target_destination_folder=target_destination_folder,
        target_entity_type=target_entity_type,
        target_entity_id=target_entity_id,
    )


apply_filing_proposal._is_watchtower_wrapper = True


def ignore_filing_proposal(database_path: Path, proposal_id: int) -> dict:
    from librarymanager.services.filing import ignore_filing_proposal as _impl
    return _impl(database_path, proposal_id)


ignore_filing_proposal._is_watchtower_wrapper = True


def invalidate_stale_filing_proposals(database_path: Path, stash=None) -> list[int]:
    from librarymanager.services.filing import invalidate_stale_filing_proposals as _impl
    return _impl(database_path, stash=stash)


invalidate_stale_filing_proposals._is_watchtower_wrapper = True


def get_pending_filing_proposals(database_path: Path, stash=None) -> list[dict]:
    from librarymanager.services.filing import get_pending_filing_proposals as _impl
    return _impl(database_path, stash=stash)


get_pending_filing_proposals._is_watchtower_wrapper = True


def recover_filing_proposal(database_path: Path, stash, proposal_id: int) -> dict:
    from librarymanager.services.filing import recover_filing_proposal as _impl
    return _impl(database_path, stash, proposal_id)


recover_filing_proposal._is_watchtower_wrapper = True


def process_incoming_file_now(database_path: Path, file_path: str) -> dict:
    """Bypass the settling delay for an individual waiting video after explicit user confirmation.
    - Validates file existence and incoming queue membership.
    - Rejects already-processing, scanning, downloading, or imported items.
    - Checks file size and mtime on disk: if modified, stops safely and restarts settling.
    - If valid and unchanged, fast-forwards stable_since in incoming_files to make it immediately due.
    """
    path_str = str(file_path).strip()
    if not path_str:
        return {"success": False, "error": "File path is required"}

    p = Path(path_str)
    if not p.is_file():
        return {"success": False, "error": f"File does not exist on disk: {path_str}"}

    connection = connect(database_path)
    try:
        row = connection.execute("SELECT * FROM incoming_files WHERE path=?", (path_str,)).fetchone()
        if not row:
            return {"success": False, "error": f"File is not in the incoming queue: {p.name}"}

        curr_status = row["status"]
        if curr_status in ("scanning", "downloading", "generating_sheet"):
            return {"success": False, "error": f"File is already being processed ({curr_status})"}
        elif curr_status == "imported":
            return {"success": False, "error": "File has already been imported into Stash"}
        elif curr_status != "waiting":
            return {"success": False, "error": f"File is in '{curr_status}' state and cannot be processed now"}

        try:
            stat = p.stat()
        except OSError as err:
            return {"success": False, "error": f"File could not be accessed: {err}"}

        # Safety check: if file changed on disk before or during click, stop safely and restart wait
        if row["size"] is not None and (stat.st_size != row["size"] or stat.st_mtime_ns != row["modified_ns"]):
            now_ts = time.time()
            connection.execute(
                "UPDATE incoming_files SET size=?, modified_ns=?, stable_since=?, detail='File modified; settling restarted' WHERE path=?",
                (stat.st_size, stat.st_mtime_ns, now_ts, path_str)
            )
            connection.commit()
            record_activity(database_path, "incoming", "file modified", "waiting", new_path=path_str,
                            detail="File changed on disk; settling delay restarted for safety")
            return {
                "success": False,
                "error": "File was modified on disk; settling delay has been restarted for safety.",
                "restarted": True
            }

        # Bypass settling delay by making stable_since due
        settle_sec = row["settle_seconds"] or 300
        due_ts = time.time() - settle_sec - 1.0
        connection.execute(
            "UPDATE incoming_files SET stable_since=?, detail='User bypassed settling delay' WHERE path=?",
            (due_ts, path_str)
        )
        connection.commit()
        record_activity(database_path, "incoming", "process now", "waiting", new_path=path_str,
                        detail=f"User bypassed settling delay for {p.name}; processing initiated")
        return {
            "success": True,
            "path": path_str,
            "message": f"Settling delay bypassed for {p.name}. Processing initiated."
        }
    finally:
        connection.close()



def get_backlog_items(database_path: Path, stash=None, config: dict = None, force_refresh: bool = False) -> dict:
    """Return current Incoming work plus unresolved activation protection rows."""
    if config is None:
        config = (stash.find_plugin_config("librarymanager") if hasattr(stash, "find_plugin_config") else {}) or {}

    incoming_folders = get_configured_incoming_folders(config)
    unavailable_roots = get_authoritative_unavailable_roots(database_path, incoming_folders)
    prune_resolved_filing_baseline(database_path, config)
    connection = connect(database_path)
    try:
        rows = connection.execute(
            """SELECT path, size, modified_ns, oshash, seen_at
               FROM filing_incoming_baseline
               ORDER BY path ASC"""
        ).fetchall()

        items = []
        video_count = 0
        companion_count = 0
        eligible_count = 0
        pending_proposal_count = 0
        remaining_companion_count = 0
        verified_moved_video_count = 0
        verified_moved_companion_count = 0
        resolved_duplicate_video_count = 0
        resolved_duplicate_companion_count = 0
        duplicate_review_count = 0
        missing_video_count = 0
        missing_companion_count = 0
        acknowledged_missing_count = 0
        baseline_summary = connection.execute(
            "SELECT * FROM filing_baseline_summary WHERE id=1"
        ).fetchone()

        acknowledged_paths = {
            row["path"] for row in connection.execute(
                "SELECT path FROM filing_baseline_acknowledgements"
            ).fetchall()
        }

        resolved_duplicate_paths = {}
        for repair_row in connection.execute(
            """SELECT candidate_path,scene_id,retained_path,deleted_companions_json
               FROM duplicate_file_repairs WHERE status='completed'"""
        ).fetchall():
            repair = dict(repair_row)
            resolved_duplicate_paths[repair["candidate_path"]] = repair
            try:
                companion_paths = json.loads(repair.get("deleted_companions_json") or "[]")
            except (TypeError, ValueError):
                companion_paths = []
            for companion_path in companion_paths:
                resolved_duplicate_paths[str(companion_path)] = repair

        # Pre-fetch existing proposals and files for efficiency
        proposals_by_path = {}
        for p_row in connection.execute(
            "SELECT id, file_id, scene_id, source_path, proposed_path, destination_folder, status FROM filing_proposals"
        ).fetchall():
            proposals_by_path[p_row["source_path"]] = dict(p_row)

        files_by_path = {}
        files_by_id = {}
        for f_row in connection.execute(
            "SELECT file_id, scene_id, path, basename, title FROM files WHERE exists_on_disk=1"
        ).fetchall():
            files_by_path[f_row["path"]] = dict(f_row)
            files_by_id[str(f_row["file_id"])] = dict(f_row)

        # Baseline paths are intentionally immutable. Follow Watchtower's own
        # completed filename history when presenting their current location.
        renamed_paths = {}
        for rename_row in connection.execute(
            """SELECT old_path,new_path FROM activity_log
               WHERE old_path IS NOT NULL AND new_path IS NOT NULL
                 AND status IN ('renamed','complete')
               ORDER BY id ASC"""
        ).fetchall():
            renamed_paths[str(rename_row["old_path"])] = str(rename_row["new_path"])

        def current_recorded_path(original_path: str) -> str:
            candidate = original_path
            visited = set()
            while candidate in renamed_paths and candidate not in visited:
                visited.add(candidate)
                candidate = renamed_paths[candidate]
            return candidate if Path(candidate).is_file() else original_path

        # Read active incoming lifecycle states to avoid displaying half-downloaded or settling files
        active_incoming_states = {}
        diagnostics_by_path = {}
        for inc_row in connection.execute(
            "SELECT path, filing_diagnostic, status FROM incoming_files"
        ).fetchall():
            active_incoming_states[inc_row["path"]] = inc_row["status"]
            if inc_row["filing_diagnostic"]:
                diagnostics_by_path[inc_row["path"]] = inc_row["filing_diagnostic"]

        # The immutable baseline protects original files, but it must not freeze
        # the organiser's working list forever. Add files that currently exist
        # in Incoming using cached directory discovery.
        represented_current_paths = {
            current_recorded_path(str(row["path"])) for row in rows
        }
        discovered_files = discover_incoming_files_cached(
            incoming_folders,
            active_incoming_states,
            force_refresh=force_refresh
        )
        for disc in discovered_files:
            if disc["path"] not in represented_current_paths:
                rows.append(disc)
                represented_current_paths.add(disc["path"])

        # A previously completed move may carry a stale historical proposal
        # status. Treat it as moved only when the destination exists and the
        # preserved Stash file ID and scene ID both match the current inventory.
        verified_moved_proposals_by_src = {}
        for src, proposal in proposals_by_path.items():
            proposed_path = proposal.get("proposed_path")
            if not proposed_path or Path(src).suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            destination_file = files_by_path.get(proposed_path)
            if not destination_file and proposal.get("status") == "completed":
                current_identity = files_by_id.get(str(proposal.get("file_id") or ""))
                if (
                    current_identity
                    and str(current_identity.get("scene_id") or "") == str(proposal.get("scene_id") or "")
                    and Path(str(current_identity.get("path") or "")).is_file()
                ):
                    destination_file = current_identity
                    proposal = dict(proposal)
                    proposal["proposed_path"] = str(current_identity["path"])
                    proposed_path = proposal["proposed_path"]
            identity_matches = bool(
                destination_file
                and str(destination_file.get("file_id") or "") == str(proposal.get("file_id") or "")
                and str(destination_file.get("scene_id") or "") == str(proposal.get("scene_id") or "")
            )
            if Path(proposed_path).is_file() and (
                proposal.get("status") == "completed" or identity_matches
            ):
                verified_moved_proposals_by_src[src] = proposal



        for row in rows:
            baseline_path = row["path"]
            path_str = current_recorded_path(baseline_path)
            p_obj = Path(path_str)
            if p_obj.name.lower() in SYSTEM_JUNK_FILENAMES:
                continue
            ext = p_obj.suffix.lower()
            is_video = ext in VIDEO_EXTENSIONS
            is_file = p_obj.is_file()

            # Check if physically inside incoming folders
            is_inside_incoming = False
            if is_file and incoming_folders:
                try:
                    is_inside_incoming = any(
                        _is_subpath_of(p_obj, f)
                        for f in incoming_folders
                    )
                except Exception:
                    is_inside_incoming = False
            elif is_file and not incoming_folders:
                is_inside_incoming = True

            if not is_video:
                companion_count += 1
                destination_path = None
                duplicate_repair = resolved_duplicate_paths.get(baseline_path) if not is_file else None
                if duplicate_repair:
                    resolved_duplicate_companion_count += 1
                    status_code = "duplicate_removed"
                    status_label = "Duplicate Companion Removed"
                    destination_path = duplicate_repair.get("retained_path")
                    exists_on_disk = False
                elif is_file and is_inside_incoming:
                    remaining_companion_count += 1
                    status_code = "companion"
                    status_label = f"{ext.replace('.', '').upper() if ext else 'Non-video'} Companion"
                    exists_on_disk = True
                else:
                    # Check if corresponding video was verified moved
                    matched_video_prop = None
                    for v_src, v_prop in verified_moved_proposals_by_src.items():
                        if path_str.startswith(v_src) or (p_obj.stem == Path(v_src).stem and p_obj.parent == Path(v_src).parent):
                            matched_video_prop = v_prop
                            break
                    if matched_video_prop and matched_video_prop.get("proposed_path"):
                        dst_video = Path(matched_video_prop["proposed_path"])
                        comp_dst = dst_video.parent / p_obj.name
                        if comp_dst.is_file():
                            verified_moved_companion_count += 1
                            status_code = "moved"
                            status_label = f"{ext.replace('.', '').upper() if ext else 'Non-video'} Companion (Moved)"
                            destination_path = str(comp_dst)
                            exists_on_disk = True
                        elif path_str in acknowledged_paths:
                            acknowledged_missing_count += 1
                            status_code = "acknowledged_missing"
                            status_label = "Removal Acknowledged"
                            destination_path = str(comp_dst)
                            exists_on_disk = False
                        else:
                            missing_companion_count += 1
                            status_code = "missing_on_disk"
                            status_label = f"{ext.replace('.', '').upper() if ext else 'Non-video'} Companion (Missing)"
                            destination_path = str(comp_dst)
                            exists_on_disk = False
                    elif path_str in acknowledged_paths:
                        acknowledged_missing_count += 1
                        status_code = "acknowledged_missing"
                        status_label = "Removal Acknowledged"
                        destination_path = None
                        exists_on_disk = False
                    elif is_file_on_unavailable_root(path_str, unavailable_roots) or is_file_on_unavailable_root(baseline_path, unavailable_roots):
                        status_code = "root_unavailable"
                        status_label = f"{ext.replace('.', '').upper() if ext else 'Non-video'} Companion (Folder Unavailable)"
                        destination_path = None
                        exists_on_disk = False
                    else:
                        missing_companion_count += 1
                        status_code = "missing_on_disk"
                        status_label = f"{ext.replace('.', '').upper() if ext else 'Non-video'} Companion (Missing)"
                        destination_path = None
                        exists_on_disk = False

                items.append({
                    "path": path_str,
                    "baseline_path": baseline_path,
                    "basename": p_obj.name,
                    "size": row["size"] or 0,
                    "is_video": False,
                    "is_companion": True,
                    "exists_on_disk": exists_on_disk,
                    "is_inside_incoming": is_inside_incoming,
                    "eligible": False,
                    "status": status_code,
                    "status_label": status_label,
                    "destination_path": destination_path,
                    "scene_id": None,
                    "scene_title": None,
                    "diagnostic": None,
                    "seen_at": row["seen_at"]
                })
                continue

            video_count += 1

            # Check proposals
            prop = proposals_by_path.get(baseline_path)
            verified_move = verified_moved_proposals_by_src.get(baseline_path)

            # Check linked Stash scene (from files table or proposal)
            f_info = files_by_path.get(path_str)
            scene_id = str(f_info["scene_id"]) if f_info and f_info.get("scene_id") else (str(prop["scene_id"]) if prop and prop.get("scene_id") else None)
            scene_title = f_info.get("title") if f_info else None
            filing_status = prop["status"] if prop else None
            proposed_dst = Path(prop["proposed_path"]) if prop and prop.get("proposed_path") else None
            dst_exists = proposed_dst.is_file() if proposed_dst else False

            diag = diagnostics_by_path.get(path_str)

            eligible = False
            destination_path = None
            duplicate_info = None
            duplicate_repair = resolved_duplicate_paths.get(baseline_path) if not is_file else None

            if duplicate_repair:
                resolved_duplicate_video_count += 1
                status_code = "duplicate_removed"
                status_label = "Exact Duplicate Removed"
                exists_on_disk = False
                destination_path = duplicate_repair.get("retained_path")
                scene_id = str(duplicate_repair.get("scene_id") or scene_id or "") or None
            elif verified_move:
                proposed_dst = Path(verified_move["proposed_path"])
                destination_path = str(proposed_dst)
                verified_moved_video_count += 1
                status_code = "moved"
                status_label = "Moved out of Incoming"
                exists_on_disk = True
                destination_info = files_by_path.get(str(proposed_dst))
                if destination_info:
                    scene_id = str(destination_info.get("scene_id") or scene_id or "") or None
                    scene_title = destination_info.get("title") or scene_title
            elif path_str in acknowledged_paths and not is_file:
                acknowledged_missing_count += 1
                status_code = "acknowledged_missing"
                status_label = "Removal Acknowledged"
                exists_on_disk = False
                destination_path = str(proposed_dst) if proposed_dst else None
            elif filing_status == "completed":
                destination_path = str(proposed_dst) if proposed_dst else None
                if dst_exists:
                    verified_moved_video_count += 1
                    status_code = "moved"
                    status_label = "Moved out of Incoming"
                    exists_on_disk = True
                else:
                    missing_video_count += 1
                    status_code = "moved_destination_missing"
                    status_label = "Moved (Destination Missing)"
                    exists_on_disk = False
            elif filing_status == "needs_recovery":
                status_code = "needs_recovery"
                status_label = "Unresolved Recovery"
                exists_on_disk = is_file
                destination_path = str(proposed_dst) if proposed_dst else None
            elif filing_status == "pending":
                pending_proposal_count += 1
                status_code = "has_proposal"
                status_label = "Active Proposal Pending"
                exists_on_disk = is_file
                destination_path = str(proposed_dst) if proposed_dst else None
            elif is_file and is_inside_incoming:
                try:
                    duplicate_info = inspect_backlog_duplicate(
                        database_path, None, path_str, config, request_verification=False
                    )
                except (OSError, RuntimeError, ValueError):
                    duplicate_info = None
                if duplicate_info:
                    duplicate_review_count += 1
                    status_code = "exact_duplicate" if duplicate_info.get("checksum_status") == "verified" else "duplicate_candidate"
                    status_label = "Exact Duplicate Verified" if duplicate_info.get("checksum_status") == "verified" else "Duplicate Review"
                    exists_on_disk = True
                    destination_path = duplicate_info.get("retained_path")
                    diag = duplicate_info.get("reason")
                else:
                    eligible = True
                    eligible_count += 1
                    status_code = "eligible"
                    status_label = "Eligible"
                    exists_on_disk = True
            elif is_file and not is_inside_incoming:
                status_code = "outside_incoming"
                status_label = "Outside Incoming"
                exists_on_disk = True
            elif path_str in acknowledged_paths:
                acknowledged_missing_count += 1
                status_code = "acknowledged_missing"
                status_label = "Removal Acknowledged"
                exists_on_disk = False
            elif is_file_on_unavailable_root(path_str, unavailable_roots) or is_file_on_unavailable_root(baseline_path, unavailable_roots):
                status_code = "root_unavailable"
                status_label = "Incoming Folder Unavailable"
                exists_on_disk = False
                diag = "Incoming folder unavailable. Reconnect the disk, then recheck."
            else:
                missing_video_count += 1
                status_code = "missing_on_disk"
                status_label = "File Missing on Disk"
                exists_on_disk = False
                diag = "The file was recorded in the protected Incoming snapshot, but that path no longer exists. Recheck after restoring it, or acknowledge an intentional removal."

            items.append({
                "path": path_str,
                "baseline_path": baseline_path,
                "basename": p_obj.name,
                "size": row["size"] or (p_obj.stat().st_size if is_file else 0),
                "is_video": True,
                "is_companion": False,
                "exists_on_disk": exists_on_disk,
                "is_inside_incoming": is_inside_incoming,
                "eligible": eligible,
                "status": status_code,
                "status_label": status_label,
                "destination_path": destination_path,
                "scene_id": scene_id,
                "scene_title": scene_title,
                "diagnostic": diag,
                "duplicate_info": duplicate_info,
                "seen_at": row["seen_at"]
            })

        remaining_incoming_count = sum(1 for it in items if it["exists_on_disk"] and it["is_inside_incoming"])
        archived_filed_count = int(baseline_summary["filed_count"] or 0) if baseline_summary else 0
        archived_duplicate_count = int(baseline_summary["duplicate_count"] or 0) if baseline_summary else 0
        archived_acknowledged_count = int(baseline_summary["acknowledged_count"] or 0) if baseline_summary else 0
        verified_moved_count = verified_moved_video_count + verified_moved_companion_count + archived_filed_count
        resolved_duplicate_count = resolved_duplicate_video_count + resolved_duplicate_companion_count + archived_duplicate_count
        acknowledged_missing_count += archived_acknowledged_count
        missing_count = missing_video_count + missing_companion_count
        ineligible_count = max(0, video_count - eligible_count)
        needs_attention_count = sum(
            1 for item in items
            if item["status"] in {
                "missing_on_disk", "moved_destination_missing", "needs_recovery",
                "duplicate_candidate", "exact_duplicate",
            }
        )

        return {
            "total_count": len(rows),
            "baseline_total": int(baseline_summary["initial_count"] or 0) if baseline_summary else len(rows),
            "video_count": video_count,
            "companion_count": companion_count,
            "remaining_companion_count": remaining_companion_count,
            "remaining_incoming_count": remaining_incoming_count,
            "eligible_count": eligible_count,
            "pending_proposal_count": pending_proposal_count,
            "already_filed_count": verified_moved_video_count,
            "verified_moved_video_count": verified_moved_video_count,
            "verified_moved_companion_count": verified_moved_companion_count,
            "verified_moved_count": verified_moved_count,
            "resolved_duplicate_video_count": resolved_duplicate_video_count,
            "resolved_duplicate_companion_count": resolved_duplicate_companion_count,
            "resolved_duplicate_count": resolved_duplicate_count,
            "duplicate_review_count": duplicate_review_count,
            "missing_count": missing_count,
            "acknowledged_missing_count": acknowledged_missing_count,
            "needs_attention_count": needs_attention_count,
            "ineligible_count": ineligible_count,
            "unavailable_roots": unavailable_roots,
            "unavailable_root_count": len(unavailable_roots),
            "items": items
        }
    finally:
        connection.close()


def acknowledge_backlog_missing(
    database_path: Path,
    file_paths: list[str],
    config: dict = None
) -> dict:
    """Retire deliberately removed paths while retaining aggregate history."""
    requested = sorted({str(path) for path in (file_paths or []) if str(path).strip()})
    if not requested:
        raise ValueError("At least one missing baseline path is required")
    incoming_folders = get_configured_incoming_folders(config or {})
    unavailable_roots = get_authoritative_unavailable_roots(database_path, incoming_folders)
    for path_str in requested:
        if is_file_on_unavailable_root(path_str, unavailable_roots):
            raise ValueError(
                f"Cannot acknowledge removal: incoming folder is currently unavailable for '{path_str}'. "
                "Reconnect the disk, then recheck."
            )
    acknowledged = []
    skipped = []
    connection = connect(database_path)
    try:
        for path_str in requested:
            baseline = connection.execute(
                "SELECT 1 FROM filing_incoming_baseline WHERE path=?", (path_str,)
            ).fetchone()
            if baseline is None or Path(path_str).exists():
                skipped.append(path_str)
                continue
            connection.execute(
                """INSERT INTO filing_baseline_acknowledgements(path,reason,acknowledged_at)
                   VALUES (?, 'intentionally_removed', ?)
                   ON CONFLICT(path) DO UPDATE SET acknowledged_at=excluded.acknowledged_at""",
                (path_str, utc_now()),
            )
            acknowledged.append(path_str)
        connection.commit()
    finally:
        connection.close()
    return {"success": True, "acknowledged": acknowledged, "skipped": skipped}


def evaluate_backlog_batch(
    database_path: Path,
    stash,
    file_paths: list[str],
    config: dict = None,
    allow_refresh: bool = False,
) -> dict:
    """Evaluate a small batch of selected baseline backlog video files.
    - Processes files safely and sequentially without overwhelming disk I/O.
    - Preserves existing scene IDs, metadata, and baseline protection for unselected files.
    - Never moves files automatically.
    - Returns structured outcome tallies and per-item results.
    """
    if config is None:
        config = (stash.find_plugin_config("librarymanager") if hasattr(stash, "find_plugin_config") else {}) or {}

    results = []
    tally = {
        "proposal_ready": 0,
        "candidate_selection_required": 0,
        "no_identity_found": 0,
        "destination_not_found": 0,
        "ambiguous_match": 0,
        "already_filed": 0,
        "duplicate_review": 0,
        "ineligible": 0,
        "errors": 0
    }

    for path_str in file_paths:
        try:
            identity_connection = connect(database_path)
            try:
                identity_row = identity_connection.execute(
                    "SELECT file_id,scene_id FROM files WHERE path=? ORDER BY exists_on_disk DESC LIMIT 1",
                    (str(path_str),),
                ).fetchone()
            finally:
                identity_connection.close()
            result_file_id = str(identity_row["file_id"]) if identity_row and identity_row["file_id"] else None
            result_scene_id = str(identity_row["scene_id"]) if identity_row and identity_row["scene_id"] else None
            res = retry_filing_proposal(
                database_path, stash, path_str, config=config,
                allow_baseline=True, allow_refresh=bool(allow_refresh),
            )
            outcome = "ineligible"
            if res.get("success"):
                prop = res.get("proposal") or {}
                if prop.get("status") == "needs_selection" or prop.get("candidates"):
                    outcome = "candidate_selection_required"
                    tally["candidate_selection_required"] += 1
                else:
                    outcome = "proposal_ready"
                    tally["proposal_ready"] += 1
            else:
                err_or_diag = (res.get("diagnostic") or res.get("error") or res.get("message") or "").lower()
                duplicate_info = None
                if "already attached to stash scene" in err_or_diag or "matching library file" in err_or_diag:
                    duplicate_info = inspect_backlog_duplicate(
                        database_path, stash, path_str, config, request_verification=False
                    )
                if duplicate_info:
                    outcome = "duplicate_review"
                    tally["duplicate_review"] += 1
                elif "already" in err_or_diag or "completed" in err_or_diag:
                    outcome = "already_filed"
                    tally["already_filed"] += 1
                elif "no matching performer" in err_or_diag or "no identity" in err_or_diag or "no performer or studio" in err_or_diag:
                    outcome = "no_identity_found"
                    tally["no_identity_found"] += 1
                elif "no destination folder" in err_or_diag or "destination_not_found" in err_or_diag:
                    outcome = "destination_not_found"
                    tally["destination_not_found"] += 1
                elif "ambiguous" in err_or_diag:
                    outcome = "ambiguous_match"
                    tally["ambiguous_match"] += 1
                else:
                    outcome = "ineligible"
                    tally["ineligible"] += 1

            results.append({
                "path": path_str,
                "basename": Path(path_str).name,
                "success": res.get("success", False),
                "outcome": outcome,
                "diagnostic": res.get("diagnostic"),
                "error": res.get("error"),
                "message": res.get("message"),
                "proposal": res.get("proposal"),
                "file_id": result_file_id,
                "scene_id": result_scene_id,
                "duplicate_info": duplicate_info if not res.get("success") else None,
            })
        except Exception as exc:
            logger.error("Error evaluating backlog item %s: %s", path_str, exc)
            tally["errors"] += 1
            results.append({
                "path": path_str,
                "basename": Path(path_str).name,
                "success": False,
                "outcome": "errors",
                "error": str(exc),
                "message": str(exc)
            })

    return {
        "results": results,
        "tally": tally
    }


# Backward compatibility exports for database schema and migrations
from librarymanager.database.schema import SCHEMA, RECONCILIATION_SCHEMA  # noqa: E402
from librarymanager.database.migrations import (  # noqa: E402
    _schema_applied,
    _schema_lock,
)

# Backward compatibility exports for Stash GraphQL integration
from librarymanager.integrations.stash_graphql import (  # noqa: E402
    QUERY,
    INVENTORY_SCENES_QUERY,
    SCENE_QUERY,
    PERFORMER_SCENES_QUERY,
    STUDIO_SCENES_QUERY,
    ROOTS_QUERY,
    SCENE_COUNT_QUERY,
    DELETE_FILES_MUTATION,
    check_scenes_batch,
    check_scene_files,
    check_scene_exists,
    current_scene_count,
    delete_files,
    ensure_stash_supports_extension,
    execute_gql,
    fetch_all_files,
    fetch_all_performers,
    fetch_all_studios,
    fetch_library_roots,
    fetch_scene,
    fetch_scenes,
    find_scene_by_path,
)




from librarymanager.services.filing_rules import (
    NOISE_ALIASES,
    _TAG_FOLDER_DESCRIPTOR_TOKENS,
    _EXCLUDED_DESTINATION_DISCOVERY_NAMES,
    _build_token_regex,
    _single_token_alias_is_standalone,
    _normalize_name_for_folder_match,
    _compact_name_for_folder_match,
    _strip_conservative_folder_descriptors,
    _filter_subsumed_matches,
    _singular_category_token,
    _category_name_tokens,
    _DESTINATION_DIR_CACHE,
    _DESTINATION_DIR_CACHE_LOCK,
    _DESTINATION_DIR_CACHE_TTL,
    _INCOMING_DISCOVERY_CACHE,
    _INCOMING_DISCOVERY_CACHE_LOCK,
    _INCOMING_DISCOVERY_CACHE_TTL,
)


def __getattr__(name: str):
    if name == "_CACHE_GENERATION":
        from librarymanager.services.filing_rules import _CACHE_GENERATION
        return _CACHE_GENERATION
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

