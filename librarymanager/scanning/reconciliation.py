"""Filesystem scanning and missing media reconciliation subsystem for Watchtower.

This module is responsible for:
- Detecting and reconnecting moved scene files across monitored library roots
- Scanning original directories for rename candidates (read-only missing-file reconciliation)
- Pruning dead records from WatchTower database and cached diagnostic reports after Stash cleanup
- Calculating OpenSubtitles hashes and comparing scene fingerprints
- Operating with short, isolated SQLite transactions decoupled from filesystem and network I/O
"""
from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import sqlite3
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from librarymanager.database import create_connection, ensure_schema
from librarymanager.integrations.stash_graphql import check_scenes_batch

logger = logging.getLogger("librarymanager.scanning.reconciliation")

VIDEO_EXTENSIONS: Set[str] = {
    ".3gp", ".asf", ".avi", ".divx", ".flv", ".m2ts", ".m4v", ".mkv",
    ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ogm", ".ogv", ".rm",
    ".rmvb", ".ts", ".vob", ".webm", ".wmv",
}

TEMPORARY_DOWNLOAD_EXTENSIONS: Set[str] = {
    ".part", ".partial", ".crdownload", ".download", ".tmp", ".temp", ".!qb", ".mega", ".aria2",
}


def _get_plugin_root() -> Path:
    """Return the root directory of the Watchtower plugin."""
    return Path(__file__).resolve().parent.parent.parent


def utc_now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(database_path: Path | str) -> sqlite3.Connection:
    """Create a configured SQLite connection and ensure schema is applied."""
    conn = create_connection(database_path)
    ensure_schema(conn, database_path)
    return conn


def _is_subpath_of(path: Path | str, parent: Path | str) -> bool:
    """Return True if path is equal to or inside parent directory without touching the disk."""
    try:
        p_str = os.path.normcase(os.path.abspath(str(path)))
        parent_str = os.path.normcase(os.path.abspath(str(parent)))
        if p_str == parent_str:
            return True
        if not parent_str.endswith(os.sep):
            parent_str += os.sep
        return p_str.startswith(parent_str)
    except Exception:
        return False


def is_temporary_download(path: Path | str) -> bool:
    """Return True if the file path represents a temporary or partial browser/downloader file."""
    if not path:
        return False
    try:
        p = Path(path)
        name = p.name.lower()
        suffix = p.suffix.lower()
        if suffix in TEMPORARY_DOWNLOAD_EXTENSIONS:
            return True
        if name.startswith(".com.google.chrome.") or name.startswith("com.google.chrome."):
            return True
        if name.startswith("unconfirmed ") and (name.endswith(".crdownload") or ".crdownload" in name):
            return True
        if name.endswith(".crdownload"):
            return True
        for ext in TEMPORARY_DOWNLOAD_EXTENSIONS:
            if name.endswith(ext):
                return True
    except Exception:
        pass
    return False


