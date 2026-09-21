"""Persistent foundation for grouped external-file reconciliation.

This module deliberately contains no filesystem mutation, hashing, or Stash API
calls.  It owns the durable batch state that later reconciliation phases can
populate and execute through explicit adapters.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import stat
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath


RECONCILIATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS grouped_reconciliation_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_key TEXT NOT NULL UNIQUE,
    operation_type TEXT NOT NULL,
    source_prefix TEXT,
    destination_prefix TEXT,
    state TEXT NOT NULL DEFAULT 'collecting',
    tracked_count INTEGER NOT NULL DEFAULT 0,
    verified_count INTEGER NOT NULL DEFAULT 0,
    uncertain_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    stash_job_id TEXT,
    reason TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    claimed_by TEXT,
    claim_expires_at REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grouped_reconciliation_state
    ON grouped_reconciliation_batches(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_grouped_reconciliation_claim
    ON grouped_reconciliation_batches(claim_expires_at);

CREATE TABLE IF NOT EXISTS grouped_reconciliation_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES grouped_reconciliation_batches(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL,
    scene_id TEXT NOT NULL,
    old_path TEXT NOT NULL,
    relative_path TEXT,
    expected_path TEXT,
    expected_size INTEGER,
    source_fingerprints_json TEXT NOT NULL DEFAULT '[]',
    destination_size INTEGER,
    destination_mtime_ns INTEGER,
    state TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    observed_file_id TEXT,
    observed_scene_id TEXT,
    observed_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, file_id, old_path)
);
CREATE INDEX IF NOT EXISTS idx_grouped_reconciliation_members_batch
    ON grouped_reconciliation_members(batch_id, state);
CREATE INDEX IF NOT EXISTS idx_grouped_reconciliation_members_file
    ON grouped_reconciliation_members(file_id, scene_id);

CREATE TABLE IF NOT EXISTS grouped_reconciliation_event_links (
    batch_id INTEGER NOT NULL REFERENCES grouped_reconciliation_batches(id) ON DELETE CASCADE,
    event_key TEXT NOT NULL REFERENCES filesystem_events(event_key) ON DELETE CASCADE,
    linked_at TEXT NOT NULL,
    PRIMARY KEY(batch_id, event_key),
    UNIQUE(event_key)
);
CREATE INDEX IF NOT EXISTS idx_grouped_reconciliation_event_batch
    ON grouped_reconciliation_event_links(batch_id);
"""


BATCH_STATES = frozenset({
    "collecting",
    "settling",
    "ready_for_review",
    "scanning",
    "verifying",
    "partially_verified",
    "resolved",
    "needs_attention",
    "dismissed",
})

MEMBER_STATES = frozenset({
    "pending",
    "ready",
    "verifying",
    "verified",
    "uncertain",
    "failed",
    "dismissed",
})

TERMINAL_BATCH_STATES = frozenset({"resolved", "dismissed"})

