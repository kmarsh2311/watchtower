"""Read-only inventory storage for Stash Library Manager."""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import re
import unicodedata
import tempfile
import subprocess
import shutil
import glob
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


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
CREATE TABLE IF NOT EXISTS rename_queue (
    scene_id TEXT PRIMARY KEY,
    enqueued_at REAL NOT NULL,
    available_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS filesystem_monitor_status (
    id INTEGER PRIMARY KEY CHECK(id=1),
    token TEXT,
    pid INTEGER,
    state TEXT NOT NULL DEFAULT 'stopped',
    started_at TEXT,
    heartbeat_at TEXT,
    roots_json TEXT NOT NULL DEFAULT '[]',
    unavailable_roots_json TEXT NOT NULL DEFAULT '[]'
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
    detail TEXT
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
"""

VIDEO_EXTENSIONS = {
    ".3gp", ".asf", ".avi", ".divx", ".flv", ".m2ts", ".m4v", ".mkv",
    ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ogm", ".ogv", ".rm",
    ".rmvb", ".ts", ".vob", ".webm", ".wmv",
}
ASSOCIATED_EXTENSIONS = {".funscript", ".srt", ".vtt", ".scc", ".ttml", ".dfxp", ".lrc", ".txt"}
IMAGE_SIDECAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _sidecar_match_key(name: str) -> str:
    """Normalize text by stripping combining accents and non-alphanumeric punctuation (brackets, dashes, etc.)."""
    nfkd = unicodedata.normalize("NFKD", str(name or ""))
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", "", no_accents.casefold())


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
            lock_file.seek(0)
            if os.fstat(lock_file.fileno()).st_size == 0:
                lock_file.write(b"0")
                lock_file.flush()
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
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
        _safe_alter(connection, "incoming_files", "attempts",
                    "ALTER TABLE incoming_files ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        _safe_alter(connection, "incoming_files", "settle_seconds",
                    "ALTER TABLE incoming_files ADD COLUMN settle_seconds INTEGER NOT NULL DEFAULT 300")
        _safe_alter(connection, "inventory_runs", "stash_scene_count",
                    "ALTER TABLE inventory_runs ADD COLUMN stash_scene_count INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            """UPDATE inventory_runs SET stash_scene_count=(SELECT COUNT(DISTINCT scene_id) FROM files)
               WHERE status='complete' AND stash_scene_count=0"""
        )
        connection.commit()
        _schema_applied.add(key)


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    _ensure_schema(connection, database_path)
    return connection


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


def incoming_summary(database_path: Path) -> dict:
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
        for row in connection.execute(
            """SELECT path,status,size,stable_since,settle_seconds,attempts,scan_job_id,detail,last_checked_at
               FROM incoming_files WHERE status IN ('waiting','scanning','failed','downloading','generating_sheet')
               ORDER BY CASE status WHEN 'failed' THEN 0 WHEN 'generating_sheet' THEN 1 WHEN 'scanning' THEN 2 WHEN 'waiting' THEN 3 ELSE 4 END,last_checked_at DESC LIMIT 20"""
        ):
            item = dict(row)
            item["remaining_seconds"] = max(0, int((item["stable_since"] or now) + (item["settle_seconds"] or 300) - now)) \
                if item["status"] == "waiting" else 0
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
            "latest": dict(latest) if latest else None,
            "active": active,
        }
    finally:
        connection.close()


def expect_filesystem_move(database_path: Path, source: str, destination: str, ttl_seconds: float = 60):
    connection = connect(database_path)
    try:
        connection.execute("DELETE FROM expected_moves WHERE expires_at < ?", (datetime.now().timestamp(),))
        connection.execute(
            "INSERT OR REPLACE INTO expected_moves(source_path,destination_path,expires_at) VALUES (?,?,?)",
            (str(source), str(destination), datetime.now().timestamp() + ttl_seconds),
        )
        connection.commit()
    finally:
        connection.close()


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


def dashboard_data(database_path: Path, activity_limit: int = 100) -> dict:
    """Return the small read-only snapshot used by the central dashboard."""
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
        pending_events = pending_filesystem_events(database_path)
        return {
            "inventory": dict(inventory_row) if inventory_row else None,
            "rename_queue": queue_counts,
            "monitor": filesystem_monitor_summary(database_path),
            "activity": recent_activity(database_path, activity_limit),
            "filename_preview": {"run": dict(preview_run), "rows": preview_rows} if preview_run else None,
            "pending_events": pending_events,
            "incoming": incoming_summary(database_path),
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
               FROM filesystem_events e LEFT JOIN files f ON f.path=e.source_path
               WHERE e.status='pending' ORDER BY e.last_seen_at DESC LIMIT ?""",
            (int(limit),),
        )]
        valid_rows = []
        to_resolve = []
        for r in rows:
            # If a deleted event was recorded, but the file currently exists on disk,
            # it was a transient download file replacement/atomic write. Auto-resolve it!
            if r.get("event_type") == "deleted" and r.get("source_path") and Path(r["source_path"]).exists():
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
                "fingerprints_json": json.dumps(file_record.get("fingerprints") or [], sort_keys=True),
                "scene_metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            }


