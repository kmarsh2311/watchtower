"""Filesystem probes, companion file management, cold storage detection,
and checksum queue engine for Watchtower.

This module is responsible for:
- Companion-file identification, matching, and safe relocation targets
- Cold-storage drive availability probes and authoritative unreachable root tracking
- High-resolution filesystem stat snapshots and verified SHA-256 calculation
- Durable checksum job queuing, retries, and cache persistence
- Expected filesystem event tracking (moves, creates, deletes) for external monitor reconciliation
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from librarymanager.database import create_connection, ensure_schema
from librarymanager.scanning.reconciliation import (
    VIDEO_EXTENSIONS,
    TEMPORARY_DOWNLOAD_EXTENSIONS,
    is_temporary_download,
    parse_cold_storage_roots,
)

logger = logging.getLogger("librarymanager.scanning.filesystem")

ASSOCIATED_EXTENSIONS: Set[str] = {
    ".funscript", ".srt", ".vtt", ".scc", ".ttml", ".dfxp", ".lrc", ".txt"
}

IMAGE_SIDECAR_EXTENSIONS: Set[str] = {
    ".jpg", ".jpeg", ".png", ".webp"
}

COMPANION_EXTENSIONS: Set[str] = {
    ".funscript", ".srt", ".vtt", ".scc", ".ttml", ".dfxp", ".lrc", ".txt",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".nfo", ".json", ".xml", ".sub", ".idx",
    ".csm.jpg", ".csm.png", ".csm.webp",
}

SYSTEM_JUNK_FILENAMES: Set[str] = {
    ".ds_store", "thumbs.db", "desktop.ini"
}

ACTIVE_INCOMING_LIFECYCLE_STATUSES: Set[str] = {
    "waiting", "downloading", "scanning", "generating_sheet", "renaming",
}

CHECKSUM_STABILITY_SECONDS: float = 5.0
CHECKSUM_RETRY_BASE_SECONDS: float = 15.0
CHECKSUM_MAX_ACTIVE_JOBS: int = 8


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


def is_actionable_incoming_file(path: Path | str, active_status: str | None = None) -> bool:
    """Return True if path is a completed, settled video or companion file ready for organiser action."""
    if not path or is_temporary_download(path):
        return False
    if active_status and active_status in ACTIVE_INCOMING_LIFECYCLE_STATUSES:
        return False
    try:
        ext = Path(path).suffix.lower()
        return ext in VIDEO_EXTENSIONS or ext in COMPANION_EXTENSIONS
    except Exception:
        return False


def is_verified_companion_destination(connection: sqlite3.Connection, destination_path: str, source_path: str | None = None) -> bool:
    """Check if a moved companion file exists at destination and pairs with an active scene video."""
    dest = Path(destination_path)
    try:
        if not dest.is_file():
            return False
    except OSError:
        return False

    if source_path:
        try:
            if Path(source_path).exists():
                return False
        except OSError:
            pass

    dest_suffix = dest.suffix.lower()
    if dest_suffix not in COMPANION_EXTENSIONS:
        return False

    parent_dir = dest.parent

    # Case 1: Compound video name, e.g. video.mp4.jpg -> video.mp4
    stem_path = Path(dest.stem)
    if stem_path.suffix.lower() in VIDEO_EXTENSIONS:
        compound_video = parent_dir / dest.stem
        row = connection.execute(
            "SELECT 1 FROM files WHERE path=? AND exists_on_disk=1",
            (str(compound_video),)
        ).fetchone()
        if row:
            return True

    # Case 2: Same stem name, e.g. video.jpg -> video.mp4, video.mkv, etc.
    matched_videos = []
    for v_ext in VIDEO_EXTENSIONS:
        vid_cand = parent_dir / (dest.stem + v_ext)
        row = connection.execute(
            "SELECT 1 FROM files WHERE path=? AND exists_on_disk=1",
            (str(vid_cand),)
        ).fetchone()
        if row:
            matched_videos.append(str(vid_cand))

    return len(matched_videos) == 1


def _sidecar_match_key(name: str) -> str:
    """Normalize text by stripping combining accents and non-alphanumeric punctuation (brackets, dashes, etc.)."""
    nfkd = unicodedata.normalize("NFKD", str(name or ""))
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", "", no_accents.casefold())


def is_source_companion_of_moved_video(connection: sqlite3.Connection, source_path: str) -> bool:
    """Resolve a companion deletion only from the current verified video reconnection."""
    src = Path(source_path)
    src_suffix = src.suffix.lower()
    is_comp = (
        src_suffix in COMPANION_EXTENSIONS or
        src.name.lower().endswith((".csm.jpg", ".csm.png", ".csm.webp"))
    )
    if not is_comp:
        return False

    companion_event = connection.execute(
        """SELECT first_seen_at FROM filesystem_events
           WHERE event_type='deleted' AND source_path=? AND status='pending'
           ORDER BY last_seen_at DESC LIMIT 1""",
        (str(src),),
    ).fetchone()
    if not companion_event or not companion_event["first_seen_at"]:
        return False

    parent_dir = src.parent
    candidate_video_paths = []
    stem_path = Path(src.stem)
    if stem_path.suffix.lower() in VIDEO_EXTENSIONS:
        candidate_video_paths.append(str(parent_dir / src.stem))
    for v_ext in VIDEO_EXTENSIONS:
        candidate_video_paths.append(str(parent_dir / (src.stem + v_ext)))

    for vid_src in candidate_video_paths:
        act = connection.execute(
            """SELECT file_id,scene_id,old_path,new_path,recorded_at FROM activity_log
               WHERE category='reconciliation' AND action='targeted Stash scan'
                 AND status='updated' AND old_path=? AND new_path IS NOT NULL
                 AND file_id IS NOT NULL AND scene_id IS NOT NULL
                 AND recorded_at>=?
               ORDER BY recorded_at DESC LIMIT 1""",
            (vid_src, companion_event["first_seen_at"])
        ).fetchone()
        new_vid_path = act["new_path"] if act else None

        if new_vid_path:
            dest_vid = Path(new_vid_path)
            try:
                if not dest_vid.is_file():
                    continue
            except OSError:
                continue

            f_row = connection.execute(
                """SELECT file_id,scene_id FROM files
                   WHERE file_id=? AND scene_id=? AND path=? AND exists_on_disk=1""",
                (str(act["file_id"]), str(act["scene_id"]), str(dest_vid))
            ).fetchone()
            if not f_row:
                continue

            dest_parent = dest_vid.parent
            possible_dest_comps = [
                dest_parent / (dest_vid.name + src_suffix),
                dest_parent / (dest_vid.stem + src_suffix),
                dest_parent / src.name,
            ]
            for dc in possible_dest_comps:
                try:
                    if dc.is_file() and dc.stat().st_size > 0:
                        return True
                except OSError:
                    pass
    return False


def get_companion_files(video_path: Path | str) -> list[str]:
    """Return a list of companion filenames associated with a video file on disk."""
    try:
        vpath = Path(video_path)
        parent = vpath.parent
        if not parent.is_dir():
            return []
        v_stem = vpath.stem.lower()
        v_name = vpath.name.lower()
        v_key = _sidecar_match_key(vpath.stem)
        companions = []
        for entry in parent.iterdir():
            try:
                if not entry.is_file() or entry == vpath:
                    continue
                e_suffix = entry.suffix.lower()
                e_name = entry.name.lower()
                e_stem = entry.stem.lower()
                is_comp = (
                    e_suffix in COMPANION_EXTENSIONS or
                    e_name.endswith(('.csm.jpg', '.csm.png', '.csm.webp'))
                )
                if not is_comp:
                    continue
                if (
                    e_name == v_name + e_suffix or
                    e_stem == v_stem or
                    (v_key and _sidecar_match_key(entry.stem) == v_key)
                ):
                    companions.append(entry.name)
            except OSError:
                continue
        return sorted(companions)
    except OSError:
        return []


def associated_file_target(candidate: Path, current: Path, proposed: Path) -> Path | None:
    """Return a companion's target while preserving its existing naming convention (case-insensitively)."""
    suffix = candidate.suffix.lower()
    candidate_stem_lower = candidate.stem.lower()
    current_stem_lower = current.stem.lower()
    # Pre-compute normalised keys once — avoids double-calling and prevents
    # two punctuation-only filenames whose keys both collapse to "" matching each other.
    key_cand = _sidecar_match_key(candidate.stem)
    key_curr = _sidecar_match_key(current.stem)
    stems_match = candidate_stem_lower == current_stem_lower or (
        bool(key_cand) and key_cand == key_curr
    )
    if suffix in ASSOCIATED_EXTENSIONS:
        if stems_match:
            return candidate.with_name(proposed.stem + candidate.suffix)
    if suffix not in IMAGE_SIDECAR_EXTENSIONS:
        return None
    # Matches exact or bracket/accent-tolerant stem companions (e.g. video.jpg)
    if stems_match:
        return candidate.with_name(proposed.stem + candidate.suffix)
    # Covers Stash-style companions such as video.mp4.jpg (exact or bracket/accent-tolerant)
    image_base = candidate.name[:-len(candidate.suffix)]
    key_img = _sidecar_match_key(image_base)
    key_curr_name = _sidecar_match_key(current.name)
    if image_base.lower() == current.name.lower() or (
        bool(key_img) and key_img == key_curr_name
    ):
        return candidate.with_name(proposed.name + candidate.suffix)
    return None


