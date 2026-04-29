"""Unit tests for BrownfieldStore: DB setup, CRUD, pagination, and edge cases."""

from __future__ import annotations

import pytest

from ouroboros.core.errors import PersistenceError
from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore


@pytest.fixture
async def store(tmp_path):
    """Create a BrownfieldStore backed by a temp SQLite file."""
    db_path = tmp_path / "test_brownfield.db"
    s = BrownfieldStore(f"sqlite+aiosqlite:///{db_path}")
    await s.initialize()
    yield s
    await s.close()


async def _seed(store: BrownfieldStore, count: int) -> list[BrownfieldRepo]:
    """Register *count* repos named repo-00 … repo-{count-1}."""
    repos = []
    for i in range(count):
        r = await store.register(
            path=f"/home/user/repo-{i:02d}",
            name=f"repo-{i:02d}",
            desc=f"Project {i}",
        )
        repos.append(r)
    return repos


# ── DB Initialization ────────────────────────────────────────────


class TestBrownfieldStoreInitialization:
    """Test DB setup and initialization."""

    @pytest.mark.asyncio
    async def test_initialize_creates_table(self, tmp_path) -> None:
        """initialize() creates brownfield_repos table in a fresh DB."""
        db_path = tmp_path / "fresh.db"
        s = BrownfieldStore(f"sqlite+aiosqlite:///{db_path}")
        await s.initialize()

        # Table exists — listing returns empty without error
        repos = await s.list()
        assert repos == []
        await s.close()

    @pytest.mark.asyncio
    async def test_initialize_is_idempotent(self, tmp_path) -> None:
        """Calling initialize() twice does not raise or corrupt data."""
        db_path = tmp_path / "idempotent.db"
        s = BrownfieldStore(f"sqlite+aiosqlite:///{db_path}")
        await s.initialize()
        await s.register("/a", "a")
        await s.initialize()  # second call should be safe

        repos = await s.list()
        assert len(repos) == 1
        assert repos[0].name == "a"
        await s.close()

    @pytest.mark.asyncio
    async def test_operations_fail_without_initialize(self, tmp_path) -> None:
        """CRUD calls before initialize() raise PersistenceError."""
        db_path = tmp_path / "uninit.db"
        s = BrownfieldStore(f"sqlite+aiosqlite:///{db_path}")

        with pytest.raises(PersistenceError):
            await s.list()

        with pytest.raises(PersistenceError):
            await s.register("/x", "x")

        with pytest.raises(PersistenceError):
            await s.remove("/x")

        with pytest.raises(PersistenceError):
            await s.get_default()

    @pytest.mark.asyncio
    async def test_from_engine_shares_connection(self, store: BrownfieldStore) -> None:
        """from_engine() creates a store that shares the same engine."""
        # Register via the original store
        await store.register("/home/user/shared", "shared")

        # Create a second store from the same engine
        engine = store._engine
        store2 = BrownfieldStore.from_engine(engine)

        repos = await store2.list()
        assert len(repos) == 1
        assert repos[0].name == "shared"

    @pytest.mark.asyncio
    async def test_close_disposes_engine(self, tmp_path) -> None:
        """close() disposes engine; subsequent calls fail."""
        db_path = tmp_path / "close_test.db"
        s = BrownfieldStore(f"sqlite+aiosqlite:///{db_path}")
        await s.initialize()
        await s.register("/a", "a")
        await s.close()

        # After close, engine is None so operations raise PersistenceError
        with pytest.raises(PersistenceError):
            await s.list()

    @pytest.mark.asyncio
    async def test_failed_initialize_rolls_back_to_uninitialized(
        self, tmp_path, monkeypatch
    ) -> None:
        """A failed initialize() must leave the store explicitly un-initialized.

        Regression for the readiness contract: a partial init (engine
        attached but schema not yet created or migrations not yet applied)
        must not be exposed as ready. The next call to ``initialize()``
        should be free to retry, and intermediate operations must raise
        ``PersistenceError`` rather than running against a half-built engine.
        """
        db_path = tmp_path / "partial.db"
        s = BrownfieldStore(f"sqlite+aiosqlite:///{db_path}")

        async def _boom(_engine):
            raise RuntimeError("simulated migration failure")

        # Force run_migrations to fail after the engine is created and
        # schema metadata has been attached — exactly the half-initialized
        # window that motivated the readiness flag.
        monkeypatch.setattr(
            "ouroboros.persistence.migrations.runner.run_migrations",
            _boom,
        )

        with pytest.raises(RuntimeError, match="simulated migration failure"):
            await s.initialize()

        # Engine must be disposed and the readiness flag cleared so
        # subsequent operations cannot silently use a broken engine.
        assert s._engine is None
        assert s._initialized is False
        with pytest.raises(PersistenceError):
            await s.list()

        # A retry — once the underlying issue is fixed — must succeed
        # cleanly without leaking a stale engine reference.
        monkeypatch.undo()
        await s.initialize()
        assert s._initialized is True
        repos = await s.list()
        assert repos == []
        await s.close()


