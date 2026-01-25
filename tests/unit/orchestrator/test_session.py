"""Unit tests for session tracking."""

from __future__ import annotations

from datetime import UTC, datetime
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.orchestrator.session import (
    SessionRepository,
    SessionStatus,
    SessionTracker,
)


class TestSessionStatus:
    """Tests for SessionStatus enum."""

    def test_status_values(self) -> None:
        """Test that all status values are defined."""
        assert SessionStatus.RUNNING == "running"
        assert SessionStatus.PAUSED == "paused"
        assert SessionStatus.COMPLETED == "completed"
        assert SessionStatus.FAILED == "failed"


class TestSessionTracker:
    """Tests for SessionTracker dataclass."""

    def test_create_new_session(self) -> None:
        """Test creating a new session tracker."""
        tracker = SessionTracker.create(
            execution_id="exec_123",
            seed_id="seed_456",
        )
        assert tracker.execution_id == "exec_123"
        assert tracker.seed_id == "seed_456"
        assert tracker.status == SessionStatus.RUNNING
        assert tracker.session_id.startswith("orch_")
        assert tracker.messages_processed == 0
        assert tracker.progress == {}

    def test_create_with_custom_session_id(self) -> None:
        """Test creating session with custom ID."""
        tracker = SessionTracker.create(
            execution_id="exec_123",
            seed_id="seed_456",
            session_id="custom_session_id",
        )
        assert tracker.session_id == "custom_session_id"

    def test_with_progress_updates_immutably(self) -> None:
        """Test that with_progress creates a new instance."""
        original = SessionTracker.create("exec", "seed")
        updated = original.with_progress({"step": 1})

        assert original.messages_processed == 0
        assert original.progress == {}
        assert updated.messages_processed == 1
        assert updated.progress == {"step": 1}
        assert original is not updated

    def test_with_progress_merges_progress(self) -> None:
        """Test that progress is merged, not replaced."""
        tracker = SessionTracker.create("exec", "seed")
        tracker = tracker.with_progress({"a": 1})
        tracker = tracker.with_progress({"b": 2})

        assert tracker.progress == {"a": 1, "b": 2}
        assert tracker.messages_processed == 2

    def test_with_status(self) -> None:
        """Test changing session status."""
        tracker = SessionTracker.create("exec", "seed")
        assert tracker.status == SessionStatus.RUNNING

        updated = tracker.with_status(SessionStatus.COMPLETED)
        assert updated.status == SessionStatus.COMPLETED
        assert tracker.status == SessionStatus.RUNNING  # Original unchanged

    def test_is_active(self) -> None:
        """Test is_active property."""
        tracker = SessionTracker.create("exec", "seed")
        assert tracker.is_active is True

        paused = tracker.with_status(SessionStatus.PAUSED)
        assert paused.is_active is True

        completed = tracker.with_status(SessionStatus.COMPLETED)
        assert completed.is_active is False

        failed = tracker.with_status(SessionStatus.FAILED)
        assert failed.is_active is False

    def test_is_completed(self) -> None:
        """Test is_completed property."""
        tracker = SessionTracker.create("exec", "seed")
        assert tracker.is_completed is False

        completed = tracker.with_status(SessionStatus.COMPLETED)
        assert completed.is_completed is True

    def test_is_failed(self) -> None:
        """Test is_failed property."""
        tracker = SessionTracker.create("exec", "seed")
        assert tracker.is_failed is False

        failed = tracker.with_status(SessionStatus.FAILED)
        assert failed.is_failed is True

    def test_to_dict(self) -> None:
        """Test serialization to dictionary."""
        tracker = SessionTracker.create("exec_123", "seed_456")
        tracker = tracker.with_progress({"current": "step1"})

        data = tracker.to_dict()

        assert data["execution_id"] == "exec_123"
        assert data["seed_id"] == "seed_456"
        assert data["status"] == "running"
        assert data["progress"] == {"current": "step1"}
        assert data["messages_processed"] == 1
        assert "start_time" in data

    def test_tracker_is_frozen(self) -> None:
        """Test that SessionTracker is immutable."""
        tracker = SessionTracker.create("exec", "seed")
        with pytest.raises(AttributeError):
            tracker.status = SessionStatus.COMPLETED  # type: ignore