def parse_cold_storage_roots(raw: Any) -> List[str]:
    """Parse configured cold storage root paths from JSON list, Python list, comma-separated, or newline-separated string."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(p).strip() for p in raw if str(p).strip()]
    if isinstance(raw, str):
        trimmed = raw.strip()
        if not trimmed:
            return []
        if trimmed.startswith("["):
            try:
                parsed = json.loads(trimmed)
                if isinstance(parsed, list):
                    return [str(p).strip() for p in parsed if str(p).strip()]
            except (ValueError, json.JSONDecodeError):
                pass
        items = []
        for line in trimmed.replace("\r", "\n").replace(",", "\n").split("\n"):
            item = line.strip()
            if item:
                items.append(item)
        return items
    return []


def fingerprint_value(fingerprints_json: str | None, fingerprint_type: str) -> Optional[str]:
    """Extract a specific fingerprint value from fingerprints JSON string."""
    try:
        for fingerprint in json.loads(fingerprints_json or "[]"):
            if str(fingerprint.get("type", "")).lower() == fingerprint_type.lower():
                return str(fingerprint.get("value", "")).lower() or None
    except (TypeError, ValueError):
        return None
    return None


def opensubtitles_hash(path: Path) -> Optional[str]:
    """Calculate the OpenSubtitles hash without reading the complete video."""
    chunk_size = 65536
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size < chunk_size * 2:
        return None
    checksum = size
    try:
        with path.open("rb") as video:
            first = video.read(chunk_size)
            video.seek(-chunk_size, os.SEEK_END)
            last = video.read(chunk_size)
        for chunk in (first, last):
            usable = len(chunk) - (len(chunk) % 8)
            if usable:
                checksum += sum(struct.unpack(f"<{usable // 8}Q", chunk[:usable]))
        return f"{checksum & 0xFFFFFFFFFFFFFFFF:016x}"
    except OSError:
        return None


_ORIGINAL_OPENSUBTITLES_HASH = opensubtitles_hash


def get_opensubtitles_hash_fn():
    """Return the active opensubtitles_hash function, prioritizing any monkeypatch
    applied to either librarymanager.scanning.reconciliation or librarymanager_core.
    """
    canon = sys.modules.get(__name__, None)
    canon_fn = getattr(canon, "opensubtitles_hash", None) if canon else opensubtitles_hash
    if canon_fn is not None and canon_fn is not _ORIGINAL_OPENSUBTITLES_HASH:
        return canon_fn

    core_mod = sys.modules.get("librarymanager_core")
    if core_mod is not None:
        core_hash = getattr(core_mod, "opensubtitles_hash", None)
        if core_hash is not None and not getattr(core_hash, "_is_watchtower_wrapper", False):
            return core_hash

    return canon_fn or _ORIGINAL_OPENSUBTITLES_HASH


def _prune_cached_reports(target_fids_set: Set[str], root_dir: Optional[Path] = None) -> None:
    """Safely remove entries matching target_fids_set from reconciliation-report.json and resolution-plan.json.
    Preserves all unrelated records, reports, and summary metrics."""
    if not target_fids_set:
        return
    base_dir = root_dir or _get_plugin_root()
    for r_name, list_key in [("reconciliation-report.json", "matches"), ("resolution-plan.json", "items")]:
        r_path = base_dir / r_name
        if not r_path.is_file():
            continue
        try:
            content = json.loads(r_path.read_text(encoding="utf-8"))
            rows_list = content.get(list_key, [])
            if not isinstance(rows_list, list):
                continue
            filtered = [
                row for row in rows_list
                if str(row.get("file_id") or row.get("stale_file_id") or "") not in target_fids_set
            ]
            if len(filtered) != len(rows_list):
                content[list_key] = filtered
                if isinstance(content.get("summary"), dict) and "missing" in content["summary"]:
                    content["summary"]["missing"] = len(filtered)
                if "safe_redundant" in content:
                    content["safe_redundant"] = sum(item.get("recommendation") == "stale_record_redundant" for item in filtered)
                if "merge_required" in content:
                    content["merge_required"] = sum(item.get("recommendation") == "merge_metadata_first" for item in filtered)
                r_path.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def clean_deleted_scenes_from_stash(database_path: Path, stash: Any = None, root_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Safely prune dead records from Watchtower's files table after Stash clean or file deletion.
    - Removes files where exists_on_disk = 0 and the scene/file no longer exists in Stash or disk.
    - Also verifies files on disk; if a missing file was restored/reconnected, updates exists_on_disk = 1.
    - Records an activity audit event in the live feed.
    """
    pruned_count = 0
    restored_count = 0

    conn_read = connect(database_path)
    try:
        rows = conn_read.execute(
            "SELECT file_id, scene_id, path, basename FROM files WHERE exists_on_disk = 0"
        ).fetchall()
        rows = [dict(r) for r in rows]
    finally:
        conn_read.close()

    if not rows:
        return {
            "cleaned_count": 0,
            "restored_count": 0,
            "remaining_missing": 0,
            "message": "No missing files found to clean."
        }

    active_missing = []
    restored_fids = []
    for r in rows:
        p = r["path"]
        exists = False
        try:
            exists = os.path.isfile(p)
        except OSError:
            exists = False
        if exists:
            restored_fids.append(r["file_id"])
        else:
            active_missing.append(r)

    restored_count = len(restored_fids)
    if restored_fids:
        conn_restore = connect(database_path)
        try:
            conn_restore.execute("BEGIN IMMEDIATE")
            for fid in restored_fids:
                conn_restore.execute(
                    "UPDATE files SET exists_on_disk = 1, missing_since = NULL WHERE file_id = ?",
                    (fid,),
                )
            conn_restore.commit()
        except Exception:
            try:
                conn_restore.rollback()
            except Exception:
                pass
            raise
        finally:
            conn_restore.close()

    active_stash_scenes = set()
    active_stash_files = set()
    checked_stash = False
    if stash and active_missing:
        scene_ids = list({str(r["scene_id"]) for r in active_missing if r["scene_id"]})
        active_stash_scenes, active_stash_files, checked_stash = check_scenes_batch(stash, scene_ids)

    dead_files = []
    for r in active_missing:
        fid = str(r["file_id"])
        sid = str(r["scene_id"])
        if checked_stash:
            if sid not in active_stash_scenes or (fid and fid not in active_stash_files):
                dead_files.append(fid)
        elif stash is None:
            dead_files.append(fid)

    if dead_files:
        dead_paths = [r["path"] for r in active_missing if str(r["file_id"]) in dead_files and r["path"]]
        conn_prune = connect(database_path)
        try:
            conn_prune.execute("BEGIN IMMEDIATE")
            for fid in dead_files:
                conn_prune.execute("DELETE FROM filename_state WHERE file_id = ?", (fid,))
                conn_prune.execute("DELETE FROM filename_previews WHERE file_id = ?", (fid,))
                conn_prune.execute("DELETE FROM files WHERE file_id = ?", (fid,))
            for dp in dead_paths:
                conn_prune.execute(
                    "UPDATE filesystem_events SET status='reviewed' WHERE (source_path=? OR destination_path=?) AND status='pending'",
                    (dp, dp),
                )
            if dead_paths:
                conn_prune.executemany(
                    "DELETE FROM unindexed_external_media WHERE path = ?",
                    [(dp,) for dp in dead_paths],
                )
            conn_prune.commit()
            pruned_count = len(dead_files)
        except Exception:
            try:
                conn_prune.rollback()
            except Exception:
                pass
            raise
        finally:
            conn_prune.close()

        # Prune dead records from cached diagnostic report files so UI clears cleanly
        _prune_cached_reports({str(fid) for fid in dead_files}, root_dir=root_dir)

    conn_count = connect(database_path)
    try:
        remaining_missing = conn_count.execute(
            "SELECT COUNT(*) AS c FROM files WHERE exists_on_disk = 0"
        ).fetchone()["c"]
    finally:
        conn_count.close()

    if pruned_count > 0:
        try:
            from librarymanager_core import record_activity
            record_activity(
                database_path, "clean", "clean deleted from stash", "complete",
                severity="info",
                detail=f"Cleaned Stash database: pruned {pruned_count} deleted scene record(s) from Watchtower."
            )
        except Exception as act_err:
            logger.debug("Failed recording clean activity: %s", act_err)

    return {
        "cleaned_count": pruned_count,
        "restored_count": restored_count,
        "remaining_missing": remaining_missing,
        "message": f"Pruned {pruned_count} dead record(s). {remaining_missing} missing file(s) remain."
    }