def inventory(database_path: Path, scenes) -> dict:
    now = utc_now()
    connection = connect(database_path)
    try:
        run_id = connection.execute(
            "INSERT INTO inventory_runs(started_at) VALUES (?)", (now,)
        ).lastrowid
        records = list(flatten_scene_files(scenes))
        summary = {"scenes": len({record["scene_id"] for record in records}), "files": 0,
                   "present": 0, "missing": 0, "changed_paths": 0, "restored": 0}

        for record in records:
            previous = connection.execute(
                "SELECT path, exists_on_disk, missing_since FROM files WHERE file_id = ?",
                (record["file_id"],),
            ).fetchone()
            exists = os.path.isfile(record["path"])
            summary["files"] += 1
            summary["present" if exists else "missing"] += 1
            missing_since = None if exists else (previous["missing_since"] if previous else now)

            if previous and previous["path"] != record["path"]:
                summary["changed_paths"] += 1
                connection.execute(
                    "INSERT INTO inventory_events(run_id,file_id,event_type,old_path,new_path,recorded_at) VALUES (?,?,?,?,?,?)",
                    (run_id, record["file_id"], "stash_path_changed", previous["path"], record["path"], now),
                )
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
                """INSERT INTO files(file_id,scene_id,path,basename,title,studio,performers_json,size,duration,
                       fingerprints_json,scene_metadata_json,exists_on_disk,first_seen_at,last_seen_at,missing_since)
                   VALUES (:file_id,:scene_id,:path,:basename,:title,:studio,:performers_json,:size,:duration,
                       :fingerprints_json,:scene_metadata_json,:exists_on_disk,:first_seen_at,:last_seen_at,:missing_since)
                   ON CONFLICT(file_id) DO UPDATE SET scene_id=excluded.scene_id,path=excluded.path,
                       basename=excluded.basename,title=excluded.title,studio=excluded.studio,
                       performers_json=excluded.performers_json,size=excluded.size,duration=excluded.duration,
                       fingerprints_json=excluded.fingerprints_json,scene_metadata_json=excluded.scene_metadata_json,
                       exists_on_disk=excluded.exists_on_disk,
                       last_seen_at=excluded.last_seen_at,missing_since=excluded.missing_since""",
                {**record, "exists_on_disk": int(exists), "first_seen_at": now, "last_seen_at": now, "missing_since": missing_since},
            )

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
        for record in records:
            exists = os.path.isfile(record["path"])
            previous = connection.execute("SELECT first_seen_at,missing_since FROM files WHERE file_id=?",
                                          (record["file_id"],)).fetchone()
            connection.execute(
                """INSERT INTO files(file_id,scene_id,path,basename,title,studio,performers_json,size,duration,
                       fingerprints_json,scene_metadata_json,exists_on_disk,first_seen_at,last_seen_at,missing_since)
                   VALUES (:file_id,:scene_id,:path,:basename,:title,:studio,:performers_json,:size,:duration,
                       :fingerprints_json,:scene_metadata_json,:exists_on_disk,:first_seen_at,:last_seen_at,:missing_since)
                   ON CONFLICT(file_id) DO UPDATE SET scene_id=excluded.scene_id,path=excluded.path,
                       basename=excluded.basename,title=excluded.title,studio=excluded.studio,
                       performers_json=excluded.performers_json,size=excluded.size,duration=excluded.duration,
                       fingerprints_json=excluded.fingerprints_json,scene_metadata_json=excluded.scene_metadata_json,
                       exists_on_disk=excluded.exists_on_disk,last_seen_at=excluded.last_seen_at,
                       missing_since=excluded.missing_since""",
                {**record, "exists_on_disk": int(exists),
                 "first_seen_at": previous["first_seen_at"] if previous else now, "last_seen_at": now,
                 "missing_since": None if exists else (previous["missing_since"] if previous else now)},
            )
        connection.commit()
        return len(records)
    finally:
        connection.close()


