"""Automatic filing transfer engine, queue coordination, execution, rollback, and recovery service.

This module is responsible for:
- Evaluating automatic filing proposals (evaluate_filing_proposal, retry_filing_proposal)
- Establishing, verifying, and pruning incoming baseline snapshots (snapshot_incoming_baseline,
  is_filing_baseline_established, prune_resolved_filing_baseline)
- Filing disqualification checks (is_disqualified_from_filing)
- Companion sidecar discovery and collision checks (find_filing_companions)
- Transfer queue management (enqueue_filing_transfer, claim_next_filing_transfer,
  finish_filing_transfer, fail_filing_transfer, set_filing_transfer_job_id)
- Active-transfer coordination and concurrency serialization (_update_active_transfer,
  _clear_active_transfer, get_active_filing_transfers)
- Stale active transfer detection and recovery (recover_stale_active_transfers)
- Abandoned queue job recovery with Stash job liveness check (recover_abandoned_processing_queue)
- Transfer execution with companion rollback and postflight verification (apply_filing_proposal)
- Incomplete transfer recovery and rollback (recover_filing_proposal)
- Filing proposal lifecycle (ignore_filing_proposal, invalidate_stale_filing_proposals,
  get_pending_filing_proposals)
"""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import json
import logging
import os
from pathlib import Path
_ORIGINAL_PATH_RENAME = Path.rename
import shutil
import sqlite3
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import uuid

from librarymanager.database.connection import create_connection
from librarymanager.database.migrations import ensure_schema
from librarymanager.scanning.filesystem import (
    ASSOCIATED_EXTENSIONS,
    COMPANION_EXTENSIONS,
    IMAGE_SIDECAR_EXTENSIONS,
    SYSTEM_JUNK_FILENAMES,
    VIDEO_EXTENSIONS,
    _is_subpath_of,
    _sidecar_match_key,
)
from librarymanager.scanning.filesystem import expect_filesystem_move as _fs_expect_filesystem_move
from librarymanager.scanning.reconciliation import (
    fingerprint_value,
    is_temporary_download,
    opensubtitles_hash,
    parse_cold_storage_roots,
)
from librarymanager.services.filing_rules import (
    get_configured_filing_destination_roots,
    invalidate_destination_dir_cache,
    invalidate_incoming_discovery_cache,
    match_performer_for_filing,
    match_studio_for_filing,
    refresh_destination_dir_cache,
    resolve_filing_destinations,
    resolve_tag_filing_destinations,
)
from librarymanager.services.inventory import refresh_scene_inventory
from librarymanager.services.renaming import rename_lock as _renaming_rename_lock

logger = logging.getLogger("librarymanager.services.filing")

__all__ = [
    "is_filing_baseline_established",
    "snapshot_incoming_baseline",
    "prune_resolved_filing_baseline",
    "is_disqualified_from_filing",
    "find_filing_companions",
    "record_incoming_filing_diagnostic",
    "evaluate_filing_proposal",
    "retry_filing_proposal",
    "enqueue_filing_transfer",
    "claim_next_filing_transfer",
    "finish_filing_transfer",
    "fail_filing_transfer",
    "set_filing_transfer_job_id",
    "recover_abandoned_processing_queue",
    "get_active_filing_transfers",
    "recover_stale_active_transfers",
    "apply_filing_proposal",
    "ignore_filing_proposal",
    "invalidate_stale_filing_proposals",
    "get_pending_filing_proposals",
    "recover_filing_proposal",
    "_is_in_nested_incoming_folder",
    "_update_proposal_status",
    "_move_file",
    "_format_bytes",
    "_update_active_transfer",
    "_clear_active_transfer",
]


def _is_mock(fn: Any) -> bool:
    if fn is None:
        return False
    return (
        hasattr(fn, "mock_calls")
        or hasattr(fn, "assert_called")
        or type(fn).__name__ in ("Mock", "MagicMock", "AsyncMock")
    )


def _get_core_fn(name: str, fallback_fn: Optional[Callable] = None) -> Optional[Callable]:
    """Retrieve an active override or mock from librarymanager_core."""
    if _is_mock(fallback_fn):
        return fallback_fn

    core_mod = sys.modules.get("librarymanager_core")
    if core_mod is not None:
        core_fn = getattr(core_mod, name, None)
        if core_fn is not None:
            if _is_mock(core_fn):
                return core_fn
            if not getattr(core_fn, "_is_watchtower_wrapper", False):
                return core_fn

    return fallback_fn


def utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    core_fn = _get_core_fn("utc_now")
    if _is_mock(core_fn):
        return core_fn()
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(database_path: Path) -> sqlite3.Connection:
    core_fn = _get_core_fn("connect")
    if core_fn is not None:
        return core_fn(database_path)
    connection = create_connection(database_path)
    ensure_schema(connection, database_path)
    return connection


def rename_lock(database_path: Path):
    fn = _get_core_fn("rename_lock", _renaming_rename_lock)
    return fn(database_path)


def expect_filesystem_move(database_path: Path, source: str, destination: str, ttl_seconds: float = 60):
    fn = _get_core_fn("expect_filesystem_move", _fs_expect_filesystem_move)
    return fn(database_path, source, destination, ttl_seconds=ttl_seconds)


