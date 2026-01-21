"""Checkpoint and recovery system for workflow persistence.

This module provides:
- CheckpointData: Data model for checkpoint state
- CheckpointStore: Save/load checkpoints with integrity validation
- Recovery logic with rollback support (max 3 levels per NFR11)
- PeriodicCheckpointer: Background task for automatic checkpointing
"""

import asyncio
import fcntl
import hashlib
import json
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from ouroboros.core.errors import PersistenceError
from ouroboros.core.types import Result


@contextmanager
def _file_lock(file_path: Path, exclusive: bool = True) -> Iterator[None]:
    """Context manager for file locking to prevent race conditions.

    Args:
        file_path: Path to the file to lock.
        exclusive: If True, use exclusive lock (for writes).
                   If False, use shared lock (for reads).

    Yields:
        None when lock is acquired.
    """
    lock_path = file_path.with_suffix(file_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as lock_file:
        lock_type = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(lock_file.fileno(), lock_type)
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class CheckpointData:
    """Immutable checkpoint data for workflow state.

    Attributes:
        seed_id: Unique identifier for the seed being executed.
        phase: Current execution phase (e.g., "planning", "execution").
        state: Arbitrary state data as JSON-serializable dict.
        timestamp: UTC timestamp when checkpoint was created.
        hash: SHA-256 hash of serialized data for integrity validation.
    """

    seed_id: str
    phase: str
    state: dict[str, Any]
    timestamp: datetime
    hash: str

    @classmethod
    def create(
        cls, seed_id: str, phase: str, state: dict[str, Any]
    ) -> "CheckpointData":
        """Create a new checkpoint with automatic hash generation.

        Args:
            seed_id: Unique identifier for the seed.
            phase: Current execution phase.
            state: State data to checkpoint.

        Returns:
            New CheckpointData instance with computed hash.
        """
        timestamp = datetime.now(UTC)
        # Create temporary instance without hash to compute it
        temp_data = {
            "seed_id": seed_id,
            "phase": phase,
            "state": state,
            "timestamp": timestamp.isoformat(),
        }
        serialized = json.dumps(temp_data, sort_keys=True)
        hash_value = hashlib.sha256(serialized.encode()).hexdigest()

        return cls(
            seed_id=seed_id,
            phase=phase,
            state=state,
            timestamp=timestamp,
            hash=hash_value,
        )

    def validate_integrity(self) -> Result[bool, str]:
        """Validate checkpoint integrity by recomputing hash.

        Returns:
            Result.ok(True) if hash matches, Result.err with details if corrupted.
        """
        temp_data = {
            "seed_id": self.seed_id,
            "phase": self.phase,
            "state": self.state,
            "timestamp": self.timestamp.isoformat(),
        }
        serialized = json.dumps(temp_data, sort_keys=True)
        computed_hash = hashlib.sha256(serialized.encode()).hexdigest()

        if computed_hash != self.hash:
            return Result.err(
                f"Hash mismatch: expected {self.hash}, got {computed_hash}"
            )
        return Result.ok(True)

    def to_dict(self) -> dict[str, Any]:
        """Convert checkpoint to JSON-serializable dict.

        Returns:
            Dict representation suitable for JSON serialization.
        """
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CheckpointData":
        """Reconstruct checkpoint from dict.

        Args:
            data: Dict with checkpoint data.

        Returns:
            CheckpointData instance.

        Raises:
            ValueError: If timestamp parsing fails.
        """
        timestamp_str = data["timestamp"]
        timestamp = datetime.fromisoformat(timestamp_str)
        return cls(
            seed_id=data["seed_id"],
            phase=data["phase"],
            state=data["state"],
            timestamp=timestamp,
            hash=data["hash"],
        )


class CheckpointStore:
    """Store for persisting and recovering checkpoints with integrity validation.

    Checkpoints are stored as JSON files in ~/.ouroboros/data/checkpoints/.
    Each checkpoint is validated with SHA-256 hash for integrity.
    Supports rollback up to 3 levels (NFR11) when corruption is detected.

    Usage:
        store = CheckpointStore()
        store.initialize()

        # Save checkpoint
        checkpoint = CheckpointData.create("seed-123", "planning", {"step": 1})
        result = store.save(checkpoint)

        # Load latest valid checkpoint with automatic rollback
        result = store.load("seed-123")
        if result.is_ok:
            checkpoint = result.value
    """

    MAX_ROLLBACK_DEPTH = 3

    def __init__(self, base_path: Path | None = None) -> None:
        """Initialize checkpoint store.

        Args:
            base_path: Base directory for checkpoints.
                      Defaults to ~/.ouroboros/data/checkpoints/
        """
        if base_path is None:
            base_path = Path.home() / ".ouroboros" / "data" / "checkpoints"
        self._base_path = base_path

    def initialize(self) -> None:
        """Create checkpoint directory if it doesn't exist.

        This method is idempotent - safe to call multiple times.
        """
        self._base_path.mkdir(parents=True, exist_ok=True)

    def save(self, checkpoint: CheckpointData) -> Result[None, PersistenceError]:
        """Save checkpoint to disk.

        The checkpoint is rotated: existing checkpoints are shifted to .1, .2, .3
        for rollback support (max 3 levels per NFR11).

        Uses file locking to prevent race conditions during concurrent access.

        Args:
            checkpoint: Checkpoint data to save.

        Returns:
            Result.ok(None) on success, Result.err(PersistenceError) on failure.
        """
        try:
            checkpoint_path = self._get_checkpoint_path(checkpoint.seed_id)

            # Use file locking to prevent race conditions
            with _file_lock(checkpoint_path, exclusive=True):
                # Rotate existing checkpoints for rollback support
                self._rotate_checkpoints(checkpoint.seed_id)

                # Write new checkpoint
                with checkpoint_path.open("w") as f:
                    json.dump(checkpoint.to_dict(), f, indent=2)

            return Result.ok(None)
        except Exception as e:
            return Result.err(
                PersistenceError(
                    f"Failed to save checkpoint: {e}",
                    operation="write",
                    details={"seed_id": checkpoint.seed_id, "phase": checkpoint.phase},
                )
            )

    def load(self, seed_id: str) -> Result[CheckpointData, PersistenceError]:
        """Load latest valid checkpoint with automatic rollback on corruption.

        Attempts to load the latest checkpoint. If corrupted (hash mismatch or
        parse error), automatically rolls back to previous checkpoint up to 3 levels.
        Logs corruption details for debugging.

        Args:
            seed_id: Seed identifier to load checkpoint for.

        Returns:
            Result.ok(CheckpointData) with valid checkpoint,
            Result.err(PersistenceError) if no valid checkpoint found.
        """
        # Try loading checkpoints in order: current, .1, .2, .3
        for level in range(self.MAX_ROLLBACK_DEPTH + 1):
            result = self._load_checkpoint_level(seed_id, level)
            if result.is_ok:
                if level > 0:
                    # Log successful recovery after rollback
                    print(
                        f"Recovered checkpoint for {seed_id} "
                        f"from rollback level {level}"
                    )
                return result

            # Log corruption details for debugging
            error = result.error
            print(
                f"Checkpoint corruption at level {level} for {seed_id}: "
                f"{error.message}"
            )

        # No valid checkpoint found at any level
        return Result.err(
            PersistenceError(
                f"No valid checkpoint found for seed {seed_id} "
                f"(tried {self.MAX_ROLLBACK_DEPTH + 1} levels)",
                operation="load",
                details={"seed_id": seed_id},
            )
        )

    def _load_checkpoint_level(
        self, seed_id: str, level: int
    ) -> Result[CheckpointData, PersistenceError]:
        """Load checkpoint at specific rollback level.

        Uses file locking to prevent race conditions during concurrent access.

        Args:
            seed_id: Seed identifier.
            level: Rollback level (0=current, 1-3=previous).

        Returns:
            Result.ok(CheckpointData) if valid, Result.err otherwise.
        """
        checkpoint_path = self._get_checkpoint_path(seed_id, level)

        if not checkpoint_path.exists():
            return Result.err(
                PersistenceError(
                    f"Checkpoint not found at level {level}",
                    operation="read",
                    details={"seed_id": seed_id, "level": level},
                )
            )

        try:
            # Use shared lock for reading
            with _file_lock(checkpoint_path, exclusive=False):
                with checkpoint_path.open("r") as f:
                    data = json.load(f)

            checkpoint = CheckpointData.from_dict(data)

            # Validate integrity
            validation_result = checkpoint.validate_integrity()
            if validation_result.is_err:
                return Result.err(
                    PersistenceError(
                        f"Checkpoint integrity validation failed: "
                        f"{validation_result.error}",
                        operation="validate",
                        details={"seed_id": seed_id, "level": level},
                    )
                )

            return Result.ok(checkpoint)

        except json.JSONDecodeError as e:
            return Result.err(
                PersistenceError(
                    f"Failed to parse checkpoint JSON: {e}",
                    operation="parse",
                    details={"seed_id": seed_id, "level": level},
                )
            )
        except Exception as e:
            return Result.err(
                PersistenceError(
                    f"Failed to load checkpoint: {e}",
                    operation="read",
                    details={"seed_id": seed_id, "level": level},
                )
            )

    def _rotate_checkpoints(self, seed_id: str) -> None:
        """Rotate existing checkpoints for rollback support.

        Shifts checkpoints: current -> .1, .1 -> .2, .2 -> .3
        Oldest checkpoint (.3) is deleted if it exists.

        Args:
            seed_id: Seed identifier for checkpoint rotation.
        """
        # Delete oldest checkpoint (.3) if it exists
        oldest_path = self._get_checkpoint_path(seed_id, self.MAX_ROLLBACK_DEPTH)
        if oldest_path.exists():
            oldest_path.unlink()

        # Shift existing checkpoints
        for level in range(self.MAX_ROLLBACK_DEPTH - 1, -1, -1):
            current_path = self._get_checkpoint_path(seed_id, level)
            if current_path.exists():
                next_path = self._get_checkpoint_path(seed_id, level + 1)
                current_path.rename(next_path)

    def _get_checkpoint_path(self, seed_id: str, level: int = 0) -> Path:
        """Get file path for checkpoint at specific rollback level.

        Args:
            seed_id: Seed identifier.
            level: Rollback level (0=current, 1-3=previous).

        Returns:
            Path to checkpoint file.
        """
        filename = f"checkpoint_{seed_id}.json"
        if level > 0:
            filename = f"checkpoint_{seed_id}.json.{level}"
        return self._base_path / filename


class PeriodicCheckpointer:
    """Background task for automatic periodic checkpointing.

    Runs a background asyncio task that calls a checkpoint callback
    at regular intervals (default 5 minutes per AC2).

    Usage:
        async def checkpoint_callback():
            # Get current state and save checkpoint
            checkpoint = CheckpointData.create("seed-123", "planning", state)
            store.save(checkpoint)

        checkpointer = PeriodicCheckpointer(checkpoint_callback, interval=300)
        await checkpointer.start()

        # Later, when done
        await checkpointer.stop()
    """

    def __init__(
        self,
        checkpoint_callback: Callable[[], Awaitable[None]],
        interval: int = 300,  # 5 minutes default
    ) -> None:
        """Initialize periodic checkpointer.

        Args:
            checkpoint_callback: Async function to call for checkpointing.
            interval: Interval in seconds between checkpoints (default 300 = 5 min).
        """
        self._callback = checkpoint_callback
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Start the periodic checkpointing background task.

        This method is idempotent - calling it multiple times is safe.
        """
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the periodic checkpointing background task.

        Waits for the current checkpoint to complete before stopping.
        """
        if self._task is not None and not self._task.done():
            self._stop_event.set()
            await self._task
            self._task = None

    async def _run(self) -> None:
        """Internal background task loop."""
        while not self._stop_event.is_set():
            try:
                # Wait for interval or stop event
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval
                )
                # If we get here, stop event was set
                break
            except asyncio.TimeoutError:
                # Timeout means it's time to checkpoint
                try:
                    await self._callback()
                except Exception as e:
                    # Log error but continue checkpointing
                    print(f"Periodic checkpoint failed: {e}")


