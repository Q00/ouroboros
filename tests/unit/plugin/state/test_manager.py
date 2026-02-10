"""Unit tests for State Manager.

Tests cover:
- SessionState dataclass (to_dict, from_dict)
- CheckpointData dataclass
- SessionStatus enum
- StateManager class methods
- Session save/load/update/delete
- Checkpoint create/restore/list
- Active session management
"""

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.types import Result
from ouroboros.orchestrator.workflow_state import (
    AcceptanceCriterion,
    ACStatus,
    ActivityType,
    Phase,
    WorkflowState,
)
from ouroboros.plugin.state.manager import (
    CheckpointData,
    SessionState,
    SessionStatus,
    StateManager,
)
from ouroboros.plugin.state.store import StateMode, StateStore


class TestSessionStatus:
    """Test SessionStatus enum."""

    def test_all_status_values_defined(self) -> None:
        """Test that all expected status values are defined."""
        expected_statuses = {
            "ACTIVE",
            "PAUSED",
            "COMPLETED",
            "FAILED",
            "RESUMED",
        }
        actual_statuses = {status.name for status in SessionStatus}
        assert actual_statuses == expected_statuses


class TestSessionState:
    """Test SessionState dataclass."""

    def test_create_session_state(self) -> None:
        """Test creating a SessionState."""
        state = SessionState(
            session_id="sess-123",
            execution_id="exec-456",
            seed_id="seed-789",
            seed_goal="Build a CLI tool",
            acceptance_criteria=["AC1", "AC2"],
            workflow_state={"step": "planning"},
            mode=StateMode.AUTOPILOT,
        )

        assert state.session_id == "sess-123"
        assert state.execution_id == "exec-456"
        assert state.seed_id == "seed-789"
        assert state.seed_goal == "Build a CLI tool"
        assert state.acceptance_criteria == ["AC1", "AC2"]
        assert state.mode == StateMode.AUTOPILOT
        assert state.status == SessionStatus.ACTIVE

    def test_session_state_to_dict(self) -> None:
        """Test converting SessionState to dictionary."""
        state = SessionState(
            session_id="sess-123",
            execution_id="exec-456",
            seed_id="seed-789",
            seed_goal="Test goal",
            acceptance_criteria=["AC1"],
            workflow_state={},
            mode=StateMode.AUTOPILOT,
        )

        data = state.to_dict()

        assert data["session_id"] == "sess-123"
        assert data["execution_id"] == "exec-456"
        assert data["mode"] == "autopilot"
        assert data["status"] == "active"
        assert "created_at" in data
        assert "updated_at" in data

    def test_session_state_from_dict(self) -> None:
        """Test creating SessionState from dictionary."""
        data = {
            "session_id": "sess-123",
            "execution_id": "exec-456",
            "seed_id": "seed-789",
            "seed_goal": "Test goal",
            "acceptance_criteria": ["AC1"],
            "workflow_state": {},
            "mode": "autopilot",
            "status": "active",
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T01:00:00+00:00",
            "metadata": {},
        }

        state = SessionState.from_dict(data)

        assert state.session_id == "sess-123"
        assert state.mode == StateMode.AUTOPILOT
        assert state.status == SessionStatus.ACTIVE

    def test_session_state_from_dict_defaults(self) -> None:
        """Test from_dict handles missing optional fields."""
        data = {
            "session_id": "sess-123",
            "execution_id": "exec-456",
            "seed_id": "seed-789",
            "seed_goal": "Test",
            "mode": "autopilot",
            "status": "active",
        }

        state = SessionState.from_dict(data)

        assert state.acceptance_criteria == []
        assert state.workflow_state == {}
        assert state.metadata == {}


class TestCheckpointData:
    """Test CheckpointData dataclass."""

    def test_create_checkpoint_data(self) -> None:
        """Test creating CheckpointData."""
        checkpoint = CheckpointData(
            checkpoint_id="ckpt-123",
            session_id="sess-456",
            phase="execution",
            state={"tasks": ["task1", "task2"]},
        )

        assert checkpoint.checkpoint_id == "ckpt-123"
        assert checkpoint.session_id == "sess-456"
        assert checkpoint.phase == "execution"
        assert checkpoint.state == {"tasks": ["task1", "task2"]}

    def test_checkpoint_data_to_dict(self) -> None:
        """Test converting CheckpointData to dictionary."""
        checkpoint = CheckpointData(
            checkpoint_id="ckpt-123",
            session_id="sess-456",
            phase="planning",
            state={"step": 1},
        )

        data = checkpoint.to_dict()

        assert data["checkpoint_id"] == "ckpt-123"
        assert data["session_id"] == "sess-456"
        assert data["phase"] == "planning"
        assert "created_at" in data