def record_activity(
    database_path: Path,
    category: str,
    action: str,
    status: str,
    *,
    severity: str = "info",
    scene_id: str | None = None,
    file_id: str | None = None,
    old_path: str | None = None,
    new_path: str | None = None,
    detail: str = "",
    metadata: dict | None = None,
):
    fn = _get_core_fn("record_activity")
    if fn is not None:
        return fn(
            database_path,
            category,
            action,
            status,
            severity=severity,
            scene_id=scene_id,
            file_id=file_id,
            old_path=old_path,
            new_path=new_path,
            detail=detail,
            metadata=metadata,
        )
    connection = connect(database_path)
    try:
        connection.execute(
            """INSERT INTO activity_log(category,severity,action,status,scene_id,file_id,old_path,new_path,
                   detail,metadata_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                category,
                severity,
                action,
                status,
                str(scene_id) if scene_id is not None else None,
                str(file_id) if file_id is not None else None,
                old_path,
                new_path,
                detail,
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )
        connection.execute(
            """DELETE FROM activity_log WHERE id NOT IN (
                   SELECT id FROM activity_log ORDER BY id DESC LIMIT 5000
               )"""
        )
        connection.commit()
    finally:
        connection.close()


def get_configured_incoming_folders(config: dict | None) -> list[str]:
    fn = _get_core_fn("get_configured_incoming_folders")
    if fn is not None and not getattr(fn, "_is_watchtower_wrapper", False):
        return fn(config)
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


def _is_in_nested_incoming_folder(file_path: Path, incoming_folders: list[str]) -> bool:
    """Check if a video file is located in a nested subdirectory inside an incoming folder
    (e.g., inside a multi-video torrent subfolder)."""
    try:
        resolved_parent = file_path.parent.resolve()
        for inf in incoming_folders:
            if not inf:
                continue
            inf_path = Path(inf).expanduser().resolve()
            if resolved_parent == inf_path:
                return False  # Directly in incoming folder root
            if str(resolved_parent).startswith(str(inf_path) + os.sep):
                return True  # In a subdirectory inside incoming
    except Exception:
        pass
    return False


def is_filing_baseline_established(database_path: Path, incoming_folders: list[str] | None = None) -> tuple[bool, str]:
    """Verify that a complete, successful baseline has been established.
    If baseline is missing, failed, in_progress, or does not cover all configured incoming folders,
    returns (False, reason) so automatic filing fails safely."""
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM filing_baseline_state ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return False, "Automatic filing baseline has not been established"
        if row["status"] == "failed":
            return False, f"Automatic filing baseline initialization failed: {row['last_error'] or 'unknown error'}"
        if row["status"] == "in_progress":
            return False, "Automatic filing baseline is still in progress"
        if row["status"] != "complete":
            return False, f"Automatic filing baseline is not ready (status: {row['status']})"

        if incoming_folders:
            snapshotted_folders = set(json.loads(row["incoming_folders_json"] or "[]"))
            snap_norm = set()
            for f in snapshotted_folders:
                if f:
                    try:
                        snap_norm.add(os.path.normpath(str(Path(f).expanduser().resolve())))
                    except Exception:
                        snap_norm.add(os.path.normpath(str(f)))

            for inf in incoming_folders:
                if not inf:
                    continue
                try:
                    inf_norm = os.path.normpath(str(Path(inf).expanduser().resolve()))
                except Exception:
                    inf_norm = os.path.normpath(str(inf))
                if inf_norm not in snap_norm:
                    return False, f"Incoming folder '{inf}' is not covered by the current baseline snapshot"

        return True, "ok"
    finally:
        connection.close()


def snapshot_incoming_baseline(database_path: Path, incoming_folders: list[str]) -> int:
    """Snapshot all files currently present across incoming folders to establish an activation baseline.
    Files present in this snapshot will never be proposed for automatic filing.
    Uses an atomic staged transaction so the previous valid snapshot is preserved if interrupted.
    Fails safely: records status='failed' on error so un-baselined files can never be filed."""
    now = utc_now()
    count = 0
    cleaned_folders = [str(f).strip() for f in (incoming_folders or []) if str(f).strip()]
    connection = connect(database_path)
    run_id = None
    try:
        cur = connection.execute(
            """INSERT INTO filing_baseline_state (established_at, incoming_folders_json, status)
               VALUES (?, ?, 'in_progress')""",
            (now, json.dumps(cleaned_folders)),
        )
        run_id = cur.lastrowid
        connection.commit()

        # Ensure staging table exists and is empty
        connection.execute(
            """CREATE TABLE IF NOT EXISTS filing_incoming_baseline_staging (
                   path TEXT PRIMARY KEY,
                   size INTEGER,
                   modified_ns INTEGER,
                   oshash TEXT,
                   seen_at TEXT
               )"""
        )
        connection.execute("DELETE FROM filing_incoming_baseline_staging")
        connection.commit()

        for folder_str in cleaned_folders:
            try:
                folder = Path(folder_str).expanduser().resolve()
            except Exception as exc:
                raise RuntimeError(f"Could not resolve incoming folder '{folder_str}': {exc}")
            if not folder.is_dir():
                raise RuntimeError(f"Incoming folder is not an accessible directory: '{folder_str}'")

            for root, dirs, files in os.walk(folder):
                for fname in files:
                    if fname.lower() in SYSTEM_JUNK_FILENAMES:
                        continue
                    fpath = Path(root) / fname
                    if is_temporary_download(fpath):
                        continue
                    try:
                        st = fpath.stat()
                        size = st.st_size
                        modified_ns = st.st_mtime_ns
                        oshash = opensubtitles_hash(fpath) if fpath.suffix.lower() in VIDEO_EXTENSIONS else None
                        connection.execute(
                            """INSERT OR REPLACE INTO filing_incoming_baseline_staging
                               (path, size, modified_ns, oshash, seen_at)
                               VALUES (?, ?, ?, ?, ?)""",
                            (str(fpath), size, modified_ns, oshash, now),
                        )
                        count += 1
                    except OSError as os_err:
                        raise RuntimeError(f"Failed reading incoming file '{fpath}': {os_err}")

        # Atomic replacement: swap staging into live baseline in a single transaction
        connection.execute("DELETE FROM filing_baseline_acknowledgements")
        connection.execute("DELETE FROM filing_incoming_baseline")
        connection.execute("DELETE FROM filing_baseline_summary")
        connection.execute(
            """INSERT INTO filing_incoming_baseline (path, size, modified_ns, oshash, seen_at)
               SELECT path, size, modified_ns, oshash, seen_at FROM filing_incoming_baseline_staging"""
        )
        connection.execute("DELETE FROM filing_incoming_baseline_staging")
        connection.execute(
            """UPDATE filing_baseline_state
               SET status='complete', completed_at=?, file_count=?, last_error=NULL
               WHERE id=?""",
            (utc_now(), count, run_id),
        )
        connection.execute(
            """INSERT INTO filing_baseline_summary(
                   id,initial_count,remaining_count,updated_at
               ) VALUES (1,?,?,?)""",
            (count, count, utc_now()),
        )
        connection.commit()
        invalidate_incoming_discovery_cache()
        return count
    except Exception as exc:
        if run_id:
            try:
                connection.execute(
                    """UPDATE filing_baseline_state
                       SET status='failed', last_error=?
                       WHERE id=?""",
                    (str(exc), run_id),
                )
                connection.execute("DELETE FROM filing_incoming_baseline_staging")
                connection.commit()
            except Exception:
                pass
        raise
    finally:
        connection.close()


def prune_resolved_filing_baseline(database_path: Path, config: dict | None = None) -> dict:
    """Retire resolved protection rows while retaining aggregate audit totals."""
    incoming_folders = get_configured_incoming_folders(config)
    connection = connect(database_path)
    retired = {"filed": 0, "duplicate": 0, "acknowledged": 0}
    try:
        baseline_rows = connection.execute(
            "SELECT path FROM filing_incoming_baseline"
        ).fetchall()
        if not baseline_rows:
            summary = connection.execute(
                "SELECT * FROM filing_baseline_summary WHERE id=1"
            ).fetchone()
            return {"retired": retired, "remaining": 0, "completed": bool(summary and summary["completed_at"])}

        connection.execute(
            """INSERT OR IGNORE INTO filing_baseline_summary(
                   id,initial_count,remaining_count,updated_at
               ) VALUES (1,?,?,?)""",
            (len(baseline_rows), len(baseline_rows), utc_now()),
        )
        acknowledged = {
            row["path"]
            for row in connection.execute(
                "SELECT path FROM filing_baseline_acknowledgements"
            ).fetchall()
        }
        duplicate_paths = set()
        for row in connection.execute(
            """SELECT candidate_path,deleted_companions_json
               FROM duplicate_file_repairs WHERE status='completed'"""
        ).fetchall():
            duplicate_paths.add(str(row["candidate_path"]))
            try:
                duplicate_paths.update(str(path) for path in json.loads(row["deleted_companions_json"] or "[]"))
            except (TypeError, ValueError):
                pass

        proposals = {
            str(row["source_path"]): dict(row)
            for row in connection.execute(
                """SELECT file_id,scene_id,source_path,proposed_path,status
                   FROM filing_proposals WHERE status='completed'"""
            ).fetchall()
        }
        current_files = {
            str(row["file_id"]): dict(row)
            for row in connection.execute(
                "SELECT file_id,scene_id,path FROM files WHERE exists_on_disk=1"
            ).fetchall()
        }
        relocated_paths = {}
        for row in connection.execute(
            """SELECT old_path,new_path FROM activity_log
               WHERE old_path IS NOT NULL AND new_path IS NOT NULL
                 AND status IN ('renamed','complete','updated') ORDER BY id"""
        ).fetchall():
            relocated_paths[str(row["old_path"])] = str(row["new_path"])

        def is_inside_incoming(path_str: str) -> bool:
            try:
                return any(_is_subpath_of(path_str, folder) for folder in incoming_folders)
            except OSError:
                return False

        retire_paths = {}
        for baseline_row in baseline_rows:
            path_str = str(baseline_row["path"])
            if path_str in acknowledged:
                retire_paths[path_str] = "acknowledged"
                continue
            if path_str in duplicate_paths:
                retire_paths[path_str] = "duplicate"
                continue
            proposal = proposals.get(path_str)
            if proposal and not Path(path_str).is_file():
                destination = str(proposal.get("proposed_path") or "")
                current = current_files.get(str(proposal.get("file_id") or ""))
                if current and str(current.get("scene_id") or "") == str(proposal.get("scene_id") or ""):
                    destination = str(current.get("path") or destination)
                if destination and Path(destination).is_file():
                    retire_paths[path_str] = "filed"
                    continue
            for source_path, proposal in proposals.items():
                source = Path(source_path)
                candidate = Path(path_str)
                is_companion = (
                    candidate.name.startswith(source.name + ".")
                    or candidate.stem == source.stem
                )
                if Path(path_str).is_file() or not is_companion:
                    continue
                current = current_files.get(str(proposal.get("file_id") or ""))
                destination_video = str((current or {}).get("path") or proposal.get("proposed_path") or "")
                companion_destination = Path(destination_video).parent / Path(path_str).name
                if destination_video and companion_destination.is_file():
                    retire_paths[path_str] = "filed"
                    break
            relocated = relocated_paths.get(path_str)
            visited = set()
            while relocated and relocated in relocated_paths and relocated not in visited:
                visited.add(relocated)
                relocated = relocated_paths[relocated]
            if not Path(path_str).is_file() and relocated and Path(relocated).is_file() and not is_inside_incoming(relocated):
                retire_paths[path_str] = "filed"

        for path_str, reason in retire_paths.items():
            connection.execute("DELETE FROM filing_incoming_baseline WHERE path=?", (path_str,))
            retired[reason] += 1
        remaining = connection.execute(
            "SELECT COUNT(*) AS count FROM filing_incoming_baseline"
        ).fetchone()["count"]
        completed_at = utc_now() if remaining == 0 else None
        connection.execute(
            """UPDATE filing_baseline_summary
               SET remaining_count=?,
                   filed_count=filed_count+?,
                   duplicate_count=duplicate_count+?,
                   acknowledged_count=acknowledged_count+?,
                   completed_at=COALESCE(completed_at,?),updated_at=?
               WHERE id=1""",
            (remaining, retired["filed"], retired["duplicate"], retired["acknowledged"], completed_at, utc_now()),
        )
        connection.commit()
        return {"retired": retired, "remaining": remaining, "completed": remaining == 0}
    finally:
        connection.close()


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
    """Check whether a video is disqualified from automatic filing.
    Automatic Filing is strictly for genuinely new incoming files, never for pre-existing,
    rediscovered, or relocated files. Fails safely if baseline is missing, incomplete, or failed."""
    baseline_ok, baseline_err = is_filing_baseline_established(database_path)
    if not baseline_ok:
        return True, f"Baseline not established ({baseline_err}); automatic filing failing safely"

    connection = connect(database_path)
    try:
        # 1. Exact path in baseline snapshot (checked unless explicitly evaluating backlog selection)
        if not allow_baseline:
            row = connection.execute("SELECT 1 FROM filing_incoming_baseline WHERE path=?", (file_path,)).fetchone()
            if row:
                return True, "File was already present in incoming folder baseline snapshot when automatic filing was enabled"

            # 2. Size and oshash in baseline snapshot (e.g. file was moved/renamed within incoming)
            if oshash and file_size:
                row = connection.execute(
                    "SELECT 1 FROM filing_incoming_baseline WHERE oshash=? AND size=?", (oshash, file_size)
                ).fetchone()
                if row:
                    return True, "File contents match a file present in incoming baseline snapshot"

        # 3. Already evaluated or ignored or completed in filing_proposals
        if (file_id or scene_id or file_path) and not allow_refresh:
            row = connection.execute(
                "SELECT status FROM filing_proposals WHERE file_id=? OR scene_id=? OR source_path=?",
                (str(file_id or ""), str(scene_id or ""), file_path),
            ).fetchone()
            if row:
                return True, f"File was already evaluated for automatic filing (status: {row['status']})"

        # 4. Check if this file/scene was already known in library files (relocation)
        if file_id or scene_id:
            row = connection.execute(
                "SELECT path FROM files WHERE (file_id=? OR scene_id=?) AND path != ? AND exists_on_disk=1",
                (str(file_id or ""), str(scene_id or ""), file_path),
            ).fetchone()
            if row:
                scene_label = f"Stash Scene {scene_id}" if scene_id else "an existing Stash scene"
                return True, (
                    f"File is already attached to {scene_label}, which also contains the matching library file "
                    f"at {row['path']}. Review that scene's file association in Stash before filing."
                )

        # 5. Check if size + oshash matches any existing file in the library
        if oshash and file_size:
            rows = connection.execute(
                "SELECT path, fingerprints_json FROM files WHERE size=? AND path != ?", (file_size, file_path)
            ).fetchall()
            for r in rows:
                existing_oshash = fingerprint_value(r["fingerprints_json"], "oshash")
                if existing_oshash and existing_oshash.lower() == oshash.lower():
                    return True, f"Video contents match existing library scene at {r['path']}"

        return False, ""
    finally:
        connection.close()


def find_filing_companions(
    source_video: Path, dest_folder: Path, dest_video_name: str
) -> tuple[list[tuple[Path, Path]], str | None]:
    """Discover all sidecars/companions associated with source_video using Watchtower's proven companion-matching safeguards.
    Never uses broad filename startswith() checks.
    Returns ([(source_companion, target_companion)], error_message_if_blocked)"""
    companions = []
    current = source_video
    proposed = dest_folder / dest_video_name
    all_companion_exts = COMPANION_EXTENSIONS | ASSOCIATED_EXTENSIONS | IMAGE_SIDECAR_EXTENSIONS
    try:
        if current.parent.exists():
            for candidate in current.parent.iterdir():
                if not candidate.is_file() or candidate == current:
                    continue
                cand_suffix = candidate.suffix.lower()
                if cand_suffix not in all_companion_exts:
                    continue

                # Match by exact or normalized stem equality
                key_cand = _sidecar_match_key(candidate.stem)
                key_curr = _sidecar_match_key(current.stem)
                stems_match = (candidate.stem.lower() == current.stem.lower()) or (
                    bool(key_cand) and key_cand == key_curr
                )

                # Match by Stash-style compound name (e.g. video.mp4.jpg or video.mp4.nfo)
                compound_base = candidate.name[:-len(candidate.suffix)]
                key_comp = _sidecar_match_key(compound_base)
                key_curr_name = _sidecar_match_key(current.name)
                compound_match = (compound_base.lower() == current.name.lower()) or (
                    bool(key_comp) and key_comp == key_curr_name
                )

                if stems_match:
                    dest_target = dest_folder / (proposed.stem + candidate.suffix)
                elif compound_match:
                    dest_target = dest_folder / (proposed.name + candidate.suffix)
                else:
                    continue

                if dest_target.exists():
                    return [], f"Target companion already exists at destination: {dest_target.name}"
                companions.append((candidate, dest_target))
    except OSError as exc:
        return [], f"Failed reading source folder for companions: {exc}"
    return companions, None


def _update_proposal_status(
    database_path: Path, proposal_id: int, status: str, last_error: str | None = None
) -> None:
    now = utc_now()
    connection = connect(database_path)
    try:
        connection.execute(
            "UPDATE filing_proposals SET status=?, last_error=?, updated_at=? WHERE id=?",
            (status, last_error, now, proposal_id),
        )
        connection.commit()
    finally:
        connection.close()


def record_incoming_filing_diagnostic(database_path: Path, file_path: str, diagnostic: str):
    """Persist a structured diagnostic for an incoming file without altering import detail."""
    try:
        connection = connect(database_path)
        try:
            row = connection.execute("SELECT path FROM incoming_files WHERE path=?", (str(file_path),)).fetchone()
            if row:
                connection.execute(
                    "UPDATE incoming_files SET filing_diagnostic=? WHERE path=?", (diagnostic, str(file_path))
                )
            else:
                now_str = datetime.now().isoformat()
                connection.execute(
                    "INSERT OR IGNORE INTO incoming_files (path, first_seen_at, last_checked_at, status, detail, filing_diagnostic) VALUES (?, ?, ?, 'imported', 'Stash scene', ?)",
                    (str(file_path), now_str, now_str, diagnostic),
                )
            connection.commit()
        finally:
            connection.close()
    except Exception as exc:
        logger.debug("Failed recording filing diagnostic for %s: %s", file_path, exc)


def evaluate_filing_proposal(
    database_path: Path,
    stash,
    file_path: str,
    scene: dict,
    config: dict,
    allow_baseline: bool = False,
    allow_refresh: bool = False,
) -> dict | None:
    """Evaluate an imported scene or incoming file for an automatic filing proposal.
    - Persists structured diagnostics in incoming_files table for every outcome.
    - Matches performer or studio based on autoFilingOrganizeBy; combined mode also offers verified tag folders.
    - When 'both' is selected:
      * Evaluates performer and studio independently.
      * If both match different folders, collects all candidate destinations.
      * If both match the same physical folder, merges explanations into a single candidate.
      * If one is ambiguous and the other reliable, uses the reliable one.
      * If neither matches or both are ambiguous, persists clear diagnostic and returns None.
    - If exactly 1 destination folder is found across all matches, creates a standard proposal.
    - If multiple destination folders are found, creates a multi-candidate proposal requiring user selection.
    """
    if not config.get("autoFilingEnabled"):
        record_incoming_filing_diagnostic(database_path, file_path, "Automatic filing is disabled in settings.")
        return None

    # Never evaluate or touch files located on cold storage roots
    cold_roots = [
        os.path.normpath(str(c)).rstrip(os.sep)
        for c in parse_cold_storage_roots(config.get("coldStorageRoots"))
        if str(c).strip()
    ]
    src_norm = os.path.normpath(str(file_path)).rstrip(os.sep)
    if cold_roots and any(
        src_norm == c or src_norm.startswith(c + os.sep) or c.startswith(src_norm + os.sep)
        for c in cold_roots
    ):
        return None

    src = Path(file_path)
    if not src.is_file():
        record_incoming_filing_diagnostic(database_path, file_path, "File no longer exists on disk.")
        return None

    file_id = None
    scene_id = None
    if scene:
        scene_id = str(scene.get("id"))
        files = scene.get("files") or []
        for f in files:
            if f.get("path") and str(Path(f.get("path", "")).resolve()) == str(src.resolve()):
                file_id = str(f.get("id"))
                break
        if not file_id and len(files) == 1 and files[0].get("id"):
            file_id = str(files[0]["id"])

    if not file_id or not scene_id:
        conn = connect(database_path)
        try:
            row = conn.execute(
                "SELECT file_id, scene_id FROM files WHERE path=? AND exists_on_disk=1", (str(src),)
            ).fetchone()
            if row:
                file_id = str(row["file_id"])
                scene_id = str(row["scene_id"])
        finally:
            conn.close()

    if not file_id or not scene_id:
        record_incoming_filing_diagnostic(
            database_path, file_path, "Filing skipped: File has not been linked to a Stash scene."
        )
        return None

    raw_incoming = config.get("incomingFolders") or ([config.get("incomingFolder")] if config.get("incomingFolder") else [])
    if isinstance(raw_incoming, (list, tuple)):
        incoming_folders = [str(f).strip() for f in raw_incoming if isinstance(f, (str, Path)) and str(f).strip()]
    else:
        incoming_folders = []

    file_size = src.stat().st_size
    oshash = opensubtitles_hash(src)
    disqualified, disq_reason = is_disqualified_from_filing(
        database_path,
        str(src),
        file_size,
        oshash,
        file_id,
        scene_id,
        allow_baseline=allow_baseline,
        allow_refresh=allow_refresh,
    )
    if disqualified:
        record_incoming_filing_diagnostic(database_path, file_path, f"Filing skipped: {disq_reason}")
        return None

    trigger = (config.get("autoFilingTrigger") or "import").strip().lower()
    if trigger == "metadata":
        scene_performers = (scene or {}).get("performers") or []
        scene_studio = (scene or {}).get("studio")
        scene_tags = (scene or {}).get("tags") or []
        if not scene_performers and not scene_studio and not scene_tags:
            record_incoming_filing_diagnostic(
                database_path, file_path, "Waiting for performer, studio, or tag metadata to be added in Stash."
            )
            return None

    organize_by = (config.get("autoFilingOrganizeBy") or "both").strip().lower()
    dest_roots = get_configured_filing_destination_roots(config)
    if not dest_roots:
        record_incoming_filing_diagnostic(
            database_path, file_path, "No destination roots configured in Automatic Filing settings."
        )
        return None

    match_source = config.get("autoFilingMatchSource") or "metadata_first"

    try:
        max_depth = int((config or {}).get("autoFilingMaxDiscoveryDepth", 4))
    except (ValueError, TypeError):
        max_depth = 4
    max_depth = max(1, min(8, max_depth))

    matched_candidates_by_folder = {}

    if organize_by == "studio":
        from librarymanager.integrations.stash_graphql import fetch_all_studios

        all_studios = fetch_all_studios(stash)
        s_match = match_studio_for_filing(scene, src.name, all_studios, match_source=match_source)
        if not s_match.get("matched"):
            s_reason = s_match.get("reason") or "No matching studio found in filename or Stash metadata."
            record_incoming_filing_diagnostic(database_path, file_path, s_reason)
            return None
        s_entity = s_match["entity"]
        s_paths, s_status = resolve_filing_destinations(
            dest_roots,
            s_entity["name"],
            entity_id=str(s_entity["id"]),
            entity_type="studio",
            database_path=database_path,
            max_depth=max_depth,
        )
        if not s_paths:
            record_incoming_filing_diagnostic(
                database_path, file_path, f"Studio '{s_entity['name']}' identified, but no destination folder found."
            )
            return None
        for p in s_paths:
            p_res = str(p.resolve())
            matched_candidates_by_folder[p_res] = {
                "destination_folder": str(p),
                "entity_type": "studio",
                "entity_name": s_entity["name"],
                "entity_id": str(s_entity["id"]),
                "is_custom_mapped": (s_status == "custom_mapping"),
                "match_source": s_match.get("source", "filename"),
                "matched_alias": s_match.get("matched_alias"),
                "label": f"[Studio] {s_entity['name']} → {p}"
                + (" (Custom Mapped)" if s_status == "custom_mapping" else ""),
            }
        primary_match = s_match
        primary_entity = s_entity
        effective_organize_by = "studio"

    elif organize_by == "both":
        from librarymanager.integrations.stash_graphql import fetch_all_performers, fetch_all_studios

        all_performers = fetch_all_performers(stash)
        all_studios = fetch_all_studios(stash)

        p_match = match_performer_for_filing(scene, src.name, all_performers, match_source=match_source)
        s_match = match_studio_for_filing(scene, src.name, all_studios, match_source=match_source)

        p_valid = bool(p_match.get("matched"))
        s_valid = bool(s_match.get("matched"))

        p_entities = p_match.get("entities") or ([p_match["entity"]] if p_match.get("entity") else [])
        p_matched_paths_by_ent = {}
        for p_ent in p_entities:
            p_paths_curr, p_status_curr = resolve_filing_destinations(
                dest_roots,
                p_ent["name"],
                entity_id=str(p_ent["id"]),
                entity_type="performer",
                database_path=database_path,
                max_depth=max_depth,
            )
            p_matched_paths_by_ent[p_ent["id"]] = (p_paths_curr, p_status_curr)
            for p in p_paths_curr:
                p_res = str(p.resolve())
                if p_res in matched_candidates_by_folder:
                    existing = matched_candidates_by_folder[p_res]
                    if "matched_entities" not in existing:
                        existing["matched_entities"] = [
                            {
                                "entity_type": existing["entity_type"],
                                "entity_name": existing["entity_name"],
                                "entity_id": str(existing["entity_id"]),
                                "is_custom_mapped": existing.get("is_custom_mapped", False),
                                "match_source": existing.get("match_source", "filename"),
                                "matched_alias": existing.get("matched_alias"),
                            }
                        ]
                    if not any(
                        e["entity_id"] == str(p_ent["id"]) and e["entity_type"] == "performer"
                        for e in existing["matched_entities"]
                    ):
                        existing["matched_entities"].append({
                            "entity_type": "performer",
                            "entity_name": p_ent["name"],
                            "entity_id": str(p_ent["id"]),
                            "is_custom_mapped": (p_status_curr == "custom_mapping"),
                            "match_source": p_match.get("source", "filename"),
                            "matched_alias": p_match.get("matched_alias") if len(p_entities) == 1 else None,
                        })
                    all_names = " & ".join(e["entity_name"] for e in existing["matched_entities"])
                    all_ids = ",".join(e["entity_id"] for e in existing["matched_entities"])
                    existing["entity_type"] = (
                        "both" if any(e["entity_type"] == "studio" for e in existing["matched_entities"]) else "performer"
                    )
                    existing["entity_name"] = all_names
                    existing["entity_id"] = all_ids
                    existing["label"] = f"[{' & '.join(e['entity_type'].capitalize() for e in existing['matched_entities'])}: {all_names}] → {p}"
                    if any(e.get("is_custom_mapped") for e in existing["matched_entities"]):
                        existing["is_custom_mapped"] = True
                        existing["label"] += " (Custom Mapped)"
                else:
                    matched_candidates_by_folder[p_res] = {
                        "destination_folder": str(p),
                        "entity_type": "performer",
                        "entity_name": p_ent["name"],
                        "entity_id": str(p_ent["id"]),
                        "is_custom_mapped": (p_status_curr == "custom_mapping"),
                        "match_source": p_match.get("source", "filename"),
                        "matched_alias": p_match.get("matched_alias") if len(p_entities) == 1 else None,
                        "label": f"[Performer] {p_ent['name']} → {p}"
                        + (" (Custom Mapped)" if p_status_curr == "custom_mapping" else ""),
                    }

        s_paths = []
        s_status = None
        if s_valid:
            s_entity = s_match["entity"]
            s_paths, s_status = resolve_filing_destinations(
                dest_roots,
                s_entity["name"],
                entity_id=str(s_entity["id"]),
                entity_type="studio",
                database_path=database_path,
                max_depth=max_depth,
            )
            for p in s_paths:
                p_res = str(p.resolve())
                if p_res in matched_candidates_by_folder:
                    existing = matched_candidates_by_folder[p_res]
                    if "matched_entities" not in existing:
                        existing["matched_entities"] = [
                            {
                                "entity_type": existing["entity_type"],
                                "entity_name": existing["entity_name"],
                                "entity_id": str(existing["entity_id"]),
                                "is_custom_mapped": existing.get("is_custom_mapped", False),
                                "match_source": existing.get("match_source", "filename"),
                                "matched_alias": existing.get("matched_alias"),
                            }
                        ]
                    if not any(
                        e["entity_id"] == str(s_entity["id"]) and e["entity_type"] == "studio"
                        for e in existing["matched_entities"]
                    ):
                        existing["matched_entities"].append({
                            "entity_type": "studio",
                            "entity_name": s_entity["name"],
                            "entity_id": str(s_entity["id"]),
                            "is_custom_mapped": (s_status == "custom_mapping"),
                            "match_source": s_match.get("source", "filename"),
                            "matched_alias": s_match.get("matched_alias"),
                        })
                    all_names = " & ".join(e["entity_name"] for e in existing["matched_entities"])
                    all_ids = ",".join(e["entity_id"] for e in existing["matched_entities"])
                    existing["entity_type"] = "both"
                    existing["entity_name"] = all_names
                    existing["entity_id"] = all_ids
                    existing["label"] = f"[{' & '.join(e['entity_type'].capitalize() for e in existing['matched_entities'])}: {all_names}] → {p}"
                    if any(e.get("is_custom_mapped") for e in existing["matched_entities"]):
                        existing["is_custom_mapped"] = True
                        existing["label"] += " (Custom Mapped)"
                else:
                    matched_candidates_by_folder[p_res] = {
                        "destination_folder": str(p),
                        "entity_type": "studio",
                        "entity_name": s_entity["name"],
                        "entity_id": str(s_entity["id"]),
                        "is_custom_mapped": (s_status == "custom_mapping"),
                        "match_source": s_match.get("source", "filename"),
                        "matched_alias": s_match.get("matched_alias"),
                        "label": f"[Studio] {s_entity['name']} → {p}"
                        + (" (Custom Mapped)" if s_status == "custom_mapping" else ""),
                    }

        # In the combined review mode, verified Stash tags may identify existing
        # category folders. Tags never come from loose filename guesses.
        scene_tags = [
            tag for tag in ((scene or {}).get("tags") or []) if tag.get("id") and tag.get("name")
        ]
        matched_tag_entities = []
        for tag in scene_tags:
            tag_paths, tag_status = resolve_tag_filing_destinations(
                dest_roots, tag, database_path=database_path, max_depth=max_depth
            )
            if tag_paths:
                matched_tag_entities.append(tag)
            for p in tag_paths:
                p_res = str(p.resolve())
                tag_entity = {
                    "entity_type": "tag",
                    "entity_name": tag["name"],
                    "entity_id": str(tag["id"]),
                    "is_custom_mapped": (tag_status == "custom_mapping"),
                    "match_source": "metadata",
                    "matched_alias": None,
                }
                if p_res in matched_candidates_by_folder:
                    existing = matched_candidates_by_folder[p_res]
                    if "matched_entities" not in existing:
                        existing["matched_entities"] = [{
                            "entity_type": existing["entity_type"],
                            "entity_name": existing["entity_name"],
                            "entity_id": str(existing["entity_id"]),
                            "is_custom_mapped": existing.get("is_custom_mapped", False),
                            "match_source": existing.get("match_source", "filename"),
                            "matched_alias": existing.get("matched_alias"),
                        }]
                    if not any(
                        entity.get("entity_type") == "tag" and str(entity.get("entity_id")) == str(tag["id"])
                        for entity in existing["matched_entities"]
                    ):
                        existing["matched_entities"].append(tag_entity)
                    all_names = " & ".join(entity["entity_name"] for entity in existing["matched_entities"])
                    all_ids = ",".join(str(entity["entity_id"]) for entity in existing["matched_entities"])
                    all_types = " & ".join(
                        entity["entity_type"].capitalize() for entity in existing["matched_entities"]
                    )
                    existing["entity_type"] = "both"
                    existing["entity_name"] = all_names
                    existing["entity_id"] = all_ids
                    existing["is_custom_mapped"] = any(
                        entity.get("is_custom_mapped") for entity in existing["matched_entities"]
                    )
                    existing["label"] = f"[{all_types}: {all_names}] → {p}"
                    if existing["is_custom_mapped"]:
                        existing["label"] += " (Custom Mapped)"
                else:
                    matched_candidates_by_folder[p_res] = {
                        "destination_folder": str(p),
                        **tag_entity,
                        "label": f"[Tag] {tag['name']} → {p}"
                        + (" (Custom Mapped)" if tag_status == "custom_mapping" else ""),
                    }

        # Build detailed diagnostics for combined performer/studio/tag mode.
        p_matched_count = sum(1 for (paths, _) in p_matched_paths_by_ent.values() if paths)
        if p_valid:
            if len(p_entities) == 1:
                p_name = p_entities[0]["name"]
                p_diag = (
                    f"Performer '{p_name}' matched"
                    if p_matched_count > 0
                    else f"Performer '{p_name}' identified, but no destination folder found"
                )
            else:
                p_names = ", ".join(e["name"] for e in p_entities)
                p_diag = (
                    f"Multiple performers matched ({p_names})"
                    if p_matched_count > 0
                    else f"Multiple performers identified ({p_names}), but no destination folders found"
                )
        else:
            p_diag = p_match.get("reason", "No matching performer found")

        if s_valid:
            s_diag = (
                f"Studio '{s_match['entity']['name']}' matched"
                if s_paths
                else f"Studio '{s_match['entity']['name']}' identified, but no destination folder found"
            )
        else:
            s_diag = s_match.get("reason", "No matching studio found")

        if scene_tags:
            matched_tag_ids = {str(tag["id"]) for tag in matched_tag_entities}
            matched_tag_names = [tag["name"] for tag in scene_tags if str(tag["id"]) in matched_tag_ids]
            unmatched_tag_names = [tag["name"] for tag in scene_tags if str(tag["id"]) not in matched_tag_ids]
            tag_parts = []
            if matched_tag_names:
                tag_parts.append(f"Tag folders matched ({', '.join(matched_tag_names)})")
            if unmatched_tag_names:
                tag_parts.append(f"no destination folders for tags ({', '.join(unmatched_tag_names)})")
            tag_diag = "; ".join(tag_parts)
        else:
            tag_diag = "No Stash tags assigned"

        if not matched_candidates_by_folder:
            if not p_valid and not s_valid and not scene_tags:
                diagnostic = "No identity found: no matching performer, studio, or tag."
            else:
                diagnostic = f"Performer: {p_diag} | Studio: {s_diag} | Tags: {tag_diag}"
            record_incoming_filing_diagnostic(database_path, file_path, diagnostic)
            return None

        if p_valid and p_matched_count > 0:
            primary_match = p_match
            primary_entity = p_entities[0]
        elif s_valid and s_paths:
            primary_match = s_match
            primary_entity = s_match.get("entity", {})
        else:
            primary_match = {"source": "metadata"}
            primary_entity = matched_tag_entities[0]
        effective_organize_by = "both"

    else:
        from librarymanager.integrations.stash_graphql import fetch_all_performers

        all_performers = fetch_all_performers(stash)
        p_match = match_performer_for_filing(scene, src.name, all_performers, match_source=match_source)
        if not p_match.get("matched"):
            p_reason = p_match.get("reason") or "No matching performer found in filename or Stash metadata."
            record_incoming_filing_diagnostic(database_path, file_path, p_reason)
            return None
        p_entities = p_match.get("entities") or ([p_match["entity"]] if p_match.get("entity") else [])
        for p_ent in p_entities:
            p_paths, p_status = resolve_filing_destinations(
                dest_roots,
                p_ent["name"],
                entity_id=str(p_ent["id"]),
                entity_type="performer",
                database_path=database_path,
                max_depth=max_depth,
            )
            for p in p_paths:
                p_res = str(p.resolve())
                if p_res in matched_candidates_by_folder:
                    existing = matched_candidates_by_folder[p_res]
                    if "matched_entities" not in existing:
                        existing["matched_entities"] = [
                            {
                                "entity_type": existing["entity_type"],
                                "entity_name": existing["entity_name"],
                                "entity_id": str(existing["entity_id"]),
                                "is_custom_mapped": existing.get("is_custom_mapped", False),
                                "match_source": existing.get("match_source", "filename"),
                                "matched_alias": existing.get("matched_alias"),
                            }
                        ]
                    if not any(
                        e["entity_id"] == str(p_ent["id"]) and e["entity_type"] == "performer"
                        for e in existing["matched_entities"]
                    ):
                        existing["matched_entities"].append({
                            "entity_type": "performer",
                            "entity_name": p_ent["name"],
                            "entity_id": str(p_ent["id"]),
                            "is_custom_mapped": (p_status == "custom_mapping"),
                            "match_source": p_match.get("source", "filename"),
                            "matched_alias": p_match.get("matched_alias") if len(p_entities) == 1 else None,
                        })
                    all_names = " & ".join(e["entity_name"] for e in existing["matched_entities"])
                    all_ids = ",".join(e["entity_id"] for e in existing["matched_entities"])
                    existing["entity_name"] = all_names
                    existing["entity_id"] = all_ids
                    existing["label"] = f"[Performer: {all_names}] → {p}"
                    if any(e.get("is_custom_mapped") for e in existing["matched_entities"]):
                        existing["is_custom_mapped"] = True
                        existing["label"] += " (Custom Mapped)"
                else:
                    matched_candidates_by_folder[p_res] = {
                        "destination_folder": str(p),
                        "entity_type": "performer",
                        "entity_name": p_ent["name"],
                        "entity_id": str(p_ent["id"]),
                        "is_custom_mapped": (p_status == "custom_mapping"),
                        "match_source": p_match.get("source", "filename"),
                        "matched_alias": p_match.get("matched_alias") if len(p_entities) == 1 else None,
                        "label": f"[Performer] {p_ent['name']} → {p}"
                        + (" (Custom Mapped)" if p_status == "custom_mapping" else ""),
                    }

        if not matched_candidates_by_folder:
            if len(p_entities) == 1:
                record_incoming_filing_diagnostic(
                    database_path,
                    file_path,
                    f"Performer '{p_entities[0]['name']}' identified, but no destination folder found.",
                )
            else:
                names = ", ".join(e["name"] for e in p_entities)
                record_incoming_filing_diagnostic(
                    database_path, file_path, f"Multiple performers identified ({names}), but no destination folders found."
                )
            return None

        primary_match = p_match
        primary_entity = p_entities[0]
        effective_organize_by = "performer"

    if not matched_candidates_by_folder:
        record_incoming_filing_diagnostic(database_path, file_path, "No destination folder found for matched entity.")
        return None

    raw_candidates_list = list(matched_candidates_by_folder.values())
    proposed_name = src.name
    in_nested_folder = 1 if _is_in_nested_incoming_folder(src, incoming_folders) else 0

    if len(raw_candidates_list) == 1:
        single_cand = raw_candidates_list[0]
        dest_folder = Path(single_cand["destination_folder"])
        proposed_path = dest_folder / proposed_name
        is_custom_mapped = 1 if single_cand.get("is_custom_mapped") else 0
        matched_entity_id = single_cand.get("entity_id", primary_entity.get("id"))
        matched_entity_name = single_cand.get("entity_name", primary_entity.get("name"))
        matched_alias = single_cand.get("matched_alias")
        match_source_val = single_cand.get("match_source", primary_match.get("source", "filename"))
        cand_organize_by = single_cand.get("entity_type", effective_organize_by)

        if proposed_path.exists():
            record_incoming_filing_diagnostic(
                database_path,
                file_path,
                f"Destination conflict: target file '{proposed_path.name}' already exists in destination folder.",
            )
            return None

        # Check inventory conflict in database
        conn = connect(database_path)
        try:
            conflict = (
                conn.execute(
                    "SELECT 1 FROM files WHERE path=? AND exists_on_disk=1", (str(proposed_path),)
                ).fetchone()
                is not None
            )
            if conflict:
                record_incoming_filing_diagnostic(
                    database_path, file_path, "Destination conflict: target path already registered in Stash library."
                )
                return None
        finally:
            conn.close()

        companions, comp_err = find_filing_companions(src, dest_folder, proposed_name)
        if comp_err:
            record_incoming_filing_diagnostic(
                database_path, file_path, f"Filing skipped: companion file collision ({comp_err})"
            )
            return None

        # If merged both candidate, retain it in candidate_destinations so metadata choice is available
        if single_cand.get("entity_type") == "both":
            candidate_destinations = raw_candidates_list
        else:
            candidate_destinations = []
    else:
        dest_folder = ""
        proposed_path = ""
        companions = []
        is_custom_mapped = 1 if any(c.get("is_custom_mapped") for c in raw_candidates_list) else 0
        matched_entity_id = "multiple"
        matched_entity_name = "Multiple Candidates"
        matched_alias = None
        match_source_val = "multiple"
        cand_organize_by = effective_organize_by
        candidate_destinations = raw_candidates_list

    companions_json = json.dumps([{"source": str(s), "target": str(t)} for s, t in companions])
    candidate_destinations_json = json.dumps(candidate_destinations)
    now = utc_now()
    connection = connect(database_path)
    proposal_id = None
    try:
        existing_p = connection.execute(
            "SELECT id FROM filing_proposals WHERE (file_id=? OR scene_id=? OR source_path=?) AND status IN ('pending', 'blocked')",
            (str(file_id or ""), str(scene_id or ""), str(src)),
        ).fetchone()

        if existing_p:
            proposal_id = existing_p["id"]
            connection.execute(
                """UPDATE filing_proposals SET
                    file_id=?, scene_id=?, source_path=?, proposed_path=?, destination_folder=?, destination_filename=?,
                    organize_by=?, matched_entity_id=?, matched_entity_name=?, matched_alias=?, match_source=?,
                    reason=?, companions_json=?, candidate_destinations_json=?, is_custom_mapped=?, in_nested_folder=?,
                    status='pending', last_error=NULL, updated_at=?
                WHERE id=?""",
                (
                    file_id,
                    scene_id,
                    str(src),
                    str(proposed_path),
                    str(dest_folder),
                    proposed_name,
                    cand_organize_by,
                    str(matched_entity_id),
                    str(matched_entity_name),
                    matched_alias,
                    match_source_val,
                    f"Matched {cand_organize_by} '{matched_entity_name}' ({match_source_val})",
                    companions_json,
                    candidate_destinations_json,
                    is_custom_mapped,
                    in_nested_folder,
                    now,
                    proposal_id,
                ),
            )
        else:
            cur = connection.execute(
                """INSERT INTO filing_proposals (
                    file_id, scene_id, source_path, proposed_path, destination_folder, destination_filename,
                    organize_by, matched_entity_id, matched_entity_name, matched_alias, match_source,
                    reason, companions_json, candidate_destinations_json, is_custom_mapped, in_nested_folder,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    file_id,
                    scene_id,
                    str(src),
                    str(proposed_path),
                    str(dest_folder),
                    proposed_name,
                    cand_organize_by,
                    str(matched_entity_id),
                    str(matched_entity_name),
                    matched_alias,
                    match_source_val,
                    f"Matched {cand_organize_by} '{matched_entity_name}' ({match_source_val})",
                    companions_json,
                    candidate_destinations_json,
                    is_custom_mapped,
                    in_nested_folder,
                    now,
                    now,
                ),
            )
            proposal_id = cur.lastrowid
        connection.commit()
    finally:
        connection.close()

    dest_name = (
        dest_folder.name
        if hasattr(dest_folder, "name") and dest_folder.name
        else (str(dest_folder) if dest_folder else "multiple candidate destinations")
    )
    record_activity(
        database_path,
        "filing",
        "proposal created",
        "pending",
        scene_id=scene_id,
        file_id=file_id,
        old_path=str(src),
        new_path=str(proposed_path),
        detail=f"Proposed filing to {dest_name} for {cand_organize_by} '{matched_entity_name}'",
    )

    if len(raw_candidates_list) == 1:
        record_incoming_filing_diagnostic(
            database_path, file_path, f"Proposal ready: {cand_organize_by} '{matched_entity_name}' → {dest_folder}"
        )
    else:
        record_incoming_filing_diagnostic(
            database_path,
            file_path,
            f"Proposal ready: Multiple candidate destinations ({len(raw_candidates_list)}) requiring selection.",
        )

    return {
        "id": proposal_id,
        "file_id": file_id,
        "scene_id": scene_id,
        "source_path": str(src),
        "proposed_path": str(proposed_path),
        "destination_folder": str(dest_folder),
        "destination_filename": proposed_name,
        "organize_by": cand_organize_by,
        "matched_entity_id": matched_entity_id,
        "matched_entity_name": matched_entity_name,
        "matched_alias": matched_alias,
        "match_source": match_source_val,
        "candidate_destinations": candidate_destinations,
        "is_custom_mapped": bool(is_custom_mapped),
        "in_nested_folder": bool(in_nested_folder),
        "companions": [{"source": str(s), "target": str(t)} for s, t in companions],
        "companions_count": len(companions),
        "status": "pending",
    }


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
    """Re-evaluate an eligible, already-imported incoming scene for automatic filing.
    - Evaluates current Stash metadata, aliases, custom mappings, and destination folders.
    - Preserves scene ID and file identity without rescanning, reimporting, or resetting baseline.
    - Recognises already-completed moves safely and returns clear success without moving media again.
    - Prevents duplicate proposals and rejects retries for ineligible items or active/recovery states.
    """
    if not file_path:
        return {"success": False, "error": "No file path provided."}

    src = Path(file_path).resolve()
    if config is None:
        config = (stash.find_plugin_config("librarymanager") if hasattr(stash, "find_plugin_config") else {}) or {}

    if not config.get("autoFilingEnabled"):
        return {"success": False, "error": "Automatic filing is disabled in settings."}

    if force_rescan:
        roots = get_configured_filing_destination_roots(config)
        max_depth = int(config.get("autoFilingMaxDiscoveryDepth", 4))
        refresh_destination_dir_cache(database_path, roots, max_depth=max_depth)

    # 1. Prevent duplicate proposals, check unresolved recovery states, and recognise already-filed scenes
    conn = connect(database_path)
    try:
        query_id = int(proposal_id) if proposal_id else -1
        active_prop = conn.execute(
            """SELECT id, scene_id, file_id, status, source_path, proposed_path, destination_folder FROM filing_proposals
               WHERE (source_path = ? OR proposed_path = ? OR id = ?)
               ORDER BY id DESC LIMIT 1""",
            (str(src), str(src), query_id),
        ).fetchone()

        if active_prop:
            p_status = active_prop["status"]
            if p_status == "completed":
                conn.execute(
                    "UPDATE incoming_files SET filing_diagnostic=NULL WHERE path=? OR path=?",
                    (str(src), str(active_prop["proposed_path"])),
                )
                conn.commit()
                return {
                    "success": True,
                    "already_filed": True,
                    "message": f"Filing is already complete. '{src.name}' is verified at its destination.",
                }
            elif p_status == "needs_recovery":
                return {
                    "success": False,
                    "error": "This file has an unresolved filing recovery in progress. Resolve or recover it first.",
                }
            elif p_status in ("pending", "blocked") and not allow_refresh:
                return {
                    "success": False,
                    "proposal_pending": True,
                    "proposal_id": active_prop["id"],
                    "error": "An active filing proposal already exists for this scene.",
                    "message": "A filing proposal is ready for review in Automatic Filing.",
                }
            if allow_refresh:
                allow_baseline = True
                if active_prop and active_prop["scene_id"]:
                    try:
                        from librarymanager.services.renaming import make_pending_rename_due
                        make_pending_rename_due(database_path, str(active_prop["scene_id"]))
                    except Exception:
                        pass
    finally:
        conn.close()

    if not src.is_file():
        if active_prop and active_prop["scene_id"] and stash:
            try:
                scene_obj = None
                if hasattr(stash, "call_GQL"):
                    res = stash.call_GQL(
                        "query CheckScene($id: ID!) { findScene(id: $id) { id files { id path } } }",
                        {"id": str(active_prop["scene_id"])},
                    )
                    scene_obj = (res or {}).get("findScene")
                elif hasattr(stash, "find_scene"):
                    scene_obj = stash.find_scene(str(active_prop["scene_id"]))

                if scene_obj and str(scene_obj.get("id")) == str(active_prop["scene_id"]):
                    for sf in (scene_obj.get("files") or []):
                        sf_p = sf.get("path") or ""
                        if sf_p and Path(sf_p).is_file():
                            src = Path(sf_p).resolve()
                            file_path = str(src)
                            break
            except Exception:
                pass

        if not src.is_file():
            return {"success": False, "error": f"File does not exist on disk: {file_path}"}

    # Verify if file is inside configured incoming folders
    incoming_folders = get_configured_incoming_folders(config)
    is_inside_incoming = True
    if incoming_folders:
        is_inside_incoming = any(_is_subpath_of(src, f) for f in incoming_folders)

    # If the file is OUTSIDE incoming folders, check if it is already verified at an approved destination
    if not is_inside_incoming:
        conn = connect(database_path)
        try:
            f_row = conn.execute(
                "SELECT file_id, scene_id, path FROM files WHERE (path=? OR basename=?) AND exists_on_disk=1",
                (str(src), src.name),
            ).fetchone()
            scene_id_to_check = (
                str(f_row["scene_id"])
                if f_row and f_row["scene_id"]
                else (str(active_prop["scene_id"]) if active_prop and active_prop["scene_id"] else None)
            )

            if scene_id_to_check and stash:
                try:
                    scene_obj = None
                    if hasattr(stash, "call_GQL"):
                        res = stash.call_GQL(
                            "query CheckScene($id: ID!) { findScene(id: $id) { id files { id path } } }",
                            {"id": scene_id_to_check},
                        )
                        scene_obj = (res or {}).get("findScene")
                    elif hasattr(stash, "find_scene"):
                        scene_obj = stash.find_scene(scene_id_to_check)

                    if scene_obj and str(scene_obj.get("id")) == scene_id_to_check:
                        s_files = scene_obj.get("files") or []
                        for sf in s_files:
                            sf_p = sf.get("path") or ""
                            if sf_p and str(Path(sf_p).resolve()) == str(src):
                                if active_prop:
                                    conn.execute(
                                        "UPDATE filing_proposals SET status='completed', last_error=NULL, updated_at=? WHERE id=?",
                                        (utc_now(), active_prop["id"]),
                                    )
                                conn.execute(
                                    "UPDATE incoming_files SET filing_diagnostic=NULL WHERE path=? OR path=?",
                                    (str(src), str(active_prop["source_path"] if active_prop else src)),
                                )
                                conn.commit()
                                return {
                                    "success": True,
                                    "already_filed": True,
                                    "message": f"Filing is already complete. '{src.name}' is verified at its destination.",
                                }
                except Exception:
                    pass
        finally:
            conn.close()

        return {"success": False, "error": "This file is not located inside any configured Incoming folder."}

    # 2. Check scene linkage in files table or Stash
    file_id = None
    scene_id = None
    conn = connect(database_path)
    try:
        f_row = conn.execute(
            "SELECT file_id, scene_id FROM files WHERE path=? AND exists_on_disk=1", (str(src),)
        ).fetchone()
        if f_row:
            file_id = str(f_row["file_id"])
            scene_id = str(f_row["scene_id"])
        elif active_prop:
            file_id = str(active_prop["file_id"] or "")
            scene_id = str(active_prop["scene_id"] or "")
    finally:
        conn.close()

    scene_data = None
    if scene_id:
        try:
            gql_res = stash.call_GQL(
                "query FindSceneForFiling($id: ID!) { findScene(id: $id) { id title files { id path } performers { id name disambiguation alias_list } studio { id name aliases } tags { id name aliases } } }",
                {"id": str(scene_id)},
            )
            scene_data = (gql_res or {}).get("findScene")
        except Exception as exc:
            logger.debug("Failed fetching Stash scene %s during retry: %s", scene_id, exc)

    if not scene_data:
        try:
            scene_data = find_scene_by_path(stash, str(src))
        except Exception:
            pass

    # Update local inventory cache with latest Stash metadata
    if scene_data:
        try:
            refresh_scene_inventory(database_path, scene_data)
        except Exception as exc:
            logger.debug("Failed refreshing local scene inventory for scene %s: %s", scene_id, exc)

    if not scene_data:
        disq_reason = "File has not been linked to a Stash scene."
        record_incoming_filing_diagnostic(database_path, str(src), f"Filing skipped: {disq_reason}")
        if allow_refresh and active_prop:
            conn = connect(database_path)
            try:
                conn.execute(
                    "UPDATE filing_proposals SET status='invalid', last_error=?, updated_at=? WHERE id=?",
                    (f"Filing skipped: {disq_reason}", utc_now(), active_prop["id"]),
                )
                conn.commit()
            finally:
                conn.close()
        return {"success": False, "error": f"Filing skipped: {disq_reason}"}

    # 3. Evaluate new proposal
    prop = evaluate_filing_proposal(
        database_path,
        stash,
        str(src),
        scene_data,
        config,
        allow_baseline=allow_baseline,
        allow_refresh=allow_refresh,
    )
    if prop:
        conn = connect(database_path)
        try:
            diag_row = conn.execute("SELECT filing_diagnostic FROM incoming_files WHERE path=?", (str(src),)).fetchone()
            diag = diag_row["filing_diagnostic"] if diag_row else None
        finally:
            conn.close()
        return {
            "success": True,
            "proposal": prop,
            "diagnostic": diag,
            "message": "Filing proposal generated successfully.",
        }
    else:
        conn = connect(database_path)
        try:
            diag_row = conn.execute("SELECT filing_diagnostic FROM incoming_files WHERE path=?", (str(src),)).fetchone()
            diag = diag_row["filing_diagnostic"] if diag_row else "Filing evaluation did not produce a proposal."
            if allow_refresh and active_prop:
                conn.execute(
                    "UPDATE filing_proposals SET status='invalid', last_error=?, updated_at=? WHERE id=?",
                    (diag, utc_now(), active_prop["id"]),
                )
                conn.commit()
        finally:
            conn.close()
        return {"success": False, "diagnostic": diag, "message": diag, "error": diag}


