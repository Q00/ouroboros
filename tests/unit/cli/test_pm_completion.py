"""Regression tests for PM CLI completion and ambiguity persistence."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.bigbang.interview import InterviewRound
from ouroboros.cli.commands.pm import _run_pm_interview
from ouroboros.core.types import Result


def _resume_state(*, pending_question: str = "Current question?") -> SimpleNamespace:
    """Create a minimal resumable PM interview state."""
    return SimpleNamespace(
        interview_id="sess-123",
        rounds=[InterviewRound(round_number=1, question=pending_question, user_response=None)],
        is_complete=False,
        ambiguity_score=None,
        clear_stored_ambiguity=MagicMock(),
        mark_updated=MagicMock(),
    )


@pytest.mark.asyncio
async def test_run_pm_interview_auto_completes_after_answer(tmp_path: Path) -> None:
    """A normal answer should auto-complete when ambiguity is resolved."""
    state = _resume_state()
    updated_state = SimpleNamespace(**{**state.__dict__})
    completed_state = SimpleNamespace(**{**updated_state.__dict__, "is_complete": True})
    engine = MagicMock()
    engine.load_state = AsyncMock(return_value=Result.ok(state))
    engine.record_response = AsyncMock(return_value=Result.ok(updated_state))
    engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))
    engine.format_decide_later_summary.return_value = ""
    engine._install_pm_steering = MagicMock()

    completion = {
        "interview_complete": True,
        "completion_reason": "ambiguity_resolved",
        "rounds_completed": 1,
        "ambiguity_score": 0.18,
    }

    with (
        patch("ouroboros.providers.litellm_adapter.LiteLLMAdapter", return_value=object()),
        patch("ouroboros.bigbang.pm_interview.PMInterviewEngine.create", return_value=engine),
        patch(
            "ouroboros.cli.commands.pm.multiline_prompt_async",
            new_callable=AsyncMock,
            return_value="Final answer",
        ),
        patch("ouroboros.cli.commands.pm._save_cli_pm_meta"),
        patch(
            "ouroboros.cli.commands.pm.maybe_complete_pm_interview",
            new_callable=AsyncMock,
            return_value=Result.ok((completed_state, completion)),
        ) as mock_complete,
        patch("ouroboros.cli.commands.pm.console.print") as mock_print,
        patch("ouroboros.cli.commands.pm.print_success"),
    ):
        await _run_pm_interview(resume_id="sess-123", model="test-model", debug=False)

    updated_state.clear_stored_ambiguity.assert_called_once()
    mock_complete.assert_awaited_once_with(updated_state, engine)
    assert engine.save_state.await_count == 2
    assert engine.save_state.await_args_list[-1].args == (completed_state,)

    printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
    assert "Interview complete. Session ID: sess-123" in printed
    assert "Ambiguity score: 0.18" in printed


@pytest.mark.asyncio
async def test_run_pm_interview_done_path_persists_stored_ambiguity(tmp_path: Path) -> None:
    """Forced completion via done should still store and display ambiguity."""
    state = _resume_state()
    completed_state = SimpleNamespace(
        **{**state.__dict__, "is_complete": True, "ambiguity_score": 0.34}
    )
    engine = MagicMock()
    engine.load_state = AsyncMock(return_value=Result.ok(state))
    engine.complete_interview = AsyncMock(return_value=Result.ok(completed_state))
    engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))
    engine.format_decide_later_summary.return_value = ""
    engine._install_pm_steering = MagicMock()

    async def fake_check_completion(current_state: SimpleNamespace) -> None:
        current_state.ambiguity_score = 0.34
        return None

    engine.check_completion = AsyncMock(side_effect=fake_check_completion)

    with (
        patch("ouroboros.providers.litellm_adapter.LiteLLMAdapter", return_value=object()),
        patch("ouroboros.bigbang.pm_interview.PMInterviewEngine.create", return_value=engine),
        patch(
            "ouroboros.cli.commands.pm.multiline_prompt_async",
            new_callable=AsyncMock,
            return_value="done",
        ),
        patch("ouroboros.cli.commands.pm._save_cli_pm_meta"),
        patch("ouroboros.cli.commands.pm.console.print") as mock_print,
        patch("ouroboros.cli.commands.pm.print_success"),
    ):
        await _run_pm_interview(resume_id="sess-123", model="test-model", debug=False)

    engine.complete_interview.assert_awaited_once_with(state)
    assert engine.save_state.await_count == 2
    assert engine.save_state.await_args_list[-1].args == (completed_state,)

    printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
    assert "Interview complete. Session ID: sess-123" in printed
    assert "Ambiguity score: 0.34" in printed
