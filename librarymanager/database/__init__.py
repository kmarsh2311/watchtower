"""Database subpackage for Watchtower."""
from __future__ import annotations

from librarymanager.database.connection import (
    DEFAULT_BUSY_TIMEOUT_MS,
    DEFAULT_CONNECT_TIMEOUT,
    apply_connection_pragmas,
    create_connection,
    get_connection_info,
)

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DEFAULT_CONNECT_TIMEOUT",
    "apply_connection_pragmas",
    "create_connection",
    "get_connection_info",
]
