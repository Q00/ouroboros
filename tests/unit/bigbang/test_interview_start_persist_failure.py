"""Regression tests for Q00/ouroboros#687 (engine layer).

``InterviewEngine.start_interview`` must hard-fail when persisting the
initial state to disk fails.  Returning ``Result.ok`` after a failed save
would silently lie to downstream callers (the MCP handler now returns a
recoverable result whose ``meta.session_id`` claim of resumability hinges
on the file being on disk).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.interview import InterviewEngine
from ouroboros.core.errors import ValidationError
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionResponse, UsageInfo


def _make_engine(tmp_path) -> InterviewEngine:
    adapter = MagicMock()
    adapter.complete = AsyncMock(
        return_value=CompletionResponse(
            content="ignored",
            model="test",
            usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            finish_reason="stop",
        )
    )
    return InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="test-model")


@pytest.mark.asyncio
async def test_start_interview_returns_err_when_save_state_fails(tmp_path, monkeypatch) -> None:
    """A failed initial save must surface as ``Result.err``, not a silent warning."""

    engine = _make_engine(tmp_path)

    async def _failing_save(state):  # noqa: ANN001 — runtime stub
        return Result.err(
            ValidationError("disk full", field="interview_id", value=state.interview_id)
        )

    monkeypatch.setattr(engine, "save_state", _failing_save)

    outcome = await engine.start_interview("Build a CLI", cwd=str(tmp_path))

    assert outcome.is_err, "start_interview must hard-fail when save_state errors"
    error = outcome.error
    assert isinstance(error, ValidationError)
    assert "Failed to persist initial interview state" in str(error)