def scene_naming_signature(database_path: Path, scene_id: str):
    """Return only metadata that is allowed to influence a filename."""
    connection = connect(database_path)
    try:
        row = connection.execute(
            "SELECT title,studio,performers_json FROM files WHERE scene_id=? ORDER BY file_id LIMIT 1",
            (str(scene_id),),
        ).fetchone()
        return None if not row else (row["title"] or "", row["studio"] or "", row["performers_json"] or "[]")
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
            """INSERT INTO rename_queue(scene_id,enqueued_at,available_at,status,attempts,last_error)
               VALUES (?,?,?,'pending',0,NULL)
               ON CONFLICT(scene_id) DO UPDATE SET enqueued_at=excluded.enqueued_at,
                   available_at=excluded.available_at,status='pending',last_error=NULL""",
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
            """UPDATE rename_queue SET status='pending'
               WHERE status='processing' AND enqueued_at <= ?""",
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
            "UPDATE rename_queue SET status='processing',attempts=attempts+1 WHERE scene_id=?", (scene_id,)
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
                   event_count=filesystem_events.event_count+1,
                   status=CASE WHEN filesystem_events.status='reviewed' THEN 'reviewed' ELSE 'pending' END""",
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


def _is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def filesystem_monitor_summary(database_path: Path) -> dict:
    connection = connect(database_path)
    try:
        status = connection.execute("SELECT * FROM filesystem_monitor_status WHERE id=1").fetchone()
        if not status:
            return {"state": "stopped", "raw_state": "stopped", "is_stale": False, "heartbeat_age_seconds": None,
                    "pid": None, "pid_alive": False, "started_at": None, "token": None, "heartbeat_at": None,
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
        raw_state = status["state"] or "stopped"
        effective_state = raw_state
        is_stale = False
        stale_reason = None

        if raw_state == "running":
            if pid and not pid_alive:
                is_stale = True
                effective_state = "stale"
                stale_reason = f"Process (PID {pid}) terminated unexpectedly"
            elif heartbeat_age is not None and heartbeat_age > 30.0:
                is_stale = True
                effective_state = "stale"
                stale_reason = f"No heartbeat for {int(heartbeat_age)}s (expected every 2s)"
            elif not heartbeat_at:
                is_stale = True
                effective_state = "stale"
                stale_reason = "No heartbeat recorded since startup"

        return {
            "state": effective_state,
            "raw_state": raw_state,
            "is_stale": is_stale,
            "stale_reason": stale_reason,
            "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None,
            "pid": pid,
            "pid_alive": pid_alive,
            "started_at": status["started_at"],
            "token": status["token"],
            "heartbeat_at": heartbeat_at,
            "roots": json.loads(status["roots_json"] or "[]"),
            "unavailable_roots": json.loads(status["unavailable_roots_json"] or "[]"),
            "pending_events": sum(counts.values()),
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
            if not folder.is_dir():
                result.update(confidence="skipped", reason="Original folder is unavailable")
                summary["skipped_folders"] += 1
            elif record["size"] is None:
                result["reason"] = "Stash has no recorded file size"
                summary["unmatched"] += 1
            else:
                size_matches = []
                for candidate in folder.iterdir():
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
        connectors = r"(?:(?:and|feat\.?|featuring|with|w/|vs\.?|versus|presents|in)|[&,+])"
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
            is_pairing = bool(re.search(r"(?i)(?:and|with|meets|vs|&)", check_title))
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

    connection.execute(
        """UPDATE filename_state SET base_stem=?,base_source=?,source_title=?,manual_studio=?,
           manual_performers_json=?,updated_at=? WHERE file_id=?""",
        (base, source, source_title, current_studio, json.dumps(performers, ensure_ascii=False),
         now, row["file_id"]),
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
           manual_studio,manual_performers_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (row["file_id"], clean_base, source, title or None, None, studio,
         json.dumps(performers, ensure_ascii=False), now, now),
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
        "section_separator": section_separators.get(str(config.get("filenameSectionSeparator") or "dash"), " - "),
        "performer_separator": performer_separators.get(str(config.get("filenamePerformerSeparator") or "comma"), ", "),
    }



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


def _proposed_stem(base: str, studio: str | None, performers: list[str], options: dict | None = None) -> str:
    """Build one canonical filename from the stored base + current Stash metadata."""
    formatting = filename_format_options(options)
    opts = options or {}
    
    title_val = str(base or "").strip()
    
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

            base = str(state["base_stem"] or "").strip()
            proposed_stem = _proposed_stem(base, row["studio"], performers, filename_options)
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
            if len(proposed.name.encode("utf-8")) > 255:
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

def preview_scene_filename(database_path: Path, scene_id: str, filename_options: dict | None = None) -> dict:
    """Return the latest calculated filename proposal for one scene."""
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

        proposed_stem = _proposed_stem(state["base_stem"], row["studio"], performers, filename_options)
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
                "reason": reason, "associated_files": sidecars, "action_performed": False}
    finally:
        connection.close()

def apply_scene_filename(database_path: Path, scene_id: str, move_file, filename_options: dict | None = None) -> dict:
    """Apply one preflighted rename through a supplied Stash move callback."""
    with rename_lock(database_path):
        preview = preview_scene_filename(database_path, scene_id, filename_options)
        if preview.get("status") != "ready":
            return preview
        moved_sidecars = []
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
            connection = connect(database_path)
            try:
                connection.execute(
                    "UPDATE filename_state SET last_generated_stem=?,updated_at=? WHERE file_id=?",
                    (proposed.stem, utc_now(), preview["file_id"]),
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
            finally:
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
                    "action_performed": True}
        except Exception:
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
            connection = connect(database_path)
            try:
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
            finally:
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
                    "action_performed": True}
        except Exception:
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


def generate_video_contact_sheet(
    video_path,
    output_path=None,
    grid="4x4",
    include_banner=True,
    adjust_vertical=True,
    custom_script=None,
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

    # If custom script is specified
    if custom_script and Path(custom_script).is_file():
        cmd = [str(custom_script), str(video_file)]
        if shutil.which("taskpolicy") or os.path.exists("/usr/sbin/taskpolicy"):
            cmd = ["/usr/sbin/taskpolicy", "-b"] + cmd
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if dest_path.exists() or Path(f"{video_file.stem}.jpg").exists():
                actual = dest_path if dest_path.exists() else Path(f"{video_file.stem}.jpg")
                return {"status": "generated", "path": str(actual), "custom_script": True}
        except Exception as e:
            return {"status": "error", "error": f"Custom script error: {e}"}

    # Locate ffmpeg, ffprobe, magick across macOS, Linux, and Windows
    ffmpeg_bin = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else "/usr/bin/ffmpeg" if os.path.exists("/usr/bin/ffmpeg") else "ffmpeg")
    ffprobe_bin = shutil.which("ffprobe") or ("/opt/homebrew/bin/ffprobe" if os.path.exists("/opt/homebrew/bin/ffprobe") else "/usr/bin/ffprobe" if os.path.exists("/usr/bin/ffprobe") else "ffprobe")
    magick_bin = shutil.which("magick") or ("/opt/homebrew/bin/magick" if os.path.exists("/opt/homebrew/bin/magick") else "/usr/bin/magick" if os.path.exists("/usr/bin/magick") else "magick")

    if not (shutil.which(ffmpeg_bin) or os.path.exists(ffmpeg_bin)) or not (shutil.which(ffprobe_bin) or os.path.exists(ffprobe_bin)) or not (shutil.which(magick_bin) or os.path.exists(magick_bin)):
        return {"status": "error", "error": "ffmpeg, ffprobe or magick not found on system"}

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
        "elapsed_seconds": round(elapsed, 2)
    }
