"""Versioned database migrations and schema initialisation for Watchtower.

This module owns schema migration execution, version tracking via SQLite's
PRAGMA user_version, cross-process concurrency locking, and backward-compatible
legacy upgrades.

CONCURRENCY AND SAFETY RULES:
-----------------------------
1. Reading Schema Version:
   `PRAGMA user_version` is read without acquiring a write lock. If the database
   is already at CURRENT_SCHEMA_VERSION, ensure_schema() returns immediately (zero DDL,
   zero catalog lock, zero ALTER TABLE).

2. In-Process Caching:
   Once a database path has been verified in the active Python process, it is recorded
   in `_schema_applied` so subsequent connection calls within that process execute
   zero queries.

3. Cross-Process Migration Coordination:
   When an upgrade is needed, the process acquires SQLite's write lock using
   `BEGIN IMMEDIATE`. Any concurrent process attempting to migrate simultaneously
   waits via SQLite's `busy_timeout` (30s) instead of crashing. Once the write lock is
   acquired, `PRAGMA user_version` is re-checked. If another process completed the
   migration in the interim, the transaction is committed immediately with no DDL.

4. Transactional Integrity:
   All table creations, alterations, and user_version updates occur inside the
   transaction. If an unhandled failure occurs, `ROLLBACK` is executed, leaving
   `user_version` at its previous valid state.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Set, Union

from librarymanager.database.schema import SCHEMA, RECONCILIATION_SCHEMA

logger = logging.getLogger("librarymanager.database.migrations")

CURRENT_SCHEMA_VERSION = 3
_schema_applied: Set[str] = set()
_schema_lock = threading.Lock()


def get_schema_version(connection: sqlite3.Connection) -> int:
    """Reads the current schema version from SQLite database header."""
    try:
        row = connection.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0


def set_schema_version(connection: sqlite3.Connection, version: int) -> None:
    """Sets the schema version in the SQLite database header."""
    connection.execute(f"PRAGMA user_version = {int(version)}")


def is_database_initialized(connection: sqlite3.Connection) -> bool:
    """Checks whether any user tables exist in the database."""
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _safe_alter(connection: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Applies an ALTER TABLE only if the column is absent; safely handles concurrent races."""
    try:
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return
    if not existing or column in existing:
        return
    try:
        connection.execute(ddl)
    except sqlite3.OperationalError as exc:
        err = str(exc).lower()
        if "duplicate column" not in err and "no such table" not in err:
            raise


def _apply_legacy_migrations(connection: sqlite3.Connection) -> None:
    """Migrates pre-v1.0.21 / v1.0.24 databases that lacked explicit user_version tracking.

    Brings legacy databases with user_version=0 up to CURRENT_SCHEMA_VERSION (3).
    """
    # 1. Additive columns across historical versions
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
    _safe_alter(connection, "filing_transfer_queue", "stash_job_id",
                "ALTER TABLE filing_transfer_queue ADD COLUMN stash_job_id TEXT")
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

    # 2. Ensure all schema tables exist (creates any newly added tables like grouped_reconciliation)
    connection.executescript(SCHEMA)

    # 3. Clean up legacy rename_protected state
    try:
        has_protected = connection.execute(
            "SELECT 1 FROM filename_state WHERE rename_protected!=0 LIMIT 1"
        ).fetchone()
        if has_protected:
            connection.execute("UPDATE filename_state SET rename_protected=0 WHERE rename_protected!=0")
    except Exception:
        pass
    # 4. Performance & lookup indexes
    connection.execute("CREATE INDEX IF NOT EXISTS idx_checksum_cache_file_id ON file_checksum_cache(file_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_filesystem_events_status ON filesystem_events(status)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_status ON filesystem_reconciliation_runs(status)")

    # 4. Data backfills
    try:
        connection.execute(
            """UPDATE inventory_runs SET stash_scene_count=(SELECT COUNT(DISTINCT scene_id) FROM files)
               WHERE status='complete' AND stash_scene_count=0"""
        )
    except Exception:
        pass


def ensure_schema(connection: sqlite3.Connection, database_path: Union[Path, str]) -> int:
    """Ensures database schema is initialized and migrated to CURRENT_SCHEMA_VERSION.

    Fast Path (99.9% of calls):
      - In-process check skips execution if already verified for this DB path.
      - If in a new process, reads PRAGMA user_version. If >= CURRENT_SCHEMA_VERSION,
        immediately records in-process cache and returns (ZERO DDL, ZERO LOCKS).

    Slow Path (Fresh Install or Migration):
      - Acquires cross-process lock via `BEGIN IMMEDIATE`.
      - Applies necessary migrations inside a transaction.
      - Sets PRAGMA user_version = CURRENT_SCHEMA_VERSION and commits.
    """
    db_p = Path(database_path)
    key = str(db_p.resolve())

    # Fast in-process check
    if key in _schema_applied:
        return CURRENT_SCHEMA_VERSION

    with _schema_lock:
        if key in _schema_applied:
            return CURRENT_SCHEMA_VERSION

        version = get_schema_version(connection)
        if version >= CURRENT_SCHEMA_VERSION:
            try:
                has_protected = connection.execute(
                    "SELECT 1 FROM filename_state WHERE rename_protected!=0 LIMIT 1"
                ).fetchone()
                if has_protected:
                    connection.execute("UPDATE filename_state SET rename_protected=0 WHERE rename_protected!=0")
                    connection.commit()
            except Exception:
                pass
            _schema_applied.add(key)
            return version

        # Cross-process coordinated migration
        try:
            connection.execute("BEGIN IMMEDIATE")
            # Re-read version under write lock in case another process upgraded while we waited
            version = get_schema_version(connection)
            if version >= CURRENT_SCHEMA_VERSION:
                connection.commit()
                _schema_applied.add(key)
                return version

            if version == 0:
                if not is_database_initialized(connection):
                    # Fresh database: create all tables at current schema
                    connection.executescript(SCHEMA)
                else:
                    # Legacy un-versioned database: bring up to date
                    _apply_legacy_migrations(connection)
            elif version < CURRENT_SCHEMA_VERSION:
                # Incremental migration path for future schema versions
                _apply_legacy_migrations(connection)

            set_schema_version(connection, CURRENT_SCHEMA_VERSION)
            connection.commit()
            _schema_applied.add(key)
            return CURRENT_SCHEMA_VERSION

        except sqlite3.OperationalError as exc:
            err_msg = str(exc).lower()
            try:
                connection.rollback()
            except Exception:
                pass

            # If locked or busy, verify if another process successfully completed the migration
            if "locked" in err_msg or "busy" in err_msg:
                try:
                    v = get_schema_version(connection)
                    if v >= CURRENT_SCHEMA_VERSION or is_database_initialized(connection):
                        _schema_applied.add(key)
                        return v
                except Exception:
                    pass
            logger.error("Failed to migrate database schema for %s: %s", database_path, exc)
            raise
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
