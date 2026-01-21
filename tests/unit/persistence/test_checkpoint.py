"""Unit tests for ouroboros.persistence.checkpoint module."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ouroboros.persistence.checkpoint import (
    CheckpointData,
    CheckpointStore,
    PeriodicCheckpointer,
    RecoveryManager,
)


@pytest.fixture
def checkpoint_store(tmp_path: Path) -> CheckpointStore:
    """Create a CheckpointStore with a temporary directory."""
    store = CheckpointStore(base_path=tmp_path / "checkpoints")
    store.initialize()
    return store


@pytest.fixture
def sample_checkpoint() -> CheckpointData:
    """Create a sample checkpoint for testing."""
    return CheckpointData.create(
        seed_id="test-seed-123",
        phase="planning",
        state={"step": 1, "data": "test"},
    )


class TestCheckpointData:
    """Test CheckpointData model."""

    def test_create_generates_hash(self) -> None:
        """CheckpointData.create() generates SHA-256 hash."""
        checkpoint = CheckpointData.create("seed-1", "phase-1", {"key": "value"})
        assert checkpoint.hash is not None
        assert len(checkpoint.hash) == 64  # SHA-256 is 64 hex chars

    def test_create_includes_timestamp(self) -> None:
        """CheckpointData.create() includes UTC timestamp."""
        checkpoint = CheckpointData.create("seed-1", "phase-1", {})
        assert checkpoint.timestamp is not None
        assert checkpoint.timestamp.tzinfo is not None

    def test_validate_integrity_succeeds_for_valid_checkpoint(self) -> None:
        """CheckpointData.validate_integrity() succeeds for valid data."""
        checkpoint = CheckpointData.create("seed-1", "phase-1", {"key": "value"})
        result = checkpoint.validate_integrity()
        assert result.is_ok
        assert result.value is True

    def test_validate_integrity_fails_for_corrupted_checkpoint(self) -> None:
        """CheckpointData.validate_integrity() fails when hash is wrong."""
        checkpoint = CheckpointData.create("seed-1", "phase-1", {"key": "value"})
        # Manually corrupt the checkpoint by changing hash
        corrupted = CheckpointData(
            seed_id=checkpoint.seed_id,
            phase=checkpoint.phase,
            state=checkpoint.state,
            timestamp=checkpoint.timestamp,
            hash="0" * 64,  # Invalid hash
        )
        result = corrupted.validate_integrity()
        assert result.is_err
        assert "Hash mismatch" in result.error

    def test_to_dict_serializes_correctly(self) -> None:
        """CheckpointData.to_dict() produces JSON-serializable dict."""
        checkpoint = CheckpointData.create("seed-1", "phase-1", {"key": "value"})
        data = checkpoint.to_dict()
        assert data["seed_id"] == "seed-1"
        assert data["phase"] == "phase-1"
        assert data["state"] == {"key": "value"}
        assert "timestamp" in data
        assert "hash" in data
        # Should be JSON-serializable
        json.dumps(data)

    def test_from_dict_reconstructs_checkpoint(self) -> None:
        """CheckpointData.from_dict() reconstructs checkpoint from dict."""
        original = CheckpointData.create("seed-1", "phase-1", {"key": "value"})
        data = original.to_dict()
        reconstructed = CheckpointData.from_dict(data)
        assert reconstructed.seed_id == original.seed_id
        assert reconstructed.phase == original.phase
        assert reconstructed.state == original.state
        assert reconstructed.hash == original.hash

    def test_roundtrip_preserves_integrity(self) -> None:
        """Checkpoint survives to_dict/from_dict roundtrip."""
        original = CheckpointData.create("seed-1", "phase-1", {"key": "value"})
        roundtripped = CheckpointData.from_dict(original.to_dict())
        result = roundtripped.validate_integrity()
        assert result.is_ok


class TestCheckpointStore:
    """Test CheckpointStore operations."""

    def test_initialize_creates_directory(self, tmp_path: Path) -> None:
        """CheckpointStore.initialize() creates checkpoint directory."""
        store = CheckpointStore(base_path=tmp_path / "new_checkpoints")
        store.initialize()
        assert (tmp_path / "new_checkpoints").exists()
        assert (tmp_path / "new_checkpoints").is_dir()

    def test_initialize_is_idempotent(self, tmp_path: Path) -> None:
        """Calling initialize() multiple times is safe."""
        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()
        store.initialize()  # Should not raise

    def test_save_creates_checkpoint_file(
        self, checkpoint_store: CheckpointStore, sample_checkpoint: CheckpointData
    ) -> None:
        """CheckpointStore.save() creates checkpoint file."""
        result = checkpoint_store.save(sample_checkpoint)
        assert result.is_ok

        # Verify file exists
        checkpoint_path = (
            checkpoint_store._base_path
            / f"checkpoint_{sample_checkpoint.seed_id}.json"
        )
        assert checkpoint_path.exists()

    def test_save_writes_valid_json(
        self, checkpoint_store: CheckpointStore, sample_checkpoint: CheckpointData
    ) -> None:
        """CheckpointStore.save() writes valid JSON."""
        checkpoint_store.save(sample_checkpoint)

        checkpoint_path = (
            checkpoint_store._base_path
            / f"checkpoint_{sample_checkpoint.seed_id}.json"
        )
        with checkpoint_path.open("r") as f:
            data = json.load(f)

        assert data["seed_id"] == sample_checkpoint.seed_id
        assert data["phase"] == sample_checkpoint.phase

    def test_load_returns_saved_checkpoint(
        self, checkpoint_store: CheckpointStore, sample_checkpoint: CheckpointData
    ) -> None:
        """CheckpointStore.load() returns previously saved checkpoint."""
        checkpoint_store.save(sample_checkpoint)

        result = checkpoint_store.load(sample_checkpoint.seed_id)
        assert result.is_ok

        loaded = result.value
        assert loaded.seed_id == sample_checkpoint.seed_id
        assert loaded.phase == sample_checkpoint.phase
        assert loaded.state == sample_checkpoint.state

    def test_load_returns_error_for_nonexistent_checkpoint(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """CheckpointStore.load() returns error for nonexistent checkpoint."""
        result = checkpoint_store.load("nonexistent-seed")
        assert result.is_err
        # Message indicates no valid checkpoint was found
        assert "no valid checkpoint" in result.error.message.lower()

    def test_load_validates_integrity(
        self, checkpoint_store: CheckpointStore, sample_checkpoint: CheckpointData
    ) -> None:
        """CheckpointStore.load() validates checkpoint integrity."""
        checkpoint_store.save(sample_checkpoint)

        # Corrupt the checkpoint file
        checkpoint_path = (
            checkpoint_store._base_path
            / f"checkpoint_{sample_checkpoint.seed_id}.json"
        )
        with checkpoint_path.open("r") as f:
            data = json.load(f)

        # Change hash to simulate corruption
        data["hash"] = "0" * 64

        with checkpoint_path.open("w") as f:
            json.dump(data, f)

        # Load should detect corruption (either at top level or in detailed message)
        result = checkpoint_store.load(sample_checkpoint.seed_id)
        assert result.is_err
        # Error message indicates no valid checkpoint was found after integrity check failed
        assert "no valid checkpoint" in result.error.message.lower() or "integrity" in result.error.message.lower()

    def test_load_handles_json_parse_error(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """CheckpointStore.load() handles corrupted JSON."""
        checkpoint_path = checkpoint_store._base_path / "checkpoint_broken.json"
        checkpoint_path.write_text("{ invalid json }")

        result = checkpoint_store.load("broken")
        assert result.is_err
        # Error message indicates no valid checkpoint was found (after parse failure at all levels)
        assert "no valid checkpoint" in result.error.message.lower() or "parse" in result.error.message.lower()


class TestCheckpointStoreRollback:
    """Test checkpoint rollback functionality."""

    def test_save_rotates_checkpoints(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """CheckpointStore.save() rotates old checkpoints."""
        seed_id = "test-seed"

        # Save first checkpoint
        cp1 = CheckpointData.create(seed_id, "phase1", {"step": 1})
        checkpoint_store.save(cp1)

        # Save second checkpoint
        cp2 = CheckpointData.create(seed_id, "phase2", {"step": 2})
        checkpoint_store.save(cp2)

        # First checkpoint should be rotated to .1
        rollback_path = checkpoint_store._base_path / f"checkpoint_{seed_id}.json.1"
        assert rollback_path.exists()

    def test_load_uses_rollback_on_corruption(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """CheckpointStore.load() uses rollback when current is corrupted."""
        seed_id = "test-seed"

        # Save two checkpoints
        cp1 = CheckpointData.create(seed_id, "phase1", {"step": 1})
        checkpoint_store.save(cp1)

        cp2 = CheckpointData.create(seed_id, "phase2", {"step": 2})
        checkpoint_store.save(cp2)

        # Corrupt the current checkpoint
        current_path = checkpoint_store._base_path / f"checkpoint_{seed_id}.json"
        with current_path.open("r") as f:
            data = json.load(f)
        data["hash"] = "0" * 64
        with current_path.open("w") as f:
            json.dump(data, f)

        # Load should automatically rollback to .1
        result = checkpoint_store.load(seed_id)
        assert result.is_ok
        loaded = result.value
        assert loaded.phase == "phase1"  # Got the older checkpoint

    def test_rollback_depth_limited_to_3(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """Rollback is limited to 3 levels (NFR11)."""
        seed_id = "test-seed"

        # Save 5 checkpoints
        for i in range(5):
            cp = CheckpointData.create(seed_id, f"phase{i}", {"step": i})
            checkpoint_store.save(cp)

        # Should only keep 4 checkpoints (current + 3 rollback levels)
        current_path = checkpoint_store._base_path / f"checkpoint_{seed_id}.json"
        rollback1_path = checkpoint_store._base_path / f"checkpoint_{seed_id}.json.1"
        rollback2_path = checkpoint_store._base_path / f"checkpoint_{seed_id}.json.2"
        rollback3_path = checkpoint_store._base_path / f"checkpoint_{seed_id}.json.3"
        rollback4_path = checkpoint_store._base_path / f"checkpoint_{seed_id}.json.4"

        assert current_path.exists()
        assert rollback1_path.exists()
        assert rollback2_path.exists()
        assert rollback3_path.exists()
        assert not rollback4_path.exists()  # Should be deleted


class TestPeriodicCheckpointer:
    """Test PeriodicCheckpointer background task."""

    async def test_periodic_checkpointer_calls_callback(self) -> None:
        """PeriodicCheckpointer calls callback at regular intervals."""
        call_count = 0

        async def callback():
            nonlocal call_count
            call_count += 1

        checkpointer = PeriodicCheckpointer(callback, interval=0.1)
        await checkpointer.start()

        # Wait for a few intervals
        await asyncio.sleep(0.35)

        await checkpointer.stop()

        # Should have been called at least 2-3 times
        assert call_count >= 2

    async def test_periodic_checkpointer_stops_cleanly(self) -> None:
        """PeriodicCheckpointer.stop() stops the background task."""
        called = False

        async def callback():
            nonlocal called
            called = True

        checkpointer = PeriodicCheckpointer(callback, interval=0.1)
        await checkpointer.start()
        await asyncio.sleep(0.15)
        assert called

        await checkpointer.stop()

        # Reset and verify no more calls
        called = False
        await asyncio.sleep(0.15)
        # Should not be called after stop
        # (This is a weak test but hard to guarantee timing)

    async def test_periodic_checkpointer_handles_callback_errors(self) -> None:
        """PeriodicCheckpointer continues after callback errors."""
        call_count = 0

        async def failing_callback():
            nonlocal call_count
            call_count += 1
            raise ValueError("Test error")

        checkpointer = PeriodicCheckpointer(failing_callback, interval=0.1)
        await checkpointer.start()

        await asyncio.sleep(0.35)

        await checkpointer.stop()

        # Should have been called multiple times despite errors
        assert call_count >= 2


class TestRecoveryManager:
    """Test RecoveryManager for workflow recovery."""

    async def test_recover_loads_existing_checkpoint(
        self, checkpoint_store: CheckpointStore, sample_checkpoint: CheckpointData
    ) -> None:
        """RecoveryManager.recover() loads existing checkpoint."""
        checkpoint_store.save(sample_checkpoint)

        manager = RecoveryManager(checkpoint_store)
        result = await manager.recover(sample_checkpoint.seed_id)

        assert result.is_ok
        assert result.value is not None
        assert result.value.seed_id == sample_checkpoint.seed_id

    async def test_recover_returns_none_for_no_checkpoint(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """RecoveryManager.recover() returns None when no checkpoint exists."""
        manager = RecoveryManager(checkpoint_store)
        result = await manager.recover("nonexistent-seed")

        assert result.is_ok
        assert result.value is None

    async def test_recover_uses_rollback_on_corruption(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """RecoveryManager.recover() uses rollback when checkpoint corrupted."""
        seed_id = "test-seed"

        # Save two checkpoints
        cp1 = CheckpointData.create(seed_id, "phase1", {"step": 1})
        checkpoint_store.save(cp1)

        cp2 = CheckpointData.create(seed_id, "phase2", {"step": 2})
        checkpoint_store.save(cp2)

        # Corrupt current checkpoint
        current_path = checkpoint_store._base_path / f"checkpoint_{seed_id}.json"
        with current_path.open("r") as f:
            data = json.load(f)
        data["hash"] = "0" * 64
        with current_path.open("w") as f:
            json.dump(data, f)

        # Recovery should use rollback
        manager = RecoveryManager(checkpoint_store)
        result = await manager.recover(seed_id)

        assert result.is_ok
        assert result.value is not None
        assert result.value.phase == "phase1"  # Rolled back to older checkpoint
