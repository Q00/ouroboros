"""State Store - JSON file-based storage with atomic writes.

This module provides the storage backend for state persistence:
- Atomic writes using file rename pattern
- Schema migration support
- Backup/restore functionality
- Thread-safe operations

State files are stored in .omc/state/ with the following structure:
- {mode}-state.json - Current mode state (autopilot, ralph, ultrawork, etc.)
- checkpoints/{checkpoint_id}.json - Checkpoint snapshots
- backups/{timestamp}-{mode}-state.json.bak - Automatic backups
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
import fcntl
import json
import os
from pathlib import Path
import shutil
from threading import RLock
from typing import Any

from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)


class AtomicWriteError(Exception):
    """Raised when atomic write operation fails."""


class StateMode(Enum):
    """Execution modes with persistent state."""

    AUTOPILOT = "autopilot"
    RALPH = "ralph"
    ULTRAWORK = "ultrawork"
    ULTRAPILOT = "ultrapilot"
    ECOMODE = "ecomode"
    SWARM = "swarm"
    PIPELINE = "pipeline"
    TEAM = "team"
    ULTRAQA = "ultraqa"


class SchemaMigration:
    """Schema migration handler for state files.

    Handles version migration between different schema versions.
    """

    CURRENT_VERSION = "1.0.0"

    # Migration registry: version -> migration function
    MIGRATIONS: dict[str, dict[str, Any]] = {
        "0.9.0": {
            "target": "1.0.0",
            "migrate": lambda data: {
                **data,
                "version": "1.0.0",
                "schema_compatible": True,
                "migration_timestamp": datetime.now(UTC).isoformat(),
            },
        },
    }

    @classmethod
    def migrate(cls, data: dict[str, Any], from_version: str) -> dict[str, Any]:
        """Migrate state data to current version.

        Args:
            data: State data to migrate.
            from_version: Current version of the data.

        Returns:
            Migrated state data at current version.
        """
        current_version = from_version

        while current_version != cls.CURRENT_VERSION:
            if current_version not in cls.MIGRATIONS:
                log.warning(
                    "state.store.migration.not_found",
                    from_version=current_version,
                    current_version=cls.CURRENT_VERSION,
                )
                # Add compatibility flag for unknown versions
                data["schema_compatible"] = False
                data["version"] = cls.CURRENT_VERSION
                break

            migration = cls.MIGRATIONS[current_version]
            data = migration["migrate"](data)
            current_version = migration["target"]

            log.info(
                "state.store.migrated",
                from_version=from_version,
                to_version=current_version,
            )

        return data


class StateStore:
    """JSON file-based state store with atomic writes.

    Provides thread-safe, atomic persistence for state data.
    Automatic backups are created before writes.

    Example:
        store = StateStore(worktree="/path/to/project")

        # Write state
        await store.write_mode_state(StateMode.AUTOPILOT, {"tasks": [...]})

        # Read state
        state = await store.read_mode_state(StateMode.AUTOPILOT)

        # Create checkpoint
        checkpoint_id = await store.create_checkpoint({"session_id": "..."})
    """

    STATE_DIR_NAME = ".omc/state"
    CHECKPOINT_DIR_NAME = "checkpoints"
    BACKUP_DIR_NAME = "backups"
    MAX_BACKUPS = 10

    def __init__(self, worktree: str | Path) -> None:
        """Initialize the state store.

        Args:
            worktree: Path to the git worktree root.
        """
        self._worktree = Path(worktree)
        self._state_dir = self._worktree / self.STATE_DIR_NAME
        self._checkpoint_dir = self._state_dir / self.CHECKPOINT_DIR_NAME
        self._backup_dir = self._state_dir / self.BACKUP_DIR_NAME
        self._lock = RLock()

        # Ensure directories exist
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        """Create state directories if they don't exist."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoint_dir.mkdir(exist_ok=True)
        self._backup_dir.mkdir(exist_ok=True)

    @property
    def state_dir(self) -> Path:
        """Get the state directory path."""
        return self._state_dir

    def get_mode_state_path(self, mode: StateMode) -> Path:
        """Get the state file path for a mode.

        Args:
            mode: The execution mode.

        Returns:
            Path to the mode's state file.
        """
        return self._state_dir / f"{mode.value}-state.json"

    def get_checkpoint_path(self, checkpoint_id: str) -> Path:
        """Get the checkpoint file path.

        Args:
            checkpoint_id: Unique checkpoint identifier.

        Returns:
            Path to the checkpoint file.
        """
        return self._checkpoint_dir / f"{checkpoint_id}.json"

    async def read_mode_state(self, mode: StateMode) -> dict[str, Any] | None:
        """Read state for a specific mode.

        Args:
            mode: The execution mode to read.

        Returns:
            State dictionary or None if file doesn't exist.
        """
        path = self.get_mode_state_path(mode)

        if not path.exists():
            return None

        try:
            with open(path, encoding="utf-8") as f:
                # Acquire shared read lock
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    loaded = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            # Type assertion: json.load returns dict for state files
            data: dict[str, Any] = loaded if isinstance(loaded, dict) else {}

            # Check and migrate schema version
            version = data.get("version", "0.9.0")
            if version != SchemaMigration.CURRENT_VERSION:
                data = SchemaMigration.migrate(data, version)
                # Write migrated data back
                await self.write_mode_state(mode, data)

            log.debug("state.store.read", mode=mode.value, path=str(path))
            return data

        except json.JSONDecodeError as e:
            log.error(
                "state.store.read.json_error",
                mode=mode.value,
                path=str(path),
                error=str(e),
            )
            # Try to restore from backup
            return await self._restore_from_backup(mode)
        except Exception as e:
            log.error(
                "state.store.read.error",
                mode=mode.value,
                path=str(path),
                error=str(e),
            )
            return None

    async def write_mode_state(
        self,
        mode: StateMode,
        data: dict[str, Any],
        create_backup: bool = True,
    ) -> Result[Path, AtomicWriteError]:
        """Write state for a specific mode atomically.

        Args:
            mode: The execution mode to write.
            data: State data to write.
            create_backup: Whether to create backup before writing.

        Returns:
            Result containing the written file path or an error.
        """
        path = self.get_mode_state_path(mode)

        # Ensure version is set
        if "version" not in data:
            data = {**data, "version": SchemaMigration.CURRENT_VERSION}

        # Add timestamp
        data = {
            **data,
            "last_modified": datetime.now(UTC).isoformat(),
        }

        with self._lock:
            try:
                # Create backup if requested
                if create_backup and path.exists():
                    await self._create_backup(mode)

                # Atomic write: write to temp file, then rename
                temp_path = path.with_suffix(".tmp")
                with open(temp_path, "w", encoding="utf-8") as f:
                    # Acquire exclusive write lock
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                        f.flush()
                        os.fsync(f.fileno())
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

                # Atomic rename
                temp_path.rename(path)

                log.debug("state.store.wrote", mode=mode.value, path=str(path))
                return Result.ok(path)

            except Exception as e:
                log.error(
                    "state.store.write.error",
                    mode=mode.value,
                    path=str(path),
                    error=str(e),
                )
                return Result.err(AtomicWriteError(f"Failed to write state: {e}"))

    async def delete_mode_state(self, mode: StateMode) -> Result[bool, str]:
        """Delete state for a specific mode.

        Args:
            mode: The execution mode to delete.

        Returns:
            Result indicating success or error message.
        """
        path = self.get_mode_state_path(mode)

        if not path.exists():
            return Result.ok(False)

        try:
            # Create backup before deleting
            await self._create_backup(mode)

            path.unlink()
            log.info("state.store.deleted", mode=mode.value, path=str(path))
            return Result.ok(True)

        except Exception as e:
            error_msg = f"Failed to delete state: {e}"
            log.error("state.store.delete.error", mode=mode.value, error=error_msg)
            return Result.err(error_msg)

    async def create_checkpoint(
        self,
        checkpoint_data: dict[str, Any],
        checkpoint_id: str | None = None,
    ) -> Result[str, str]:
        """Create a checkpoint snapshot.

        Args:
            checkpoint_data: Data to include in the checkpoint.
            checkpoint_id: Optional checkpoint ID. Auto-generated if None.

        Returns:
            Result containing checkpoint ID or error message.
        """
        import uuid

        if checkpoint_id is None:
            checkpoint_id = f"checkpoint_{uuid.uuid4().hex[:12]}"

        path = self.get_checkpoint_path(checkpoint_id)

        # Add checkpoint metadata
        data = {
            "checkpoint_id": checkpoint_id,
            "created_at": datetime.now(UTC).isoformat(),
            "version": SchemaMigration.CURRENT_VERSION,
            **checkpoint_data,
        }

        try:
            # Atomic write to checkpoint file
            temp_path = path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            temp_path.rename(path)

            log.info("state.store.checkpoint_created", checkpoint_id=checkpoint_id)
            return Result.ok(checkpoint_id)

        except Exception as e:
            error_msg = f"Failed to create checkpoint: {e}"
            log.error("state.store.checkpoint.error", error=error_msg)
            return Result.err(error_msg)

    async def load_checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        """Load a checkpoint snapshot.

        Args:
            checkpoint_id: Checkpoint identifier.

        Returns:
            Checkpoint data or None if not found.
        """
        path = self.get_checkpoint_path(checkpoint_id)

        if not path.exists():
            log.warning("state.store.checkpoint.not_found", checkpoint_id=checkpoint_id)
            return None

        try:
            with open(path, encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    loaded = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            # Type assertion: json.load returns dict for checkpoint files
            data: dict[str, Any] = loaded if isinstance(loaded, dict) else {}

            log.debug("state.store.checkpoint.loaded", checkpoint_id=checkpoint_id)
            return data

        except Exception as e:
            log.error(
                "state.store.checkpoint.load_error",
                checkpoint_id=checkpoint_id,
                error=str(e),
            )
            return None

    async def list_checkpoints(self) -> list[dict[str, Any]]:
        """List all available checkpoints.

        Returns:
            List of checkpoint metadata dicts.
        """
        checkpoints = []

        for path in self._checkpoint_dir.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                    checkpoints.append(
                        {
                            "checkpoint_id": data.get("checkpoint_id", path.stem),
                            "session_id": data.get("session_id"),
                            "phase": data.get("phase"),
                            "created_at": data.get("created_at"),
                            "path": str(path),
                        }
                    )
            except Exception:
                # Skip invalid checkpoint files
                continue

        # Sort by creation time (newest first)
        checkpoints.sort(
            key=lambda c: str(c.get("created_at", "")),
            reverse=True,
        )

        return checkpoints

    async def delete_checkpoint(self, checkpoint_id: str) -> Result[bool, str]:
        """Delete a checkpoint.

        Args:
            checkpoint_id: Checkpoint identifier.

        Returns:
            Result indicating success or error message.
        """
        path = self.get_checkpoint_path(checkpoint_id)

        if not path.exists():
            return Result.ok(False)

        try:
            path.unlink()
            log.info("state.store.checkpoint.deleted", checkpoint_id=checkpoint_id)
            return Result.ok(True)

        except Exception as e:
            error_msg = f"Failed to delete checkpoint: {e}"
            return Result.err(error_msg)

    async def _create_backup(self, mode: StateMode) -> Result[Path, str]:
        """Create a backup of mode state.

        Args:
            mode: The execution mode to backup.

        Returns:
            Result containing backup path or error message.
        """
        source_path = self.get_mode_state_path(mode)

        if not source_path.exists():
            return Result.ok(Path(""))  # No backup needed

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        backup_name = f"{timestamp}-{mode.value}-state.json.bak"
        backup_path = self._backup_dir / backup_name

        try:
            shutil.copy2(source_path, backup_path)

            # Clean up old backups
            await self._cleanup_old_backups()

            log.debug("state.store.backup.created", mode=mode.value, path=str(backup_path))
            return Result.ok(backup_path)

        except Exception as e:
            error_msg = f"Failed to create backup: {e}"
            log.warning("state.store.backup.error", error=error_msg)
            return Result.err(error_msg)

    async def _restore_from_backup(self, mode: StateMode) -> dict[str, Any] | None:
        """Attempt to restore state from backup.

        Args:
            mode: The execution mode to restore.

        Returns:
            Restored state data or None if no valid backup found.
        """
        # Find the most recent backup for this mode
        pattern = f"*-{mode.value}-state.json.bak"
        backups = sorted(self._backup_dir.glob(pattern), reverse=True)

        for backup_path in backups:
            try:
                with open(backup_path, encoding="utf-8") as f:
                    loaded = json.load(f)

                # Type assertion: json.load returns dict for backup files
                data: dict[str, Any] = loaded if isinstance(loaded, dict) else {}

                log.info(
                    "state.store.backup.restored",
                    mode=mode.value,
                    backup=str(backup_path),
                )

                # Restore to main state file
                await self.write_mode_state(mode, data, create_backup=False)
                return data

            except Exception:
                # Try next backup
                continue

        log.warning("state.store.backup.none_valid", mode=mode.value)
        return None

    async def _cleanup_old_backups(self) -> None:
        """Remove old backups, keeping only MAX_BACKUPS most recent."""
        backups = list(self._backup_dir.glob("*.bak"))

        if len(backups) <= self.MAX_BACKUPS:
            return

        # Sort by modification time (oldest first)
        backups.sort(key=lambda p: p.stat().st_mtime)

        # Remove oldest backups
        for old_backup in backups[: len(backups) - self.MAX_BACKUPS]:
            try:
                old_backup.unlink()
            except Exception as e:
                log.warning("state.store.backup.cleanup_failed", path=str(old_backup), error=str(e))


def load_state_store(worktree: str | Path | None = None) -> StateStore:
    """Load or create the state store for a worktree.

    Args:
        worktree: Path to git worktree. Defaults to current directory.

    Returns:
        StateStore instance for the worktree.
    """
    if worktree is None:
        worktree = Path.cwd()

    return StateStore(worktree)
