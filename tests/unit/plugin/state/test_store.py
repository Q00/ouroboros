"""Unit tests for ouroboros.plugin.state.store module."""

import json
from pathlib import Path

import pytest

from ouroboros.plugin.state.store import StateMode, StateStore, load_state_store


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    """StateStore backed by a temporary directory."""
    return StateStore(worktree=tmp_path)


class TestStateStoreInitialization:
    """Tests for StateStore setup."""

    def test_creates_state_directories(self, tmp_path: Path) -> None:
        """StateStore creates .omc/state and subdirectories on init."""
        StateStore(worktree=tmp_path)
        assert (tmp_path / ".omc" / "state").is_dir()
        assert (tmp_path / ".omc" / "state" / "checkpoints").is_dir()
        assert (tmp_path / ".omc" / "state" / "backups").is_dir()

    def test_load_state_store_defaults_to_cwd(self) -> None:
        """load_state_store() returns a StateStore without raising."""
        store = load_state_store()
        assert isinstance(store, StateStore)

    def test_load_state_store_accepts_path(self, tmp_path: Path) -> None:
        """load_state_store() accepts an explicit worktree path."""
        store = load_state_store(worktree=tmp_path)
        assert store._worktree == tmp_path


class TestReadWriteRoundtrip:
    """Tests for write_mode_state / read_mode_state."""

    async def test_write_then_read_returns_same_data(self, store: StateStore) -> None:
        """Data written with write_mode_state is recovered by read_mode_state."""
        data = {"tasks": ["a", "b"], "count": 2}
        result = await store.write_mode_state(StateMode.RALPH, data)
        assert result.is_ok

        loaded = await store.read_mode_state(StateMode.RALPH)
        assert loaded is not None
        assert loaded["tasks"] == ["a", "b"]
        assert loaded["count"] == 2

    async def test_read_nonexistent_mode_returns_none(self, store: StateStore) -> None:
        """read_mode_state returns None when no state file exists."""
        result = await store.read_mode_state(StateMode.AUTOPILOT)
        assert result is None

    async def test_write_adds_version_field(self, store: StateStore) -> None:
        """write_mode_state stamps a version onto data that lacks one."""
        await store.write_mode_state(StateMode.RALPH, {"x": 1})
        loaded = await store.read_mode_state(StateMode.RALPH)
        assert loaded is not None
        assert "version" in loaded

    async def test_write_adds_last_modified_field(self, store: StateStore) -> None:
        """write_mode_state adds a last_modified timestamp."""
        await store.write_mode_state(StateMode.RALPH, {"x": 1})
        loaded = await store.read_mode_state(StateMode.RALPH)
        assert loaded is not None
        assert "last_modified" in loaded

    async def test_overwrite_existing_state(self, store: StateStore) -> None:
        """Writing twice overwrites the previous state (covers Path.replace fix)."""
        await store.write_mode_state(StateMode.RALPH, {"v": 1})
        await store.write_mode_state(StateMode.RALPH, {"v": 2})

        loaded = await store.read_mode_state(StateMode.RALPH)
        assert loaded is not None
        assert loaded["v"] == 2

    async def test_state_file_is_valid_json(self, store: StateStore) -> None:
        """The written state file is valid JSON."""
        await store.write_mode_state(StateMode.RALPH, {"key": "value"})
        state_path = store.get_mode_state_path(StateMode.RALPH)
        with state_path.open() as f:
            data = json.load(f)
        assert data["key"] == "value"

    async def test_no_temp_file_left_after_write(self, store: StateStore) -> None:
        """No .tmp file is left on disk after a successful write."""
        await store.write_mode_state(StateMode.RALPH, {"x": 1})
        state_path = store.get_mode_state_path(StateMode.RALPH)
        tmp_path = state_path.with_suffix(".tmp")
        assert not tmp_path.exists()


class TestDeleteModeState:
    """Tests for delete_mode_state."""

    async def test_delete_removes_state_file(self, store: StateStore) -> None:
        """delete_mode_state removes the state file and returns True."""
        await store.write_mode_state(StateMode.RALPH, {"x": 1})
        result = await store.delete_mode_state(StateMode.RALPH)

        assert result.is_ok
        assert result.value is True
        assert await store.read_mode_state(StateMode.RALPH) is None

    async def test_delete_nonexistent_returns_false(self, store: StateStore) -> None:
        """delete_mode_state returns False when no file exists."""
        result = await store.delete_mode_state(StateMode.AUTOPILOT)
        assert result.is_ok
        assert result.value is False