class TestStateManagerInit:
    """Test StateManager initialization."""

    def test_state_manager_initializes(self) -> None:
        """Test StateManager initialization."""
        mock_store = MagicMock(spec=StateStore)
        manager = StateManager(store=mock_store)

        assert manager._store == mock_store
        assert manager._auto_checkpoint is True
        assert manager._checkpoint_task is None
        assert manager._session_cache == {}

    def test_state_manager_with_auto_checkpoint_disabled(self) -> None:
        """Test creating manager without auto-checkpoint."""
        mock_store = MagicMock(spec=StateStore)
        manager = StateManager(
            store=mock_store,
            auto_checkpoint=False,
        )

        assert manager._auto_checkpoint is False


class TestStateManagerSaveSession:
    """Test StateManager.save_session method."""

    async def test_save_session_success(self) -> None:
        """Test successful session save."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.write_mode_state = AsyncMock(return_value=Result.ok(True))

        manager = StateManager(store=mock_store)

        result = await manager.save_session(
            session_id="sess-123",
            execution_id="exec-456",
            seed_id="seed-789",
            seed_goal="Test goal",
            acceptance_criteria=[],
            workflow_state={},
            mode=StateMode.AUTOPILOT,
        )

        assert result.is_ok
        assert result.value == "sess-123"

    async def test_save_session_updates_cache(self) -> None:
        """Test that save updates session cache."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.write_mode_state = AsyncMock(return_value=Result.ok(True))

        manager = StateManager(store=mock_store)

        await manager.save_session(
            session_id="sess-123",
            execution_id="exec-456",
            seed_id="seed-789",
            seed_goal="Test",
            acceptance_criteria=[],
            workflow_state={},
            mode=StateMode.AUTOPILOT,
        )

        assert "sess-123" in manager._session_cache

    async def test_save_session_with_metadata(self) -> None:
        """Test saving session with metadata."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.write_mode_state = AsyncMock(return_value=Result.ok(True))

        manager = StateManager(store=mock_store)

        result = await manager.save_session(
            session_id="sess-123",
            execution_id="exec-456",
            seed_id="seed-789",
            seed_goal="Test",
            acceptance_criteria=[],
            workflow_state={},
            mode=StateMode.AUTOPILOT,
            metadata={"key": "value"},
        )

        assert result.is_ok
        assert manager._session_cache["sess-123"].metadata == {"key": "value"}

    async def test_save_session_store_error(self) -> None:
        """Test save handles store errors."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.write_mode_state = AsyncMock(
            return_value=Result.err("Storage error")
        )

        manager = StateManager(store=mock_store)

        result = await manager.save_session(
            session_id="sess-123",
            execution_id="exec-456",
            seed_id="seed-789",
            seed_goal="Test",
            acceptance_criteria=[],
            workflow_state={},
            mode=StateMode.AUTOPILOT,
        )

        assert result.is_err