def strict_incoming_companions(video_path: Path | str, incoming_folders: list[str]) -> list[dict]:
    """List exact-name companions beside an incoming video without fuzzy matching."""
    video = Path(video_path)
    try:
        video_parent = video.parent.resolve()
        if not any(_is_subpath_of(video_parent, Path(root).expanduser().resolve()) for root in incoming_folders):
            return []
        if not video_parent.is_dir():
            return []
    except (OSError, RuntimeError):
        return []

    video_name = video.name.lower()
    video_stem = video.stem.lower()
    companions = []
    for entry in video_parent.iterdir():
        try:
            if entry == video or entry.is_symlink() or not entry.is_file():
                continue
            name = entry.name.lower()
            exact_video_suffix = name.startswith(video_name + ".")
            exact_stem_suffix = name.startswith(video_stem + ".")
            suffix_text = name[len(video_name):] if exact_video_suffix else (
                name[len(video_stem):] if exact_stem_suffix else ""
            )
            allowed = any(
                suffix_text == ext or suffix_text.endswith(ext)
                for ext in COMPANION_EXTENSIONS
            )
            if not allowed:
                continue
            snapshot = get_file_stat_snapshot(entry)
            if snapshot is None:
                continue
            companions.append({
                "path": str(entry),
                "basename": entry.name,
                "size": snapshot[0],
                "mtime_ns": snapshot[1],
                "device": snapshot[2],
                "inode": snapshot[3],
            })
        except OSError:
            continue
    return sorted(companions, key=lambda item: item["path"].lower())


