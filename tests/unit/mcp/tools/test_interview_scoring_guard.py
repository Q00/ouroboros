"""Tests for InterviewHandler — ambiguity scoring guard.

Regression coverage for the performance issue where ambiguity scoring
was invoked on every interview step, even when early completion was
impossible (rounds < MIN_ROUNDS_BEFORE_EARLY_EXIT).

See: https://github.com/Q00/ouroboros/issues/283
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
    # Append one unanswered round (the pending question) if rounds exist
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


def _make_component(name: str = "test") -> ComponentScore:
    """Create a minimal ComponentScore."""
    return ComponentScore(name=name, clarity_score=0.9, weight=1.0, justification="clear")


def _make_ready_score() -> AmbiguityScore:
    """Create an AmbiguityScore that would trigger early completion."""
    breakdown = ScoreBreakdown(
        goal_clarity=_make_component("goal"),
        constraint_clarity=_make_component("constraints"),
        success_criteria_clarity=_make_component("success_criteria"),
    )
    # overall_score=0.1 → is_ready_for_seed property returns True (≤ 0.2)
    return AmbiguityScore(overall_score=0.1, breakdown=breakdown)


def _build_handler() -> InterviewHandler:
    """Build an InterviewHandler with mocked dependencies."""
    return InterviewHandler(
        llm_backend="claude",
        event_store=None,
    )


# ── Tests ────────────────────────────────────────────────────────────────────


class TestScoringSkippedOnInterviewStart:
    """Ambiguity scoring must NOT run when starting a new interview."""

    @pytest.mark.asyncio
    async def test_start_does_not_call_score(self) -> None:
        """On interview start (0 answered rounds), scoring is skipped."""
        handler = _build_handler()

        mock_engine = MagicMock()
        mock_engine.start_interview = AsyncMock(
            return_value=MagicMock(
                is_err=False,
                value=_make_state(answered_rounds=0),
            )
        )
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="What framework?")
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        with (
            patch.object(handler, "_score_interview_state", new_callable=AsyncMock) as mock_score,
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
        ):
            await handler.handle({"initial_context": "Build a test app", "cwd": "/tmp"})

            # Scoring should NOT have been called
            mock_score.assert_not_called()

            # But question generation should still proceed
            mock_engine.ask_next_question.assert_called_once()


class TestScoringSkippedOnEarlyRounds:
    """Ambiguity scoring must NOT run when answered rounds < MIN_ROUNDS."""

    @pytest.mark.asyncio
    async def test_round_1_answer_skips_scoring(self) -> None:
        """Answering round 1 (1 answered round) does not trigger scoring."""
        handler = _build_handler()

        state = _make_state(answered_rounds=1)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="Next question?")
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
            await handler.handle({"session_id": "test-001", "answer": "React with TypeScript"})

            mock_score.assert_not_called()
            mock_engine.ask_next_question.assert_called_once()

    @pytest.mark.asyncio
    async def test_round_2_answer_skips_scoring(self) -> None:
        """Answering round 2 (2 answered rounds) does not trigger scoring."""
        handler = _build_handler()

        state = _make_state(answered_rounds=2)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="Next question?")
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
            await handler.handle({"session_id": "test-001", "answer": "PostgreSQL"})

            mock_score.assert_not_called()


class TestScoringRunsWhenCompletionPossible:
    """Ambiguity scoring MUST run when answered rounds >= MIN_ROUNDS."""

    @pytest.mark.asyncio
    async def test_round_at_threshold_triggers_scoring(self) -> None:
        """Answering at MIN_ROUNDS_BEFORE_EARLY_EXIT triggers scoring."""
        handler = _build_handler()

        state = _make_state(answered_rounds=MIN_ROUNDS_BEFORE_EARLY_EXIT)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.record_response = AsyncMock(return_value=MagicMock(is_err=False, value=state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_err=False, value="Another question?")
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

        # overall_score=0.5 → is_ready_for_seed returns False (> 0.2)
        not_ready_score = AmbiguityScore(
            overall_score=0.5,
            breakdown=ScoreBreakdown(
                goal_clarity=_make_component("goal"),
                constraint_clarity=_make_component("constraints"),
                success_criteria_clarity=_make_component("success_criteria"),
            ),
        )
        assert not not_ready_score.is_ready_for_seed

        with (
            patch.object(
                handler,
                "_score_interview_state",
                new_callable=AsyncMock,
                return_value=not_ready_score,
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
            await handler.handle({"session_id": "test-001", "answer": "Yes, that's the plan"})

            # Scoring SHOULD be called at threshold
            mock_score.assert_called_once()
            mock_engine.ask_next_question.assert_called_once()
