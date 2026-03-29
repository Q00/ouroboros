"""Unit tests for interactive PM prompt behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from ouroboros.bigbang.interview import InterviewRound
from ouroboros.cli.commands.pm import _run_pm_interview, pm_command
from ouroboros.core.types import Result


def _build_ctx() -> MagicMock:
    """Build a Typer context mock for direct callback testing."""
    ctx = MagicMock()
    ctx.invoked_subcommand = None
    return ctx


def _build_state(*, pending_question: str | None = None) -> SimpleNamespace:
    """Create the minimal interview state shape used by the PM CLI."""
    rounds = []
    if pending_question is not None:
        rounds.append(
            InterviewRound(
                round_number=1,
                question=pending_question,
                user_response=None,
            )
        )

    return SimpleNamespace(
        is_complete=False,
        rounds=rounds,
        current_round_number=1,
        interview_id="interview_123",
        ambiguity_score=None,
        mark_updated=MagicMock(),
        clear_stored_ambiguity=MagicMock(),
    )


def test_pm_command_exits_cleanly_on_keyboard_interrupt() -> None:
    """The PM command should surface Ctrl+C as a normal CLI exit."""

    def fake_run(coro: object) -> None:
        coro.close()  # type: ignore[union-attr]
        raise KeyboardInterrupt

    with patch("ouroboros.cli.commands.pm.asyncio.run", side_effect=fake_run):
        with pytest.raises(typer.Exit) as exc_info:
            pm_command(_build_ctx(), model="test-model", debug=False)

    assert exc_info.value.exit_code == 0


def test_pm_command_enables_debug_logging_when_requested() -> None:
    """The PM debug flag should enable verbose logging for the session."""

    def fake_run(coro: object) -> None:
        coro.close()  # type: ignore[union-attr]

    with (
        patch("ouroboros.cli.commands.pm.asyncio.run", side_effect=fake_run),
        patch("ouroboros.cli.commands.pm.configure_logging") as mock_configure,
        patch("ouroboros.cli.commands.pm.print_info") as mock_print_info,
    ):
        pm_command(_build_ctx(), model="test-model", debug=True)

    mock_configure.assert_called_once()
    config = mock_configure.call_args.args[0]
    assert config.log_level == "DEBUG"
    messages = [call.args[0] for call in mock_print_info.call_args_list]
    assert "Debug mode enabled - showing verbose logs" in messages


@pytest.mark.asyncio
async def test_run_pm_interview_new_session_uses_multiline_prompt_and_shows_progress(
    tmp_path: Path,
) -> None:
    """New PM sessions should use the multiline prompt for both answers."""
    initial_state = _build_state()
    recorded_state = _build_state()
    recorded_state.is_complete = True
    engine = MagicMock()
    engine.get_opening_question.return_value = "What do you want to build?"
    engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(initial_state))
    engine.ask_next_question = AsyncMock(return_value=Result.ok("What is the first workflow?"))
    engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))
    engine.record_response = AsyncMock(return_value=Result.ok(recorded_state))
    engine.complete_interview = AsyncMock()
    engine.check_completion = AsyncMock(return_value=None)
    engine.deferred_items = []
    engine.decide_later_items = []
    engine.format_decide_later_summary.return_value = ""

    with (
        patch.object(Path, "home", return_value=tmp_path),
        patch("ouroboros.providers.litellm_adapter.LiteLLMAdapter", return_value=object()),
        patch("ouroboros.bigbang.pm_interview.PMInterviewEngine.create", return_value=engine),
        patch("ouroboros.cli.commands.pm._check_existing_pm_seeds", return_value=True),
        patch("ouroboros.cli.commands.pm._load_brownfield_from_db", return_value=[]),
        patch("ouroboros.cli.commands.pm._select_repos", return_value=[]),
        patch("ouroboros.cli.commands.pm._save_cli_pm_meta"),
        patch(
            "ouroboros.cli.commands.pm.multiline_prompt_async",
            side_effect=["Initial context", "Actual answer"],
        ) as mock_prompt,
        patch("ouroboros.cli.commands.pm.print_info") as mock_print_info,
        patch("ouroboros.cli.commands.pm.print_success"),
    ):
        await _run_pm_interview(resume_id=None, model="test-model", debug=False)

    engine.ask_opening_and_start.assert_awaited_once()
    engine.ask_next_question.assert_awaited_once_with(initial_state)
    assert mock_prompt.await_count == 2
    engine.record_response.assert_awaited_once_with(
        initial_state,
        "Actual answer",
        "What is the first workflow?",
    )
    engine.complete_interview.assert_not_called()
    assert engine.save_state.await_args_list[-1].args[0] is recorded_state

    messages = [call.args[0] for call in mock_print_info.call_args_list]
    assert "Starting interview..." in messages
    assert "Generating next question..." in messages
    assert messages.index("Starting interview...") < messages.index("Generating next question...")


@pytest.mark.asyncio
async def test_run_pm_interview_resume_with_pending_question_skips_generation_message(
    tmp_path: Path,
) -> None:
    """Resume should reuse the saved pending question instead of generating a new one."""
    state = _build_state(pending_question="Saved question?")
    completed_state = _build_state()
    completed_state.is_complete = True
    engine = MagicMock()
    engine.load_state = AsyncMock(return_value=Result.ok(state))
    engine.ask_next_question = AsyncMock()
    engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))
    engine.complete_interview = AsyncMock(return_value=Result.ok(completed_state))
    engine.check_completion = AsyncMock(return_value=None)
    engine.deferred_items = []
    engine.decide_later_items = []
    engine.format_decide_later_summary.return_value = ""
    engine._install_pm_steering = MagicMock()

    with (
        patch.object(Path, "home", return_value=tmp_path),
        patch("ouroboros.providers.litellm_adapter.LiteLLMAdapter", return_value=object()),
        patch("ouroboros.bigbang.pm_interview.PMInterviewEngine.create", return_value=engine),
        patch("ouroboros.cli.commands.pm._save_cli_pm_meta"),
        patch(
            "ouroboros.cli.commands.pm.multiline_prompt_async", return_value="done"
        ) as mock_prompt,
        patch("ouroboros.cli.commands.pm.print_info") as mock_print_info,
        patch("ouroboros.cli.commands.pm.print_success"),
        patch("ouroboros.cli.commands.pm.console.print"),
    ):
        await _run_pm_interview(resume_id="resume_123", model="test-model", debug=False)

    engine.ask_next_question.assert_not_awaited()
    engine._install_pm_steering.assert_called_once()
    mock_prompt.assert_awaited_once()
    engine.complete_interview.assert_awaited_once_with(state)
    assert engine.save_state.await_args_list[-1].args[0] is completed_state

    messages = [call.args[0] for call in mock_print_info.call_args_list]
    assert "Generating next question..." not in messages


@pytest.mark.asyncio
async def test_run_pm_interview_preserves_pending_question_across_interrupt_and_resume(
    tmp_path: Path,
) -> None:
    """A saved pending question should be re-displayed after an interrupted session."""
    interrupted_state = _build_state()
    resumed_state = _build_state(pending_question="What is the first workflow?")
    engine = MagicMock()
    engine.get_opening_question.return_value = "What do you want to build?"
    engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(interrupted_state))
    engine.ask_next_question = AsyncMock(return_value=Result.ok("What is the first workflow?"))
    engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))
    engine.load_state = AsyncMock(return_value=Result.ok(resumed_state))
    engine.complete_interview = AsyncMock(
        return_value=Result.ok(SimpleNamespace(**{**resumed_state.__dict__, "is_complete": True}))
    )
    engine.check_completion = AsyncMock(return_value=None)
    engine.deferred_items = []
    engine.decide_later_items = []
    engine.format_decide_later_summary.return_value = ""
    engine._install_pm_steering = MagicMock()

    with (
        patch.object(Path, "home", return_value=tmp_path),
        patch("ouroboros.providers.litellm_adapter.LiteLLMAdapter", return_value=object()),
        patch("ouroboros.bigbang.pm_interview.PMInterviewEngine.create", return_value=engine),
        patch("ouroboros.cli.commands.pm._check_existing_pm_seeds", return_value=True),
        patch("ouroboros.cli.commands.pm._load_brownfield_from_db", return_value=[]),
        patch("ouroboros.cli.commands.pm._select_repos", return_value=[]),
        patch("ouroboros.cli.commands.pm._save_cli_pm_meta"),
        patch(
            "ouroboros.cli.commands.pm.multiline_prompt_async",
            side_effect=["Initial context", KeyboardInterrupt, "done"],
        ),
        patch("ouroboros.cli.commands.pm.print_success"),
        patch("ouroboros.cli.commands.pm.console.print"),
    ):
        with pytest.raises(KeyboardInterrupt):
            await _run_pm_interview(resume_id=None, model="test-model", debug=False)

        saved_question_state = engine.save_state.await_args_list[0].args[0]
        assert saved_question_state.rounds[-1].question == "What is the first workflow?"
        assert saved_question_state.rounds[-1].user_response is None

        await _run_pm_interview(resume_id="resume_123", model="test-model", debug=False)

    engine.ask_next_question.assert_awaited_once_with(interrupted_state)
    engine.load_state.assert_awaited_once_with("resume_123")
