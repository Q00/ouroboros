"""BrownfieldStore search-index table: storage, cleanup, and existing-DB upgrade."""

from __future__ import annotations

import sqlite3

from ouroboros.persistence.brownfield import BrownfieldStore

_PRE_INDEX_SCHEMA = """
CREATE TABLE brownfield_repos (
    path TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    desc TEXT,
    is_default BOOLEAN NOT NULL DEFAULT 0,
    registered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE _migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
INSERT INTO _migrations (name) VALUES ('001_initial.sql'), ('002_brownfield.sql');
INSERT INTO brownfield_repos (path, name, desc, is_default)
    VALUES ('/home/u/legacy', 'legacy', 'Old repo', 1);
"""


async def test_existing_db_gains_index_table_and_keeps_rows(tmp_path) -> None:
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_PRE_INDEX_SCHEMA)

    store = BrownfieldStore(f"sqlite+aiosqlite:///{db_path}")
    await store.initialize()
    try:
        repos = await store.list()
        assert [(r.path, r.name, r.desc, r.is_default) for r in repos] == [
            ("/home/u/legacy", "legacy", "Old repo", True)
        ]
        assert await store.get_index_entries() == {}

        await store.upsert_index_entries({"/home/u/legacy": ("doc text", "fp1")})
        assert await store.get_index_entries() == {"/home/u/legacy": ("doc text", "fp1")}
    finally:
        await store.close()

    with sqlite3.connect(db_path) as conn:
        applied = {row[0] for row in conn.execute("SELECT name FROM _migrations")}
    assert "003_brownfield_index.sql" in applied

    # Re-opening an upgraded DB is a no-op and keeps the stored document.
    reopened = BrownfieldStore(f"sqlite+aiosqlite:///{db_path}")
    await reopened.initialize()
    try:
        assert await reopened.get_index_entries() == {"/home/u/legacy": ("doc text", "fp1")}
    finally:
        await reopened.close()


async def test_upsert_overwrites_and_remove_clear_drop_entries(tmp_path) -> None:
    store = BrownfieldStore(f"sqlite+aiosqlite:///{tmp_path / 'bf.db'}")
    await store.initialize()
    try:
        await store.register("/a", "a")
        await store.register("/b", "b")
        assert await store.upsert_index_entries({}) == 0
        await store.upsert_index_entries({"/a": ("one", "f1"), "/b": ("two", "f2")})
        await store.upsert_index_entries({"/a": ("uno", "f9")})
        assert await store.get_index_entries() == {"/a": ("uno", "f9"), "/b": ("two", "f2")}

        assert await store.remove("/a") is True
        assert await store.get_index_entries() == {"/b": ("two", "f2")}

        await store.clear_all()
        assert await store.get_index_entries() == {}
    finally:
        await store.close()
