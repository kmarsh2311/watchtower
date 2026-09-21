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
from datetime import datetime, timezone
from pathlib import Path

from librarymanager_reconciliation import RECONCILIATION_SCHEMA


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS inventory_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    stash_file_count INTEGER NOT NULL DEFAULT 0,
    stash_scene_count INTEGER NOT NULL DEFAULT 0,
    present_count INTEGER NOT NULL DEFAULT 0,
    missing_count INTEGER NOT NULL DEFAULT 0,
    changed_path_count INTEGER NOT NULL DEFAULT 0,
    restored_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    scene_id TEXT NOT NULL,
    path TEXT NOT NULL,
    basename TEXT NOT NULL,
    title TEXT,
    studio TEXT,
    performers_json TEXT NOT NULL DEFAULT '[]',
    size INTEGER,
    duration REAL,
    height INTEGER,
    fingerprints_json TEXT NOT NULL DEFAULT '[]',
    scene_metadata_json TEXT NOT NULL DEFAULT '{}',
    exists_on_disk INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    missing_since TEXT
);
CREATE TABLE IF NOT EXISTS inventory_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES inventory_runs(id),
    file_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    old_path TEXT,
    new_path TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_scene_id ON files(scene_id);
CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
CREATE INDEX IF NOT EXISTS idx_files_basename ON files(basename);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON inventory_events(run_id);
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    missing_count INTEGER NOT NULL DEFAULT 0,
    matched_count INTEGER NOT NULL DEFAULT 0,
    ambiguous_count INTEGER NOT NULL DEFAULT 0,
    unmatched_count INTEGER NOT NULL DEFAULT 0,
    skipped_folder_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS reconciliation_candidates (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES reconciliation_runs(id),
    file_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    expected_path TEXT NOT NULL,
    candidate_path TEXT,
    confidence TEXT NOT NULL,
    reason TEXT NOT NULL,
    expected_size INTEGER,
    candidate_size INTEGER,
    oshash_match INTEGER,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_run_id ON reconciliation_candidates(run_id);
CREATE TABLE IF NOT EXISTS filename_state (
    file_id TEXT PRIMARY KEY REFERENCES files(file_id),
    base_stem TEXT NOT NULL,
    base_source TEXT NOT NULL,
    source_title TEXT,
    last_generated_stem TEXT,
    manual_studio TEXT,
    manual_performers_json TEXT NOT NULL DEFAULT '[]',
    managed_date TEXT,
    managed_quality TEXT,
    rename_protected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filename_preview_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    examined_count INTEGER NOT NULL DEFAULT 0,
    proposed_count INTEGER NOT NULL DEFAULT 0,
    unchanged_count INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS filename_previews (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES filename_preview_runs(id),
    file_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    current_path TEXT NOT NULL,
    base_stem TEXT NOT NULL,
    proposed_path TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filename_previews_run ON filename_previews(run_id);
CREATE TABLE IF NOT EXISTS file_checksum_cache (
    path TEXT PRIMARY KEY,
    file_id TEXT,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    mtime_ns INTEGER,
    device INTEGER,
    inode INTEGER,
    sha256 TEXT,
    oshash TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    calculated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checksum_cache_size ON file_checksum_cache(size);
CREATE TABLE IF NOT EXISTS checksum_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    file_id TEXT,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    device INTEGER,
    inode INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL,
    claimed_at REAL,
    updated_at TEXT NOT NULL,
    last_error TEXT,
    UNIQUE(path, size, mtime_ns)
);
CREATE INDEX IF NOT EXISTS idx_checksum_jobs_due ON checksum_jobs(status,available_at,id);
CREATE TABLE IF NOT EXISTS rename_queue (
    scene_id TEXT PRIMARY KEY,
    enqueued_at REAL NOT NULL,
    available_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    processing_started_at REAL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS rename_worker_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    scheduled INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO rename_worker_state(id,scheduled) VALUES (1,0);
CREATE TABLE IF NOT EXISTS rename_audit (
    id INTEGER PRIMARY KEY,
    scene_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    scene_id TEXT,
    file_id TEXT,
    old_path TEXT,
    new_path TEXT,
    detail TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_recorded_at ON activity_log(recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_scene_id ON activity_log(scene_id);
CREATE TABLE IF NOT EXISTS expected_moves (
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY(source_path,destination_path)
);
CREATE TABLE IF NOT EXISTS expected_creates (
    path TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS filesystem_events (
    event_key TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    destination_path TEXT,
    is_directory INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS transcoder_candidates (
    candidate_path TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_transcoder_candidates_source ON transcoder_candidates(source_path);
CREATE TABLE IF NOT EXISTS filesystem_monitor_status (
    id INTEGER PRIMARY KEY CHECK(id=1),
    token TEXT,
    pid INTEGER,
    state TEXT NOT NULL DEFAULT 'stopped',
    started_at TEXT,
    heartbeat_at TEXT,
    roots_json TEXT NOT NULL DEFAULT '[]',
    unavailable_roots_json TEXT NOT NULL DEFAULT '[]',
    active_moves_json TEXT NOT NULL DEFAULT '[]',
    auto_restart_attempted_at REAL,
    auto_restart_failures INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO filesystem_monitor_status(id,state) VALUES (1,'stopped');
CREATE TABLE IF NOT EXISTS incoming_files (
    path TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    size INTEGER,
    modified_ns INTEGER,
    stable_since REAL,
    settle_seconds INTEGER NOT NULL DEFAULT 300,
    status TEXT NOT NULL DEFAULT 'waiting',
    attempts INTEGER NOT NULL DEFAULT 0,
    scan_job_id TEXT,
    detail TEXT,
    filing_diagnostic TEXT
);
CREATE INDEX IF NOT EXISTS idx_incoming_status ON incoming_files(status);
CREATE TABLE IF NOT EXISTS filesystem_reconciliation_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    event_count INTEGER NOT NULL DEFAULT 0,
    verified_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    informational_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS filesystem_reconciliation_proposals (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES filesystem_reconciliation_runs(id),
    event_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    file_id TEXT,
    scene_id TEXT,
    old_path TEXT,
    new_path TEXT,
    confidence TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    reason TEXT NOT NULL,
    action_performed INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fs_proposals_run ON filesystem_reconciliation_proposals(run_id);
CREATE TABLE IF NOT EXISTS filing_incoming_baseline (
    path TEXT PRIMARY KEY,
    size INTEGER,
    modified_ns INTEGER,
    oshash TEXT,
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filing_baseline_hash ON filing_incoming_baseline(oshash, size);
CREATE TABLE IF NOT EXISTS filing_baseline_acknowledgements (
    path TEXT PRIMARY KEY REFERENCES filing_incoming_baseline(path) ON DELETE CASCADE,
    reason TEXT NOT NULL DEFAULT 'intentionally_removed',
    acknowledged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filing_baseline_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    established_at TEXT NOT NULL,
    completed_at TEXT,
    incoming_folders_json TEXT NOT NULL DEFAULT '[]',
    file_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'in_progress',
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_filing_baseline_status ON filing_baseline_state(status);
CREATE TABLE IF NOT EXISTS filing_baseline_summary (
    id INTEGER PRIMARY KEY CHECK (id=1),
    initial_count INTEGER NOT NULL DEFAULT 0,
    remaining_count INTEGER NOT NULL DEFAULT 0,
    filed_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    acknowledged_count INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filing_destination_dir_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path TEXT NOT NULL,
    dir_path TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    depth INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filing_dest_cache_root ON filing_destination_dir_cache(root_path);
CREATE INDEX IF NOT EXISTS idx_filing_dest_cache_norm ON filing_destination_dir_cache(norm_name);
CREATE TABLE IF NOT EXISTS filing_destination_cache_meta (
    root_path TEXT PRIMARY KEY,
    max_depth INTEGER NOT NULL,
    scanned_at TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS filing_folder_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    folder_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_filing_mappings_entity ON filing_folder_mappings(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_filing_mappings_folder ON filing_folder_mappings(folder_path);
CREATE TABLE IF NOT EXISTS active_filing_transfers (
    proposal_id INTEGER PRIMARY KEY,
    scene_id TEXT,
    file_id TEXT,
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    destination_folder TEXT NOT NULL,
    stage TEXT NOT NULL,
    stage_label TEXT NOT NULL,
    detail TEXT NOT NULL,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filing_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    proposed_path TEXT NOT NULL,
    destination_folder TEXT NOT NULL,
    destination_filename TEXT NOT NULL,
    organize_by TEXT NOT NULL,
    matched_entity_id TEXT NOT NULL,
    matched_entity_name TEXT NOT NULL,
    matched_alias TEXT,
    match_source TEXT NOT NULL,
    reason TEXT NOT NULL,
    companions_json TEXT NOT NULL DEFAULT '[]',
    candidate_destinations_json TEXT NOT NULL DEFAULT '[]',
    is_custom_mapped INTEGER NOT NULL DEFAULT 0,
    in_nested_folder INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filing_status ON filing_proposals(status);
CREATE INDEX IF NOT EXISTS idx_filing_file_id ON filing_proposals(file_id);
CREATE INDEX IF NOT EXISTS idx_filing_source_path ON filing_proposals(source_path);
CREATE TABLE IF NOT EXISTS duplicate_file_repairs (
    candidate_path TEXT PRIMARY KEY,
    candidate_file_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    retained_file_id TEXT NOT NULL,
    retained_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    deleted_companions_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_duplicate_repairs_status ON duplicate_file_repairs(status);
"""

SCHEMA += RECONCILIATION_SCHEMA

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
ACTIVE_INCOMING_LIFECYCLE_STATUSES = {
    "waiting", "downloading", "scanning", "generating_sheet", "renaming",
}


def is_temporary_download(path) -> bool:
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


def is_actionable_incoming_file(path, active_status: str | None = None) -> bool:
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


def is_verified_companion_destination(connection, destination_path: str, source_path: str | None = None) -> bool:
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



def is_source_companion_of_moved_video(connection, source_path: str) -> bool:
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Per-process sentinel: schema migrations run exactly once per database path,
# even when connect() is called many times by a long-running monitor.
_schema_applied: set[str] = set()
_schema_lock = threading.Lock()


def _safe_alter(connection: "sqlite3.Connection", table: str, column: str, ddl: str) -> None:
    """Apply an ALTER TABLE only if the column is absent; silently handles concurrent-process races."""
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return
    try:
        connection.execute(ddl)
        connection.commit()
    except sqlite3.OperationalError as exc:
        # Another process added the column between our check and our execute — harmless.
        if "duplicate column" not in str(exc).lower():
            raise


def _ensure_schema(connection: "sqlite3.Connection", database_path: Path) -> None:
    """Run CREATE TABLE / ALTER TABLE migrations exactly once per process per database."""
    key = str(database_path.resolve())
    if key in _schema_applied:
        return
    with _schema_lock:
        if key in _schema_applied:
            return  # Another thread beat us here
        connection.executescript(SCHEMA)
        _safe_alter(connection, "files", "scene_metadata_json",
                    "ALTER TABLE files ADD COLUMN scene_metadata_json TEXT NOT NULL DEFAULT '{}'")
        _safe_alter(connection, "filename_state", "manual_studio",
                    "ALTER TABLE filename_state ADD COLUMN manual_studio TEXT")
        _safe_alter(connection, "filename_state", "manual_performers_json",
                    "ALTER TABLE filename_state ADD COLUMN manual_performers_json TEXT NOT NULL DEFAULT '[]'")
        _safe_alter(connection, "filename_state", "managed_date",
                    "ALTER TABLE filename_state ADD COLUMN managed_date TEXT")
        _safe_alter(connection, "filename_state", "managed_quality",
                    "ALTER TABLE filename_state ADD COLUMN managed_quality TEXT")
        _safe_alter(connection, "files", "height",
                    "ALTER TABLE files ADD COLUMN height INTEGER")
        _safe_alter(connection, "incoming_files", "attempts",
                    "ALTER TABLE incoming_files ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        _safe_alter(connection, "incoming_files", "settle_seconds",
                    "ALTER TABLE incoming_files ADD COLUMN settle_seconds INTEGER NOT NULL DEFAULT 300")
        _safe_alter(connection, "inventory_runs", "stash_scene_count",
                    "ALTER TABLE inventory_runs ADD COLUMN stash_scene_count INTEGER NOT NULL DEFAULT 0")
        _safe_alter(connection, "rename_queue", "processing_started_at",
                    "ALTER TABLE rename_queue ADD COLUMN processing_started_at REAL")
        _safe_alter(connection, "filesystem_monitor_status", "auto_restart_attempted_at",
                    "ALTER TABLE filesystem_monitor_status ADD COLUMN auto_restart_attempted_at REAL")
        _safe_alter(connection, "filesystem_monitor_status", "active_moves_json",
                    "ALTER TABLE filesystem_monitor_status ADD COLUMN active_moves_json TEXT NOT NULL DEFAULT '[]'")
        _safe_alter(connection, "filesystem_monitor_status", "auto_restart_failures",
                    "ALTER TABLE filesystem_monitor_status ADD COLUMN auto_restart_failures INTEGER NOT NULL DEFAULT 0")
        _safe_alter(connection, "filing_proposals", "candidate_destinations_json",
                    "ALTER TABLE filing_proposals ADD COLUMN candidate_destinations_json TEXT NOT NULL DEFAULT '[]'")
        _safe_alter(connection, "filing_proposals", "is_custom_mapped",
                    "ALTER TABLE filing_proposals ADD COLUMN is_custom_mapped INTEGER NOT NULL DEFAULT 0")
        _safe_alter(connection, "filing_proposals", "in_nested_folder",
                    "ALTER TABLE filing_proposals ADD COLUMN in_nested_folder INTEGER NOT NULL DEFAULT 0")
        _safe_alter(connection, "filename_state", "rename_protected",
                    "ALTER TABLE filename_state ADD COLUMN rename_protected INTEGER NOT NULL DEFAULT 0")
        _safe_alter(connection, "incoming_files", "filing_diagnostic",
                    "ALTER TABLE incoming_files ADD COLUMN filing_diagnostic TEXT")
        _safe_alter(connection, "file_checksum_cache", "file_id",
                    "ALTER TABLE file_checksum_cache ADD COLUMN file_id TEXT")
        _safe_alter(connection, "file_checksum_cache", "mtime_ns",
                    "ALTER TABLE file_checksum_cache ADD COLUMN mtime_ns INTEGER")
        _safe_alter(connection, "file_checksum_cache", "device",
                    "ALTER TABLE file_checksum_cache ADD COLUMN device INTEGER")
        _safe_alter(connection, "file_checksum_cache", "inode",
                    "ALTER TABLE file_checksum_cache ADD COLUMN inode INTEGER")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_checksum_cache_file_id ON file_checksum_cache(file_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_filesystem_events_status ON filesystem_events(status)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_status ON filesystem_reconciliation_runs(status)")
        connection.execute(
            """UPDATE inventory_runs SET stash_scene_count=(SELECT COUNT(DISTINCT scene_id) FROM files)
               WHERE status='complete' AND stash_scene_count=0"""
        )
        connection.commit()
        _schema_applied.add(key)


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    # SQLite does not persist this per-connection setting.
    connection.execute("PRAGMA foreign_keys=ON")
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


def _is_subpath_of(path: Path, parent: Path) -> bool:
    """Return True if path is equal to or inside parent directory."""
    try:
        p_res = path.resolve()
        parent_res = parent.resolve()
        if p_res == parent_res:
            return True
        p_res.relative_to(parent_res)
        return True
    except (ValueError, RuntimeError, Exception):
        return False


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
        backlog_eligible_count = 0
        if baseline_count > 0:
            b_rows = connection.execute("SELECT path FROM filing_incoming_baseline").fetchall()
            active_props = {
                p["source_path"]: p["status"]
                for p in connection.execute("SELECT source_path, status FROM filing_proposals").fetchall()
            }
            for b_row in b_rows:
                b_path_str = b_row["path"]
                b_p = Path(b_path_str)
                if b_p.suffix.lower() in VIDEO_EXTENSIONS:
                    p_st = active_props.get(b_path_str)
                    if p_st in ("completed", "pending", "needs_recovery"):
                        continue
                    if b_p.is_file():
                        backlog_eligible_count += 1
        incoming_folders = get_configured_incoming_folders(config)

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
            is_file = p_obj.is_file()
            item["exists_on_disk"] = is_file

            # Check if physically located inside a configured incoming folder
            is_inside_incoming = True
            if incoming_folders:
                is_inside_incoming = any(
                    _is_subpath_of(p_obj.resolve(), Path(f).resolve())
                    for f in incoming_folders
                )
            item["is_in_incoming_folder"] = is_inside_incoming

            f_row = connection.execute(
                "SELECT scene_id, file_id, path FROM files WHERE path=? OR basename=?",
                (item["path"], p_obj.name)
            ).fetchone()
            item_scene_id = str(f_row["scene_id"]) if f_row and f_row["scene_id"] else None

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


def expect_filesystem_delete(database_path: Path, path: str, ttl_seconds: float = 60):
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


def cancel_expected_filesystem_delete(database_path: Path, path: str):
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


def expect_filesystem_create(database_path: Path, path: str, ttl_seconds: float = 120):
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
        return {
            "inventory": dict(inventory_row) if inventory_row else None,
            "rename_queue": queue_counts,
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
    for scene in scenes:
        performers = sorted(
            {p.get("name", "").strip() for p in scene.get("performers") or [] if p.get("name", "").strip()},
            key=str.casefold,
        )
        studio = (scene.get("studio") or {}).get("name")
        metadata = {
            "inventory_version": 1,
            "title": scene.get("title"), "details": scene.get("details"), "date": scene.get("date"),
            "director": scene.get("director"), "code": scene.get("code"), "rating100": scene.get("rating100"),
            "organized": scene.get("organized"), "studio": studio,
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


def is_file_on_unavailable_root(path_str: str, unavailable_roots=None) -> bool:
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


def get_authoritative_unavailable_roots(database_path: Path = None, extra_roots: list = None) -> list[str]:
    """Return the list of configured or monitored roots that are currently offline or unreachable.
    Probes filesystem directly; discards stale monitor status entries if the root is now accessible.
    """
    candidates = set()
    if extra_roots:
        for r in extra_roots:
            if r and str(r).strip():
                try:
                    candidates.add(str(Path(r).expanduser()))
                except Exception:
                    candidates.add(str(r))

    if database_path:
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


def inventory(database_path: Path, scenes, progress_callback=None) -> dict:
    now = utc_now()
    connection = connect(database_path)
    try:
        run_id = connection.execute(
            "INSERT INTO inventory_runs(started_at) VALUES (?)", (now,)
        ).lastrowid
        records = list(flatten_scene_files(scenes))
        total_records = len(records)
        if progress_callback:
            progress_callback(0, total_records)
        summary = {"scenes": len({record["scene_id"] for record in records}), "files": 0,
                   "present": 0, "missing": 0, "changed_paths": 0, "restored": 0}

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
                is_offline = is_file_on_unavailable_root(record["path"], unavailable_roots)

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
    connection = connect(database_path)
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

        for record in records:
            try:
                exists = os.path.isfile(record["path"])
            except OSError:
                exists = False
            previous = connection.execute("SELECT first_seen_at,missing_since,exists_on_disk FROM files WHERE file_id=?",
                                          (record["file_id"],)).fetchone()
            is_offline = False
            if not exists:
                is_offline = is_file_on_unavailable_root(record["path"], unavailable_roots)

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
                {**record, "exists_on_disk": int(exists),
                 "first_seen_at": previous["first_seen_at"] if previous else now, "last_seen_at": now,
                 "missing_since": missing_since},
            )
        connection.commit()
        return len(records)
    finally:
        connection.close()


def _row_scene_date(row) -> str:
    """Return a validated ISO Stash scene date from an inventoried file row."""
    try:
        metadata = json.loads(row["scene_metadata_json"] or "{}")
        value = str(metadata.get("date") or "").strip()
        if value and datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value:
            return value
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return ""


def scene_naming_signature(database_path: Path, scene_id: str, include_date: bool = False, include_quality: bool = False):
    """Return only metadata that is allowed to influence a filename."""
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT title,studio,performers_json,scene_metadata_json,height FROM files WHERE scene_id=? ORDER BY file_id LIMIT 1",
            (str(scene_id),),
        ).fetchone()
        if not row:
            return None
        signature = (row["title"] or "", row["studio"] or "", row["performers_json"] or "[]")
        if include_date:
            signature = signature + (_row_scene_date(row),)
        if include_quality:
            signature = signature + (_row_video_quality(row),)
        return signature
    finally:
        connection.close()


def _connect_short(database_path: Path) -> sqlite3.Connection:
    """Open a connection with a short write-lock timeout for hook-critical paths."""
    connection = sqlite3.connect(database_path, timeout=5)
    connection.row_factory = sqlite3.Row
    _ensure_schema(connection, database_path)
    return connection


def enqueue_rename(database_path: Path, scene_id: str, now_timestamp: float, debounce_seconds: float = 1.0) -> bool:
    """Coalesce a scene's hooks and return True only when a worker must be scheduled.

    Uses a 5-second write-lock timeout (instead of the default 30 s) so that a
    batch of simultaneous Stash hooks never stalls the UI for half a minute.
    """
    connection = _connect_short(database_path)
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
    connection = connect(database_path)
    try:
        connection.execute("DELETE FROM rename_queue WHERE scene_id=?", (str(scene_id),))
        changes = connection.execute("SELECT changes()").fetchone()[0]
        connection.commit()
    finally:
        connection.close()
    if changes > 0:
        record_activity(
            database_path, "rename", "automatic rename", "cancelled",
            scene_id=str(scene_id), detail="Pending rename cancelled by user; original filename preserved on disk"
        )
    return bool(changes)


def make_pending_rename_due(database_path: Path, scene_id: str) -> bool:
    connection = connect(database_path)
    try:
        connection.execute(
            "UPDATE rename_queue SET available_at=? WHERE scene_id=? AND status='pending'",
            (time.time() - 1.0, str(scene_id))
        )
        changes = connection.execute("SELECT changes()").fetchone()[0]
        connection.commit()
        return bool(changes)
    finally:
        connection.close()


def release_worker_schedule(database_path: Path):
    connection = connect(database_path)
    try:
        connection.execute("UPDATE rename_worker_state SET scheduled=0 WHERE id=1")
        connection.commit()
    finally:
        connection.close()


# Renames left in 'processing' longer than this are considered orphaned
# (e.g. monitor was force-quit mid-rename) and are reset to 'pending'.
_STALE_PROCESSING_SECONDS = 90


def claim_due_rename(database_path: Path, now_timestamp: float):
    connection = connect(database_path)
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
            (now_timestamp, scene_id)
        )
        connection.commit()
        return scene_id, None, None
    finally:
        connection.close()


def finish_queued_rename(database_path: Path, scene_id: str, outcome: str, detail: str):
    connection = connect(database_path)
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
    connection = connect(database_path)
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
    try:
        for fingerprint in json.loads(fingerprints_json or "[]"):
            if str(fingerprint.get("type", "")).lower() == fingerprint_type.lower():
                return str(fingerprint.get("value", "")).lower() or None
    except (TypeError, ValueError):
        return None
    return None


def opensubtitles_hash(path: Path):
    """Calculate the OpenSubtitles hash without reading the complete video."""
    chunk_size = 65536
    size = path.stat().st_size
    if size < chunk_size * 2:
        return None
    checksum = size
    with path.open("rb") as video:
        first = video.read(chunk_size)
        video.seek(-chunk_size, os.SEEK_END)
        last = video.read(chunk_size)
    for chunk in (first, last):
        usable = len(chunk) - (len(chunk) % 8)
        if usable:
            checksum += sum(struct.unpack(f"<{usable // 8}Q", chunk[:usable]))
    return f"{checksum & 0xFFFFFFFFFFFFFFFF:016x}"



CHECKSUM_STABILITY_SECONDS = 5.0
CHECKSUM_RETRY_BASE_SECONDS = 15.0
CHECKSUM_MAX_ACTIVE_JOBS = 8


def get_file_stat_snapshot(path: Path | str) -> tuple[int, int, int, int] | None:
    """Return a high-resolution (size, mtime_ns, device, inode) identity snapshot."""
    try:
        p = Path(path)
        st = p.stat()
        return (int(st.st_size), int(st.st_mtime_ns), int(st.st_dev), int(st.st_ino))
    except OSError:
        return None


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

    sha256 = calculate_sha256_verified(job["path"])
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


def calculate_sha256(path: Path | str, chunk_size: int = 65536) -> str | None:
    """Calculate the complete SHA-256 checksum for a file on disk."""
    return calculate_sha256_verified(path, chunk_size)


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

        sha256 = calculate_sha256_verified(p)
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
    now = utc_now()
    connection = connect(database_path)
    try:
        run_id = connection.execute(
            "INSERT INTO reconciliation_runs(started_at) VALUES (?)", (now,)
        ).lastrowid
        missing = connection.execute(
            "SELECT file_id,scene_id,path,size,fingerprints_json FROM files WHERE exists_on_disk=0 ORDER BY path"
        ).fetchall()
        known_paths = {
            os.path.normcase(os.path.abspath(row["path"])): row
            for row in connection.execute(
                "SELECT file_id,scene_id,path FROM files WHERE exists_on_disk=1"
            )
        }
        summary = {"missing": len(missing), "matched": 0, "ambiguous": 0, "unmatched": 0, "skipped_folders": 0}
        report = []

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
                try:
                    entries = list(folder.iterdir())
                except OSError:
                    entries = []
                for candidate in entries:
                    try:
                        normalized = os.path.normcase(os.path.abspath(candidate))
                        if (not candidate.is_file() or candidate.suffix.lower() not in VIDEO_EXTENSIONS
                                or candidate.stat().st_size != record["size"]):
                            continue
                        size_matches.append(candidate)
                    except OSError:
                        continue
                expected_hash = fingerprint_value(record["fingerprints_json"], "oshash")
                hash_matches = []
                if expected_hash:
                    for candidate in size_matches:
                        try:
                            if opensubtitles_hash(candidate) == expected_hash:
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

            connection.execute(
                """INSERT INTO reconciliation_candidates(run_id,file_id,scene_id,expected_path,candidate_path,
                       confidence,reason,expected_size,candidate_size,oshash_match,recorded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, result["file_id"], result["scene_id"], result["expected_path"], result["candidate_path"],
                 result["confidence"], result["reason"], result["expected_size"], result["candidate_size"],
                 result["oshash_match"], now),
            )
            report.append(result)

        connection.execute(
            """UPDATE reconciliation_runs SET completed_at=?,missing_count=?,matched_count=?,ambiguous_count=?,
                   unmatched_count=?,skipped_folder_count=?,status='complete' WHERE id=?""",
            (now, summary["missing"], summary["matched"], summary["ambiguous"], summary["unmatched"],
             summary["skipped_folders"], run_id),
        )
        connection.commit()
        summary["run_id"] = run_id
        return summary, report
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


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
    return re.sub(r"[^\w]+", " ", str(value or "").casefold(), flags=re.UNICODE).strip()


def _compact_filename_text(value: str) -> str:
    return re.sub(r"[^\w]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _same_filename_name(left: str, right: str) -> bool:
    """Treat spaced, compact and punctuation variants of a metadata name as equal."""
    return bool(_compact_filename_text(left)) and _compact_filename_text(left) == _compact_filename_text(right)


def _contains_name(text: str, name: str) -> bool:
    normalized_name = _normalized_filename_text(name)
    normalized_text = f" {_normalized_filename_text(text)} "
    if bool(normalized_name) and f" {normalized_name} " in normalized_text:
        return True
    compact_name = _compact_filename_text(name)
    return bool(compact_name) and compact_name in _compact_filename_text(text)


def _remove_legacy_studio(base: str, studio: str | None) -> str:
    """Remove an equivalent legacy [studio] or {studio} marker from a stable base."""
    if not studio:
        return base
    cleaned = re.sub(
        r"\s*[\[{]([^\[\]{}]+)[\]}]\s*",
        lambda match: " " if _same_filename_name(match.group(1), studio) else match.group(0),
        base,
    )
    return re.sub(r"\s+", " ", cleaned).strip(" -")


def _metadata_name_pattern(name: str, include_connectors: bool = False) -> str | None:
    """Return an exact-name regex that tolerates spaces/punctuation but not partial words."""
    parts = re.findall(r"\w+", str(name or ""), flags=re.UNICODE)
    if not parts:
        return None
    core = r"(?<!\w)" + r"[\W_]*".join(re.escape(part) for part in parts) + r"(?!\w)"
    if include_connectors:
        connectors = r"(?:\b(?:and|feat\.?|featuring|with|w/|vs\.?|versus|presents|in)\b|[&,+])"
        return rf"(?:{connectors}\s+)?{core}(?:\s+{connectors})?"
    return core


def _strip_managed_metadata(base: str, studios, performers, options: dict | None = None) -> str:
    """Remove only studio/performer names that Stash currently or previously identified as metadata."""
    opts = options or {}
    cleaned = str(base or "").strip()
    names = []
    seen = set()
    for value in list(studios or []) + list(performers or []):
        name = str(value or "").strip()
        key = _compact_filename_text(name)
        if name and key and key not in seen:
            seen.add(key)
            names.append(name)

    # Longest first prevents a shorter known name from consuming part of a longer one.
    names.sort(key=lambda value: len(_compact_filename_text(value)), reverse=True)
    strip_connectors = opts.get("stripConnectiveWords") is not False
    for name in names:
        pattern = _metadata_name_pattern(name, include_connectors=strip_connectors)
        if pattern:
            cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    # Remove empty legacy wrappers/groups and tidy punctuation left by extraction.
    cleaned = re.sub(r"\(\s*[,;&-]*\s*\)", " ", cleaned)
    cleaned = re.sub(r"\[\s*\]|\{\s*\}", " ", cleaned)
    cleaned = re.sub(r"\s*,\s*,+", ", ", cleaned)
    
    if opts.get("stripConnectiveWords") is not False:
        cleaned = re.sub(r"\s*[,;&+]*\s*(?:and|feat\.?|featuring|with|w/|vs\.?|versus|presents|in|&|\+)\s*[,;&+]*\s*-\s*", " - ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*-\s*[,;&+]*\s*(?:and|feat\.?|featuring|with|w/|vs\.?|versus|presents|in|&|\+)\s*[,;&+]*\s*", " - ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*-\s*[,;&+]*\s*(?:and|feat\.?|featuring|with|w/|vs\.?|versus|presents|in)?\s*[,;&+]*\s*(?=\s*[\(\[{]|$)", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(?i)\s+(?:feat\.?|featuring|with|and|w/|vs\.?|versus|presents|in|&|\+)\s*$", "", cleaned).strip(" -_,&")
        cleaned = re.sub(r"(?i)^\s*(?:feat\.?|featuring|with|and|w/|vs\.?|versus|presents|in|&|\+)\s+", "", cleaned).strip(" -_,&")
        if re.fullmatch(r"(?i)\s*(?:feat\.?|featuring|with|and|w/|vs\.?|versus|presents|in|&|\+|\-|,)+\s*", cleaned):
            cleaned = ""

    if opts.get("collapseMultipleDashes") is not False:
        cleaned = re.sub(r"(?:\s*-\s*){2,}", " - ", cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(" -_,&")
    return re.sub(r"\s+", " ", cleaned).strip()


def _remove_manual_metadata(base: str, studio: str | None, performers: list[str]) -> str:
    """Compatibility wrapper for older callers; may return an empty clean base."""
    return _strip_managed_metadata(base, [studio] if studio else [], performers)


def _is_officially_scraped(row) -> bool:
    """Return True if the scene has been matched to an external metadata provider (e.g. StashDB, The PornDB)."""
    if not row:
        return False
    try:
        meta_raw = row["scene_metadata_json"]
    except (IndexError, KeyError, TypeError):
        return False
    if not meta_raw:
        return False
    try:
        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        if isinstance(meta, dict):
            return bool(meta.get("stash_ids") or meta.get("urls"))
    except (TypeError, ValueError):
        pass
    return False


def _should_strip_metadata_from_title(filename_options: dict | None, row=None, title: str | None = None) -> bool:
    if not filename_options:
        strip_enabled = True
    else:
        val = filename_options.get("stripMetadataFromTitle")
        if val is None:
            strip_enabled = True
        elif isinstance(val, bool):
            strip_enabled = val
        elif isinstance(val, str):
            strip_enabled = val.strip().lower() in ("true", "1", "yes", "on")
        else:
            strip_enabled = bool(val)

    if not strip_enabled:
        return False

    # Rule 1 (Refined): Protect creative pairing titles (e.g. "Jade and Xander", "Austin and LeGrand")
    # on officially scraped scenes so legitimate creative pairings are not mutilated.
    # If the title contains release delimiters (hyphens or bracketed tags), release metadata
    # is safely stripped to avoid duplicating studios or performers.
    if row is not None and _is_officially_scraped(row):
        check_title = str(title or "").strip()
        if not check_title and hasattr(row, "__getitem__"):
            try:
                check_title = str(row["title"] or "").strip()
            except Exception:
                pass
        if check_title:
            has_delimiters = bool(re.search(r"\s*-\s*", check_title) or re.search(r"\[.*?\]|\(.*?\)", check_title))
            is_pairing = bool(re.search(r"(?i)(?:\b(?:and|with|meets|vs)\b|&)", check_title))
            if is_pairing and not has_delimiters:
                return False

    return True


def _sync_filename_state(connection, state, row, current: Path, performers: list[str], filename_options: dict | None = None):
    """Synchronize the stable base with current Stash metadata and the physical filename."""
    opts = filename_options or {}
    master_source = str(opts.get("masterTitleSource") or "stash_title").lower()
    now = utc_now()
    raw_stash_title = str(row["title"] or "").strip()
    current_studio = str(row["studio"] or "").strip() or None

    try:
        previous_performers = json.loads(state["manual_performers_json"] or "[]")
    except (TypeError, ValueError):
        previous_performers = []
    previous_studio = str(state["manual_studio"] or "").strip() or None

    base = str(state["base_stem"] or "").strip()
    source = str(state["base_source"] or "filename")
    source_title = state["source_title"]

    previous_keys = {_compact_filename_text(name) for name in previous_performers if str(name).strip()}
    current_keys = {_compact_filename_text(name) for name in performers if str(name).strip()}
    studio_changed = not _same_filename_name(previous_studio or "", current_studio or "")
    performers_changed = previous_keys != current_keys
    metadata_changed = studio_changed or performers_changed

    last_generated = str(state["last_generated_stem"] or "").strip()
    externally_changed = bool(last_generated and current.stem != last_generated)

    strip_title = _should_strip_metadata_from_title(opts, row, raw_stash_title)
    studios_to_strip = [value for value in (previous_studio, current_studio) if value]
    performers_to_strip = list(previous_performers) + list(performers)

    if master_source == "filename":
        if metadata_changed or externally_changed or source != "filename":
            base = current.stem
            source = "filename"
            source_title = None
        base = _strip_managed_metadata(base, studios_to_strip, performers_to_strip, opts)
    elif master_source == "strict_title":
        source = "title"
        source_title = raw_stash_title
        if raw_stash_title:
            if strip_title:
                cleaned = _strip_managed_metadata(raw_stash_title, studios_to_strip, performers_to_strip, opts)
                base = cleaned if len(cleaned) >= 2 else raw_stash_title
            else:
                base = raw_stash_title
        else:
            base = ""
    else:  # "stash_title" (default)
        if raw_stash_title:
            source = "title"
            source_title = raw_stash_title
            if strip_title:
                cleaned = _strip_managed_metadata(raw_stash_title, studios_to_strip, performers_to_strip, opts)
                base = cleaned if len(cleaned) >= 2 else raw_stash_title
            else:
                base = raw_stash_title
        else:
            if metadata_changed or externally_changed or source == "title":
                base = current.stem
                source = "filename"
                source_title = None
            base = _strip_managed_metadata(base, studios_to_strip, performers_to_strip, opts)

    managed_date = str(state["managed_date"] or "").strip() or _row_scene_date(row)
    managed_quality = str(dict(state).get("managed_quality") or "").strip() or _row_video_quality(row)
    connection.execute(
        """UPDATE filename_state SET base_stem=?,base_source=?,source_title=?,manual_studio=?,
           manual_performers_json=?,managed_date=?,managed_quality=?,updated_at=? WHERE file_id=?""",
        (base, source, source_title, current_studio, json.dumps(performers, ensure_ascii=False),
         managed_date or None, managed_quality or None, now, row["file_id"]),
    )
    return connection.execute("SELECT * FROM filename_state WHERE file_id=?", (row["file_id"],)).fetchone()


def _create_filename_state(connection, row, current: Path, performers: list[str], filename_options: dict | None = None):
    """Create filename state from Stash title when known, otherwise the existing physical stem."""
    opts = filename_options or {}
    master_source = str(opts.get("masterTitleSource") or "stash_title").lower()
    now = utc_now()
    raw_stash_title = str(row["title"] or "").strip()
    studio = str(row["studio"] or "").strip() or None

    if master_source == "filename":
        title = ""
        source = "filename"
        raw_base = current.stem
    elif master_source == "strict_title":
        title = raw_stash_title
        source = "title"
        raw_base = raw_stash_title
    else:  # "stash_title" (default)
        title = raw_stash_title
        source = "title" if title else "filename"
        raw_base = title or current.stem

    strip_title = _should_strip_metadata_from_title(opts, row, raw_base)
    if source == "title" and title:
        if strip_title:
            cleaned = _strip_managed_metadata(title, [studio] if studio else [], performers, opts)
            clean_base = cleaned if len(cleaned) >= 2 else title
        else:
            clean_base = title
    elif raw_base:
        clean_base = _strip_managed_metadata(raw_base, [studio] if studio else [], performers, opts)
    else:
        clean_base = ""

    connection.execute(
        """INSERT INTO filename_state(file_id,base_stem,base_source,source_title,last_generated_stem,
           manual_studio,manual_performers_json,managed_date,managed_quality,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (row["file_id"], clean_base, source, title or None, None, studio,
         json.dumps(performers, ensure_ascii=False), _row_scene_date(row) or None,
         _row_video_quality(row) or None, now, now),
    )
    return connection.execute("SELECT * FROM filename_state WHERE file_id=?", (row["file_id"],)).fetchone()


def _refresh_manual_filename_state(connection, state, row, performers: list[str]):
    """Compatibility entry point: normalize any stored base using previous/current metadata."""
    current = Path(row["path"])
    return _sync_filename_state(connection, state, row, current, performers)

def _derive_blank_title_base(stem: str, studio: str | None, performers: list[str]) -> str:
    """Remove only exact trailing metadata produced by supported filename formats."""
    base = _remove_legacy_studio(stem.strip(), studio)
    if performers:
        match = re.search(r"\s*-\s*\(([^()]*)\)\s*$", base)
        if match:
            existing = {_normalized_filename_text(value) for value in match.group(1).split(",") if value.strip()}
            expected = {_normalized_filename_text(value) for value in performers if value.strip()}
            if existing == expected:
                base = base[:match.start()].rstrip()
        else:
            performer_suffix = ", ".join(name.strip() for name in performers if name.strip())
            plain_match = re.search(rf"\s*-\s*{re.escape(performer_suffix)}\s*$", base, flags=re.IGNORECASE)
            if plain_match:
                base = base[:plain_match.start()].rstrip()
    if studio:
        match = re.search(r"\s*-\s*\{([^{}]*)\}\s*$", base)
        if match and _same_filename_name(match.group(1), studio):
            base = base[:match.start()].rstrip()
        else:
            plain_match = re.search(rf"\s*-\s*{re.escape(studio)}\s*$", base, flags=re.IGNORECASE)
            if plain_match:
                base = base[:plain_match.start()].rstrip()
    base = re.sub(r"(?:\s*-\s*){2,}", " - ", base).strip(" -")
    return base or stem.strip()


def filename_format_options(config: dict | None = None) -> dict:
    """Turn the UI's friendly filename choices into validated formatting values."""
    config = config or {}
    order = [value.strip() for value in str(config.get("filenameOrder") or "title,studio,performers").split(",")]
    if sorted(order) != ["performers", "studio", "title"]:
        order = ["title", "studio", "performers"]
    section_separators = {"dash": " - ", "comma": ", ", "space": " ", "underscore": "_"}
    performer_separators = {"comma": ", ", "space": " ", "dash": " - ", "ampersand": " & "}
    return {
        "order": order,
        "date_position": "end" if str(config.get("filenameDatePosition") or "beginning").lower() == "end" else "beginning",
        "quality_position": "beginning" if str(config.get("filenameQualityPosition") or "end").lower() == "beginning" else "end",
        "section_separator": section_separators.get(str(config.get("filenameSectionSeparator") or "dash"), " - "),
        "performer_separator": performer_separators.get(str(config.get("filenamePerformerSeparator") or "comma"), ", "),
    }


def _validated_video_quality(quality: int | str | None) -> str:
    """Return canonical quality token like '[1080p]' or empty string."""
    if quality is None:
        return ""
    if isinstance(quality, int):
        return f"[{quality}p]" if quality > 0 else ""
    s = str(quality).strip()
    if not s:
        return ""
    m = re.match(r"^\[?(\d+)[pP]?\]?$", s)
    if m:
        try:
            val = int(m.group(1))
            return f"[{val}p]" if val > 0 else ""
        except ValueError:
            pass
    return ""


def _strip_matching_video_quality(text: str, quality: str | int | None) -> str:
    """Remove matching resolution token at a clear title boundary without guessing at other resolutions."""
    cleaned = str(text or "").strip()
    tok = _validated_video_quality(quality)
    if not cleaned or not tok:
        return cleaned
    num = tok.strip("[]pP")
    prefix = re.compile(rf"^(?:\[{num}[pP]\]|\({num}[pP]\)|{num}[pP])(?:\s*[-–—_,.:]+\s*|\s+|$)", re.IGNORECASE)
    suffix = re.compile(rf"(?:^|\s+|\s*[-–—_,.:]+\s*)(?:\[{num}[pP]\]|\({num}[pP]\)|{num}[pP])$", re.IGNORECASE)
    cleaned = prefix.sub("", cleaned, count=1)
    cleaned = suffix.sub("", cleaned, count=1)
    return cleaned.strip(" -–—_,.:")


def _row_video_quality(row) -> str:
    """Extract validated video quality token e.g. '[1080p]' from a files table row or record."""
    try:
        if not row:
            return ""
        row_dict = dict(row)
        height = row_dict.get("height")
        if height is not None:
            return _validated_video_quality(height)
    except (ValueError, TypeError):
        pass
    return ""


def _validated_scene_date(scene_date: str | None) -> str:
    try:
        value = str(scene_date or "").strip()
        return value if value and datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value else ""
    except (TypeError, ValueError):
        return ""


def _matching_scene_date_variants(scene_date: str) -> list[str]:
    """Return supported textual forms of a known Stash date for exact deduplication only."""
    try:
        parsed = datetime.strptime(str(scene_date or ""), "%Y-%m-%d")
    except (TypeError, ValueError):
        return []
    values = [parsed.strftime(pattern) for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y", "%Y-%d-%m")]
    variants = []
    for value in values:
        for separator in ("-", ".", "_"):
            candidate = value.replace("-", separator)
            if candidate not in variants:
                variants.append(candidate)
    return variants


def _strip_matching_scene_date(text: str, scene_date: str) -> str:
    """Remove the known scene date at a clear title boundary without guessing at other dates."""
    cleaned = str(text or "").strip()
    variants = _matching_scene_date_variants(scene_date)
    if not cleaned or not variants:
        return cleaned
    alternatives = "|".join(re.escape(value) for value in sorted(variants, key=len, reverse=True))
    prefix = re.compile(rf"^(?:{alternatives})(?:\s*[-–—_,.:]+\s*|\s+|$)", re.IGNORECASE)
    suffix = re.compile(rf"(?:^|\s+|\s*[-–—_,.:]+\s*)(?:{alternatives})$", re.IGNORECASE)
    cleaned = prefix.sub("", cleaned, count=1)
    cleaned = suffix.sub("", cleaned, count=1)
    return cleaned.strip(" -–—_,.:")



def _sanitize_filename_stem(stem: str, options: dict | None = None) -> str:
    """Remove or replace illegal filesystem characters with clean punctuation."""
    opts = options or {}
    cleaned = re.sub(r'', '', str(stem or ''))  # strip null bytes first
    cleaned = re.sub(r'[?"*]', '', cleaned)
    cleaned = re.sub(r'[/\:<>|]', '-', cleaned)
    cleaned = re.sub(r'\s*-\s*([!.,;])', r' \1', cleaned)
    if opts.get("collapseMultipleDashes") is not False:
        cleaned = re.sub(r'(?:\s*-\s*){2,}', ' - ', cleaned)
    return re.sub(r'\s+', ' ', cleaned).strip(' .')


def _is_only_metadata_or_connectors(text: str, studio: str | None, performers: list[str], options: dict | None = None) -> bool:
    """Check if text consists exclusively of studio, performers, and connective words/punctuation."""
    opts = options or {}
    cleaned = str(text or "").strip()
    if not cleaned:
        return True
    names = []
    if studio and str(studio).strip() and opts.get("stripStudioFromTitle") is not False:
        names.append(str(studio).strip())
    if opts.get("stripPerformersFromTitle") is not False:
        for p in performers:
            if p and str(p).strip():
                names.append(str(p).strip())
    names.sort(key=lambda s: len(s), reverse=True)
    for name in names:
        escaped = re.escape(name)
        cleaned = re.sub(rf"(?i)\b{escaped}\b", " ", cleaned)
        compact_escaped = "".join(re.escape(c) + r"\s*" for c in name if c.isalnum())
        if compact_escaped:
            cleaned = re.sub(rf"(?i)\b{compact_escaped}\b", " ", cleaned)

    connectors = r"(?i)\b(and|feat\.?|featuring|with|w/|vs\.?|versus|presents|in)\b|[&,+_–—\-\(\)\[\]\{\}\.\s]"
    cleaned = re.sub(connectors, " ", cleaned)
    return len(cleaned.strip()) == 0


def _proposed_stem(base: str, studio: str | None, performers: list[str], options: dict | None = None,
                   scene_date: str | None = None, previous_scene_date: str | None = None,
                   video_quality: str | int | None = None, previous_video_quality: str | int | None = None) -> str:
    """Build one canonical filename from the stored base + current Stash metadata."""
    formatting = filename_format_options(options)
    opts = options or {}
    
    title_val = str(base or "").strip()
    date_val = _validated_scene_date(scene_date) if opts.get("includeSceneDate") is True else ""
    if date_val:
        previous_date = _validated_scene_date(previous_scene_date)
        if previous_date and previous_date != date_val:
            title_val = _strip_matching_scene_date(title_val, previous_date)
        title_val = _strip_matching_scene_date(title_val, date_val)
    
    quality_val = _validated_video_quality(video_quality) if opts.get("includeVideoQuality") is True else ""
    if quality_val:
        if previous_video_quality:
            prev_quality = _validated_video_quality(previous_video_quality)
            if prev_quality and prev_quality != quality_val:
                title_val = _strip_matching_video_quality(title_val, prev_quality)
        title_val = _strip_matching_video_quality(title_val, quality_val)
    
    include_studio = opts.get("includeStudio") is not False
    studio_val = str(studio or "").strip() if include_studio else ""
    
    include_performers = opts.get("includePerformers") is not False
    raw_performers = [name.strip() for name in performers if str(name).strip()]
    max_perfs = int(opts.get("maxPerformersInFilename") or 0)
    if max_perfs > 0:
        raw_performers = raw_performers[:max_perfs]
    
    perf_val = formatting["performer_separator"].join(raw_performers) if include_performers else ""

    # Check granular title cleaning rules
    clean_perfs_only = opts.get("cleanPerformerOnlyTitles") is not False
    if clean_perfs_only and _is_only_metadata_or_connectors(title_val, studio_val, raw_performers, opts):
        title_val = ""
    else:
        studios_to_strip = [studio.strip()] if (opts.get("stripStudioFromTitle") is not False and studio and str(studio).strip()) else []
        perfs_to_strip = raw_performers if opts.get("stripPerformersFromTitle") is not False else []
        if studios_to_strip or perfs_to_strip:
            title_val = _strip_managed_metadata(title_val, studios_to_strip, perfs_to_strip, opts)

    values = {
        "title": title_val,
        "studio": studio_val,
        "performers": perf_val,
    }
    parts = [values[field] for field in formatting["order"] if values[field]]
    if date_val:
        parts.append(date_val) if formatting["date_position"] == "end" else parts.insert(0, date_val)
    if quality_val:
        if formatting["quality_position"] == "beginning":
            if date_val and formatting["date_position"] == "beginning":
                parts.insert(1, quality_val)
            else:
                parts.insert(0, quality_val)
        else:
            parts.append(quality_val)
    proposed = formatting["section_separator"].join(parts)
    return _sanitize_filename_stem(proposed, opts)


def preview_safe_filenames(database_path: Path, filename_options: dict | None = None) -> tuple[dict, list[dict]]:
    """Persist stable clean bases and calculate proposed paths; never rename a file."""
    now = utc_now()
    connection = connect(database_path)
    try:
        run_id = connection.execute("INSERT INTO filename_preview_runs(started_at) VALUES (?)", (now,)).lastrowid
        rows = connection.execute("SELECT * FROM files WHERE exists_on_disk=1 ORDER BY file_id").fetchall()
        provisional = []
        target_counts = {}
        for row in rows:
            current = Path(row["path"])
            performers = json.loads(row["performers_json"] or "[]")
            state = connection.execute("SELECT * FROM filename_state WHERE file_id=?", (row["file_id"],)).fetchone()
            if state is None:
                state = _create_filename_state(connection, row, current, performers, filename_options)
            else:
                state = _sync_filename_state(connection, state, row, current, performers, filename_options)

            if state and (dict(state).get("rename_protected") or False):
                base = str(state["base_stem"] or current.stem)
                proposed = current
                normalized_target = os.path.normcase(os.path.abspath(current))
                target_counts[normalized_target] = target_counts.get(normalized_target, 0) + 1
                provisional.append((row, current, proposed, base, normalized_target))
                continue

            base = str(state["base_stem"] or "").strip()
            proposed_stem = _proposed_stem(base, row["studio"], performers, filename_options,
                                            _row_scene_date(row), state["managed_date"],
                                            _row_video_quality(row), dict(state).get("managed_quality"))
            # If all metadata and the clean base are empty, preserve the current stem rather than
            # proposing an invalid/empty filename.
            if not proposed_stem:
                proposed_stem = current.stem
            proposed = current.with_name(proposed_stem + current.suffix)
            normalized_target = os.path.normcase(os.path.abspath(proposed))
            target_counts[normalized_target] = target_counts.get(normalized_target, 0) + 1
            provisional.append((row, current, proposed, base, normalized_target))

        report = []
        summary = {"examined": len(provisional), "proposed": 0, "unchanged": 0, "conflicts": 0}
        for row, current, proposed, base, normalized_target in provisional:
            st = connection.execute("SELECT rename_protected FROM filename_state WHERE file_id=?", (row["file_id"],)).fetchone()
            is_prot = st and st["rename_protected"]
            if is_prot:
                status, reason = "unchanged", "Filename is protected from automatic renaming by Automatic Filing"
            elif len(proposed.name.encode("utf-8")) > 255:
                status, reason = "conflict", "Proposed filename exceeds 255 UTF-8 bytes"
            elif target_counts[normalized_target] > 1:
                status, reason = "conflict", "Safely skipped: Multiple files share the same filename target (collision protected)"
            elif proposed != current and proposed.exists():
                status, reason = "conflict", "A different filesystem entry already uses the target path"
            elif proposed == current:
                status, reason = "unchanged", "Current filename already matches the safe format"
            else:
                status, reason = "proposed", "Safe rename candidate"
            summary["conflicts" if status == "conflict" else status] += 1
            item = {"file_id": row["file_id"], "scene_id": row["scene_id"], "current_path": str(current),
                    "base_stem": base, "proposed_path": str(proposed), "status": status, "reason": reason,
                    "action_performed": False}
            report.append(item)
            connection.execute(
                "INSERT INTO filename_previews(run_id,file_id,scene_id,current_path,base_stem,proposed_path,status,reason,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, row["file_id"], row["scene_id"], str(current), base, str(proposed), status, reason, now),
            )
        connection.execute(
            "UPDATE filename_preview_runs SET completed_at=?,examined_count=?,proposed_count=?,unchanged_count=?,conflict_count=?,status='complete' WHERE id=?",
            (now, summary["examined"], summary["proposed"], summary["unchanged"], summary["conflicts"], run_id),
        )
        connection.commit()
        summary["run_id"] = run_id
        return summary, report
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def preview_scene_filename(database_path: Path, scene_id: str, filename_options: dict | None = None, ignore_protection: bool = False) -> dict:
    """Return the latest calculated filename proposal for one scene."""
    opts = filename_options or {}
    skip_protection = ignore_protection or opts.get("ignore_protection") is True
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM files WHERE scene_id=? AND exists_on_disk=1 ORDER BY file_id LIMIT 1",
            (str(scene_id),),
        ).fetchone()
        if not row:
            return {"scene_id": str(scene_id), "status": "blocked", "reason": "No present inventoried file for this scene"}

        current = Path(row["path"])
        performers = json.loads(row["performers_json"] or "[]")
        state = connection.execute("SELECT * FROM filename_state WHERE file_id=?", (row["file_id"],)).fetchone()
        if state is None:
            state = _create_filename_state(connection, row, current, performers, filename_options)
        else:
            state = _sync_filename_state(connection, state, row, current, performers, filename_options)
        connection.commit()

        if not skip_protection and state and (dict(state).get("rename_protected") or False):
            return {
                "scene_id": str(scene_id),
                "file_id": row["file_id"],
                "current_path": str(current),
                "proposed_path": str(current),
                "base_stem": state["base_stem"],
                "status": "unchanged",
                "reason": "Filename is protected from automatic renaming by Automatic Filing",
                "associated_files": [],
                "scene_date": _row_scene_date(row),
                "action_performed": False
            }

        proposed_stem = _proposed_stem(state["base_stem"], row["studio"], performers, filename_options,
                                        _row_scene_date(row), state["managed_date"],
                                        _row_video_quality(row), dict(state).get("managed_quality"))
        if not proposed_stem:
            proposed_stem = current.stem
        proposed = current.with_name(proposed_stem + current.suffix)
        if len(proposed.name.encode("utf-8")) > 255:
            status, reason = "blocked", "Proposed filename exceeds 255 UTF-8 bytes"
        elif proposed != current and proposed.exists():
            status, reason = "blocked", "Target path already exists"
        elif proposed == current:
            status, reason = "unchanged", "Current filename already matches"
        else:
            status, reason = "ready", "Single-scene rename passed preflight"
        sidecars = []
        if status == "ready" and current.parent.exists():
            for candidate in current.parent.iterdir():
                if not candidate.is_file() or candidate == current:
                    continue
                target = associated_file_target(candidate, current, proposed)
                if target is None:
                    continue
                if target.exists():
                    status, reason = "blocked", f"Associated-file target already exists: {target.name}"
                    break
                sidecars.append({"source": str(candidate), "target": str(target)})
        return {"scene_id": str(scene_id), "file_id": row["file_id"], "current_path": str(current),
                "proposed_path": str(proposed), "base_stem": state["base_stem"], "status": status,
                "reason": reason, "associated_files": sidecars, "scene_date": _row_scene_date(row),
                "video_quality": _row_video_quality(row),
                "action_performed": False}
    finally:
        connection.close()

def apply_scene_filename(database_path: Path, scene_id: str, move_file, filename_options: dict | None = None, ignore_protection: bool = False) -> dict:
    """Apply one preflighted rename through a supplied Stash move callback."""
    opts = filename_options or {}
    skip_protection = ignore_protection or opts.get("ignore_protection") is True
    with rename_lock(database_path):
        preview = preview_scene_filename(database_path, scene_id, filename_options, ignore_protection=skip_protection)
        if preview.get("status") != "ready":
            return preview
        moved_sidecars = []
        video_renamed = False
        try:
            for item in preview["associated_files"]:
                source, target = Path(item["source"]), Path(item["target"])
                expect_filesystem_move(database_path, str(source), str(target))
                source.rename(target)
                moved_sidecars.append((source, target))
            current = Path(preview["current_path"])
            proposed = Path(preview["proposed_path"])
            expect_filesystem_move(database_path, str(current), str(proposed))
            result = move_file(preview["file_id"], str(current.parent), proposed.name)
            if result is False or result is None:
                raise RuntimeError("Stash did not confirm the file rename")
            video_renamed = True
            connection = None
            try:
                connection = connect(database_path)
                connection.execute(
                    "UPDATE filename_state SET last_generated_stem=?,managed_date=?,managed_quality=?,updated_at=? WHERE file_id=?",
                    (proposed.stem, preview.get("scene_date") or None, preview.get("video_quality") or None, utc_now(), preview["file_id"]),
                )
                connection.execute(
                    "UPDATE files SET path=?,basename=?,exists_on_disk=1,last_seen_at=? WHERE file_id=?",
                    (str(proposed), proposed.name, utc_now(), preview["file_id"]),
                )
                for source, target in moved_sidecars:
                    connection.execute(
                        "UPDATE incoming_files SET path=?, last_checked_at=? WHERE path=?",
                        (str(target), utc_now(), str(source))
                    )
                connection.commit()
            except Exception as database_error:
                if connection is not None:
                    connection.rollback()
                return {**preview, "status": "renamed_with_warning",
                        "reason": f"Stash renamed the video, but Watchtower must refresh its local record: {database_error}",
                        "renamed_sidecars": [{"source": str(source), "target": str(target)} for source, target in moved_sidecars],
                        "action_performed": True, "local_cache_updated": False}
            finally:
                if connection is not None:
                    connection.close()
            # Sidecars are part of the same successful rename transaction, but make
            # them visible in Recent Activity as separate informational entries.
            # Logging is deliberately best-effort so a log write can never turn a
            # completed file rename into a failed/rollback operation.
            for source, target in moved_sidecars:
                try:
                    record_activity(
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
            return {**preview, "status": "renamed", "reason": "Stash confirmed the rename",
                    "renamed_sidecars": [{"source": str(source), "target": str(target)} for source, target in moved_sidecars],
                    "action_performed": True, "local_cache_updated": True}
        except Exception:
            if not video_renamed:
                for source, target in reversed(moved_sidecars):
                    if target.exists() and not source.exists():
                        expect_filesystem_move(database_path, str(target), str(source))
                        target.rename(source)
            raise


def preview_manual_filename(database_path: Path, scene_id: str, requested_name: str) -> dict:
    """Preflight one exact user-supplied basename without changing anything."""
    requested_name = str(requested_name or "").strip()
    if not requested_name or Path(requested_name).name != requested_name or requested_name in (".", ".."):
        return {"scene_id": str(scene_id), "status": "blocked", "reason": "Enter a filename, not a path",
                "action_performed": False}
    connection = connect(database_path)
    try:
        row = connection.execute("SELECT * FROM files WHERE scene_id=? AND exists_on_disk=1 ORDER BY file_id LIMIT 1",
                                 (str(scene_id),)).fetchone()
        if not row:
            return {"scene_id": str(scene_id), "status": "blocked", "reason": "No present inventoried file for this scene",
                    "action_performed": False}
        current = Path(row["path"])
        supplied = Path(requested_name)
        if supplied.suffix and supplied.suffix.casefold() != current.suffix.casefold():
            return {"scene_id": str(scene_id), "file_id": row["file_id"], "current_path": str(current),
                    "status": "blocked", "reason": f"The video extension must remain {current.suffix}", "action_performed": False}
        stem = supplied.stem if supplied.suffix else requested_name
        stem = _sanitize_filename_stem(stem)
        proposed = current.with_name(stem + current.suffix)
        if not stem:
            status, reason = "blocked", "The filename cannot be empty"
        elif len(proposed.name.encode("utf-8")) > 255:
            status, reason = "blocked", "Proposed filename exceeds 255 UTF-8 bytes"
        elif proposed != current and proposed.exists():
            status, reason = "blocked", "Target path already exists"
        elif proposed == current:
            status, reason = "unchanged", "The filename already matches"
        else:
            status, reason = "ready", "Manual correction passed preflight"
        sidecars = []
        if status == "ready":
            for candidate in current.parent.iterdir():
                if not candidate.is_file() or candidate == current:
                    continue
                target = associated_file_target(candidate, current, proposed)
                if target is None:
                    continue
                if target.exists():
                    status, reason = "blocked", f"Associated-file target already exists: {target.name}"
                    break
                sidecars.append({"source": str(candidate), "target": str(target)})
        return {"scene_id": str(scene_id), "file_id": row["file_id"], "current_path": str(current),
                "proposed_path": str(proposed), "status": status, "reason": reason,
                "associated_files": sidecars, "metadata_studio": row["studio"],
                "metadata_performers": json.loads(row["performers_json"] or "[]"), "action_performed": False}
    finally:
        connection.close()


def apply_manual_filename(database_path: Path, scene_id: str, requested_name: str, move_file) -> dict:
    """Apply one exact previewed correction and make it the scene's new filename base."""
    with rename_lock(database_path):
        preview = preview_manual_filename(database_path, scene_id, requested_name)
        if preview.get("status") != "ready":
            return preview
        moved_sidecars = []
        video_renamed = False
        try:
            for item in preview["associated_files"]:
                source, target = Path(item["source"]), Path(item["target"])
                expect_filesystem_move(database_path, str(source), str(target))
                source.rename(target)
                moved_sidecars.append((source, target))
            current, proposed = Path(preview["current_path"]), Path(preview["proposed_path"])
            expect_filesystem_move(database_path, str(current), str(proposed))
            result = move_file(preview["file_id"], str(current.parent), proposed.name)
            if result is False or result is None:
                raise RuntimeError("Stash did not confirm the file rename")
            video_renamed = True
            connection = None
            try:
                connection = connect(database_path)
                connection.execute("UPDATE files SET path=?,basename=?,exists_on_disk=1,last_seen_at=? WHERE file_id=?",
                                   (str(proposed), proposed.name, utc_now(), preview["file_id"]))
                connection.execute(
                    """INSERT INTO filename_state(file_id,base_stem,base_source,source_title,last_generated_stem,
                           manual_studio,manual_performers_json,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET base_stem=excluded.base_stem,
                       base_source='manual',source_title=NULL,last_generated_stem=excluded.last_generated_stem,
                       manual_studio=excluded.manual_studio,manual_performers_json=excluded.manual_performers_json,
                       updated_at=excluded.updated_at""",
                    (preview["file_id"], proposed.stem, "manual", None, proposed.stem, preview.get("metadata_studio"),
                     json.dumps(preview.get("metadata_performers") or [], ensure_ascii=False), utc_now(), utc_now()),
                )
                connection.commit()
            except Exception as database_error:
                if connection is not None:
                    connection.rollback()
                return {**preview, "status": "renamed_with_warning",
                        "reason": f"Stash renamed the video, but Watchtower must refresh its local record: {database_error}",
                        "renamed_sidecars": [{"source": str(source), "target": str(target)} for source, target in moved_sidecars],
                        "action_performed": True, "local_cache_updated": False}
            finally:
                if connection is not None:
                    connection.close()
            # Log successful companion-file renames separately so manual filename
            # corrections show their JPG/other sidecar updates in Recent Activity.
            # Keep this best-effort: logging must never undo a completed rename.
            for source, target in moved_sidecars:
                try:
                    record_activity(
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
            return {**preview, "status": "renamed", "reason": "Stash confirmed the manual correction",
                    "renamed_sidecars": [{"source": str(source), "target": str(target)} for source, target in moved_sidecars],
                    "action_performed": True, "local_cache_updated": True}
        except Exception:
            if not video_renamed:
                for source, target in reversed(moved_sidecars):
                    if target.exists() and not source.exists():
                        expect_filesystem_move(database_path, str(target), str(source))
                        target.rename(source)
            raise


def find_csm_font():
    for f in [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]:
        if os.path.exists(f):
            return f
    return "Helvetica"


def format_csm_duration(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def ffmpeg_supports_drawtext(ffmpeg_bin):
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-h", "filter=drawtext"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    output = f"{result.stdout}\n{result.stderr}"
    return result.returncode == 0 and "Filter drawtext" in output


def generate_ffmpeg_contact_sheet_fallback(
    ffmpeg_bin, video_file, dest_path, tmp_dir, timestamps, cols, rows,
    scale_w, include_banner, width, height, file_size_str, formatted_duration,
    taskpolicy_prefix,
):
    """Render a contact sheet without ImageMagick using bundled font assets."""
    font_source = Path(__file__).with_name("assets") / "Roboto-Regular.ttf"
    if not font_source.is_file():
        return {"status": "error", "error": "Bundled contact-sheet font is missing"}
    if not ffmpeg_supports_drawtext(ffmpeg_bin):
        return {
            "status": "error",
            "error": "ImageMagick is unavailable and this FFmpeg build does not support the drawtext filter",
        }

    font_file = tmp_dir / "watchtower-font.ttf"
    shutil.copy2(font_source, font_file)
    errors = []
    frames = []
    for i, ts in enumerate(timestamps):
        label_file = tmp_dir / f"timestamp_{i:03d}.txt"
        label_file.write_text(format_csm_duration(ts), encoding="utf-8")
        frame_path = tmp_dir / f"frame_{i:03d}.png"
        command = taskpolicy_prefix + [
            ffmpeg_bin, "-y", "-ss", f"{ts:.2f}", "-i", str(video_file),
            "-an", "-sn", "-vf",
            (
                f"scale={scale_w}:-1,setsar=1,"
                f"drawtext=fontfile={font_file.name}:textfile={label_file.name}:"
                "fontsize=14:fontcolor=white:box=1:boxcolor=black@0.6:"
                "boxborderw=2:x=w-tw-12:y=h-th-12"
            ),
            "-frames:v", "1", "-compression_level", "3", str(frame_path),
        ]
        result = subprocess.run(
            command, cwd=tmp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if result.returncode == 0 and frame_path.is_file():
            frames.append(frame_path)
        else:
            errors.append(
                f"frame {i} @{ts:.1f}s: ffmpeg rc={result.returncode} "
                + result.stderr.decode(errors="replace").strip()[-100:]
            )

    if len(frames) < max(2, len(timestamps) // 2):
        detail = "; ".join(errors[:3]) if errors else "unknown"
        return {
            "status": "error",
            "error": f"Failed to extract enough frames ({len(frames)}/{len(timestamps)}): {detail}",
        }

    montage_path = tmp_dir / "montage.png"
    montage_command = taskpolicy_prefix + [
        ffmpeg_bin, "-y", "-framerate", "1", "-i", str(tmp_dir / "frame_%03d.png"),
        "-vf", f"tile={cols}x{rows}:padding=8:margin=4:color=0xF5F6F8",
        "-frames:v", "1", "-compression_level", "3", str(montage_path),
    ]
    montage_result = subprocess.run(
        montage_command, cwd=tmp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if montage_result.returncode != 0 or not montage_path.is_file():
        detail = montage_result.stderr.decode(errors="replace").strip()[-200:]
        return {"status": "error", "error": f"FFmpeg montage creation failed: {detail or 'unknown error'}"}

    final_filter = "format=yuvj420p"
    if include_banner:
        title_file = tmp_dir / "title.txt"
        info_file = tmp_dir / "info.txt"
        title_file.write_text(video_file.name, encoding="utf-8")
        info_file.write_text(
            f"Resolution: {width}x{height}  |  Size: {file_size_str}  |  Duration: {formatted_duration}",
            encoding="utf-8",
        )
        final_filter = (
            "pad=iw:ih+95:0:90:color=0xF5F6F8,"
            f"drawtext=fontfile={font_file.name}:textfile={title_file.name}:"
            "fontsize=18:fontcolor=0x2D3748:x=40:y=16,"
            f"drawtext=fontfile={font_file.name}:textfile={info_file.name}:"
            "fontsize=18:fontcolor=0x2D3748:x=40:y=50,format=yuvj420p"
        )

    final_path = tmp_dir / "contact-sheet.jpg"
    final_command = taskpolicy_prefix + [
        ffmpeg_bin, "-y", "-i", str(montage_path), "-vf", final_filter,
        "-frames:v", "1", "-q:v", "3", str(final_path),
    ]
    final_result = subprocess.run(
        final_command, cwd=tmp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if final_result.returncode != 0 or not final_path.is_file():
        detail = final_result.stderr.decode(errors="replace").strip()[-200:]
        return {"status": "error", "error": f"FFmpeg banner creation failed: {detail or 'unknown error'}"}

    shutil.copy2(final_path, dest_path)
    return {"status": "generated", "frames": len(frames), "renderer": "ffmpeg"}


def generate_video_contact_sheet(
    video_path,
    output_path=None,
    grid="4x4",
    include_banner=True,
    adjust_vertical=True,
    custom_script=None,
    allow_custom_script=False,
    overwrite=False,
    logger=None
):
    video_file = Path(video_path).resolve()
    if not video_file.is_file():
        return {"status": "error", "error": f"Video not found: {video_file}"}

    # Standard companion file name: <video.ext>.jpg
    dest_path = Path(output_path) if output_path else Path(f"{video_file}.jpg")
    if dest_path.exists() and not overwrite:
        return {"status": "skipped", "message": "Contact sheet already exists", "path": str(dest_path)}

    # Custom scripts are trusted local executable code and require an explicit switch.
    if custom_script and allow_custom_script:
        script_path = Path(custom_script).expanduser().resolve()
        if not script_path.is_file():
            return {"status": "error", "error": f"Custom script is not a regular file: {script_path}"}
        if os.name != "nt" and not os.access(script_path, os.X_OK):
            return {"status": "error", "error": f"Custom script is not executable: {script_path}"}
        cmd = [str(script_path), str(video_file)]
        if shutil.which("taskpolicy") or os.path.exists("/usr/sbin/taskpolicy"):
            cmd = ["/usr/sbin/taskpolicy", "-b"] + cmd
        try:
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if completed.returncode:
                return {"status": "error", "error":
                        f"Custom script exited with code {completed.returncode}: {(completed.stderr or '').strip()}"}
            if dest_path.exists() or Path(f"{video_file.stem}.jpg").exists():
                actual = dest_path if dest_path.exists() else Path(f"{video_file.stem}.jpg")
                return {"status": "generated", "path": str(actual), "custom_script": True}
            return {"status": "error", "error": "Custom script completed but did not create the expected contact sheet"}
        except Exception as e:
            return {"status": "error", "error": f"Custom script error: {e}"}

    # Locate ffmpeg, ffprobe and optional ImageMagick across supported systems.
    ffmpeg_bin = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else "/usr/bin/ffmpeg" if os.path.exists("/usr/bin/ffmpeg") else "ffmpeg")
    ffprobe_bin = shutil.which("ffprobe") or ("/opt/homebrew/bin/ffprobe" if os.path.exists("/opt/homebrew/bin/ffprobe") else "/usr/bin/ffprobe" if os.path.exists("/usr/bin/ffprobe") else "ffprobe")
    magick_bin = shutil.which("magick") or ("/opt/homebrew/bin/magick" if os.path.exists("/opt/homebrew/bin/magick") else "/usr/bin/magick" if os.path.exists("/usr/bin/magick") else "magick")

    ffmpeg_available = bool(shutil.which(ffmpeg_bin) or os.path.exists(ffmpeg_bin))
    ffprobe_available = bool(shutil.which(ffprobe_bin) or os.path.exists(ffprobe_bin))
    magick_available = bool(shutil.which(magick_bin) or os.path.exists(magick_bin))
    missing_required = [
        name for name, available in (("ffmpeg", ffmpeg_available), ("ffprobe", ffprobe_available))
        if not available
    ]
    if missing_required:
        return {"status": "error", "error": f"Required tool not found: {', '.join(missing_required)}"}

    # Probe metadata
    probe_cmd = [
        ffprobe_bin, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,display_aspect_ratio",
        "-show_entries", "format=duration,size",
        "-of", "json", str(video_file)
    ]
    try:
        p_res = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
        p_data = json.loads(p_res.stdout)
    except Exception as e:
        return {"status": "error", "error": f"Could not probe video: {e}"}

    streams = p_data.get("streams", [{}])
    stream = streams[0] if streams else {}
    width = int(stream.get("width") or 1920)
    height = int(stream.get("height") or 1080)
    duration_str = stream.get("duration") or p_data.get("format", {}).get("duration") or "0"
    try:
        duration = float(duration_str)
    except (TypeError, ValueError):
        duration = 0.0

    if duration <= 10:
        return {"status": "skipped", "message": f"Video too short ({duration:.1f}s)"}

    # Parse grid
    try:
        parts = grid.lower().split("x")
        cols = int(parts[0])
        rows = int(parts[1])
    except Exception:
        cols, rows = 4, 4

    # Use Display Aspect Ratio when available — raw pixel dimensions are wrong
    # for videos with non-square pixels (e.g. 1440x1080 DVD rips with SAR 4:3).
    dar = stream.get("display_aspect_ratio") or ""
    try:
        dar_parts = [int(x) for x in dar.split(":")]
        display_ratio = dar_parts[0] / dar_parts[1] if len(dar_parts) == 2 and dar_parts[1] else None
    except (ValueError, ZeroDivisionError):
        display_ratio = None
    is_vertical = (display_ratio < 1.0) if display_ratio is not None else (height > width)
    if is_vertical and adjust_vertical:
        cols, rows = max(cols, 6), min(rows, 3)

    total_frames = cols * rows

    formatted_duration = format_csm_duration(duration)
    size_bytes = video_file.stat().st_size
    if size_bytes >= 1024**3:
        file_size_str = f"{size_bytes / (1024**3):.1f} GB"
    elif size_bytes >= 1024**2:
        file_size_str = f"{size_bytes / (1024**2):.1f} MB"
    else:
        file_size_str = f"{size_bytes / 1024:.0f} KB"

    font_path = find_csm_font()
    has_taskpolicy = bool(shutil.which("taskpolicy") or os.path.exists("/usr/sbin/taskpolicy"))
    taskpolicy_prefix = ["/usr/sbin/taskpolicy", "-b"] if has_taskpolicy else []

    # Frame sampling avoiding black intro & outro
    start_sec = duration * 0.05
    end_sec = duration * 0.92
    step_sec = (end_sec - start_sec) / max(1, total_frames - 1)
    timestamps = [start_sec + i * step_sec for i in range(total_frames)]

    scale_w = 460 if cols <= 4 else (376 if cols == 5 else 310)
    t_start = time.time()

    with tempfile.TemporaryDirectory(prefix="watchtower_csm_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)

        if not magick_available:
            fallback = generate_ffmpeg_contact_sheet_fallback(
                ffmpeg_bin, video_file, dest_path, tmp_dir, timestamps, cols, rows,
                scale_w, include_banner, width, height, file_size_str, formatted_duration,
                taskpolicy_prefix,
            )
            if fallback.get("status") != "generated":
                return fallback
            elapsed = time.time() - t_start
            return {
                "status": "generated",
                "path": str(dest_path),
                "grid": f"{cols}x{rows}",
                "frames": fallback["frames"],
                "duration": formatted_duration,
                "resolution": f"{width}x{height}",
                "elapsed_seconds": round(elapsed, 2),
                "renderer": "ffmpeg",
            }

        _csm_errors: list[str] = []
        for i, ts in enumerate(timestamps):
            frame_path = tmp_dir / f"frame_{i:03d}.jpg"
            cmd = taskpolicy_prefix + [
                ffmpeg_bin, "-y",
                "-ss", f"{ts:.2f}",
                "-i", str(video_file),
                "-an", "-sn",
                "-vf", f"scale={scale_w}:-1",
                "-vframes", "1",
                str(frame_path)
            ]
            _fr = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if _fr.returncode != 0 and not frame_path.is_file():
                _csm_errors.append(
                    f"frame {i} @{ts:.1f}s: ffmpeg rc={_fr.returncode} "
                    + _fr.stderr.decode(errors="replace").strip()[-100:]
                )

            if frame_path.is_file():
                ts_str = format_csm_duration(ts)
                stamp_cmd = taskpolicy_prefix + [
                    magick_bin, str(frame_path),
                    "-fill", "white",
                    "-font", font_path,
                    "-pointsize", "14",
                    "-gravity", "southeast",
                    "-undercolor", "rgba(0,0,0,0.6)",
                    "-annotate", "+12+12", ts_str,
                    str(frame_path)
                ]
                _sr = subprocess.run(stamp_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                if _sr.returncode != 0:
                    _csm_errors.append(
                        f"stamp {i}: magick rc={_sr.returncode} "
                        + _sr.stderr.decode(errors="replace").strip()[-80:]
                    )

        frames = sorted(glob.glob(str(tmp_dir / "frame_*.jpg")))
        if len(frames) < max(2, total_frames // 2):
            err_detail = "; ".join(_csm_errors[:3]) if _csm_errors else "unknown"
            return {"status": "error",
                    "error": f"Failed to extract enough frames ({len(frames)}/{total_frames}): {err_detail}"}

        # Montage assembly
        temp_out = tmp_dir / "montage.jpg"
        montage_cmd = taskpolicy_prefix + [
            magick_bin, "montage"
        ] + frames + [
            "-background", "#F5F6F8",
            "-font", font_path,
            "-geometry", "+4+4",
            "-tile", f"{cols}x{rows}",
            "-strip", "-quality", "85",
            str(temp_out)
        ]
        _mr = subprocess.run(montage_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        if not temp_out.is_file():
            _merr = _mr.stderr.decode(errors="replace").strip()[-200:]
            return {"status": "error",
                    "error": f"Montage creation failed: {_merr or 'unknown magick error'}"}

        # Banner addition
        if include_banner:
            id_cmd = [magick_bin, "identify", "-format", "%w", str(temp_out)]
            w_res = subprocess.run(id_cmd, capture_output=True, text=True)
            try:
                sheet_w = int(w_res.stdout.strip())
            except Exception:
                sheet_w = 1840

            banner_h = 240 if is_vertical else 90
            banner_font_size = 36 if is_vertical else 18
            banner_splice = 40 if is_vertical else 24

            info_text = f"{video_file.name}\nResolution: {width}x{height}  |  Size: {file_size_str}  |  Duration: {formatted_duration}"

            banner_cmd = taskpolicy_prefix + [
                magick_bin,
                "-background", "#F5F6F8",
                "-fill", "#2D3748",
                "-font", font_path,
                "-pointsize", str(banner_font_size),
                "-size", f"{sheet_w - 24}x{banner_h}",
                "-gravity", "west",
                f"label:{info_text}",
                "-background", "#F5F6F8",
                "-splice", f"{banner_splice}x0",
                str(temp_out),
                "-append", "-strip", "-quality", "85",
                str(temp_out)
            ]
            subprocess.run(banner_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            footer_cmd = taskpolicy_prefix + [
                magick_bin, str(temp_out),
                "-size", f"{sheet_w}x5", "xc:#F5F6F8",
                "-append", "-strip", "-quality", "85",
                str(temp_out)
            ]
            subprocess.run(footer_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        shutil.copy2(temp_out, dest_path)

    elapsed = time.time() - t_start
    return {
        "status": "generated",
        "path": str(dest_path),
        "grid": f"{cols}x{rows}",
        "frames": len(frames),
        "duration": formatted_duration,
        "resolution": f"{width}x{height}",
        "elapsed_seconds": round(elapsed, 2),
        "renderer": "imagemagick",
    }


# ---------------------------------------------------------------------------
# Automatic Filing (Phase 1)
# ---------------------------------------------------------------------------

NOISE_ALIASES = {
    "the", "and", "or", "in", "on", "at", "to", "for", "of", "with", "by",
    "a", "an", "all", "is", "it", "this", "that", "from", "into",
    "hd", "4k", "1080p", "720p", "2160p", "uhd", "sd", "dvd", "bluray", "rip",
    "scene", "part", "video", "clip", "movie", "film", "show", "series", "episode",
    "star", "stars", "girl", "girls", "boy", "boys", "man", "men", "guy", "guys",
    "top", "best", "hot", "live", "club", "vip", "pro", "new", "raw", "cut",
    "xxx", "porn", "sex", "action", "vr", "bonus", "extra", "trailer"
}


def _build_token_regex(phrase: str) -> re.Pattern | None:
    tokens = [re.escape(t) for t in re.split(r'[\s_\-\.]+', phrase.strip()) if t]
    if not tokens:
        return None
    pattern_str = r'(?<![a-zA-Z0-9])' + r'[\s_\-\.]+'.join(tokens) + r'(?![a-zA-Z0-9])'
    return re.compile(pattern_str, re.IGNORECASE)


def _single_token_alias_is_standalone(target: str, start: int, end: int) -> bool:
    """Accept a one-word alias only when it occupies a complete filename/title segment.

    This prevents an alias such as ``Sam`` from claiming the distinct apparent
    name ``Sam Taylor``. Multi-word aliases retain the normal token matching
    rules. Segments are separated by common filename credit delimiters.
    """
    left = target[:start]
    right = target[end:]
    left_parts = re.split(r'\s+(?:[-–—&,+|/\\])\s+|[\(\)\[\]\{\};|]', left)
    right_parts = re.split(r'\s+(?:[-–—&,+|/\\])\s+|[\(\)\[\]\{\};|]', right)
    segment = f"{left_parts[-1] if left_parts else ''}{target[start:end]}{right_parts[0] if right_parts else ''}"
    return len(re.findall(r"[A-Za-z0-9]+", segment)) == 1


def _normalize_name_for_folder_match(s: str) -> str:
    return re.sub(r'[\s_\-\.]+', ' ', str(s or "")).strip().lower()


def _compact_name_for_folder_match(s: str) -> str:
    """Remove supported separators for conservative joined-word folder matching."""
    return re.sub(r'[\s_\-\.]+', '', str(s or "")).strip().lower()


def _strip_conservative_folder_descriptors(norm_name: str) -> str:
    """Conservatively strip known prefix/suffix descriptors ('The ', ' Collection', 'The ... Collection')
    and parenthetical/bracketed annotations to support folder patterns like
    'The Cole Bentley Collection' or 'The Kyle Polaski (Michal Stranik, Damien Porch) Collection'
    without arbitrary fuzzy matching."""
    s = norm_name.strip()
    if s.startswith("the "):
        s = s[4:].strip()
    if s.endswith(" collection"):
        s = s[:-11].strip()
    s_no_paren = re.sub(r'[\(\[\{].*?[\)\]\}]', '', s).strip()
    s_no_paren = re.sub(r'\s+', ' ', s_no_paren)
    return s_no_paren or s


def _filter_subsumed_matches(raw_matches: list[dict]) -> dict[str, dict]:
    """Prune matches that are strictly subsumed by longer canonical matches,
    or where a canonical name match shares the exact same span as an alias.
    Preserves ambiguity when genuinely separate entities match, or when conflicting
    identities match the same span."""
    surviving = []
    for m in raw_matches:
        s, e = m["span"]
        subsumed = False
        for other in raw_matches:
            if other["entity"]["id"] == m["entity"]["id"]:
                continue
            os, oe = other["span"]

            # If other is a canonical-name match strictly enclosing m (e.g. shorter alias or subtoken)
            if other["is_name"] and os <= s and oe >= e and (oe - os) > (e - s):
                subsumed = True
                break

            # If both are aliases and other strictly encloses m
            if not m["is_name"] and os <= s and oe >= e and (oe - os) > (e - s):
                subsumed = True
                break

            # Exact same span: prefer complete canonical name over alias
            if os == s and oe == e and other["is_name"] and not m["is_name"]:
                subsumed = True
                break

        if not subsumed:
            surviving.append(m)

    unique = {}
    for m in surviving:
        ent_id = m["entity"]["id"]
        if ent_id not in unique or (m["is_name"] and not unique[ent_id]["is_name"]):
            unique[ent_id] = m
    return unique


def match_performer_for_filing(scene: dict, filename: str, all_performers: list[dict], match_source: str = "metadata_first") -> dict:
    """Conservatively match performers for automatic filing.
    - An uncertain entity match must NEVER produce a proposal.
    - Reliably parses single or multiple distinct co-starring performers.
    - Prefer complete canonical-name matches over shorter aliases contained within the same span.
    - Preserves ambiguity when conflicting identities match the exact same or overlapping text span."""
    if match_source in ("metadata_first", "metadata_only"):
        scene_performers = scene.get("performers") or []
        if len(scene_performers) >= 1:
            p_list = [{"id": str(p["id"]), "name": p["name"]} for p in scene_performers if p.get("id") and p.get("name")]
            if p_list:
                return {
                    "matched": True,
                    "entities": p_list,
                    "entity": p_list[0],
                    "matched_alias": None,
                    "source": "metadata"
                }
        elif match_source == "metadata_only":
            return {"matched": False, "reason": "No performer tagged in scene metadata"}

    target_str = f"{Path(filename).stem} | {scene.get('title') or ''}".strip(" |")
    if not target_str:
        return {"matched": False, "reason": "No filename or title to match"}

    alias_owner = {}
    ambiguous_aliases = set()
    for p in all_performers:
        p_id = str(p.get("id"))
        aliases = p.get("alias_list") or []
        for alias in aliases:
            a_norm = alias.strip().lower()
            if not a_norm:
                continue
            if a_norm in alias_owner and alias_owner[a_norm] != p_id:
                ambiguous_aliases.add(a_norm)
            else:
                alias_owner[a_norm] = p_id

    raw_matches = []
    for p in all_performers:
        p_id = str(p.get("id"))
        p_name = (p.get("name") or "").strip()
        if not p_name:
            continue

        name_norm = p_name.lower()
        if len(name_norm) >= 3 and name_norm not in NOISE_ALIASES:
            rx = _build_token_regex(p_name)
            if rx:
                for m in rx.finditer(target_str):
                    raw_matches.append({
                        "entity": {"id": p_id, "name": p_name},
                        "matched_alias": None,
                        "span": (m.start(), m.end()),
                        "is_name": True
                    })

        aliases = p.get("alias_list") or []
        for alias in aliases:
            a_norm = alias.strip().lower()
            if len(a_norm) < 3 or a_norm in NOISE_ALIASES or a_norm in ambiguous_aliases:
                continue
            rx = _build_token_regex(alias)
            if rx:
                for m in rx.finditer(target_str):
                    if len(re.findall(r"[A-Za-z0-9]+", alias)) == 1 and not _single_token_alias_is_standalone(target_str, m.start(), m.end()):
                        continue
                    raw_matches.append({
                        "entity": {"id": p_id, "name": p_name},
                        "matched_alias": alias.strip(),
                        "span": (m.start(), m.end()),
                        "is_name": False
                    })

    matched_performers = _filter_subsumed_matches(raw_matches)

    if len(matched_performers) == 0:
        return {"matched": False, "reason": "No matching performer found in filename"}

    # Check for genuine overlapping span collisions between different performers
    matches_list = list(matched_performers.values())
    for i in range(len(matches_list)):
        for j in range(i + 1, len(matches_list)):
            m1, m2 = matches_list[i], matches_list[j]
            s1, e1 = m1["span"]
            s2, e2 = m2["span"]
            if max(s1, s2) < min(e1, e2):
                return {
                    "matched": False,
                    "reason": f"Ambiguous performer match in filename ({m1['entity']['name']} vs {m2['entity']['name']})"
                }

    entities = [m["entity"] for m in matches_list]
    return {
        "matched": True,
        "entities": entities,
        "entity": entities[0],
        "matches": matches_list,
        "matched_alias": matches_list[0]["matched_alias"] if len(entities) == 1 else None,
        "source": "filename"
    }


def match_studio_for_filing(scene: dict, filename: str, all_studios: list[dict], match_source: str = "metadata_first") -> dict:
    """Conservatively match a studio for automatic filing.
    - Prefer complete canonical-name matches over shorter aliases contained within the same span.
    - Preserve ambiguity when genuinely separate studios match, or when conflicting identities match the same span."""
    if match_source in ("metadata_first", "metadata_only"):
        scene_studio = scene.get("studio")
        if scene_studio and scene_studio.get("id") and scene_studio.get("name"):
            return {
                "matched": True,
                "entity": {"id": str(scene_studio["id"]), "name": scene_studio["name"]},
                "matched_alias": None,
                "source": "metadata"
            }
        elif match_source == "metadata_only":
            return {"matched": False, "reason": "No studio tagged in scene metadata"}

    target_str = f"{Path(filename).stem} | {scene.get('title') or ''}".strip(" |")
    if not target_str:
        return {"matched": False, "reason": "No filename or title to match"}

    alias_owner = {}
    ambiguous_aliases = set()
    for s in all_studios:
        s_id = str(s.get("id"))
        aliases = s.get("aliases") or []
        for alias in aliases:
            a_norm = alias.strip().lower()
            if not a_norm:
                continue
            if a_norm in alias_owner and alias_owner[a_norm] != s_id:
                ambiguous_aliases.add(a_norm)
            else:
                alias_owner[a_norm] = s_id

    raw_matches = []
    for s in all_studios:
        s_id = str(s.get("id"))
        s_name = (s.get("name") or "").strip()
        if not s_name:
            continue

        name_norm = s_name.lower()
        if len(name_norm) >= 3 and name_norm not in NOISE_ALIASES:
            rx = _build_token_regex(s_name)
            if rx:
                for m in rx.finditer(target_str):
                    raw_matches.append({
                        "entity": {"id": s_id, "name": s_name},
                        "matched_alias": None,
                        "span": (m.start(), m.end()),
                        "is_name": True
                    })

        aliases = s.get("aliases") or []
        for alias in aliases:
            a_norm = alias.strip().lower()
            if len(a_norm) < 3 or a_norm in NOISE_ALIASES or a_norm in ambiguous_aliases:
                continue
            rx = _build_token_regex(alias)
            if rx:
                for m in rx.finditer(target_str):
                    if len(re.findall(r"[A-Za-z0-9]+", alias)) == 1 and not _single_token_alias_is_standalone(target_str, m.start(), m.end()):
                        continue
                    raw_matches.append({
                        "entity": {"id": s_id, "name": s_name},
                        "matched_alias": alias.strip(),
                        "span": (m.start(), m.end()),
                        "is_name": False
                    })

    matched_studios = _filter_subsumed_matches(raw_matches)

    if len(matched_studios) == 0:
        return {"matched": False, "reason": "No matching studio found in filename"}
    elif len(matched_studios) > 1:
        names = [m["entity"]["name"] for m in matched_studios.values()]
        return {"matched": False, "reason": f"Multiple studios matched in filename ({', '.join(names[:3])}); ambiguous destination"}
    else:
        match = list(matched_studios.values())[0]
        return {
            "matched": True,
            "entity": match["entity"],
            "matched_alias": match["matched_alias"],
            "source": "filename"
        }


def get_configured_filing_destination_roots(config: dict | None) -> list[str]:
    """Return a cleaned, deduplicated list of configured destination roots for automatic filing."""
    if not config or not isinstance(config, dict):
        return []
    explicit_raw = config.get("autoFilingDestinationRoots")
    has_explicit = bool(explicit_raw) or bool(config.get("autoFilingDestinationRoot"))
    override_setting = config.get("autoFilingDestinationRootsOverride")
    use_explicit = override_setting is True or (override_setting is None and has_explicit)
    raw = explicit_raw if use_explicit else config.get("_libraryRoots")
    roots = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, (str, Path)) and str(item).strip():
                roots.append(str(item).strip())
    elif isinstance(raw, str) and raw.strip():
        for chunk in raw.splitlines():
            for part in chunk.split(","):
                part_str = part.strip()
                if part_str:
                    roots.append(part_str)

    # Fallback to legacy single root setting if roots is empty
    if not roots and use_explicit:
        legacy = str(config.get("autoFilingDestinationRoot") or "").strip()
        if legacy:
            roots.append(legacy)

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for r in roots:
        norm = os.path.normpath(r)
        if norm not in seen:
            seen.add(norm)
            deduped.append(r)
    return deduped


def get_filing_folder_mappings(database_path: Path) -> list[dict]:
    """Return all custom entity-to-folder mappings."""
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM filing_folder_mappings ORDER BY entity_type, entity_name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        connection.close()


def save_filing_folder_mapping(
    database_path: Path,
    entity_type: str,
    entity_id: str,
    entity_name: str,
    folder_path: str,
    configured_roots: list[str] | None = None
) -> tuple[bool, str]:
    """Validate and save a custom folder mapping for a Stash performer, studio, or tag.
    Enforces:
    - Entity type must be 'performer', 'studio', or 'tag'.
    - Entity ID and name must be non-empty.
    - Folder path must exist on disk as a directory.
    - Folder path must be located inside one of configured destination roots (if roots provided).
    - Multiple entities can share the same destination folder, including cross-type mappings.
    - Never creates or renames folders automatically.
    """
    etype = (entity_type or "").strip().lower()
    if etype not in ("performer", "studio", "tag"):
        return False, f"Invalid entity type: '{entity_type}'. Must be 'performer', 'studio', or 'tag'."
    eid = str(entity_id or "").strip()
    if not eid:
        return False, "Entity ID is required."
    ename = str(entity_name or "").strip()
    if not ename:
        return False, "Entity name is required."
    fstr = str(folder_path or "").strip()
    if not fstr:
        return False, "Folder path is required."

    try:
        fpath = Path(fstr).expanduser().resolve()
    except Exception as exc:
        return False, f"Invalid folder path '{fstr}': {exc}"

    if not fpath.is_dir():
        return False, f"Mapped folder does not exist on disk: '{fstr}'. Watchtower never creates folders automatically."

    # Validate that folder is within one of the configured destination roots
    if configured_roots:
        is_inside_root = False
        resolved_folder_str = str(fpath)
        for root in configured_roots:
            if not root or not str(root).strip():
                continue
            try:
                root_res = str(Path(root).expanduser().resolve())
                if resolved_folder_str == root_res or resolved_folder_str.startswith(root_res + os.sep):
                    is_inside_root = True
                    break
            except Exception:
                continue
        if not is_inside_root:
            roots_str = ", ".join(f"'{r}'" for r in configured_roots if str(r).strip())
            return False, f"Mapped folder '{fstr}' is outside configured destination roots ({roots_str})."

    connection = connect(database_path)
    try:
        now = utc_now()
        existing = connection.execute(
            "SELECT id FROM filing_folder_mappings WHERE entity_type=? AND entity_id=?",
            (etype, eid)
        ).fetchone()

        if existing:
            connection.execute(
                "UPDATE filing_folder_mappings SET entity_name=?, folder_path=?, updated_at=? WHERE id=?",
                (ename, str(fpath), now, existing["id"])
            )
        else:
            connection.execute(
                """INSERT INTO filing_folder_mappings
                   (entity_type, entity_id, entity_name, folder_path, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (etype, eid, ename, str(fpath), now, now)
            )
        connection.commit()
        invalidate_destination_dir_cache()
        record_activity(
            database_path, "config", "folder mapping saved", "complete",
            detail=f"Saved custom folder mapping for {etype} '{ename}' -> {fpath.name}"
        )
        return True, "Mapping saved successfully."
    finally:
        connection.close()


def delete_filing_folder_mapping(database_path: Path, mapping_id: int) -> bool:
    """Delete a custom folder mapping."""
    connection = connect(database_path)
    try:
        row = connection.execute("SELECT * FROM filing_folder_mappings WHERE id=?", (mapping_id,)).fetchone()
        if not row:
            return False
        connection.execute("DELETE FROM filing_folder_mappings WHERE id=?", (mapping_id,))
        connection.commit()
        invalidate_destination_dir_cache()
        record_activity(
            database_path, "config", "folder mapping deleted", "complete",
            detail=f"Deleted custom folder mapping for {row['entity_type']} '{row['entity_name']}'"
        )
        return True
    finally:
        connection.close()


_INCOMING_DISCOVERY_CACHE: dict[str, tuple[float, list[dict]]] = {}
_INCOMING_DISCOVERY_CACHE_LOCK = threading.Lock()
_INCOMING_DISCOVERY_CACHE_TTL = 3600.0  # 1 hour; explicit recheck remains available


def invalidate_incoming_discovery_cache(root_path: str | Path | None = None) -> None:
    """Invalidate cached incoming directory scan results in memory."""
    with _INCOMING_DISCOVERY_CACHE_LOCK:
        if root_path is None:
            _INCOMING_DISCOVERY_CACHE.clear()
        else:
            try:
                norm_target = os.path.normcase(str(Path(root_path).expanduser())).replace(chr(92), "/")
                keys_to_remove = [k for k in _INCOMING_DISCOVERY_CACHE if norm_target in k]
                for k in keys_to_remove:
                    _INCOMING_DISCOVERY_CACHE.pop(k, None)
            except Exception:
                _INCOMING_DISCOVERY_CACHE.clear()


def discover_incoming_files_cached(
    incoming_folders: list[str],
    active_incoming_states: dict[str, str] | None = None,
    force_refresh: bool = False,
    ttl: float = _INCOMING_DISCOVERY_CACHE_TTL
) -> list[dict]:
    """Scan configured incoming folders for actionable completed files with a 1-hour cache.
    - Reuses cached discovery when roots match and within TTL.
    - Explicit force_refresh or TTL expiry rescans available incoming folders.
    - Filters out active incomplete lifecycle states and temporary download files.
    """
    if not incoming_folders:
        return []
    
    active_states = active_incoming_states or {}
    normalized_roots = sorted([
        os.path.normcase(str(Path(f).expanduser())).replace(chr(92), "/")
        for f in incoming_folders if f and str(f).strip()
    ])
    cache_key = json.dumps(normalized_roots)
    now_mono = time.monotonic()

    current_mtimes = {}
    for f in incoming_folders:
        try:
            folder = Path(f).expanduser()
            if folder.is_dir():
                current_mtimes[str(folder)] = folder.stat().st_mtime_ns
        except Exception:
            pass

    if not force_refresh:
        with _INCOMING_DISCOVERY_CACHE_LOCK:
            cached = _INCOMING_DISCOVERY_CACHE.get(cache_key)
            if cached:
                cached_mono, cached_mtimes, cached_entries = cached
                if now_mono - cached_mono < ttl and cached_mtimes == current_mtimes:
                    return [
                        dict(e) for e in cached_entries
                        if is_actionable_incoming_file(e["path"], active_states.get(e["path"]))
                    ]

    fresh_entries = []
    seen_paths = set()
    for incoming_folder in incoming_folders:
        folder = Path(incoming_folder).expanduser()
        if not folder.is_dir():
            continue
        try:
            for root, _dirs, filenames in os.walk(folder):
                for filename in filenames:
                    current_path = Path(root) / filename
                    current_path_str = str(current_path)
                    if current_path_str in seen_paths:
                        continue
                    if not is_actionable_incoming_file(current_path, active_states.get(current_path_str)):
                        continue
                    try:
                        stat = current_path.stat()
                    except OSError:
                        continue
                    seen_paths.add(current_path_str)
                    fresh_entries.append({
                        "path": current_path_str,
                        "size": stat.st_size,
                        "modified_ns": stat.st_mtime_ns,
                        "oshash": None,
                        "seen_at": utc_now(),
                    })
        except OSError:
            continue

    with _INCOMING_DISCOVERY_CACHE_LOCK:
        _INCOMING_DISCOVERY_CACHE[cache_key] = (now_mono, current_mtimes, fresh_entries)

    return [dict(e) for e in fresh_entries]


_DESTINATION_DIR_CACHE: dict[tuple[str, int], tuple[float, list[tuple[Path, str]]]] = {}
_DESTINATION_DIR_CACHE_LOCK = threading.Lock()
_DESTINATION_DIR_CACHE_TTL = 3600.0  # 1 hour; explicit folder refresh remains available
_CACHE_GENERATION: int = 0


def invalidate_destination_dir_cache(root_path: str | Path | None = None, database_path: Path | None = None) -> None:
    """Invalidate cached destination directories in memory and persistent database cache."""
    global _CACHE_GENERATION
    with _DESTINATION_DIR_CACHE_LOCK:
        _CACHE_GENERATION += 1
        if root_path is None:
            _DESTINATION_DIR_CACHE.clear()
        else:
            try:
                norm_key = str(Path(root_path).expanduser().resolve())
                keys_to_remove = [k for k in _DESTINATION_DIR_CACHE if k[0] == norm_key]
                for k in keys_to_remove:
                    _DESTINATION_DIR_CACHE.pop(k, None)
            except Exception:
                pass

    if database_path:
        try:
            conn = connect(database_path)
            try:
                if root_path is None:
                    conn.execute("DELETE FROM filing_destination_dir_cache")
                    conn.execute("DELETE FROM filing_destination_cache_meta")
                else:
                    norm_key = str(Path(root_path).expanduser().resolve())
                    conn.execute("DELETE FROM filing_destination_dir_cache WHERE root_path=?", (norm_key,))
                    conn.execute("DELETE FROM filing_destination_cache_meta WHERE root_path=?", (norm_key,))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("Failed invalidating database destination cache: %s", exc)


def refresh_destination_dir_cache(
    database_path: Path,
    roots: list[str],
    max_depth: int = 4
) -> dict:
    """Explicitly rescan destination roots on disk and store a fresh snapshot in SQLite database."""
    safe_depth = max(1, min(8, int(max_depth)))
    total_folders = 0
    scanned_roots = []

    invalidate_destination_dir_cache(database_path=database_path)

    conn = connect(database_path)
    try:
        now_str = utc_now()
        now_mono = time.monotonic()
        for root_str in roots:
            if not root_str or not str(root_str).strip():
                continue
            try:
                root_path = Path(root_str).expanduser().resolve()
            except Exception:
                continue
            if not root_path.is_dir():
                continue

            root_key = str(root_path)
            entries = _scan_destination_subdirectories(root_path, safe_depth)

            with _DESTINATION_DIR_CACHE_LOCK:
                _DESTINATION_DIR_CACHE[(root_key, safe_depth)] = (now_mono, entries)

            conn.execute("DELETE FROM filing_destination_dir_cache WHERE root_path=?", (root_key,))
            conn.execute("DELETE FROM filing_destination_cache_meta WHERE root_path=?", (root_key,))

            rows = [
                (root_key, str(p), norm, len(p.parts) - len(root_path.parts))
                for p, norm in entries
            ]
            if rows:
                conn.executemany(
                    "INSERT INTO filing_destination_dir_cache (root_path, dir_path, norm_name, depth) VALUES (?, ?, ?, ?)",
                    rows
                )
            conn.execute(
                "INSERT INTO filing_destination_cache_meta (root_path, max_depth, scanned_at, entry_count, generation) VALUES (?, ?, ?, ?, ?)",
                (root_key, safe_depth, now_str, len(entries), _CACHE_GENERATION)
            )
            conn.commit()
            total_folders += len(entries)
            scanned_roots.append(root_key)

        record_activity(
            database_path, "config", "folders refreshed", "complete",
            detail=f"Refreshed destination folders snapshot: found {total_folders} directories across {len(scanned_roots)} root(s)"
        )
        return {
            "success": True,
            "total_folders": total_folders,
            "scanned_roots": scanned_roots,
            "scanned_at": now_str
        }
    finally:
        conn.close()


_EXCLUDED_DESTINATION_DISCOVERY_NAMES = {
    ".ds_store", ".git", ".stfolder", ".stash", ".thumbnails",
    "@eadir", "lost+found", ".bin", ".trashes", ".temporaryitems",
    ".bin", ".recycle", "recycle.bin",
    "_orphaned_covers", "orphaned_covers",
    "deleted_mismatched_covers",
    "_recovery", "recovery",
    "_watchtower", "watchtower",
}


def _is_excluded_destination_discovery_dir(dirname: str) -> bool:
    d_lower = dirname.strip().lower()
    if not d_lower:
        return True
    if d_lower.startswith((".", "_orphaned", "orphaned_", "_recovery", "_watchtower", "deleted_mismatched", "$", "@")):
        return True
    if d_lower in _EXCLUDED_DESTINATION_DISCOVERY_NAMES:
        return True
    return False


def _scan_destination_subdirectories(
    root_path: Path,
    max_depth: int = 4
) -> list[tuple[Path, str]]:
    """Recursively scan a destination root up to a bounded depth for existing subdirectories.
    Skips hidden/system directories, maintenance, recovery, and orphaned-cover directories.
    In-place filtering of dirnames ensures all descendants of excluded directories are skipped.
    Returns list of (resolved_path, normalized_name) tuples."""
    results: list[tuple[Path, str]] = []
    try:
        root_resolved = root_path.expanduser().resolve()
        if not root_resolved.is_dir():
            return []
        base_depth = len(root_resolved.parts)

        for dirpath, dirnames, _ in os.walk(str(root_resolved), topdown=True, followlinks=False):
            curr_path = Path(dirpath)
            current_depth = len(curr_path.parts) - base_depth
            if current_depth >= max_depth:
                dirnames.clear()
                continue

            # Safely filter out excluded directories and all their descendants in-place
            dirnames[:] = [
                d for d in dirnames
                if not _is_excluded_destination_discovery_dir(d)
            ]

            for d in dirnames:
                p = curr_path / d
                norm_name = _normalize_name_for_folder_match(d)
                results.append((p.resolve(), norm_name))
    except Exception as exc:
        logger.debug("Failed scanning destination subdirectories in %s: %s", root_path, exc)

    return results


def _get_cached_destination_subdirectories(
    root_path: Path,
    max_depth: int = 4,
    ttl: float = _DESTINATION_DIR_CACHE_TTL,
    database_path: Path | None = None
) -> list[tuple[Path, str]]:
    """Retrieve subdirectories for a root from memory cache or persistent SQLite cache,
    or scan and store if missing/expired."""
    safe_depth = max(1, min(8, int(max_depth)))
    try:
        root_resolved = root_path.expanduser().resolve()
        if not root_resolved.is_dir():
            return []
        root_key = str(root_resolved)
    except Exception:
        return _scan_destination_subdirectories(root_path, safe_depth)

    cache_key = (root_key, safe_depth)
    now_mono = time.monotonic()

    # 1. Fast in-memory check
    with _DESTINATION_DIR_CACHE_LOCK:
        cached = _DESTINATION_DIR_CACHE.get(cache_key)
        if cached is not None:
            cached_time, entries = cached
            if now_mono - cached_time < ttl:
                return entries

    # 2. Check persistent SQLite cache across process boundaries
    if database_path:
        try:
            conn = connect(database_path)
            try:
                meta = conn.execute(
                    "SELECT max_depth, scanned_at, entry_count, generation FROM filing_destination_cache_meta WHERE root_path=?",
                    (root_key,)
                ).fetchone()
                if meta and meta["max_depth"] >= safe_depth and meta["generation"] >= _CACHE_GENERATION:
                    try:
                        scanned_dt = datetime.fromisoformat(meta["scanned_at"])
                        age_sec = (datetime.now(timezone.utc) - scanned_dt).total_seconds()
                    except Exception:
                        age_sec = 0
                    if age_sec < ttl:
                        rows = conn.execute(
                            "SELECT dir_path, norm_name FROM filing_destination_dir_cache WHERE root_path=? AND depth <= ?",
                            (root_key, safe_depth)
                        ).fetchall()
                        entries = [(Path(r["dir_path"]), r["norm_name"]) for r in rows]
                        with _DESTINATION_DIR_CACHE_LOCK:
                            _DESTINATION_DIR_CACHE[cache_key] = (now_mono, entries)
                        return entries
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("Error reading SQLite destination cache: %s", exc)

    # 3. Cache miss or expired: scan destination root on disk
    fresh_entries = _scan_destination_subdirectories(root_resolved, safe_depth)
    with _DESTINATION_DIR_CACHE_LOCK:
        _DESTINATION_DIR_CACHE[cache_key] = (now_mono, fresh_entries)

    # Populate SQLite persistent cache
    if database_path:
        try:
            conn = connect(database_path)
            try:
                now_str = utc_now()
                conn.execute("DELETE FROM filing_destination_dir_cache WHERE root_path=?", (root_key,))
                conn.execute("DELETE FROM filing_destination_cache_meta WHERE root_path=?", (root_key,))
                rows = [
                    (root_key, str(p), norm, len(p.parts) - len(root_resolved.parts))
                    for p, norm in fresh_entries
                ]
                if rows:
                    conn.executemany(
                        "INSERT INTO filing_destination_dir_cache (root_path, dir_path, norm_name, depth) VALUES (?, ?, ?, ?)",
                        rows
                    )
                conn.execute(
                    "INSERT INTO filing_destination_cache_meta (root_path, max_depth, scanned_at, entry_count, generation) VALUES (?, ?, ?, ?, ?)",
                    (root_key, safe_depth, now_str, len(fresh_entries), _CACHE_GENERATION)
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("Error updating SQLite destination cache: %s", exc)

    return fresh_entries


def resolve_filing_destinations(
    destination_roots: list[str],
    entity_name: str,
    entity_id: str | None = None,
    entity_type: str | None = None,
    database_path: Path | None = None,
    max_depth: int = 4
) -> tuple[list[Path], str]:
    """Find eligible existing destination subfolders across all configured destination roots.
    1. First checks for a custom folder mapping in database if provided.
       - Validates mapped folder exists on disk and is inside one of destination_roots (at any depth).
       - If valid, returns ([mapped_folder], 'custom_mapping').
    2. Otherwise scans all configured destination roots for nested child directories matching entity_name (cached).
    Returns:
    - ([], 'no_destination_roots') if no roots configured
    - ([], 'no_match') if 0 folders match
    - ([path], 'ok') if exactly 1 folder matches
    - ([path1, path2, ...], 'multiple_destinations') if >1 folders match
    """
    if not destination_roots:
        return [], "Destination root folder is not configured"

    # 1. Custom folder mapping check (supports any depth under destination roots).
    # Older UI builds could store the entity name in entity_id when their Stash
    # lookup failed. Prefer the stable ID, then accept an exact case-insensitive
    # name match so those existing mappings remain useful.
    if database_path and entity_id and entity_type:
        connection = connect(database_path)
        try:
            rows = connection.execute(
                "SELECT folder_path FROM filing_folder_mappings WHERE entity_type=? AND entity_id=?",
                (entity_type.lower(), str(entity_id))
            ).fetchall()
            if not rows and entity_name:
                rows = connection.execute(
                    """SELECT folder_path FROM filing_folder_mappings
                       WHERE entity_type=? AND entity_name=? COLLATE NOCASE""",
                    (entity_type.lower(), str(entity_name).strip())
                ).fetchall()
            valid_mapped: list[Path] = []
            seen_mapped: set[str] = set()
            for row in rows:
                mapped_path = Path(row["folder_path"]).expanduser()
                if mapped_path.is_dir():
                    res_mapped = str(mapped_path.resolve())
                    for r in destination_roots:
                        if not r:
                            continue
                        try:
                            r_res = str(Path(r).expanduser().resolve())
                            if res_mapped == r_res or res_mapped.startswith(r_res + os.sep):
                                if res_mapped not in seen_mapped:
                                    seen_mapped.add(res_mapped)
                                    valid_mapped.append(mapped_path.resolve())
                                break
                        except Exception:
                            continue
            if valid_mapped:
                return valid_mapped, "custom_mapping" if len(valid_mapped) == 1 else "multiple_destinations"
        finally:
            connection.close()

    # 2. Search all configured destination roots (bounded recursive discovery with cache)
    target_norm = _normalize_name_for_folder_match(entity_name)
    if not target_norm:
        return [], "Entity name is empty"
    target_compact = _compact_name_for_folder_match(target_norm)

    matched_folders = []
    seen_paths = set()
    for root_str in destination_roots:
        if not root_str or not str(root_str).strip():
            continue
        try:
            root_path = Path(root_str).expanduser().resolve()
        except Exception:
            continue
        if not root_path.is_dir():
            continue

        entries = _get_cached_destination_subdirectories(root_path, max_depth=max_depth, database_path=database_path)
        for dir_path, norm_name in entries:
            # Check exact normalized match or conservative descriptor match (e.g. 'The Cole Bentley Collection')
            stripped = _strip_conservative_folder_descriptors(norm_name)
            raw_stripped = norm_name
            if raw_stripped.startswith("the "):
                raw_stripped = raw_stripped[4:].strip()
            if raw_stripped.endswith(" collection"):
                raw_stripped = raw_stripped[:-11].strip()

            compact_match = (
                len(target_compact) >= 4 and
                (_compact_name_for_folder_match(norm_name) == target_compact or
                 _compact_name_for_folder_match(raw_stripped) == target_compact or
                 _compact_name_for_folder_match(stripped) == target_compact)
            )
            if norm_name == target_norm or raw_stripped == target_norm or stripped == target_norm or compact_match:
                res_str = str(dir_path)
                if res_str not in seen_paths:
                    seen_paths.add(res_str)
                    matched_folders.append(dir_path)

    if len(matched_folders) == 0:
        return [], f"Destination folder for '{entity_name}' does not exist under configured destination roots"
    elif len(matched_folders) == 1:
        return matched_folders, "ok"
    else:
        return matched_folders, "multiple_destinations"


_TAG_FOLDER_DESCRIPTOR_TOKENS = {
    "category", "categories", "collection", "collections", "content", "contents",
    "hair", "haired", "scene", "scenes", "tag", "tags", "video", "videos",
}


def _singular_category_token(token: str) -> str:
    token = token.lower()
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es") and token[-3:-2] in {"s", "x", "z"}:
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _category_name_tokens(name: str, drop_descriptors: bool = False) -> set[str]:
    tokens = {
        _singular_category_token(token)
        for token in re.findall(r"[A-Za-z0-9]+", str(name or "").lower())
        if len(token) >= 3
    }
    if drop_descriptors:
        meaningful = tokens - _TAG_FOLDER_DESCRIPTOR_TOKENS
        if meaningful:
            return meaningful
    return tokens


def resolve_tag_filing_destinations(
    destination_roots: list[str],
    tag: dict,
    database_path: Path | None = None,
    max_depth: int = 4,
) -> tuple[list[Path], str]:
    """Resolve a verified Stash tag to existing category folders.

    Exact/custom mappings win. The fallback compares whole normalized word
    tokens, handles ordinary plurals, and ignores generic category descriptors.
    It never creates a folder and preserves multiple matches for user review.
    """
    tag_id = str(tag.get("id") or "").strip()
    canonical_name = str(tag.get("name") or "").strip()
    aliases = [str(alias).strip() for alias in (tag.get("aliases") or []) if str(alias).strip()]
    names = [name for name in [canonical_name, *aliases] if name]
    if not names:
        return [], "Tag name is empty"

    exact_matches: list[Path] = []
    seen = set()
    for name in names:
        paths, status = resolve_filing_destinations(
            destination_roots, name, entity_id=tag_id, entity_type="tag",
            database_path=database_path, max_depth=max_depth,
        )
        if status == "custom_mapping":
            return paths, status
        for path in paths:
            resolved = str(path.resolve())
            if resolved not in seen:
                seen.add(resolved)
                exact_matches.append(path)
    if exact_matches:
        return exact_matches, "ok" if len(exact_matches) == 1 else "multiple_destinations"

    tag_token_sets = [tokens for tokens in (_category_name_tokens(name, drop_descriptors=True) for name in names) if tokens]
    matched: list[Path] = []
    for root_str in destination_roots:
        if not root_str or not str(root_str).strip():
            continue
        try:
            root = Path(root_str).expanduser().resolve()
        except Exception:
            continue
        if not root.is_dir():
            continue
        for folder, folder_name in _get_cached_destination_subdirectories(
            root, max_depth=max_depth, database_path=database_path
        ):
            folder_tokens = _category_name_tokens(folder_name)
            if not folder_tokens:
                continue
            if any(tokens.issubset(folder_tokens) for tokens in tag_token_sets):
                resolved = str(folder.resolve())
                if resolved not in seen:
                    seen.add(resolved)
                    matched.append(folder)

    if not matched:
        return [], f"Destination folder for tag '{canonical_name}' does not exist under configured destination roots"
    return matched, "ok" if len(matched) == 1 else "multiple_destinations"


def resolve_filing_destination_folder(destination_root: str, entity_name: str) -> tuple[Path | None, str]:
    """Legacy single-root compatibility wrapper for resolve_filing_destinations."""
    paths, status = resolve_filing_destinations([destination_root] if destination_root else [], entity_name)
    if status in ("ok", "custom_mapping") and len(paths) == 1:
        return paths[0], "ok"
    elif status == "multiple_destinations":
        match_names = [m.name for m in paths]
        return None, f"Ambiguous destination: multiple folders match '{entity_name}' ({', '.join(match_names)})"
    else:
        return None, status


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
                return True   # In a subdirectory inside incoming
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
            (now, json.dumps(cleaned_folders))
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
                            (str(fpath), size, modified_ns, oshash, now)
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
            (utc_now(), count, run_id)
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
                    (str(exc), run_id)
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
            row["path"] for row in connection.execute(
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
                return any(_is_subpath_of(Path(path_str).resolve(), Path(folder).resolve()) for folder in incoming_folders)
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


def is_disqualified_from_filing(database_path: Path, file_path: str, file_size: int, oshash: str | None, file_id: str | None, scene_id: str | None, allow_baseline: bool = False, allow_refresh: bool = False) -> tuple[bool, str]:
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
                row = connection.execute("SELECT 1 FROM filing_incoming_baseline WHERE oshash=? AND size=?", (oshash, file_size)).fetchone()
                if row:
                    return True, "File contents match a file present in incoming baseline snapshot"

        # 3. Already evaluated or ignored or completed in filing_proposals
        if (file_id or scene_id or file_path) and not allow_refresh:
            row = connection.execute(
                "SELECT status FROM filing_proposals WHERE file_id=? OR scene_id=? OR source_path=?",
                (str(file_id or ""), str(scene_id or ""), file_path)
            ).fetchone()
            if row:
                return True, f"File was already evaluated for automatic filing (status: {row['status']})"

        # 4. Check if this file/scene was already known in library files (relocation)
        if file_id or scene_id:
            row = connection.execute(
                "SELECT path FROM files WHERE (file_id=? OR scene_id=?) AND path != ? AND exists_on_disk=1",
                (str(file_id or ""), str(scene_id or ""), file_path)
            ).fetchone()
            if row:
                scene_label = f"Stash Scene {scene_id}" if scene_id else "an existing Stash scene"
                return True, (
                    f"File is already attached to {scene_label}, which also contains the matching library file "
                    f"at {row['path']}. Review that scene's file association in Stash before filing."
                )

        # 5. Check if size + oshash matches any existing file in the library
        if oshash and file_size:
            rows = connection.execute("SELECT path, fingerprints_json FROM files WHERE size=? AND path != ?", (file_size, file_path)).fetchall()
            for r in rows:
                existing_oshash = fingerprint_value(r["fingerprints_json"], "oshash")
                if existing_oshash and existing_oshash.lower() == oshash.lower():
                    return True, f"Video contents match existing library scene at {r['path']}"

        return False, ""
    finally:
        connection.close()


def find_filing_companions(source_video: Path, dest_folder: Path, dest_video_name: str) -> tuple[list[tuple[Path, Path]], str | None]:
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


def _update_proposal_status(database_path: Path, proposal_id: int, status: str, last_error: str | None = None) -> None:
    now = utc_now()
    connection = connect(database_path)
    try:
        connection.execute(
            "UPDATE filing_proposals SET status=?, last_error=?, updated_at=? WHERE id=?",
            (status, last_error, now, proposal_id)
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
                connection.execute("UPDATE incoming_files SET filing_diagnostic=? WHERE path=?", (diagnostic, str(file_path)))
            else:
                now_str = datetime.now().isoformat()
                connection.execute(
                    "INSERT OR IGNORE INTO incoming_files (path, first_seen_at, last_checked_at, status, detail, filing_diagnostic) VALUES (?, ?, ?, 'imported', 'Stash scene', ?)",
                    (str(file_path), now_str, now_str, diagnostic)
                )
            connection.commit()
        finally:
            connection.close()
    except Exception as exc:
        logger.debug("Failed recording filing diagnostic for %s: %s", file_path, exc)


def evaluate_filing_proposal(database_path: Path, stash, file_path: str, scene: dict, config: dict, allow_baseline: bool = False, allow_refresh: bool = False) -> dict | None:
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
            row = conn.execute("SELECT file_id, scene_id FROM files WHERE path=? AND exists_on_disk=1", (str(src),)).fetchone()
            if row:
                file_id = str(row["file_id"])
                scene_id = str(row["scene_id"])
        finally:
            conn.close()

    if not file_id or not scene_id:
        record_incoming_filing_diagnostic(database_path, file_path, "Filing skipped: File has not been linked to a Stash scene.")
        return None

    raw_incoming = config.get("incomingFolders") or ([config.get("incomingFolder")] if config.get("incomingFolder") else [])
    if isinstance(raw_incoming, (list, tuple)):
        incoming_folders = [str(f).strip() for f in raw_incoming if isinstance(f, (str, Path)) and str(f).strip()]
    else:
        incoming_folders = []

    file_size = src.stat().st_size
    oshash = opensubtitles_hash(src)
    disqualified, disq_reason = is_disqualified_from_filing(database_path, str(src), file_size, oshash, file_id, scene_id, allow_baseline=allow_baseline, allow_refresh=allow_refresh)
    if disqualified:
        record_incoming_filing_diagnostic(database_path, file_path, f"Filing skipped: {disq_reason}")
        return None

    trigger = (config.get("autoFilingTrigger") or "import").strip().lower()
    if trigger == "metadata":
        scene_performers = (scene or {}).get("performers") or []
        scene_studio = (scene or {}).get("studio")
        scene_tags = (scene or {}).get("tags") or []
        if not scene_performers and not scene_studio and not scene_tags:
            record_incoming_filing_diagnostic(database_path, file_path, "Waiting for performer, studio, or tag metadata to be added in Stash.")
            return None

    organize_by = (config.get("autoFilingOrganizeBy") or "both").strip().lower()
    dest_roots = get_configured_filing_destination_roots(config)
    if not dest_roots:
        record_incoming_filing_diagnostic(database_path, file_path, "No destination roots configured in Automatic Filing settings.")
        return None

    match_source = config.get("autoFilingMatchSource") or "metadata_first"

    try:
        max_depth = int((config or {}).get("autoFilingMaxDiscoveryDepth", 4))
    except (ValueError, TypeError):
        max_depth = 4
    max_depth = max(1, min(8, max_depth))

    matched_candidates_by_folder = {}

    if organize_by == "studio":
        all_studios = []
        try:
            gql_res = stash.call_GQL('{ allStudios { id name aliases } }')
            all_studios = (gql_res or {}).get("allStudios") or []
        except Exception:
            pass
        s_match = match_studio_for_filing(scene, src.name, all_studios, match_source=match_source)
        if not s_match.get("matched"):
            s_reason = s_match.get("reason") or "No matching studio found in filename or Stash metadata."
            record_incoming_filing_diagnostic(database_path, file_path, s_reason)
            return None
        s_entity = s_match["entity"]
        s_paths, s_status = resolve_filing_destinations(
            dest_roots, s_entity["name"], entity_id=str(s_entity["id"]), entity_type="studio",
            database_path=database_path, max_depth=max_depth
        )
        if not s_paths:
            record_incoming_filing_diagnostic(database_path, file_path, f"Studio '{s_entity['name']}' identified, but no destination folder found.")
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
                "label": f"[Studio] {s_entity['name']} → {p}" + (" (Custom Mapped)" if s_status == "custom_mapping" else "")
            }
        primary_match = s_match
        primary_entity = s_entity
        effective_organize_by = "studio"

    elif organize_by == "both":
        all_performers = []
        all_studios = []
        try:
            gql_p = stash.call_GQL('{ allPerformers { id name disambiguation alias_list } }')
            all_performers = (gql_p or {}).get("allPerformers") or []
        except Exception:
            pass
        try:
            gql_s = stash.call_GQL('{ allStudios { id name aliases } }')
            all_studios = (gql_s or {}).get("allStudios") or []
        except Exception:
            pass

        p_match = match_performer_for_filing(scene, src.name, all_performers, match_source=match_source)
        s_match = match_studio_for_filing(scene, src.name, all_studios, match_source=match_source)

        p_valid = bool(p_match.get("matched"))
        s_valid = bool(s_match.get("matched"))

        p_entities = p_match.get("entities") or ([p_match["entity"]] if p_match.get("entity") else [])
        p_matched_paths_by_ent = {}
        for p_ent in p_entities:
            p_paths_curr, p_status_curr = resolve_filing_destinations(
                dest_roots, p_ent["name"], entity_id=str(p_ent["id"]), entity_type="performer",
                database_path=database_path, max_depth=max_depth
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
                                "matched_alias": existing.get("matched_alias")
                            }
                        ]
                    if not any(e["entity_id"] == str(p_ent["id"]) and e["entity_type"] == "performer" for e in existing["matched_entities"]):
                        existing["matched_entities"].append({
                            "entity_type": "performer",
                            "entity_name": p_ent["name"],
                            "entity_id": str(p_ent["id"]),
                            "is_custom_mapped": (p_status_curr == "custom_mapping"),
                            "match_source": p_match.get("source", "filename"),
                            "matched_alias": p_match.get("matched_alias") if len(p_entities) == 1 else None
                        })
                    all_names = " & ".join(e["entity_name"] for e in existing["matched_entities"])
                    all_ids = ",".join(e["entity_id"] for e in existing["matched_entities"])
                    existing["entity_type"] = "both" if any(e["entity_type"] == "studio" for e in existing["matched_entities"]) else "performer"
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
                        "label": f"[Performer] {p_ent['name']} → {p}" + (" (Custom Mapped)" if p_status_curr == "custom_mapping" else "")
                    }

        s_paths = []
        s_status = None
        if s_valid:
            s_entity = s_match["entity"]
            s_paths, s_status = resolve_filing_destinations(
                dest_roots, s_entity["name"], entity_id=str(s_entity["id"]), entity_type="studio",
                database_path=database_path, max_depth=max_depth
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
                                "matched_alias": existing.get("matched_alias")
                            }
                        ]
                    if not any(e["entity_id"] == str(s_entity["id"]) and e["entity_type"] == "studio" for e in existing["matched_entities"]):
                        existing["matched_entities"].append({
                            "entity_type": "studio",
                            "entity_name": s_entity["name"],
                            "entity_id": str(s_entity["id"]),
                            "is_custom_mapped": (s_status == "custom_mapping"),
                            "match_source": s_match.get("source", "filename"),
                            "matched_alias": s_match.get("matched_alias")
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
                        "label": f"[Studio] {s_entity['name']} → {p}" + (" (Custom Mapped)" if s_status == "custom_mapping" else "")
                    }

        # In the combined review mode, verified Stash tags may identify existing
        # category folders. Tags never come from loose filename guesses.
        scene_tags = [
            tag for tag in ((scene or {}).get("tags") or [])
            if tag.get("id") and tag.get("name")
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
                    all_types = " & ".join(entity["entity_type"].capitalize() for entity in existing["matched_entities"])
                    existing["entity_type"] = "both"
                    existing["entity_name"] = all_names
                    existing["entity_id"] = all_ids
                    existing["is_custom_mapped"] = any(entity.get("is_custom_mapped") for entity in existing["matched_entities"])
                    existing["label"] = f"[{all_types}: {all_names}] → {p}"
                    if existing["is_custom_mapped"]:
                        existing["label"] += " (Custom Mapped)"
                else:
                    matched_candidates_by_folder[p_res] = {
                        "destination_folder": str(p),
                        **tag_entity,
                        "label": f"[Tag] {tag['name']} → {p}" + (" (Custom Mapped)" if tag_status == "custom_mapping" else ""),
                    }

        # Build detailed diagnostics for combined performer/studio/tag mode.
        p_matched_count = sum(1 for (paths, _) in p_matched_paths_by_ent.values() if paths)
        if p_valid:
            if len(p_entities) == 1:
                p_name = p_entities[0]["name"]
                p_diag = f"Performer '{p_name}' matched" if p_matched_count > 0 else f"Performer '{p_name}' identified, but no destination folder found"
            else:
                p_names = ", ".join(e["name"] for e in p_entities)
                p_diag = f"Multiple performers matched ({p_names})" if p_matched_count > 0 else f"Multiple performers identified ({p_names}), but no destination folders found"
        else:
            p_diag = p_match.get("reason", "No matching performer found")

        if s_valid:
            s_diag = f"Studio '{s_match['entity']['name']}' matched" if s_paths else f"Studio '{s_match['entity']['name']}' identified, but no destination folder found"
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
        all_performers = []
        try:
            gql_res = stash.call_GQL('{ allPerformers { id name disambiguation alias_list } }')
            all_performers = (gql_res or {}).get("allPerformers") or []
        except Exception:
            pass
        p_match = match_performer_for_filing(scene, src.name, all_performers, match_source=match_source)
        if not p_match.get("matched"):
            p_reason = p_match.get("reason") or "No matching performer found in filename or Stash metadata."
            record_incoming_filing_diagnostic(database_path, file_path, p_reason)
            return None
        p_entities = p_match.get("entities") or ([p_match["entity"]] if p_match.get("entity") else [])
        for p_ent in p_entities:
            p_paths, p_status = resolve_filing_destinations(
                dest_roots, p_ent["name"], entity_id=str(p_ent["id"]), entity_type="performer",
                database_path=database_path, max_depth=max_depth
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
                                "matched_alias": existing.get("matched_alias")
                            }
                        ]
                    if not any(e["entity_id"] == str(p_ent["id"]) and e["entity_type"] == "performer" for e in existing["matched_entities"]):
                        existing["matched_entities"].append({
                            "entity_type": "performer",
                            "entity_name": p_ent["name"],
                            "entity_id": str(p_ent["id"]),
                            "is_custom_mapped": (p_status == "custom_mapping"),
                            "match_source": p_match.get("source", "filename"),
                            "matched_alias": p_match.get("matched_alias") if len(p_entities) == 1 else None
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
                        "label": f"[Performer] {p_ent['name']} → {p}" + (" (Custom Mapped)" if p_status == "custom_mapping" else "")
                    }

        if not matched_candidates_by_folder:
            if len(p_entities) == 1:
                record_incoming_filing_diagnostic(database_path, file_path, f"Performer '{p_entities[0]['name']}' identified, but no destination folder found.")
            else:
                names = ", ".join(e["name"] for e in p_entities)
                record_incoming_filing_diagnostic(database_path, file_path, f"Multiple performers identified ({names}), but no destination folders found.")
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
            record_incoming_filing_diagnostic(database_path, file_path, f"Destination conflict: target file '{proposed_path.name}' already exists in destination folder.")
            return None

        # Check inventory conflict in database
        conn = connect(database_path)
        try:
            conflict = conn.execute("SELECT 1 FROM files WHERE path=? AND exists_on_disk=1", (str(proposed_path),)).fetchone() is not None
            if conflict:
                record_incoming_filing_diagnostic(database_path, file_path, f"Destination conflict: target path already registered in Stash library.")
                return None
        finally:
            conn.close()

        companions, comp_err = find_filing_companions(src, dest_folder, proposed_name)
        if comp_err:
            record_incoming_filing_diagnostic(database_path, file_path, f"Filing skipped: companion file collision ({comp_err})")
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
            (str(file_id or ""), str(scene_id or ""), str(src))
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
                    file_id, scene_id, str(src), str(proposed_path), str(dest_folder), proposed_name,
                    cand_organize_by, str(matched_entity_id), str(matched_entity_name), matched_alias,
                    match_source_val, f"Matched {cand_organize_by} '{matched_entity_name}' ({match_source_val})",
                    companions_json, candidate_destinations_json, is_custom_mapped, in_nested_folder,
                    now, proposal_id
                )
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
                    file_id, scene_id, str(src), str(proposed_path), str(dest_folder), proposed_name,
                    cand_organize_by, str(matched_entity_id), str(matched_entity_name), matched_alias,
                    match_source_val, f"Matched {cand_organize_by} '{matched_entity_name}' ({match_source_val})",
                    companions_json, candidate_destinations_json, is_custom_mapped, in_nested_folder,
                    now, now
                )
            )
            proposal_id = cur.lastrowid
        connection.commit()
    finally:
        connection.close()

    dest_name = dest_folder.name if hasattr(dest_folder, "name") and dest_folder.name else (str(dest_folder) if dest_folder else "multiple candidate destinations")
    record_activity(
        database_path, "filing", "proposal created", "pending",
        scene_id=scene_id, file_id=file_id, old_path=str(src), new_path=str(proposed_path),
        detail=f"Proposed filing to {dest_name} for {cand_organize_by} '{matched_entity_name}'"
    )

    if len(raw_candidates_list) == 1:
        record_incoming_filing_diagnostic(database_path, file_path, f"Proposal ready: {cand_organize_by} '{matched_entity_name}' → {dest_folder}")
    else:
        record_incoming_filing_diagnostic(database_path, file_path, f"Proposal ready: Multiple candidate destinations ({len(raw_candidates_list)}) requiring selection.")

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
    proposal_id: int | None = None
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

    # 1. Prevent duplicate proposals, check unresolved recovery states, and recognise already-filed scenes
    conn = connect(database_path)
    try:
        query_id = int(proposal_id) if proposal_id else -1
        active_prop = conn.execute(
            """SELECT id, scene_id, file_id, status, source_path, proposed_path, destination_folder FROM filing_proposals
               WHERE (source_path = ? OR proposed_path = ? OR id = ?)
               ORDER BY id DESC LIMIT 1""",
            (str(src), str(src), query_id)
        ).fetchone()

        if active_prop:
            p_status = active_prop["status"]
            if p_status == "completed":
                conn.execute(
                    "UPDATE incoming_files SET filing_diagnostic=NULL WHERE path=? OR path=?",
                    (str(src), str(active_prop["proposed_path"]))
                )
                conn.commit()
                return {
                    "success": True,
                    "already_filed": True,
                    "message": f"Filing is already complete. '{src.name}' is verified at its destination."
                }
            elif p_status == "needs_recovery":
                return {
                    "success": False,
                    "error": "This file has an unresolved filing recovery in progress. Resolve or recover it first."
                }
            elif p_status in ("pending", "blocked") and not allow_refresh:
                return {
                    "success": False,
                    "proposal_pending": True,
                    "proposal_id": active_prop["id"],
                    "error": "An active filing proposal already exists for this scene.",
                    "message": "A filing proposal is ready for review in Automatic Filing."
                }
            if allow_refresh:
                allow_baseline = True
    finally:
        conn.close()

    if not src.is_file():
        return {"success": False, "error": f"File does not exist on disk: {file_path}"}

    # Verify if file is inside configured incoming folders
    incoming_folders = get_configured_incoming_folders(config)
    is_inside_incoming = True
    if incoming_folders:
        is_inside_incoming = any(
            _is_subpath_of(src, Path(f).resolve())
            for f in incoming_folders
        )

    # If the file is OUTSIDE incoming folders, check if it is already verified at an approved destination
    if not is_inside_incoming:
        conn = connect(database_path)
        try:
            f_row = conn.execute(
                "SELECT file_id, scene_id, path FROM files WHERE (path=? OR basename=?) AND exists_on_disk=1",
                (str(src), src.name)
            ).fetchone()
            scene_id_to_check = str(f_row["scene_id"]) if f_row and f_row["scene_id"] else (str(active_prop["scene_id"]) if active_prop and active_prop["scene_id"] else None)

            if scene_id_to_check and stash:
                try:
                    scene_obj = None
                    if hasattr(stash, "call_GQL"):
                        res = stash.call_GQL(
                            "query CheckScene($id: ID!) { findScene(id: $id) { id files { id path } } }",
                            {"id": scene_id_to_check}
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
                                        (utc_now(), active_prop["id"])
                                    )
                                conn.execute(
                                    "UPDATE incoming_files SET filing_diagnostic=NULL WHERE path=? OR path=?",
                                    (str(src), str(active_prop["source_path"] if active_prop else src))
                                )
                                conn.commit()
                                return {
                                    "success": True,
                                    "already_filed": True,
                                    "message": f"Filing is already complete. '{src.name}' is verified at its destination."
                                }
                except Exception:
                    pass
        finally:
            conn.close()

        return {
            "success": False,
            "error": "This file is not located inside any configured Incoming folder."
        }

    # ``allow_refresh`` refreshes current Stash metadata and proposal choices.
    # Folder discovery has its own persistent cache because rescanning multiple
    # disks or NAS roots for every metadata edit is unnecessarily expensive.
    # The explicit Refresh Folders operation invalidates and rebuilds that cache.

    # 2. Check scene linkage in files table or Stash
    file_id = None
    scene_id = None
    conn = connect(database_path)
    try:
        f_row = conn.execute(
            "SELECT file_id, scene_id FROM files WHERE path=? AND exists_on_disk=1",
            (str(src),)
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
                {"id": str(scene_id)}
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
                    (f"Filing skipped: {disq_reason}", utc_now(), active_prop["id"])
                )
                conn.commit()
            finally:
                conn.close()
        return {
            "success": False,
            "error": f"Filing skipped: {disq_reason}"
        }

    # 3. Evaluate new proposal
    prop = evaluate_filing_proposal(
        database_path,
        stash,
        str(src),
        scene_data,
        config,
        allow_baseline=allow_baseline,
        allow_refresh=allow_refresh
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
            "message": "Filing proposal generated successfully."
        }
    else:
        conn = connect(database_path)
        try:
            diag_row = conn.execute("SELECT filing_diagnostic FROM incoming_files WHERE path=?", (str(src),)).fetchone()
            diag = diag_row["filing_diagnostic"] if diag_row else "Filing evaluation did not produce a proposal."
            if allow_refresh and active_prop:
                conn.execute(
                    "UPDATE filing_proposals SET status='invalid', last_error=?, updated_at=? WHERE id=?",
                    (diag, utc_now(), active_prop["id"])
                )
                conn.commit()
        finally:
            conn.close()
        return {
            "success": False,
            "diagnostic": diag,
            "message": diag,
            "error": diag
        }


def _move_file(source: Path | str, target: Path | str):
    """Move a file safely, supporting cross-device moves across different volumes."""
    src_p = Path(source)
    tgt_p = Path(target)
    try:
        src_p.rename(tgt_p)
    except OSError as e:
        if e.errno == 18 or getattr(e, "winerror", None) == 17:
            shutil.move(str(src_p), str(tgt_p))
        else:
            raise



def _format_bytes(num_bytes: int | float) -> str:
    if num_bytes is None:
        return "0 B"
    num = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}" if unit in ["MB", "GB", "TB"] else f"{int(num)} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def _update_active_transfer(database_path: Path, proposal_id: int, scene_id: str, file_id: str, source_path: str, destination_path: str, destination_folder: str, stage: str, stage_label: str, detail: str, total_bytes: int = 0):
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
                proposal_id, str(scene_id or ""), str(file_id or ""), str(source_path), str(destination_path), str(destination_folder),
                stage, stage_label, detail, int(total_bytes or 0),
                proposal_id, now, now
            )
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


def get_active_filing_transfers(database_path: Path) -> list[dict]:
    connection = connect(database_path)
    try:
        # Prune transfers that died over 1 hour ago
        now = datetime.now().timestamp()
        rows = connection.execute("SELECT * FROM active_filing_transfers ORDER BY started_at ASC").fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        connection.close()


def apply_filing_proposal(
    database_path: Path,
    stash,
    proposal_id: int,
    config: dict = None,
    update_metadata: bool = False,
    target_destination_folder: str = None,
    target_entity_type: str = None,
    target_entity_id: str = None
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
                "reason": "Another filing transfer is currently in progress. Transfers are serialized for safety."
            }

        connection = connect(database_path)
        try:
            row = connection.execute("SELECT * FROM filing_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not row:
                return {"id": proposal_id, "status": "blocked", "reason": "Proposal not found"}
            proposal = dict(row)
        finally:
            connection.close()

        if proposal["status"] != "pending":
            return {"id": proposal_id, "status": "blocked", "reason": f"Proposal is already '{proposal['status']}'"}

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
            database_path, proposal_id, str(scene_id or ""), str(file_id or ""), str(src), str(dest_video), str(dest_folder),
            stage="validating", stage_label="Preflight Verification",
            detail=f"Validating source, destination and companion paths ({_format_bytes(file_size)})...",
            total_bytes=file_size
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
                    (str(file_id), str(scene_id), str(src))
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
                conflict = conn.execute("SELECT 1 FROM files WHERE path=? AND exists_on_disk=1", (str(dest_video),)).fetchone() is not None
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
                    {"id": str(scene_id)}
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
                database_path, proposal_id, str(scene_id or ""), str(file_id or ""), str(src), str(dest_video), str(dest_folder),
                stage="moving_video", stage_label="Transferring Video",
                detail=f"Moving video to {dest_folder.name} ({_format_bytes(file_size)})...",
                total_bytes=file_size
            )

            expect_filesystem_move(database_path, str(src), str(dest_video))
            stash_res = None
            move_err = None
            try:
                stash_res = stash.move_files({
                    "ids": [proposal["file_id"]],
                    "destination_folder": str(dest_folder),
                    "destination_basename": dest_video.name
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
                        {"id": str(scene_id)}
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
                        database_path, "filing", "move failed", "failed",
                        scene_id=scene_id, file_id=file_id,
                        old_path=str(src), new_path=str(dest_video),
                        detail=err_msg
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
                        database_path, "filing", "move failed - needs recovery", "needs_recovery",
                        scene_id=scene_id, file_id=file_id,
                        old_path=str(src), new_path=str(dest_video),
                        detail=err_msg
                    )
                    return {"id": proposal_id, "status": "needs_recovery", "reason": err_msg}

            # Step 4: Move companions
            moved_companions = []
            companion_fail = None
            if companions:
                _update_active_transfer(
                    database_path, proposal_id, str(scene_id or ""), str(file_id or ""), str(src), str(dest_video), str(dest_folder),
                    stage="moving_companions", stage_label="Transferring Companions",
                    detail=f"Moving {len(companions)} companion file(s)...",
                    total_bytes=file_size
                )
            for c_src, c_dst in companions:
                if c_src.is_file():
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
                for orig_src, orig_dst in reversed(moved_companions):
                    if orig_dst.is_file():
                        try:
                            expect_filesystem_move(database_path, str(orig_dst), str(orig_src))
                            _move_file(orig_dst, orig_src)
                        except Exception:
                            rollback_ok = False
                # Move video back via Stash
                video_rb_err = None
                try:
                    expect_filesystem_move(database_path, str(dest_video), str(src))
                    stash_back = stash.move_files({
                        "ids": [proposal["file_id"]],
                        "destination_folder": str(src.parent),
                        "destination_basename": src.name
                    })
                    if stash_back is False:
                        rollback_ok = False
                        video_rb_err = "Stash move_files returned False during rollback"
                except Exception as v_err:
                    rollback_ok = False
                    video_rb_err = str(v_err)

                if rollback_ok and src.is_file() and not dest_video.exists():
                    err_msg = f"{companion_fail}; safely restored to source"
                    _update_proposal_status(database_path, proposal_id, "failed", err_msg)
                    return {"id": proposal_id, "status": "failed", "rolled_back": True, "reason": err_msg}
                else:
                    err_msg = f"CRITICAL: Failed moving companion: {companion_fail}; Video rollback failed: {video_rb_err or 'dest exists or src missing'}"
                    _update_proposal_status(database_path, proposal_id, "needs_recovery", err_msg)
                    return {"id": proposal_id, "status": "needs_recovery", "rolled_back": False, "reason": err_msg}

            # Step 5: Update database records
            _update_active_transfer(
                database_path, proposal_id, str(scene_id or ""), str(file_id or ""), str(src), str(dest_video), str(dest_folder),
                stage="updating_records", stage_label="Finalizing Records",
                detail="Updating Stash scene path, metadata, and Library Manager inventory...",
                total_bytes=file_size
            )

            now = utc_now()
            connection = connect(database_path)
            try:
                connection.execute(
                    "UPDATE filing_proposals SET status='completed', proposed_path=?, destination_folder=?, last_error=NULL, updated_at=? WHERE id=?",
                    (str(dest_video), str(dest_folder), now, proposal_id)
                )
                connection.execute(
                    "UPDATE files SET path=?, basename=?, exists_on_disk=1, last_seen_at=? WHERE file_id=?",
                    (str(dest_video), dest_video.name, now, file_id)
                )
                if (config or {}).get("autoFilingPreserveFilename", True):
                    existing_st = connection.execute("SELECT file_id FROM filename_state WHERE file_id=?", (file_id,)).fetchone()
                    if existing_st:
                        connection.execute("UPDATE filename_state SET rename_protected=1, updated_at=? WHERE file_id=?", (now, file_id))
                    else:
                        connection.execute(
                            """INSERT INTO filename_state
                               (file_id, base_stem, base_source, rename_protected, created_at, updated_at)
                               SELECT ?, ?, 'automatic_filing', 1, ?, ?
                               WHERE EXISTS (SELECT 1 FROM files WHERE file_id=?)""",
                            (file_id, dest_video.stem, now, now, file_id)
                        )
                connection.execute(
                    "UPDATE incoming_files SET path=?, filing_diagnostic=NULL, last_checked_at=? WHERE path=? OR path=?",
                    (str(dest_video), now, str(src), str(dest_video))
                )
                for c_src, c_dst in companions:
                    connection.execute(
                        "UPDATE incoming_files SET path=?, filing_diagnostic=NULL, last_checked_at=? WHERE path=? OR path=?",
                        (str(c_dst), now, str(c_src), str(c_dst))
                    )
                connection.commit()
            finally:
                connection.close()

            record_activity(
                database_path, "filing", "proposal approved", "completed",
                scene_id=scene_id, file_id=file_id,
                old_path=str(src), new_path=str(dest_video),
                detail=f"Moved file and {len(companions)} companion(s) to {dest_folder.name}"
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
                            {"id": str(scene_id)}
                        )
                        scene_data = (scene_res or {}).get("findScene") or {}
                        current_performers = scene_data.get("performers") or []
                        current_ids = [str(p["id"]) for p in current_performers if p.get("id")]
                        if entity_to_update_id not in current_ids:
                            new_ids = current_ids + [entity_to_update_id]
                            stash.call_GQL(
                                "mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }",
                                {"input": {"id": str(scene_id), "performer_ids": new_ids}}
                            )
                            metadata_updated = True
                            record_activity(
                                database_path, "filing", "metadata updated", "completed",
                                scene_id=scene_id, file_id=file_id,
                                detail=f"Added performer '{entity_to_update_name or entity_to_update_id}' to scene {scene_id}"
                            )
                        else:
                            metadata_updated = True
                    elif entity_to_update_type == "studio" and entity_to_update_id:
                        scene_res = stash.call_GQL(
                            "query FindScene($id: ID!) { findScene(id: $id) { id studio { id name } } }",
                            {"id": str(scene_id)}
                        )
                        scene_data = (scene_res or {}).get("findScene") or {}
                        current_studio = scene_data.get("studio")
                        if not current_studio:
                            stash.call_GQL(
                                "mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }",
                                {"input": {"id": str(scene_id), "studio_id": entity_to_update_id}}
                            )
                            metadata_updated = True
                            record_activity(
                                database_path, "filing", "metadata updated", "completed",
                                scene_id=scene_id, file_id=file_id,
                                detail=f"Set studio '{entity_to_update_name or entity_to_update_id}' on scene {scene_id}"
                            )
                        elif str(current_studio.get("id")) == entity_to_update_id:
                            metadata_updated = True
                        else:
                            metadata_error = f"Scene {scene_id} already has a different studio: '{current_studio.get('name')}'"
                            record_activity(
                                database_path, "filing", "metadata preserved", "completed",
                                scene_id=scene_id, file_id=file_id,
                                detail=f"Preserved existing scene studio '{current_studio.get('name')}'; did not overwrite with '{entity_to_update_name or entity_to_update_id}'"
                            )
                    elif entity_to_update_type == "tag" and entity_to_update_id:
                        scene_res = stash.call_GQL(
                            "query FindScene($id: ID!) { findScene(id: $id) { id tags { id name } } }",
                            {"id": str(scene_id)}
                        )
                        scene_data = (scene_res or {}).get("findScene") or {}
                        current_tags = scene_data.get("tags") or []
                        current_ids = [str(tag["id"]) for tag in current_tags if tag.get("id")]
                        if entity_to_update_id not in current_ids:
                            stash.call_GQL(
                                "mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }",
                                {"input": {"id": str(scene_id), "tag_ids": current_ids + [entity_to_update_id]}}
                            )
                            metadata_updated = True
                            record_activity(
                                database_path, "filing", "metadata updated", "completed",
                                scene_id=scene_id, file_id=file_id,
                                detail=f"Added tag '{entity_to_update_name or entity_to_update_id}' to scene {scene_id}"
                            )
                        else:
                            metadata_updated = True
                    elif not metadata_error:
                        metadata_error = "No matching entity available to tag"
                except Exception as meta_exc:
                    metadata_error = str(meta_exc)
                    record_activity(
                        database_path, "filing", "metadata update failed", "completed",
                        scene_id=scene_id, file_id=file_id,
                        detail=f"File move completed, but metadata update could not be completed: {metadata_error}"
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
                "metadata_error": metadata_error
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
            (now, proposal_id)
        )
        connection.commit()
        record_activity(
            database_path, "filing", "proposal ignored", "ignored",
            scene_id=row["scene_id"], file_id=row["file_id"],
            old_path=row["source_path"],
            detail=f"Filing proposal ignored by user for {Path(row['source_path']).name}"
        )
        return {"id": proposal_id, "status": "ignored"}
    finally:
        connection.close()



def invalidate_stale_filing_proposals(database_path: Path, stash=None) -> list[int]:
    """Scan pending filing proposals and automatically mark as 'invalid' any proposal
    whose source video file or Stash scene no longer exists.
    Does not affect valid proposals or transactions requiring recovery.
    Returns list of invalidated proposal IDs."""
    invalidated = []
    has_updates = False
    connection = connect(database_path)
    try:
        rows = connection.execute(
            "SELECT id, scene_id, file_id, source_path, proposed_path, destination_folder FROM filing_proposals WHERE status='pending'"
        ).fetchall()
        now = utc_now()
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
                                {"id": str(r["scene_id"])}
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
                    connection.execute(
                        "UPDATE filing_proposals SET status='completed', last_error=NULL, updated_at=? WHERE id=?",
                        (now, prop_id)
                    )
                    connection.execute(
                        "UPDATE incoming_files SET filing_diagnostic=NULL, last_checked_at=? WHERE path=? OR path=?",
                        (now, str(src_path), str(dest_path))
                    )
                    has_updates = True
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
                            {"id": str(r["scene_id"])}
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
                connection.execute(
                    "UPDATE filing_proposals SET status='invalid', last_error=?, updated_at=? WHERE id=?",
                    (reason, now, prop_id)
                )
                invalidated.append(prop_id)
                has_updates = True
        if has_updates:
            connection.commit()
        return invalidated
    finally:
        connection.close()


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
                stash_res = stash.move_files({"ids": [proposal["file_id"]], "destination_folder": str(src.parent), "destination_basename": src.name})
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
                {"id": proposal["scene_id"]}
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
                    (now, proposal_id)
                )
                connection.execute(
                    "UPDATE files SET path=?, basename=?, exists_on_disk=1, last_seen_at=? WHERE file_id=?",
                    (str(src), src.name, now, proposal["file_id"])
                )
                connection.execute(
                    "UPDATE incoming_files SET path=?, last_checked_at=? WHERE path=?",
                    (str(src), now, str(dest_video))
                )
                for item in recorded_companions:
                    connection.execute(
                        "UPDATE incoming_files SET path=?, last_checked_at=? WHERE path=?",
                        (item["source"], now, item["target"])
                    )
                connection.commit()
            finally:
                connection.close()

            record_activity(
                database_path, "filing", "recovery complete", "complete",
                scene_id=proposal["scene_id"], file_id=proposal["file_id"],
                old_path=str(dest_video), new_path=str(src),
                detail="Transaction recovery verified: video, companions, and Stash scene restored to source"
            )
            return {
                "id": proposal_id,
                "status": "failed",
                "recovered": True,
                "reason": "Transaction recovery verified: video, companions, and Stash scene restored to source"
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
                "reason": err_msg
            }


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
            ext = p_obj.suffix.lower()
            is_video = ext in VIDEO_EXTENSIONS
            is_file = p_obj.is_file()

            # Check if physically inside incoming folders
            is_inside_incoming = False
            if is_file and incoming_folders:
                try:
                    p_res = p_obj.resolve()
                    is_inside_incoming = any(
                        _is_subpath_of(p_res, Path(f).resolve())
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