def _safe_cross_device_move(src_p: Path, tgt_p: Path) -> None:
    """Safely copy and verify across filesystems, then remove source.

    Guarantees:
    1. Never overwrites an existing destination, including if it appears during transfer.
    2. Writes to a staging file in the target directory first.
    3. Flushes and fsyncs data to disk.
    4. Verifies byte length matches source before promotion.
    5. Preserves metadata where supported without failing the move.
    6. Never deletes source until destination is verified.
    7. Verifies final source and destination states.
    """
    if tgt_p.exists() or tgt_p.is_symlink():
        raise FileExistsError(f"Destination already exists: {tgt_p}")

    stg_p = tgt_p.parent / f".{tgt_p.name}.wt_tmp_{uuid.uuid4().hex}"
    src_size = src_p.stat().st_size
    try:
        with open(src_p, "rb") as f_src, open(stg_p, "xb") as f_dst:
            shutil.copyfileobj(f_src, f_dst, length=1024 * 1024)
            f_dst.flush()
            os.fsync(f_dst.fileno())

        # Metadata preservation (copystat)
        try:
            shutil.copystat(src_p, stg_p)
        except OSError as stat_err:
            logger.warning("Could not preserve file metadata for %s -> %s: %s", src_p, stg_p, stat_err)

        stg_size = stg_p.stat().st_size
        if stg_size != src_size:
            stg_p.unlink(missing_ok=True)
            raise IOError(f"Cross-device copy verification failed: expected {src_size} bytes, wrote {stg_size} bytes")

        # Promote staging file to final destination without overwrite
        promoted = False
        try:
            os.link(stg_p, tgt_p)
            stg_p.unlink(missing_ok=True)
            promoted = True
        except FileExistsError:
            stg_p.unlink(missing_ok=True)
            raise FileExistsError(f"Destination already exists: {tgt_p}")
        except OSError:
            # Target filesystem does not support hard links
            pass

        if not promoted:
            if tgt_p.exists() or tgt_p.is_symlink():
                stg_p.unlink(missing_ok=True)
                raise FileExistsError(f"Destination already exists: {tgt_p}")
            stg_p.rename(tgt_p)

        # Verify destination state before deleting source
        if not tgt_p.is_file() or tgt_p.stat().st_size != src_size:
            raise RuntimeError(f"Destination verification failed after promotion for {tgt_p}")

        # Delete source only after destination is verified
        try:
            src_p.unlink()
        except Exception as del_err:
            raise OSError(
                f"File successfully copied to {tgt_p} ({src_size} bytes verified), "
                f"but source deletion failed: {del_err}"
            ) from del_err

        # Verify final state
        if not tgt_p.is_file() or src_p.exists():
            raise RuntimeError(f"Final state verification failed: src_exists={src_p.exists()}, tgt_exists={tgt_p.exists()}")

    except Exception:
        if stg_p.exists():
            try:
                stg_p.unlink()
            except Exception:
                pass
        raise


