"""Database subpackage for Watchtower."""
from __future__ import annotations

from librarymanager.database.connection import (
    DEFAULT_BUSY_TIMEOUT_MS,
    DEFAULT_CONNECT_TIMEOUT,
    apply_connection_pragmas,
    create_connection,
    get_connection_info,
)
from librarymanager.database.schema import (
    BASE_SCHEMA,
    RECONCILIATION_SCHEMA,
    FULL_SCHEMA,
    SCHEMA,
    TABLE_NAMES,
)
from librarymanager.database.migrations import (
    CURRENT_SCHEMA_VERSION,
    ensure_schema,
    get_schema_version,
    set_schema_version,
    is_database_initialized,
    _safe_alter,
    _schema_applied,
    _schema_lock,
)

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_CONNECT_TIMEOUT",
    "apply_connection_pragmas",
    "create_connection",
    "get_connection_info",
    "BASE_SCHEMA",
    "RECONCILIATION_SCHEMA",
    "FULL_SCHEMA",
    "SCHEMA",
    "TABLE_NAMES",
    "CURRENT_SCHEMA_VERSION",
    "ensure_schema",
    "get_schema_version",
    "set_schema_version",
    "is_database_initialized",
    "_safe_alter",
    "_schema_applied",
    "_schema_lock",
]
