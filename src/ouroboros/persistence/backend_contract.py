"""The EventStore's storage-backend contract (#1832).

Every cursor API is built on SQLite's implicit ``rowid`` ordering — the
semantics documented in #1274: ``append_with_rowid``, ``get_current_rowid``,
``get_events_after`` and the replay paths all query ``rowid`` directly, and
the declared ``events`` schema carries no portable substitute. A non-SQLite
backend cannot run those statements (PostgreSQL has no ``rowid`` system
column), so accepting such a URL only defers the failure to first cursor
use while misreporting capabilities in the meantime. The boundary is
therefore explicit: the EventStore is SQLite-only, checked at construction.
Introducing a declared monotonic cursor column later can relax this in one
place, as a deliberate change rather than a silent one.
"""

from __future__ import annotations

from sqlalchemy.engine import make_url


def require_sqlite_event_store_url(database_url: str) -> None:
    """Reject database URLs the EventStore's cursor contract cannot serve.

    A malformed URL is left for engine creation to report with its own,
    more specific error; only a well-formed URL naming a non-SQLite
    backend is refused here.
    """
    try:
        backend = make_url(database_url).get_backend_name()
    except Exception:
        return
    if backend != "sqlite":
        raise ValueError(
            "EventStore is SQLite-only: its cursor APIs are built on SQLite "
            f"rowid semantics, which a {backend!r} backend cannot serve. "
            "Use a sqlite+aiosqlite:// URL."
        )