class TestStateManagerLoadSession:
    """Test StateManager.load_session method."""

    async def test_load_session_from_cache(self) -> None:
        """Test loading session from cache."""
        mock_store = MagicMock(spec=StateStore)

        manager = StateManager(store=mock_store)

        # Pre-populate cache
        session = SessionState(
            session_id="cached-sess",
            execution_id="exec-1",
            seed_id="seed-1",
            seed_goal="Cached",
            acceptance_criteria=[],
            workflow_state={},
            mode=StateMode.AUTOPILOT,
        )
        manager._session_cache["cached-sess"] = session

        loaded = await manager.load_session("cached-sess")

        assert loaded is not None
        assert loaded.session_id == "cached-sess"
        # Should not call store
        mock_store.read_mode_state.assert_not_awaited()

    async def test_load_session_from_store(self) -> None:
        """Test loading session from store."""
        mock_store = MagicMock(spec=StateStore)

        session_data = {
            "sessions": {
                "stored-sess": {
                    "session_id": "stored-sess",
                    "execution_id": "exec-1",
                    "seed_id": "seed-1",
                    "seed_goal": "Stored",
                    "acceptance_criteria": [],
                    "workflow_state": {},
                    "mode": "autopilot",
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                    "metadata": {},
                }
            }
        }
        mock_store.read_mode_state = AsyncMock(return_value=session_data)

        manager = StateManager(store=mock_store)

        loaded = await manager.load_session("stored-sess")

        assert loaded is not None
        assert loaded.session_id == "stored-sess"
        assert "stored-sess" in manager._session_cache

    async def test_load_session_not_found(self) -> None:
        """Test loading nonexistent session returns None."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.read_mode_state = AsyncMock(return_value=None)

        manager = StateManager(store=mock_store)

        loaded = await manager.load_session("nonexistent")

        assert loaded is None


class TestStateManagerUpdateSession:
    """Test StateManager.update_session method."""

    async def test_update_session_workflow_state(self) -> None:
        """Test updating session workflow state."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.read_mode_state = AsyncMock(return_value=None)
        mock_store.write_mode_state = AsyncMock(return_value=Result.ok(True))

        manager = StateManager(store=mock_store)

        # Create session first
        await manager.save_session(
            session_id="sess-123",
            execution_id="exec-1",
            seed_id="seed-1",
            seed_goal="Test",
            acceptance_criteria=[],
            workflow_state={"old": "state"},
            mode=StateMode.AUTOPILOT,
        )

        result = await manager.update_session(
            session_id="sess-123",
            workflow_state={"new": "state"},
        )

        assert result.is_ok
        assert result.value.workflow_state == {"new": "state"}

    async def test_update_session_status(self) -> None:
        """Test updating session status."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.read_mode_state = AsyncMock(return_value=None)
        mock_store.write_mode_state = AsyncMock(return_value=Result.ok(True))

        manager = StateManager(store=mock_store)

        await manager.save_session(
            session_id="sess-123",
            execution_id="exec-1",
            seed_id="seed-1",
            seed_goal="Test",
            acceptance_criteria=[],
            workflow_state={},
            mode=StateMode.AUTOPILOT,
        )

        result = await manager.update_session(
            session_id="sess-123",
            status=SessionStatus.COMPLETED,
        )

        assert result.is_ok
        assert result.value.status == SessionStatus.COMPLETED

    async def test_update_session_metadata_merge(self) -> None:
        """Test updating session merges metadata."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.read_mode_state = AsyncMock(return_value=None)
        mock_store.write_mode_state = AsyncMock(return_value=Result.ok(True))

        manager = StateManager(store=mock_store)

        await manager.save_session(
            session_id="sess-123",
            execution_id="exec-1",
            seed_id="seed-1",
            seed_goal="Test",
            acceptance_criteria=[],
            workflow_state={},
            mode=StateMode.AUTOPILOT,
            metadata={"existing": "value"},
        )

        result = await manager.update_session(
            session_id="sess-123",
            metadata={"new": "data"},
        )

        assert result.is_ok
        assert result.value.metadata == {"existing": "value", "new": "data"}

    async def test_update_nonexistent_session_returns_error(self) -> None:
        """Test updating nonexistent session returns error."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.read_mode_state = AsyncMock(return_value=None)

        manager = StateManager(store=mock_store)

        result = await manager.update_session(
            session_id="nonexistent",
            workflow_state={},
        )

        assert result.is_err


class TestStateManagerDeleteSession:
    """Test StateManager.delete_session method."""

    async def test_delete_session_removes_from_store(self) -> None:
        """Test deleting session removes from store."""
        mock_store = MagicMock(spec=StateStore)

        session_data = {
            "sessions": {
                "sess-123": {
                    "session_id": "sess-123",
                    "execution_id": "exec-1",
                    "seed_id": "seed-1",
                    "seed_goal": "Test",
                    "acceptance_criteria": [],
                    "workflow_state": {},
                    "mode": "autopilot",
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                    "metadata": {},
                }
            },
            "active_session": "sess-123",
        }
        mock_store.read_mode_state = AsyncMock(return_value=session_data)
        mock_store.write_mode_state = AsyncMock(return_value=Result.ok(True))

        manager = StateManager(store=mock_store)

        result = await manager.delete_session("sess-123")

        assert result.is_ok
        assert "sess-123" not in manager._session_cache

    async def test_delete_nonexistent_session_returns_ok(self) -> None:
        """Test deleting nonexistent session returns ok."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.read_mode_state = AsyncMock(return_value=None)

        manager = StateManager(store=mock_store)

        result = await manager.delete_session("nonexistent")

        assert result.is_ok
        assert result.value is False


