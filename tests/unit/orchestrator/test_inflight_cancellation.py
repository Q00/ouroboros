"""Unit tests for graceful in-flight cancellation logic.

Tests cover:
- Cancellation signal handling (registry propagation, concurrent access, dual-path detection)
- Cleanup behavior (state consistency, registry clearing, session unregistration)
- Timeout/error scenarios (mark_cancelled failures, event store errors during cancellation)
- Edge cases (cancellation at boundary, double cancellation, cancellation after completion)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.errors import PersistenceError
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


@pytest.fixture
def sample_seed():
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


# =============================================================================
# Tests: Cancellation Signal Handling
# =============================================================================


class TestCancellationSignalHandling:
    """Tests for cancellation signal propagation and detection."""

    @pytest.mark.asyncio
    async def test_cancel_execution_signals_in_memory_registry(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Cancelling an in-flight execution populates the registry immediately."""
        runner._register_session("exec_1", "sess_1")
        await runner.cancel_execution("exec_1", reason="User cancelled")

        assert await is_cancellation_requested("sess_1")
        # Registry should contain exactly the one session
        assert await get_pending_cancellations() == frozenset({"sess_1"})

    @pytest.mark.asyncio
    async def test_check_cancellation_fast_path_no_io(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Fast path (in-memory registry) should NOT query event store."""
        await request_cancellation("sess_fast")
        result = await runner._check_cancellation("sess_fast")

        assert result is True
        # Event store should not have been queried since fast path hit
        mock_event_store.query_events.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_cancellation_slow_path_queries_event_store(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Slow path queries event store when registry is empty."""
        mock_event_store.query_events = AsyncMock(return_value=[])
        result = await runner._check_cancellation("sess_slow")

        assert result is False
        mock_event_store.query_events.assert_called_once_with(
            aggregate_id="sess_slow",
            event_type="orchestrator.session.cancelled",
            limit=1,
        )

    @pytest.mark.asyncio
    async def test_check_cancellation_slow_path_detects_persisted_event(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Slow path detects cancellation events persisted by external process (e.g., CLI)."""
        mock_event_store.query_events = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.cancelled",
                    aggregate_type="session",
                    aggregate_id="sess_ext",
                    data={"reason": "CLI cancel", "cancelled_by": "user"},
                )
            ]
        )
        result = await runner._check_cancellation("sess_ext")
        assert result is True

    @pytest.mark.asyncio
    async def test_concurrent_cancel_and_check(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Cancellation request during a concurrent check is detected."""
        runner._register_session("exec_c", "sess_c")

        # Simulate: cancel fires while check is pending
        async def delayed_cancel():
            await asyncio.sleep(0.01)
            await request_cancellation("sess_c")

        asyncio.create_task(delayed_cancel())

        # Give the cancel a moment to fire
        await asyncio.sleep(0.02)
        result = await runner._check_cancellation("sess_c")
        assert result is True

    @pytest.mark.asyncio
    async def test_cancel_execution_returns_cancellation_requested_status(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """In-flight cancel returns status='cancellation_requested' without blocking."""
        runner._register_session("exec_1", "sess_1")
        result = await runner.cancel_execution("exec_1", reason="Test")

        assert result.is_ok
        assert result.value["status"] == "cancellation_requested"
        assert result.value["in_flight"] is True
        assert result.value["reason"] == "Test"

    @pytest.mark.asyncio
    async def test_cancel_execution_preserves_reason_and_metadata(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Cancellation preserves the reason and execution metadata."""
        runner._register_session("exec_meta", "sess_meta")
        result = await runner.cancel_execution(
            "exec_meta", reason="Timed out after 30m", cancelled_by="auto_cleanup"
        )

        assert result.is_ok
        assert result.value["execution_id"] == "exec_meta"
        assert result.value["session_id"] == "sess_meta"
        assert result.value["reason"] == "Timed out after 30m"


# =============================================================================
# Tests: Cleanup Behavior
# =============================================================================


class TestCancellationCleanup:
    """Tests for state cleanup during and after cancellation handling."""

    def _mock_running_session(self, runner: OrchestratorRunner, session_id: str) -> None:
        """Mock reconstruct_session to return a RUNNING session."""
        from ouroboros.orchestrator.session import SessionStatus

        mock_tracker = MagicMock(spec=SessionTracker)
        mock_tracker.status = SessionStatus.RUNNING
        runner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(mock_tracker))

    @pytest.mark.asyncio
    async def test_handle_cancellation_clears_all_state(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """_handle_cancellation clears registry, unregisters session, and marks repo."""
        runner._register_session("exec_clean", "sess_clean")
        await request_cancellation("sess_clean")
        self._mock_running_session(runner, "sess_clean")

        with patch.object(
            runner._session_repo, "mark_cancelled", return_value=Result.ok(None)
        ) as mock_mark:
            await runner._handle_cancellation(
                session_id="sess_clean",
                execution_id="exec_clean",
                messages_processed=15,
                start_time=datetime.now(UTC),
            )

        # All three cleanup steps should have happened
        assert not await is_cancellation_requested("sess_clean")
        assert "exec_clean" not in runner.active_sessions
        mock_mark.assert_called_once_with(
            "sess_clean",
            reason="Cancellation detected during execution",
            cancelled_by="runner",
        )

    @pytest.mark.asyncio
    async def test_handle_cancellation_idempotent_registry_clear(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Calling _handle_cancellation when registry is already clear does not raise."""
        runner._register_session("exec_idem", "sess_idem")
        self._mock_running_session(runner, "sess_idem")
        # Don't request_cancellation — registry is already empty for this session

        with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
            result = await runner._handle_cancellation(
                session_id="sess_idem",
                execution_id="exec_idem",
                messages_processed=5,
                start_time=datetime.now(UTC),
            )

        assert result.is_ok
        assert not await is_cancellation_requested("sess_idem")

    @pytest.mark.asyncio
    async def test_handle_cancellation_result_has_correct_message_count(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Cancellation result accurately reports messages processed before stopping."""
        runner._register_session("exec_cnt", "sess_cnt")
        self._mock_running_session(runner, "sess_cnt")

        with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
            result = await runner._handle_cancellation(
                session_id="sess_cnt",
                execution_id="exec_cnt",
                messages_processed=42,
                start_time=datetime.now(UTC),
            )

        assert result.value.messages_processed == 42
        assert result.value.success is False
        assert result.value.summary == {"cancelled": True}

    @pytest.mark.asyncio
    async def test_handle_cancellation_duration_is_positive(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Cancellation result has a positive duration."""
        runner._register_session("exec_dur", "sess_dur")
        self._mock_running_session(runner, "sess_dur")
        start = datetime.now(UTC)

        with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
            result = await runner._handle_cancellation(
                session_id="sess_dur",
                execution_id="exec_dur",
                messages_processed=1,
                start_time=start,
            )

        assert result.value.duration_seconds >= 0

    @pytest.mark.asyncio
    async def test_double_cancel_execution_is_safe(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Calling cancel_execution twice on same execution is safe and idempotent."""
        runner._register_session("exec_dbl", "sess_dbl")

        result1 = await runner.cancel_execution("exec_dbl", reason="First cancel")
        result2 = await runner.cancel_execution("exec_dbl", reason="Second cancel")

        assert result1.is_ok
        assert result2.is_ok
        # Both should succeed; registry should still have the session
        assert await is_cancellation_requested("sess_dbl")

    @pytest.mark.asyncio
    async def test_unregister_after_cancel_prevents_second_inflight_cancel(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """After handle_cancellation unregisters, a second cancel goes to direct path."""
        runner._register_session("exec_unreg", "sess_unreg")
        self._mock_running_session(runner, "sess_unreg")

        with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
            await runner._handle_cancellation(
                session_id="sess_unreg",
                execution_id="exec_unreg",
                messages_processed=5,
                start_time=datetime.now(UTC),
            )

        # Session is now unregistered, so cancel_execution should take direct path
        assert "exec_unreg" not in runner.active_sessions

        # Second cancel goes to _cancel_session_directly (not in-flight)
        mock_event_store.get_all_sessions = AsyncMock(return_value=[])
        result = await runner.cancel_execution("exec_unreg")
        assert result.is_err  # No session found in get_all_sessions (mock returns [])


# =============================================================================
# Tests: Error Scenarios During Cancellation
# =============================================================================


class TestCancellationErrorScenarios:
    """Tests for graceful degradation when errors occur during cancellation."""

    def _mock_running_session(self, runner: OrchestratorRunner, session_id: str) -> None:
        """Mock reconstruct_session to return a RUNNING session."""
        from ouroboros.orchestrator.session import SessionStatus

        mock_tracker = MagicMock(spec=SessionTracker)
        mock_tracker.status = SessionStatus.RUNNING
        runner._session_repo.reconstruct_session = AsyncMock(return_value=Result.ok(mock_tracker))

    @pytest.mark.asyncio
    async def test_check_cancellation_event_store_timeout_returns_false(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Event store timeout during check should not crash execution."""
        mock_event_store.query_events = AsyncMock(side_effect=TimeoutError("Connection timed out"))
        result = await runner._check_cancellation("sess_timeout")
        assert result is False  # Graceful: don't cancel if unsure

    @pytest.mark.asyncio
    async def test_check_cancellation_connection_error_returns_false(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Database connection error during check should not crash execution."""
        mock_event_store.query_events = AsyncMock(
            side_effect=ConnectionError("Database unavailable")
        )
        result = await runner._check_cancellation("sess_conn")
        assert result is False

    @pytest.mark.asyncio
    async def test_handle_cancellation_mark_failed_still_returns_result(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """If mark_cancelled fails during handling, the result is still returned.

        The _handle_cancellation method logs a warning but still returns an
        OrchestratorResult. This ensures the execution stops even if persistence fails.
        """
        runner._register_session("exec_fail", "sess_fail")
        await request_cancellation("sess_fail")
        self._mock_running_session(runner, "sess_fail")

        with (
            patch.object(
                runner._session_repo,
                "mark_cancelled",
                return_value=Result.err(PersistenceError("DB write failed")),
            ),
            patch("ouroboros.orchestrator.runner.log") as mock_log,
        ):
            result = await runner._handle_cancellation(
                session_id="sess_fail",
                execution_id="exec_fail",
                messages_processed=7,
                start_time=datetime.now(UTC),
            )

            # Should log a warning about the failed mark_cancelled
            mock_log.warning.assert_any_call(
                "orchestrator.runner.mark_cancelled_failed",
                session_id="sess_fail",
                error=str(PersistenceError("DB write failed")),
            )

        # Should still return a valid cancellation result
        assert result.is_ok
        assert result.value.success is False
        assert result.value.messages_processed == 7
        # Cleanup should still happen even if persistence failed
        assert not await is_cancellation_requested("sess_fail")
        assert "exec_fail" not in runner.active_sessions

    @pytest.mark.asyncio
    async def test_handle_cancellation_mark_raises_still_propagates(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """If mark_cancelled raises an exception, it propagates (not swallowed).

        Unlike _check_cancellation which degrades gracefully, _handle_cancellation
        does NOT catch exceptions from mark_cancelled because the execution is
        already stopping.
        """
        runner._register_session("exec_raise", "sess_raise")
        self._mock_running_session(runner, "sess_raise")

        with patch.object(
            runner._session_repo,
            "mark_cancelled",
            side_effect=RuntimeError("Unexpected DB crash"),
        ):
            with pytest.raises(RuntimeError, match="Unexpected DB crash"):
                await runner._handle_cancellation(
                    session_id="sess_raise",
                    execution_id="exec_raise",
                    messages_processed=3,
                    start_time=datetime.now(UTC),
                )

        # Despite the exception, registry and session should have been
        # cleaned up before mark_cancelled was called
        assert not await is_cancellation_requested("sess_raise")
        assert "exec_raise" not in runner.active_sessions

    @pytest.mark.asyncio
    async def test_cancel_session_directly_event_store_replay_error(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Direct cancellation gracefully handles event store get_all_sessions errors."""
        mock_event_store.get_all_sessions = AsyncMock(side_effect=Exception("DB connection lost"))

        result = await runner.cancel_execution("exec_lost")
        assert result.is_err
        assert "no session found" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_cancel_session_directly_mark_cancelled_fails(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Direct cancellation reports error when mark_cancelled fails."""
        mock_event_store.get_all_sessions = AsyncMock(
            return_value=[
                BaseEvent(
                    type="orchestrator.session.started",
                    aggregate_type="session",
                    aggregate_id="sess_dfail",
                    data={"execution_id": "exec_dfail"},
                )
            ]
        )

        with patch.object(
            runner._session_repo,
            "mark_cancelled",
            return_value=Result.err(PersistenceError("Write failed")),
        ):
            result = await runner.cancel_execution("exec_dfail")

        assert result.is_err
        assert "failed to cancel" in str(result.error).lower()


# =============================================================================
# Tests: In-Flight Cancellation During Execution
# =============================================================================


class TestInFlightCancellationGraceful:
    """Tests for graceful in-flight cancellation during execute_seed."""

    @pytest.mark.asyncio
    async def test_cancellation_at_exact_check_interval(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Any,
    ) -> None:
        """Cancellation detected exactly at CANCELLATION_CHECK_INTERVAL boundary."""

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            for i in range(CANCELLATION_CHECK_INTERVAL * 3):
                yield AgentMessage(type="assistant", content=f"msg {i}")

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec_boundary", sample_seed.metadata.seed_id))

        call_count = 0

        async def mock_check(session_id):
            nonlocal call_count
            call_count += 1
            return call_count == 1  # Cancel on first check

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
                with patch.object(runner, "_check_cancellation", side_effect=mock_check):
                    result = await runner.execute_seed(
                        sample_seed, execution_id="exec_boundary", parallel=False
                    )

        assert result.is_ok
        assert result.value.success is False
        assert result.value.messages_processed == CANCELLATION_CHECK_INTERVAL
        assert "cancelled" in result.value.final_message.lower()

    @pytest.mark.asyncio
    async def test_no_cancellation_allows_full_execution(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Any,
    ) -> None:
        """Without cancellation, execution completes normally."""
        total_messages = CANCELLATION_CHECK_INTERVAL + 3

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            for i in range(total_messages - 1):
                yield AgentMessage(type="assistant", content=f"msg {i}")
            yield AgentMessage(type="result", content="Done", data={"subtype": "success"})

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec_full", sample_seed.metadata.seed_id))

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_completed", return_value=Result.ok(None)):
                with patch.object(runner, "_check_cancellation", return_value=False):
                    result = await runner.execute_seed(
                        sample_seed, execution_id="exec_full", parallel=False
                    )

        assert result.is_ok
        assert result.value.success is True
        assert result.value.messages_processed == total_messages

    @pytest.mark.asyncio
    async def test_cancellation_after_first_interval_not_before(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Any,
    ) -> None:
        """Cancellation is only checked at interval boundaries, not every message."""
        check_points: list[int] = []

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            for i in range(CANCELLATION_CHECK_INTERVAL * 2 + 1):
                yield AgentMessage(type="assistant", content=f"msg {i}")
            yield AgentMessage(type="result", content="Done", data={"subtype": "success"})

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec_int", sample_seed.metadata.seed_id))

        async def tracking_check(session_id):
            check_points.append(len(check_points) + 1)
            return False

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_completed", return_value=Result.ok(None)):
                with patch.object(runner, "_check_cancellation", side_effect=tracking_check):
                    await runner.execute_seed(sample_seed, execution_id="exec_int", parallel=False)

        # Total messages = CANCELLATION_CHECK_INTERVAL * 2 + 2 (including result)
        # Checks happen at multiples of CANCELLATION_CHECK_INTERVAL
        expected_checks = (CANCELLATION_CHECK_INTERVAL * 2 + 2) // CANCELLATION_CHECK_INTERVAL
        assert len(check_points) == expected_checks

    @pytest.mark.asyncio
    async def test_session_unregistered_after_cancelled_execution(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Any,
    ) -> None:
        """Session tracking is cleaned up after cancellation stops execution."""

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            for i in range(CANCELLATION_CHECK_INTERVAL + 1):
                yield AgentMessage(type="assistant", content=f"msg {i}")

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec_unreg", sample_seed.metadata.seed_id))

        async def mock_check(session_id):
            return True  # Always cancel

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
                with patch.object(runner, "_check_cancellation", side_effect=mock_check):
                    await runner.execute_seed(
                        sample_seed, execution_id="exec_unreg", parallel=False
                    )

        assert "exec_unreg" not in runner.active_sessions

    @pytest.mark.asyncio
    async def test_cancellation_mid_stream_preserves_partial_results(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Any,
    ) -> None:
        """Messages processed before cancellation are counted in the result."""
        target_count = CANCELLATION_CHECK_INTERVAL * 2

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            for i in range(CANCELLATION_CHECK_INTERVAL * 5):
                yield AgentMessage(type="assistant", content=f"msg {i}")

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec_partial", sample_seed.metadata.seed_id))

        call_count = 0

        async def mock_check(session_id):
            nonlocal call_count
            call_count += 1
            return call_count == 2  # Cancel on second check

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_cancelled", return_value=Result.ok(None)):
                with patch.object(runner, "_check_cancellation", side_effect=mock_check):
                    result = await runner.execute_seed(
                        sample_seed, execution_id="exec_partial", parallel=False
                    )

        assert result.is_ok
        assert result.value.messages_processed == target_count


# =============================================================================
# Tests: Cancellation Registry Thread Safety
# =============================================================================


class TestCancellationRegistryConcurrency:
    """Tests for cancellation registry behavior under concurrent access patterns."""

    @pytest.mark.asyncio
    async def test_request_and_clear_different_sessions_independent(self) -> None:
        """Clearing one session doesn't affect another."""
        await request_cancellation("sess_a")
        await request_cancellation("sess_b")
        await clear_cancellation("sess_a")

        assert not await is_cancellation_requested("sess_a")
        assert await is_cancellation_requested("sess_b")

    @pytest.mark.asyncio
    async def test_pending_cancellations_snapshot_is_immutable(self) -> None:
        """Modifying the registry after getting pending doesn't change the snapshot."""
        await request_cancellation("sess_snap")
        snapshot = await get_pending_cancellations()
        await request_cancellation("sess_snap_2")

        assert "sess_snap_2" not in snapshot
        assert "sess_snap_2" in await get_pending_cancellations()

    @pytest.mark.asyncio
    async def test_multiple_sessions_cancel_independently(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Multiple active sessions can be cancelled independently."""
        runner._register_session("exec_1", "sess_1")
        runner._register_session("exec_2", "sess_2")
        runner._register_session("exec_3", "sess_3")

        # Cancel only sess_2
        await runner.cancel_execution("exec_2", reason="Cancel middle")

        assert not await is_cancellation_requested("sess_1")
        assert await is_cancellation_requested("sess_2")
        assert not await is_cancellation_requested("sess_3")

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_execution_returns_error(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Cancelling an execution that doesn't exist returns an error."""
        mock_event_store.replay = AsyncMock(return_value=[])
        result = await runner.cancel_execution("exec_ghost")

        assert result.is_err
        assert "no session found" in str(result.error).lower()


# =============================================================================
# Tests: ExecutionCancelledError
# =============================================================================


class TestExecutionCancelledErrorExtended:
    """Extended tests for ExecutionCancelledError construction and usage."""

    def test_error_is_exception(self) -> None:
        """ExecutionCancelledError is a proper exception subclass."""
        err = ExecutionCancelledError("sess_1")
        assert isinstance(err, Exception)

    def test_error_str_contains_session_and_reason(self) -> None:
        """String representation includes both session ID and reason."""
        err = ExecutionCancelledError("sess_xyz", reason="Auto-cleanup")
        s = str(err)
        assert "sess_xyz" in s
        assert "Auto-cleanup" in s

    def test_error_attributes_accessible(self) -> None:
        """Error attributes are accessible after construction."""
        err = ExecutionCancelledError("sess_attr", reason="Timeout")
        assert err.session_id == "sess_attr"
        assert err.reason == "Timeout"
