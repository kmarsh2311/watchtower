"""Focused tests for Batch 2: Schema and Migration Separation.

Verifies:
1. Fresh database initialisation (all tables created, user_version set to CURRENT_SCHEMA_VERSION).
2. Upgrade from legacy un-versioned schema (adds columns, indexes, data backfills).
3. Preservation of existing data across upgrades.
4. Repeated initialisation without redundant DDL (both in-process and cross-process).
5. Concurrent migration attempts from separate processes.
6. Transactional integrity and recovery after a migration failure.
7. Compatibility with existing callers and librarymanager_core interface.
"""
from __future__ import annotations

import multiprocessing
import sqlite3
from pathlib import Path
from typing import List

import pytest

import librarymanager_core
from librarymanager.database.connection import create_connection
from librarymanager.database.migrations import (
    CURRENT_SCHEMA_VERSION,
    _apply_legacy_migrations,
    _safe_alter,
    _schema_applied,
    ensure_schema,
    get_schema_version,
    is_database_initialized,
    set_schema_version,
)
from librarymanager.database.schema import BASE_SCHEMA, FULL_SCHEMA, SCHEMA, TABLE_NAMES


def test_fresh_database_initialization(tmp_path: Path):
    """Verifies that fresh DB initialization creates all 37 tables and sets user_version."""
    db_path = tmp_path / "fresh.sqlite3"
    con = create_connection(db_path)
    try:
        assert get_schema_version(con) == 0
        assert not is_database_initialized(con)

        version = ensure_schema(con, db_path)
        assert version == CURRENT_SCHEMA_VERSION
        assert get_schema_version(con) == CURRENT_SCHEMA_VERSION
        assert is_database_initialized(con)

        tables = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        # Verify all known table names are present
        for expected_table in TABLE_NAMES:
            assert expected_table in tables, f"Missing expected table: {expected_table}"
        assert len(tables) >= len(TABLE_NAMES)
    finally:
        con.close()


def test_upgrade_from_legacy_schema(tmp_path: Path):
    """Verifies upgrade of a pre-v1.0.21 database that has user_version=0 but tables already exist."""
    db_path = tmp_path / "legacy.sqlite3"
    raw_con = sqlite3.connect(db_path)
    # Simulate a legacy schema: core files and filename_state tables without newer columns
    raw_con.execute(
        """CREATE TABLE files (
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
            exists_on_disk INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            missing_since TEXT
        )"""
    )
    raw_con.execute(
        """CREATE TABLE filename_state (
            file_path TEXT PRIMARY KEY,
            canonical_name TEXT,
            clean_title TEXT,
            original_title TEXT,
            studio_code TEXT,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    raw_con.execute(
        """CREATE TABLE inventory_runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            files_scanned INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL
        )"""
    )
    # Insert legacy data
    raw_con.execute(
        """INSERT INTO files (file_id, scene_id, path, basename, size, exists_on_disk, first_seen_at, last_seen_at)
           VALUES ('f1', '123', '/media/scene1.mp4', 'scene1.mp4', 500000, 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
    )
    raw_con.execute(
        "INSERT INTO filename_state (file_path, status, updated_at) VALUES ('/media/scene1.mp4', 'matched', '2026-01-01T00:00:00Z')"
    )
    raw_con.execute(
        "INSERT INTO inventory_runs (id, started_at, status) VALUES (1, '2026-01-01T00:00:00Z', 'complete')"
    )
    raw_con.commit()
    raw_con.close()

    # Now open with Watchtower connection and migrate
    con = create_connection(db_path)
    try:
        assert get_schema_version(con) == 0
        assert is_database_initialized(con)

        # Clear in-process cache to force check
        _schema_applied.discard(str(db_path.resolve()))

        version = ensure_schema(con, db_path)
        assert version == CURRENT_SCHEMA_VERSION
        assert get_schema_version(con) == CURRENT_SCHEMA_VERSION

        # Verify added columns exist
        file_cols = {row[1] for row in con.execute("PRAGMA table_info(files)")}
        assert "scene_metadata_json" in file_cols
        assert "height" in file_cols

        fn_cols = {row[1] for row in con.execute("PRAGMA table_info(filename_state)")}
        assert "manual_studio" in fn_cols
        assert "manual_performers_json" in fn_cols
        assert "managed_date" in fn_cols
        assert "managed_quality" in fn_cols
        assert "rename_protected" in fn_cols

        inv_cols = {row[1] for row in con.execute("PRAGMA table_info(inventory_runs)")}
        assert "stash_scene_count" in inv_cols

        # Verify data preservation
        f_row = con.execute("SELECT * FROM files WHERE path='/media/scene1.mp4'").fetchone()
        assert f_row["scene_id"] == "123"
        assert f_row["scene_metadata_json"] == "{}"  # Default value applied

        # Verify backfill for inventory_runs.stash_scene_count
        inv_row = con.execute("SELECT stash_scene_count FROM inventory_runs WHERE id=1").fetchone()
        assert inv_row["stash_scene_count"] == 1  # 1 distinct scene_id in files
    finally:
        con.close()


