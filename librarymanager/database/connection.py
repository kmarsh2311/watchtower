"""Database connection creation and connection-level configuration for Watchtower.

This module is responsible ONLY for creating and configuring SQLite connections:
- Setting timeout and busy_timeout pragmas
- Enabling Write-Ahead Logging (WAL) journal mode
- Enabling foreign key constraints
- Configuring sqlite3.Row row_factory
- Providing clean connection instances without executing schema DDL

CONCURRENCY AND LOCKING ARCHITECTURE:
-------------------------------------
1. WAL Mode (Write-Ahead Logging):
   Enabling WAL allows multiple concurrent readers to proceed alongside exactly
   one active writer without blocking one another. Readers do not block writers,
   and writers do not block readers.

2. Write Serialization:
   SQLite only allows ONE process/thread to hold the write lock (or reserved/pending lock)
   at any given moment. If a writer begins a transaction while another process is writing,
   the second writer must wait.

3. Busy Timeout (PRAGMA busy_timeout = 30000):
   Instead of immediately failing with `sqlite3.OperationalError: database is locked`,
   SQLite's internal busy handler will sleep and retry with exponential backoff up to
   the specified timeout (30,000ms = 30 seconds) waiting for the active write lock
   to be released.

4. Limitation (Why timeout alone is not enough):
   If transactions are kept open during long-running disk I/O, network requests,
   or during repeated schema DDL execution (such as multiple ALTER TABLE calls),
   the busy timeout can still expire under load, leading to lock errors. Therefore,
   ordinary connection creation MUST NOT execute DDL, and transactions must be kept
   short and scoped strictly to database operations.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional, Union

DEFAULT_BUSY_TIMEOUT_MS = 30000
DEFAULT_CONNECT_TIMEOUT = 60.0


def apply_connection_pragmas(
    connection: sqlite3.Connection,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    wal_mode: bool = True,
    foreign_keys: bool = True,
) -> None:
    """Applies standard connection-level pragmas to an open SQLite connection.

    Args:
        connection: An active sqlite3.Connection.
        busy_timeout_ms: Timeout in milliseconds for SQLite's built-in busy handler.
        wal_mode: If True, sets PRAGMA journal_mode=WAL.
        foreign_keys: If True, sets PRAGMA foreign_keys=ON.
    """
    if wal_mode:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # Silently ignored if opened read-only or on filesystems that don't support WAL
            pass

    if busy_timeout_ms > 0:
        try:
            connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        except sqlite3.OperationalError:
            pass

    if foreign_keys:
        try:
            connection.execute("PRAGMA foreign_keys=ON")
        except sqlite3.OperationalError:
            pass


def create_connection(
    database_path: Union[Path, str],
    *,
    timeout: float = DEFAULT_CONNECT_TIMEOUT,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    wal_mode: bool = True,
    foreign_keys: bool = True,
    row_factory: Any = sqlite3.Row,
) -> sqlite3.Connection:
    """Creates and configures an ordinary SQLite connection without executing DDL or migrations.

    This function is strictly for connection establishment. It sets the connection pragmas
    and returns an open, configured connection. It performs NO table checks, NO DDL,
    and NO migrations.

    Args:
        database_path: Path to the SQLite database file.
        timeout: SQLite connection timeout in seconds (passed to sqlite3.connect).
        busy_timeout_ms: PRAGMA busy_timeout in milliseconds (default: 30,000ms / 30s).
        wal_mode: Whether to enable Write-Ahead Logging (PRAGMA journal_mode=WAL).
        foreign_keys: Whether to enforce foreign keys (PRAGMA foreign_keys=ON).
        row_factory: Connection row factory (default: sqlite3.Row).

    Returns:
        A configured sqlite3.Connection ready for querying.
    """
    db_p = Path(database_path)
    connection = sqlite3.connect(db_p, timeout=timeout)
    if row_factory is not None:
        connection.row_factory = row_factory

    apply_connection_pragmas(
        connection,
        busy_timeout_ms=busy_timeout_ms,
        wal_mode=wal_mode,
        foreign_keys=foreign_keys,
    )
    return connection


def get_connection_info(connection: sqlite3.Connection) -> Dict[str, Any]:
    """Inspects and returns active pragma and configuration state of a connection."""
    info: Dict[str, Any] = {}
    try:
        row = connection.execute("PRAGMA journal_mode").fetchone()
        info["journal_mode"] = row[0] if row else "unknown"
    except Exception as e:
        info["journal_mode"] = str(e)

    try:
        row = connection.execute("PRAGMA busy_timeout").fetchone()
        info["busy_timeout"] = row[0] if row else 0
    except Exception as e:
        info["busy_timeout"] = str(e)

    try:
        row = connection.execute("PRAGMA foreign_keys").fetchone()
        info["foreign_keys"] = bool(row[0]) if row else False
    except Exception as e:
        info["foreign_keys"] = str(e)

    try:
        row = connection.execute("PRAGMA user_version").fetchone()
        info["user_version"] = row[0] if row else 0
    except Exception as e:
        info["user_version"] = str(e)

    return info
