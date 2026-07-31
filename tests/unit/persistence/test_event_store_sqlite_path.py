"""``EventStore.sqlite_path()`` — the DB path the dashboard daemon must be scoped to."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.persistence.brownfield import BrownfieldStore
from ouroboros.persistence.event_store import EventStore, sqlite_database_url


class TestSqlitePath:
    def test_custom_path_url_round_trips(self) -> None:
        store = EventStore("sqlite+aiosqlite:////tmp/custom/ouroboros.db")
        assert store.sqlite_path() == "/tmp/custom/ouroboros.db"

    def test_default_store_points_at_configured_db(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("persistence:\n  database_path: data/events.db\n")

        with patch("ouroboros.config.models.get_config_dir", return_value=tmp_path):
            store = EventStore()

        assert store.sqlite_path() == str(tmp_path / "data" / "events.db")

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

    @pytest.mark.asyncio
    async def test_reserved_path_characters_share_one_database(self, tmp_path: Path) -> None:
        db_path = tmp_path / "events?blue#100%.db"
        database_url = sqlite_database_url(db_path)
        event_store = EventStore(database_url)
        brownfield_store = BrownfieldStore(database_url)

        await event_store.initialize()
        await brownfield_store.initialize()
        await brownfield_store.close()
        await event_store.close()

        assert event_store.sqlite_path() == str(db_path)
        assert db_path.is_file()
        assert not (tmp_path / "events").exists()
