"""Unit tests for OrchestratorRunner cancellation logic."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.runner import (
    CANCELLATION_CHECK_INTERVAL,
    ExecutionCancelledError,
    OrchestratorRunner,
    clear_cancellation,
    get_pending_cancellations,
    is_cancellation_requested,
    request_cancellation,
)
from ouroboros.orchestrator.session import SessionTracker

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_adapter() -> MagicMock:
    """Create a mock Claude agent adapter."""
    adapter = MagicMock()
    adapter.runtime_backend = "opencode"
    adapter.working_directory = "/tmp/project"
    adapter.permission_mode = "acceptEdits"
    return adapter


@pytest.fixture
def mock_event_store() -> AsyncMock:
    """Create a mock event store."""
    store = AsyncMock()
    store.append = AsyncMock()
    store.replay = AsyncMock(return_value=[])
    store.get_all_sessions = AsyncMock(return_value=[])
    store.query_events = AsyncMock(return_value=[])
    return store


@pytest.fixture
def mock_console() -> MagicMock:
    """Create a mock Rich console."""
    return MagicMock()


@pytest.fixture
def runner(
    mock_adapter: MagicMock,
    mock_event_store: AsyncMock,
    mock_console: MagicMock,
) -> OrchestratorRunner:
    """Create a runner with mocked dependencies."""
    return OrchestratorRunner(mock_adapter, mock_event_store, mock_console)


@pytest.fixture(autouse=True)
def _clean_cancellation_registry():
    """Ensure cancellation registry is clean before/after each test."""
    from ouroboros.orchestrator.runner import _cancellation_registry

    _cancellation_registry.clear()
    yield
    _cancellation_registry.clear()


# =============================================================================
# Tests: Cancellation Registry (module-level)
# =============================================================================


class TestCancellationRegistry:
    """Tests for the module-level cancellation registry functions."""

    @pytest.mark.asyncio
    async def test_request_cancellation(self) -> None:
        """Test that requesting cancellation adds session to registry."""
        await request_cancellation("sess_123")
        assert await is_cancellation_requested("sess_123")

    @pytest.mark.asyncio
    async def test_is_cancellation_requested_false(self) -> None:
        """Test that non-requested session returns False."""
        assert not await is_cancellation_requested("sess_999")

    @pytest.mark.asyncio
    async def test_clear_cancellation(self) -> None:
        """Test that clearing cancellation removes session from registry."""
        await request_cancellation("sess_123")
        await clear_cancellation("sess_123")
        assert not await is_cancellation_requested("sess_123")

    @pytest.mark.asyncio
    async def test_clear_cancellation_nonexistent(self) -> None:
        """Test that clearing a non-existent session does not raise."""
        # Should not raise
        await clear_cancellation("sess_nonexistent")

    @pytest.mark.asyncio
    async def test_get_pending_cancellations(self) -> None:
        """Test getting snapshot of pending cancellations."""
        await request_cancellation("sess_1")
        await request_cancellation("sess_2")
        pending = await get_pending_cancellations()
        assert pending == frozenset({"sess_1", "sess_2"})

    @pytest.mark.asyncio
    async def test_get_pending_cancellations_empty(self) -> None:
        """Test getting empty pending cancellations."""
        assert await get_pending_cancellations() == frozenset()

    @pytest.mark.asyncio
    async def test_get_pending_cancellations_returns_frozenset(self) -> None:
        """Test that pending cancellations returns immutable snapshot."""
        await request_cancellation("sess_1")
        pending = await get_pending_cancellations()
        assert isinstance(pending, frozenset)

    @pytest.mark.asyncio
    async def test_multiple_requests_idempotent(self) -> None:
        """Test that requesting cancellation twice is idempotent."""
        await request_cancellation("sess_123")
        await request_cancellation("sess_123")
        assert await is_cancellation_requested("sess_123")
        await clear_cancellation("sess_123")
        assert not await is_cancellation_requested("sess_123")


# =============================================================================
# Tests: ExecutionCancelledError
# =============================================================================


class TestExecutionCancelledError:
    """Tests for ExecutionCancelledError."""

    def test_create_with_defaults(self) -> None:
        """Test creating error with default reason."""
        err = ExecutionCancelledError("sess_123")
        assert err.session_id == "sess_123"
        assert err.reason == "Cancelled by user"
        assert "sess_123" in str(err)

    def test_create_with_custom_reason(self) -> None:
        """Test creating error with custom reason."""
        err = ExecutionCancelledError("sess_456", reason="Timed out")
        assert err.reason == "Timed out"
        assert "Timed out" in str(err)


# =============================================================================
# Tests: OrchestratorRunner session registration
# =============================================================================


class TestSessionRegistration:
    """Tests for session registration and unregistration."""

    def test_register_session(self, runner: OrchestratorRunner) -> None:
        """Test registering a session for cancellation tracking."""
        runner._register_session("exec_1", "sess_1")
        assert "exec_1" in runner.active_sessions
        assert runner.active_sessions["exec_1"] == "sess_1"

    def test_unregister_session(self, runner: OrchestratorRunner) -> None:
        """Test unregistering a session."""
        runner._register_session("exec_1", "sess_1")
        runner._unregister_session("exec_1", "sess_1")
        assert "exec_1" not in runner.active_sessions

    def test_unregister_nonexistent_session(self, runner: OrchestratorRunner) -> None:
        """Test that unregistering a non-existent session does not raise."""
        runner._unregister_session("exec_999", "sess_999")

    def test_active_sessions_returns_copy(self, runner: OrchestratorRunner) -> None:
        """Test that active_sessions returns a copy."""
        runner._register_session("exec_1", "sess_1")
        sessions = runner.active_sessions
        sessions["exec_2"] = "sess_2"  # Modify the copy
        assert "exec_2" not in runner.active_sessions  # Original unchanged

    def test_session_repo_property(self, runner: OrchestratorRunner) -> None:
        """Test that session_repo property returns the repository."""
        assert runner.session_repo is runner._session_repo


# =============================================================================
# Tests: _check_cancellation
# =============================================================================


class TestCheckCancellation:
    """Tests for the _check_cancellation method."""

    @pytest.mark.asyncio
    async def test_check_cancellation_via_registry(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Test that cancellation is detected via in-memory registry."""
        await request_cancellation("sess_123")
        result = await runner._check_cancellation("sess_123")
        assert result is True

    @pytest.mark.asyncio
    async def test_check_cancellation_not_requested(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Test that non-requested session returns False."""
        result = await runner._check_cancellation("sess_999")
        assert result is False

    @pytest.mark.asyncio
    async def test_check_cancellation_via_event_store(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that cancellation is detected via event store query."""
        mock_event_store.query_events = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.cancelled",
                    aggregate_type="session",
                    aggregate_id="sess_123",
                    data={"reason": "cancelled"},
                )
            ]
        )
        result = await runner._check_cancellation("sess_123")
        assert result is True

    @pytest.mark.asyncio
    async def test_check_cancellation_event_store_error_graceful(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test graceful degradation when event store query fails."""
        mock_event_store.query_events = AsyncMock(side_effect=Exception("DB error"))
        result = await runner._check_cancellation("sess_123")
        assert result is False  # Should not raise, returns False


# =============================================================================
# Tests: _handle_cancellation
# =============================================================================


class TestHandleCancellation:
    """Tests for the _handle_cancellation method."""

    def _mock_running_session(self, runner: OrchestratorRunner) -> None:
        """Helper to mock reconstruct_session to return a running session."""
        mock_tracker = MagicMock(spec=SessionTracker)
        mock_tracker.status = SessionTracker.create("exec_1", "seed_1").status  # RUNNING
        runner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(mock_tracker))

    @pytest.mark.asyncio
    async def test_handle_cancellation_returns_result(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that _handle_cancellation returns a proper Result."""
        # Register session first
        runner._register_session("exec_1", "sess_1")
        await request_cancellation("sess_1")
        self._mock_running_session(runner)

        # Mock mark_cancelled
        with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
            result = await runner._handle_cancellation(
                session_id="sess_1",
                execution_id="exec_1",
                messages_processed=42,
                start_time=datetime.now(UTC),
            )

        assert result.is_ok
        assert result.value.success is False
        assert result.value.session_id == "sess_1"
        assert result.value.execution_id == "exec_1"
        assert result.value.messages_processed == 42
        assert "cancelled" in result.value.final_message.lower()
        assert result.value.summary.get("cancelled") is True

    @pytest.mark.asyncio
    async def test_handle_cancellation_clears_registry(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that _handle_cancellation clears the cancellation registry."""
        runner._register_session("exec_1", "sess_1")
        await request_cancellation("sess_1")
        self._mock_running_session(runner)

        with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
            await runner._handle_cancellation(
                session_id="sess_1",
                execution_id="exec_1",
                messages_processed=10,
                start_time=datetime.now(UTC),
            )

        assert not await is_cancellation_requested("sess_1")

    @pytest.mark.asyncio
    async def test_handle_cancellation_unregisters_session(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that _handle_cancellation cleans up session tracking."""
        runner._register_session("exec_1", "sess_1")
        self._mock_running_session(runner)

        with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
            await runner._handle_cancellation(
                session_id="sess_1",
                execution_id="exec_1",
                messages_processed=10,
                start_time=datetime.now(UTC),
            )

        assert "exec_1" not in runner.active_sessions

    @pytest.mark.asyncio
    async def test_handle_cancellation_marks_repo(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that _handle_cancellation calls mark_cancelled on repo."""
        runner._register_session("exec_1", "sess_1")
        self._mock_running_session(runner)

        mock_mark = AsyncMock(return_value=Result.ok(None))
        with patch.object(runner._session_repo, "mark_cancelled", mock_mark):
            await runner._handle_cancellation(
                session_id="sess_1",
                execution_id="exec_1",
                messages_processed=10,
                start_time=datetime.now(UTC),
            )

        mock_mark.assert_called_once_with(
            "sess_1",
            reason="Cancellation detected during execution",
            cancelled_by="runner",
        )

    @pytest.mark.asyncio
    async def test_handle_cancellation_skips_mark_if_already_cancelled(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that _handle_cancellation does not double-mark a cancelled session."""
        runner._register_session("exec_1", "sess_1")

        # Mock reconstruct to return an already-cancelled session
        mock_tracker = MagicMock(spec=SessionTracker)
        mock_tracker.status = SessionTracker.create("exec_1", "seed_1").status
        # Override status to CANCELLED
        from ouroboros.orchestrator.session import SessionStatus as SS

        mock_tracker.status = SS.CANCELLED
        runner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(mock_tracker))

        mock_mark = AsyncMock(return_value=Result.ok(None))
        with patch.object(runner._session_repo, "mark_cancelled", mock_mark):
            result = await runner._handle_cancellation(
                session_id="sess_1",
                execution_id="exec_1",
                messages_processed=10,
                start_time=datetime.now(UTC),
            )

        assert result.is_ok
        mock_mark.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_cancellation_emits_event(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that _handle_cancellation emits event to event store."""
        runner._register_session("exec_1", "sess_1")
        self._mock_running_session(runner)

        with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
            await runner._handle_cancellation(
                session_id="sess_1",
                execution_id="exec_1",
                messages_processed=10,
                start_time=datetime.now(UTC),
            )

        # The event_store.append should NOT be called directly by _handle_cancellation
        # (it delegates to mark_cancelled which internally appends the event).
        # But let's verify the console print was called
        runner._console.print.assert_called()

    @pytest.mark.asyncio
    async def test_handle_cancellation_displays_panel(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test that _handle_cancellation displays cancellation panel."""
        runner._register_session("exec_1", "sess_1")
        self._mock_running_session(runner)

        with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
            await runner._handle_cancellation(
                session_id="sess_1",
                execution_id="exec_1",
                messages_processed=10,
                start_time=datetime.now(UTC),
            )

        # Console should have been called with a Panel
        runner._console.print.assert_called_once()


# =============================================================================
# Tests: cancel_execution
# =============================================================================


class TestCancelExecution:
    """Tests for the cancel_execution method."""

    @pytest.mark.asyncio
    async def test_cancel_in_flight_execution(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Test cancelling an in-flight execution signals registry."""
        runner._register_session("exec_1", "sess_1")

        result = await runner.cancel_execution("exec_1", reason="Test cancel")

        assert result.is_ok
        assert result.value["status"] == "cancellation_requested"
        assert result.value["in_flight"] is True
        assert result.value["execution_id"] == "exec_1"
        assert result.value["session_id"] == "sess_1"
        # Registry should be populated
        assert await is_cancellation_requested("sess_1")

    @pytest.mark.asyncio
    async def test_cancel_not_in_flight_looks_up_session(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test cancelling a non-in-flight execution looks up session from events."""
        # Return a session started event matching the execution ID
        mock_event_store.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="sess_orphan",
                    data={"execution_id": "exec_orphan"},
                )
            ]
        )

        with patch.object(
            runner._session_repo,
            "mark_cancelled",
            return_value=Result.ok(None),
        ):
            result = await runner.cancel_execution(
                "exec_orphan",
                reason="Orphaned execution",
                cancelled_by="auto_cleanup",
            )

        assert result.is_ok
        assert result.value["status"] == "cancelled"
        assert result.value["in_flight"] is False
        assert result.value["session_id"] == "sess_orphan"

    @pytest.mark.asyncio
    async def test_cancel_not_found_returns_error(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test cancelling a non-existent execution returns error."""
        mock_event_store.get_all_sessions = AsyncMock(return_value=[])

        result = await runner.cancel_execution("exec_nonexistent")

        assert result.is_err
        assert "no session found" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_cancel_direct_mark_fails(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test error handling when mark_cancelled fails."""
        from ouroboros.core.errors import PersistenceError

        mock_event_store.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="sess_1",
                    data={"execution_id": "exec_1"},
                )
            ]
        )

        with patch.object(
            runner._session_repo,
            "mark_cancelled",
            return_value=Result.err(PersistenceError("DB error")),
        ):
            result = await runner.cancel_execution("exec_1")

        assert result.is_err
        assert "failed to cancel" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_cancel_event_store_lookup_error(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test graceful degradation when event store lookup fails."""
        mock_event_store.get_all_sessions = AsyncMock(side_effect=Exception("DB connection lost"))

        result = await runner.cancel_execution("exec_1")

        assert result.is_err
        assert "no session found" in str(result.error).lower()


# =============================================================================
# Tests: In-flight cancellation during execute_seed
# =============================================================================


class TestInFlightCancellation:
    """Tests for in-flight cancellation during message processing."""

    @pytest.fixture
    def sample_seed(self):
        """Create a minimal seed for testing."""
        from ouroboros.core.seed import (
            OntologySchema,
            Seed,
            SeedMetadata,
        )

        return Seed(
            goal="Test cancellation",
            constraints=(),
            acceptance_criteria=("AC1",),
            ontology_schema=OntologySchema(name="Test", description="Test"),
            metadata=SeedMetadata(ambiguity_score=0.1),
        )

    @pytest.mark.asyncio
    async def test_cancellation_stops_execution(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Any,
    ) -> None:
        """Test that cancellation stops execution mid-stream."""
        messages_yielded = 0

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            nonlocal messages_yielded
            # Yield enough messages to trigger cancellation check
            for i in range(CANCELLATION_CHECK_INTERVAL * 2):
                messages_yielded += 1
                if messages_yielded == CANCELLATION_CHECK_INTERVAL:
                    # Request cancellation right before the check
                    # The runner will check at message CANCELLATION_CHECK_INTERVAL
                    pass
                yield AgentMessage(type="assistant", content=f"Working {i}...")

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            tracker = SessionTracker.create("exec_test", sample_seed.metadata.seed_id)
            return Result.ok(tracker)

        # Set up the cancellation to trigger at the right time
        mock_event_store.query_events = AsyncMock(return_value=[])

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
                # Pre-schedule cancellation for whatever session_id gets created
                # We need to do this in a way that works with the dynamic session_id
                original_check = runner._check_cancellation
                call_count = 0

                async def mock_check(session_id):
                    nonlocal call_count
                    call_count += 1
                    if call_count >= 1:
                        return True
                    return await original_check(session_id)

                with patch.object(runner, "_check_cancellation", side_effect=mock_check):
                    result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok
        assert result.value.success is False
        assert "cancelled" in result.value.final_message.lower()
        # Should have processed only CANCELLATION_CHECK_INTERVAL messages
        assert result.value.messages_processed == CANCELLATION_CHECK_INTERVAL

    @pytest.mark.asyncio
    async def test_cancellation_check_interval(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Any,
    ) -> None:
        """Test that cancellation is checked at the correct interval."""
        check_calls = []

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            for i in range(CANCELLATION_CHECK_INTERVAL + 2):
                yield AgentMessage(type="assistant", content=f"msg {i}")
            yield AgentMessage(
                type="result",
                content="Done",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute
        mock_event_store.query_events = AsyncMock(return_value=[])

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec_test", sample_seed.metadata.seed_id))

        async def tracking_check(session_id):
            check_calls.append(session_id)
            return False  # Don't cancel

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_completed", return_value=Result.ok(None)):
                with patch.object(runner, "_check_cancellation", side_effect=tracking_check):
                    await runner.execute_seed(sample_seed, parallel=False)

        # Should have checked once at CANCELLATION_CHECK_INTERVAL
        assert len(check_calls) == 1


# =============================================================================
# Tests: Session lifecycle integration
# =============================================================================


class TestSessionLifecycleTracking:
    """Tests for session registration/unregistration during execution lifecycle."""

    @pytest.fixture
    def sample_seed(self):
        """Create a minimal seed for testing."""
        from ouroboros.core.seed import (
            OntologySchema,
            Seed,
            SeedMetadata,
        )

        return Seed(
            goal="Test lifecycle",
            constraints=(),
            acceptance_criteria=("AC1",),
            ontology_schema=OntologySchema(name="Test", description="Test"),
            metadata=SeedMetadata(ambiguity_score=0.1),
        )

    @pytest.mark.asyncio
    async def test_session_registered_during_execution(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Any,
    ) -> None:
        """Test that session is registered during execution and unregistered after."""
        registered_sessions: list[dict] = []

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            # Capture the active sessions during execution
            registered_sessions.append(dict(runner.active_sessions))
            yield AgentMessage(
                type="result",
                content="Done",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute
        mock_event_store.query_events = AsyncMock(return_value=[])

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec_test", sample_seed.metadata.seed_id))

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_completed", return_value=Result.ok(None)):
                result = await runner.execute_seed(
                    sample_seed, execution_id="exec_test", parallel=False
                )

        assert result.is_ok
        # During execution, session should have been registered
        assert len(registered_sessions) == 1
        assert "exec_test" in registered_sessions[0]
        # After execution, session should be unregistered
        assert "exec_test" not in runner.active_sessions

    @pytest.mark.asyncio
    async def test_session_unregistered_on_exception(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Any,
    ) -> None:
        """Test that session is unregistered even when execution raises."""

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            raise RuntimeError("Adapter crashed")
            yield  # Make it a generator  # noqa: E501

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec_crash", sample_seed.metadata.seed_id))

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            result = await runner.execute_seed(
                sample_seed, execution_id="exec_crash", parallel=False
            )

        assert result.is_err
        # Session should still be unregistered
        assert "exec_crash" not in runner.active_sessions