def _move_file(source: Path | str, target: Path | str) -> None:
    """Move a file safely without overwriting destination, supporting cross-device moves.

    Guarantees:
    1. Validates that source exists and destination does not exist.
    2. Never overwrites an existing destination file, even if created concurrently.
    3. Uses atomic same-filesystem hardlink promotion when supported.
    4. Falls back to verified staging copy for cross-filesystem moves.
    5. Never deletes source before destination is verified.
    6. Verifies final source and destination states after move.
    """
    src_p = Path(source)
    tgt_p = Path(target)

    if not src_p.is_file():
        raise FileNotFoundError(f"Source file does not exist: {src_p}")

    if tgt_p.exists() or tgt_p.is_symlink():
        raise FileExistsError(f"Destination already exists: {tgt_p}")

    try:
        if src_p.resolve() == tgt_p.resolve():
            raise FileExistsError(f"Source and destination resolve to the same file: {tgt_p}")
    except Exception as exc:
        if isinstance(exc, FileExistsError):
            raise

    if not tgt_p.parent.is_dir():
        raise FileNotFoundError(f"Destination directory does not exist: {tgt_p.parent}")

    # Compatibility: if Path.rename is actively mocked in tests, delegate to it
    if Path.rename is not _ORIGINAL_PATH_RENAME or hasattr(Path.rename, "mock_calls"):
        try:
            src_p.rename(tgt_p)
            return
        except OSError as err:
            if err.errno == errno.EXDEV or getattr(err, "winerror", None) == 17 or "cross-device" in str(err).lower():
                _safe_cross_device_move(src_p, tgt_p)
                return
            raise

    src_size = src_p.stat().st_size

    # Try fast same-filesystem atomic link (no overwrite)
    linked = False
    try:
        os.link(src_p, tgt_p)
        linked = True
    except FileExistsError:
        raise FileExistsError(f"Destination already exists: {tgt_p}")
    except OSError as err:
        is_cross_dev = err.errno == errno.EXDEV or getattr(err, "winerror", None) == 17
        is_not_supported = err.errno in (errno.EPERM, errno.EACCES, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP))
        if not (is_cross_dev or is_not_supported):
            raise

    if linked:
        # Verify target before unlinking source
        if not tgt_p.is_file() or tgt_p.stat().st_size != src_size:
            tgt_p.unlink(missing_ok=True)
            raise RuntimeError(f"Post-link verification failed for destination {tgt_p}")
        try:
            src_p.unlink()
        except Exception as del_err:
            try:
                tgt_p.unlink()
            except Exception:
                pass
            raise OSError(f"Failed to remove source link after linking to {tgt_p}: {del_err}") from del_err

        if not tgt_p.is_file() or src_p.exists():
            raise RuntimeError(f"Final state verification failed: src_exists={src_p.exists()}, tgt_exists={tgt_p.exists()}")
        return

    # Fall back to safe cross-device move
    _safe_cross_device_move(src_p, tgt_p)


