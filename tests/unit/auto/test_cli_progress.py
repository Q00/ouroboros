"""CLI rendering tests for the live ``ooo auto`` phase trace."""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.progress import AutoProgressEvent
from ouroboros.cli.commands import auto as auto_command
from ouroboros.cli.commands.auto import _make_progress_renderer
from ouroboros.cli.main import app


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_make_progress_renderer_quiet_returns_none() -> None:
    assert _make_progress_renderer(quiet=True) is None


def test_make_progress_renderer_emits_phase_grade_repair_lines(capsys) -> None:
    render = _make_progress_renderer(quiet=False)
    assert render is not None

    render(
        AutoProgressEvent(
            auto_session_id="auto_x",
            phase="interview",
            kind="phase",
            message="asking interview round 3/12",
        )
    )
    render(
        AutoProgressEvent(
            auto_session_id="auto_x",
            phase="review",
            kind="grade",
            message="Seed grade A",
            grade="A",
        )
    )
    render(
        AutoProgressEvent(
            auto_session_id="auto_x",
            phase="repair",
            kind="repair",
            message="repair round 1",
            round=1,
        )
    )

    output = _strip_ansi(capsys.readouterr().out)
    assert "[auto] interview — asking interview round 3/12" in output
    assert "[auto] grade A — Seed grade A" in output
    assert "[auto] repair round 1 — repair round 1" in output


def test_cli_auto_help_documents_quiet_flag() -> None:
    result = CliRunner().invoke(app, ["auto", "--help"])
    output = _strip_ansi(result.output)

    assert result.exit_code == 0
    assert "--quiet" in output


@pytest.mark.asyncio
async def test_cli_run_auto_threads_progress_callback_to_pipeline(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            captured["progress_callback"] = kwargs.get("progress_callback")

        async def run(self, run_state):  # noqa: ANN001
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, **_kwargs):
            pass

    from ouroboros.auto.state import AutoStore

    monkeypatch.setattr(auto_command, "AutoStore", lambda: AutoStore(tmp_path))
    monkeypatch.setattr(
        auto_command, "resolve_agent_runtime_backend", lambda value=None: value or "codex"
    )
    monkeypatch.setattr(auto_command, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_command, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "StartExecuteSeedHandler", FakeHandler)

    sentinel: list[AutoProgressEvent] = []

    def cb(event: AutoProgressEvent) -> None:
        sentinel.append(event)

    result = await auto_command._run_auto(
        goal="Build a CLI",
        resume=None,
        runtime=None,
        max_interview_rounds=1,
        max_repair_rounds=1,
        skip_run=False,
        progress_callback=cb,
    )

    assert result.status == "complete"
    assert captured["progress_callback"] is cb


def test_auto_interview_driver_emits_progress_via_callback() -> None:
    """The interview driver must fire the progress callback on every save.

    AutoPipeline pushes its progress callback into the driver at the start
    of run() so the longest-running phase (interview rounds) surfaces
    live snapshots through the same contract as phase / grade / repair
    events emitted from the pipeline itself.
    """
    from ouroboros.auto.interview_driver import AutoInterviewDriver
    from ouroboros.auto.state import AutoPhase, AutoPipelineState

    captured: list[AutoProgressEvent] = []

    class _StubBackend:
        async def start(self, _goal, *, cwd):  # noqa: ARG002
            raise AssertionError("not used in this test")

        async def answer(self, _session_id, _answer):  # noqa: ARG002
            raise AssertionError("not used in this test")

        async def resume(self, _session_id):  # noqa: ARG002
            raise AssertionError("not used in this test")

    driver = AutoInterviewDriver(_StubBackend(), progress_callback=captured.append)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp")
    state.transition(AutoPhase.INTERVIEW, "asking interview round 1/12")

    driver._save(state)
    state.mark_progress("answered round 1/12 from repo_fact", tool_name="auto_answerer")
    driver._save(state)
    # A second save with the same message must not emit a duplicate.
    driver._save(state)

    assert [event.message for event in captured] == [
        "asking interview round 1/12",
        "answered round 1/12 from repo_fact",
    ]
    assert all(event.kind == "phase" for event in captured)
    assert all(event.phase == "interview" for event in captured)


def test_auto_interview_driver_swallows_callback_errors() -> None:
    from ouroboros.auto.interview_driver import AutoInterviewDriver
    from ouroboros.auto.state import AutoPhase, AutoPipelineState

    def exploding(_event):
        raise RuntimeError("observer failure")

    driver = AutoInterviewDriver(object(), progress_callback=exploding)  # type: ignore[arg-type]
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp")
    state.transition(AutoPhase.INTERVIEW, "asking interview round 1/12")

    # Must not raise even though the observer always raises.
    driver._save(state)


def test_cli_auto_quiet_suppresses_progress_lines(monkeypatch, tmp_path) -> None:
    """End-to-end: --quiet must not stream progress lines to the user."""
    from ouroboros.auto.state import AutoStore

    progress_seen: list[AutoProgressEvent] = []

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            cb = kwargs.get("progress_callback")
            # Simulate a phase emit during the run; it should be suppressed
            # whenever the CLI was invoked with --quiet.
            if cb is not None:
                cb(
                    AutoProgressEvent(
                        auto_session_id="auto_test",
                        phase="interview",
                        kind="phase",
                        message="asking interview round 1/12",
                    )
                )
            progress_seen.append(cb)  # type: ignore[arg-type]

        async def run(self, run_state):  # noqa: ANN001
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(auto_command, "AutoStore", lambda: AutoStore(tmp_path))
    monkeypatch.setattr(
        auto_command, "resolve_agent_runtime_backend", lambda value=None: value or "codex"
    )
    monkeypatch.setattr(auto_command, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_command, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "StartExecuteSeedHandler", FakeHandler)

    quiet_result = CliRunner().invoke(app, ["auto", "Build a CLI", "--quiet"])
    quiet_output = _strip_ansi(quiet_result.output)
    assert quiet_result.exit_code == 0
    assert "[auto]" not in quiet_output
    assert progress_seen[-1] is None  # quiet → no callback wired

    loud_result = CliRunner().invoke(app, ["auto", "Build a CLI"])
    loud_output = _strip_ansi(loud_result.output)
    assert loud_result.exit_code == 0
    assert "[auto] interview — asking interview round 1/12" in loud_output
    assert progress_seen[-1] is not None
