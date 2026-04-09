"""Tests for InterviewHandler — parallel ambiguity scoring + question generation.

Verifies that when answered rounds >= MIN_ROUNDS_BEFORE_EARLY_EXIT, both
ambiguity scoring and question generation run concurrently via asyncio.gather,
and that early-exit still works correctly when scoring triggers completion.

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
    interview_id: str = "test-parallel",
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


class TestParallelScoringAndQuestionGeneration:
    """Scoring and question generation run concurrently for rounds >= MIN."""

    @pytest.mark.asyncio
    async def test_both_run_concurrently(self) -> None:
        """Scoring and question gen both execute when answered >= MIN_ROUNDS."""
        handler = _build_handler()
        state = _make_state(answered_rounds=MIN_ROUNDS_BEFORE_EARLY_EXIT)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="Parallel question?")
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
            result = await handler.handle({"session_id": "test-parallel", "answer": "My answer"})

            # Both must be called
            mock_score.assert_called_once()
            mock_engine.ask_next_question.assert_called_once()

            # Question should be in the result (not re-generated)
            assert result.is_ok

    @pytest.mark.asyncio
    async def test_early_exit_when_score_ready(self) -> None:
        """When scoring returns is_ready_for_seed, interview completes."""
        handler = _build_handler()
        state = _make_state(answered_rounds=MIN_ROUNDS_BEFORE_EARLY_EXIT)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="Discarded question")
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
            await handler.handle({"session_id": "test-parallel", "answer": "Final clarification"})

            # Early completion should trigger
            mock_complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_scoring_failure_does_not_block_question(self) -> None:
        """If scoring raises an exception, question gen result is still used."""
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
                side_effect=RuntimeError("Scoring exploded"),
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
            result = await handler.handle({"session_id": "test-parallel", "answer": "Some answer"})

            # Question gen should still succeed
            assert result.is_ok

    @pytest.mark.asyncio
    async def test_question_failure_falls_back_to_sequential(self) -> None:
        """If parallel question gen fails, it retries sequentially."""
        handler = _build_handler()
        state = _make_state(answered_rounds=MIN_ROUNDS_BEFORE_EARLY_EXIT)

        call_count = 0

        async def _question_side_effect(s):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("First attempt failed")
            return MagicMock(is_err=False, value="Retry question")

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(side_effect=_question_side_effect)
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        with (
            patch.object(
                handler,
                "_score_interview_state",
                new_callable=AsyncMock,
                return_value=_make_not_ready_score(),
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
            result = await handler.handle({"session_id": "test-parallel", "answer": "Some answer"})

            # Should have been called twice: once parallel (failed), once sequential (retry)
            assert mock_engine.ask_next_question.call_count == 2
            assert result.is_ok


class TestNoParallelizationBelowThreshold:
    """Below MIN_ROUNDS, scoring is skipped and question gen runs alone."""

    @pytest.mark.asyncio
    async def test_early_round_no_parallel(self) -> None:
        """At round 1, only question gen runs (no scoring, no parallelization)."""
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
            result = await handler.handle({"session_id": "test-parallel", "answer": "Early answer"})

            mock_score.assert_not_called()
            mock_engine.ask_next_question.assert_called_once()
            assert result.is_ok