class TestCheckpointRoundtrip:
    """Tests for create_checkpoint / load_checkpoint."""

    async def test_create_checkpoint_returns_id(self, store: StateStore) -> None:
        """create_checkpoint returns a checkpoint ID string."""
        result = await store.create_checkpoint({"session_id": "abc", "phase": "run"})
        assert result.is_ok
        assert isinstance(result.value, str)
        assert result.value.startswith("checkpoint_")

    async def test_load_checkpoint_after_create(self, store: StateStore) -> None:
        """A checkpoint created with create_checkpoint can be loaded back."""
        result = await store.create_checkpoint({"session_id": "s1", "step": 42})
        checkpoint_id = result.value

        data = await store.load_checkpoint(checkpoint_id)
        assert data is not None
        assert data["session_id"] == "s1"
        assert data["step"] == 42

    async def test_load_nonexistent_checkpoint_returns_none(self, store: StateStore) -> None:
        """load_checkpoint returns None for an unknown checkpoint ID."""
        result = await store.load_checkpoint("does-not-exist")
        assert result is None

    async def test_create_checkpoint_with_explicit_id(self, store: StateStore) -> None:
        """create_checkpoint accepts an explicit checkpoint ID."""
        result = await store.create_checkpoint({"data": 1}, checkpoint_id="my-cp-001")
        assert result.is_ok
        assert result.value == "my-cp-001"

        loaded = await store.load_checkpoint("my-cp-001")
        assert loaded is not None
        assert loaded["data"] == 1

    async def test_checkpoint_file_is_valid_json(self, store: StateStore) -> None:
        """The checkpoint file written to disk is valid JSON."""
        result = await store.create_checkpoint({"k": "v"}, checkpoint_id="json-test")
        cp_path = store.get_checkpoint_path("json-test")
        with cp_path.open() as f:
            data = json.load(f)
        assert data["k"] == "v"

    async def test_no_temp_file_left_after_create(self, store: StateStore) -> None:
        """No .tmp file is left on disk after a successful checkpoint create."""
        result = await store.create_checkpoint({"x": 1}, checkpoint_id="no-tmp-test")
        cp_path = store.get_checkpoint_path("no-tmp-test")
        tmp_path = cp_path.with_suffix(".tmp")
        assert not tmp_path.exists()

    async def test_overwrite_checkpoint_with_same_id(self, store: StateStore) -> None:
        """Creating a checkpoint with an existing ID overwrites it (covers Path.replace fix)."""
        await store.create_checkpoint({"v": 1}, checkpoint_id="dup-cp")
        await store.create_checkpoint({"v": 2}, checkpoint_id="dup-cp")

        data = await store.load_checkpoint("dup-cp")
        assert data is not None
        assert data["v"] == 2


class TestListCheckpoints:
    """Tests for list_checkpoints."""

    async def test_empty_store_returns_empty_list(self, store: StateStore) -> None:
        """list_checkpoints returns empty list when no checkpoints exist."""
        result = await store.list_checkpoints()
        assert result == []

    async def test_lists_created_checkpoints(self, store: StateStore) -> None:
        """list_checkpoints returns all created checkpoints."""
        await store.create_checkpoint({"a": 1}, checkpoint_id="cp-a")
        await store.create_checkpoint({"b": 2}, checkpoint_id="cp-b")

        result = await store.list_checkpoints()
        ids = {item["checkpoint_id"] for item in result}
        assert "cp-a" in ids
        assert "cp-b" in ids


class TestDeleteCheckpoint:
    """Tests for delete_checkpoint."""

    async def test_delete_removes_checkpoint(self, store: StateStore) -> None:
        """delete_checkpoint removes the checkpoint file."""
        await store.create_checkpoint({"x": 1}, checkpoint_id="to-delete")
        result = await store.delete_checkpoint("to-delete")

        assert result.is_ok
        assert result.value is True
        assert await store.load_checkpoint("to-delete") is None

    async def test_delete_nonexistent_returns_false(self, store: StateStore) -> None:
        """delete_checkpoint returns False for a nonexistent checkpoint."""
        result = await store.delete_checkpoint("ghost")
        assert result.is_ok
        assert result.value is False
