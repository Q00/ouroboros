"""Unit tests for the cancel CLI command."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from ouroboros.cli.main import app
from ouroboros.core.errors import PersistenceError
from ouroboros.core.types import Result
from ouroboros.orchestrator.session import SessionStatus, SessionTracker

runner = CliRunner()


def _make_tracker(
    session_id: str = "orch_test123",
    status: SessionStatus = SessionStatus.RUNNING,
) -> SessionTracker:
    """Create a SessionTracker for testing."""
    from datetime import UTC, datetime

    return SessionTracker(
        session_id=session_id,
        execution_id="exec_001",
        seed_id="seed_001",
        status=status,
        start_time=datetime.now(UTC),
    )


class TestCancelCommandGroup:
    """Tests for cancel command group registration."""

    def test_cancel_command_group_registered(self) -> None:
        """Test that cancel command group is registered."""
        result = runner.invoke(app, ["cancel", "--help"])
        assert result.exit_code == 0
        assert "Cancel" in result.output or "cancel" in result.output

    def test_cancel_execution_help(self) -> None:
        """Test cancel execution command help."""
        result = runner.invoke(app, ["cancel", "execution", "--help"])
        assert result.exit_code == 0
        assert "execution" in result.output.lower()


class TestCancelExecutionValidation:
    """Tests for cancel execution input validation."""

    def test_both_id_and_all_shows_error(self) -> None:
        """Test that providing both ID and --all shows error."""
        result = runner.invoke(app, ["cancel", "execution", "orch_test123", "--all"])
        assert result.exit_code == 1
        assert "Cannot specify both" in result.output


class TestBareMode:
    """Tests for bare mode (no args) — interactive listing."""

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_bare_mode_lists_active_sessions(self, mock_get_es: AsyncMock) -> None:
        """Test that bare mode lists active sessions with a selection prompt."""
        from ouroboros.events.base import BaseEvent

        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        mock_es.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="orch_001",
                    data={},
                ),
            ]
        )

        tracker = _make_tracker(session_id="orch_001", status=SessionStatus.RUNNING)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))

            # User quits without selecting
            result = runner.invoke(app, ["cancel", "execution"], input="q\n")

        assert result.exit_code == 0
        assert "orch_001" in result.output

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_bare_mode_no_active_sessions(self, mock_get_es: AsyncMock) -> None:
        """Test bare mode when no active sessions exist."""
        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        mock_es.get_all_sessions = AsyncMock(return_value=[])

        result = runner.invoke(app, ["cancel", "execution"])

        assert result.exit_code == 0
        assert "No active executions" in result.output

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_bare_mode_select_and_cancel(self, mock_get_es: AsyncMock) -> None:
        """Test bare mode: user selects a session and confirms cancellation."""
        from ouroboros.events.base import BaseEvent

        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        mock_es.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="orch_001",
                    data={},
                ),
            ]
        )

        tracker = _make_tracker(session_id="orch_001", status=SessionStatus.RUNNING)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            # First call for listing, second call for cancellation
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
            mock_repo.mark_cancelled = AsyncMock(return_value=Result.ok(None))

            # User selects "1" then confirms "y"
            result = runner.invoke(app, ["cancel", "execution"], input="1\ny\n")

        assert result.exit_code == 0
        assert "Cancelled" in result.output

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_bare_mode_select_and_decline(self, mock_get_es: AsyncMock) -> None:
        """Test bare mode: user selects a session but declines confirmation."""
        from ouroboros.events.base import BaseEvent

        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        mock_es.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="orch_001",
                    data={},
                ),
            ]
        )

        tracker = _make_tracker(session_id="orch_001", status=SessionStatus.RUNNING)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))

            # User selects "1" then declines "N"
            result = runner.invoke(app, ["cancel", "execution"], input="1\nN\n")

        assert result.exit_code == 0
        assert "No executions were modified" in result.output

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_bare_mode_invalid_selection(self, mock_get_es: AsyncMock) -> None:
        """Test bare mode: user enters invalid selection."""
        from ouroboros.events.base import BaseEvent

        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        mock_es.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="orch_001",
                    data={},
                ),
            ]
        )

        tracker = _make_tracker(session_id="orch_001", status=SessionStatus.RUNNING)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))

            result = runner.invoke(app, ["cancel", "execution"], input="abc\n")

        assert result.exit_code == 1
        assert "Invalid selection" in result.output

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_bare_mode_out_of_range_selection(self, mock_get_es: AsyncMock) -> None:
        """Test bare mode: user enters out-of-range number."""
        from ouroboros.events.base import BaseEvent

        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        mock_es.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="orch_001",
                    data={},
                ),
            ]
        )

        tracker = _make_tracker(session_id="orch_001", status=SessionStatus.RUNNING)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))

            result = runner.invoke(app, ["cancel", "execution"], input="5\n")

        assert result.exit_code == 1
        assert "out of range" in result.output.lower()

    @pytest.mark.asyncio
    async def test_list_active_sessions_filters_correctly(self) -> None:
        """Test _list_active_sessions only returns running/paused sessions."""
        from ouroboros.cli.commands.cancel import _list_active_sessions
        from ouroboros.events.base import BaseEvent

        mock_es = AsyncMock()
        mock_es.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id=f"orch_00{i}",
                    data={},
                )
                for i in range(4)
            ]
        )

        trackers = [
            _make_tracker(session_id="orch_000", status=SessionStatus.RUNNING),
            _make_tracker(session_id="orch_001", status=SessionStatus.PAUSED),
            _make_tracker(session_id="orch_002", status=SessionStatus.COMPLETED),
            _make_tracker(session_id="orch_003", status=SessionStatus.CANCELLED),
        ]

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(side_effect=[Result.ok(t) for t in trackers])

            active = await _list_active_sessions(mock_es)

        assert len(active) == 2
        assert active[0].session_id == "orch_000"
        assert active[1].session_id == "orch_001"

    @pytest.mark.asyncio
    async def test_list_active_sessions_empty(self) -> None:
        """Test _list_active_sessions returns empty list when no sessions."""
        from ouroboros.cli.commands.cancel import _list_active_sessions

        mock_es = AsyncMock()
        mock_es.get_all_sessions = AsyncMock(return_value=[])

        active = await _list_active_sessions(mock_es)

        assert active == []


class TestCancelSpecificExecution:
    """Tests for cancelling a specific execution by ID."""

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_cancel_running_session(self, mock_get_es: AsyncMock) -> None:
        """Test cancelling a running session succeeds."""
        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        tracker = _make_tracker(status=SessionStatus.RUNNING)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
            mock_repo.mark_cancelled = AsyncMock(return_value=Result.ok(None))

            result = runner.invoke(app, ["cancel", "execution", "orch_test123"])

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        mock_repo.mark_cancelled.assert_called_once_with(
            session_id="orch_test123",
            reason="Cancelled by user via CLI",
            cancelled_by="user",
        )

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_cancel_paused_session(self, mock_get_es: AsyncMock) -> None:
        """Test cancelling a paused session succeeds."""
        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        tracker = _make_tracker(status=SessionStatus.PAUSED)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
            mock_repo.mark_cancelled = AsyncMock(return_value=Result.ok(None))

            result = runner.invoke(app, ["cancel", "execution", "orch_test123"])

        assert result.exit_code == 0
        assert "Cancelled" in result.output

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_cancel_nonexistent_session(self, mock_get_es: AsyncMock) -> None:
        """Test cancelling a nonexistent session shows error."""
        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(
                return_value=Result.err(
                    PersistenceError("No events found for session: orch_missing")
                )
            )

            result = runner.invoke(app, ["cancel", "execution", "orch_missing"])

        assert result.exit_code == 0  # Command itself doesn't fail, just reports
        assert "not found" in result.output.lower()

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_cancel_already_completed_session(self, mock_get_es: AsyncMock) -> None:
        """Test cancelling a completed session shows warning."""
        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        tracker = _make_tracker(status=SessionStatus.COMPLETED)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))

            result = runner.invoke(app, ["cancel", "execution", "orch_test123"])

        assert result.exit_code == 0
        assert "already completed" in result.output.lower()
        mock_repo.mark_cancelled.assert_not_called()

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_cancel_already_failed_session(self, mock_get_es: AsyncMock) -> None:
        """Test cancelling a failed session shows warning."""
        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        tracker = _make_tracker(status=SessionStatus.FAILED)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))

            result = runner.invoke(app, ["cancel", "execution", "orch_test123"])

        assert result.exit_code == 0
        assert "already failed" in result.output.lower()

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_cancel_already_cancelled_session(self, mock_get_es: AsyncMock) -> None:
        """Test cancelling an already cancelled session shows warning."""
        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        tracker = _make_tracker(status=SessionStatus.CANCELLED)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))

            result = runner.invoke(app, ["cancel", "execution", "orch_test123"])

        assert result.exit_code == 0
        assert "already cancelled" in result.output.lower()

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_cancel_with_custom_reason(self, mock_get_es: AsyncMock) -> None:
        """Test cancelling with a custom reason."""
        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        tracker = _make_tracker(status=SessionStatus.RUNNING)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
            mock_repo.mark_cancelled = AsyncMock(return_value=Result.ok(None))

            result = runner.invoke(
                app,
                ["cancel", "execution", "orch_test123", "--reason", "Stuck for 2 hours"],
            )

        assert result.exit_code == 0
        mock_repo.mark_cancelled.assert_called_once_with(
            session_id="orch_test123",
            reason="Stuck for 2 hours",
            cancelled_by="user",
        )


class TestCancelAllExecutions:
    """Tests for --all mode to cancel all running executions."""

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_cancel_all_with_running_sessions(self, mock_get_es: AsyncMock) -> None:
        """Test cancelling all running sessions."""
        from ouroboros.events.base import BaseEvent

        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        # Simulate two session start events
        mock_es.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="orch_001",
                    data={},
                ),
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="orch_002",
                    data={},
                ),
            ]
        )

        tracker_running = _make_tracker(session_id="orch_001", status=SessionStatus.RUNNING)
        tracker_completed = _make_tracker(session_id="orch_002", status=SessionStatus.COMPLETED)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(
                side_effect=[
                    Result.ok(tracker_running),
                    Result.ok(tracker_completed),
                ]
            )
            mock_repo.mark_cancelled = AsyncMock(return_value=Result.ok(None))

            result = runner.invoke(app, ["cancel", "execution", "--all"])

        assert result.exit_code == 0
        assert "Cancelled 1 execution" in result.output
        mock_repo.mark_cancelled.assert_called_once()

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_cancel_all_no_running_sessions(self, mock_get_es: AsyncMock) -> None:
        """Test --all with no running sessions found."""
        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        mock_es.get_all_sessions = AsyncMock(return_value=[])

        result = runner.invoke(app, ["cancel", "execution", "--all"])

        assert result.exit_code == 0
        assert "No running executions" in result.output

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_cancel_all_multiple_running(self, mock_get_es: AsyncMock) -> None:
        """Test --all cancels multiple running sessions."""
        from ouroboros.events.base import BaseEvent

        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        mock_es.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id=f"orch_00{i}",
                    data={},
                )
                for i in range(3)
            ]
        )

        trackers = [
            _make_tracker(session_id=f"orch_00{i}", status=SessionStatus.RUNNING) for i in range(3)
        ]

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(side_effect=[Result.ok(t) for t in trackers])
            mock_repo.mark_cancelled = AsyncMock(return_value=Result.ok(None))

            result = runner.invoke(app, ["cancel", "execution", "--all"])

        assert result.exit_code == 0
        assert "Cancelled 3 execution" in result.output
        assert mock_repo.mark_cancelled.call_count == 3

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_cancel_all_with_custom_reason(self, mock_get_es: AsyncMock) -> None:
        """Test --all with custom reason."""
        from ouroboros.events.base import BaseEvent

        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        mock_es.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="orch_001",
                    data={},
                ),
            ]
        )

        tracker = _make_tracker(session_id="orch_001", status=SessionStatus.RUNNING)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
            mock_repo.mark_cancelled = AsyncMock(return_value=Result.ok(None))

            result = runner.invoke(
                app,
                ["cancel", "execution", "--all", "--reason", "Server maintenance"],
            )

        assert result.exit_code == 0
        mock_repo.mark_cancelled.assert_called_once_with(
            session_id="orch_001",
            reason="Server maintenance",
            cancelled_by="user",
        )


class TestCancelEventStoreCleanup:
    """Tests for event store cleanup on cancel command."""

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_event_store_closed_on_success(self, mock_get_es: AsyncMock) -> None:
        """Test that event store is always closed after command."""
        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        tracker = _make_tracker(status=SessionStatus.RUNNING)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
            mock_repo.mark_cancelled = AsyncMock(return_value=Result.ok(None))

            runner.invoke(app, ["cancel", "execution", "orch_test123"])

        mock_es.close.assert_called_once()

    @patch("ouroboros.cli.commands.cancel._get_event_store")
    def test_event_store_closed_on_error(self, mock_get_es: AsyncMock) -> None:
        """Test that event store is closed even when cancel fails."""
        mock_es = AsyncMock()
        mock_get_es.return_value = mock_es

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(
                return_value=Result.err(PersistenceError("DB error"))
            )

            runner.invoke(app, ["cancel", "execution", "orch_test123"])

        mock_es.close.assert_called_once()


class TestCancelHelperFunctions:
    """Tests for internal helper functions."""

    @pytest.mark.asyncio
    async def test_cancel_session_returns_false_on_not_found(self) -> None:
        """Test _cancel_session returns False when session doesn't exist."""
        from ouroboros.cli.commands.cancel import _cancel_session

        mock_es = AsyncMock()

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(
                return_value=Result.err(PersistenceError("Not found"))
            )

            result = await _cancel_session(mock_es, "orch_missing")

        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_session_returns_true_on_success(self) -> None:
        """Test _cancel_session returns True when cancellation succeeds."""
        from ouroboros.cli.commands.cancel import _cancel_session

        mock_es = AsyncMock()
        tracker = _make_tracker(status=SessionStatus.RUNNING)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
            mock_repo.mark_cancelled = AsyncMock(return_value=Result.ok(None))

            result = await _cancel_session(mock_es, "orch_test123")

        assert result is True

    @pytest.mark.asyncio
    async def test_cancel_all_running_returns_counts(self) -> None:
        """Test _cancel_all_running returns correct cancelled/skipped counts."""
        from ouroboros.cli.commands.cancel import _cancel_all_running
        from ouroboros.events.base import BaseEvent

        mock_es = AsyncMock()
        mock_es.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="orch_run",
                    data={},
                ),
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="orch_done",
                    data={},
                ),
            ]
        )

        tracker_running = _make_tracker(session_id="orch_run", status=SessionStatus.RUNNING)
        tracker_completed = _make_tracker(session_id="orch_done", status=SessionStatus.COMPLETED)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(
                side_effect=[
                    Result.ok(tracker_running),
                    Result.ok(tracker_completed),
                ]
            )
            mock_repo.mark_cancelled = AsyncMock(return_value=Result.ok(None))

            cancelled, skipped = await _cancel_all_running(mock_es)

        assert cancelled == 1
        assert skipped == 1

    @pytest.mark.asyncio
    async def test_cancel_all_running_empty_sessions(self) -> None:
        """Test _cancel_all_running with no sessions returns zeros."""
        from ouroboros.cli.commands.cancel import _cancel_all_running

        mock_es = AsyncMock()
        mock_es.get_all_sessions = AsyncMock(return_value=[])

        cancelled, skipped = await _cancel_all_running(mock_es)

        assert cancelled == 0
        assert skipped == 0

    @pytest.mark.asyncio
    async def test_cancel_session_handles_mark_cancelled_error(self) -> None:
        """Test _cancel_session handles mark_cancelled failure gracefully."""
        from ouroboros.cli.commands.cancel import _cancel_session

        mock_es = AsyncMock()
        tracker = _make_tracker(status=SessionStatus.RUNNING)

        with patch("ouroboros.orchestrator.session.SessionRepository") as MockRepo:
            mock_repo = MockRepo.return_value
            mock_repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
            mock_repo.mark_cancelled = AsyncMock(
                return_value=Result.err(PersistenceError("DB write failed"))
            )

            result = await _cancel_session(mock_es, "orch_test123")

        assert result is False