_ALLOWED_TRANSITIONS = {
    "collecting": {"settling", "needs_attention", "dismissed"},
    "settling": {"collecting", "ready_for_review", "needs_attention", "dismissed"},
    "ready_for_review": {"collecting", "scanning", "needs_attention", "dismissed"},
    "scanning": {"verifying", "needs_attention"},
    "verifying": {"resolved", "partially_verified", "needs_attention"},
    "partially_verified": {"scanning", "verifying", "resolved", "needs_attention", "dismissed"},
    "needs_attention": {"collecting", "ready_for_review", "scanning", "dismissed"},
    "resolved": set(),
    "dismissed": set(),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_reconciliation_schema(connection: sqlite3.Connection) -> None:
    """Install the additive grouped-reconciliation schema on an open database."""
    connection.executescript(RECONCILIATION_SCHEMA)


def _connect(database_path: Path | str) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(database_path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    ensure_reconciliation_schema(connection)
    return connection


def _json_object(value: dict | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_or_get_batch(
    database_path: Path | str,
    *,
    correlation_key: str,
    operation_type: str,
    source_prefix: str | None = None,
    destination_prefix: str | None = None,
    state: str = "collecting",
    reason: str | None = None,
    evidence: dict | None = None,
) -> dict:
    """Create one durable batch, or return the existing batch for the same evidence key."""
    key = str(correlation_key or "").strip()
    operation = str(operation_type or "").strip()
    if not key:
        raise ValueError("correlation_key is required")
    if not operation:
        raise ValueError("operation_type is required")
    if state not in BATCH_STATES:
        raise ValueError(f"Unknown reconciliation batch state: {state}")

    now = _utc_now()
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO grouped_reconciliation_batches(
                   correlation_key,operation_type,source_prefix,destination_prefix,state,
                   reason,evidence_json,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(correlation_key) DO NOTHING""",
            (key, operation, source_prefix, destination_prefix, state,
             reason, _json_object(evidence), now, now),
        )
        row = connection.execute(
            "SELECT * FROM grouped_reconciliation_batches WHERE correlation_key=?", (key,)
        ).fetchone()
        connection.commit()
        return dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def add_batch_member(
    database_path: Path | str,
    batch_id: int,
    *,
    file_id: str,
    scene_id: str,
    old_path: str,
    relative_path: str | None = None,
    expected_path: str | None = None,
    expected_size: int | None = None,
    source_fingerprints: list | None = None,
    destination_size: int | None = None,
    destination_mtime_ns: int | None = None,
    member_state: str = "pending",
    reason: str | None = None,
) -> dict:
    """Idempotently attach one inventoried Stash file identity to a batch."""
    if not str(file_id or "").strip() or not str(scene_id or "").strip():
        raise ValueError("file_id and scene_id are required")
    if not str(old_path or "").strip():
        raise ValueError("old_path is required")
    if member_state not in MEMBER_STATES:
        raise ValueError(f"Unknown reconciliation member state: {member_state}")
    now = _utc_now()
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute(
            "SELECT 1 FROM grouped_reconciliation_batches WHERE id=?", (int(batch_id),)
        ).fetchone():
            raise ValueError(f"Reconciliation batch {batch_id} does not exist")
        connection.execute(
            """INSERT INTO grouped_reconciliation_members(
                   batch_id,file_id,scene_id,old_path,relative_path,expected_path,
                   expected_size,source_fingerprints_json,destination_size,destination_mtime_ns,
                   state,reason,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(batch_id,file_id,old_path) DO UPDATE SET
                   scene_id=excluded.scene_id,
                   relative_path=excluded.relative_path,
                   expected_path=excluded.expected_path,
                   expected_size=excluded.expected_size,
                   source_fingerprints_json=excluded.source_fingerprints_json,
                   destination_size=excluded.destination_size,
                   destination_mtime_ns=excluded.destination_mtime_ns,
                   state=excluded.state,
                   reason=excluded.reason,
                   updated_at=excluded.updated_at""",
            (int(batch_id), str(file_id), str(scene_id), str(old_path), relative_path,
             expected_path, expected_size,
             json.dumps(source_fingerprints or [], ensure_ascii=False, sort_keys=True),
             destination_size, destination_mtime_ns, member_state, reason, now, now),
        )
        row = connection.execute(
            """SELECT * FROM grouped_reconciliation_members
               WHERE batch_id=? AND file_id=? AND old_path=?""",
            (int(batch_id), str(file_id), str(old_path)),
        ).fetchone()
        counts = connection.execute(
            """SELECT COUNT(*) AS tracked,
                      SUM(CASE WHEN state='verified' THEN 1 ELSE 0 END) AS verified,
                      SUM(CASE WHEN state IN ('pending','uncertain') THEN 1 ELSE 0 END) AS uncertain,
                      SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed
               FROM grouped_reconciliation_members WHERE batch_id=?""",
            (int(batch_id),),
        ).fetchone()
        connection.execute(
            """UPDATE grouped_reconciliation_batches
               SET tracked_count=?,verified_count=?,uncertain_count=?,failed_count=?,updated_at=? WHERE id=?""",
            (int(counts["tracked"] or 0), int(counts["verified"] or 0),
             int(counts["uncertain"] or 0), int(counts["failed"] or 0), now, int(batch_id)),
        )
        connection.commit()
        return dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def link_event_to_batch(
    database_path: Path | str,
    batch_id: int,
    event_key: str,
) -> bool:
    """Link an existing low-level event to exactly one batch without resolving it."""
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute(
            "SELECT 1 FROM grouped_reconciliation_batches WHERE id=?", (int(batch_id),)
        ).fetchone():
            raise ValueError(f"Reconciliation batch {batch_id} does not exist")
        if not connection.execute(
            "SELECT 1 FROM filesystem_events WHERE event_key=?", (str(event_key),)
        ).fetchone():
            raise ValueError("Filesystem event does not exist")
        cursor = connection.execute(
            """INSERT INTO grouped_reconciliation_event_links(batch_id,event_key,linked_at)
               VALUES (?,?,?) ON CONFLICT(batch_id,event_key) DO NOTHING""",
            (int(batch_id), str(event_key), _utc_now()),
        )
        connection.commit()
        return cursor.rowcount == 1
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        if "event_key" in str(exc).lower() or "unique" in str(exc).lower():
            raise ValueError("Filesystem event already belongs to another reconciliation batch") from exc
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def transition_batch(
    database_path: Path | str,
    batch_id: int,
    new_state: str,
    *,
    expected_state: str | None = None,
    reason: str | None = None,
    stash_job_id: str | None = None,
) -> dict:
    """Atomically move a batch through the explicit reconciliation state machine."""
    if new_state not in BATCH_STATES:
        raise ValueError(f"Unknown reconciliation batch state: {new_state}")
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM grouped_reconciliation_batches WHERE id=?", (int(batch_id),)
        ).fetchone()
        if not row:
            raise ValueError(f"Reconciliation batch {batch_id} does not exist")
        current = row["state"]
        if expected_state is not None and current != expected_state:
            raise RuntimeError(f"Batch state changed from {expected_state} to {current}")
        if new_state != current and new_state not in _ALLOWED_TRANSITIONS[current]:
            raise ValueError(f"Invalid reconciliation transition: {current} -> {new_state}")
        connection.execute(
            """UPDATE grouped_reconciliation_batches
               SET state=?,reason=COALESCE(?,reason),stash_job_id=COALESCE(?,stash_job_id),updated_at=?
               WHERE id=?""",
            (new_state, reason, stash_job_id, _utc_now(), int(batch_id)),
        )
        updated = connection.execute(
            "SELECT * FROM grouped_reconciliation_batches WHERE id=?", (int(batch_id),)
        ).fetchone()
        connection.commit()
        return dict(updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_batch(
    database_path: Path | str,
    batch_id: int,
    owner: str,
    *,
    lease_seconds: float = 30.0,
    now: float | None = None,
) -> bool:
    """Atomically lease a non-terminal batch to one coordinator."""
    owner = str(owner or "").strip()
    if not owner:
        raise ValueError("owner is required")
    now_value = time.time() if now is None else float(now)
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE grouped_reconciliation_batches
               SET claimed_by=?,claim_expires_at=?,updated_at=?
               WHERE id=? AND state NOT IN ('resolved','dismissed')
                 AND (claimed_by IS NULL OR claim_expires_at IS NULL OR claim_expires_at<=? OR claimed_by=?)""",
            (owner, now_value + max(0.1, float(lease_seconds)), _utc_now(),
             int(batch_id), now_value, owner),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def release_batch_claim(database_path: Path | str, batch_id: int, owner: str) -> bool:
    connection = _connect(database_path)
    try:
        cursor = connection.execute(
            """UPDATE grouped_reconciliation_batches
               SET claimed_by=NULL,claim_expires_at=NULL,updated_at=?
               WHERE id=? AND claimed_by=?""",
            (_utc_now(), int(batch_id), str(owner)),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def release_expired_claims(database_path: Path | str, *, now: float | None = None) -> int:
    """Release abandoned leases without changing batch or member state."""
    now_value = time.time() if now is None else float(now)
    connection = _connect(database_path)
    try:
        cursor = connection.execute(
            """UPDATE grouped_reconciliation_batches
               SET claimed_by=NULL,claim_expires_at=NULL,updated_at=?
               WHERE claimed_by IS NOT NULL AND claim_expires_at IS NOT NULL AND claim_expires_at<=?""",
            (_utc_now(), now_value),
        )
        connection.commit()
        return int(cursor.rowcount)
    finally:
        connection.close()


def list_unfinished_batches(database_path: Path | str) -> list[dict]:
    connection = _connect(database_path)
    try:
        return [dict(row) for row in connection.execute(
            """SELECT * FROM grouped_reconciliation_batches
               WHERE state NOT IN ('resolved','dismissed') ORDER BY created_at,id"""
        )]
    finally:
        connection.close()


def batch_snapshot(database_path: Path | str, batch_id: int) -> dict | None:
    """Return durable batch, member, and event-link state for tests and later UI adapters."""
    connection = _connect(database_path)
    try:
        row = connection.execute(
            "SELECT * FROM grouped_reconciliation_batches WHERE id=?", (int(batch_id),)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["evidence"] = json.loads(result.pop("evidence_json") or "{}")
        result["members"] = [dict(member) for member in connection.execute(
            "SELECT * FROM grouped_reconciliation_members WHERE batch_id=? ORDER BY id", (int(batch_id),)
        )]
        result["event_keys"] = [link["event_key"] for link in connection.execute(
            "SELECT event_key FROM grouped_reconciliation_event_links WHERE batch_id=? ORDER BY linked_at,event_key",
            (int(batch_id),),
        )]
        return result
    finally:
        connection.close()


def _path_flavour(path: str):
    value = str(path or "")
    return PureWindowsPath if ("\\" in value or (len(value) > 1 and value[1] == ":")) else PurePosixPath


def _path_key(path: str) -> str:
    flavour = _path_flavour(path)
    value = unicodedata.normalize(
        "NFC", str(flavour(str(path or ""))).replace("\\", "/").rstrip("/")
    )
    return value.casefold() if flavour is PureWindowsPath else value


def _basename(path: str) -> str:
    return _path_flavour(path)(str(path)).name


def _is_beneath(path: str, prefix: str) -> bool:
    path_key = _path_key(path)
    prefix_key = _path_key(prefix)
    return bool(prefix_key and (path_key == prefix_key or path_key.startswith(prefix_key + "/")))


def _relative_path(path: str, prefix: str) -> str | None:
    flavour = _path_flavour(path)
    try:
        path_obj = flavour(str(path))
        prefix_obj = flavour(str(prefix))
        return str(path_obj.relative_to(prefix_obj))
    except (ValueError, TypeError):
        path_key = _path_key(path)
        prefix_key = _path_key(prefix)
        if path_key.startswith(prefix_key + "/"):
            return str(path).replace("\\", "/")[len(str(prefix).replace("\\", "/").rstrip("/")) + 1:]
        return None


def _join_path(prefix: str, relative: str) -> str:
    flavour = _path_flavour(prefix)
    return str(flavour(str(prefix)) / flavour(str(relative)))


def _derive_prefix_mapping(old_path: str, new_path: str) -> tuple[str, str] | None:
    """Infer one conservative old-prefix/new-prefix mapping from a paired file path."""
    old_flavour = _path_flavour(old_path)
    new_flavour = _path_flavour(new_path)
    if old_flavour is not new_flavour:
        return None
    old_parts = list(old_flavour(str(old_path)).parts)
    new_parts = list(new_flavour(str(new_path)).parts)
    suffix_count = 0
    while (suffix_count < len(old_parts) and suffix_count < len(new_parts)
           and old_parts[-1 - suffix_count].casefold() == new_parts[-1 - suffix_count].casefold()):
        suffix_count += 1
    if suffix_count < 1:
        return None
    # When the moved folder retains its own name, keep that top common component
    # in both prefixes and use only its descendants as relative paths.
    relative_count = suffix_count - 1 if suffix_count > 1 else suffix_count
    old_prefix_parts = old_parts[:-relative_count] if relative_count else old_parts
    new_prefix_parts = new_parts[:-relative_count] if relative_count else new_parts
    if not old_prefix_parts or not new_prefix_parts:
        return None
    old_prefix = str(old_flavour(*old_prefix_parts))
    new_prefix = str(new_flavour(*new_prefix_parts))
    if _path_key(old_prefix) == _path_key(new_prefix):
        return None
    return old_prefix, new_prefix


def _parse_event_time(value: str | None) -> float:
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return 0.0


def _default_path_probe(path: str) -> dict:
    try:
        stat_result = Path(path).stat()
        return {
            "status": "ok",
            "exists": True,
            "is_file": stat.S_ISREG(stat_result.st_mode),
            "is_dir": stat.S_ISDIR(stat_result.st_mode),
            "size": int(stat_result.st_size),
            "mtime_ns": int(stat_result.st_mtime_ns),
        }
    except FileNotFoundError:
        return {"status": "missing", "exists": False}
    except OSError as exc:
        return {"status": "unavailable", "exists": None, "error": str(exc)}


def _unavailable_roots(connection: sqlite3.Connection) -> list[str]:
    row = connection.execute(
        "SELECT unavailable_roots_json FROM filesystem_monitor_status WHERE id=1"
    ).fetchone()
    if not row:
        return []
    try:
        return [str(item) for item in json.loads(row["unavailable_roots_json"] or "[]")]
    except (TypeError, ValueError):
        return []


def _group_key(operation_type: str, source_prefix: str, destination_prefix: str, event_keys: list[str]) -> str:
    operation_family = "copy" if "copy" in str(operation_type) else "move"
    evidence = json.dumps(
        [operation_family, _path_key(source_prefix), _path_key(destination_prefix)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "group-v1:" + hashlib.sha256(evidence.encode("utf-8")).hexdigest()


def _member_evidence(
    row: dict,
    source_prefix: str,
    destination_prefix: str,
    operation_type: str,
    probe,
    unavailable_roots: list[str],
) -> dict:
    relative = _relative_path(row["path"], source_prefix)
    if relative is None:
        return {"state": "uncertain", "reason": "Original path is outside the inferred source prefix"}
    expected = _join_path(destination_prefix, relative)
    if any(_is_beneath(row["path"], root) or _is_beneath(expected, root) for root in unavailable_roots):
        return {
            "relative_path": relative,
            "expected_path": expected,
            "state": "uncertain",
            "reason": "Source or destination library root is unavailable",
        }
    destination = probe(expected)
    if destination.get("status") != "ok" or not destination.get("is_file"):
        return {
            "relative_path": relative,
            "expected_path": expected,
            "state": "uncertain",
            "reason": "Expected destination file is not currently available",
        }
    expected_size = row.get("size")
    observed_size = destination.get("size")
    if expected_size is not None and observed_size is not None and int(expected_size) != int(observed_size):
        return {
            "relative_path": relative,
            "expected_path": expected,
            "destination_size": observed_size,
            "destination_mtime_ns": destination.get("mtime_ns"),
            "state": "uncertain",
            "reason": "Destination size differs from the inventoried source",
        }
    source = probe(row["path"])
    if operation_type == "folder_copy" and source.get("exists") is not True:
        state, reason = "uncertain", "Original file is no longer present, so this is not a verified copy"
    elif operation_type != "folder_copy" and source.get("exists") is True:
        state, reason = "uncertain", "Original file is still present, so the move is incomplete or is a copy"
    elif source.get("status") == "unavailable":
        state, reason = "uncertain", "Original path could not be checked"
    else:
        state, reason = "ready", "Relative destination exists and matches the inventoried size"
    return {
        "relative_path": relative,
        "expected_path": expected,
        "destination_size": observed_size,
        "destination_mtime_ns": destination.get("mtime_ns"),
        "state": state,
        "reason": reason,
    }


def _complete_settling_batch(
    database_path: Path | str,
    batch: dict,
    selected_members: list[dict],
    event_keys: list[str],
    probe,
    unavailable_roots: list[str],
) -> dict:
    """Idempotently finish a durable settling batch after normal work or restart."""
    for row in selected_members:
        evidence = _member_evidence(
            row, batch["source_prefix"], batch["destination_prefix"],
            batch["operation_type"], probe, unavailable_roots,
        )
        try:
            fingerprints = json.loads(row.get("fingerprints_json") or "[]")
        except (TypeError, ValueError):
            fingerprints = []
        add_batch_member(
            database_path,
            batch["id"],
            file_id=str(row["file_id"]),
            scene_id=str(row["scene_id"]),
            old_path=row["path"],
            relative_path=evidence.get("relative_path"),
            expected_path=evidence.get("expected_path"),
            expected_size=row.get("size"),
            source_fingerprints=fingerprints,
            destination_size=evidence.get("destination_size"),
            destination_mtime_ns=evidence.get("destination_mtime_ns"),
            member_state=evidence["state"],
            reason=evidence["reason"],
        )
    for event_key in event_keys:
        link_event_to_batch(database_path, batch["id"], event_key)
    snapshot = batch_snapshot(database_path, batch["id"])
    target_state = "ready_for_review" if any(
        member["state"] == "ready" for member in snapshot["members"]
    ) else "needs_attention"
    transition_batch(database_path, batch["id"], target_state, expected_state="settling")
    return batch_snapshot(database_path, batch["id"])


def detect_settled_operations(
    database_path: Path | str,
    *,
    settle_seconds: float = 15.0,
    now: float | None = None,
    max_events: int = 1000,
    path_probe=None,
) -> list[dict]:
    """Create read-only grouped proposals from settled, unclaimed filesystem events.

    This detector performs metadata-only path checks. It never hashes file bodies,
    calls Stash, changes event status, or mutates media.
    """
    now_value = time.time() if now is None else float(now)
    cutoff = now_value - max(0.0, float(settle_seconds))
    probe = path_probe or _default_path_probe
    connection = _connect(database_path)
    try:
        unavailable = _unavailable_roots(connection)
        files = [dict(row) for row in connection.execute("SELECT * FROM files ORDER BY path")]
    finally:
        connection.close()

    snapshots = []
    files_by_id = {str(row["file_id"]): row for row in files}
    for unfinished in list_unfinished_batches(database_path):
        if unfinished["state"] != "settling":
            continue
        try:
            stored_evidence = json.loads(unfinished.get("evidence_json") or "{}")
        except (TypeError, ValueError):
            stored_evidence = {}
        member_ids = [str(value) for value in stored_evidence.get("member_file_ids") or []]
        event_keys = [str(value) for value in stored_evidence.get("event_keys") or []]
        if not member_ids or not event_keys or any(value not in files_by_id for value in member_ids):
            continue
        snapshots.append(_complete_settling_batch(
            database_path, unfinished, [files_by_id[value] for value in member_ids],
            event_keys, probe, unavailable,
        ))

    connection = _connect(database_path)
    try:
        events = [dict(row) for row in connection.execute(
            """SELECT e.* FROM filesystem_events e
               LEFT JOIN grouped_reconciliation_event_links l ON l.event_key=e.event_key
               WHERE e.status='pending' AND l.event_key IS NULL
               ORDER BY e.first_seen_at,e.event_key LIMIT ?""",
            (max(1, int(max_events)),),
        ) if _parse_event_time(row["last_seen_at"]) <= cutoff]
    finally:
        connection.close()
    if not events:
        return snapshots

    files_by_path = {_path_key(row["path"]): row for row in files}
    created_events = [event for event in events if event["event_type"] == "created" and not event["is_directory"]]
    deleted_events = [event for event in events if event["event_type"] == "deleted" and not event["is_directory"]]
    direct_move_events = [event for event in events if event["event_type"] == "moved" and not event["is_directory"]]
    directory_moves = [event for event in events if event["event_type"] == "moved" and event["is_directory"]]
    candidates: list[dict] = []

    for event in directory_moves:
        candidates.append({
            "source_prefix": event["source_path"],
            "destination_prefix": event["destination_path"],
            "operation_hint": "folder_move",
            "pairs": [],
            "event_keys": [event["event_key"]],
            "directory_event": True,
        })

    pairs = []
    for event in direct_move_events:
        row = files_by_path.get(_path_key(event["source_path"]))
        mapping = _derive_prefix_mapping(event["source_path"], event["destination_path"] or "")
        if row and mapping:
            pairs.append({"row": row, "source": event["source_path"], "destination": event["destination_path"],
                          "event_keys": [event["event_key"]], "mapping": mapping, "source_expected": "missing"})

    created_by_name: dict[str, list[dict]] = {}
    for event in created_events:
        created_by_name.setdefault(_basename(event["source_path"]).casefold(), []).append(event)
    possible_split_pairs = []
    for deleted in deleted_events:
        row = files_by_path.get(_path_key(deleted["source_path"]))
        if not row:
            continue
        matches = created_by_name.get(_basename(deleted["source_path"]).casefold(), [])
        viable = []
        for created in matches:
            destination_info = probe(created["source_path"])
            if destination_info.get("status") != "ok" or not destination_info.get("is_file"):
                continue
            if row.get("size") is not None and destination_info.get("size") is not None:
                if int(row["size"]) != int(destination_info["size"]):
                    continue
            viable.append(created)
        if len(viable) == 1:
            possible_split_pairs.append((deleted, row, viable[0]))
    created_use_counts: dict[str, int] = {}
    for _deleted, _row, created in possible_split_pairs:
        created_use_counts[created["event_key"]] = created_use_counts.get(created["event_key"], 0) + 1
    for deleted, row, created in possible_split_pairs:
        if created_use_counts[created["event_key"]] != 1:
            continue
        if len([item for item in possible_split_pairs if item[0]["event_key"] == deleted["event_key"]]) != 1:
            continue
        mapping = _derive_prefix_mapping(deleted["source_path"], created["source_path"])
        if mapping:
            pairs.append({"row": row, "source": deleted["source_path"], "destination": created["source_path"],
                          "event_keys": [deleted["event_key"], created["event_key"]],
                          "mapping": mapping, "source_expected": "missing"})

    # Created files whose inventoried originals remain present are possible copies.
    paired_created_keys = {key for pair in pairs for key in pair["event_keys"] if key in {e["event_key"] for e in created_events}}
    for created in created_events:
        if created["event_key"] in paired_created_keys:
            continue
        destination_info = probe(created["source_path"])
        if destination_info.get("status") != "ok" or not destination_info.get("is_file"):
            continue
        matches = [row for row in files
                   if _basename(row["path"]).casefold() == _basename(created["source_path"]).casefold()
                   and (row.get("size") is None or destination_info.get("size") is None
                        or int(row["size"]) == int(destination_info["size"]))]
        if len(matches) == 1 and probe(matches[0]["path"]).get("exists") is True:
            mapping = _derive_prefix_mapping(matches[0]["path"], created["source_path"])
            if mapping:
                pairs.append({"row": matches[0], "source": matches[0]["path"], "destination": created["source_path"],
                              "event_keys": [created["event_key"]], "mapping": mapping,
                              "source_expected": "present"})

    grouped_pairs: dict[tuple[str, str, str], list[dict]] = {}
    for pair in pairs:
        operation_hint = "folder_copy" if pair["source_expected"] == "present" else "folder_move"
        source_prefix, destination_prefix = pair["mapping"]
        grouped_pairs.setdefault((operation_hint, _path_key(source_prefix), _path_key(destination_prefix)), []).append(pair)
    for (operation_hint, _source_key, _destination_key), group_pairs in grouped_pairs.items():
        if len(group_pairs) < 2:
            continue
        candidates.append({
            "source_prefix": group_pairs[0]["mapping"][0],
            "destination_prefix": group_pairs[0]["mapping"][1],
            "operation_hint": operation_hint,
            "pairs": group_pairs,
            "event_keys": sorted({key for pair in group_pairs for key in pair["event_keys"]}),
            "directory_event": False,
        })

    used_event_keys: set[str] = set()
    for candidate in candidates:
        source_prefix = candidate["source_prefix"]
        destination_prefix = candidate["destination_prefix"]
        if not destination_prefix:
            continue
        if any(_is_beneath(source_prefix, root) or _is_beneath(destination_prefix, root) for root in unavailable):
            continue
        if used_event_keys.intersection(candidate["event_keys"]):
            continue
        inventory_members = [row for row in files if _is_beneath(row["path"], source_prefix)]
        paired_ids = {str(pair["row"]["file_id"]) for pair in candidate["pairs"]}
        if candidate["directory_event"]:
            selected_members = inventory_members
            operation_type = "folder_move"
        else:
            coverage_complete = bool(inventory_members) and paired_ids == {str(row["file_id"]) for row in inventory_members}
            if candidate["operation_hint"] == "folder_copy":
                operation_type = "folder_copy" if coverage_complete else "bulk_copy"
            else:
                operation_type = "folder_move" if coverage_complete else "bulk_move"
            selected_members = inventory_members if coverage_complete else [pair["row"] for pair in candidate["pairs"]]
        if not selected_members:
            continue
        if candidate["directory_event"]:
            old_paths = {_path_key(row["path"]) for row in selected_members}
            destination_paths = {
                _path_key(_join_path(destination_prefix, _relative_path(row["path"], source_prefix)))
                for row in selected_members if _relative_path(row["path"], source_prefix) is not None
            }
            candidate["event_keys"] = sorted({
                event["event_key"] for event in events
                if (event["event_key"] in candidate["event_keys"]
                    or _path_key(event["source_path"]) in old_paths
                    or _path_key(event["source_path"]) in destination_paths
                    or (event.get("destination_path") and
                        _path_key(event["destination_path"]) in destination_paths))
            })

        correlation_key = _group_key(
            operation_type, source_prefix, destination_prefix, candidate["event_keys"]
        )
        batch = create_or_get_batch(
            database_path,
            correlation_key=correlation_key,
            operation_type=operation_type,
            source_prefix=source_prefix,
            destination_prefix=destination_prefix,
            state="collecting",
            reason="Settled filesystem events share one old-folder to new-folder prefix mapping",
            evidence={
                "event_count": len(candidate["event_keys"]),
                "paired_file_count": len(paired_ids),
                "inventory_file_count": len(inventory_members),
                "selected_member_count": len(selected_members),
                "member_file_ids": [str(row["file_id"]) for row in selected_members],
                "event_keys": candidate["event_keys"],
                "directory_event": bool(candidate["directory_event"]),
            },
        )
        if batch["state"] in ("ready_for_review", "needs_attention"):
            transition_batch(database_path, batch["id"], "collecting", expected_state=batch["state"])
            batch = batch_snapshot(database_path, batch["id"])
        if batch["state"] == "collecting":
            transition_batch(database_path, batch["id"], "settling", expected_state="collecting")
            batch = batch_snapshot(database_path, batch["id"])
        elif batch["state"] != "settling":
            used_event_keys.update(candidate["event_keys"])
            snapshot = batch_snapshot(database_path, batch["id"])
            if snapshot:
                snapshots.append(snapshot)
            continue
        snapshot = _complete_settling_batch(
            database_path, batch, selected_members, candidate["event_keys"], probe, unavailable
        )
        used_event_keys.update(candidate["event_keys"])
        snapshots.append(snapshot)
    return snapshots


class ReconciliationCoordinator:
    """Persistent lifecycle and read-only event-correlation adapter."""

    def __init__(self, database_path: Path | str, owner: str):
        self.database_path = Path(database_path)
        self.owner = str(owner)

    def recover_after_restart(self, *, now: float | None = None) -> list[dict]:
        release_expired_claims(self.database_path, now=now)
        return list_unfinished_batches(self.database_path)

    def detect_settled_events(
        self,
        *,
        settle_seconds: float = 15.0,
        now: float | None = None,
        max_events: int = 1000,
        path_probe=None,
    ) -> list[dict]:
        return detect_settled_operations(
            self.database_path,
            settle_seconds=settle_seconds,
            now=now,
            max_events=max_events,
            path_probe=path_probe,
        )

    def stop(self) -> int:
        connection = _connect(self.database_path)
        try:
            cursor = connection.execute(
                """UPDATE grouped_reconciliation_batches
                   SET claimed_by=NULL,claim_expires_at=NULL,updated_at=? WHERE claimed_by=?""",
                (_utc_now(), self.owner),
            )
            connection.commit()
            return int(cursor.rowcount)
        finally:
            connection.close()


def list_review_batches(database_path: Path | str, *, limit: int = 50) -> list[dict]:
    """Return non-terminal grouped operations for the dashboard review layer."""
    connection = _connect(database_path)
    try:
        rows = [dict(row) for row in connection.execute(
            """SELECT id FROM grouped_reconciliation_batches
               WHERE state IN ('ready_for_review','needs_attention','partially_verified','scanning','verifying')
               ORDER BY updated_at DESC,id DESC LIMIT ?""",
            (max(1, min(int(limit), 200)),),
        )]
    finally:
        connection.close()
    return [snapshot for row in rows
            if (snapshot := batch_snapshot(database_path, row["id"])) is not None]


def dismiss_review_batch(database_path: Path | str, batch_id: int) -> dict:
    """Acknowledge one grouped proposal without touching media or Stash records."""
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM grouped_reconciliation_batches WHERE id=?", (int(batch_id),)
        ).fetchone()
        if not row:
            raise ValueError(f"Reconciliation batch {batch_id} does not exist")
        if row["state"] not in ("ready_for_review", "needs_attention", "partially_verified"):
            raise ValueError("This grouped operation is not awaiting review")
        now = _utc_now()
        connection.execute(
            """UPDATE grouped_reconciliation_batches
               SET state='dismissed',reason=?,claimed_by=NULL,claim_expires_at=NULL,updated_at=?
               WHERE id=?""",
            ("User dismissed grouped reconciliation review; no filesystem or Stash action was taken",
             now, int(batch_id)),
        )
        connection.execute(
            """UPDATE grouped_reconciliation_members SET state='dismissed',updated_at=?
               WHERE batch_id=? AND state!='verified'""",
            (now, int(batch_id)),
        )
        connection.execute(
            """UPDATE filesystem_events SET status='reviewed'
               WHERE event_key IN (
                   SELECT event_key FROM grouped_reconciliation_event_links WHERE batch_id=?
               )""",
            (int(batch_id),),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return batch_snapshot(database_path, int(batch_id))

_GROUPED_SCENE_FILES_QUERY = """
query WatchtowerGroupedSceneFiles($id: ID!) {
  findScene(id: $id) { id files { id path basename size fingerprints { type value } } }
}
"""

_GROUPED_SCENE_BY_PATH_QUERY = """
query WatchtowerGroupedSceneByPath($path: String!) {
  findScenes(scene_filter: {path: {value: $path, modifier: EQUALS}}, filter: {per_page: 10}) {
    scenes { id files { id path basename } }
  }
}
"""


def _normalized_filesystem_path(path: str | None) -> str:
    return _path_key(str(path or "")) if path else ""


def _set_member_result(
    connection: sqlite3.Connection,
    member_id: int,
    state: str,
    reason: str,
    *,
    observed_file_id: str | None = None,
    observed_scene_id: str | None = None,
    observed_path: str | None = None,
    destination_size: int | None = None,
    destination_mtime_ns: int | None = None,
) -> None:
    connection.execute(
        """UPDATE grouped_reconciliation_members
           SET state=?,reason=?,observed_file_id=?,observed_scene_id=?,observed_path=?,
               destination_size=COALESCE(?,destination_size),
               destination_mtime_ns=COALESCE(?,destination_mtime_ns),updated_at=?
           WHERE id=?""",
        (state, reason, observed_file_id, observed_scene_id, observed_path,
         destination_size, destination_mtime_ns, _utc_now(), int(member_id)),
    )


def _refresh_batch_counts(connection: sqlite3.Connection, batch_id: int) -> dict:
    counts = connection.execute(
        """SELECT COUNT(*) AS tracked,
                  SUM(CASE WHEN state='verified' THEN 1 ELSE 0 END) AS verified,
                  SUM(CASE WHEN state IN ('pending','ready','verifying','uncertain') THEN 1 ELSE 0 END) AS uncertain,
                  SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed
           FROM grouped_reconciliation_members WHERE batch_id=?""",
        (int(batch_id),),
    ).fetchone()
    result = {
        "tracked": int(counts["tracked"] or 0),
        "verified": int(counts["verified"] or 0),
        "uncertain": int(counts["uncertain"] or 0),
        "failed": int(counts["failed"] or 0),
    }
    connection.execute(
        """UPDATE grouped_reconciliation_batches
           SET tracked_count=?,verified_count=?,uncertain_count=?,failed_count=?,updated_at=?
           WHERE id=?""",
        (result["tracked"], result["verified"], result["uncertain"], result["failed"],
         _utc_now(), int(batch_id)),
    )
    return result


def _revalidate_grouped_move_members(database_path: Path | str, batch_id: int, path_probe) -> int:
    """Recheck candidate metadata immediately before a user-approved Stash scan."""
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        members = connection.execute(
            """SELECT * FROM grouped_reconciliation_members
               WHERE batch_id=? AND state!='verified' ORDER BY id""",
            (int(batch_id),),
        ).fetchall()
        ready = 0
        for member in members:
            expected_path = member["expected_path"]
            if not expected_path:
                _set_member_result(connection, member["id"], "uncertain", "No expected destination path is available")
                continue
            result = path_probe(expected_path)
            if result.get("status") != "ok" or not result.get("is_file"):
                _set_member_result(connection, member["id"], "uncertain", "Expected destination file is unavailable")
                continue
            size = result.get("size")
            mtime_ns = result.get("mtime_ns")
            if member["expected_size"] is not None and size is not None and int(member["expected_size"]) != int(size):
                _set_member_result(connection, member["id"], "uncertain", "Destination size changed or differs from the inventoried source",
                                   destination_size=size, destination_mtime_ns=mtime_ns)
                continue
            if (member["destination_mtime_ns"] is not None and mtime_ns is not None
                    and int(member["destination_mtime_ns"]) != int(mtime_ns)):
                _set_member_result(connection, member["id"], "uncertain", "Destination changed after grouped review was created",
                                   destination_size=size, destination_mtime_ns=mtime_ns)
                continue
            source_result = path_probe(member["old_path"])
            if source_result.get("status") == "unavailable":
                _set_member_result(connection, member["id"], "uncertain", "Original path could not be checked")
                continue
            if source_result.get("exists") is True:
                _set_member_result(connection, member["id"], "uncertain", "Original file is still present, so this is not a verified move")
                continue
            _set_member_result(connection, member["id"], "ready", "Destination metadata is stable and the original path is absent",
                               destination_size=size, destination_mtime_ns=mtime_ns)
            ready += 1
        _refresh_batch_counts(connection, batch_id)
        connection.commit()
        return ready
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _verify_grouped_members(database_path: Path | str, batch_id: int, stash, path_probe) -> dict:
    """Verify ownership without holding a database lock during Stash requests."""
    snapshot = batch_snapshot(database_path, batch_id)
    results = []
    for member in snapshot["members"]:
        if member["state"] == "verified":
            continue
        if member["state"] not in ("ready", "verifying"):
            continue
        expected_path = member.get("expected_path")
        expected_key = _normalized_filesystem_path(expected_path)
        current_file = path_probe(expected_path)
        if current_file.get("status") != "ok" or not current_file.get("is_file"):
            results.append((member, "uncertain", "Expected destination became unavailable during the Stash scan", {}))
            continue
        if (member.get("destination_size") is not None and current_file.get("size") is not None
                and int(member["destination_size"]) != int(current_file["size"])):
            results.append((member, "uncertain", "Destination size changed during the Stash scan", {}))
            continue
        if (member.get("destination_mtime_ns") is not None and current_file.get("mtime_ns") is not None
                and int(member["destination_mtime_ns"]) != int(current_file["mtime_ns"])):
            results.append((member, "uncertain", "Destination changed during the Stash scan", {}))
            continue
        try:
            scene_result = stash.call_GQL(
                _GROUPED_SCENE_FILES_QUERY, {"id": str(member["scene_id"])}
            )
            scene = (scene_result or {}).get("findScene")
        except Exception as exc:
            results.append((member, "uncertain", f"Could not verify Stash scene: {exc}", {}))
            continue
        if not scene or str(scene.get("id")) != str(member["scene_id"]):
            results.append((member, "uncertain", "Original Stash scene is unavailable", {}))
            continue
        exact = [item for item in (scene.get("files") or [])
                 if str(item.get("id")) == str(member["file_id"])
                 and _normalized_filesystem_path(item.get("path")) == expected_key]
        if len(exact) != 1:
            same_path = [item for item in (scene.get("files") or [])
                         if _normalized_filesystem_path(item.get("path")) == expected_key]
            if same_path:
                observed = same_path[0]
                original = next((item for item in (scene.get("files") or [])
                                 if str(item.get("id")) == str(member["file_id"])), None)
                original_fingerprints = {
                    (str(item.get("type") or "").lower(), str(item.get("value") or ""))
                    for item in ((original or {}).get("fingerprints") or [])
                    if item.get("type") and item.get("value")
                }
                observed_fingerprints = {
                    (str(item.get("type") or "").lower(), str(item.get("value") or ""))
                    for item in (observed.get("fingerprints") or [])
                    if item.get("type") and item.get("value")
                }
                strong_match = bool({pair for pair in original_fingerprints & observed_fingerprints
                                     if pair[0] in {"oshash", "sha256", "md5"}})
                stale_old_attachment = (
                    original is not None
                    and _normalized_filesystem_path(original.get("path"))
                    == _normalized_filesystem_path(member.get("old_path"))
                    and path_probe(member.get("old_path")).get("status") == "missing"
                    and strong_match
                    and (original.get("size") is None or observed.get("size") is None
                         or int(original.get("size")) == int(observed.get("size")))
                )
                reason = (
                    f"Stash still lists file ID {member['file_id']} at the missing old path, while "
                    f"the same scene uses matching file ID {observed.get('id')} at the renamed path. "
                    "Confirm the scene plays, run Stash Clean to remove the missing attachment, then recheck."
                    if stale_old_attachment else
                    "Expected path is attached to the scene with a different Stash file ID"
                )
                results.append((member, "uncertain",
                                reason,
                                {"observed_file_id": str(observed.get("id") or ""),
                                 "observed_scene_id": str(scene.get("id") or ""),
                                 "observed_path": observed.get("path")}))
            else:
                results.append((member, "uncertain",
                                "Original scene/file identity was not found at the expected path", {}))
            continue
        try:
            owners_result = stash.call_GQL(
                _GROUPED_SCENE_BY_PATH_QUERY, {"path": str(expected_path)}
            )
            owners = set()
            for owner_scene in ((owners_result or {}).get("findScenes") or {}).get("scenes") or []:
                if any(_normalized_filesystem_path(item.get("path")) == expected_key
                       for item in owner_scene.get("files") or []):
                    owners.add(str(owner_scene.get("id")))
        except Exception as exc:
            results.append((member, "uncertain", f"Could not verify destination ownership: {exc}", {}))
            continue
        if owners != {str(member["scene_id"])}:
            label = ", ".join(sorted(owners)) if owners else "no scene"
            results.append((member, "uncertain",
                            f"Destination ownership is {label}; expected only scene {member['scene_id']}", {}))
            continue
        observed = exact[0]
        results.append((member, "verified",
                        "Stash retained the original scene and file identity at the expected path",
                        {"observed_file_id": str(observed.get("id")),
                         "observed_scene_id": str(scene.get("id")),
                         "observed_path": observed.get("path")}))

    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for member, state, reason, observed in results:
            if state == "verified":
                local = connection.execute(
                    "SELECT scene_id,path FROM files WHERE file_id=?",
                    (str(member["file_id"]),),
                ).fetchone()
                allowed_paths = {
                    _normalized_filesystem_path(member["old_path"]),
                    _normalized_filesystem_path(member["expected_path"]),
                }
                if (not local or str(local["scene_id"]) != str(member["scene_id"])
                        or _normalized_filesystem_path(local["path"]) not in allowed_paths):
                    state = "uncertain"
                    reason = "Local inventory identity changed during verification"
                    observed = {}
            _set_member_result(connection, member["id"], state, reason, **observed)
            if state == "verified":
                connection.execute(
                    """UPDATE files SET path=?,basename=?,exists_on_disk=1,last_seen_at=?,missing_since=NULL
                       WHERE file_id=? AND scene_id=?""",
                    (str(member["expected_path"]), _basename(member["expected_path"]), _utc_now(),
                     str(member["file_id"]), str(member["scene_id"])),
                )
        counts = _refresh_batch_counts(connection, batch_id)
        connection.commit()
        return counts
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _finalize_verified_batch(database_path: Path | str, batch_id: int, reason: str) -> None:
    """Atomically resolve a fully verified batch and all of its linked events."""
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT state FROM grouped_reconciliation_batches WHERE id=?", (int(batch_id),)
        ).fetchone()
        if not row or row["state"] != "verifying":
            raise RuntimeError("Grouped reconciliation state changed before final resolution")
        now = _utc_now()
        connection.execute(
            """UPDATE grouped_reconciliation_batches
               SET state='resolved',reason=?,claimed_by=NULL,claim_expires_at=NULL,updated_at=?
               WHERE id=?""",
            (reason, now, int(batch_id)),
        )
        connection.execute(
            """UPDATE filesystem_events SET status='resolved'
               WHERE event_key IN (
                   SELECT event_key FROM grouped_reconciliation_event_links WHERE batch_id=?
               )""",
            (int(batch_id),),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def execute_grouped_move_reconciliation(
    database_path: Path | str,
    batch_id: int,
    stash,
    *,
    owner: str,
    path_probe=None,
    scan_timeout: int = 600,
) -> dict:
    """Run one explicit folder scan and verify members without relinking directly."""
    probe = path_probe or _default_path_probe
    if not claim_batch(database_path, batch_id, owner, lease_seconds=max(60, scan_timeout + 60)):
        raise RuntimeError("Another Watchtower operation is already processing this grouped review")
    try:
        batch = batch_snapshot(database_path, batch_id)
        if not batch:
            raise ValueError(f"Reconciliation batch {batch_id} does not exist")
        if batch["operation_type"] not in ("folder_move", "bulk_move"):
            raise ValueError("Only verified move groups can be reconciled through a Stash folder scan")
        if batch["state"] not in ("ready_for_review", "partially_verified", "needs_attention", "scanning", "verifying"):
            raise ValueError("This grouped move is not ready for reconciliation")

        state = batch["state"]
        if state in ("ready_for_review", "partially_verified", "needs_attention"):
            ready_count = _revalidate_grouped_move_members(database_path, batch_id, probe)
            if ready_count == 0:
                transition_batch(database_path, batch_id, "needs_attention", expected_state=state,
                                 reason="No stable verified destinations are currently ready to scan")
                return batch_snapshot(database_path, batch_id)
            if state == "partially_verified":
                transition_batch(database_path, batch_id, "verifying", expected_state=state,
                                 reason="Rechecking remaining identities after user review")
                batch = batch_snapshot(database_path, batch_id)
            else:
                destination = probe(batch["destination_prefix"])
                if destination.get("status") != "ok" or not destination.get("is_dir"):
                    transition_batch(database_path, batch_id, "needs_attention", expected_state=state,
                                     reason="Destination folder is unavailable; no Stash scan was started")
                    return batch_snapshot(database_path, batch_id)
                transition_batch(database_path, batch_id, "scanning", expected_state=state,
                                 reason="User approved one bounded Stash scan of the destination folder",
                                 stash_job_id="starting")
                try:
                    job_id = stash.metadata_scan(paths=[batch["destination_prefix"]])
                except Exception as exc:
                    transition_batch(database_path, batch_id, "needs_attention", expected_state="scanning",
                                     reason=f"Stash scan could not be started: {exc}")
                    return batch_snapshot(database_path, batch_id)
                transition_batch(database_path, batch_id, "scanning", expected_state="scanning",
                                 stash_job_id=str(job_id))
                batch = batch_snapshot(database_path, batch_id)
        elif state == "scanning" and batch.get("stash_job_id") in (None, "", "starting"):
            transition_batch(database_path, batch_id, "needs_attention", expected_state="scanning",
                             reason="The previous scan started but its job ID was not recorded; check Stash jobs before retrying")
            return batch_snapshot(database_path, batch_id)

        batch = batch_snapshot(database_path, batch_id)
        if batch["state"] == "scanning":
            try:
                completed = stash.wait_for_job(batch["stash_job_id"], timeout=int(scan_timeout))
            except Exception as exc:
                transition_batch(database_path, batch_id, "needs_attention", expected_state="scanning",
                                 reason=f"Could not confirm Stash scan completion: {exc}")
                return batch_snapshot(database_path, batch_id)
            if not completed:
                transition_batch(database_path, batch_id, "needs_attention", expected_state="scanning",
                                 reason=f"Stash scan job {batch['stash_job_id']} did not complete successfully")
                return batch_snapshot(database_path, batch_id)
            transition_batch(database_path, batch_id, "verifying", expected_state="scanning",
                             reason=f"Stash scan job {batch['stash_job_id']} completed; verifying original identities")

        counts = _verify_grouped_members(database_path, batch_id, stash, probe)
        current = batch_snapshot(database_path, batch_id)
        if counts["tracked"] > 0 and counts["verified"] == counts["tracked"]:
            _finalize_verified_batch(
                database_path, batch_id,
                "Every tracked scene and file identity was verified at its expected path",
            )
        elif counts["verified"] > 0:
            transition_batch(database_path, batch_id, "partially_verified", expected_state="verifying",
                             reason=f"Verified {counts['verified']} of {counts['tracked']} tracked files; uncertain files remain visible")
        else:
            transition_batch(database_path, batch_id, "needs_attention", expected_state="verifying",
                             reason="Stash scan completed but no original scene/file identity was verified")
        return batch_snapshot(database_path, batch_id)
    finally:
        release_batch_claim(database_path, batch_id, owner)
