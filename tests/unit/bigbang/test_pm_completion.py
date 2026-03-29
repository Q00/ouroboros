"""Tests for shared PM completion helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.pm_completion import (
    build_pm_completion_summary,
    maybe_complete_pm_interview,
)
from ouroboros.core.types import Result


@pytest.mark.asyncio
async def test_maybe_complete_pm_interview_returns_none_when_not_ready() -> None:
    """No completion metadata means the interview should continue."""
    state = SimpleNamespace(interview_id="sess-1")
    engine = MagicMock()
    engine.check_completion = AsyncMock(return_value=None)
    engine.complete_interview = AsyncMock()

    result = await maybe_complete_pm_interview(state, engine)

    assert result.is_ok
    assert result.value == (state, None)
    engine.complete_interview.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_complete_pm_interview_marks_complete_when_ready() -> None:
    """Ready interviews should be marked complete through the shared helper."""
    state = SimpleNamespace(interview_id="sess-1")
    completed_state = SimpleNamespace(interview_id="sess-1", status="completed")
    engine = MagicMock()
    engine.check_completion = AsyncMock(
        return_value={
            "interview_complete": True,
            "completion_reason": "ambiguity_resolved",
            "rounds_completed": 5,
            "ambiguity_score": 0.18,
        }
    )
    engine.complete_interview = AsyncMock(return_value=Result.ok(completed_state))

    result = await maybe_complete_pm_interview(state, engine)

    assert result.is_ok
    assert result.value == (
        completed_state,
        {
            "interview_complete": True,
            "completion_reason": "ambiguity_resolved",
            "rounds_completed": 5,
            "ambiguity_score": 0.18,
        },
    )
    engine.complete_interview.assert_awaited_once_with(state)


def test_build_pm_completion_summary_uses_stored_score_fallback() -> None:
    """Forced completion should still surface the stored ambiguity score."""
    summary = build_pm_completion_summary(
        session_id="sess-1",
        completion=None,
        stored_ambiguity_score=0.34,
        deferred_count=1,
        decide_later_count=2,
        decide_later_summary="Items to decide later:\n  1. Example",
    )

    assert "Interview complete. Session ID: sess-1" in summary
    assert "Ambiguity score: 0.34" in summary
    assert 'Generate PM with: action="generate", session_id="sess-1"' in summary
