"""Tests for the ``ooo auto`` blocked-result panel (actionable UX summary)."""

from __future__ import annotations

import io

from rich.console import Console

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoResumeCapability
from ouroboros.cli.commands.auto import _render_blocked_panel


def _capture(state: AutoPipelineState | None, result: AutoPipelineResult) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, width=1000)
    _render_blocked_panel(console, state, result)
    return buffer.getvalue()


def test_blocked_panel_preflight_open_questions_and_start_new_session() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.auto_session_id = "auto_preflight"
    state.last_tool_name = "seed_preflight"
    state.last_error_code = "seed_preflight_unexecutable"
    result = AutoPipelineResult(
        status="blocked",
        auto_session_id="auto_preflight",
        phase="blocked",
        stop_reason_code="seed_preflight_unexecutable",
        blocker=(
            "Seed preflight found 2 unexecutable contract claim(s); answer "
            "these open questions, revise the Seed, and start a new session: "
            "does verify.sh exist? | what value binds $DEPLOY_TOKEN?"
        ),
        resume_capability=AutoResumeCapability.NONE,
    )

    output = _capture(state, result)

    assert "stage      : seed_preflight" in output
    assert "reason code: seed_preflight_unexecutable" in output
    assert "- does verify.sh exist?" in output
    assert "- what value binds $DEPLOY_TOKEN?" in output
    assert "resume     : not resumable — start a new session" in output
    assert "--resume auto_preflight" not in output


def test_blocked_panel_seed_qa_bullets_up_to_five() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.auto_session_id = "auto_qa"
    state.last_tool_name = "seed_qa"
    state.last_error_code = "seed_qa_feedback_unmapped"
    result = AutoPipelineResult(
        status="blocked",
        auto_session_id="auto_qa",
        phase="blocked",
        stop_reason_code="seed_qa_feedback_unmapped",
        blocker="Pre-run Seed QA failed with unmapped feedback",
        last_qa_differences=("diff one", "diff two", "diff three"),
        last_qa_suggestions=("suggestion one", "suggestion two", "suggestion three"),
        resume_capability=AutoResumeCapability.RESUME,
    )

    output = _capture(state, result)

    assert "stage      : seed_qa" in output
    bullet_lines = [line for line in output.splitlines() if line.strip().startswith("-")]
    assert len(bullet_lines) == 5
    assert "- diff one" in output
    assert "- suggestion one" in output
    # Only the first 5 of the combined 6 items are bulleted.
    assert "- suggestion three" not in output
    assert "resume     : ooo auto --resume auto_qa" in output


def test_blocked_panel_not_resumable_never_fabricates_command() -> None:
    result = AutoPipelineResult(
        status="blocked",
        auto_session_id="auto_dead",
        phase="blocked",
        stop_reason_code="interview_unsafe_gaps_remain",
        blocker="genuine deadlock; no safe default available",
        resume_capability=AutoResumeCapability.NONE,
    )

    output = _capture(None, result)

    assert "resume     : not resumable — start a new session" in output
    assert "--resume auto_dead" not in output
    assert "stage      : unknown" in output


def test_blocked_panel_partial_resume_carries_note_from_shared_helper() -> None:
    result = AutoPipelineResult(
        status="blocked",
        auto_session_id="auto_partial",
        phase="blocked",
        blocker="run execution paused before completion",
        resume_capability=AutoResumeCapability.PARTIAL_RESUME,
    )

    output = _capture(None, result)

    assert "resume     : ooo auto --resume auto_partial" in output
    assert "some progress preserved" in output


def test_blocked_panel_retry_carries_note_from_shared_helper() -> None:
    result = AutoPipelineResult(
        status="blocked",
        auto_session_id="auto_retry",
        phase="blocked",
        blocker="run execution finished unsuccessfully",
        resume_capability=AutoResumeCapability.RETRY,
    )

    output = _capture(None, result)

    assert "resume     : ooo auto --resume auto_retry" in output
    assert "no prior session context" in output


def test_blocked_panel_ralph_deadline_checkpoint_offers_resume() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.auto_session_id = "auto_ralph_deadline"
    state.phase = AutoPhase.RALPH_HANDOFF
    state.ralph_job_id = "job_1"
    state.mark_blocked("pipeline timeout", tool_name="pipeline_deadline")
    result = AutoPipelineResult(
        status="blocked",
        auto_session_id=state.auto_session_id,
        phase="blocked",
        blocker=state.last_error,
        resume_capability=state.resume_capability(),
    )

    output = _capture(state, result)

    assert "resume     : ooo auto --resume auto_ralph_deadline" in output
    assert "not resumable" not in output


def test_blocked_panel_truncates_long_blocker_and_collapses_whitespace() -> None:
    result = AutoPipelineResult(
        status="blocked",
        auto_session_id="auto_long",
        phase="blocked",
        blocker=("word " * 200) + "\n\n  extra   whitespace",
        resume_capability=AutoResumeCapability.RESUME,
    )

    output = _capture(None, result)

    blocker_line = next(line for line in output.splitlines() if "blocker" in line)
    rendered = blocker_line.split(":", 1)[1].strip()
    assert len(rendered) <= 400
    assert "  " not in rendered