class TestSessionRepository:
    """Tests for SessionRepository."""

    @pytest.fixture
    def mock_event_store(self) -> AsyncMock:
        """Create a mock event store."""
        store = AsyncMock()
        store.append = AsyncMock()
        store.replay = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def repository(self, mock_event_store: AsyncMock) -> SessionRepository:
        """Create a repository with mock store."""
        return SessionRepository(mock_event_store)

    @pytest.mark.asyncio
    async def test_create_session(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test creating a new session."""
        result = await repository.create_session(
            execution_id="exec_123",
            seed_id="seed_456",
        )

        assert result.is_ok
        tracker = result.value
        assert tracker.execution_id == "exec_123"
        assert tracker.seed_id == "seed_456"

        # Verify event was emitted
        mock_event_store.append.assert_called_once()
        event = mock_event_store.append.call_args[0][0]
        assert event.type == "orchestrator.session.started"
        assert event.aggregate_type == "session"

    @pytest.mark.asyncio
    async def test_create_session_with_custom_id(
        self,
        repository: SessionRepository,
    ) -> None:
        """Test creating session with custom ID."""
        result = await repository.create_session(
            execution_id="exec",
            seed_id="seed",
            session_id="custom_id",
        )

        assert result.is_ok
        assert result.value.session_id == "custom_id"

    @pytest.mark.asyncio
    async def test_track_progress(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test tracking progress."""
        result = await repository.track_progress(
            session_id="sess_123",
            progress={"step": 5, "message": "Working"},
        )

        assert result.is_ok
        mock_event_store.append.assert_called_once()
        event = mock_event_store.append.call_args[0][0]
        assert event.type == "orchestrator.progress.updated"
        assert event.data["progress"]["step"] == 5

    @pytest.mark.asyncio
    async def test_mark_completed(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test marking session as completed."""
        result = await repository.mark_completed(
            session_id="sess_123",
            summary={"total_messages": 50},
        )

        assert result.is_ok
        event = mock_event_store.append.call_args[0][0]
        assert event.type == "orchestrator.session.completed"
        assert event.data["summary"]["total_messages"] == 50

    @pytest.mark.asyncio
    async def test_mark_failed(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test marking session as failed."""
        result = await repository.mark_failed(
            session_id="sess_123",
            error_message="Connection lost",
            error_details={"code": 500},
        )

        assert result.is_ok
        event = mock_event_store.append.call_args[0][0]
        assert event.type == "orchestrator.session.failed"
        assert event.data["error"] == "Connection lost"

    @pytest.mark.asyncio
    async def test_reconstruct_session_no_events(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test reconstructing session with no events."""
        mock_event_store.replay.return_value = []

        result = await repository.reconstruct_session("sess_123")

        assert result.is_err
        assert "No events found" in str(result.error)

    @pytest.mark.asyncio
    async def test_reconstruct_session_success(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test successful session reconstruction."""
        # Create mock events
        start_event = MagicMock()
        start_event.type = "orchestrator.session.started"
        start_event.data = {
            "execution_id": "exec_123",
            "seed_id": "seed_456",
            "start_time": datetime.now(UTC).isoformat(),
        }

        progress_event = MagicMock()
        progress_event.type = "orchestrator.progress.updated"
        progress_event.data = {"progress": {"step": 1}}

        mock_event_store.replay.return_value = [start_event, progress_event]

        result = await repository.reconstruct_session("sess_123")

        assert result.is_ok
        tracker = result.value
        assert tracker.session_id == "sess_123"
        assert tracker.execution_id == "exec_123"
        assert tracker.messages_processed == 1

    @pytest.mark.asyncio
    async def test_reconstruct_completed_session(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test reconstructing a completed session."""
        start_event = MagicMock()
        start_event.type = "orchestrator.session.started"
        start_event.data = {
            "execution_id": "exec",
            "seed_id": "seed",
            "start_time": datetime.now(UTC).isoformat(),
        }

        completed_event = MagicMock()
        completed_event.type = "orchestrator.session.completed"
        completed_event.data = {}

        mock_event_store.replay.return_value = [start_event, completed_event]

        result = await repository.reconstruct_session("sess")

        assert result.is_ok
        assert result.value.status == SessionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_reconstruct_failed_session(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test reconstructing a failed session."""
        start_event = MagicMock()
        start_event.type = "orchestrator.session.started"
        start_event.data = {
            "execution_id": "exec",
            "seed_id": "seed",
            "start_time": datetime.now(UTC).isoformat(),
        }

        failed_event = MagicMock()
        failed_event.type = "orchestrator.session.failed"
        failed_event.data = {}

        mock_event_store.replay.return_value = [start_event, failed_event]

        result = await repository.reconstruct_session("sess")

        assert result.is_ok
        assert result.value.status == SessionStatus.FAILED