# ── Single Register ──────────────────────────────────────────────


class TestBrownfieldStoreRegister:
    """Test single repo registration (insert / upsert)."""

    @pytest.mark.asyncio
    async def test_register_returns_repo(self, store: BrownfieldStore) -> None:
        repo = await store.register("/home/user/proj", "proj", desc="My project")
        assert isinstance(repo, BrownfieldRepo)
        assert repo.path == "/home/user/proj"
        assert repo.name == "proj"
        assert repo.desc == "My project"
        assert repo.is_default is False

    @pytest.mark.asyncio
    async def test_register_with_default(self, store: BrownfieldStore) -> None:
        repo = await store.register("/home/user/proj", "proj", is_default=True)
        assert repo.is_default is True

        default = await store.get_default()
        assert default is not None
        assert default.path == "/home/user/proj"

    @pytest.mark.asyncio
    async def test_register_upsert_updates_existing(self, store: BrownfieldStore) -> None:
        """Re-registering the same path updates name/desc."""
        await store.register("/home/user/proj", "old-name", desc="old desc")
        await store.register("/home/user/proj", "new-name", desc="new desc")

        repos = await store.list()
        assert len(repos) == 1
        assert repos[0].name == "new-name"
        assert repos[0].desc == "new desc"

    @pytest.mark.asyncio
    async def test_register_default_does_not_clear_previous_default(
        self, store: BrownfieldStore
    ) -> None:
        """register(is_default=True) adds a new default without clearing others (multi-default)."""
        await store.register("/a", "a", is_default=True)
        await store.register("/b", "b", is_default=True)

        repos = await store.list()
        defaults = [r for r in repos if r.is_default]
        assert len(defaults) == 2
        default_paths = {r.path for r in defaults}
        assert default_paths == {"/a", "/b"}

    @pytest.mark.asyncio
    async def test_register_without_desc(self, store: BrownfieldStore) -> None:
        repo = await store.register("/home/user/proj", "proj")
        assert repo.desc is None


# ── Get Default ──────────────────────────────────────────────────


class TestBrownfieldStoreGetDefault:
    """Test get_default() retrieval."""

    @pytest.mark.asyncio
    async def test_get_default_empty_db(self, store: BrownfieldStore) -> None:
        assert await store.get_default() is None

    @pytest.mark.asyncio
    async def test_get_default_no_default_set(self, store: BrownfieldStore) -> None:
        await store.register("/a", "a")
        await store.register("/b", "b")
        assert await store.get_default() is None

    @pytest.mark.asyncio
    async def test_get_default_returns_correct_repo(self, store: BrownfieldStore) -> None:
        await store.register("/a", "a")
        await store.register("/b", "b", is_default=True)
        await store.register("/c", "c")

        default = await store.get_default()
        assert default is not None
        assert default.path == "/b"
        assert default.is_default is True


# ── Remove ───────────────────────────────────────────────────────