def is_file_on_unavailable_root(path_str: str, unavailable_roots: Optional[List[str]] = None) -> bool:
    """Check if a file's root or mount point is currently offline or unreachable.
    Uses normalized path prefix resolution against the configured unavailable roots.
    """
    if not unavailable_roots or not path_str:
        return False
    try:
        norm_path = os.path.normcase(str(path_str)).replace(chr(92), "/")
        for unavail in unavailable_roots:
            if not unavail:
                continue
            norm_unavail = os.path.normcase(str(unavail)).replace(chr(92), "/")
            if norm_path == norm_unavail or norm_path.startswith(norm_unavail.rstrip("/") + "/"):
                return True
    except (ValueError, OSError):
        pass
    return False


def get_authoritative_unavailable_roots(database_path: Optional[Path] = None, extra_roots: Optional[list] = None) -> list[str]:
    """Return the list of configured or monitored roots that are currently offline or unreachable.
    Probes filesystem directly; discards stale monitor status entries if the root is now accessible.
    """
    candidates = set()
    if extra_roots is not None:
        for r in extra_roots:
            if r and str(r).strip():
                try:
                    candidates.add(str(Path(r).expanduser()))
                except Exception:
                    candidates.add(str(r))
    elif database_path:
        try:
            conn = connect(database_path)
            try:
                status_row = conn.execute(
                    "SELECT unavailable_roots_json FROM filesystem_monitor_status WHERE id=1"
                ).fetchone()
                if status_row and status_row["unavailable_roots_json"]:
                    for r in json.loads(status_row["unavailable_roots_json"]):
                        if r and str(r).strip():
                            try:
                                candidates.add(str(Path(r).expanduser()))
                            except Exception:
                                candidates.add(str(r))
            finally:
                conn.close()
        except Exception:
            pass

    unavailable = []
    for cand in candidates:
        try:
            p = Path(cand)
            if not p.is_dir():
                unavailable.append(cand)
        except (OSError, ValueError):
            unavailable.append(cand)

    return sorted(unavailable)


