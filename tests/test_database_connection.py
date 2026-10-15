"""Focused tests for Batch 1: Database Connection Separation.

Verifies:
1. Connection pragmas (WAL, busy_timeout, foreign_keys, row_factory).
2. Proof that ordinary connection creation (create_connection) executes ZERO DDL and creates NO tables.
3. Compatibility with existing librarymanager_core.connect caller.
4. Concurrency and busy timeout handling between multiple connections.
"""
from __future__ import annotations

import sqlite3
import time
import threading
from pathlib import Path

import pytest

from librarymanager.database.connection import (
    DEFAULT_BUSY_TIMEOUT_MS,
    apply_connection_pragmas,
    create_connection,
    get_connection_info,
)
import librarymanager_core


def test_create_connection_applies_pragmas(tmp_path: Path):
    """Verifies that create_connection correctly sets WAL, busy_timeout, and foreign keys."""
    db_path = tmp_path / "test_pragmas.sqlite3"
    con = create_connection(db_path)
    try:
        info = get_connection_info(con)
        assert info["journal_mode"].lower() == "wal"
        assert int(info["busy_timeout"]) == DEFAULT_BUSY_TIMEOUT_MS
        assert info["foreign_keys"] is True
        assert con.row_factory is sqlite3.Row
    finally:
        con.close()


def test_create_connection_performs_no_ddl_on_empty_db(tmp_path: Path):
    """PROOFS that ordinary connection creation executes zero schema DDL and creates no tables."""
    db_path = tmp_path / "empty_test.sqlite3"
    assert not db_path.exists()

    con = create_connection(db_path)
    try:
        # Check sqlite_master table count
        tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert len(tables) == 0, f"Expected 0 tables from pure connection creation, found: {[t[0] for t in tables]}"
    finally:
        con.close()


def test_create_connection_performs_no_ddl_on_existing_db(tmp_path: Path):
    """PROOFS that opening an existing populated database executes no DDL."""
    db_path = tmp_path / "existing_test.sqlite3"
    setup_con = sqlite3.connect(db_path)
    setup_con.execute("CREATE TABLE sample_data (id INTEGER PRIMARY KEY, value TEXT)")
    setup_con.execute("INSERT INTO sample_data (value) VALUES ('hello')")
    setup_con.commit()
    setup_con.close()

    con = create_connection(db_path)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert tables == ["sample_data"], "create_connection must not alter existing schema"
        row = con.execute("SELECT value FROM sample_data WHERE id=1").fetchone()
        assert row["value"] == "hello"
    finally:
        con.close()


def test_get_connection_info_reporting(tmp_path: Path):
    """Verifies that get_connection_info accurately inspects connection state."""
    db_path = tmp_path / "info_test.sqlite3"
    con = create_connection(db_path, busy_timeout_ms=12345)
    try:
        info = get_connection_info(con)
        assert info["journal_mode"].lower() == "wal"
        assert int(info["busy_timeout"]) == 12345
        assert info["foreign_keys"] is True
        assert "user_version" in info
    finally:
        con.close()


def test_custom_pragma_options(tmp_path: Path):
    """Verifies that caller can override pragmas when necessary."""
    db_path = tmp_path / "custom_test.sqlite3"
    con = create_connection(
        db_path,
        busy_timeout_ms=5000,
        wal_mode=False,
        foreign_keys=False,
        row_factory=None,
    )
    try:
        info = get_connection_info(con)
        assert int(info["busy_timeout"]) == 5000
        assert info["foreign_keys"] is False
        assert con.row_factory is None
    finally:
        con.close()


def test_librarymanager_core_connect_compatibility(tmp_path: Path):
    """Verifies that librarymanager_core.connect continues to function seamlessly with create_connection."""
    db_path = tmp_path / "compat_test.sqlite3"
    con = librarymanager_core.connect(db_path)
    try:
        info = get_connection_info(con)
        assert info["journal_mode"].lower() == "wal"
        assert int(info["busy_timeout"]) == DEFAULT_BUSY_TIMEOUT_MS
        assert info["foreign_keys"] is True
        assert con.row_factory is sqlite3.Row

        # Compatibility wrapper ensures schema is initialized for existing callers
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "files" in tables
        assert "inventory_runs" in tables
    finally:
        con.close()


def test_concurrent_connections_busy_timeout(tmp_path: Path):
    """Verifies that busy_timeout prevents immediate database locked failures between concurrent connections."""
    db_path = tmp_path / "concurrency_test.sqlite3"
    # Create table first
    init_con = create_connection(db_path)
    init_con.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, count INTEGER)")
    init_con.execute("INSERT INTO counter (id, count) VALUES (1, 0)")
    init_con.commit()
    init_con.close()

    writer1 = create_connection(db_path, busy_timeout_ms=5000)

    try:
        # writer1 acquires an EXCLUSIVE lock
        writer1.execute("BEGIN EXCLUSIVE")
        writer1.execute("UPDATE counter SET count = count + 1 WHERE id=1")

        lock_released = threading.Event()
        writer2_success = []
        writer2_error = []

        def _writer2_task():
            writer2 = create_connection(db_path, busy_timeout_ms=5000)
            try:
                # writer2 attempts to write; SQLite busy_timeout will wait for writer1 to commit
                writer2.execute("BEGIN IMMEDIATE")
                writer2.execute("UPDATE counter SET count = count + 10 WHERE id=1")
                writer2.commit()
                writer2_success.append(True)
            except Exception as e:
                writer2_error.append(str(e))
            finally:
                writer2.close()

        t = threading.Thread(target=_writer2_task)
        t.start()

        # writer1 holds lock for 200ms then commits and releases
        time.sleep(0.2)
        writer1.commit()
        lock_released.set()

        t.join(timeout=3.0)
        assert not t.is_alive(), "writer2 thread timed out"
        assert len(writer2_error) == 0, f"writer2 encountered lock error: {writer2_error}"
        assert writer2_success == [True], "writer2 should have successfully acquired lock after writer1 committed"

        # Check final count: 0 + 1 + 10 = 11
        reader = create_connection(db_path)
        final_count = reader.execute("SELECT count FROM counter WHERE id=1").fetchone()[0]
        reader.close()
        assert final_count == 11
    finally:
        writer1.close()


def test_reconciliation_connect_uses_central_factory_and_executes_no_ddl(tmp_path: Path):
    """Verifies that librarymanager_reconciliation._connect uses create_connection and executes no DDL."""
    import librarymanager_reconciliation

    db_path = tmp_path / "recon_connect_test.sqlite3"
    assert not db_path.exists()

    con = librarymanager_reconciliation._connect(db_path)
    try:
        # 1. Connection pragmas must match central factory
        info = get_connection_info(con)
        assert info["journal_mode"].lower() == "wal"
        assert int(info["busy_timeout"]) == DEFAULT_BUSY_TIMEOUT_MS
        assert info["foreign_keys"] is True
        assert con.row_factory is sqlite3.Row

        # 2. PROOF: _connect must execute zero DDL and create zero tables on empty DB
        tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert len(tables) == 0, f"Expected 0 tables from reconciliation _connect, found: {[t[0] for t in tables]}"

        # 3. ensure_reconciliation_schema executes transactionally
        librarymanager_reconciliation.ensure_reconciliation_schema(con)
        tables_after = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "grouped_reconciliation_batches" in tables_after
        assert "grouped_reconciliation_members" in tables_after
        assert "grouped_reconciliation_event_links" in tables_after
    finally:
        con.close()
