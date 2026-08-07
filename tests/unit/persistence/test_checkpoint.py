"""Unit tests for ouroboros.persistence.checkpoint module."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

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
            checkpoint_store._base_path / f"checkpoint_{sample_checkpoint.seed_id}.json"
        )
        assert checkpoint_path.exists()

    def test_save_writes_valid_json(
        self, checkpoint_store: CheckpointStore, sample_checkpoint: CheckpointData
    ) -> None:
        """CheckpointStore.save() writes valid JSON."""
        checkpoint_store.save(sample_checkpoint)

        checkpoint_path = (
            checkpoint_store._base_path / f"checkpoint_{sample_checkpoint.seed_id}.json"
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
            checkpoint_store._base_path / f"checkpoint_{sample_checkpoint.seed_id}.json"
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
        assert (
            "no valid checkpoint" in result.error.message.lower()
            or "integrity" in result.error.message.lower()
        )

    def test_load_handles_json_parse_error(self, checkpoint_store: CheckpointStore) -> None:
        """CheckpointStore.load() handles corrupted JSON."""
        checkpoint_path = checkpoint_store._base_path / "checkpoint_broken.json"
        checkpoint_path.write_text("{ invalid json }")

        result = checkpoint_store.load("broken")
        assert result.is_err
        # Error message indicates no valid checkpoint was found (after parse failure at all levels)
        assert (
            "no valid checkpoint" in result.error.message.lower()
            or "parse" in result.error.message.lower()
        )


class TestCheckpointStorePathTraversal:
    """Test that path traversal attacks via seed_id are blocked."""

    def test_traversal_with_slashes_is_sanitized(self, checkpoint_store: CheckpointStore) -> None:
        """seed_id with path separators is sanitized to underscores."""
        cp = CheckpointData.create("x/../../PWNED", "phase1", {"a": 1})
        result = checkpoint_store.save(cp)
        assert result.is_ok
        # ".." removed -> "x/__/PWNED", slashes -> underscores -> "x___PWNED"
        expected = checkpoint_store._base_path / "checkpoint_x___PWNED.json"
        assert expected.exists()

    def test_traversal_with_backslashes_is_sanitized(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """seed_id with backslash separators is sanitized."""
        cp = CheckpointData.create("x\\..\\..\\PWNED", "phase1", {"a": 1})
        result = checkpoint_store.save(cp)
        assert result.is_ok
        # ".." removed -> "x\\\\" , backslashes -> underscores -> "x___PWNED"
        expected = checkpoint_store._base_path / "checkpoint_x___PWNED.json"
        assert expected.exists()

    def test_colon_in_seed_id_is_sanitized(self, checkpoint_store: CheckpointStore) -> None:
        """Regression (#1155): a colon-bearing seed_id (e.g. an MCP cancel-checkpoint
        id) is sanitized so the checkpoint is writable on Windows (WinError 123)."""
        seed_id = "ouroboros_agent_process_cancel:mcp_job:job_19a927b41098"
        cp = CheckpointData.create(seed_id, "phase1", {"a": 1})
        result = checkpoint_store.save(cp)
        assert result.is_ok
        # Colons -> underscores -> a filename valid on every platform.
        expected = (
            checkpoint_store._base_path
            / "checkpoint_ouroboros_agent_process_cancel_mcp_job_job_19a927b41098.json"
        )
        assert expected.exists()
        # And it round-trips through load() (which re-sanitizes identically).
        load_result = checkpoint_store.load(seed_id)
        assert load_result.is_ok
        assert load_result.value.phase == "phase1"

    @pytest.mark.parametrize("reserved", [":", "*", "?", '"', "<", ">", "|"])
    def test_windows_reserved_chars_are_sanitized(
        self, checkpoint_store: CheckpointStore, reserved: str
    ) -> None:
        """seed_ids with Windows-reserved filename characters are sanitized so the
        on-disk checkpoint path is valid on Windows."""
        cp = CheckpointData.create(f"seed{reserved}id", "phase1", {"a": 1})
        result = checkpoint_store.save(cp)
        assert result.is_ok
        # The reserved char must not survive into the filename.
        path = checkpoint_store._get_checkpoint_path(f"seed{reserved}id")
        assert reserved not in path.name
        assert path.exists()

    def test_traversal_does_not_escape_base_dir(self, checkpoint_store: CheckpointStore) -> None:
        """No checkpoint file is created outside the base directory."""
        cp = CheckpointData.create("../../../etc/passwd", "phase1", {})
        result = checkpoint_store.save(cp)
        assert result.is_ok
        # Ensure nothing was written outside the checkpoint dir

        for entry in checkpoint_store._base_path.parent.iterdir():
            if entry != checkpoint_store._base_path:
                assert "passwd" not in entry.name

    def test_normal_seed_id_still_works(self, checkpoint_store: CheckpointStore) -> None:
        """Normal seed_ids without traversal patterns work correctly."""
        cp = CheckpointData.create("my-seed-123", "phase1", {"step": 1})
        result = checkpoint_store.save(cp)
        assert result.is_ok

        load_result = checkpoint_store.load("my-seed-123")
        assert load_result.is_ok
        assert load_result.value.seed_id == "my-seed-123"

    def test_empty_seed_id_raises_value_error(self, checkpoint_store: CheckpointStore) -> None:
        """Empty seed_id raises ValueError."""
        with pytest.raises(ValueError, match="seed_id must not be empty"):
            checkpoint_store._get_checkpoint_path("")

    def test_null_byte_seed_id_raises_value_error(self, checkpoint_store: CheckpointStore) -> None:
        """seed_id consisting only of null bytes raises ValueError."""
        with pytest.raises(ValueError, match="empty after sanitization"):
            checkpoint_store._get_checkpoint_path("\x00\x00")

    def test_dot_dot_only_seed_id_raises_value_error(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """seed_id that is purely '..' sequences raises ValueError."""
        with pytest.raises(ValueError, match="empty after sanitization"):
            checkpoint_store._get_checkpoint_path("..")

    def test_seed_id_with_null_bytes_stripped(self, checkpoint_store: CheckpointStore) -> None:
        """Null bytes are stripped but remaining content is preserved."""
        cp = CheckpointData.create("seed\x00id", "phase1", {"a": 1})
        result = checkpoint_store.save(cp)
        assert result.is_ok
        expected = checkpoint_store._base_path / "checkpoint_seedid.json"
        assert expected.exists()

    def test_long_seed_id_is_truncated_within_filename_budget(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """seed_id is capped so the full filename never exceeds 255 bytes."""
        long_id = "a" * 300
        path = checkpoint_store._get_checkpoint_path(long_id)
        # The full basename must fit in 255 bytes for any rollback level
        assert len(path.name) <= 255
        # The sanitized seed portion is at most _MAX_SEED_LEN (237)
        seed_part = path.name.removeprefix("checkpoint_").removesuffix(".json")
        assert len(seed_part) <= CheckpointStore._MAX_SEED_LEN
        # With rollback suffix the basename still fits
        path_with_level = checkpoint_store._get_checkpoint_path(long_id, level=3)
        assert len(path_with_level.name) <= 255

    def test_long_seed_id_has_hash_suffix_for_collision_resistance(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """Truncated seed ids include a hash suffix to avoid collisions."""
        import hashlib

        long_id = "a" * 300
        sanitized = CheckpointStore._sanitize_seed_id(long_id)
        # Must end with _<8-hex-char hash>
        assert "_" in sanitized
        hash_suffix = sanitized.rsplit("_", 1)[-1]
        assert len(hash_suffix) == CheckpointStore._HASH_SUFFIX_LEN
        # Verify the hash is derived from the full (pre-truncation) sanitized id
        expected_hash = hashlib.sha256(long_id.encode()).hexdigest()[
            : CheckpointStore._HASH_SUFFIX_LEN
        ]
        assert hash_suffix == expected_hash

    def test_long_seed_id_write_and_read_roundtrip(self, checkpoint_store: CheckpointStore) -> None:
        """Regression: a checkpoint with a very long seed_id can be written and read back."""
        long_id = "b" * 300
        cp = CheckpointData.create(long_id, "phase1", {"step": 42})
        result = checkpoint_store.save(cp)
        assert result.is_ok

        # The file must actually exist on disk
        path = checkpoint_store._get_checkpoint_path(long_id)
        assert path.exists(), f"checkpoint file was not created: {path}"
        assert len(path.name) <= 255

        # Read it back via load (which re-sanitizes the seed_id identically)
        load_result = checkpoint_store.load(long_id)
        assert load_result.is_ok
        assert load_result.value.phase == "phase1"
        assert load_result.value.state == {"step": 42}

    def test_long_seed_id_different_ids_do_not_collide(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """Two long seed_ids that share a 228-char prefix map to different files."""
        prefix = "x" * 228
        id_a = prefix + "a" * 72
        id_b = prefix + "b" * 72
        path_a = checkpoint_store._get_checkpoint_path(id_a)
        path_b = checkpoint_store._get_checkpoint_path(id_b)
        assert path_a != path_b, "distinct long seed_ids must not collide"

    def test_load_with_traversal_seed_id(self, checkpoint_store: CheckpointStore) -> None:
        """load() with a traversal seed_id is safely handled."""
        # Save with a traversal-attempt seed_id
        cp = CheckpointData.create("x/../secret", "phase1", {"a": 1})
        result = checkpoint_store.save(cp)
        assert result.is_ok

        # Load with the same traversal-attempt seed_id -- sanitized consistently
        load_result = checkpoint_store.load("x/../secret")
        assert load_result.is_ok
        assert load_result.value.phase == "phase1"
        # File lives inside the base directory with sanitized name
        expected = checkpoint_store._base_path / "checkpoint_x__secret.json"
        assert expected.exists()


class TestCheckpointStoreRollback:
    """Test checkpoint rollback functionality."""

    def test_save_rotates_checkpoints(self, checkpoint_store: CheckpointStore) -> None:
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

    def test_load_uses_rollback_on_corruption(self, checkpoint_store: CheckpointStore) -> None:
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

    def test_rollback_depth_limited_to_3(self, checkpoint_store: CheckpointStore) -> None:
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


class TestCheckpointStateIsolation:
    """#1829: caller mutation after create() must not desync payload and hash."""

    def test_top_level_mutation_after_create_is_invisible(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        state = {"steps": [1]}
        checkpoint = CheckpointData.create("mutable-state", "execution", state)
        state["added"] = "later"

        assert checkpoint_store.save(checkpoint).is_ok
        loaded = checkpoint_store.load("mutable-state")
        assert loaded.is_ok, "a mutated caller dict invalidated the saved checkpoint"
        assert loaded.value.state == {"steps": [1]}, "the snapshot leaked caller mutation"

    def test_nested_mutation_after_create_is_invisible(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        state = {"steps": [1], "nested": {"flag": True}}
        checkpoint = CheckpointData.create("nested-state", "execution", state)
        state["steps"].append(2)
        state["nested"]["flag"] = False

        assert checkpoint_store.save(checkpoint).is_ok
        loaded = checkpoint_store.load("nested-state")
        assert loaded.is_ok, "nested caller mutation invalidated the saved checkpoint"
        assert loaded.value.state == {"steps": [1], "nested": {"flag": True}}

    def test_save_refuses_a_checkpoint_whose_hash_does_not_match(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """Integrity is validated before any rotation or write."""
        valid = CheckpointData.create("tampered", "execution", {"step": 1})
        assert checkpoint_store.save(valid).is_ok
        current = checkpoint_store._get_checkpoint_path("tampered")
        original = current.read_bytes()

        tampered = CheckpointData(
            seed_id="tampered",
            phase="execution",
            state={"step": 999},
            timestamp=valid.timestamp,
            hash=valid.hash,
        )
        result = checkpoint_store.save(tampered)

        assert result.is_err, "save persisted a checkpoint load() would reject"
        assert current.read_bytes() == original, (
            "a refused save still touched the committed checkpoint"
        )
        rollback = checkpoint_store._get_checkpoint_path("tampered", 1)
        assert not rollback.exists(), "a refused save rotated history"

    def test_mutation_between_validation_and_serialization_is_harmless(
        self, checkpoint_store: CheckpointStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """save() writes the exact snapshot it validated.

        The file lock only serializes store access — it cannot protect the
        caller-owned object. A mutation landing during lock acquisition must
        not desync the written payload from its hash: the persisted file is
        the validated snapshot, and load() accepts it.
        """
        from ouroboros.persistence import checkpoint as checkpoint_module

        saved = CheckpointData.create("race-mutation", "execution", {"step": 1})
        real_lock = checkpoint_module._file_lock

        from contextlib import contextmanager

        @contextmanager
        def mutating_lock(path, exclusive):
            # The bot-shaped interleaving: the holder mutates the live
            # object while save() waits for the lock.
            saved.state["step"] = 999
            with real_lock(path, exclusive=exclusive):
                yield

        monkeypatch.setattr(checkpoint_module, "_file_lock", mutating_lock)
        result = checkpoint_store.save(saved)

        assert result.is_ok, str(result.error) if result.is_err else ""
        loaded = checkpoint_store.load("race-mutation")
        assert loaded.is_ok, "the persisted payload no longer matches its hash"
        assert loaded.value.state == {"step": 1}, (
            "save() wrote a different payload than the one it validated"
        )


class TestCheckpointStoreAtomicSave:
    """#1830: a failed save must not disturb the committed checkpoint chain."""

    def _files(self, store: CheckpointStore) -> list[str]:
        return sorted(entry.name for entry in store._base_path.iterdir())

    def test_failed_write_preserves_current_checkpoint_and_history(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """A partial serialization failure leaves the previous save untouched.

        save() used to rotate the committed checkpoint to level 1 and then
        truncate the destination before serializing, so a mid-write failure
        destroyed the current checkpoint and consumed rollback history.
        """
        first = CheckpointData.create("atomic-write", "execution", {"step": 1})
        assert checkpoint_store.save(first).is_ok
        current = checkpoint_store._get_checkpoint_path("atomic-write")
        original = current.read_bytes()
        files_before = self._files(checkpoint_store)

        def partial_dump(obj, fp, *args, **kwargs):
            fp.write('{"seed_id":"atomic-write","phase":')
            fp.flush()
            raise OSError("simulated disk write failure")

        second = CheckpointData.create("atomic-write", "execution", {"step": 2})
        with patch("ouroboros.persistence.checkpoint.json.dump", side_effect=partial_dump):
            result = checkpoint_store.save(second)

        assert result.is_err
        assert current.read_bytes() == original, "failed save mutated the committed checkpoint"
        assert self._files(checkpoint_store) == files_before, (
            "failed save rotated history or left temporary files behind"
        )
        loaded = checkpoint_store.load("atomic-write")
        assert loaded.is_ok
        assert loaded.value.state == {"step": 1}

    def test_failed_first_save_leaves_no_partial_checkpoint(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """With no prior save, a failed write must leave the store empty."""

        def failing_dump(obj, fp, *args, **kwargs):
            fp.write("{")
            raise OSError("simulated disk write failure")

        checkpoint = CheckpointData.create("first-save", "execution", {"step": 1})
        with patch("ouroboros.persistence.checkpoint.json.dump", side_effect=failing_dump):
            result = checkpoint_store.save(checkpoint)

        assert result.is_err
        current = checkpoint_store._get_checkpoint_path("first-save")
        assert not current.exists(), "failed first save left a partial level-0 checkpoint"
        litter = [name for name in self._files(checkpoint_store) if not name.endswith(".lock")]
        assert litter == [], f"failed first save left files behind: {litter}"
        assert checkpoint_store.load("first-save").is_err

    def test_failed_publish_restores_rotated_history(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """If promoting the staged file fails, the rotation is rolled back.

        The transaction is rotate-then-publish; a failure between the two
        must not leave the chain shifted with no current checkpoint.
        """
        for step in range(1, 6):
            saved = checkpoint_store.save(
                CheckpointData.create("publish-fail", "execution", {"step": step})
            )
            assert saved.is_ok
        current = checkpoint_store._get_checkpoint_path("publish-fail")
        original = current.read_bytes()
        files_before = self._files(checkpoint_store)
        real_replace = os.replace

        def failing_publish(src, dst):
            if Path(dst) == current and ".tmp-" in Path(src).name:
                raise OSError("simulated rename failure")
            return real_replace(src, dst)

        sixth = CheckpointData.create("publish-fail", "execution", {"step": 6})
        with patch("ouroboros.persistence.checkpoint.os.replace", side_effect=failing_publish):
            result = checkpoint_store.save(sixth)

        assert result.is_err
        assert current.read_bytes() == original, "publish failure lost the committed checkpoint"
        assert self._files(checkpoint_store) == files_before, (
            "publish failure left the rotation shifted or files behind"
        )
        loaded = checkpoint_store.load("publish-fail")
        assert loaded.is_ok
        assert loaded.value.state == {"step": 5}

    @pytest.mark.parametrize("failing_undo_step", [1, 2, 3])
    def test_partial_undo_never_overwrites_a_generation(
        self, checkpoint_store: CheckpointStore, failing_undo_step: int
    ) -> None:
        """No committed generation may be lost, whichever undo rename fails.

        After a failed promotion the rotation is undone in reverse; if an
        undo rename itself fails, the retired oldest checkpoint must not be
        restored over a level that still holds an unshifted generation.
        Every pre-save generation has to survive somewhere on disk.
        """
        for step in range(1, 6):
            assert checkpoint_store.save(
                CheckpointData.create("undo-fail", "execution", {"step": step})
            ).is_ok
        real_replace = os.replace
        state = {"publish_failed": False, "undo_calls": 0}

        def faulty_replace(src, dst):
            if ".tmp-" in Path(src).name:
                state["publish_failed"] = True
                raise OSError("simulated promotion failure")
            if state["publish_failed"]:
                state["undo_calls"] += 1
                if state["undo_calls"] == failing_undo_step:
                    raise OSError("simulated undo failure")
            return real_replace(src, dst)

        sixth = CheckpointData.create("undo-fail", "execution", {"step": 6})
        with patch("ouroboros.persistence.checkpoint.os.replace", side_effect=faulty_replace):
            result = checkpoint_store.save(sixth)

        assert result.is_err
        surviving_steps = set()
        for entry in checkpoint_store._base_path.iterdir():
            if entry.name.endswith(".lock"):
                continue
            try:
                surviving_steps.add(json.loads(entry.read_text())["state"]["step"])
            except (OSError, ValueError, KeyError):
                continue
        assert {2, 3, 4, 5} <= surviving_steps, (
            f"failed undo step {failing_undo_step} lost generations: "
            f"only {sorted(surviving_steps)} survive"
        )

    @pytest.mark.parametrize("failing_undo_step", [1, 2, 3])
    def test_retry_after_partial_undo_keeps_full_rollback_history(
        self, checkpoint_store: CheckpointStore, failing_undo_step: int
    ) -> None:
        """A retried save must rebuild the full chain, not discard through holes.

        A partial undo leaves the chain sparse; the next successful save has
        to compact those holes before rotating so that a generation still
        inside the three-level rollback window is never dropped while an
        empty slot remains.
        """
        for step in range(1, 6):
            assert checkpoint_store.save(
                CheckpointData.create("retry-undo", "execution", {"step": step})
            ).is_ok
        real_replace = os.replace
        state = {"publish_failed": False, "undo_calls": 0}

        def faulty_replace(src, dst):
            if ".tmp-" in Path(src).name:
                state["publish_failed"] = True
                raise OSError("simulated promotion failure")
            if state["publish_failed"]:
                state["undo_calls"] += 1
                if state["undo_calls"] == failing_undo_step:
                    raise OSError("simulated undo failure")
            return real_replace(src, dst)

        sixth = CheckpointData.create("retry-undo", "execution", {"step": 6})
        with patch("ouroboros.persistence.checkpoint.os.replace", side_effect=faulty_replace):
            assert checkpoint_store.save(sixth).is_err

        retried = checkpoint_store.save(
            CheckpointData.create("retry-undo", "execution", {"step": 6})
        )
        assert retried.is_ok

        for level, expected_step in ((0, 6), (1, 5), (2, 4), (3, 3)):
            level_path = checkpoint_store._get_checkpoint_path("retry-undo", level)
            assert level_path.exists(), (
                f"undo step {failing_undo_step}: level {level} is a hole after retry"
            )
            actual = json.loads(level_path.read_text())["state"]["step"]
            assert actual == expected_step, (
                f"undo step {failing_undo_step}: level {level} holds generation "
                f"{actual}, expected {expected_step}"
            )

    def test_post_commit_cleanup_failure_still_reports_success(
        self, checkpoint_store: CheckpointStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Promotion is the commit point; later cleanup must not fail the save.

        Once the staged checkpoint is promoted, a failure while unlinking the
        retired oldest checkpoint has to surface as debris, not as an error —
        otherwise callers retry a transaction that already committed.
        """
        for step in range(1, 6):
            assert checkpoint_store.save(
                CheckpointData.create("cleanup-fail", "execution", {"step": step})
            ).is_ok
        real_unlink = Path.unlink

        def failing_retire_unlink(self, *args, **kwargs):
            if ".retire-" in self.name:
                raise OSError("simulated cleanup failure")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr("ouroboros.persistence.checkpoint.Path.unlink", failing_retire_unlink)
        sixth = CheckpointData.create("cleanup-fail", "execution", {"step": 6})
        result = checkpoint_store.save(sixth)

        assert result.is_ok, "a committed save must not be reported as failed"
        loaded = checkpoint_store.load("cleanup-fail")
        assert loaded.is_ok
        assert loaded.value.state == {"step": 6}
        debris = [
            entry.name
            for entry in checkpoint_store._base_path.iterdir()
            if ".retire-" in entry.name
        ]
        assert debris, "expected the retired checkpoint to remain as debris"

    def test_successful_save_rotates_and_leaves_no_temp_files(
        self, checkpoint_store: CheckpointStore
    ) -> None:
        """The staged-write path keeps the existing rotation contract."""
        for step in (1, 2):
            saved = checkpoint_store.save(
                CheckpointData.create("happy-save", "execution", {"step": step})
            )
            assert saved.is_ok

        assert checkpoint_store.load("happy-save").value.state == {"step": 2}
        rollback = checkpoint_store._get_checkpoint_path("happy-save", 1)
        assert json.loads(rollback.read_text())["state"] == {"step": 1}
        litter = [
            name for name in self._files(checkpoint_store) if ".tmp-" in name or ".retire-" in name
        ]
        assert litter == [], f"save left temporary files behind: {litter}"


class TestPeriodicCheckpointer:
    """Test PeriodicCheckpointer background task."""

    async def test_periodic_checkpointer_calls_callback(self) -> None:
        """PeriodicCheckpointer calls callback at regular intervals."""
        call_count = 0
        second_call = asyncio.Event()

        async def callback():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                second_call.set()

        checkpointer = PeriodicCheckpointer(callback, interval=0.1)
        await checkpointer.start()

        try:
            await asyncio.wait_for(second_call.wait(), timeout=2)
        finally:
            await checkpointer.stop()

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
        second_call = asyncio.Event()

        async def failing_callback():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                second_call.set()
            raise ValueError("Test error")

        checkpointer = PeriodicCheckpointer(failing_callback, interval=0.1)
        await checkpointer.start()

        try:
            await asyncio.wait_for(second_call.wait(), timeout=2)
        finally:
            await checkpointer.stop()

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
