"""SQLite connection setup shared by persistence writers."""

from __future__ import annotations

import sqlite3
from typing import Any


def configure_writable_sqlite_connection(dbapi_connection: Any) -> None:
    """Configure one writer connection without racing concurrent WAL setup."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=30000")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if not any(marker in str(exc).lower() for marker in ("busy", "locked")):
                raise
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


__all__ = ["configure_writable_sqlite_connection"]
