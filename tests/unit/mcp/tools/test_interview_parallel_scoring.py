"""Tests for InterviewHandler — sequential ambiguity scoring + question generation.

Verifies that when answered rounds >= MIN_ROUNDS_BEFORE_EARLY_EXIT, ambiguity
scoring runs **before** question generation so the question prompt sees the
freshly mutated state (ambiguity_score, completion_candidate_streak, closure
mode).  Early-exit still works correctly when scoring triggers completion.

See: https://github.com/Q00/ouroboros/issues/286
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import (
    MIN_ROUNDS_BEFORE_EARLY_EXIT,
    InterviewRound,
    InterviewState,
    InterviewStatus,
)
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_state(
    interview_id: str = "test-scoring",
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
        completion_candidate_streak=2,
    )


def _make_component(name: str = "test") -> ComponentScore:
    return ComponentScore(name=name, clarity_score=0.9, weight=1.0, justification="clear")


def _make_not_ready_score() -> AmbiguityScore:
    """Score that does NOT trigger early completion (> 0.2)."""
    return AmbiguityScore(
        overall_score=0.5,
        breakdown=ScoreBreakdown(
            goal_clarity=_make_component("goal"),
            constraint_clarity=_make_component("constraints"),
            success_criteria_clarity=_make_component("success_criteria"),
        ),
    )


def _make_ready_score() -> AmbiguityScore:
    """Score that triggers early completion (<= 0.2)."""
    return AmbiguityScore(
        overall_score=0.1,
        breakdown=ScoreBreakdown(
            goal_clarity=_make_component("goal"),
            constraint_clarity=_make_component("constraints"),
            success_criteria_clarity=_make_component("success_criteria"),
        ),
    )


def _build_handler() -> InterviewHandler:
    return InterviewHandler(llm_backend="claude", event_store=None)


# ── Tests ────────────────────────────────────────────────────────────────────


class TestScoringBeforeQuestionGeneration:
    """Scoring runs before question generation for rounds >= MIN."""

    @pytest.mark.asyncio
    async def test_scoring_then_question_gen(self) -> None:
        """Both scoring and question gen are called when answered >= MIN_ROUNDS."""
        handler = _build_handler()
        state = _make_state(answered_rounds=MIN_ROUNDS_BEFORE_EARLY_EXIT)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="Next question?")
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        not_ready = _make_not_ready_score()

        with (
            patch.object(
                handler,
                "_score_interview_state",
                new_callable=AsyncMock,
                return_value=not_ready,
            ) as mock_score,
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
            result = await handler.handle({"session_id": "test-scoring", "answer": "My answer"})

            mock_score.assert_called_once()
            mock_engine.ask_next_question.assert_called_once()
            assert result.is_ok

    @pytest.mark.asyncio
    async def test_early_exit_skips_question_gen(self) -> None:
        """When scoring triggers completion, question gen is never called."""
        handler = _build_handler()
        state = _make_state(answered_rounds=MIN_ROUNDS_BEFORE_EARLY_EXIT)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="Should not be called")
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        ready_score = _make_ready_score()

        with (
            patch.object(
                handler,
                "_score_interview_state",
                new_callable=AsyncMock,
                return_value=ready_score,
            ),
            patch.object(
                handler,
                "_complete_interview_response",
                new_callable=AsyncMock,
                return_value=MagicMock(is_ok=True),
            ) as mock_complete,
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
            await handler.handle({"session_id": "test-scoring", "answer": "Final clarification"})

            mock_complete.assert_called_once()
            # Sequential: question gen is skipped on early completion
            mock_engine.ask_next_question.assert_not_called()

    @pytest.mark.asyncio
    async def test_scoring_returns_none_still_generates_question(self) -> None:
        """If scoring returns None (internal failure), question gen still runs."""
        handler = _build_handler()
        state = _make_state(answered_rounds=MIN_ROUNDS_BEFORE_EARLY_EXIT)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="Question after score failure")
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        with (
            patch.object(
                handler,
                "_score_interview_state",
                new_callable=AsyncMock,
                return_value=None,
            ),
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
            result = await handler.handle({"session_id": "test-scoring", "answer": "Some answer"})

            mock_engine.ask_next_question.assert_called_once()
            assert result.is_ok


class TestNoScoringBelowThreshold:
    """Below MIN_ROUNDS, scoring is skipped and question gen runs alone."""

    @pytest.mark.asyncio
    async def test_early_round_no_scoring(self) -> None:
        """At round 1, only question gen runs (no scoring)."""
        handler = _build_handler()
        state = _make_state(answered_rounds=1)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="Early question")
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        with (
            patch.object(handler, "_score_interview_state", new_callable=AsyncMock) as mock_score,
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
            result = await handler.handle({"session_id": "test-scoring", "answer": "Early answer"})

            mock_score.assert_not_called()
            mock_engine.ask_next_question.assert_called_once()
            assert result.is_ok
