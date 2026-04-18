"""Tests for InterviewHandler scoring flow after the closure-gating changes.

These tests verify that:
- scoring is still skipped before MIN_ROUNDS_BEFORE_EARLY_EXIT
- once scoring starts, it runs before question generation so the next question
  sees the latest ambiguity snapshot and completion-candidate streak
"""

from __future__ import annotations

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


class TestStartPathScoringFlow:
    """Scoring is skipped entirely when no answered rounds exist yet."""

    @pytest.mark.asyncio
    async def test_start_skips_scoring(self) -> None:
        handler = _build_handler()
        captured_adapter_kwargs: dict = {}

        mock_engine = MagicMock()
        mock_engine.start_interview = AsyncMock(
            return_value=MagicMock(is_err=False, value=_make_state(answered_rounds=0))
        )
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="First question?")
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        score_mock = AsyncMock(return_value=None)

        def _capture_adapter(**kwargs):
            captured_adapter_kwargs.update(kwargs)
            return MagicMock()

        with (
            patch.object(handler, "_score_interview_state", score_mock),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
                side_effect=_capture_adapter,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.resolve_initial_context_input",
                return_value=MagicMock(is_err=False, value="Build a test app"),
            ),
        ):
            await handler.handle({"initial_context": "Build a test app", "cwd": "/tmp"})

        score_mock.assert_not_called()
        mock_engine.ask_next_question.assert_called_once()
        assert captured_adapter_kwargs["allowed_tools"] == [
            "Read",
            "Glob",
            "Grep",
            "WebFetch",
            "WebSearch",
        ]


class TestAnswerPathScoringFlow:
    """Scoring starts at the minimum round threshold and runs before questions."""

    @pytest.mark.asyncio
    async def test_answer_below_threshold_skips_scoring(self) -> None:
        handler = _build_handler()
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
        ):
            await handler.handle({"session_id": "test-001", "answer": "Some answer"})

        score_mock.assert_not_called()
        mock_engine.ask_next_question.assert_called_once()

    @pytest.mark.asyncio
    async def test_answer_at_threshold_scores_before_question_generation(self) -> None:
        handler = _build_handler()
        state = _make_state(answered_rounds=MIN_ROUNDS_BEFORE_EARLY_EXIT)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        call_order: list[str] = []

        async def score_state(*args, **kwargs):
            del args, kwargs
            call_order.append("score")
            return None

        async def ask_next_question(*args, **kwargs):
            del args, kwargs
            call_order.append("question")
            return MagicMock(is_err=False, value="Next question?")

        mock_engine.ask_next_question = AsyncMock(side_effect=ask_next_question)

        with (
            patch.object(handler, "_score_interview_state", AsyncMock(side_effect=score_state)),
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
            await handler.handle({"session_id": "test-001", "answer": "Some answer"})

        assert call_order == ["score", "question"]