class TestBrownfieldStoreRemove:
    """Test remove() deletion."""

    @pytest.mark.asyncio
    async def test_remove_existing_returns_true(self, store: BrownfieldStore) -> None:
        await store.register("/home/user/proj", "proj")
        result = await store.remove("/home/user/proj")
        assert result is True
        assert await store.count() == 0

    @pytest.mark.asyncio
    async def test_remove_nonexistent_returns_false(self, store: BrownfieldStore) -> None:
        result = await store.remove("/nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_remove_does_not_affect_other_repos(self, store: BrownfieldStore) -> None:
        await store.register("/a", "a")
        await store.register("/b", "b")
        await store.register("/c", "c")

        await store.remove("/b")
        repos = await store.list()
        assert len(repos) == 2
        paths = {r.path for r in repos}
        assert paths == {"/a", "/c"}

    @pytest.mark.asyncio
    async def test_remove_default_clears_default(self, store: BrownfieldStore) -> None:
        """Removing the default repo means get_default returns None."""
        await store.register("/a", "a", is_default=True)
        await store.remove("/a")
        assert await store.get_default() is None


# ── Update Desc ──────────────────────────────────────────────────


class TestBrownfieldStoreUpdateDesc:
    """Test update_desc() method."""

    @pytest.mark.asyncio
    async def test_update_desc_returns_updated_repo(self, store: BrownfieldStore) -> None:
        await store.register("/a", "a", desc="old")
        result = await store.update_desc("/a", "new description")
        assert result is not None
        assert result.desc == "new description"

    @pytest.mark.asyncio
    async def test_update_desc_nonexistent_returns_none(self, store: BrownfieldStore) -> None:
        result = await store.update_desc("/nonexistent", "desc")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_desc_preserves_other_fields(self, store: BrownfieldStore) -> None:
        await store.register("/a", "a", desc="old", is_default=True)
        result = await store.update_desc("/a", "new")
        assert result is not None
        assert result.name == "a"
        assert result.is_default is True


# ── BrownfieldRepo dataclass ────────────────────────────────────


class TestBrownfieldRepoDataclass:
    """Test BrownfieldRepo serialization helpers."""

    def test_to_dict(self) -> None:
        repo = BrownfieldRepo(path="/a", name="a", desc="A project", is_default=True)
        d = repo.to_dict()
        assert d == {"path": "/a", "name": "a", "desc": "A project", "is_default": True}

    def test_to_dict_none_desc(self) -> None:
        repo = BrownfieldRepo(path="/a", name="a")
        assert repo.to_dict()["desc"] == ""
        assert repo.to_dict()["is_default"] is False

    def test_from_dict(self) -> None:
        repo = BrownfieldRepo.from_dict({"path": "/a", "name": "a", "desc": "A project"})
        assert repo.path == "/a"
        assert repo.name == "a"
        assert repo.desc == "A project"

    def test_from_dict_missing_keys(self) -> None:
        with pytest.raises(ValueError, match="Missing required keys"):
            BrownfieldRepo.from_dict({"path": "/a"})

    def test_from_dict_empty_path(self) -> None:
        with pytest.raises(ValueError, match="'path' must not be empty"):
            BrownfieldRepo.from_dict({"path": "", "name": "a"})

    def test_from_dict_empty_name(self) -> None:
        with pytest.raises(ValueError, match="'name' must not be empty"):
            BrownfieldRepo.from_dict({"path": "/a", "name": "  "})

    def test_from_row(self) -> None:
        repo = BrownfieldRepo.from_row({"path": "/a", "name": "a", "desc": "desc", "is_default": 1})
        assert repo.is_default is True


# ── Pagination (existing) ───────────────────────────────────────


class TestBrownfieldStoreList:
    """Test list() with offset/limit pagination."""

    @pytest.mark.asyncio
    async def test_list_no_pagination(self, store: BrownfieldStore) -> None:
        await _seed(store, 5)
        repos = await store.list()
        assert len(repos) == 5

    @pytest.mark.asyncio
    async def test_list_with_limit(self, store: BrownfieldStore) -> None:
        await _seed(store, 5)
        repos = await store.list(limit=2)
        assert len(repos) == 2
        assert repos[0].name == "repo-00"
        assert repos[1].name == "repo-01"

    @pytest.mark.asyncio
    async def test_list_with_offset(self, store: BrownfieldStore) -> None:
        await _seed(store, 5)
        repos = await store.list(offset=3)
        assert len(repos) == 2
        assert repos[0].name == "repo-03"
        assert repos[1].name == "repo-04"

    @pytest.mark.asyncio
    async def test_list_with_offset_and_limit(self, store: BrownfieldStore) -> None:
        await _seed(store, 5)
        repos = await store.list(offset=1, limit=2)
        assert len(repos) == 2
        assert repos[0].name == "repo-01"
        assert repos[1].name == "repo-02"

    @pytest.mark.asyncio
    async def test_list_offset_beyond_total(self, store: BrownfieldStore) -> None:
        await _seed(store, 3)
        repos = await store.list(offset=10)
        assert repos == []

    @pytest.mark.asyncio
    async def test_list_limit_larger_than_remaining(self, store: BrownfieldStore) -> None:
        await _seed(store, 3)
        repos = await store.list(offset=1, limit=100)
        assert len(repos) == 2

    @pytest.mark.asyncio
    async def test_list_limit_zero(self, store: BrownfieldStore) -> None:
        await _seed(store, 3)
        repos = await store.list(limit=0)
        assert repos == []


class TestBrownfieldStoreCount:
    """Test count() method."""

    @pytest.mark.asyncio
    async def test_count_empty(self, store: BrownfieldStore) -> None:
        assert await store.count() == 0

    @pytest.mark.asyncio
    async def test_count_after_inserts(self, store: BrownfieldStore) -> None:
        await _seed(store, 4)
        assert await store.count() == 4

    @pytest.mark.asyncio
    async def test_count_after_remove(self, store: BrownfieldStore) -> None:
        await _seed(store, 3)
        await store.remove("/home/user/repo-01")
        assert await store.count() == 2


class TestBrownfieldStoreBulkRegister:
    """Test bulk_register() method."""

    @pytest.mark.asyncio
    async def test_bulk_register_inserts_all(self, store: BrownfieldStore) -> None:
        repos = [
            {"path": "/home/user/alpha", "name": "alpha"},
            {"path": "/home/user/beta", "name": "beta"},
            {"path": "/home/user/gamma", "name": "gamma"},
        ]
        count = await store.bulk_register(repos)
        assert count == 3
        assert await store.count() == 3

    @pytest.mark.asyncio
    async def test_bulk_register_sets_empty_desc(self, store: BrownfieldStore) -> None:
        repos = [{"path": "/home/user/proj", "name": "proj"}]
        await store.bulk_register(repos)
        result = await store.list()
        assert len(result) == 1
        assert result[0].desc == ""

    @pytest.mark.asyncio
    async def test_bulk_register_sets_is_default_false(self, store: BrownfieldStore) -> None:
        repos = [{"path": "/home/user/proj", "name": "proj"}]
        await store.bulk_register(repos)
        result = await store.list()
        assert result[0].is_default is False

    @pytest.mark.asyncio
    async def test_bulk_register_empty_list(self, store: BrownfieldStore) -> None:
        count = await store.bulk_register([])
        assert count == 0
        assert await store.count() == 0

    @pytest.mark.asyncio
    async def test_bulk_register_preserves_existing_metadata(self, store: BrownfieldStore) -> None:
        """bulk_register skips existing repos to preserve desc/is_default."""
        await store.register("/home/user/proj", "proj", desc="Old desc", is_default=True)

        # Bulk register the same path — should be skipped
        count = await store.bulk_register([{"path": "/home/user/proj", "name": "proj-renamed"}])

        assert count == 0  # Nothing new registered
        result = await store.list()
        assert len(result) == 1
        assert result[0].name == "proj"  # Original name preserved
        assert result[0].desc == "Old desc"  # Metadata preserved
        assert result[0].is_default is True  # Default preserved

    @pytest.mark.asyncio
    async def test_bulk_register_preserves_other_repos(self, store: BrownfieldStore) -> None:
        """Existing repos NOT in the bulk list are not affected."""
        await store.register("/home/user/existing", "existing", desc="Keep me")
        await store.bulk_register([{"path": "/home/user/new", "name": "new"}])

        assert await store.count() == 2
        repos = await store.list()
        existing = next(r for r in repos if r.path == "/home/user/existing")
        assert existing.desc == "Keep me"


class TestBrownfieldStoreClearAll:
    """Test clear_all() method."""

    @pytest.mark.asyncio
    async def test_clear_all_empty_table(self, store: BrownfieldStore) -> None:
        """clear_all on empty table returns 0."""
        deleted = await store.clear_all()
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_clear_all_removes_all_rows(self, store: BrownfieldStore) -> None:
        """clear_all removes all registered repos."""
        await _seed(store, 5)
        assert await store.count() == 5

        deleted = await store.clear_all()
        assert deleted == 5
        assert await store.count() == 0

    @pytest.mark.asyncio
    async def test_clear_all_allows_fresh_register(self, store: BrownfieldStore) -> None:
        """After clear_all, new repos can be registered cleanly."""
        await _seed(store, 3)
        await store.clear_all()

        # Register new repos
        await store.register("/home/user/new-repo", "new-repo", desc="Fresh")
        repos = await store.list()
        assert len(repos) == 1
        assert repos[0].name == "new-repo"

    @pytest.mark.asyncio
    async def test_clear_all_resets_defaults(self, store: BrownfieldStore) -> None:
        """clear_all removes default repo — get_default returns None after."""
        await store.register("/home/user/proj", "proj", is_default=True)
        assert await store.get_default() is not None

        await store.clear_all()
        assert await store.get_default() is None


class TestBrownfieldStoreSetDefault:
    """Test set_default preserves desc on previously-default repos."""

    @pytest.mark.asyncio
    async def test_unset_default_preserves_desc(self, store: BrownfieldStore) -> None:
        """When switching default from A→B, A's desc must be preserved."""
        await store.register(
            path="/home/user/repo-a",
            name="repo-a",
            desc="Description for repo A",
            is_default=True,
        )
        await store.register(
            path="/home/user/repo-b",
            name="repo-b",
            desc="Description for repo B",
        )

        # Switch default to repo-b
        result = await store.set_single_default("/home/user/repo-b")
        assert result is not None
        assert result.is_default is True

        # Verify repo-a still has its desc
        repos = await store.list()
        repo_a = next(r for r in repos if r.path == "/home/user/repo-a")
        assert repo_a.desc == "Description for repo A"
        assert repo_a.is_default is False

    @pytest.mark.asyncio
    async def test_unset_default_preserves_desc_multiple_switches(
        self, store: BrownfieldStore
    ) -> None:
        """Switching default multiple times must never lose any desc."""
        await store.register("/home/user/a", "a", desc="Desc A", is_default=True)
        await store.register("/home/user/b", "b", desc="Desc B")
        await store.register("/home/user/c", "c", desc="Desc C")

        # Switch A → B → C → A
        await store.set_single_default("/home/user/b")
        await store.set_single_default("/home/user/c")
        await store.set_single_default("/home/user/a")

        repos = await store.list()
        descs = {r.name: r.desc for r in repos}
        assert descs == {"a": "Desc A", "b": "Desc B", "c": "Desc C"}

        # Only 'a' should be default
        defaults = [r for r in repos if r.is_default]
        assert len(defaults) == 1
        assert defaults[0].name == "a"

    @pytest.mark.asyncio
    async def test_register_with_default_preserves_other_desc(self, store: BrownfieldStore) -> None:
        """register(is_default=True) must preserve desc AND is_default on existing repos.

        With multi-default support, register() no longer clears other defaults.
        Both old and new repos should be marked as default.
        """
        await store.register("/home/user/old", "old", desc="Old desc", is_default=True)

        # Register a new repo as default — old stays default too (multi-default)
        await store.register("/home/user/new", "new", desc="New desc", is_default=True)

        repos = await store.list()
        old = next(r for r in repos if r.name == "old")
        assert old.desc == "Old desc"
        assert old.is_default is True  # NOT cleared — multi-default

        new = next(r for r in repos if r.name == "new")
        assert new.desc == "New desc"
        assert new.is_default is True
