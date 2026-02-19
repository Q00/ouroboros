"""State Manager - Session state persistence and recovery.

This module provides the high-level state management interface:
- Session state persistence across /clear
- Task state tracking
- Mode state (Autopilot/Ralph/Ultrawork/etc.)
- Checkpoint and recovery operations

Integrates with existing WorkflowState and event system.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

import structlog

from ouroboros.core.types import Result
from ouroboros.orchestrator.workflow_state import (
    AcceptanceCriterion,
    ACStatus,
    ActivityType,
    WorkflowState,
)
from ouroboros.plugin.state.store import StateMode, StateStore

log = structlog.get_logger()


class SessionStatus(Enum):
    """Status of a session."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    RESUMED = "resumed"


@dataclass
class SessionState:
    """Complete session state for persistence.

    Attributes:
        session_id: Unique session identifier.
        execution_id: Associated execution ID.
        seed_id: ID of the seed being executed.
        seed_goal: Goal from the seed.
        acceptance_criteria: List of acceptance criteria.
        workflow_state: Current workflow state.
        mode: Current execution mode.
        status: Session status.
        created_at: When session was created.
        updated_at: When session was last updated.
        metadata: Additional metadata.
    """

    session_id: str
    execution_id: str
    seed_id: str
    seed_goal: str
    acceptance_criteria: list[str]
    workflow_state: dict[str, Any]
    mode: StateMode
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization.

        Returns:
            Dictionary representation of session state.
        """
        return {
            "session_id": self.session_id,
            "execution_id": self.execution_id,
            "seed_id": self.seed_id,
            "seed_goal": self.seed_goal,
            "acceptance_criteria": self.acceptance_criteria,
            "workflow_state": self.workflow_state,
            "mode": self.mode.value,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        """Create SessionState from dictionary.

        Args:
            data: Dictionary representation of session state.

        Returns:
            SessionState instance.
        """
        created_at = (
            datetime.fromisoformat(data["created_at"])
            if data.get("created_at")
            else datetime.now(UTC)
        )
        updated_at = (
            datetime.fromisoformat(data["updated_at"])
            if data.get("updated_at")
            else datetime.now(UTC)
        )

        return cls(
            session_id=data["session_id"],
            execution_id=data["execution_id"],
            seed_id=data["seed_id"],
            seed_goal=data["seed_goal"],
            acceptance_criteria=data.get("acceptance_criteria", []),
            workflow_state=data.get("workflow_state", {}),
            mode=StateMode(data.get("mode", StateMode.AUTOPILOT.value)),
            status=SessionStatus(data.get("status", SessionStatus.ACTIVE.value)),
            created_at=created_at,
            updated_at=updated_at,
            metadata=data.get("metadata", {}),
        )


@dataclass
class CheckpointData:
    """Data for creating a checkpoint.

    Attributes:
        checkpoint_id: Unique checkpoint identifier.
        session_id: Associated session ID.
        phase: Current execution phase.
        state: Complete state snapshot.
        created_at: When checkpoint was created.
    """

    checkpoint_id: str
    session_id: str
    phase: str
    state: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization.

        Returns:
            Dictionary representation of checkpoint data.
        """
        return {
            "checkpoint_id": self.checkpoint_id,
            "session_id": self.session_id,
            "phase": self.phase,
            "state": self.state,
            "created_at": self.created_at.isoformat(),
        }


