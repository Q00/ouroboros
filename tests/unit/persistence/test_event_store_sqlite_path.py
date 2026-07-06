"""``EventStore.sqlite_path()`` — the DB path the dashboard daemon must be scoped to."""

from __future__ import annotations

from pathlib import Path

from ouroboros.persistence.event_store import EventStore


class TestSqlitePath:
    def test_custom_path_url_round_trips(self) -> None:
        store = EventStore("sqlite+aiosqlite:////tmp/custom/ouroboros.db")
        assert store.sqlite_path() == "/tmp/custom/ouroboros.db"

    def test_default_store_points_at_home_db(self) -> None:
        # No URL → the home-directory default; the path must be recoverable so the
        # daemon resolves to the same file the writer uses.
        store = EventStore()
        expected = str(Path.home() / ".ouroboros" / "ouroboros.db")
        assert store.sqlite_path() == expected

    def test_memory_backend_has_no_file(self) -> None:
        assert EventStore("sqlite+aiosqlite:///:memory:").sqlite_path() is None

    def test_non_sqlite_backend_has_no_local_file(self) -> None:
        assert EventStore("postgresql+asyncpg://host/db").sqlite_path() is None

    def test_read_only_uri_form_is_decoded_back_to_a_path(self) -> None:
        # read_only=True rewrites the URL into the ``file:...?mode=ro&uri=true``
        # form; sqlite_path must peel that back to the plain filesystem path so a
        # read-only store still scopes the daemon to the right DB.
        store = EventStore("sqlite+aiosqlite:////tmp/custom/ouroboros.db", read_only=True)
        assert store.sqlite_path() == "/tmp/custom/ouroboros.db"
