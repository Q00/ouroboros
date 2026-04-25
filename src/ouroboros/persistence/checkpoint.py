"""Checkpoint and recovery system for workflow persistence.

This module provides:
- CheckpointData: Data model for checkpoint state
- CheckpointStore: Save/load/delete checkpoints with integrity validation
- CompoundingCheckpointState: Typed payload for serial-compounding mode checkpoints
- Recovery logic with rollback support (max 3 levels per NFR11)
- PeriodicCheckpointer: Background task for automatic checkpointing

Compounding-mode checkpoint payload schema
------------------------------------------
When used by ``SerialCompoundingExecutor``, the ``CheckpointData.state`` dict
carries exactly these keys::

    {
        "last_completed_ac_index": int,    # 0-based index of last successful AC
        "postmortem_chain": list[dict],    # serialize_postmortem_chain() output
        "mode": "compounding",             # literal sentinel
    }

Use :class:`CompoundingCheckpointState` to create/validate this payload without
relying on raw dict access.

[[INVARIANT: CompoundingCheckpointState.mode is always the literal "compounding"]]
[[INVARIANT: CheckpointStore.delete removes all rollback levels for a seed_id]]
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from ouroboros.core.errors import PersistenceError
from ouroboros.core.file_lock import file_lock as _file_lock
from ouroboros.core.types import Result


# ---------------------------------------------------------------------------
# Compounding-mode typed payload
# ---------------------------------------------------------------------------


@dataclass
class CompoundingCheckpointState:
    """Typed payload for serial-compounding mode per-AC checkpoints.

    This is stored verbatim in ``CheckpointData.state`` and can round-trip
    through JSON serialization / deserialization.

    Attributes:
        last_completed_ac_index: 0-based index of the last AC that completed
            successfully.  The *next* AC to run is ``last_completed_ac_index + 1``.
        postmortem_chain: The serialized :class:`PostmortemChain` as returned by
            :func:`~ouroboros.orchestrator.level_context.serialize_postmortem_chain`.
            Stored as a list of dicts so no import cycle is introduced here.
        mode: Literal sentinel ``"compounding"`` — asserts which executor wrote
            the checkpoint.
        partial_failing_ac_index: When set, the 0-based index of a decomposed AC
            that failed after some of its sub-ACs completed.  Used by the sub-
            postmortem resume path to identify which AC to resume and at which
            sub-AC boundary.  ``None`` when no partial sub-AC state exists.
        partial_failing_ac_sub_postmortems: Serialized sub-postmortems for the
            completed sub-ACs of the partially-failed AC indicated by
            ``partial_failing_ac_index``.  Each entry is a dict produced by
            :func:`~ouroboros.orchestrator.level_context.serialize_postmortem_chain`
            for a single sub-AC postmortem.  ``None`` when not applicable.

    [[INVARIANT: CompoundingCheckpointState.mode is always the literal "compounding"]]
    [[INVARIANT: partial_failing_ac_index is set only for decomposed ACs with sub_results]]
    """

    last_completed_ac_index: int
    postmortem_chain: list[dict[str, Any]]
    # Optional: partial sub-AC progress for a failing decomposed AC.
    # Both fields are always set together (both None or both non-None).
    partial_failing_ac_index: int | None = None
    partial_failing_ac_sub_postmortems: list[dict[str, Any]] | None = None
    mode: str = field(default="compounding", init=False)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict suitable for ``CheckpointData.state``."""
        d: dict[str, Any] = {
            "last_completed_ac_index": self.last_completed_ac_index,
            "postmortem_chain": self.postmortem_chain,
            "mode": self.mode,
        }
        if self.partial_failing_ac_index is not None:
            d["partial_failing_ac_index"] = self.partial_failing_ac_index
            d["partial_failing_ac_sub_postmortems"] = (
                self.partial_failing_ac_sub_postmortems or []
            )
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CompoundingCheckpointState":
        """Reconstruct from a dict stored in ``CheckpointData.state``.

        Args:
            data: Dict with compounding state keys.

        Returns:
            ``CompoundingCheckpointState`` instance.

        Raises:
            ValueError: If required keys are absent or ``mode`` is not
                ``"compounding"``.
        """
        if data.get("mode") != "compounding":
            raise ValueError(
                f"Expected mode='compounding', got mode={data.get('mode')!r}"
            )
        if "last_completed_ac_index" not in data:
            raise ValueError("Missing required key 'last_completed_ac_index'")
        # Parse optional partial sub-AC fields.
        partial_ac_index: int | None = None
        partial_sub_pms: list[dict[str, Any]] | None = None
        if "partial_failing_ac_index" in data and data["partial_failing_ac_index"] is not None:
            partial_ac_index = int(data["partial_failing_ac_index"])
            partial_sub_pms = list(data.get("partial_failing_ac_sub_postmortems") or [])
        obj = cls(
            last_completed_ac_index=int(data["last_completed_ac_index"]),
            postmortem_chain=list(data.get("postmortem_chain") or []),
            partial_failing_ac_index=partial_ac_index,
            partial_failing_ac_sub_postmortems=partial_sub_pms,
        )
        return obj


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
    def create(cls, seed_id: str, phase: str, state: dict[str, Any]) -> CheckpointData:
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
            return Result.err(f"Hash mismatch: expected {self.hash}, got {computed_hash}")
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
    def from_dict(cls, data: dict[str, Any]) -> CheckpointData:
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

    # Filename components used by _get_checkpoint_path:
    #   "checkpoint_" + <seed> + ".json"         (level 0)
    #   "checkpoint_" + <seed> + ".json.N"       (level 1-3)
    # file_lock appends ".lock", so worst-case basename is:
    #   "checkpoint_" + <seed> + ".json.N.lock"
    _FILENAME_PREFIX = "checkpoint_"
    _FILENAME_SUFFIX_WORST = ".json.0.lock"  # longest possible basename suffix
    _MAX_SEED_LEN = 255 - len(_FILENAME_PREFIX) - len(_FILENAME_SUFFIX_WORST)  # 232
    # When truncating, reserve space for a collision-resistant hash suffix.
    _HASH_SUFFIX_LEN = 8  # hex chars from SHA-256

    @staticmethod
    def _sanitize_seed_id(seed_id: str, *, max_len: int | None = None) -> str:
        """Sanitize seed_id to prevent path traversal attacks.

        Strips null bytes, removes path separators and parent-directory
        sequences, and caps length so that the **full** checkpoint
        filename (prefix + seed + suffix) stays within the 255-byte
        filesystem limit.  When truncation is needed a SHA-256 hash
        fragment is appended to keep the mapping collision-resistant.

        Args:
            seed_id: Raw seed identifier.
            max_len: Override for maximum sanitized length (used in tests).

        Returns:
            Sanitized seed identifier safe for use in filenames.

        Raises:
            ValueError: If seed_id is empty or becomes empty after sanitization.
        """
        if not seed_id:
            raise ValueError("seed_id must not be empty")

        budget = max_len if max_len is not None else CheckpointStore._MAX_SEED_LEN

        # Strip null bytes
        sanitized = seed_id.replace("\x00", "")

        # Remove parent-directory traversal sequences before stripping separators
        # so that inputs like "x/../../PWNED" are neutralised.
        sanitized = sanitized.replace("..", "")

        # Replace path separators with underscores
        sanitized = re.sub(r"[/\\]", "_", sanitized)

        # Cap length to the remaining filename budget.
        # When truncation is required, append a hash suffix for collision resistance.
        if len(sanitized) > budget:
            hash_hex = hashlib.sha256(sanitized.encode()).hexdigest()[
                : CheckpointStore._HASH_SUFFIX_LEN
            ]
            truncated_len = budget - CheckpointStore._HASH_SUFFIX_LEN - 1  # 1 for "_"
            sanitized = f"{sanitized[:truncated_len]}_{hash_hex}"

        # Final check: must still be non-empty after sanitization
        if not sanitized or not sanitized.strip():
            raise ValueError(f"seed_id is empty after sanitization (original: {seed_id!r})")

        return sanitized

    def _validate_path_containment(self, path: Path) -> None:
        """Verify that *path* is inside the checkpoint base directory.

        Args:
            path: Resolved path to validate.

        Raises:
            ValueError: If path escapes the base directory.
        """
        resolved = path.resolve()
        base_resolved = self._base_path.resolve()
        # Use os.path so the check works on all platforms.
        if not str(resolved).startswith(str(base_resolved) + os.sep) and resolved != base_resolved:
            raise ValueError(f"Path traversal detected: {resolved} is outside {base_resolved}")

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
                    print(f"Recovered checkpoint for {seed_id} from rollback level {level}")
                return result

            # Log corruption details for debugging
            error = result.error
            print(f"Checkpoint corruption at level {level} for {seed_id}: {error.message}")

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
                        f"Checkpoint integrity validation failed: {validation_result.error}",
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
                os.replace(current_path, next_path)

    def delete(self, seed_id: str) -> Result[None, PersistenceError]:
        """Delete all checkpoint files for *seed_id*, including rollback levels.

        After a successful delete, :meth:`load` for the same seed_id will
        return an error.  This method is idempotent — calling it when no
        checkpoint exists returns :meth:`~ouroboros.core.types.Result.ok`.

        Args:
            seed_id: Seed identifier whose checkpoints should be removed.

        Returns:
            ``Result.ok(None)`` on success, ``Result.err(PersistenceError)``
            if a file exists but cannot be deleted.

        [[INVARIANT: CheckpointStore.delete removes all rollback levels for a seed_id]]
        """
        try:
            for level in range(self.MAX_ROLLBACK_DEPTH + 1):
                path = self._get_checkpoint_path(seed_id, level)
                if path.exists():
                    path.unlink()
            return Result.ok(None)
        except Exception as e:
            return Result.err(
                PersistenceError(
                    f"Failed to delete checkpoint: {e}",
                    operation="delete",
                    details={"seed_id": seed_id},
                )
            )

    # ------------------------------------------------------------------
    # Convenience aliases for callers that prefer write / read semantics.
    # The underlying implementation is identical to save / load.
    # ------------------------------------------------------------------

    def write(self, checkpoint: CheckpointData) -> Result[None, PersistenceError]:
        """Alias for :meth:`save` — write checkpoint to disk.

        Args:
            checkpoint: Checkpoint data to persist.

        Returns:
            ``Result.ok(None)`` on success.
        """
        return self.save(checkpoint)

    def read(self, seed_id: str) -> Result[CheckpointData, PersistenceError]:
        """Alias for :meth:`load` — read latest valid checkpoint from disk.

        Args:
            seed_id: Seed identifier to read checkpoint for.

        Returns:
            ``Result.ok(CheckpointData)`` on success.
        """
        return self.load(seed_id)

    def _get_checkpoint_path(self, seed_id: str, level: int = 0) -> Path:
        """Get file path for checkpoint at specific rollback level.

        The seed_id is sanitized to prevent path traversal, and the
        resulting path is validated to stay within the base directory.

        Args:
            seed_id: Seed identifier.
            level: Rollback level (0=current, 1-3=previous).

        Returns:
            Path to checkpoint file.

        Raises:
            ValueError: If seed_id is invalid or path escapes base directory.
        """
        safe_id = self._sanitize_seed_id(seed_id)
        filename = f"checkpoint_{safe_id}.json"
        if level > 0:
            filename = f"checkpoint_{safe_id}.json.{level}"
        path = self._base_path / filename
        self._validate_path_containment(path)
        return path


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
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                # If we get here, stop event was set
                break
            except TimeoutError:
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

    async def recover(self, seed_id: str) -> Result[CheckpointData | None, PersistenceError]:
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
