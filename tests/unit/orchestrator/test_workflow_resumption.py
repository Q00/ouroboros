"""Unit tests for workflow resumption after Ctrl+C (#50)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.seed import (
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.runner import OrchestratorRunner
from ouroboros.orchestrator.session import (
    SessionRepository,
    SessionStatus,
    SessionTracker,
)


@pytest.fixture
def sample_seed() -> Seed:
    """Create a sample seed for testing."""
    return Seed(
        goal="Build a widget",
        constraints=("Python 3.14+",),
        acceptance_criteria=(
            "Widget renders correctly",
            "Widget handles clicks",
            "Widget has tests",
        ),
        ontology_schema=OntologySchema(
            name="Widget",
            description="Widget ontology",
        ),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )


@pytest.fixture
def mock_event_store() -> AsyncMock:
    store = AsyncMock()
    store.append = AsyncMock()
    store.replay = AsyncMock(return_value=[])
    return store


@pytest.fixture
def mock_adapter() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_console() -> MagicMock:
    return MagicMock()


@pytest.fixture
def runner(
    mock_adapter: MagicMock,
    mock_event_store: AsyncMock,
    mock_console: MagicMock,
) -> OrchestratorRunner:
    return OrchestratorRunner(mock_adapter, mock_event_store, mock_console)


class TestMarkPaused:
    """Tests for SessionRepository.mark_paused()."""

    @pytest.mark.asyncio
    async def test_mark_paused_emits_event(self, mock_event_store: AsyncMock) -> None:
        """mark_paused should emit orchestrator.session.paused event."""
        repo = SessionRepository(mock_event_store)
        result = await repo.mark_paused(
            "sess_123",
            reason="Ctrl+C",
            messages_processed=42,
        )

        assert result.is_ok
        mock_event_store.append.assert_called_once()
        event = mock_event_store.append.call_args[0][0]
        assert event.type == "orchestrator.session.paused"
        assert event.aggregate_id == "sess_123"
        assert event.data["reason"] == "Ctrl+C"
        assert event.data["messages_processed"] == 42
        assert "paused_at" in event.data

    @pytest.mark.asyncio
    async def test_mark_paused_handles_store_failure(
        self, mock_event_store: AsyncMock,
    ) -> None:
        """mark_paused should return error if event store fails."""
        mock_event_store.append.side_effect = RuntimeError("DB error")
        repo = SessionRepository(mock_event_store)

        result = await repo.mark_paused("sess_123", reason="test")
        assert result.is_err


class TestReconstructACState:
    """Tests for OrchestratorRunner._reconstruct_ac_state()."""

    @pytest.mark.asyncio
    async def test_returns_completed_indices(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Should return indices of completed ACs from last progress event."""
        mock_event_store.replay.return_value = [
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_1",
                data={},
            ),
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="session",
                aggregate_id="sess_1",
                data={
                    "acceptance_criteria": [
                        {"index": 1, "status": "completed", "content": "AC 1"},
                        {"index": 2, "status": "in_progress", "content": "AC 2"},
                        {"index": 3, "status": "pending", "content": "AC 3"},
                    ],
                },
            ),
        ]

        result = await runner._reconstruct_ac_state("sess_1")
        assert result == [1]

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_progress(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Should return empty list when no workflow progress events exist."""
        mock_event_store.replay.return_value = [
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_1",
                data={},
            ),
        ]

        result = await runner._reconstruct_ac_state("sess_1")
        assert result == []

    @pytest.mark.asyncio
    async def test_uses_last_progress_event(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Should use the most recent workflow progress event."""
        mock_event_store.replay.return_value = [
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="session",
                aggregate_id="sess_1",
                data={
                    "acceptance_criteria": [
                        {"index": 1, "status": "completed", "content": "AC 1"},
                        {"index": 2, "status": "pending", "content": "AC 2"},
                    ],
                },
            ),
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="session",
                aggregate_id="sess_1",
                data={
                    "acceptance_criteria": [
                        {"index": 1, "status": "completed", "content": "AC 1"},
                        {"index": 2, "status": "completed", "content": "AC 2"},
                    ],
                },
            ),
        ]

        result = await runner._reconstruct_ac_state("sess_1")
        assert sorted(result) == [1, 2]

    @pytest.mark.asyncio
    async def test_handles_replay_error(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Should return empty list on replay error."""
        mock_event_store.replay.side_effect = RuntimeError("DB error")

        result = await runner._reconstruct_ac_state("sess_1")
        assert result == []


class TestCancelledErrorHandler:
    """Tests for CancelledError handling in execute_seed."""

    @pytest.mark.asyncio
    async def test_execute_seed_marks_paused_on_cancelled(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """execute_seed should mark session PAUSED when CancelledError is raised."""

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(type="assistant", content="Working...")
            raise asyncio.CancelledError()

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec", sample_seed.metadata.seed_id))

        mock_mark_paused = AsyncMock(return_value=Result.ok(None))

        with (
            patch.object(runner._session_repo, "create_session", mock_create_session),
            patch.object(runner._session_repo, "mark_paused", mock_mark_paused),
        ):
            with pytest.raises(asyncio.CancelledError):
                await runner.execute_seed(sample_seed, parallel=False)

        # Verify mark_paused was called with the right reason
        mock_mark_paused.assert_called_once()
        call_kwargs = mock_mark_paused.call_args.kwargs
        # positional fallback: mark_paused(session_id, reason=..., ...)
        reason = call_kwargs.get("reason") or mock_mark_paused.call_args[0][1]
        assert "Ctrl+C" in reason

    @pytest.mark.asyncio
    async def test_resume_session_marks_paused_on_cancelled(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """resume_session should mark session PAUSED when CancelledError is raised."""

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(type="assistant", content="Resuming...")
            raise asyncio.CancelledError()

        mock_adapter.execute_task = mock_execute

        tracker = SessionTracker.create("exec_1", sample_seed.metadata.seed_id, session_id="sess_1")
        tracker = tracker.with_status(SessionStatus.PAUSED)

        async def mock_reconstruct(*args: Any, **kwargs: Any):
            return Result.ok(tracker)

        mock_mark_paused = AsyncMock(return_value=Result.ok(None))

        with (
            patch.object(runner._session_repo, "reconstruct_session", mock_reconstruct),
            patch.object(runner._session_repo, "mark_paused", mock_mark_paused),
            patch.object(runner, "_reconstruct_ac_state", AsyncMock(return_value=[])),
        ):
            with pytest.raises(asyncio.CancelledError):
                await runner.resume_session("sess_1", sample_seed)

        mock_mark_paused.assert_called_once()


class TestResumePromptWithACState:
    """Tests for resume prompt building with AC state."""

    @pytest.mark.asyncio
    async def test_resume_prompt_includes_completed_acs(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Resume prompt should list completed ACs and remaining work."""
        captured_prompt: list[str] = []

        async def mock_execute(
            prompt: str, **kwargs: Any,
        ) -> AsyncIterator[AgentMessage]:
            captured_prompt.append(prompt)
            yield AgentMessage(
                type="result",
                content="Done",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute

        tracker = SessionTracker.create("exec_1", sample_seed.metadata.seed_id, session_id="sess_1")
        tracker = tracker.with_status(SessionStatus.PAUSED)

        async def mock_reconstruct(*args: Any, **kwargs: Any):
            return Result.ok(tracker)

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "reconstruct_session", mock_reconstruct),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
            patch.object(runner, "_reconstruct_ac_state", AsyncMock(return_value=[1])),
        ):
            result = await runner.resume_session("sess_1", sample_seed)

        assert len(captured_prompt) == 1
        prompt = captured_prompt[0]
        assert "AC #1" in prompt
        assert "already completed" in prompt
        assert "Widget handles clicks" in prompt  # remaining AC #2
        assert "Widget has tests" in prompt  # remaining AC #3