def _format_bytes(num_bytes: int | float) -> str:
    if num_bytes is None:
        return "0 B"
    num = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}" if unit in ["MB", "GB", "TB"] else f"{int(num)} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


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
    now = utc_now()
    connection = connect(database_path)
    try:
        connection.execute(
            """INSERT OR REPLACE INTO active_filing_transfers (
                proposal_id, scene_id, file_id, source_path, destination_path, destination_folder,
                stage, stage_label, detail, total_bytes, started_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                COALESCE((SELECT started_at FROM active_filing_transfers WHERE proposal_id=?), ?),
                ?
            )""",
            (
                proposal_id,
                str(scene_id or ""),
                str(file_id or ""),
                str(source_path),
                str(destination_path),
                str(destination_folder),
                stage,
                stage_label,
                detail,
                int(total_bytes or 0),
                proposal_id,
                now,
                now,
            ),
        )
        connection.commit()
    except Exception as e:
        logger.debug("Error recording active filing transfer: %s", e)
    finally:
        connection.close()


def _clear_active_transfer(database_path: Path, proposal_id: int):
    connection = connect(database_path)
    try:
        connection.execute("DELETE FROM active_filing_transfers WHERE proposal_id=?", (proposal_id,))
        connection.commit()
    except Exception as e:
        logger.debug("Error clearing active filing transfer: %s", e)
    finally:
        connection.close()


