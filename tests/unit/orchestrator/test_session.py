"""Unit tests for session tracking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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

    def test_with_progress_uses_explicit_messages_processed(self) -> None:
        """When update dict contains messages_processed, use that value instead of +1."""
        tracker = SessionTracker.create("exec", "seed")
        tracker = tracker.with_progress({"messages_processed": 5, "step": "exec"})

        assert tracker.messages_processed == 5
        assert tracker.progress["messages_processed"] == 5

    def test_with_progress_increments_when_messages_processed_absent(self) -> None:
        """Without explicit messages_processed, auto-increment by 1."""
        tracker = SessionTracker.create("exec", "seed")
        tracker = tracker.with_progress({"step": "exec"})

        assert tracker.messages_processed == 1

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
    async def test_mark_cancelled(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test marking session as cancelled."""
        result = await repository.mark_cancelled(
            session_id="sess_123",
            reason="User requested cancellation",
            cancelled_by="user",
        )

        assert result.is_ok
        event = mock_event_store.append.call_args[0][0]
        assert event.type == "orchestrator.session.cancelled"
        assert event.data["reason"] == "User requested cancellation"
        assert event.data["cancelled_by"] == "user"
        assert "cancelled_at" in event.data

    @pytest.mark.asyncio
    async def test_mark_cancelled_auto_cleanup(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test marking session as cancelled by auto-cleanup."""
        result = await repository.mark_cancelled(
            session_id="sess_123",
            reason="Stale session detected",
            cancelled_by="auto_cleanup",
        )

        assert result.is_ok
        event = mock_event_store.append.call_args[0][0]
        assert event.type == "orchestrator.session.cancelled"
        assert event.data["cancelled_by"] == "auto_cleanup"

    @pytest.mark.asyncio
    async def test_mark_cancelled_event_store_error(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test mark_cancelled handles event store errors gracefully."""
        mock_event_store.append.side_effect = Exception("DB connection lost")

        result = await repository.mark_cancelled(
            session_id="sess_123",
            reason="User requested cancellation",
        )

        assert result.is_err
        assert "Failed to mark session cancelled" in str(result.error)

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
    async def test_reconstruct_session_merges_progress_updates(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test reconstruction merges progress payloads across events."""
        start_event = MagicMock()
        start_event.type = "orchestrator.session.started"
        start_event.data = {
            "execution_id": "exec_123",
            "seed_id": "seed_456",
            "start_time": datetime.now(UTC).isoformat(),
        }

        runtime_progress = MagicMock()
        runtime_progress.type = "orchestrator.progress.updated"
        runtime_progress.data = {
            "progress": {
                "runtime": {
                    "backend": "claude",
                    "native_session_id": "sess_native",
                },
                "messages_processed": 3,
            }
        }

        message_progress = MagicMock()
        message_progress.type = "orchestrator.progress.updated"
        message_progress.data = {
            "progress": {
                "last_message_type": "assistant",
                "messages_processed": 7,
            }
        }

        mock_event_store.replay.return_value = [
            start_event,
            runtime_progress,
            message_progress,
        ]

        result = await repository.reconstruct_session("sess_123")

        assert result.is_ok
        tracker = result.value
        assert tracker.messages_processed == 7
        assert tracker.progress["last_message_type"] == "assistant"
        assert tracker.progress["runtime"]["native_session_id"] == "sess_native"

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

    @pytest.mark.asyncio
    async def test_reconstruct_cancelled_session(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test reconstructing a cancelled session."""
        start_event = MagicMock()
        start_event.type = "orchestrator.session.started"
        start_event.data = {
            "execution_id": "exec",
            "seed_id": "seed",
            "start_time": datetime.now(UTC).isoformat(),
        }

        cancelled_event = MagicMock()
        cancelled_event.type = "orchestrator.session.cancelled"
        cancelled_event.data = {
            "reason": "User requested",
            "cancelled_by": "user",
        }

        mock_event_store.replay.return_value = [start_event, cancelled_event]

        result = await repository.reconstruct_session("sess")

        assert result.is_ok
        assert result.value.status == SessionStatus.CANCELLED


class TestFindOrphanedSessions:
    """Tests for orphaned session detection."""

    @pytest.fixture
    def mock_event_store(self) -> AsyncMock:
        """Create a mock event store."""
        store = AsyncMock()
        store.append = AsyncMock()
        store.replay = AsyncMock(return_value=[])
        store.get_all_sessions = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def repository(self, mock_event_store: AsyncMock) -> SessionRepository:
        """Create a repository with mock store."""
        return SessionRepository(mock_event_store)

    def _make_start_event(
        self,
        session_id: str,
        timestamp: datetime | None = None,
    ) -> MagicMock:
        """Helper to create a mock session start event."""
        event = MagicMock()
        event.type = "orchestrator.session.started"
        event.aggregate_id = session_id
        event.timestamp = timestamp or datetime.now(UTC)
        event.data = {
            "execution_id": f"exec_{session_id}",
            "seed_id": f"seed_{session_id}",
            "start_time": (timestamp or datetime.now(UTC)).isoformat(),
        }
        return event

    def _make_progress_event(
        self,
        session_id: str,
        timestamp: datetime | None = None,
    ) -> MagicMock:
        """Helper to create a mock progress event."""
        event = MagicMock()
        event.type = "orchestrator.progress.updated"
        event.aggregate_id = session_id
        event.timestamp = timestamp or datetime.now(UTC)
        event.data = {"progress": {"step": 1}, "timestamp": event.timestamp.isoformat()}
        return event

    def _make_terminal_event(
        self,
        session_id: str,
        event_type: str,
        timestamp: datetime | None = None,
    ) -> MagicMock:
        """Helper to create a mock terminal event (completed/failed/cancelled)."""
        event = MagicMock()
        event.type = event_type
        event.aggregate_id = session_id
        event.timestamp = timestamp or datetime.now(UTC)
        event.data = {}
        return event

    @pytest.mark.asyncio
    async def test_no_sessions_returns_empty(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that no sessions returns empty list."""
        mock_event_store.get_all_sessions.return_value = []

        result = await repository.find_orphaned_sessions()

        assert result == []

    @pytest.mark.asyncio
    async def test_running_session_within_threshold_not_orphaned(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that a recently active running session is NOT orphaned."""
        now = datetime.now(UTC)
        start_event = self._make_start_event("sess_1", timestamp=now - timedelta(minutes=30))
        progress_event = self._make_progress_event("sess_1", timestamp=now - timedelta(minutes=5))

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event, progress_event]

        result = await repository.find_orphaned_sessions()

        assert result == []

    @pytest.mark.asyncio
    async def test_running_session_beyond_threshold_is_orphaned(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that a running session with no recent activity IS orphaned."""
        now = datetime.now(UTC)
        old_time = now - timedelta(hours=2)
        start_event = self._make_start_event("sess_1", timestamp=old_time)
        progress_event = self._make_progress_event(
            "sess_1", timestamp=old_time + timedelta(minutes=5)
        )

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event, progress_event]

        result = await repository.find_orphaned_sessions()

        assert len(result) == 1
        assert result[0].session_id == "sess_1"
        assert result[0].status == SessionStatus.RUNNING

    @pytest.mark.asyncio
    async def test_completed_session_not_orphaned(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that a completed session is NOT orphaned even if old."""
        old_time = datetime.now(UTC) - timedelta(hours=5)
        start_event = self._make_start_event("sess_1", timestamp=old_time)
        completed_event = self._make_terminal_event(
            "sess_1", "orchestrator.session.completed", timestamp=old_time + timedelta(hours=1)
        )

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event, completed_event]

        result = await repository.find_orphaned_sessions()

        assert result == []

    @pytest.mark.asyncio
    async def test_failed_session_not_orphaned(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that a failed session is NOT orphaned."""
        old_time = datetime.now(UTC) - timedelta(hours=5)
        start_event = self._make_start_event("sess_1", timestamp=old_time)
        failed_event = self._make_terminal_event(
            "sess_1", "orchestrator.session.failed", timestamp=old_time + timedelta(hours=1)
        )

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event, failed_event]

        result = await repository.find_orphaned_sessions()

        assert result == []

    @pytest.mark.asyncio
    async def test_cancelled_session_not_orphaned(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that an already-cancelled session is NOT orphaned."""
        old_time = datetime.now(UTC) - timedelta(hours=5)
        start_event = self._make_start_event("sess_1", timestamp=old_time)
        cancelled_event = self._make_terminal_event(
            "sess_1", "orchestrator.session.cancelled", timestamp=old_time + timedelta(hours=1)
        )

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event, cancelled_event]

        result = await repository.find_orphaned_sessions()

        assert result == []

    @pytest.mark.asyncio
    async def test_paused_session_beyond_threshold_is_orphaned(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that a paused session beyond threshold IS orphaned."""
        old_time = datetime.now(UTC) - timedelta(hours=3)
        start_event = self._make_start_event("sess_1", timestamp=old_time)
        paused_event = self._make_terminal_event(
            "sess_1", "orchestrator.session.paused", timestamp=old_time + timedelta(minutes=10)
        )

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event, paused_event]

        result = await repository.find_orphaned_sessions()

        assert len(result) == 1
        assert result[0].status == SessionStatus.PAUSED

    @pytest.mark.asyncio
    async def test_multiple_sessions_mixed_states(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test with multiple sessions in different states."""
        now = datetime.now(UTC)
        old_time = now - timedelta(hours=2)

        # Session 1: running and stale (orphaned)
        start_1 = self._make_start_event("sess_1", timestamp=old_time)
        # Session 2: completed (not orphaned)
        start_2 = self._make_start_event("sess_2", timestamp=old_time)
        completed_2 = self._make_terminal_event(
            "sess_2", "orchestrator.session.completed", timestamp=old_time + timedelta(minutes=30)
        )
        # Session 3: running but recent (not orphaned)
        start_3 = self._make_start_event("sess_3", timestamp=now - timedelta(minutes=10))
        progress_3 = self._make_progress_event("sess_3", timestamp=now - timedelta(minutes=2))

        mock_event_store.get_all_sessions.return_value = [start_1, start_2, start_3]

        replay_data = {
            "sess_1": [start_1],
            "sess_2": [start_2, completed_2],
            "sess_3": [start_3, progress_3],
        }

        async def mock_replay(aggregate_type: str, aggregate_id: str) -> list:
            return replay_data.get(aggregate_id, [])

        mock_event_store.replay.side_effect = mock_replay

        result = await repository.find_orphaned_sessions()

        assert len(result) == 1
        assert result[0].session_id == "sess_1"

    @pytest.mark.asyncio
    async def test_custom_staleness_threshold(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test with a custom staleness threshold."""
        now = datetime.now(UTC)
        # Session started 30 minutes ago
        start_event = self._make_start_event("sess_1", timestamp=now - timedelta(minutes=30))

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event]

        # With default 1-hour threshold: NOT orphaned
        result = await repository.find_orphaned_sessions()
        assert result == []

        # With 15-minute threshold: IS orphaned
        result = await repository.find_orphaned_sessions(staleness_threshold=timedelta(minutes=15))
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_replay_failure_skips_session(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that a replay failure for one session doesn't break detection."""
        old_time = datetime.now(UTC) - timedelta(hours=2)
        start_1 = self._make_start_event("sess_1", timestamp=old_time)
        start_2 = self._make_start_event("sess_2", timestamp=old_time)

        mock_event_store.get_all_sessions.return_value = [start_1, start_2]

        async def mock_replay(aggregate_type: str, aggregate_id: str) -> list:
            if aggregate_id == "sess_1":
                raise Exception("DB error")
            return [start_2]

        mock_event_store.replay.side_effect = mock_replay

        # Should not raise, and sess_2 should still be detected
        result = await repository.find_orphaned_sessions()
        assert len(result) == 1
        assert result[0].session_id == "sess_2"

    @pytest.mark.asyncio
    async def test_event_store_failure_returns_empty(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that event store failure returns empty list gracefully."""
        mock_event_store.get_all_sessions.side_effect = Exception("DB connection lost")

        result = await repository.find_orphaned_sessions()

        assert result == []

    @pytest.mark.asyncio
    async def test_session_at_exact_threshold_not_orphaned(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that a session at exactly the threshold boundary is NOT orphaned.

        The comparison uses strict > so a session whose last activity is exactly
        staleness_threshold ago should not be considered orphaned.
        """
        from unittest.mock import patch as mock_patch

        fixed_now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
        threshold = timedelta(hours=1)
        # Last activity exactly at the threshold boundary
        start_event = self._make_start_event("sess_boundary", timestamp=fixed_now - threshold)

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event]

        # Freeze time so find_orphaned_sessions sees the same 'now'
        with mock_patch("ouroboros.orchestrator.session.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            result = await repository.find_orphaned_sessions(staleness_threshold=threshold)

        # Strict > means exactly-at-threshold is NOT orphaned
        assert result == []


class TestCancelOrphanedSessions:
    """Tests for auto-cancel-on-startup routine."""

    @pytest.fixture
    def mock_event_store(self) -> AsyncMock:
        """Create a mock event store."""
        store = AsyncMock()
        store.append = AsyncMock()
        store.replay = AsyncMock(return_value=[])
        store.get_all_sessions = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def repository(self, mock_event_store: AsyncMock) -> SessionRepository:
        """Create a repository with mock store."""
        return SessionRepository(mock_event_store)

    def _make_start_event(
        self,
        session_id: str,
        timestamp: datetime | None = None,
    ) -> MagicMock:
        """Helper to create a mock session start event."""
        event = MagicMock()
        event.type = "orchestrator.session.started"
        event.aggregate_id = session_id
        event.timestamp = timestamp or datetime.now(UTC)
        event.data = {
            "execution_id": f"exec_{session_id}",
            "seed_id": f"seed_{session_id}",
            "start_time": (timestamp or datetime.now(UTC)).isoformat(),
        }
        return event

    @pytest.mark.asyncio
    async def test_no_orphans_returns_empty(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that no orphaned sessions returns empty list."""
        mock_event_store.get_all_sessions.return_value = []

        result = await repository.cancel_orphaned_sessions()

        assert result == []
        # mark_cancelled should not have been called
        mock_event_store.append.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancels_orphaned_sessions(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that orphaned sessions are cancelled and events emitted."""
        old_time = datetime.now(UTC) - timedelta(hours=2)
        start_event = self._make_start_event("sess_1", timestamp=old_time)

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event]

        result = await repository.cancel_orphaned_sessions()

        assert len(result) == 1
        assert result[0].session_id == "sess_1"

        # Verify a cancellation event was appended
        mock_event_store.append.assert_called_once()
        appended_event = mock_event_store.append.call_args[0][0]
        assert appended_event.type == "orchestrator.session.cancelled"
        assert appended_event.aggregate_id == "sess_1"
        assert appended_event.data["cancelled_by"] == "auto_cleanup"
        assert "Auto-cancelled on startup" in appended_event.data["reason"]

    @pytest.mark.asyncio
    async def test_cancels_multiple_orphaned_sessions(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that multiple orphaned sessions are all cancelled."""
        old_time = datetime.now(UTC) - timedelta(hours=2)
        start_1 = self._make_start_event("sess_1", timestamp=old_time)
        start_2 = self._make_start_event("sess_2", timestamp=old_time)

        mock_event_store.get_all_sessions.return_value = [start_1, start_2]

        async def mock_replay(aggregate_type: str, aggregate_id: str) -> list:
            if aggregate_id == "sess_1":
                return [start_1]
            elif aggregate_id == "sess_2":
                return [start_2]
            return []

        mock_event_store.replay.side_effect = mock_replay

        result = await repository.cancel_orphaned_sessions()

        assert len(result) == 2
        assert {r.session_id for r in result} == {"sess_1", "sess_2"}
        # Two cancellation events appended
        assert mock_event_store.append.call_count == 2

    @pytest.mark.asyncio
    async def test_cancel_failure_does_not_include_in_result(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that a failed cancellation is excluded from returned list."""
        old_time = datetime.now(UTC) - timedelta(hours=2)
        start_event = self._make_start_event("sess_1", timestamp=old_time)

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event]
        # Make append fail (cancellation fails)
        mock_event_store.append.side_effect = Exception("DB write error")

        result = await repository.cancel_orphaned_sessions()

        # Session should not be in the result since cancellation failed
        assert result == []

    @pytest.mark.asyncio
    async def test_logs_to_stderr(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Test that cancellations are logged to stderr."""
        old_time = datetime.now(UTC) - timedelta(hours=2)
        start_event = self._make_start_event("sess_1", timestamp=old_time)

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event]

        await repository.cancel_orphaned_sessions()

        captured = capsys.readouterr()
        assert "Auto-cancelled orphaned session sess_1" in captured.err
        assert "exec_sess_1" in captured.err

    @pytest.mark.asyncio
    async def test_uses_auto_cleanup_cancelled_by(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that cancelled_by is set to 'auto_cleanup'."""
        old_time = datetime.now(UTC) - timedelta(hours=2)
        start_event = self._make_start_event("sess_1", timestamp=old_time)

        mock_event_store.get_all_sessions.return_value = [start_event]
        mock_event_store.replay.return_value = [start_event]

        await repository.cancel_orphaned_sessions()

        appended_event = mock_event_store.append.call_args[0][0]
        assert appended_event.data["cancelled_by"] == "auto_cleanup"

    @pytest.mark.asyncio
    async def test_emits_events_for_each_cancellation(
        self,
        repository: SessionRepository,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that a corresponding event is emitted for each cancellation."""
        old_time = datetime.now(UTC) - timedelta(hours=3)
        start_1 = self._make_start_event("sess_a", timestamp=old_time)
        start_2 = self._make_start_event("sess_b", timestamp=old_time)

        mock_event_store.get_all_sessions.return_value = [start_1, start_2]

        async def mock_replay(aggregate_type: str, aggregate_id: str) -> list:
            if aggregate_id == "sess_a":
                return [start_1]
            elif aggregate_id == "sess_b":
                return [start_2]
            return []

        mock_event_store.replay.side_effect = mock_replay

        await repository.cancel_orphaned_sessions()

        # Each orphaned session should have a cancellation event
        assert mock_event_store.append.call_count == 2
        event_ids = {call[0][0].aggregate_id for call in mock_event_store.append.call_args_list}
        assert event_ids == {"sess_a", "sess_b"}
        for call in mock_event_store.append.call_args_list:
            event = call[0][0]
            assert event.type == "orchestrator.session.cancelled"
            assert "cancelled_at" in event.data