def test_repeated_initialization_executes_zero_ddl(tmp_path: Path):
    """Verifies that calling ensure_schema on an already-current DB executes zero DDL."""
    db_path = tmp_path / "no_ddl.sqlite3"
    con = create_connection(db_path)
    try:
        ensure_schema(con, db_path)
        assert get_schema_version(con) == CURRENT_SCHEMA_VERSION

        # 1. In-process cache check: ensure_schema should return immediately
        recorded_queries: List[str] = []

        def trace_callback(stmt: str):
            recorded_queries.append(stmt.strip())

        con.set_trace_callback(trace_callback)
        ensure_schema(con, db_path)
        assert len(recorded_queries) == 0, f"Expected 0 queries via in-process cache, got: {recorded_queries}"

        # 2. Simulated cross-process check (in-process cache cleared):
        # Should only query PRAGMA user_version and NOT execute any CREATE or ALTER TABLE statements.
        _schema_applied.discard(str(db_path.resolve()))
        recorded_queries.clear()

        ensure_schema(con, db_path)
        con.set_trace_callback(None)

        # Trace should only contain user_version query
        ddl_queries = [
            q for q in recorded_queries
            if any(k in q.upper() for k in ("CREATE TABLE", "ALTER TABLE", "CREATE INDEX"))
        ]
        assert len(ddl_queries) == 0, f"Expected 0 DDL queries, but executed: {ddl_queries}"
    finally:
        con.close()


def _concurrency_worker(db_path_str: str, worker_id: int, result_queue: multiprocessing.Queue):
    """Worker process that attempts to connect and ensure_schema simultaneously."""
    try:
        db_p = Path(db_path_str)
        con = create_connection(db_p)
        v = ensure_schema(con, db_p)
        # Perform a test query
        con.execute("SELECT COUNT(*) FROM files").fetchone()
        con.close()
        result_queue.put((worker_id, True, v, None))
    except Exception as exc:
        result_queue.put((worker_id, False, 0, str(exc)))