class RecoveryManager:
    """Manager for workflow recovery on startup.

    Handles loading the latest valid checkpoint and restoring workflow state.
    Provides recovery status and logging for debugging.

    Usage:
        store = CheckpointStore()
        manager = RecoveryManager(store)

        result = await manager.recover("seed-123")
        if result.is_ok:
            checkpoint = result.value
            # Restore workflow state from checkpoint
    """

    def __init__(self, checkpoint_store: CheckpointStore) -> None:
        """Initialize recovery manager.

        Args:
            checkpoint_store: CheckpointStore instance for loading checkpoints.
        """
        self._store = checkpoint_store

    async def recover(
        self, seed_id: str
    ) -> Result[CheckpointData | None, PersistenceError]:
        """Recover workflow state from latest valid checkpoint.

        Attempts to load the latest checkpoint. If not found or corrupted,
        uses automatic rollback. Returns None if no checkpoint exists
        (normal for first run).

        Args:
            seed_id: Seed identifier to recover.

        Returns:
            Result.ok(CheckpointData) if checkpoint loaded,
            Result.ok(None) if no checkpoint exists (normal),
            Result.err(PersistenceError) if recovery failed after rollback.
        """
        result = self._store.load(seed_id)

        if result.is_err:
            error = result.error
            # Check if error is due to no checkpoint (normal for first run)
            # Match both "not found" and "no valid checkpoint found"
            error_msg_lower = error.message.lower()
            if "not found" in error_msg_lower or "no valid checkpoint found" in error_msg_lower:
                print(f"No checkpoint found for {seed_id} - starting fresh")
                return Result.ok(None)

            # Other errors indicate corruption/recovery failure
            print(f"Recovery failed for {seed_id}: {error.message}")
            return Result.err(error)

        checkpoint = result.value
        print(
            f"Recovered checkpoint for {seed_id} "
            f"from phase '{checkpoint.phase}' "
            f"at {checkpoint.timestamp.isoformat()}"
        )
        return Result.ok(checkpoint)