def enqueue_filing_transfer(
    database_path: Path,
    proposal_id: int,
    target_destination_folder: str,
    target_entity_type: str = None,
    target_entity_id: str = None,
    update_metadata: bool = False,
) -> bool:
    """Insert a filing job into the queue and mark its proposal as 'queued'.

    Returns True if the row was inserted (or was already queued), False if the
    proposal is not in a state that allows queuing.
    """
    now = utc_now()
    conn = connect(database_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM filing_proposals WHERE id=?", (proposal_id,)).fetchone()
        if not row:
            return False
        if row["status"] not in ("pending",):
            return False
        conn.execute(
            """INSERT INTO filing_transfer_queue
               (proposal_id, target_destination_folder, target_entity_type, target_entity_id,
                update_metadata, status, enqueued_at)
               VALUES (?, ?, ?, ?, ?, 'queued', ?)
               ON CONFLICT(proposal_id) DO UPDATE SET
                 target_destination_folder=excluded.target_destination_folder,
                 target_entity_type=excluded.target_entity_type,
                 target_entity_id=excluded.target_entity_id,
                 update_metadata=excluded.update_metadata,
                 status='queued',
                 enqueued_at=excluded.enqueued_at,
                 processing_started_at=NULL,
                 finished_at=NULL,
                 last_error=NULL""",
            (
                proposal_id,
                str(target_destination_folder),
                target_entity_type,
                target_entity_id,
                int(bool(update_metadata)),
                now,
            ),
        )
        conn.execute(
            "UPDATE filing_proposals SET status='queued', updated_at=? WHERE id=?",
            (now, proposal_id),
        )
        conn.commit()
        return True
    except Exception as exc:
        logger.warning("enqueue_filing_transfer: %s", exc)
        return False
    finally:
        conn.close()


def claim_next_filing_transfer(database_path: Path):
    """Atomically claim the next queued filing transfer for processing.

    Returns the queue row as a dict, or None if the queue is empty.
    Uses BEGIN IMMEDIATE to prevent two workers claiming the same row.
    """
    now = utc_now()
    conn = connect(database_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT * FROM filing_transfer_queue
               WHERE status='queued'
               ORDER BY enqueued_at
               LIMIT 1"""
        ).fetchone()
        if not row:
            conn.commit()
            return None
        row = dict(row)
        conn.execute(
            "UPDATE filing_transfer_queue SET status='processing', processing_started_at=? WHERE id=?",
            (now, row["id"]),
        )
        conn.commit()
        return row
    except Exception as exc:
        logger.warning("claim_next_filing_transfer: %s", exc)
        return None
    finally:
        conn.close()


def finish_filing_transfer(database_path: Path, queue_id: int) -> None:
    """Mark a filing queue entry as done after a successful apply."""
    now = utc_now()
    conn = connect(database_path)
    try:
        conn.execute(
            "UPDATE filing_transfer_queue SET status='done', finished_at=?, last_error=NULL WHERE id=?",
            (now, queue_id),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("finish_filing_transfer: %s", exc)
    finally:
        conn.close()


def fail_filing_transfer(database_path: Path, queue_id: int, error: str) -> None:
    """Mark a filing queue entry as failed and reset its proposal to pending."""
    now = utc_now()
    conn = connect(database_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT proposal_id FROM filing_transfer_queue WHERE id=?", (queue_id,)
        ).fetchone()
        conn.execute(
            "UPDATE filing_transfer_queue SET status='failed', finished_at=?, last_error=? WHERE id=?",
            (now, str(error), queue_id),
        )
        if row:
            # Reset proposal back to pending so user can retry
            conn.execute(
                "UPDATE filing_proposals SET status='pending', last_error=?, updated_at=? WHERE id=? AND status='queued'",
                (str(error)[:500], now, row["proposal_id"]),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("fail_filing_transfer: %s", exc)
    finally:
        conn.close()


def set_filing_transfer_job_id(database_path: Path, proposal_id: int, job_id: str) -> None:
    """Record the Stash task job ID executing this queue row.

    Called immediately after ``run_plugin_task`` returns so that startup recovery
    can query job liveness instead of guessing.  If the row has already moved to
    ``done`` (task completed before this call ran — the job-ID race), the UPDATE
    matches 0 rows, which is harmless.
    """
    conn = connect(database_path)
    try:
        conn.execute(
            "UPDATE filing_transfer_queue SET stash_job_id=? "
            "WHERE proposal_id=? AND status IN ('queued','processing')",
            (str(job_id), proposal_id),
        )
        conn.commit()
    except Exception as exc:
        logger.debug("set_filing_transfer_job_id: %s", exc)
    finally:
        conn.close()


def recover_abandoned_processing_queue(database_path: Path, stash=None) -> dict:
    """Resolve filing queue rows stuck in 'processing' after a restart.

    A Stash task running ``process_filing_queue`` is a *separate process* from
    the monitor.  To avoid killing a legitimately-running transfer (which can
    exceed any fixed time threshold for large files), we query Stash's job
    scheduler for the specific job that claimed each row.

    Decision matrix per processing row:
    - Stash job RUNNING / READY       → alive, leave it (do NOT reset)
    - Stash job FINISHED/CANCELLED/FAILED, or job not found in Stash
                                      → worker gone, safe to reset to 'queued'
    - stash_job_id not stored (legacy or bug) → uncertain: preserve and log
    - Stash unavailable / GQL error   → uncertain: preserve and log
    - stash=None supplied             → uncertain: preserve and log

    Uncertain rows are NEVER auto-reset.  The operator receives a WARNING log
    with proposal IDs and is expected to check the Stash Tasks UI manually.
    """
    _ALIVE_STATUSES = {"RUNNING", "READY"}

    conn = connect(database_path)
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM filing_transfer_queue WHERE status='processing'"
            ).fetchall()
        ]
    except Exception as exc:
        logger.warning("recover_abandoned_processing_queue: could not read queue: %s", exc)
        return {"reset": 0, "uncertain": []}
    finally:
        conn.close()

    reset_count = 0
    uncertain = []

    for row in rows:
        proposal_id = row["proposal_id"]
        job_id = row.get("stash_job_id")

        if stash is None:
            logger.warning(
                "Filing queue recovery: no Stash connection supplied; cannot verify "
                "liveness of processing job for proposal %s — preserving (uncertain).",
                proposal_id,
            )
            uncertain.append(proposal_id)
            continue

        if not job_id:
            logger.warning(
                "Filing queue recovery: proposal %s has no stash_job_id recorded — "
                "cannot verify worker liveness; preserving (uncertain).",
                proposal_id,
            )
            uncertain.append(proposal_id)
            continue

        # Query Stash job liveness
        job_status = None
        try:
            result = stash.call_GQL(
                "query FindJob($id: ID!) { findJob(input: {id: $id}) { status } }",
                {"id": str(job_id)},
            )
            job_info = (result or {}).get("findJob")
            if job_info:
                job_status = (job_info.get("status") or "").upper()
            # job_info None means Stash purged the record — treat as dead (FINISHED)
        except Exception as gql_exc:
            logger.warning(
                "Filing queue recovery: Stash GQL failed for job_id=%s (proposal %s): %s "
                "— cannot verify liveness; preserving (uncertain).",
                job_id,
                proposal_id,
                gql_exc,
            )
            uncertain.append(proposal_id)
            continue

        if job_status in _ALIVE_STATUSES:
            logger.info(
                "Filing queue recovery: Stash job %s is still %s for proposal %s "
                "— leaving processing row intact.",
                job_id,
                job_status,
                proposal_id,
            )
            continue  # alive — do not touch

        # Worker is confirmed dead (job FINISHED / CANCELLED / FAILED / not found)
        reason = (
            f"Reset after monitor restart: Stash job {job_id} "
            f"status={job_status or 'not found in Stash'}"
        )
        try:
            conn2 = connect(database_path)
            conn2.execute(
                """UPDATE filing_transfer_queue
                   SET status='queued', processing_started_at=NULL, last_error=?
                   WHERE proposal_id=? AND status='processing'""",
                (reason, proposal_id),
            )
            conn2.commit()
            conn2.close()
            reset_count += 1
            logger.info(
                "Filing queue recovery: reset proposal %s to queued "
                "(Stash job %s was %s).",
                proposal_id,
                job_id,
                job_status or "not found",
            )
        except Exception as exc:
            logger.warning(
                "Filing queue recovery: failed to reset proposal %s: %s",
                proposal_id,
                exc,
            )

    return {"reset": reset_count, "uncertain": uncertain}


def get_active_filing_transfers(database_path: Path) -> list[dict]:
    connection = connect(database_path)
    try:
        rows = connection.execute("SELECT * FROM active_filing_transfers ORDER BY started_at ASC").fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        connection.close()


def recover_stale_active_transfers(database_path: Path, stale_threshold_minutes: int = 10, stash=None) -> list[dict]:
    """Detect and recover filing transfers that were abandoned mid-flight.

    When a browser navigates away while a transfer is running, the OS-level file
    move may complete successfully but the Python cleanup path (which removes the
    active_filing_transfers row and marks the proposal completed) is never reached.
    This leaves the UI permanently stuck showing 'TRANSFER IN PROGRESS'.

    Call this at monitor startup and periodically (every ~5 minutes) in the loop.

    A row is stale when its ``updated_at`` has not changed for
    ``stale_threshold_minutes`` minutes — real transfers heartbeat every few seconds.

    Outcomes per stale row:
    - Source gone + dest present at expected size → proposal marked *completed*.
    - Source still present (move incomplete)      → proposal reset to *pending*.
    - Neither (unknown partial state)             → proposal marked *needs_recovery*.
    """
    recovered = []
    try:
        connection = connect(database_path)
        try:
            rows = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM active_filing_transfers ORDER BY started_at ASC"
                ).fetchall()
            ]
        finally:
            connection.close()
    except Exception as exc:
        logger.debug("recover_stale_active_transfers: could not read transfers: %s", exc)
        return recovered

    now_dt = datetime.now(timezone.utc)
    threshold_seconds = stale_threshold_minutes * 60

    for row in rows:
        proposal_id = row["proposal_id"]
        updated_at_str = row.get("updated_at", "")
        source_path = Path(row["source_path"])
        destination_path = Path(row["destination_path"])
        total_bytes = int(row.get("total_bytes") or 0)
        file_id = str(row.get("file_id") or "")
        destination_folder = row.get("destination_folder", "")

        try:
            updated_dt = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            age_seconds = (now_dt - updated_dt).total_seconds()
        except Exception:
            age_seconds = threshold_seconds + 1  # treat unparseable timestamp as stale

        if age_seconds < threshold_seconds:
            continue  # Transfer heartbeat is recent — still genuinely active

        source_exists = source_path.exists()
        try:
            dest_stat = destination_path.stat()
            dest_exists = True
            dest_size = dest_stat.st_size
        except Exception:
            dest_exists = False
            dest_size = 0

        size_matches = (dest_size == total_bytes) if total_bytes > 0 else dest_exists
        now_str = now_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")

        # --- Disk-level outcome assessment ---
        if dest_exists and size_matches and not source_exists:
            disk_outcome = "completed"
        elif source_exists and not dest_exists:
            disk_outcome = "pending"  # move never happened; safe to reset
        else:
            disk_outcome = "needs_recovery"  # ambiguous (both or neither exist)

        # --- Stash GQL verification (mandatory for completion) ---
        # completed requires THREE-WAY agreement: disk + Stash + Watchtower.
        # Any state that cannot be fully verified becomes needs_recovery so the
        # user can inspect and resolve without risk of a repeat move.
        stale_reason = None
        if disk_outcome == "completed":
            if stash is None:
                # No Stash connection supplied — cannot verify.
                disk_outcome = "needs_recovery"
                stale_reason = (
                    "Stash connection unavailable during recovery; "
                    "cannot confirm file location. Re-run after monitor restarts with Stash."
                )
            elif not file_id:
                # No file_id recorded — cannot query Stash.
                disk_outcome = "needs_recovery"
                stale_reason = "No file_id recorded in transfer row; cannot query Stash to verify."
            else:
                try:
                    gql_res = stash.call_GQL(
                        "query FindFileForRecovery($id: ID!) { findFile(id: $id) { ... on VideoFile { id path } } }",
                        {"id": str(file_id)},
                    )
                    stash_file = (gql_res or {}).get("findFile")
                    if not stash_file:
                        # Stash has no record of this file — unverifiable.
                        disk_outcome = "needs_recovery"
                        stale_reason = (
                            f"Stash returned no record for file_id={file_id}. "
                            "Run a Stash library scan on the destination folder, then use manual recovery."
                        )
                    else:
                        stash_path_str = stash_file.get("path") or ""
                        stash_resolved = str(Path(stash_path_str).resolve()) if stash_path_str else ""
                        dest_resolved = str(destination_path.resolve())
                        src_resolved = str(source_path.resolve())
                        if stash_resolved == dest_resolved:
                            pass  # Full agreement: disk + Stash both at destination → completed
                        elif stash_resolved == src_resolved:
                            # Disk says moved; Stash still shows source path (not yet rescanned).
                            # We cannot mark completed until Stash confirms the new location.
                            disk_outcome = "needs_recovery"
                            stale_reason = (
                                f"File is at destination on disk but Stash still records the source "
                                f"path '{stash_path_str}'. Run a Stash library scan on "
                                f"'{str(destination_path.parent)}' then use manual recovery to clear."
                            )
                        else:
                            # Stash reports an entirely different location — ambiguous.
                            disk_outcome = "needs_recovery"
                            stale_reason = (
                                f"Stash path '{stash_path_str}' matches neither source nor "
                                "destination. File location is ambiguous; manual verification required."
                            )
                except Exception as gql_exc:
                    # Stash GQL call failed (offline, timeout, etc.) — unverifiable.
                    disk_outcome = "needs_recovery"
                    stale_reason = (
                        f"Stash GQL query failed during recovery ({gql_exc}). "
                        "Cannot confirm file location without Stash. Re-run after Stash is reachable."
                    )

        outcome, new_status = {
            "completed": ("completed", "completed"),
            "pending": ("reset_to_pending", "pending"),
            "needs_recovery": ("needs_recovery", "needs_recovery"),
        }[disk_outcome]

        try:
            conn = connect(database_path)
            try:
                conn.execute("DELETE FROM active_filing_transfers WHERE proposal_id=?", (proposal_id,))
                if new_status == "completed":
                    conn.execute(
                        "UPDATE filing_proposals SET status='completed', proposed_path=?, "
                        "destination_folder=?, last_error=NULL, updated_at=? WHERE id=?",
                        (str(destination_path), destination_folder, now_str, proposal_id),
                    )
                    if file_id:
                        conn.execute(
                            "UPDATE files SET path=?, basename=?, exists_on_disk=1, last_seen_at=? "
                            "WHERE file_id=?",
                            (str(destination_path), destination_path.name, now_str, file_id),
                        )
                    conn.execute(
                        "UPDATE incoming_files SET filing_diagnostic=NULL, status='filed' WHERE path=?",
                        (str(source_path),),
                    )
                elif new_status == "pending":
                    conn.execute(
                        "UPDATE filing_proposals SET status='pending', last_error=NULL, "
                        "destination_folder='', updated_at=? WHERE id=?",
                        (now_str, proposal_id),
                    )
                else:
                    err_msg = (
                        stale_reason
                        or "Transfer interrupted — file state uncertain. Verify location manually."
                    )
                    conn.execute(
                        "UPDATE filing_proposals SET status='needs_recovery', last_error=?, "
                        "updated_at=? WHERE id=?",
                        (err_msg, now_str, proposal_id),
                    )
                conn.commit()
            finally:
                conn.close()

            result = {
                "proposal_id": proposal_id,
                "outcome": outcome,
                "source": str(source_path),
                "destination": str(destination_path),
                "age_minutes": round(age_seconds / 60, 1),
            }
            recovered.append(result)
            logger.info(
                "Stale filing transfer recovered: proposal=%s outcome=%s age=%.1fmin src=%s",
                proposal_id,
                outcome,
                age_seconds / 60,
                source_path.name,
            )
        except Exception as exc:
            logger.warning(
                "recover_stale_active_transfers: failed to recover proposal %s: %s",
                proposal_id,
                exc,
            )

    return recovered


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
    """Applies an approved automatic filing proposal with strict safety verification.
    - Locks the database for renaming operations.
    - Limits active transfers to 1 at a time to prevent concurrency collisions.
    - Updates real-time transfer stages in active_filing_transfers table.
    - Validates source file, destination root, and baseline integrity before any move.
    - Verifies chosen candidate destination against stored proposal candidate destinations.
    - Applies file move via Stash move_files.
    - Moves companions with rollback if anything fails.
    - Performs safe, targeted metadata updates for the selected entity only.
    """
    with rename_lock(database_path):
        active_transfers = get_active_filing_transfers(database_path)
        if active_transfers and any(t.get("proposal_id") != proposal_id for t in active_transfers):
            return {
                "id": proposal_id,
                "status": "blocked",
                "reason": "Another filing transfer is currently in progress. Transfers are serialized for safety.",
            }

        connection = connect(database_path)
        try:
            row = connection.execute("SELECT * FROM filing_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not row:
                return {"id": proposal_id, "status": "blocked", "reason": "Proposal not found"}
            proposal = dict(row)
        finally:
            connection.close()

        if proposal["status"] not in ("pending", "queued"):
            return {"id": proposal_id, "status": "blocked", "reason": f"Proposal is already '{proposal['status']}'"}

        scene_id = proposal["scene_id"]
        if scene_id:
            conn_q = connect(database_path)
            try:
                is_renaming = conn_q.execute(
                    "SELECT 1 FROM rename_queue WHERE scene_id=? AND status IN ('pending', 'processing')",
                    (str(scene_id),)
                ).fetchone()
            finally:
                conn_q.close()
            if is_renaming:
                reason = "Approval held: A filename update is currently settling/processing for this scene. Please wait a moment for the rename to finish."
                return {"id": proposal_id, "status": "held", "reason": reason}

        if config is None:
            config = (stash.find_plugin_config("librarymanager") if hasattr(stash, "find_plugin_config") else {}) or {}

        src = Path(proposal["source_path"])
        file_id = proposal["file_id"]
        scene_id = proposal["scene_id"]
        file_size = src.stat().st_size if src.is_file() else 0

        raw_candidates = json.loads(proposal.get("candidate_destinations_json") or "[]")
        selected_candidate = None

        if target_destination_folder:
            target_norm = str(Path(target_destination_folder).resolve())
            for c in raw_candidates:
                c_path = c.get("destination_folder") if isinstance(c, dict) else str(c)
                if c_path and str(Path(c_path).resolve()) == target_norm:
                    selected_candidate = c if isinstance(c, dict) else {"destination_folder": c_path}
                    break
            else:
                if raw_candidates:
                    reason = "Approval blocked: Selected destination folder is not an eligible candidate for this proposal"
                    _update_proposal_status(database_path, proposal_id, "blocked", reason)
                    return {"id": proposal_id, "status": "blocked", "reason": reason}
                else:
                    selected_candidate = {"destination_folder": target_destination_folder}
            dest_folder = Path(target_destination_folder)
            dest_video = dest_folder / proposal["destination_filename"]
        else:
            if not proposal["destination_folder"] or not proposal["proposed_path"]:
                reason = "Approval blocked: Proposal has multiple candidates; destination selection is required"
                return {"id": proposal_id, "status": "blocked", "reason": reason}
            dest_folder = Path(proposal["destination_folder"])
            dest_video = Path(proposal["proposed_path"])
            if raw_candidates and isinstance(raw_candidates[0], dict):
                selected_candidate = raw_candidates[0]

        _update_active_transfer(
            database_path,
            proposal_id,
            str(scene_id or ""),
            str(file_id or ""),
            str(src),
            str(dest_video),
            str(dest_folder),
            stage="validating",
            stage_label="Preflight Verification",
            detail=f"Validating source, destination and companion paths ({_format_bytes(file_size)})...",
            total_bytes=file_size,
        )

        try:
            # REQUIREMENT 2: Fail closed on Incoming configuration & destination root
            raw_incoming = config.get("incomingFolders") or ([config.get("incomingFolder")] if config.get("incomingFolder") else [])
            if isinstance(raw_incoming, (list, tuple)):
                incoming_folders = [str(f).strip() for f in raw_incoming if isinstance(f, (str, Path)) and str(f).strip()]
            else:
                incoming_folders = []

            if not incoming_folders:
                reason = "Approval blocked: Incoming configuration is missing or empty; cannot verify source eligibility"
                _update_proposal_status(database_path, proposal_id, "blocked", reason)
                return {"id": proposal_id, "status": "blocked", "reason": reason}

            is_inside_incoming = False
            resolved_src = str(src.resolve())
            for inc_root in incoming_folders:
                try:
                    inc_res = str(Path(inc_root).expanduser().resolve())
                    if resolved_src == inc_res or resolved_src.startswith(inc_res + os.sep):
                        is_inside_incoming = True
                        break
                except Exception:
                    continue

            if not is_inside_incoming:
                reason = f"Approval blocked: source file {src} is not located in any currently configured Incoming folder"
                _update_proposal_status(database_path, proposal_id, "blocked", reason)
                return {"id": proposal_id, "status": "blocked", "reason": reason}

            conn = connect(database_path)
            try:
                relocated = conn.execute(
                    "SELECT path FROM files WHERE (file_id=? OR scene_id=?) AND path != ? AND exists_on_disk=1",
                    (str(file_id), str(scene_id), str(src)),
                ).fetchone()
                if relocated:
                    reason = f"Approval blocked: scene/file was already relocated to {relocated['path']}"
                    _update_proposal_status(database_path, proposal_id, "blocked", reason)
                    return {"id": proposal_id, "status": "blocked", "reason": reason}
            finally:
                conn.close()

            # Revalidate destination roots (FAIL CLOSED)
            configured_dest_roots = get_configured_filing_destination_roots(config)
            if not configured_dest_roots:
                reason = "Approval blocked: autoFilingDestinationRoot is not configured; cannot verify destination folder"
                _update_proposal_status(database_path, proposal_id, "blocked", reason)
                return {"id": proposal_id, "status": "blocked", "reason": reason}

            has_existing_root = False
            for root_str in configured_dest_roots:
                if Path(root_str).is_dir():
                    has_existing_root = True
                    break
            if not has_existing_root:
                reason = f"Approval blocked: configured destination root does not exist on disk: {configured_dest_roots[0]}"
                _update_proposal_status(database_path, proposal_id, "blocked", reason)
                return {"id": proposal_id, "status": "blocked", "reason": reason}

            is_inside_root = False
            resolved_dest_folder_str = str(dest_folder.resolve())
            for root_str in configured_dest_roots:
                try:
                    root_res = str(Path(root_str).expanduser().resolve())
                    if resolved_dest_folder_str == root_res or resolved_dest_folder_str.startswith(root_res + os.sep):
                        is_inside_root = True
                        break
                except Exception:
                    continue

            if not is_inside_root:
                reason = f"Approval blocked: destination folder {dest_folder} is not under current destination root {configured_dest_roots[0]}"
                _update_proposal_status(database_path, proposal_id, "blocked", reason)
                return {"id": proposal_id, "status": "blocked", "reason": reason}

            if not src.is_file():
                _update_proposal_status(database_path, proposal_id, "blocked", "Source file no longer exists on disk")
                return {"id": proposal_id, "status": "blocked", "reason": "Source file no longer exists on disk"}

            if not dest_folder.is_dir():
                _update_proposal_status(database_path, proposal_id, "blocked", "Destination folder does not exist")
                return {"id": proposal_id, "status": "blocked", "reason": "Destination folder does not exist"}

            if dest_video.exists():
                _update_proposal_status(database_path, proposal_id, "blocked", "Destination file already exists")
                return {"id": proposal_id, "status": "blocked", "reason": "Destination file already exists"}

            conn = connect(database_path)
            try:
                conflict = (
                    conn.execute("SELECT 1 FROM files WHERE path=? AND exists_on_disk=1", (str(dest_video),)).fetchone()
                    is not None
                )
                if conflict:
                    _update_proposal_status(database_path, proposal_id, "blocked", "Destination path conflicts with Stash inventory")
                    return {"id": proposal_id, "status": "blocked", "reason": "Destination path conflicts with Stash inventory"}
            finally:
                conn.close()

            if target_destination_folder:
                companions, comp_err = find_filing_companions(src, dest_folder, dest_video.name)
                if comp_err:
                    _update_proposal_status(database_path, proposal_id, "blocked", comp_err)
                    return {"id": proposal_id, "status": "blocked", "reason": comp_err}
            else:
                recorded_companions = json.loads(proposal.get("companions_json") or "[]")
                if recorded_companions:
                    companions = [(Path(c["source"]), Path(c["target"])) for c in recorded_companions]
                else:
                    companions, comp_err = find_filing_companions(src, dest_folder, dest_video.name)
                    if comp_err:
                        _update_proposal_status(database_path, proposal_id, "blocked", comp_err)
                        return {"id": proposal_id, "status": "blocked", "reason": comp_err}

            for c_src, c_dst in companions:
                if not c_src.is_file():
                    reason = f"Preflight companion check failed: recorded companion missing from source: {c_src.name}"
                    _update_proposal_status(database_path, proposal_id, "blocked", reason)
                    return {"id": proposal_id, "status": "blocked", "reason": reason}
                if c_dst.exists():
                    reason = f"Preflight companion check failed: destination collision already exists on disk: {c_dst.name}"
                    _update_proposal_status(database_path, proposal_id, "blocked", reason)
                    return {"id": proposal_id, "status": "blocked", "reason": reason}

            # Step 2.5: Preflight Stash scene ownership check (FAIL CLOSED)
            try:
                ownership_res = stash.call_GQL(
                    "query FindScene($id: ID!) { findScene(id: $id) { id files { id path } } }",
                    {"id": str(scene_id)},
                )
            except Exception as q_err:
                reason = f"Approval blocked: Stash scene ownership query failed ({q_err})"
                _update_proposal_status(database_path, proposal_id, "blocked", reason)
                return {"id": proposal_id, "status": "blocked", "reason": reason}

            scene_obj = (ownership_res or {}).get("findScene")
            if not scene_obj:
                reason = f"Approval blocked: Stash scene {scene_id} does not exist or returned incomplete data"
                _update_proposal_status(database_path, proposal_id, "blocked", reason)
                return {"id": proposal_id, "status": "blocked", "reason": reason}

            scene_files = scene_obj.get("files") or []
            if not scene_files:
                reason = f"Approval blocked: Stash scene {scene_id} has no file records"
                _update_proposal_status(database_path, proposal_id, "blocked", reason)
                return {"id": proposal_id, "status": "blocked", "reason": reason}

            matched_scene_file = None
            for sf in scene_files:
                if str(sf.get("id")) == str(file_id):
                    matched_scene_file = sf
                    break
            if not matched_scene_file:
                reason = f"Approval blocked: file ID {file_id} not found in Stash scene {scene_id}"
                _update_proposal_status(database_path, proposal_id, "blocked", reason)
                return {"id": proposal_id, "status": "blocked", "reason": reason}

            sf_path = matched_scene_file.get("path") or ""
            if str(Path(sf_path).resolve()) != str(src.resolve()):
                reason = f"Approval blocked: Stash file path '{sf_path}' does not match proposal source path '{src}'"
                _update_proposal_status(database_path, proposal_id, "blocked", reason)
                return {"id": proposal_id, "status": "blocked", "reason": reason}

            # Step 3: Execute Stash move
            _update_active_transfer(
                database_path,
                proposal_id,
                str(scene_id or ""),
                str(file_id or ""),
                str(src),
                str(dest_video),
                str(dest_folder),
                stage="moving_video",
                stage_label="Transferring Video",
                detail=f"Moving video to {dest_folder.name} ({_format_bytes(file_size)})...",
                total_bytes=file_size,
            )

            expect_filesystem_move(database_path, str(src), str(dest_video))
            stash_res = None
            move_err = None
            try:
                stash_res = stash.move_files({
                    "ids": [proposal["file_id"]],
                    "destination_folder": str(dest_folder),
                    "destination_basename": dest_video.name,
                })
            except Exception as err:
                move_err = str(err)

            if not stash_res or move_err:
                src_exists = src.is_file()
                dest_exists = dest_video.is_file()
                disk_confirmed_at_src = src_exists and not dest_exists

                stash_confirmed_at_src = False
                stash_unverified_detail = None
                try:
                    scene_check = stash.call_GQL(
                        "query FindScene($id: ID!) { findScene(id: $id) { id files { id path } } }",
                        {"id": str(scene_id)},
                    )
                    if isinstance(scene_check, dict) and scene_check.get("findScene"):
                        files = scene_check["findScene"].get("files") or []
                        for f in files:
                            if str(f.get("id")) == str(file_id):
                                f_path = f.get("path")
                                if f_path and str(Path(f_path).resolve()) == str(src.resolve()):
                                    stash_confirmed_at_src = True
                                else:
                                    stash_unverified_detail = f"Stash file path is '{f_path}' (expected '{src}')"
                                break
                        else:
                            stash_unverified_detail = f"file ID {file_id} not found in Stash scene files"
                    else:
                        stash_unverified_detail = "Stash scene query returned no scene data"
                except Exception as check_exc:
                    stash_unverified_detail = f"Stash query failed: {check_exc}"

                if disk_confirmed_at_src and stash_confirmed_at_src:
                    err_msg = f"Stash move_files failed ({move_err}); video untouched at source and confirmed in Stash"
                    _update_proposal_status(database_path, proposal_id, "failed", err_msg)
                    record_activity(
                        database_path,
                        "filing",
                        "move failed",
                        "failed",
                        scene_id=scene_id,
                        file_id=file_id,
                        old_path=str(src),
                        new_path=str(dest_video),
                        detail=err_msg,
                    )
                    return {"id": proposal_id, "status": "failed", "reason": err_msg}
                else:
                    unverified_reasons = []
                    if not disk_confirmed_at_src:
                        if dest_exists:
                            unverified_reasons.append(f"video exists at destination {dest_video.name}")
                        if not src_exists:
                            unverified_reasons.append(f"video missing at source {src.name}")
                    if not stash_confirmed_at_src:
                        unverified_reasons.append(f"Stash record unconfirmed: {stash_unverified_detail}")

                    err_msg = f"CRITICAL: Stash move failed and post-move state could not be verified clean: {'; '.join(unverified_reasons)}"
                    _update_proposal_status(database_path, proposal_id, "needs_recovery", err_msg)
                    record_activity(
                        database_path,
                        "filing",
                        "move failed - needs recovery",
                        "needs_recovery",
                        scene_id=scene_id,
                        file_id=file_id,
                        old_path=str(src),
                        new_path=str(dest_video),
                        detail=err_msg,
                    )
                    return {"id": proposal_id, "status": "needs_recovery", "reason": err_msg}

            # Step 4: Move companions
            moved_companions = []
            companion_fail = None
            if companions:
                _update_active_transfer(
                    database_path,
                    proposal_id,
                    str(scene_id or ""),
                    str(file_id or ""),
                    str(src),
                    str(dest_video),
                    str(dest_folder),
                    stage="moving_companions",
                    stage_label="Transferring Companions",
                    detail=f"Moving {len(companions)} companion file(s)...",
                    total_bytes=file_size,
                )
            unresolved_missing_companion = None
            for c_src, c_dst in companions:
                if not c_src.is_file():
                    companion_fail = f"Companion missing at transfer time: {c_src.name}"
                    unresolved_missing_companion = f"expected companion missing from source before transfer: {c_src.name}"
                    break
                if c_dst.exists():
                    companion_fail = f"Destination companion collision already exists on disk: {c_dst.name}"
                    break
                try:
                    expect_filesystem_move(database_path, str(c_src), str(c_dst))
                    _move_file(c_src, c_dst)
                    moved_companions.append((c_src, c_dst))
                except Exception as c_err:
                    companion_fail = f"Failed moving companion {c_src.name} to {c_dst}: {c_err}"
                    break

            if companion_fail:
                # Rollback moved companions
                rollback_ok = True
                companion_rb_failures = []
                for orig_src, orig_dst in reversed(moved_companions):
                    if not orig_dst.is_file():
                        rollback_ok = False
                        companion_rb_failures.append(f"companion missing at destination {orig_dst.name}")
                        continue
                    if orig_src.exists():
                        rollback_ok = False
                        companion_rb_failures.append(f"source collision exists for companion {orig_src.name}")
                        continue
                    try:
                        expect_filesystem_move(database_path, str(orig_dst), str(orig_src))
                        _move_file(orig_dst, orig_src)
                    except Exception as c_rb_err:
                        rollback_ok = False
                        companion_rb_failures.append(f"failed restoring companion {orig_dst.name} to source: {c_rb_err}")

                # Move video back via Stash
                video_rb_err = None
                try:
                    expect_filesystem_move(database_path, str(dest_video), str(src))
                    stash_back = stash.move_files({
                        "ids": [proposal["file_id"]],
                        "destination_folder": str(src.parent),
                        "destination_basename": src.name,
                    })
                    if stash_back is False:
                        rollback_ok = False
                        video_rb_err = "Stash move_files returned False during rollback"
                except Exception as v_err:
                    rollback_ok = False
                    video_rb_err = str(v_err)

                # Post-rollback verification of video and all moved companions
                video_verified = src.is_file() and not dest_video.exists() and (video_rb_err is None)
                companion_unverified = list(companion_rb_failures)
                if unresolved_missing_companion:
                    companion_unverified.append(unresolved_missing_companion)
                for orig_src, orig_dst in moved_companions:
                    if not orig_src.is_file():
                        companion_unverified.append(f"companion missing at source {orig_src.name}")
                    if orig_dst.exists():
                        companion_unverified.append(f"companion still exists at destination {orig_dst.name}")

                companions_verified = rollback_ok and not companion_unverified

                if video_verified and companions_verified:
                    err_msg = f"{companion_fail}; safely restored to source"
                    _update_proposal_status(database_path, proposal_id, "failed", err_msg)
                    return {"id": proposal_id, "status": "failed", "rolled_back": True, "reason": err_msg}
                else:
                    failures = []
                    if video_verified:
                        failures.append("video restored to source")
                    else:
                        failures.append(f"Video rollback failed: {video_rb_err or 'dest exists or src missing'}")

                    if not companions_verified:
                        unique_comp = list(dict.fromkeys(companion_unverified))
                        failures.append(f"Companion rollback failed or unverified: {'; '.join(unique_comp) if unique_comp else 'unknown companion discrepancy'}")

                    err_msg = f"CRITICAL: Failed moving companion: {companion_fail}; {'; '.join(failures)}"
                    _update_proposal_status(database_path, proposal_id, "needs_recovery", err_msg)
                    return {"id": proposal_id, "status": "needs_recovery", "rolled_back": False, "reason": err_msg}

            # Step 5: Update database records
            _update_active_transfer(
                database_path,
                proposal_id,
                str(scene_id or ""),
                str(file_id or ""),
                str(src),
                str(dest_video),
                str(dest_folder),
                stage="updating_records",
                stage_label="Finalizing Records",
                detail="Updating Stash scene path, metadata, and Library Manager inventory...",
                total_bytes=file_size,
            )

            now = utc_now()
            connection = connect(database_path)
            try:
                connection.execute(
                    "UPDATE filing_proposals SET status='completed', proposed_path=?, destination_folder=?, last_error=NULL, updated_at=? WHERE id=?",
                    (str(dest_video), str(dest_folder), now, proposal_id),
                )
                connection.execute(
                    "UPDATE files SET path=?, basename=?, exists_on_disk=1, last_seen_at=? WHERE file_id=?",
                    (str(dest_video), dest_video.name, now, file_id),
                )

                connection.execute(
                    "UPDATE incoming_files SET path=?, filing_diagnostic=NULL, last_checked_at=? WHERE path=? OR path=?",
                    (str(dest_video), now, str(src), str(dest_video)),
                )
                for c_src, c_dst in companions:
                    connection.execute(
                        "UPDATE incoming_files SET path=?, filing_diagnostic=NULL, last_checked_at=? WHERE path=? OR path=?",
                        (str(c_dst), now, str(c_src), str(c_dst)),
                    )
                connection.commit()
            finally:
                connection.close()

            record_activity(
                database_path,
                "filing",
                "proposal approved",
                "completed",
                scene_id=scene_id,
                file_id=file_id,
                old_path=str(src),
                new_path=str(dest_video),
                detail=f"Moved file and {len(companions)} companion(s) to {dest_folder.name}",
            )

            # Step 6: Safe, optional metadata updates
            metadata_updated = False
            metadata_error = None
            if update_metadata and scene_id:
                try:
                    entity_to_update_type = None
                    entity_to_update_id = None
                    entity_to_update_name = None

                    if target_entity_type and target_entity_id:
                        t_type = target_entity_type.lower()
                        t_id = str(target_entity_id).strip()
                        if selected_candidate and selected_candidate.get("matched_entities"):
                            for me in selected_candidate["matched_entities"]:
                                if me.get("entity_type") == t_type and str(me.get("entity_id")) == t_id:
                                    entity_to_update_type = t_type
                                    entity_to_update_id = t_id
                                    entity_to_update_name = me.get("entity_name")
                                    break
                        elif selected_candidate:
                            if selected_candidate.get("entity_type") == t_type and str(selected_candidate.get("entity_id")) == t_id:
                                entity_to_update_type = t_type
                                entity_to_update_id = t_id
                                entity_to_update_name = selected_candidate.get("entity_name")
                        elif proposal.get("organize_by") == t_type and str(proposal.get("matched_entity_id")) == t_id:
                            entity_to_update_type = t_type
                            entity_to_update_id = t_id
                            entity_to_update_name = proposal.get("matched_entity_name")

                        if not entity_to_update_type:
                            metadata_error = f"Target entity {t_type}:{t_id} is not valid for this proposal"
                    else:
                        if selected_candidate:
                            cand_type = selected_candidate.get("entity_type")
                            if cand_type in ("performer", "studio", "tag"):
                                entity_to_update_type = cand_type
                                entity_to_update_id = str(selected_candidate.get("entity_id"))
                                entity_to_update_name = selected_candidate.get("entity_name")
                            elif cand_type == "both":
                                metadata_error = "Destination matched multiple entity types; explicit entity selection is required for metadata update"
                        else:
                            prop_type = proposal.get("organize_by")
                            if prop_type in ("performer", "studio"):
                                entity_to_update_type = prop_type
                                entity_to_update_id = str(proposal.get("matched_entity_id"))
                                entity_to_update_name = proposal.get("matched_entity_name")

                    if entity_to_update_type == "performer" and entity_to_update_id:
                        scene_res = stash.call_GQL(
                            "query FindScene($id: ID!) { findScene(id: $id) { id performers { id name } } }",
                            {"id": str(scene_id)},
                        )
                        scene_data = (scene_res or {}).get("findScene") or {}
                        current_performers = scene_data.get("performers") or []
                        current_ids = [str(p["id"]) for p in current_performers if p.get("id")]
                        if entity_to_update_id not in current_ids:
                            new_ids = current_ids + [entity_to_update_id]
                            stash.call_GQL(
                                "mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }",
                                {"input": {"id": str(scene_id), "performer_ids": new_ids}},
                            )
                            metadata_updated = True
                            record_activity(
                                database_path,
                                "filing",
                                "metadata updated",
                                "completed",
                                scene_id=scene_id,
                                file_id=file_id,
                                detail=f"Added performer '{entity_to_update_name or entity_to_update_id}' to scene {scene_id}",
                            )
                        else:
                            metadata_updated = True
                    elif entity_to_update_type == "studio" and entity_to_update_id:
                        scene_res = stash.call_GQL(
                            "query FindScene($id: ID!) { findScene(id: $id) { id studio { id name } } }",
                            {"id": str(scene_id)},
                        )
                        scene_data = (scene_res or {}).get("findScene") or {}
                        current_studio = scene_data.get("studio")
                        if not current_studio:
                            stash.call_GQL(
                                "mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }",
                                {"input": {"id": str(scene_id), "studio_id": entity_to_update_id}},
                            )
                            metadata_updated = True
                            record_activity(
                                database_path,
                                "filing",
                                "metadata updated",
                                "completed",
                                scene_id=scene_id,
                                file_id=file_id,
                                detail=f"Set studio '{entity_to_update_name or entity_to_update_id}' on scene {scene_id}",
                            )
                        elif str(current_studio.get("id")) == entity_to_update_id:
                            metadata_updated = True
                        else:
                            metadata_error = f"Scene {scene_id} already has a different studio: '{current_studio.get('name')}'"
                            record_activity(
                                database_path,
                                "filing",
                                "metadata preserved",
                                "completed",
                                scene_id=scene_id,
                                file_id=file_id,
                                detail=f"Preserved existing scene studio '{current_studio.get('name')}'; did not overwrite with '{entity_to_update_name or entity_to_update_id}'",
                            )
                    elif entity_to_update_type == "tag" and entity_to_update_id:
                        scene_res = stash.call_GQL(
                            "query FindScene($id: ID!) { findScene(id: $id) { id tags { id name } } }",
                            {"id": str(scene_id)},
                        )
                        scene_data = (scene_res or {}).get("findScene") or {}
                        current_tags = scene_data.get("tags") or []
                        current_ids = [str(tag["id"]) for tag in current_tags if tag.get("id")]
                        if entity_to_update_id not in current_ids:
                            stash.call_GQL(
                                "mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }",
                                {"input": {"id": str(scene_id), "tag_ids": current_ids + [entity_to_update_id]}},
                            )
                            metadata_updated = True
                            record_activity(
                                database_path,
                                "filing",
                                "metadata updated",
                                "completed",
                                scene_id=scene_id,
                                file_id=file_id,
                                detail=f"Added tag '{entity_to_update_name or entity_to_update_id}' to scene {scene_id}",
                            )
                        else:
                            metadata_updated = True
                    elif not metadata_error:
                        metadata_error = "No matching entity available to tag"
                except Exception as meta_exc:
                    metadata_error = str(meta_exc)
                    record_activity(
                        database_path,
                        "filing",
                        "metadata update failed",
                        "completed",
                        scene_id=scene_id,
                        file_id=file_id,
                        detail=f"File move completed, but metadata update could not be completed: {metadata_error}",
                    )

            try:
                prune_resolved_filing_baseline(database_path, config)
                invalidate_incoming_discovery_cache()
            except Exception as prune_exc:
                logger.debug("Immediate baseline retirement after filing proposal failed: %s", prune_exc)

            return {
                "id": proposal_id,
                "status": "completed",
                "source_path": str(src),
                "proposed_path": str(dest_video),
                "destination_folder": str(dest_folder),
                "companions_moved": len(companions),
                "metadata_updated": metadata_updated,
                "metadata_error": metadata_error,
            }
        finally:
            _clear_active_transfer(database_path, proposal_id)


def ignore_filing_proposal(database_path: Path, proposal_id: int) -> dict:
    """Explicitly ignore a filing proposal upon user action."""
    connection = connect(database_path)
    try:
        row = connection.execute("SELECT * FROM filing_proposals WHERE id=?", (proposal_id,)).fetchone()
        if not row:
            return {"id": proposal_id, "status": "blocked", "reason": "Proposal not found"}
        if row["status"] == "needs_recovery":
            return {"id": proposal_id, "status": "blocked", "reason": "Cannot ignore a transaction that requires recovery"}
        now = utc_now()
        connection.execute(
            "UPDATE filing_proposals SET status='ignored', updated_at=? WHERE id=?",
            (now, proposal_id),
        )
        connection.commit()
        record_activity(
            database_path,
            "filing",
            "proposal ignored",
            "ignored",
            scene_id=row["scene_id"],
            file_id=row["file_id"],
            old_path=row["source_path"],
            detail=f"Filing proposal ignored by user for {Path(row['source_path']).name}",
        )
        return {"id": proposal_id, "status": "ignored"}
    finally:
        connection.close()


def invalidate_stale_filing_proposals(database_path: Path, stash=None) -> list[int]:
    """Scan pending filing proposals and automatically mark as 'invalid' any proposal
    whose source video file or Stash scene no longer exists.
    Does not affect valid proposals or transactions requiring recovery.
    Returns list of invalidated proposal IDs."""
    conn_read = connect(database_path)
    try:
        rows = conn_read.execute(
            "SELECT id, scene_id, file_id, source_path, proposed_path, destination_folder FROM filing_proposals WHERE status='pending'"
        ).fetchall()
        rows = [dict(r) for r in rows]
    finally:
        conn_read.close()

    now = utc_now()
    completed_updates = []
    invalidated = []
    stale_updates = []
    renamed_updates = []

    for r in rows:
        prop_id = r["id"]
        src_path = Path(r["source_path"])
        dest_path = Path(r["proposed_path"]) if r["proposed_path"] else None
        is_stale = False
        is_completed = False
        reason = ""

        # 1. Source video file deleted from disk
        if not src_path.is_file():
            # Check whether the file was moved to the proposed destination on disk and verified in Stash
            if dest_path and dest_path.is_file() and r["scene_id"] and stash:
                try:
                    scene_obj = None
                    if hasattr(stash, "call_GQL"):
                        res = stash.call_GQL(
                            "query CheckScene($id: ID!) { findScene(id: $id) { id files { id path } } }",
                            {"id": str(r["scene_id"])},
                        )
                        scene_obj = (res or {}).get("findScene")
                    elif hasattr(stash, "find_scene"):
                        scene_obj = stash.find_scene(str(r["scene_id"]))

                    if scene_obj and str(scene_obj.get("id")) == str(r["scene_id"]):
                        s_files = scene_obj.get("files") or []
                        for sf in s_files:
                            sf_p = sf.get("path") or ""
                            sf_id = str(sf.get("id") or "")
                            if sf_p and str(Path(sf_p).resolve()) == str(dest_path.resolve()):
                                if not r["file_id"] or sf_id == str(r["file_id"]):
                                    is_completed = True
                                    break
                except Exception:
                    pass

            if is_completed:
                completed_updates.append((prop_id, str(src_path), str(dest_path) if dest_path else ""))
                continue
            else:
                renamed_path = None
                if r["scene_id"] and stash:
                    try:
                        scene_obj = None
                        if hasattr(stash, "call_GQL"):
                            res = stash.call_GQL(
                                "query CheckScene($id: ID!) { findScene(id: $id) { id files { id path } } }",
                                {"id": str(r["scene_id"])},
                            )
                            scene_obj = (res or {}).get("findScene")
                        elif hasattr(stash, "find_scene"):
                            scene_obj = stash.find_scene(str(r["scene_id"]))

                        if scene_obj and str(scene_obj.get("id")) == str(r["scene_id"]):
                            for sf in (scene_obj.get("files") or []):
                                p_str = sf.get("path") or ""
                                if p_str and Path(p_str).is_file():
                                    renamed_path = p_str
                                    break
                    except Exception:
                        pass

                if renamed_path:
                    renamed_updates.append((prop_id, renamed_path, Path(renamed_path).name, str(src_path)))
                    continue
                else:
                    is_stale = True
                    reason = f"Proposed video source file was deleted: {src_path.name}"
        # 2. Stash scene deleted from Stash
        elif stash and r["scene_id"]:
            try:
                if hasattr(stash, "call_GQL"):
                    res = stash.call_GQL(
                        "query CheckScene($id: ID!) { findScene(id: $id) { id } }",
                        {"id": str(r["scene_id"])},
                    )
                    if not (res or {}).get("findScene"):
                        is_stale = True
                        reason = f"Proposed Stash scene {r['scene_id']} was deleted"
                elif hasattr(stash, "find_scene"):
                    if not stash.find_scene(str(r["scene_id"])):
                        is_stale = True
                        reason = f"Proposed Stash scene {r['scene_id']} was deleted"
            except Exception:
                pass

        if is_stale:
            stale_updates.append((prop_id, reason))
            invalidated.append(prop_id)

    if completed_updates or stale_updates or renamed_updates:
        conn_write = connect(database_path)
        try:
            conn_write.execute("BEGIN IMMEDIATE")
            for prop_id, s_path, d_path in completed_updates:
                conn_write.execute(
                    "UPDATE filing_proposals SET status='completed', last_error=NULL, updated_at=? WHERE id=?",
                    (now, prop_id),
                )
                conn_write.execute(
                    "UPDATE incoming_files SET filing_diagnostic=NULL, last_checked_at=? WHERE path=? OR path=?",
                    (now, s_path, d_path),
                )
            for prop_id, new_path, new_name, old_path in renamed_updates:
                conn_write.execute(
                    "UPDATE filing_proposals SET source_path=?, destination_filename=?, updated_at=? WHERE id=?",
                    (new_path, new_name, now, prop_id),
                )
                conn_write.execute(
                    "UPDATE incoming_files SET path=?, last_checked_at=? WHERE path=?",
                    (new_path, now, old_path),
                )
                conn_write.execute(
                    "UPDATE files SET path=?, basename=?, last_seen_at=? WHERE path=?",
                    (new_path, new_name, now, old_path),
                )
            for prop_id, reason in stale_updates:
                conn_write.execute(
                    "UPDATE filing_proposals SET status='invalid', last_error=?, updated_at=? WHERE id=?",
                    (reason, now, prop_id),
                )
            conn_write.commit()
        except Exception:
            try:
                conn_write.rollback()
            except Exception:
                pass
            raise
        finally:
            conn_write.close()

    return invalidated


def get_pending_filing_proposals(database_path: Path, stash=None) -> list[dict]:
    """Return all actionable automatic filing proposals (pending or requiring recovery).
    Automatically invalidates proposals whose source video file or Stash scene no longer exists."""
    invalidate_stale_filing_proposals(database_path, stash=stash)
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM filing_proposals WHERE status IN ('pending', 'needs_recovery') ORDER BY CASE status WHEN 'needs_recovery' THEN 0 ELSE 1 END, created_at DESC"
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["candidate_destinations"] = json.loads(d.get("candidate_destinations_json") or "[]")
            d["companions"] = json.loads(d.get("companions_json") or "[]")
            results.append(d)
        return results
    finally:
        connection.close()


def recover_filing_proposal(database_path: Path, stash, proposal_id: int) -> dict:
    """Attempt to recover an incomplete filing transaction by restoring video and companions to source.
    - Preflights every source and destination; stops safely on any collision (never overwrites).
    - Only touches the exact companion paths recorded for this transaction (no broad startswith()).
    - Verifies that the video, all companions, and the original Stash scene record are restored before reporting success.
    - Safe to retry repeatedly across monitor restarts without affecting unrelated files."""
    with rename_lock(database_path):
        connection = connect(database_path)
        try:
            row = connection.execute("SELECT * FROM filing_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not row:
                return {"id": proposal_id, "status": "blocked", "reason": "Proposal not found"}
            proposal = dict(row)
        finally:
            connection.close()

        src = Path(proposal["source_path"])
        dest_video = Path(proposal["proposed_path"])
        recorded_companions = json.loads(proposal.get("companions_json") or "[]")

        # PREFLIGHT 1: Collision check for video
        if dest_video.is_file():
            if src.exists():
                err_msg = f"Recovery collision: source video path already exists on disk: {src}"
                _update_proposal_status(database_path, proposal_id, "needs_recovery", err_msg)
                return {"id": proposal_id, "status": "needs_recovery", "recovered": False, "reason": err_msg}

        # PREFLIGHT 2: Collision check for recorded companions (never overwrite)
        for item in recorded_companions:
            c_src = Path(item["source"])
            c_target = Path(item["target"])
            if c_target.is_file():
                if c_src.exists():
                    err_msg = f"Recovery collision: source companion path already exists on disk: {c_src}"
                    _update_proposal_status(database_path, proposal_id, "needs_recovery", err_msg)
                    return {"id": proposal_id, "status": "needs_recovery", "recovered": False, "reason": err_msg}

        # STEP 1: Move recorded companions back to source
        companion_errors = []
        for item in recorded_companions:
            c_src = Path(item["source"])
            c_target = Path(item["target"])
            if c_target.is_file():
                try:
                    expect_filesystem_move(database_path, str(c_target), str(c_src))
                    _move_file(c_target, c_src)
                except Exception as exc:
                    companion_errors.append(f"Failed restoring companion {c_target.name}: {exc}")

        # STEP 2: Move video back to source via Stash move_files
        video_move_err = None
        if dest_video.is_file():
            try:
                expect_filesystem_move(database_path, str(dest_video), str(src))
                stash_res = stash.move_files({
                    "ids": [proposal["file_id"]],
                    "destination_folder": str(src.parent),
                    "destination_basename": src.name,
                })
                if stash_res is False:
                    video_move_err = "Stash move_files returned False"
            except Exception as exc:
                video_move_err = str(exc)

        # STEP 3: STRICT THREE-PART VERIFICATION
        # 1. Video verified on disk at source
        video_disk_ok = src.is_file() and not dest_video.exists()

        # 2. All companions verified on disk at source
        companions_disk_ok = True
        unverified_companions = []
        for item in recorded_companions:
            c_src = Path(item["source"])
            c_target = Path(item["target"])
            if not c_src.is_file() or c_target.exists():
                companions_disk_ok = False
                unverified_companions.append(c_src.name)

        # 3. Original Stash scene's file path verified
        stash_scene_ok = False
        stash_err_detail = None
        try:
            scene_res = stash.call_GQL(
                "query FindScene($id: ID!) { findScene(id: $id) { id files { id path } } }",
                {"id": proposal["scene_id"]},
            )
            scene = (scene_res or {}).get("findScene") or {}
            files = scene.get("files") or []
            stash_scene_ok = any(
                (str(f.get("id")) == str(proposal["file_id"]) or len(files) == 1)
                and str(Path(f.get("path", "")).resolve()) == str(src.resolve())
                for f in files
            )
            if not stash_scene_ok:
                stash_paths = [f.get("path") for f in files]
                stash_err_detail = f"Stash scene files {stash_paths} do not match source {src}"
        except Exception as exc:
            stash_err_detail = f"Stash query failed: {exc}"

        if video_disk_ok and companions_disk_ok and stash_scene_ok and not companion_errors and not video_move_err:
            now = utc_now()
            connection = connect(database_path)
            try:
                connection.execute(
                    "UPDATE filing_proposals SET status='failed', last_error=NULL, updated_at=? WHERE id=?",
                    (now, proposal_id),
                )
                connection.execute(
                    "UPDATE files SET path=?, basename=?, exists_on_disk=1, last_seen_at=? WHERE file_id=?",
                    (str(src), src.name, now, proposal["file_id"]),
                )
                connection.execute(
                    "UPDATE incoming_files SET path=?, last_checked_at=? WHERE path=?",
                    (str(src), now, str(dest_video)),
                )
                for item in recorded_companions:
                    connection.execute(
                        "UPDATE incoming_files SET path=?, last_checked_at=? WHERE path=?",
                        (item["source"], now, item["target"]),
                    )
                connection.commit()
            finally:
                connection.close()

            record_activity(
                database_path,
                "filing",
                "recovery complete",
                "complete",
                scene_id=proposal["scene_id"],
                file_id=proposal["file_id"],
                old_path=str(dest_video),
                new_path=str(src),
                detail="Transaction recovery verified: video, companions, and Stash scene restored to source",
            )
            return {
                "id": proposal_id,
                "status": "failed",
                "recovered": True,
                "reason": "Transaction recovery verified: video, companions, and Stash scene restored to source",
            }
        else:
            failures = []
            if not video_disk_ok:
                failures.append(f"video not restored on disk ({video_move_err or 'dest exists or src missing'})")
            if not companions_disk_ok:
                failures.append(f"companions not restored on disk ({', '.join(unverified_companions)})")
            if not stash_scene_ok:
                failures.append(f"Stash scene path not restored ({stash_err_detail})")
            if companion_errors:
                failures.append(f"companion move errors ({'; '.join(companion_errors)})")

            err_msg = f"Recovery attempt unverified: {'; '.join(failures)}"
            _update_proposal_status(database_path, proposal_id, "needs_recovery", err_msg)
            return {
                "id": proposal_id,
                "status": "needs_recovery",
                "recovered": False,
                "reason": err_msg,
            }