def test_concurrent_migrations_from_separate_processes(tmp_path: Path):
    """Verifies that concurrent processes starting up simultaneously handle migrations safely without locking crashes."""
    db_path = tmp_path / "concurrent_mig.sqlite3"
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()

    process_count = 5
    processes = []
    for i in range(process_count):
        p = ctx.Process(
            target=_concurrency_worker,
            args=(str(db_path), i, queue),
        )
        processes.append(p)

    for p in processes:
        p.start()

    for p in processes:
        p.join(timeout=15.0)

    results = []
    while not queue.empty():
        results.append(queue.get())

    assert len(results) == process_count, f"Expected {process_count} results, got {len(results)}"
    for worker_id, success, version, err in results:
        assert success, f"Worker {worker_id} failed with error: {err}"
        assert version == CURRENT_SCHEMA_VERSION

    # Verify final database integrity
    verify_con = create_connection(db_path)
    try:
        assert get_schema_version(verify_con) == CURRENT_SCHEMA_VERSION
        tables = {r[0] for r in verify_con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "files" in tables
        assert "grouped_reconciliation_batches" in tables
    finally:
        verify_con.close()


def test_migration_failure_rollback_and_recovery(tmp_path: Path, monkeypatch):
    """Verifies transactional rollback if an unhandled failure occurs mid-migration after some DDL succeeds."""
    db_path = tmp_path / "failure_recovery.sqlite3"
    con = create_connection(db_path)
    try:
        assert get_schema_version(con) == 0

        # Simulate a migration error by injecting invalid SQL mid-migration after DDL changes succeed
        import librarymanager.database.migrations as mig_mod

        partial_schema = (
            "CREATE TABLE test_rollback_table_1 (id INT);\n"
            "CREATE TABLE test_rollback_table_2 (id INT);\n"
            "THIS IS AN INVALID SQL SYNTAX ERROR;"
        )
        monkeypatch.setattr(mig_mod, "SCHEMA", partial_schema)

        _schema_applied.discard(str(db_path.resolve()))
        with pytest.raises(sqlite3.OperationalError, match="syntax error"):
            ensure_schema(con, db_path)

        # Crucial assertion: the tables created before the error MUST BE ROLLED BACK
        # Neither test_rollback_table_1 nor test_rollback_table_2 must exist in the database!
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "test_rollback_table_1" not in tables, "Table 1 must be rolled back on mid-migration error"
        assert "test_rollback_table_2" not in tables, "Table 2 must be rolled back on mid-migration error"
        assert len(tables) == 0, f"Expected 0 tables after rollback, found {tables}"

        # user_version must remain 0 after rollback, not corrupted to 3
        assert get_schema_version(con) == 0

        # Now remove failure and ensure subsequent migration completes cleanly
        monkeypatch.undo()
        _schema_applied.discard(str(db_path.resolve()))

        version = ensure_schema(con, db_path)
        assert version == CURRENT_SCHEMA_VERSION
        assert get_schema_version(con) == CURRENT_SCHEMA_VERSION

        # Verify all actual schema tables now exist
        final_tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert "files" in final_tables
        assert "grouped_reconciliation_batches" in final_tables
    finally:
        con.close()


def test_lock_timeout_does_not_falsely_cache_legacy_database(tmp_path: Path):
    """Verifies that a lock timeout on a legacy database with tables does NOT falsely cache the schema as applied."""
    db_path = tmp_path / "legacy_locked.sqlite3"
    setup_con = sqlite3.connect(db_path)
    # Create a table so is_database_initialized() is True, but user_version is 1 (below CURRENT_SCHEMA_VERSION 3)
    setup_con.execute("CREATE TABLE files (file_id TEXT PRIMARY KEY)")
    setup_con.execute("PRAGMA user_version = 1")
    setup_con.commit()
    setup_con.close()

    # Open con first with a short timeout so pragmas succeed before locking
    con = create_connection(db_path, timeout=0.1, busy_timeout_ms=100)
    assert get_schema_version(con) == 1
    _schema_applied.discard(str(db_path.resolve()))

    # Lock the database exclusively with blocker_con
    blocker_con = sqlite3.connect(db_path, timeout=0.1)
    blocker_con.execute("BEGIN EXCLUSIVE")

    try:
        # ensure_schema tries BEGIN IMMEDIATE which will time out in 100ms
        with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
            ensure_schema(con, db_path)

        assert str(db_path.resolve()) not in _schema_applied
    finally:
        blocker_con.rollback()
        blocker_con.close()
        con.close()


def test_librarymanager_core_compatibility_exports(tmp_path: Path):
    """Verifies that librarymanager_core exports and functions continue to be backward compatible."""
    db_path = tmp_path / "compat_export.sqlite3"
    assert hasattr(librarymanager_core, "CURRENT_SCHEMA_VERSION")
    assert hasattr(librarymanager_core, "_schema_applied")
    assert hasattr(librarymanager_core, "_schema_lock")
    assert hasattr(librarymanager_core, "_safe_alter")
    assert hasattr(librarymanager_core, "_ensure_schema")
    assert hasattr(librarymanager_core, "SCHEMA")
    assert hasattr(librarymanager_core, "RECONCILIATION_SCHEMA")

    # Connect via librarymanager_core
    con = librarymanager_core.connect(db_path)
    try:
        assert get_schema_version(con) == CURRENT_SCHEMA_VERSION
        row = con.execute("SELECT 1 FROM files LIMIT 1").fetchall()
        assert row == []
    finally:
        con.close()