class StateManager:
    """Manages session state persistence and recovery.

    Provides high-level interface for:
    - Saving/loading session state
    - Creating and restoring checkpoints
    - Tracking mode state
    - Automatic checkpoint creation

    Example:
        store = StateStore(worktree="/path/to/project")
        manager = StateManager(store)

        # Save session state
        await manager.save_session(
            session_id="sess-123",
            execution_id="exec-456",
            seed_id="seed-789",
            seed_goal="Build a CLI tool",
            acceptance_criteria=["AC1", "AC2"],
            workflow_state=tracker.state.to_dict(),
            mode=StateMode.AUTOPILOT,
        )

        # Load session state
        session = await manager.load_session("sess-123")

        # Create checkpoint
        checkpoint_id = await manager.create_checkpoint(
            session_id="sess-123",
            phase="execution",
            state={"tasks": [...]},
        )
    """

    # Auto-checkpoint interval (seconds)
    CHECKPOINT_INTERVAL = 300  # 5 minutes

    def __init__(
        self,
        store: StateStore,
        auto_checkpoint: bool = True,
    ) -> None:
        """Initialize the state manager.

        Args:
            store: StateStore instance for persistence.
            auto_checkpoint: Whether to auto-create checkpoints.
        """
        self._store = store
        self._auto_checkpoint = auto_checkpoint
        self._checkpoint_task: asyncio.Task[None] | None = None
        self._session_cache: dict[str, SessionState] = {}

    async def save_session(
        self,
        session_id: str,
        execution_id: str,
        seed_id: str,
        seed_goal: str,
        acceptance_criteria: list[str],
        workflow_state: dict[str, Any],
        mode: StateMode,
        status: SessionStatus = SessionStatus.ACTIVE,
        metadata: dict[str, Any] | None = None,
    ) -> Result[str, str]:
        """Save session state.

        Args:
            session_id: Unique session identifier.
            execution_id: Associated execution ID.
            seed_id: ID of the seed being executed.
            seed_goal: Goal from the seed.
            acceptance_criteria: List of acceptance criteria.
            workflow_state: Current workflow state.
            mode: Current execution mode.
            status: Session status.
            metadata: Additional metadata.

        Returns:
            Result containing session ID or error message.
        """
        session = SessionState(
            session_id=session_id,
            execution_id=execution_id,
            seed_id=seed_id,
            seed_goal=seed_goal,
            acceptance_criteria=acceptance_criteria,
            workflow_state=workflow_state,
            mode=mode,
            status=status,
            metadata=metadata or {},
        )

        # Update cache
        self._session_cache[session_id] = session

        # Save to mode state file
        state_data = {
            "sessions": {session_id: session.to_dict()},
            "active_session": session_id,
        }

        result = await self._store.write_mode_state(mode, state_data)

        if result.is_ok:
            log.info(
                "state.manager.session_saved",
                session_id=session_id,
                mode=mode.value,
            )
            return Result.ok(session_id)
        else:
            return Result.err(str(result.error))

    async def load_session(self, session_id: str) -> SessionState | None:
        """Load session state.

        Args:
            session_id: Session identifier to load.

        Returns:
            SessionState if found, None otherwise.
        """
        # Check cache first
        if session_id in self._session_cache:
            return self._session_cache[session_id]

        # Try to load from each mode's state file
        for mode in StateMode:
            mode_data = await self._store.read_mode_state(mode)
            if mode_data and "sessions" in mode_data:
                sessions = mode_data["sessions"]
                if session_id in sessions:
                    session = SessionState.from_dict(sessions[session_id])
                    # Update cache
                    self._session_cache[session_id] = session
                    return session

        log.warning("state.manager.session_not_found", session_id=session_id)
        return None

    async def update_session(
        self,
        session_id: str,
        workflow_state: dict[str, Any] | None = None,
        status: SessionStatus | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Result[SessionState, str]:
        """Update existing session state.

        Args:
            session_id: Session identifier to update.
            workflow_state: New workflow state (optional).
            status: New status (optional).
            metadata: New metadata to merge (optional).

        Returns:
            Result containing updated SessionState or error message.
        """
        session = await self.load_session(session_id)

        if session is None:
            return Result.err(f"Session {session_id} not found")

        # Update fields
        if workflow_state is not None:
            session.workflow_state = workflow_state

        if status is not None:
            session.status = status

        if metadata is not None:
            session.metadata = {**session.metadata, **metadata}

        session.updated_at = datetime.now(UTC)

        # Save updated session
        result = await self.save_session(
            session_id=session.session_id,
            execution_id=session.execution_id,
            seed_id=session.seed_id,
            seed_goal=session.seed_goal,
            acceptance_criteria=session.acceptance_criteria,
            workflow_state=session.workflow_state,
            mode=session.mode,
            status=session.status,
            metadata=session.metadata,
        )

        if result.is_ok:
            return Result.ok(session)
        else:
            return Result.err(result.error)

    async def delete_session(self, session_id: str) -> Result[bool, str]:
        """Delete session state.

        Args:
            session_id: Session identifier to delete.

        Returns:
            Result indicating success or error message.
        """
        session = await self.load_session(session_id)

        if session is None:
            return Result.ok(False)  # Already deleted

        # Remove from mode state
        mode = session.mode
        mode_data = await self._store.read_mode_state(mode)

        if mode_data and "sessions" in mode_data:
            sessions = mode_data["sessions"]
            if session_id in sessions:
                del sessions[session_id]

                # Update active session if needed
                if mode_data.get("active_session") == session_id:
                    mode_data["active_session"] = None

                await self._store.write_mode_state(mode, mode_data)

        # Remove from cache
        self._session_cache.pop(session_id, None)

        log.info("state.manager.session_deleted", session_id=session_id)
        return Result.ok(True)

    async def create_checkpoint(
        self,
        session_id: str,
        phase: str,
        state: dict[str, Any],
        checkpoint_id: str | None = None,
    ) -> Result[str, str]:
        """Create a checkpoint for recovery.

        Args:
            session_id: Associated session ID.
            phase: Current execution phase.
            state: Complete state snapshot.
            checkpoint_id: Optional checkpoint ID.

        Returns:
            Result containing checkpoint ID or error message.
        """
        if checkpoint_id is None:
            checkpoint_id = f"ckpt_{uuid4().hex[:12]}"

        checkpoint_data = CheckpointData(
            checkpoint_id=checkpoint_id,
            session_id=session_id,
            phase=phase,
            state=state,
        )

        result = await self._store.create_checkpoint(checkpoint_data.to_dict(), checkpoint_id)

        if result.is_ok:
            log.info(
                "state.manager.checkpoint_created",
                checkpoint_id=checkpoint_id,
                session_id=session_id,
                phase=phase,
            )
            return Result.ok(checkpoint_id)
        else:
            return Result.err(result.error)

    async def restore_checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        """Restore state from checkpoint.

        Args:
            checkpoint_id: Checkpoint identifier.

        Returns:
            Restored state data or None if not found.
        """
        checkpoint_data = await self._store.load_checkpoint(checkpoint_id)

        if checkpoint_data is None:
            log.warning("state.manager.checkpoint_not_found", checkpoint_id=checkpoint_id)
            return None

        log.info("state.manager.checkpoint_restored", checkpoint_id=checkpoint_id)
        return checkpoint_data.get("state")

    async def list_checkpoints(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """List available checkpoints.

        Args:
            session_id: Optional session ID to filter by.

        Returns:
            List of checkpoint metadata.
        """
        checkpoints = await self._store.list_checkpoints()

        if session_id is not None:
            checkpoints = [c for c in checkpoints if c.get("session_id") == session_id]

        return checkpoints

    async def get_active_session(self, mode: StateMode) -> SessionState | None:
        """Get the active session for a mode.

        Args:
            mode: Execution mode.

        Returns:
            Active SessionState or None.
        """
        mode_data = await self._store.read_mode_state(mode)

        if mode_data and mode_data.get("active_session"):
            session_id = mode_data["active_session"]
            return await self.load_session(session_id)

        return None

    async def set_active_session(self, session_id: str) -> Result[bool, str]:
        """Set the active session for its mode.

        Args:
            session_id: Session to mark as active.

        Returns:
            Result indicating success or error message.
        """
        session = await self.load_session(session_id)

        if session is None:
            return Result.err(f"Session {session_id} not found")

        mode = session.mode
        mode_data = await self._store.read_mode_state(mode) or {}

        mode_data["active_session"] = session_id
        await self._store.write_mode_state(mode, mode_data)

        log.info("state.manager.active_session_set", session_id=session_id, mode=mode.value)
        return Result.ok(True)

    async def start_auto_checkpoint(self) -> None:
        """Start automatic checkpoint creation task."""
        if self._checkpoint_task is not None and not self._checkpoint_task.done():
            return  # Already running

        async def _checkpoint_loop() -> None:
            while True:
                await asyncio.sleep(self.CHECKPOINT_INTERVAL)
                await self._auto_create_checkpoints()

        self._checkpoint_task = asyncio.create_task(_checkpoint_loop())
        log.info("state.manager.auto_checkpoint_started")

    async def stop_auto_checkpoint(self) -> None:
        """Stop automatic checkpoint creation task."""
        if self._checkpoint_task and not self._checkpoint_task.done():
            self._checkpoint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._checkpoint_task
            log.info("state.manager.auto_checkpoint_stopped")

    async def _auto_create_checkpoints(self) -> None:
        """Create checkpoints for all active sessions."""
        for mode in StateMode:
            session = await self.get_active_session(mode)
            if session and session.status == SessionStatus.ACTIVE:
                await self.create_checkpoint(
                    session_id=session.session_id,
                    phase="auto",
                    state={
                        "session": session.to_dict(),
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )

    def workflow_state_to_dict(self, workflow_state: WorkflowState) -> dict[str, Any]:
        """Convert WorkflowState to dictionary for storage.

        Args:
            workflow_state: WorkflowState to convert.

        Returns:
            Dictionary representation.
        """
        return {
            "session_id": workflow_state.session_id,
            "goal": workflow_state.goal,
            "completed_acs": workflow_state.completed_count,
            "total_acs": workflow_state.total_count,
            "progress_percent": workflow_state.progress_percent,
            "current_ac_index": workflow_state.current_ac_index,
            "current_phase": workflow_state.current_phase.value,
            "activity": workflow_state.activity.value,
            "activity_detail": workflow_state.activity_detail,
            "messages_count": workflow_state.messages_count,
            "tool_calls_count": workflow_state.tool_calls_count,
            "estimated_tokens": workflow_state.estimated_tokens,
            "estimated_cost_usd": workflow_state.estimated_cost_usd,
            "elapsed_seconds": workflow_state.elapsed_seconds,
            "acceptance_criteria": [
                {
                    "index": ac.index,
                    "content": ac.content,
                    "status": ac.status.value,
                    "elapsed_display": ac.elapsed_display,
                }
                for ac in workflow_state.acceptance_criteria
            ],
        }

    def dict_to_workflow_state(self, data: dict[str, Any]) -> WorkflowState:
        """Convert dictionary to WorkflowState.

        Args:
            data: Dictionary from storage.

        Returns:
            WorkflowState instance.
        """
        from ouroboros.orchestrator.workflow_state import (
            Phase,
        )

        acceptance_criteria = [
            AcceptanceCriterion(
                index=ac["index"],
                content=ac["content"],
                status=ACStatus(ac["status"]),
            )
            for ac in data.get("acceptance_criteria", [])
        ]

        return WorkflowState(
            session_id=data.get("session_id", ""),
            goal=data.get("goal", ""),
            acceptance_criteria=acceptance_criteria,
            current_ac_index=data.get("current_ac_index", 0),
            current_phase=Phase(data.get("current_phase", "Discover")),
            activity=ActivityType(data.get("activity", "idle")),
            activity_detail=data.get("activity_detail", ""),
            messages_count=data.get("messages_count", 0),
            tool_calls_count=data.get("tool_calls_count", 0),
            estimated_tokens=data.get("estimated_tokens", 0),
            estimated_cost_usd=data.get("estimated_cost_usd", 0.0),
        )