def get_file_stat_snapshot(path: Path | str) -> tuple[int, int, int, int] | None:
    """Return a high-resolution (size, mtime_ns, device, inode) identity snapshot."""
    try:
        p = Path(path)
        st = p.stat()
        return (int(st.st_size), int(st.st_mtime_ns), int(st.st_dev), int(st.st_ino))
    except OSError:
        return None


def calculate_sha256_verified(path: Path | str, chunk_size: int = 65536) -> str | None:
    """Calculate complete SHA-256 checksum with pre- and post-read stability checks."""
    p = Path(path)
    stat_before = get_file_stat_snapshot(p)
    if stat_before is None or stat_before[0] == 0:
        return None

    try:
        hasher = hashlib.sha256()
        with p.open("rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                hasher.update(chunk)
        digest = hasher.hexdigest().lower()
    except OSError:
        return None

    stat_after = get_file_stat_snapshot(p)
    if stat_after != stat_before:
        return None
    return digest


_ORIGINAL_CALCULATE_SHA256_VERIFIED = calculate_sha256_verified


def get_calculate_sha256_verified_fn() -> Callable:
    """Return the active calculate_sha256_verified function, prioritizing any monkeypatch
    applied to either librarymanager.scanning.filesystem or librarymanager_core.
    """
    canon = sys.modules.get(__name__, None)
    canon_fn = getattr(canon, "calculate_sha256_verified", None) if canon else calculate_sha256_verified
    if canon_fn is not None and canon_fn is not _ORIGINAL_CALCULATE_SHA256_VERIFIED:
        return canon_fn

    core_mod = sys.modules.get("librarymanager_core")
    if core_mod is not None:
        core_hash = getattr(core_mod, "calculate_sha256_verified", None)
        if core_hash is not None and not getattr(core_hash, "_is_watchtower_wrapper", False):
            return core_hash

    return canon_fn or _ORIGINAL_CALCULATE_SHA256_VERIFIED


def calculate_sha256(path: Path | str, chunk_size: int = 65536) -> str | None:
    """Calculate the complete SHA-256 checksum for a file on disk."""
    hash_fn = get_calculate_sha256_verified_fn()
    return hash_fn(path, chunk_size=chunk_size)


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
    snapshot = get_file_stat_snapshot(path)
    if snapshot is None or snapshot[0] <= 0:
        return False
    size, mtime_ns, device, inode = snapshot
    now_ts = time.time()
    now = utc_now()
    connection = connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing_job = connection.execute(
            "SELECT status,device,inode FROM checksum_jobs WHERE path=? AND size=? AND mtime_ns=?",
            (str(path), size, mtime_ns),
        ).fetchone()
        will_activate = (
            existing_job is None or
            existing_job["status"] not in ("pending", "retry", "processing") or
            (
                existing_job["status"] != "processing" and
                (
                    int(existing_job["device"] if existing_job["device"] is not None else -1) != device or
                    int(existing_job["inode"] if existing_job["inode"] is not None else -1) != inode
                )
            )
        )
        if will_activate:
            active_count = connection.execute(
                "SELECT COUNT(*) FROM checksum_jobs WHERE status IN ('pending','retry','processing')"
            ).fetchone()[0]
            if active_count >= CHECKSUM_MAX_ACTIVE_JOBS:
                connection.rollback()
                return False
        connection.execute(
            """UPDATE checksum_jobs SET status='superseded',updated_at=?
               WHERE path=? AND status IN ('pending','retry')
                 AND (size!=? OR mtime_ns!=? OR COALESCE(device,-1)!=? OR COALESCE(inode,-1)!=?)""",
            (now, str(path), size, mtime_ns, device, inode),
        )
        connection.execute(
            """INSERT INTO checksum_jobs(
                   path,file_id,size,mtime_ns,device,inode,status,attempts,
                   available_at,claimed_at,updated_at,last_error
               ) VALUES (?,?,?,?,?,?,'pending',0,?,NULL,?,NULL)
               ON CONFLICT(path,size,mtime_ns) DO UPDATE SET
                   file_id=COALESCE(checksum_jobs.file_id,excluded.file_id),
                   device=CASE WHEN checksum_jobs.status='processing'
                               THEN checksum_jobs.device ELSE excluded.device END,
                   inode=CASE WHEN checksum_jobs.status='processing'
                              THEN checksum_jobs.inode ELSE excluded.inode END,
                   status=CASE WHEN checksum_jobs.status='processing' THEN checksum_jobs.status
                               WHEN checksum_jobs.status NOT IN ('pending','retry','processing')
                                 OR COALESCE(checksum_jobs.device,-1)!=COALESCE(excluded.device,-1)
                                 OR COALESCE(checksum_jobs.inode,-1)!=COALESCE(excluded.inode,-1)
                               THEN 'pending' ELSE checksum_jobs.status END,
                   attempts=CASE WHEN checksum_jobs.status NOT IN ('pending','retry','processing')
                                   OR COALESCE(checksum_jobs.device,-1)!=COALESCE(excluded.device,-1)
                                   OR COALESCE(checksum_jobs.inode,-1)!=COALESCE(excluded.inode,-1)
                                 THEN 0 ELSE checksum_jobs.attempts END,
                   available_at=CASE WHEN checksum_jobs.status NOT IN ('pending','retry','processing')
                                       OR COALESCE(checksum_jobs.device,-1)!=COALESCE(excluded.device,-1)
                                       OR COALESCE(checksum_jobs.inode,-1)!=COALESCE(excluded.inode,-1)
                                     THEN excluded.available_at ELSE checksum_jobs.available_at END,
                   claimed_at=CASE WHEN checksum_jobs.status='processing'
                                   THEN checksum_jobs.claimed_at ELSE NULL END,
                   updated_at=excluded.updated_at,
                   last_error=CASE WHEN checksum_jobs.status='processing'
                                   THEN checksum_jobs.last_error ELSE NULL END""",
            (str(path), str(file_id) if file_id is not None else None,
             size, mtime_ns, device, inode, now_ts + max(0.0, float(stability_seconds)), now),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reset_checksum_jobs_for_monitor_restart(database_path: Path) -> int:
    """Return jobs abandoned by a previous monitor process to the durable queue."""
    connection = connect(database_path)
    try:
        cursor = connection.execute(
            """UPDATE checksum_jobs SET status='retry',claimed_at=NULL,available_at=?,
                      updated_at=?,last_error='Monitor restarted before checksum completed'
               WHERE status='processing'""",
            (time.time(), utc_now()),
        )
        connection.commit()
        return int(cursor.rowcount)
    finally:
        connection.close()


def claim_checksum_job(database_path: Path) -> dict | None:
    """Atomically claim one due checksum job; concurrent workers cannot share it."""
    connection = connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT * FROM checksum_jobs
               WHERE status IN ('pending','retry') AND available_at<=?
               ORDER BY available_at,id LIMIT 1""",
            (time.time(),),
        ).fetchone()
        if not row:
            connection.rollback()
            return None
        cursor = connection.execute(
            """UPDATE checksum_jobs SET status='processing',claimed_at=?,updated_at=?
               WHERE id=? AND status IN ('pending','retry')""",
            (time.time(), utc_now(), row["id"]),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        connection.commit()
        return dict(row)
    finally:
        connection.close()


def _retry_checksum_job(database_path: Path, job: dict, reason: str) -> None:
    attempts = int(job.get("attempts") or 0) + 1
    delay = min(300.0, CHECKSUM_RETRY_BASE_SECONDS * (2 ** min(attempts - 1, 4)))
    connection = connect(database_path)
    try:
        connection.execute(
            """UPDATE checksum_jobs SET status='retry',attempts=?,available_at=?,
                      claimed_at=NULL,updated_at=?,last_error=? WHERE id=?""",
            (attempts, time.time() + delay, utc_now(), reason, job["id"]),
        )
        connection.commit()
    finally:
        connection.close()


def process_checksum_job(database_path: Path, job: dict) -> bool:
    """Hash one claimed job and persist its verified file identity."""
    expected = (
        int(job["size"]), int(job["mtime_ns"]),
        int(job.get("device") or 0), int(job.get("inode") or 0),
    )
    current = get_file_stat_snapshot(job["path"])
    if current is None:
        _retry_checksum_job(database_path, job, "File is offline or inaccessible")
        return False
    if current != expected:
        connection = connect(database_path)
        try:
            connection.execute(
                "UPDATE checksum_jobs SET status='superseded',claimed_at=NULL,updated_at=?,last_error=? WHERE id=?",
                (utc_now(), "File changed before checksum processing", job["id"]),
            )
            connection.commit()
        finally:
            connection.close()
        enqueue_checksum_calculation(database_path, job["path"], job.get("file_id"))
        return False

    hash_fn = get_calculate_sha256_verified_fn()
    sha256 = hash_fn(job["path"])
    if not sha256:
        _retry_checksum_job(database_path, job, "File changed, disappeared, or became inaccessible while hashing")
        return False
    if get_file_stat_snapshot(job["path"]) != expected:
        _retry_checksum_job(database_path, job, "File identity changed after hashing")
        return False

    connection = connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO file_checksum_cache(
                   path,file_id,size,mtime,mtime_ns,device,inode,sha256,status,calculated_at
               ) VALUES (?,?,?,?,?,?,?,?,'completed',?)
               ON CONFLICT(path) DO UPDATE SET
                   file_id=excluded.file_id,size=excluded.size,mtime=excluded.mtime,
                   mtime_ns=excluded.mtime_ns,device=excluded.device,inode=excluded.inode,
                   sha256=excluded.sha256,status='completed',calculated_at=excluded.calculated_at""",
            (job["path"], job.get("file_id"), expected[0], expected[1] / 1_000_000_000,
             expected[1], expected[2], expected[3], sha256, utc_now()),
        )
        connection.execute(
            """UPDATE checksum_jobs SET status='completed',claimed_at=NULL,updated_at=?,
                      last_error=NULL WHERE id=?""",
            (utc_now(), job["id"]),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def process_next_checksum_job(database_path: Path) -> bool:
    """Process at most one durable checksum job."""
    job = claim_checksum_job(database_path)
    if not job:
        return False
    try:
        process_checksum_job(database_path, job)
    except Exception as exc:
        _retry_checksum_job(database_path, job, f"Checksum worker error: {exc}")
    return True


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
    p = Path(path)
    stat = get_file_stat_snapshot(p)
    if stat is None:
        return None, "offline"
    size, mtime_ns, device, inode = stat
    if size == 0:
        return None, "empty"

    p_str = str(p)
    conn = connect(database_path)
    try:
        row = conn.execute(
            """SELECT sha256,status,file_id FROM file_checksum_cache
               WHERE path=? AND size=? AND mtime_ns=? AND device=? AND inode=?""",
            (p_str, size, mtime_ns, device, inode)
        ).fetchone()
        if row and row["sha256"]:
            if file_id is not None and str(row["file_id"] or "") != str(file_id):
                conn.execute("UPDATE file_checksum_cache SET file_id=? WHERE path=?", (str(file_id), p_str))
                conn.commit()
            return row["sha256"], "verified"

        if not allow_compute:
            enqueue_checksum_calculation(database_path, p, file_id)
            return None, "pending"

        hash_fn = get_calculate_sha256_verified_fn()
        sha256 = hash_fn(p)
        if not sha256:
            return None, "unstable"

        now = utc_now()
        conn.execute(
            """INSERT OR REPLACE INTO file_checksum_cache(
                path,file_id,size,mtime,mtime_ns,device,inode,sha256,status,calculated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)""",
            (p_str, str(file_id) if file_id is not None else None, size,
             mtime_ns / 1_000_000_000, mtime_ns, device, inode, sha256, now)
        )
        conn.commit()
        return sha256, "verified"
    finally:
        conn.close()


def cached_sha256_for_file_id(database_path: Path, file_id: str, size: int | None = None) -> str | None:
    """Return durable verified source identity even after its original path disappears."""
    connection = connect(database_path)
    try:
        if size is None:
            row = connection.execute(
                """SELECT sha256 FROM file_checksum_cache
                   WHERE file_id=? AND status='completed' AND sha256 IS NOT NULL
                   ORDER BY calculated_at DESC LIMIT 1""",
                (str(file_id),),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT sha256 FROM file_checksum_cache
                   WHERE file_id=? AND size=? AND status='completed' AND sha256 IS NOT NULL
                   ORDER BY calculated_at DESC LIMIT 1""",
                (str(file_id), int(size)),
            ).fetchone()
        return str(row["sha256"]).lower() if row and row["sha256"] else None
    finally:
        connection.close()


def _cached_sha256_for_current_path(database_path: Path, path: Path | str) -> str | None:
    """Return a checksum only when it belongs to the file's current stat identity."""
    snapshot = get_file_stat_snapshot(path)
    if snapshot is None:
        return None
    size, mtime_ns, device, inode = snapshot
    connection = connect(database_path)
    try:
        row = connection.execute(
            """SELECT sha256 FROM file_checksum_cache
               WHERE path=? AND size=? AND mtime_ns=? AND device=? AND inode=?
                 AND status='completed' AND sha256 IS NOT NULL""",
            (str(path), size, mtime_ns, device, inode),
        ).fetchone()
        return str(row["sha256"]).lower() if row and row["sha256"] else None
    finally:
        connection.close()


def expect_filesystem_move(database_path: Path, source: str, destination: str, ttl_seconds: float = 60) -> None:
    connection = connect(database_path)
    try:
        now = datetime.now().timestamp()
        connection.execute("DELETE FROM expected_moves WHERE expires_at < ?", (now,))
        connection.execute(
            "INSERT OR REPLACE INTO expected_moves(source_path,destination_path,expires_at) VALUES (?,?,?)",
            (str(source), str(destination), now + ttl_seconds),
        )
        connection.commit()
    finally:
        connection.close()
    expect_filesystem_create(database_path, destination, ttl_seconds=ttl_seconds)


def consume_expected_move(database_path: Path, source: str, destination: str) -> bool:
    connection = connect(database_path)
    try:
        now = datetime.now().timestamp()
        connection.execute("DELETE FROM expected_moves WHERE expires_at < ?", (now,))
        cursor = connection.execute(
            "DELETE FROM expected_moves WHERE source_path=? AND destination_path=? AND expires_at>=?",
            (str(source), str(destination), now),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def consume_expected_move_source(database_path: Path, source: str) -> bool:
    connection = connect(database_path)
    try:
        now = datetime.now().timestamp()
        connection.execute("DELETE FROM expected_moves WHERE expires_at < ?", (now,))
        cursor = connection.execute(
            "DELETE FROM expected_moves WHERE source_path=? AND expires_at>=?",
            (str(source), now),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def expect_filesystem_delete(database_path: Path, path: str, ttl_seconds: float = 60) -> None:
    """Mark one explicit deletion so the monitor does not report it as external."""
    connection = connect(database_path)
    try:
        now = datetime.now().timestamp()
        connection.execute("DELETE FROM expected_moves WHERE expires_at < ?", (now,))
        connection.execute(
            "INSERT OR REPLACE INTO expected_moves(source_path,destination_path,expires_at) VALUES (?,?,?)",
            (str(path), "watchtower://verified-duplicate-deletion", now + ttl_seconds),
        )
        connection.commit()
    finally:
        connection.close()


def cancel_expected_filesystem_delete(database_path: Path, path: str) -> None:
    """Remove an expected-deletion marker when the deletion itself failed."""
    connection = connect(database_path)
    try:
        connection.execute(
            "DELETE FROM expected_moves WHERE source_path=? AND destination_path=?",
            (str(path), "watchtower://verified-duplicate-deletion"),
        )
        connection.commit()
    finally:
        connection.close()


def consume_expected_move_destination(database_path: Path, destination: str) -> bool:
    return consume_expected_create(database_path, destination)


def expect_filesystem_create(database_path: Path, path: str, ttl_seconds: float = 120) -> None:
    connection = connect(database_path)
    try:
        now = datetime.now().timestamp()
        connection.execute("DELETE FROM expected_creates WHERE expires_at < ?", (now,))
        connection.execute(
            "INSERT OR REPLACE INTO expected_creates(path,expires_at) VALUES (?,?)",
            (os.path.normpath(str(path)), now + ttl_seconds),
        )
        connection.commit()
    finally:
        connection.close()


def consume_expected_create(database_path: Path, path: str) -> bool:
    connection = connect(database_path)
    try:
        now = datetime.now().timestamp()
        connection.execute("DELETE FROM expected_creates WHERE expires_at < ?", (now,))
        cursor = connection.execute(
            "DELETE FROM expected_creates WHERE path=? AND expires_at>=?",
            (os.path.normpath(str(path)), now),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


__all__ = [
    "ACTIVE_INCOMING_LIFECYCLE_STATUSES",
    "ASSOCIATED_EXTENSIONS",
    "CHECKSUM_MAX_ACTIVE_JOBS",
    "CHECKSUM_RETRY_BASE_SECONDS",
    "CHECKSUM_STABILITY_SECONDS",
    "COMPANION_EXTENSIONS",
    "IMAGE_SIDECAR_EXTENSIONS",
    "SYSTEM_JUNK_FILENAMES",
    "TEMPORARY_DOWNLOAD_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "_cached_sha256_for_current_path",
    "_is_subpath_of",
    "_retry_checksum_job",
    "_sidecar_match_key",
    "associated_file_target",
    "cached_sha256_for_file_id",
    "calculate_sha256",
    "calculate_sha256_verified",
    "cancel_expected_filesystem_delete",
    "claim_checksum_job",
    "connect",
    "consume_expected_create",
    "consume_expected_move",
    "consume_expected_move_destination",
    "consume_expected_move_source",
    "enqueue_checksum_calculation",
    "expect_filesystem_create",
    "expect_filesystem_delete",
    "expect_filesystem_move",
    "get_authoritative_unavailable_roots",
    "get_calculate_sha256_verified_fn",
    "get_cached_or_compute_sha256",
    "get_companion_files",
    "get_file_stat_snapshot",
    "is_actionable_incoming_file",
    "is_file_on_unavailable_root",
    "is_source_companion_of_moved_video",
    "is_temporary_download",
    "is_verified_companion_destination",
    "parse_cold_storage_roots",
    "process_checksum_job",
    "process_next_checksum_job",
    "reset_checksum_jobs_for_monitor_restart",
    "strict_incoming_companions",
    "utc_now",
]
