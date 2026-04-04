"""Tests for InterviewHandler — parallel scoring and question generation.

Regression coverage for the performance improvement where ambiguity
scoring and question generation run concurrently via asyncio.gather()
when answered rounds >= MIN_ROUNDS_BEFORE_EARLY_EXIT.  On the start
path and early answer rounds, scoring is skipped entirely (no gather).

See: https://github.com/Q00/ouroboros/issues/286
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.bigbang.interview import (
    MIN_ROUNDS_BEFORE_EARLY_EXIT,
    InterviewRound,
    InterviewState,
    InterviewStatus,
)
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler


def _make_state(
    interview_id: str = "test-001",
    answered_rounds: int = 0,
) -> InterviewState:
    """Create an InterviewState with the given number of answered rounds."""
    rounds = [
        InterviewRound(
            round_number=i + 1,
            question=f"Q{i + 1}",
            user_response=f"A{i + 1}",
        )
        for i in range(answered_rounds)
    ]
    if answered_rounds > 0:
        rounds.append(
            InterviewRound(
                round_number=answered_rounds + 1,
                question=f"Q{answered_rounds + 1}",
                user_response=None,
            )
        )
    return InterviewState(
        interview_id=interview_id,
        initial_context="Build a test app",
        rounds=rounds,
        status=InterviewStatus.IN_PROGRESS,
    )


def _build_handler() -> InterviewHandler:
    return InterviewHandler(llm_backend="claude", event_store=None)


class TestStartPathNoParallelization:
    """On interview start, scoring is skipped (no answers yet) so no gather."""

    @pytest.mark.asyncio
    async def test_start_skips_scoring_and_gather(self) -> None:
        """Start path has 0 answered rounds — scoring is pure waste."""
        handler = _build_handler()

        mock_engine = MagicMock()
        mock_engine.start_interview = AsyncMock(
            return_value=MagicMock(is_err=False, value=_make_state(answered_rounds=0))
        )
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="First question?")
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        score_mock = AsyncMock(return_value=None)

        with (
            patch.object(handler, "_score_interview_state", score_mock),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
                return_value=MagicMock(),
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.resolve_initial_context_input",
                return_value=MagicMock(is_err=False, value="Build a test app"),
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.asyncio.gather",
                wraps=asyncio.gather,
            ) as mock_gather,
        ):
            await handler.handle({"initial_context": "Build a test app", "cwd": "/tmp"})

            # No gather — scoring is skipped at interview start
            mock_gather.assert_not_called()
            # Scoring must NOT be invoked (0 answered rounds)
            score_mock.assert_not_called()
            # Question generation still runs sequentially
            mock_engine.ask_next_question.assert_called_once()


class TestAnswerPathParallelization:
    """On answer steps, scoring and question gen run concurrently."""

    @pytest.mark.asyncio
    async def test_answer_uses_gather(self) -> None:
        """After recording an answer, scoring + question gen run in parallel."""
        handler = _build_handler()
        state = _make_state(answered_rounds=MIN_ROUNDS_BEFORE_EARLY_EXIT)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="Next question?")
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        score_mock = AsyncMock(return_value=None)

        with (
            patch.object(handler, "_score_interview_state", score_mock),
            patch.object(handler, "_emit_event", new_callable=AsyncMock),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
                return_value=MagicMock(),
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.asyncio.gather",
                wraps=asyncio.gather,
            ) as mock_gather,
        ):
            await handler.handle({"session_id": "test-001", "answer": "Some answer"})

            mock_gather.assert_called_once()
            score_mock.assert_called_once()
            mock_engine.ask_next_question.assert_called_once()

    @pytest.mark.asyncio
    async def test_answer_below_threshold_skips_scoring_and_gather(self) -> None:
        """Before MIN_ROUNDS_BEFORE_EARLY_EXIT, scoring is skipped (no gather)."""
        handler = _build_handler()
        # 1 answered round — well below the threshold
        state = _make_state(answered_rounds=1)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="Next question?")
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        score_mock = AsyncMock(return_value=None)

        with (
            patch.object(handler, "_score_interview_state", score_mock),
            patch.object(handler, "_emit_event", new_callable=AsyncMock),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
                return_value=MagicMock(),
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.asyncio.gather",
                wraps=asyncio.gather,
            ) as mock_gather,
        ):
            await handler.handle({"session_id": "test-001", "answer": "Some answer"})

            # No gather — scoring is skipped below threshold
            mock_gather.assert_not_called()
            # Scoring must NOT be invoked
            score_mock.assert_not_called()
            # Question generation still runs sequentially
            mock_engine.ask_next_question.assert_called_once()

    @pytest.mark.asyncio
    async def test_resume_with_pending_question_returns_immediately(self) -> None:
        """Resuming with a pending unanswered question returns it directly.

        When there's a pending unanswered round, the handler returns the
        cached question without calling scoring or question gen (no gather).
        """
        handler = _build_handler()
        # _make_state appends an unanswered round when answered_rounds > 0
        state = _make_state(answered_rounds=2)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))

        with (
            patch.object(handler, "_score_interview_state", new_callable=AsyncMock) as score_mock,
            patch(
                "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
                return_value=MagicMock(),
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.asyncio.gather",
                wraps=asyncio.gather,
            ) as mock_gather,
        ):
            result = await handler.handle({"session_id": "test-001"})

            # Resume with pending question: returns cached question immediately
            mock_gather.assert_not_called()
            score_mock.assert_not_called()
            mock_engine.ask_next_question.assert_not_called()
            assert result.is_ok


class TestCompletionStillWorks:
    """Early completion via scoring must still work with parallel execution."""

    @pytest.mark.asyncio
    async def test_completion_triggers_despite_parallel_question_gen(self) -> None:
        """If scoring says 'ready', completion is returned (question discarded)."""
        handler = _build_handler()
        state = _make_state(answered_rounds=MIN_ROUNDS_BEFORE_EARLY_EXIT)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="This question should be discarded")
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        # Score indicating ready for seed
        ready_score = MagicMock()
        ready_score.is_ready_for_seed = True
        ready_score.overall_score = 0.1
        score_mock = AsyncMock(return_value=ready_score)

        completion_response = MagicMock()
        completion_mock = AsyncMock(return_value=completion_response)

        with (
            patch.object(handler, "_score_interview_state", score_mock),
            patch.object(handler, "_complete_interview_response", completion_mock),
            patch.object(handler, "_emit_event", new_callable=AsyncMock),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
                return_value=MagicMock(),
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
        ):
            result = await handler.handle({"session_id": "test-001", "answer": "Final answer"})

            # Completion should be returned
            assert result == completion_response
            completion_mock.assert_called_once()
            # Question gen still ran (in parallel) but result was discarded
            mock_engine.ask_next_question.assert_called_once()