def reconcile_missing_files(database_path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Find rename candidates in the missing file's original directory; never mutate files."""
    now = utc_now()

    # Step 1: Open an initial short write transaction to record the run and read missing records
    conn_init = connect(database_path)
    try:
        conn_init.execute("BEGIN IMMEDIATE")
        run_id = conn_init.execute(
            "INSERT INTO reconciliation_runs(started_at, status) VALUES (?, 'running')", (now,)
        ).lastrowid
        missing = conn_init.execute(
            "SELECT file_id,scene_id,path,size,fingerprints_json FROM files WHERE exists_on_disk=0 ORDER BY path"
        ).fetchall()
        missing = [dict(r) for r in missing]
        known_paths = {
            os.path.normcase(os.path.abspath(row["path"])): dict(row)
            for row in conn_init.execute(
                "SELECT file_id,scene_id,path FROM files WHERE exists_on_disk=1"
            )
        }
        conn_init.commit()
    except Exception:
        try:
            conn_init.rollback()
        except Exception:
            pass
        raise
    finally:
        conn_init.close()

    summary = {"missing": len(missing), "matched": 0, "ambiguous": 0, "unmatched": 0, "skipped_folders": 0}
    report = []
    candidates_to_insert = []

    # Step 2: Perform directory iteration and hash calculations purely in memory without an open DB transaction
    try:
        for record in missing:
            expected = Path(record["path"])
            folder = expected.parent
            result = {
                "file_id": record["file_id"], "scene_id": record["scene_id"],
                "expected_path": str(expected), "candidate_path": None,
                "confidence": "none", "reason": "No candidate found",
                "expected_size": record["size"], "candidate_size": None, "oshash_match": None,
            }
            is_dir = False
            try:
                is_dir = folder.is_dir()
            except OSError:
                is_dir = False
            if not is_dir:
                result.update(confidence="skipped", reason="Original folder is unavailable")
                summary["skipped_folders"] += 1
            elif record["size"] is None:
                result["reason"] = "Stash has no recorded file size"
                summary["unmatched"] += 1
            else:
                size_matches = []
                read_error = None
                try:
                    entries = list(folder.iterdir())
                except OSError as iter_err:
                    if getattr(iter_err, "errno", None) != errno.ENOENT and not isinstance(iter_err, FileNotFoundError):
                        read_error = iter_err
                    entries = []
                if read_error is not None:
                    summary["read_errors"] = summary.get("read_errors", 0) + 1
                    result.update(confidence="error", reason=f"Failed to read original folder: {read_error}")
                    candidates_to_insert.append((
                        run_id, result["file_id"], result["scene_id"], result["expected_path"],
                        result["candidate_path"], result["confidence"], result["reason"],
                        result["expected_size"], result["candidate_size"], result["oshash_match"], now
                    ))
                    report.append(result)
                    continue
                for candidate in entries:
                    try:
                        normalized = os.path.normcase(os.path.abspath(candidate))
                        if (not candidate.is_file() or candidate.suffix.lower() not in VIDEO_EXTENSIONS
                                or candidate.stat().st_size != record["size"]):
                            continue
                        size_matches.append(candidate)
                    except OSError:
                        continue
                _hash_fn = get_opensubtitles_hash_fn()
                expected_hash = fingerprint_value(record["fingerprints_json"], "oshash")
                hash_matches = []
                if expected_hash:
                    for candidate in size_matches:
                        try:
                            cand_h = _hash_fn(candidate)
                            if cand_h and cand_h.lower() == expected_hash.lower():
                                hash_matches.append(candidate)
                        except OSError:
                            continue
                if len(hash_matches) == 1:
                    candidate = hash_matches[0]
                    tracked = known_paths.get(os.path.normcase(os.path.abspath(candidate)))
                    if tracked:
                        result.update(candidate_path=str(candidate), confidence="conflict",
                                      reason=(f"Same size and oshash, but Stash already tracks this path as "
                                              f"file {tracked['file_id']} on scene {tracked['scene_id']}"),
                                      candidate_size=candidate.stat().st_size, oshash_match=1)
                        summary["ambiguous"] += 1
                    else:
                        result.update(candidate_path=str(candidate), confidence="verified",
                                      reason="Same size and matching oshash", candidate_size=candidate.stat().st_size,
                                      oshash_match=1)
                        summary["matched"] += 1
                elif len(hash_matches) > 1 or len(size_matches) > 1:
                    result.update(confidence="ambiguous", reason=f"{len(hash_matches) or len(size_matches)} possible files have matching evidence")
                    summary["ambiguous"] += 1
                elif len(size_matches) == 1:
                    candidate = size_matches[0]
                    result.update(candidate_path=str(candidate), confidence="probable",
                                  reason="Unique same-folder file with identical size; oshash unavailable or different",
                                  candidate_size=candidate.stat().st_size, oshash_match=0 if expected_hash else None)
                    summary["matched"] += 1
                else:
                    summary["unmatched"] += 1

            candidates_to_insert.append((
                run_id, result["file_id"], result["scene_id"], result["expected_path"],
                result["candidate_path"], result["confidence"], result["reason"],
                result["expected_size"], result["candidate_size"], result["oshash_match"], now
            ))
            report.append(result)
    except Exception:
        # Mark reconciliation run as failed
        try:
            conn_fail = connect(database_path)
            try:
                conn_fail.execute("BEGIN IMMEDIATE")
                conn_fail.execute(
                    "UPDATE reconciliation_runs SET completed_at=?, status='failed' WHERE id=?",
                    (utc_now(), run_id),
                )
                conn_fail.commit()
            finally:
                conn_fail.close()
        except Exception:
            pass
        raise

    # Step 3: Persist all candidates and mark the run complete in a short atomic transaction
    conn_finish = connect(database_path)
    try:
        conn_finish.execute("BEGIN IMMEDIATE")
        if candidates_to_insert:
            conn_finish.executemany(
                """INSERT INTO reconciliation_candidates(run_id,file_id,scene_id,expected_path,candidate_path,
                       confidence,reason,expected_size,candidate_size,oshash_match,recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                candidates_to_insert,
            )
        run_status = "completed_with_errors" if summary.get("read_errors", 0) > 0 else "complete"
        conn_finish.execute(
            """UPDATE reconciliation_runs SET completed_at=?,missing_count=?,matched_count=?,ambiguous_count=?,
                   unmatched_count=?,skipped_folder_count=?,status=? WHERE id=?""",
            (now, summary["missing"], summary["matched"], summary["ambiguous"], summary["unmatched"],
             summary["skipped_folders"], run_status, run_id),
        )
        conn_finish.commit()
        summary["run_id"] = run_id
        return summary, report
    except Exception:
        try:
            conn_finish.rollback()
        except Exception:
            pass
        try:
            conn_fail = connect(database_path)
            try:
                conn_fail.execute("BEGIN IMMEDIATE")
                conn_fail.execute(
                    "UPDATE reconciliation_runs SET completed_at=?, status='failed' WHERE id=?",
                    (utc_now(), run_id),
                )
                conn_fail.commit()
            finally:
                conn_fail.close()
        except Exception:
            pass
        raise
    finally:
        conn_finish.close()


def reconcile_missing_media_across_roots(
    database_path: Path,
    stash: Any,
    roots: Optional[List[str]],
    config: Optional[Dict[str, Any]] = None,
    root_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Find and reconnect moved scene media across monitored library roots without full Stash scans.
    Designed for files moved via phones, tablets, or external file managers where filesystem
    events were bypassed.
    """
    from librarymanager_core import (
        add_all_unindexed_media_files,
        associated_file_target,
        expect_filesystem_move,
        get_authoritative_unavailable_roots,
        get_companion_files,
        record_activity,
    )

    if config is None and stash:
        try:
            config = stash.find_plugin_config("librarymanager") or {}
        except Exception:
            config = {}
    elif config is None:
        config = {}

    unavailable_roots = []
    if roots is not None:
        accessible_roots = []
        for r in roots:
            r_str = str(r)
            if os.path.isdir(r_str):
                accessible_roots.append(r_str)
            else:
                unavailable_roots.append(r_str)
        if not accessible_roots:
            msg = "Selected library drive(s) are currently offline or unavailable." if roots else "No library roots selected."
            if unavailable_roots:
                record_activity(
                    database_path, "reconciliation", "reconcile missing media", "failed",
                    severity="warning",
                    detail=f"Selected library drive(s) are currently offline or unavailable: {', '.join(unavailable_roots)}",
                    metadata={"unavailable_roots": unavailable_roots}
                )
            return {
                "status": "warning",
                "missing_checked": 0,
                "reconciled_count": 0,
                "reconciled": [],
                "unmatched": [],
                "unindexed_found_count": 0,
                "auto_ingested_count": 0,
                "unavailable_roots": unavailable_roots,
                "errors": [{"path": r, "error": "Drive offline or unavailable", "errno": None} for r in unavailable_roots],
                "error_count": len(unavailable_roots),
                "message": msg,
            }
    else:
        cold_storage_roots = set(parse_cold_storage_roots(config.get("coldStorageRoots")))
        unavailable = set(get_authoritative_unavailable_roots(database_path)) | cold_storage_roots
        all_roots = []
        if stash:
            try:
                res = stash.call_GQL("query { configuration { general { stashes { path } } } }")
                all_roots = [s["path"] for s in (((res or {}).get("configuration") or {}).get("general") or {}).get("stashes") or [] if s.get("path")]
            except Exception:
                all_roots = []
        accessible_roots = [str(r) for r in all_roots if r not in unavailable and os.path.isdir(str(r))]

    now = utc_now()
    connection = connect(database_path)
    missing_candidates = []
    try:
        rows = connection.execute(
            "SELECT file_id, scene_id, path, basename, size, fingerprints_json, exists_on_disk FROM files"
        ).fetchall()
        for r in rows:
            p = r["path"]
            if not any(_is_subpath_of(p, root) for root in accessible_roots):
                continue
            if r["exists_on_disk"]:
                exists = False
                try:
                    exists = os.path.isfile(p)
                except OSError:
                    exists = False
                if not exists:
                    connection.execute(
                        "UPDATE files SET exists_on_disk=0, missing_since=? WHERE file_id=?",
                        (now, r["file_id"]),
                    )
                    missing_candidates.append(dict(r))
            else:
                missing_candidates.append(dict(r))
        connection.commit()
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    finally:
        connection.close()

    if missing_candidates and stash:
        try:
            clean_res = clean_deleted_scenes_from_stash(database_path, stash, root_dir=root_dir)
            if clean_res.get("cleaned_count", 0) > 0:
                conn_refresh = connect(database_path)
                try:
                    remaining_ids = {
                        str(r["file_id"]) for r in conn_refresh.execute(
                            "SELECT file_id FROM files WHERE exists_on_disk = 0"
                        ).fetchall()
                    }
                    missing_candidates = [c for c in missing_candidates if str(c.get("file_id")) in remaining_ids]
                finally:
                    conn_refresh.close()
        except Exception as clean_err:
            logger.debug("Failed checking deleted scenes during reconciliation: %s", clean_err)

    if not missing_candidates and stash:
        try:
            result = stash.call_GQL(
                """query FindMissingScenes {
                     findScenes(filter: {per_page: 500}) {
                       scenes {
                         id
                         title
                         files { id path basename size fingerprints { type value } }
                       }
                     }
                   }"""
            )
            scenes = ((result or {}).get("findScenes") or {}).get("scenes") or []
            for sc in scenes:
                sc_id = str(sc.get("id"))
                for f in sc.get("files") or []:
                    p = f.get("path")
                    if not p:
                        continue
                    if not any(_is_subpath_of(p, root) for root in accessible_roots):
                        continue
                    try:
                        if not os.path.isfile(p):
                            missing_candidates.append({
                                "file_id": str(f.get("id") or ""),
                                "scene_id": sc_id,
                                "path": str(p),
                                "basename": f.get("basename") or Path(p).name,
                                "size": f.get("size"),
                                "fingerprints_json": json.dumps(f.get("fingerprints") or []),
                            })
                    except OSError:
                        pass
        except Exception as gql_err:
            logger.debug("Failed querying Stash scenes for missing files: %s", gql_err)

    known_paths = set()
    ignored_paths = set()
    conn_kp = connect(database_path)
    try:
        for r in conn_kp.execute("SELECT path FROM files").fetchall():
            known_paths.add(os.path.normcase(os.path.abspath(r["path"])))
        for r in conn_kp.execute("SELECT path FROM unindexed_external_media WHERE ignored = 1").fetchall():
            ignored_paths.add(os.path.normcase(os.path.abspath(r["path"])))
    finally:
        conn_kp.close()

    if stash:
        try:
            res = stash.call_GQL("query { findFiles(filter: {per_page: -1}) { files { path } } }")
            if isinstance(res, dict):
                for f in ((res.get("findFiles") or {}).get("files") or []):
                    p = f.get("path")
                    if p:
                        known_paths.add(os.path.normcase(os.path.abspath(p)))
        except Exception as gql_err:
            logger.debug("Failed querying findFiles from Stash: %s", gql_err)

    record_activity(
        database_path, "reconciliation", "reconcile missing media", "starting",
        severity="info",
        detail=f"Searching library roots for {len(missing_candidates)} missing scene file(s) and unindexed media..."
    )

    target_map = {}
    for item in missing_candidates:
        bn = (item.get("basename") or Path(item.get("path", "")).name).lower()
        if bn:
            target_map.setdefault(bn, []).append(item)

    candidates_found = []
    unindexed_found = []
    visited_dirs = set()
    walk_errors = []

    def _on_walk_error(err: OSError):
        walk_errors.append({
            "path": getattr(err, "filename", str(err)),
            "error": str(err),
            "errno": getattr(err, "errno", None),
        })
        logger.warning("Directory traversal error during reconciliation on %s: %s", getattr(err, "filename", "path"), err)

    for root in accessible_roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=_on_walk_error):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".")
                and d not in ("$RECYCLE.BIN", "System Volume Information", "@eaDir", "#recycle", ".Trash")
            ]
            norm_dir = os.path.normcase(os.path.abspath(dirpath))
            if norm_dir in visited_dirs:
                continue
            visited_dirs.add(norm_dir)

            for fn in filenames:
                cand_path = os.path.join(dirpath, fn)
                norm_cand = os.path.normcase(os.path.abspath(cand_path))
                fn_lower = fn.lower()

                # Check if this resolves a missing scene file
                if fn_lower in target_map:
                    for item in target_map[fn_lower]:
                        norm_old = os.path.normcase(os.path.abspath(item["path"]))
                        if norm_cand == norm_old:
                            continue
                        try:
                            st = os.stat(cand_path)
                            cand_size = st.st_size
                        except OSError as stat_err:
                            if getattr(stat_err, "errno", None) != errno.ENOENT and not isinstance(stat_err, FileNotFoundError):
                                walk_errors.append({
                                    "path": cand_path,
                                    "error": str(stat_err),
                                    "errno": getattr(stat_err, "errno", None),
                                })
                                logger.warning("Failed to stat candidate %s: %s", cand_path, stat_err)
                            continue

                        expected_size = item.get("size")
                        if expected_size is not None and cand_size != expected_size:
                            continue

                        _hash_fn = get_opensubtitles_hash_fn()
                        expected_oshash = fingerprint_value(item.get("fingerprints_json"), "oshash")
                        oshash_verified = False
                        if expected_oshash:
                            try:
                                cand_hash = _hash_fn(Path(cand_path))
                                if cand_hash and cand_hash.lower() == expected_oshash.lower():
                                    oshash_verified = True
                                else:
                                    continue
                            except Exception:
                                continue
                        else:
                            oshash_verified = True

                        candidates_found.append({
                            "item": item,
                            "candidate_path": cand_path,
                            "oshash_verified": oshash_verified,
                        })

                # Check if this is unindexed new video on disk
                if Path(fn).suffix.lower() in VIDEO_EXTENSIONS:
                    if norm_cand not in known_paths and norm_cand not in ignored_paths:
                        if not is_temporary_download(cand_path):
                            try:
                                st = os.stat(cand_path)
                                if st.st_size > 0:
                                    unindexed_found.append((cand_path, fn, st.st_size, now))
                            except OSError as stat_err:
                                if getattr(stat_err, "errno", None) != errno.ENOENT and not isinstance(stat_err, FileNotFoundError):
                                    walk_errors.append({
                                        "path": cand_path,
                                        "error": str(stat_err),
                                        "errno": getattr(stat_err, "errno", None),
                                    })
                                    logger.warning("Failed to stat unindexed file %s: %s", cand_path, stat_err)
                                pass

    # Persist newly discovered unindexed media
    if unindexed_found:
        conn_unindex = connect(database_path)
        try:
            for u_path, u_name, u_size, u_time in unindexed_found:
                conn_unindex.execute(
                    """INSERT INTO unindexed_external_media (path, basename, size, discovered_at, ignored)
                       VALUES (?, ?, ?, ?, 0)
                       ON CONFLICT(path) DO UPDATE SET size=excluded.size
                       WHERE ignored = 0""",
                    (u_path, u_name, u_size, u_time),
                )
            conn_unindex.commit()
        finally:
            conn_unindex.close()

    conn_clean = connect(database_path)
    try:
        conn_clean.execute(
            "DELETE FROM unindexed_external_media WHERE path IN (SELECT path FROM files)"
        )
        if known_paths:
            all_unindexed = conn_clean.execute("SELECT path FROM unindexed_external_media").fetchall()
            to_remove = [
                u["path"] for u in all_unindexed
                if os.path.normcase(os.path.abspath(u["path"])) in known_paths
            ]
            if to_remove:
                conn_clean.executemany("DELETE FROM unindexed_external_media WHERE path = ?", [(tp,) for tp in to_remove])
        conn_clean.commit()
    finally:
        conn_clean.close()

    auto_ingested_count = 0
    if unindexed_found and (config.get("autoIngestUnindexedMedia") is True or config.get("auto_ingest_unindexed_media") is True) and stash:
        try:
            ingest_res = add_all_unindexed_media_files(database_path, stash, config=config, roots=accessible_roots)
            auto_ingested_count = ingest_res.get("count", 0)
        except Exception as auto_ingest_err:
            logger.warning("Automatic ingestion of discovered unindexed media failed: %s", auto_ingest_err)

    reconciled = []
    unmatched = []
    for item in missing_candidates:
        matches_for_item = [
            c for c in candidates_found if c["item"].get("file_id") == item.get("file_id")
        ]
        if len(matches_for_item) == 1:
            cand_path = matches_for_item[0]["candidate_path"]
            scan_job_id = None
            if stash:
                try:
                    scan_job_id = stash.metadata_scan(paths=[cand_path])
                    stash.wait_for_job(scan_job_id, timeout=60)
                except Exception as scan_err:
                    logger.warning("Targeted scan failed for %s: %s", cand_path, scan_err)

            conn_upd = connect(database_path)
            try:
                conn_upd.execute("BEGIN IMMEDIATE")
                conn_upd.execute(
                    """UPDATE files SET path=?, basename=?, exists_on_disk=1, last_seen_at=?, missing_since=NULL
                       WHERE file_id=?""",
                    (cand_path, Path(cand_path).name, utc_now(), item.get("file_id")),
                )
                conn_upd.execute(
                    "UPDATE filesystem_events SET status='reviewed' WHERE source_path=? OR source_path=?",
                    (item["path"], cand_path),
                )
                conn_upd.commit()
            except Exception:
                try:
                    conn_upd.rollback()
                except Exception:
                    pass
                raise
            finally:
                conn_upd.close()

            # Relocate any companion files left behind in the old folder
            moved_companions = []
            old_p = Path(item["path"])
            new_p = Path(cand_path)
            if (
                any(_is_subpath_of(str(old_p), root) for root in accessible_roots)
                and old_p.parent.is_dir()
                and new_p.parent.is_dir()
                and old_p.parent != new_p.parent
            ):
                for comp_name in get_companion_files(old_p):
                    src_comp = old_p.parent / comp_name
                    tgt = associated_file_target(src_comp, old_p, new_p)
                    if tgt:
                        dst_comp = new_p.parent / tgt.name
                        if src_comp.is_file() and not dst_comp.exists():
                            try:
                                expect_filesystem_move(database_path, str(src_comp), str(dst_comp))
                                from librarymanager.services.filing import _move_file
                                _move_file(src_comp, dst_comp)
                                moved_companions.append(dst_comp.name)
                                record_activity(
                                    database_path, "companion", "moved companion", "recorded",
                                    severity="info",
                                    scene_id=item.get("scene_id"), file_id=item.get("file_id"),
                                    old_path=str(src_comp), new_path=str(dst_comp),
                                    detail=f"Moved companion file alongside {new_p.name}"
                                )
                            except Exception as comp_err:
                                logger.warning("Failed moving companion %s to %s: %s", src_comp, dst_comp, comp_err)

            comp_detail = f" (along with {len(moved_companions)} companion file{'s' if len(moved_companions) != 1 else ''})" if moved_companions else ""
            record_activity(
                database_path, "reconciliation", "missing file reconnected", "updated",
                severity="info", scene_id=item.get("scene_id"), file_id=item.get("file_id"),
                old_path=item["path"], new_path=cand_path,
                detail=f"Reconnected {Path(cand_path).name} in Stash via targeted scan{comp_detail}"
            )
            reconciled.append({
                "scene_id": item.get("scene_id"),
                "file_id": item.get("file_id"),
                "old_path": item["path"],
                "new_path": cand_path,
                "filename": Path(cand_path).name,
                "moved_companions": moved_companions,
            })
        else:
            reason = (
                "Multiple matching files found across library roots; manual review required"
                if len(matches_for_item) > 1
                else "File not found in monitored library roots"
            )
            unmatched.append({
                "scene_id": item.get("scene_id"),
                "file_id": item.get("file_id"),
                "old_path": item["path"],
                "filename": item.get("basename"),
                "reason": reason,
            })

    if reconciled:
        _prune_cached_reports({str(item.get("file_id")) for item in reconciled if item.get("file_id")}, root_dir=root_dir)

    has_errors = bool(unavailable_roots or walk_errors)
    status_str = "completed_with_errors" if has_errors else "complete"
    severity_str = "warning" if has_errors else "info"

    error_summary_parts = []
    if unavailable_roots:
        error_summary_parts.append(
            f"{len(unavailable_roots)} drive(s) unavailable ({', '.join(Path(u).name or u for u in unavailable_roots)})"
        )
    if walk_errors:
        error_summary_parts.append(f"{len(walk_errors)} directory/file error(s)")
    error_summary = f" [Errors: {'; '.join(error_summary_parts)}]" if error_summary_parts else ""

    record_activity(
        database_path, "reconciliation", "reconcile missing media", status_str,
        severity=severity_str,
        detail=f"Reconciliation {status_str.replace('_', ' ')}: {len(reconciled)} reconnected, {len(unmatched)} unresolved, {auto_ingested_count if auto_ingested_count else len(unindexed_found)} {'automatically ingested into Stash' if auto_ingested_count else 'unindexed media discovered'}{error_summary}",
        metadata={
            "unavailable_roots": unavailable_roots,
            "errors": walk_errors[:10],
            "error_count": len(walk_errors) + len(unavailable_roots),
        }
    )

    msg = ""
    if reconciled:
        msg = f"Successfully reconnected {len(reconciled)} of {len(missing_candidates)} missing scene file(s)."
    elif missing_candidates:
        msg = f"Checked {len(missing_candidates)} missing file(s); none were found in monitored library roots."
    else:
        msg = "All tracked scene files verified present on disk."
    if auto_ingested_count > 0:
        msg += f" Automatically ingested {auto_ingested_count} new video(s) into Stash with full previews & contact sheets."
    elif unindexed_found:
        msg += f" Found {len(unindexed_found)} unindexed new video(s) on disk."
    if error_summary_parts:
        msg += f" Note: {'; '.join(error_summary_parts)}."

    return {
        "status": "completed_with_errors" if has_errors else "ok",
        "missing_checked": len(missing_candidates),
        "reconciled_count": len(reconciled),
        "reconciled": reconciled,
        "unmatched": unmatched,
        "unindexed_found_count": len(unindexed_found),
        "auto_ingested_count": auto_ingested_count,
        "unavailable_roots": unavailable_roots,
        "errors": walk_errors[:20],
        "error_count": len(walk_errors) + len(unavailable_roots),
        "message": msg,
    }


__all__ = [
    "_is_subpath_of",
    "_prune_cached_reports",
    "clean_deleted_scenes_from_stash",
    "connect",
    "fingerprint_value",
    "get_opensubtitles_hash_fn",
    "is_temporary_download",
    "opensubtitles_hash",
    "parse_cold_storage_roots",
    "reconcile_missing_files",
    "reconcile_missing_media_across_roots",
    "TEMPORARY_DOWNLOAD_EXTENSIONS",
    "utc_now",
    "VIDEO_EXTENSIONS",
]