class TestStateManagerCreateCheckpoint:
    """Test StateManager.create_checkpoint method."""

    async def test_create_checkpoint_generates_id(self) -> None:
        """Test creating checkpoint generates ID."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.create_checkpoint = AsyncMock(return_value=Result.ok(True))

        manager = StateManager(store=mock_store)

        result = await manager.create_checkpoint(
            session_id="sess-123",
            phase="planning",
            state={"step": 1},
        )

        assert result.is_ok
        # Checkpoint ID should be generated
        assert result.value.startswith("ckpt_")

    async def test_create_checkpoint_with_custom_id(self) -> None:
        """Test creating checkpoint with custom ID."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.create_checkpoint = AsyncMock(return_value=Result.ok(True))

        manager = StateManager(store=mock_store)

        result = await manager.create_checkpoint(
            session_id="sess-123",
            phase="planning",
            state={},
            checkpoint_id="custom-id",
        )

        assert result.is_ok
        assert result.value == "custom-id"


class TestStateManagerRestoreCheckpoint:
    """Test StateManager.restore_checkpoint method."""

    async def test_restore_checkpoint_returns_state(self) -> None:
        """Test restoring checkpoint returns state."""
        mock_store = MagicMock(spec=StateStore)

        checkpoint_data = {
            "checkpoint_id": "ckpt-123",
            "session_id": "sess-456",
            "phase": "execution",
            "state": {"restored": "data"},
            "created_at": datetime.now(UTC).isoformat(),
        }
        mock_store.load_checkpoint = AsyncMock(return_value=checkpoint_data)

        manager = StateManager(store=mock_store)

        state = await manager.restore_checkpoint("ckpt-123")

        assert state is not None
        assert state == {"restored": "data"}

    async def test_restore_nonexistent_checkpoint_returns_none(self) -> None:
        """Test restoring nonexistent checkpoint returns None."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.load_checkpoint = AsyncMock(return_value=None)

        manager = StateManager(store=mock_store)

        state = await manager.restore_checkpoint("nonexistent")

        assert state is None


class TestStateManagerListCheckpoints:
    """Test StateManager.list_checkpoints method."""

    async def test_list_all_checkpoints(self) -> None:
        """Test listing all checkpoints."""
        mock_store = MagicMock(spec=StateStore)

        checkpoints = [
            {
                "checkpoint_id": "ckpt-1",
                "session_id": "sess-1",
                "phase": "planning",
                "state": {},
                "created_at": datetime.now(UTC).isoformat(),
            },
            {
                "checkpoint_id": "ckpt-2",
                "session_id": "sess-2",
                "phase": "execution",
                "state": {},
                "created_at": datetime.now(UTC).isoformat(),
            },
        ]
        mock_store.list_checkpoints = AsyncMock(return_value=checkpoints)

        manager = StateManager(store=mock_store)

        result = await manager.list_checkpoints()

        assert len(result) == 2

    async def test_list_checkpoints_filtered_by_session(self) -> None:
        """Test listing checkpoints filtered by session."""
        mock_store = MagicMock(spec=StateStore)

        checkpoints = [
            {
                "checkpoint_id": "ckpt-1",
                "session_id": "sess-1",
                "phase": "planning",
                "state": {},
                "created_at": datetime.now(UTC).isoformat(),
            },
            {
                "checkpoint_id": "ckpt-2",
                "session_id": "sess-2",
                "phase": "execution",
                "state": {},
                "created_at": datetime.now(UTC).isoformat(),
            },
        ]
        mock_store.list_checkpoints = AsyncMock(return_value=checkpoints)

        manager = StateManager(store=mock_store)

        result = await manager.list_checkpoints(session_id="sess-1")

        assert len(result) == 1
        assert result[0]["checkpoint_id"] == "ckpt-1"


class TestStateManagerActiveSession:
    """Test StateManager active session management."""

    async def test_get_active_session(self) -> None:
        """Test getting active session for mode."""
        mock_store = MagicMock(spec=StateStore)

        mode_data = {
            "active_session": "sess-active",
            "sessions": {
                "sess-active": {
                    "session_id": "sess-active",
                    "execution_id": "exec-1",
                    "seed_id": "seed-1",
                    "seed_goal": "Test",
                    "acceptance_criteria": [],
                    "workflow_state": {},
                    "mode": "autopilot",
                    "status": "active",
                    "created_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                    "metadata": {},
                }
            },
        }
        mock_store.read_mode_state = AsyncMock(return_value=mode_data)

        manager = StateManager(store=mock_store)

        session = await manager.get_active_session(StateMode.AUTOPILOT)

        assert session is not None
        assert session.session_id == "sess-active"

    async def test_get_active_session_none_when_no_active(self) -> None:
        """Test getting active session returns None when none active."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.read_mode_state = AsyncMock(return_value={})

        manager = StateManager(store=mock_store)

        session = await manager.get_active_session(StateMode.AUTOPILOT)

        assert session is None

    async def test_set_active_session(self) -> None:
        """Test setting active session."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.read_mode_state = AsyncMock(return_value={})
        mock_store.write_mode_state = AsyncMock(return_value=Result.ok(True))

        manager = StateManager(store=mock_store)

        # Create session first
        await manager.save_session(
            session_id="sess-123",
            execution_id="exec-1",
            seed_id="seed-1",
            seed_goal="Test",
            acceptance_criteria=[],
            workflow_state={},
            mode=StateMode.AUTOPILOT,
        )

        result = await manager.set_active_session("sess-123")

        assert result.is_ok

    async def test_set_active_nonexistent_session_returns_error(self) -> None:
        """Test setting nonexistent active session returns error."""
        mock_store = MagicMock(spec=StateStore)
        mock_store.read_mode_state = AsyncMock(return_value=None)

        manager = StateManager(store=mock_store)

        result = await manager.set_active_session("nonexistent")

        assert result.is_err


class TestStateManagerAutoCheckpoint:
    """Test StateManager auto-checkpoint functionality."""

    async def test_start_auto_checkpoint(self) -> None:
        """Test starting auto-checkpoint creates task."""
        mock_store = MagicMock(spec=StateStore)

        manager = StateManager(store=mock_store)

        await manager.start_auto_checkpoint()

        assert manager._checkpoint_task is not None

    async def test_stop_auto_checkpoint(self) -> None:
        """Test stopping auto-checkpoint."""
        mock_store = MagicMock(spec=StateStore)

        manager = StateManager(store=mock_store)

        await manager.start_auto_checkpoint()
        await manager.stop_auto_checkpoint()

        # Task should be cancelled
        assert manager._checkpoint_task is None or manager._checkpoint_task.done()


class TestStateManagerWorkflowStateConversion:
    """Test WorkflowState conversion methods."""

    def test_workflow_state_to_dict(self) -> None:
        """Test converting WorkflowState to dict."""
        manager = StateManager(store=MagicMock(spec=StateStore))

        wf_state = WorkflowState(
            session_id="sess-123",
            goal="Test goal",
            acceptance_criteria=[
                AcceptanceCriterion(index=0, content="AC1", status=ACStatus.PENDING),
            ],
            current_ac_index=0,
            current_phase=Phase.DISCOVER,
            activity=ActivityType.IDLE,
        )

        result = manager.workflow_state_to_dict(wf_state)

        assert result["session_id"] == "sess-123"
        assert result["goal"] == "Test goal"
        assert result["current_ac_index"] == 0
        assert result["current_phase"] == "Discover"

    def test_dict_to_workflow_state(self) -> None:
        """Test converting dict to WorkflowState."""
        manager = StateManager(store=MagicMock(spec=StateStore))

        data = {
            "session_id": "sess-123",
            "goal": "Test goal",
            "acceptance_criteria": [
                {"index": 0, "content": "AC1", "status": "pending"},
            ],
            "current_ac_index": 0,
            "current_phase": "Discover",
            "activity": "idle",
            "activity_detail": "",
            "messages_count": 0,
            "tool_calls_count": 0,
            "estimated_tokens": 0,
            "estimated_cost_usd": 0.0,
        }

        result = manager.dict_to_workflow_state(data)

        assert result.session_id == "sess-123"
        assert result.goal == "Test goal"
        assert result.current_ac_index == 0
