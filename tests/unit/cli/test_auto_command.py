"""Tests for the direct `ouroboros auto` CLI surface."""

from __future__ import annotations

import re
from unittest.mock import patch

from typer.testing import CliRunner

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoResumeCapability
from ouroboros.cli.commands.auto import _print_result, _print_status
from ouroboros.cli.main import app

runner = CliRunner()


def _plain(text: str) -> str:
    """Strip ANSI sequences from rich-rendered Typer output."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_auto_help_uses_direct_goal_command_shape() -> None:
    result = runner.invoke(app, ["auto", "--help"])

    assert result.exit_code == 0
    output = _plain(result.output)
    assert "Usage: ouroboros auto [OPTIONS] [GOAL]" in output
    assert "COMMAND [ARGS]" not in output
    assert "Goal/task for ooo auto" in output


def test_auto_goal_skip_run_does_not_require_subcommand() -> None:
    result_value = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_test",
        phase="complete",
        grade="A",
        seed_path="/tmp/seed.yaml",
        interview_session_id="interview_test",
    )

    def consume(coro):
        coro.close()
        return result_value

    with patch("ouroboros.cli.commands.auto.asyncio.run", side_effect=consume) as run_auto:
        result = runner.invoke(app, ["auto", "safe test goal", "--skip-run"])

    assert result.exit_code == 0
    assert run_auto.called
    assert "Auto session:" in result.output
    assert "auto_test" in result.output


def _persisted_state_with_bounds(tmp_path, *, max_interview_rounds: int, max_repair_rounds: int):
    """Persist a blocked auto session with a known loop budget for resume tests."""
    from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = "claude"
    state.max_interview_rounds = max_interview_rounds
    state.max_repair_rounds = max_repair_rounds
    state.skip_run = True
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked(
        "auto interview reached max rounds with unresolved gaps: actors",
        tool_name="interview_driver",
    )
    store = AutoStore(tmp_path)
    store.save(state)
    return state, store, state.auto_session_id


def test_resume_uses_persisted_bounds_when_cli_unspecified(tmp_path) -> None:
    """No explicit CLI bound on resume must keep the persisted budget intact."""
    import asyncio

    from ouroboros.cli.commands.auto import _run_auto

    _, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=2, max_repair_rounds=1
    )

    captured: dict[str, int] = {}

    async def fake_pipeline_run(self, state):  # noqa: ARG001
        captured["max_interview_rounds"] = self.interview_driver.max_rounds
        return AutoPipelineResult(
            status="complete",
            auto_session_id=session_id,
            phase="complete",
            grade="A",
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
    ):
        store_cls.return_value = store

        result = asyncio.run(
            _run_auto(
                goal=None,
                resume=session_id,
                runtime=None,
                max_interview_rounds=None,
                max_repair_rounds=None,
                skip_run=False,
            )
        )

    assert result.status == "complete"
    assert captured["max_interview_rounds"] == 2


def test_resume_raises_persisted_bound_when_cli_overrides_higher(tmp_path) -> None:
    """Explicit CLI value larger than persisted must raise the bound for resume."""
    import asyncio

    from ouroboros.cli.commands.auto import _run_auto

    _, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=2, max_repair_rounds=1
    )

    captured: dict[str, int] = {}

    async def fake_pipeline_run(self, state):
        captured["driver_max_rounds"] = self.interview_driver.max_rounds
        captured["state_max_interview_rounds"] = state.max_interview_rounds
        captured["state_max_repair_rounds"] = state.max_repair_rounds
        return AutoPipelineResult(
            status="complete",
            auto_session_id=session_id,
            phase="complete",
            grade="A",
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
    ):
        store_cls.return_value = store

        result = asyncio.run(
            _run_auto(
                goal=None,
                resume=session_id,
                runtime=None,
                max_interview_rounds=6,
                max_repair_rounds=None,
                skip_run=False,
            )
        )

    assert result.status == "complete"
    assert captured["driver_max_rounds"] == 6
    assert captured["state_max_interview_rounds"] == 6
    assert captured["state_max_repair_rounds"] == 1


def test_run_auto_passes_state_interview_timeout_to_driver(tmp_path) -> None:
    """Regression for #686: CLI must wire state.timeout_seconds_by_phase[interview] into driver."""
    import asyncio

    from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
    from ouroboros.cli.commands.auto import _run_auto

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = "claude"
    state.skip_run = True
    state.timeout_seconds_by_phase[AutoPhase.INTERVIEW.value] = 175
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked("auto interview reached max rounds with unresolved gaps: actors")
    store = AutoStore(tmp_path)
    store.save(state)
    session_id = state.auto_session_id

    captured: dict[str, float] = {}

    async def fake_pipeline_run(self, run_state):  # noqa: ARG001
        captured["driver_timeout_seconds"] = self.interview_driver.timeout_seconds
        return AutoPipelineResult(
            status="complete",
            auto_session_id=session_id,
            phase="complete",
            grade="A",
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
    ):
        store_cls.return_value = store

        result = asyncio.run(
            _run_auto(
                goal=None,
                resume=session_id,
                runtime=None,
                max_interview_rounds=None,
                max_repair_rounds=None,
                skip_run=False,
            )
        )

    assert result.status == "complete"
    assert captured["driver_timeout_seconds"] == 175.0


def test_run_auto_uses_default_state_interview_timeout_for_new_sessions() -> None:
    """New sessions must inherit the 600s default from AutoPipelineState."""
    import asyncio

    from ouroboros.cli.commands.auto import _run_auto

    captured: dict[str, float] = {}

    async def fake_pipeline_run(self, run_state):  # noqa: ARG001
        captured["driver_timeout_seconds"] = self.interview_driver.timeout_seconds
        return AutoPipelineResult(
            status="complete",
            auto_session_id=run_state.auto_session_id,
            phase="complete",
            grade="A",
        )

    with patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run):
        result = asyncio.run(
            _run_auto(
                goal="Build a CLI",
                resume=None,
                runtime="claude",
                max_interview_rounds=None,
                max_repair_rounds=None,
                skip_run=True,
            )
        )

    assert result.status == "complete"
    assert captured["driver_timeout_seconds"] == 600.0


def test_resume_rejects_lower_bound_override(tmp_path) -> None:
    """Tightening a bound on resume must be refused — never trap a session further."""
    import asyncio

    import pytest

    from ouroboros.cli.commands.auto import _run_auto

    _, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=4, max_repair_rounds=2
    )

    with patch("ouroboros.cli.commands.auto.AutoStore") as store_cls:
        store_cls.return_value = store

        with pytest.raises(ValueError, match="refuse to tighten"):
            asyncio.run(
                _run_auto(
                    goal=None,
                    resume=session_id,
                    runtime=None,
                    max_interview_rounds=2,
                    max_repair_rounds=None,
                    skip_run=False,
                )
            )


def test_auto_status_prints_authoring_and_run_backend(tmp_path) -> None:
    """`ooo auto --status` must show authoring + run backend labels."""
    from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = "codex"
    state.opencode_mode = None
    state.transition(AutoPhase.INTERVIEW, "interview")
    store = AutoStore(tmp_path)
    store.save(state)

    with patch("ouroboros.cli.commands.auto.AutoStore") as store_cls:
        store_cls.return_value = store
        result = runner.invoke(app, ["auto", "--status", "--resume", state.auto_session_id])

    assert result.exit_code == 0
    output = _plain(result.output)
    assert "Authoring backend: in-process (codex)" in output
    assert "Run backend: codex" in output


def test_auto_status_reports_in_process_for_persisted_opencode_plugin(tmp_path) -> None:
    """Persisted opencode-plugin (saved by MCP entry point) renders correctly.

    Both auto entry points demote plugin → subprocess for authoring,
    so the status output must read in-process for authoring even when
    the persisted state still carries `plugin` (this happens for
    sessions created by `mcp/tools/auto_handler.py`, which keeps
    `plugin` for the run-handoff handler only).
    """
    from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = "opencode"
    state.opencode_mode = "plugin"
    state.transition(AutoPhase.INTERVIEW, "interview")
    store = AutoStore(tmp_path)
    store.save(state)

    with patch("ouroboros.cli.commands.auto.AutoStore") as store_cls:
        store_cls.return_value = store
        result = runner.invoke(app, ["auto", "--status", "--resume", state.auto_session_id])

    assert result.exit_code == 0
    output = _plain(result.output)
    assert "Authoring backend: in-process (opencode)" in output
    assert "Run backend: opencode (plugin)" in output
    assert "dispatched" not in output


def test_auto_status_reports_subprocess_for_cli_demoted_session(tmp_path) -> None:
    """Sessions created via the CLI entry point persist subprocess for both phases."""
    from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = "opencode"
    state.opencode_mode = "subprocess"
    state.transition(AutoPhase.INTERVIEW, "interview")
    store = AutoStore(tmp_path)
    store.save(state)

    with patch("ouroboros.cli.commands.auto.AutoStore") as store_cls:
        store_cls.return_value = store
        result = runner.invoke(app, ["auto", "--status", "--resume", state.auto_session_id])

    assert result.exit_code == 0
    output = _plain(result.output)
    assert "Authoring backend: in-process (opencode)" in output
    assert "Run backend: opencode (subprocess)" in output


def test_auto_result_pipeline_carries_runtime_labels(tmp_path) -> None:
    """AutoPipelineResult propagates runtime_backend/opencode_mode for printing."""
    import asyncio

    from ouroboros.cli.commands.auto import _run_auto

    captured: dict[str, str | None] = {}

    async def fake_pipeline_run(self, state):  # noqa: ARG001
        captured["runtime"] = state.runtime_backend
        captured["mode"] = state.opencode_mode
        return AutoPipelineResult(
            status="complete",
            auto_session_id="auto_test",
            phase="complete",
            grade="A",
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
        )

    with patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run):
        result = asyncio.run(
            _run_auto(
                goal="safe goal",
                resume=None,
                runtime="codex",
                max_interview_rounds=2,
                max_repair_rounds=1,
                skip_run=True,
            )
        )

    assert captured["runtime"] == "codex"
    assert captured["mode"] is None
    assert result.runtime_backend == "codex"
    assert result.opencode_mode is None


def test_run_auto_complete_product_configures_ralph_evolutionary_loop(
    tmp_path, monkeypatch
) -> None:
    """Regression for #1090: CLI complete-product must not create a bare Ralph handler.

    A bare ``RalphHandler(agent_runtime_backend=...)`` constructs an
    ``EvolveStepHandler`` without an ``EvolutionaryLoop``. The background Ralph
    job then fails at handoff time with ``EvolutionaryLoop not configured``.
    """
    import asyncio

    from ouroboros.cli.commands.auto import _run_auto

    captured = {}
    monkeypatch.chdir(tmp_path)

    async def fake_pipeline_run(self, run_state):  # noqa: ARG001
        ralph_starter = self.ralph_starter
        captured["evolve_handler"] = ralph_starter.handler._evolve_handler  # noqa: SLF001
        captured["project_dir"] = ralph_starter.project_dir
        return AutoPipelineResult(
            status="complete",
            auto_session_id=run_state.auto_session_id,
            phase="complete",
            grade="A",
        )

    with patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run):
        result = asyncio.run(
            _run_auto(
                goal="safe goal",
                resume=None,
                runtime="hermes",
                max_interview_rounds=2,
                max_repair_rounds=1,
                skip_run=False,
                complete_product=True,
            )
        )

    assert result.status == "complete"
    evolve_handler = captured.get("evolve_handler")
    assert evolve_handler is not None
    assert getattr(evolve_handler, "evolutionary_loop", None) is not None
    assert captured["project_dir"] == str(tmp_path)


def test_run_auto_demotes_plugin_to_subprocess_in_state(tmp_path) -> None:
    """`_run_auto` must overwrite persisted plugin opencode_mode to subprocess."""
    import asyncio

    from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
    from ouroboros.cli.commands.auto import _run_auto

    state = AutoPipelineState(goal="resume goal", cwd=str(tmp_path))
    state.runtime_backend = "opencode"
    state.opencode_mode = "plugin"
    state.skip_run = True
    state.max_interview_rounds = 2
    state.max_repair_rounds = 1
    state.transition(AutoPhase.INTERVIEW, "interview")
    store = AutoStore(tmp_path)
    store.save(state)

    captured: dict[str, str | None] = {}

    async def fake_pipeline_run(self, state):  # noqa: ARG001
        captured["runtime"] = state.runtime_backend
        captured["mode"] = state.opencode_mode
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            grade="A",
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
    ):
        store_cls.return_value = store
        asyncio.run(
            _run_auto(
                goal=None,
                resume=state.auto_session_id,
                runtime=None,
                max_interview_rounds=None,
                max_repair_rounds=None,
                skip_run=False,
            )
        )

    assert captured["runtime"] == "opencode"
    assert captured["mode"] == "subprocess"


# ---------------------------------------------------------------------------
# _print_status / _print_result — capability-aware resume hint rendering (#688)
# ---------------------------------------------------------------------------


def _capture_status(state: AutoPipelineState) -> str:
    """Capture the bare-text rendering of :func:`_print_status` for assertions."""
    from ouroboros.cli.formatters import console

    with console.capture() as capture:
        _print_status(state)
    return _plain(capture.get())


def _capture_result(result: AutoPipelineResult) -> str:
    """Capture the bare-text rendering of :func:`_print_result` for assertions."""
    from ouroboros.cli.formatters import console

    with console.capture() as capture:
        _print_result(result, show_ledger=False)
    return _plain(capture.get())


def _capture_result_with_ledger(result: AutoPipelineResult) -> str:
    """Capture :func:`_print_result` with the optional ledger block enabled."""
    from ouroboros.cli.formatters import console

    with console.capture() as capture:
        _print_result(result, show_ledger=True)
    return _plain(capture.get())


def _state_in_phase(phase: AutoPhase) -> AutoPipelineState:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.auto_session_id = "auto_render"
    if phase is AutoPhase.CREATED:
        return state
    state.transition(AutoPhase.INTERVIEW, "interview")
    if phase is AutoPhase.INTERVIEW:
        return state
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    if phase is AutoPhase.SEED_GENERATION:
        return state
    state.transition(AutoPhase.REVIEW, "review")
    if phase is AutoPhase.REVIEW:
        return state
    state.transition(AutoPhase.RUN, "run")
    return state


def test_print_status_resume_capability_resume() -> None:
    state = _state_in_phase(AutoPhase.INTERVIEW)
    output = _capture_status(state)

    assert "Resume: ooo auto --resume auto_render" in output
    assert "Resume (partial)" not in output
    assert "Retry:" not in output
    assert "Start fresh" not in output


def test_print_result_show_ledger_renders_assumption_sources() -> None:
    from ouroboros.auto.ledger import AssumptionRecord

    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_assumptions",
        phase="complete",
        assumption_sources=(
            AssumptionRecord(
                text="Existing patterns",
                source="conservative_default",
                confidence=0.85,
            ),
            AssumptionRecord(
                text="Use [project] defaults",
                source="assumption[ledger]",
                confidence=0.7,
            ),
        ),
    )

    output = _capture_result_with_ledger(result)

    assert "Assumption sources:" in output
    assert ("source=conservative_default; confidence=0.85; text=Existing patterns") in output
    assert (
        "source=assumption[ledger]; confidence=0.70; text=Use [project] defaults"
    ) in output


def test_print_status_resume_capability_partial() -> None:
    state = _state_in_phase(AutoPhase.INTERVIEW)
    state.interview_session_id = "interview_1"
    state.mark_blocked("interview.answer timed out", tool_name="interview.answer")

    output = _capture_status(state)

    assert "Resume (partial): ooo auto --resume auto_render" in output
    assert "some progress preserved but the exact pick-up point may be approximate" in output


def test_print_status_resume_capability_retry() -> None:
    state = _state_in_phase(AutoPhase.INTERVIEW)
    state.mark_blocked("interview.start timed out", tool_name="interview.start")

    output = _capture_status(state)

    assert "Retry: ooo auto --resume auto_render" in output
    assert "no prior session context" in output
    assert "re-runs the failed step from scratch" in output


def test_print_status_resume_capability_none_blocked_emits_start_fresh() -> None:
    state = _state_in_phase(AutoPhase.INTERVIEW)
    state.mark_blocked("internal guard fired", tool_name="auto_pipeline")

    output = _capture_status(state)

    assert "Start fresh: ooo auto 'Build a CLI'" in output
    assert "Resume:" not in output
    assert "Retry:" not in output


def test_print_status_start_fresh_shell_quotes_goal_with_metacharacters() -> None:
    """Security: a goal with shell meta-characters must be safely quoted."""
    state = AutoPipelineState(goal='evil"; rm -rf /; echo "', cwd="/tmp/project")
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked("internal guard fired", tool_name="auto_pipeline")

    output = _capture_status(state)

    # The rendered command, when tokenised by ``shlex.split``, must recover
    # the original goal exactly — i.e. the payload cannot break out of the
    # shell quoting and become its own argument.
    import shlex

    rendered = next(line for line in output.splitlines() if "Start fresh" in line)
    cmd = rendered.split("Start fresh:", 1)[1].strip()
    tokens = shlex.split(cmd)
    assert tokens == ["ooo", "auto", 'evil"; rm -rf /; echo "']


def test_print_status_start_fresh_escapes_rich_markup_in_goal() -> None:
    """Security: a goal with Rich markup tokens must not render as styled."""
    state = AutoPipelineState(goal="[red]ALERT[/]", cwd="/tmp/project")
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked("internal guard fired", tool_name="auto_pipeline")

    output = _capture_status(state)

    # The literal markup must survive into the rendered output (since it was
    # escaped before Rich could interpret it).
    assert "[red]ALERT[/]" in output


def test_print_status_resume_capability_none_complete_emits_no_resume_line() -> None:
    """Critic fix C5: COMPLETE produces no resume/retry/start-fresh hint."""
    state = _state_in_phase(AutoPhase.REVIEW)
    state.transition(AutoPhase.COMPLETE, "done")

    output = _capture_status(state)

    assert "Resume:" not in output
    assert "Retry:" not in output
    assert "Start fresh" not in output


def test_print_result_resume_capability_resume() -> None:
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_r1",
        phase="complete",
        resume_capability=AutoResumeCapability.RESUME,
    )

    output = _capture_result(result)

    assert "Resume: ooo auto --resume auto_r1" in output


def test_print_result_resume_capability_partial() -> None:
    result = AutoPipelineResult(
        status="blocked",
        auto_session_id="auto_r2",
        phase="blocked",
        resume_capability=AutoResumeCapability.PARTIAL_RESUME,
    )

    output = _capture_result(result)

    assert "Resume (partial): ooo auto --resume auto_r2" in output
    assert "some progress preserved" in output


def test_print_result_resume_capability_retry() -> None:
    result = AutoPipelineResult(
        status="blocked",
        auto_session_id="auto_r3",
        phase="blocked",
        resume_capability=AutoResumeCapability.RETRY,
    )

    output = _capture_result(result)

    assert "Retry: ooo auto --resume auto_r3" in output
    assert "re-runs the failed step from scratch" in output


def test_print_result_resume_capability_none_emits_no_resume_line() -> None:
    """``_print_result`` cannot reach ``state.goal``, so NONE prints nothing."""
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_r4",
        phase="complete",
        resume_capability=AutoResumeCapability.NONE,
    )

    output = _capture_result(result)

    assert "Resume:" not in output
    assert "Retry:" not in output
    assert "Start fresh" not in output


def test_print_result_handoff_completion_is_not_labeled_product_complete() -> None:
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_handoff",
        phase="complete",
        run_handoff_status="started",
        job_id="job_123",
        execution_id="exec_123",
        resume_capability=AutoResumeCapability.NONE,
    )

    output = _capture_result(result)

    assert "Auto run handoff started" in output
    assert "Status: run_handoff_started" in output
    assert "Product status: not verified complete" in output
    assert "Auto pipeline completed" not in output


def test_print_result_complete_product_completion_suppresses_stale_handoff_status() -> None:
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_complete_product",
        phase="complete",
        run_handoff_status="started",
        job_id="job_123",
        execution_id="exec_123",
        run_session_id="orch_123",
        ralph_job_id="job_ralph",
        ralph_lineage_id="lineage_123",
        ralph_dispatch_mode="sync",
        resume_capability=AutoResumeCapability.NONE,
    )

    output = _capture_result(result)

    assert "Auto pipeline completed" in output
    assert "Status: complete" in output
    assert "Product status: completed by Ralph loop" in output
    assert "Status: run_handoff_started" not in output
    assert "Run handoff status: started" not in output
    assert "Product status: not verified complete" not in output


def test_print_result_complete_product_completion_suppresses_retry_handoff_status() -> None:
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_complete_product_retry",
        phase="complete",
        run_handoff_status="ralph_retry_after_blocker",
        job_id="job_123",
        execution_id="exec_123",
        run_session_id="orch_123",
        ralph_job_id="job_ralph",
        ralph_lineage_id="lineage_123",
        ralph_dispatch_mode="job",
        resume_capability=AutoResumeCapability.NONE,
    )

    output = _capture_result(result)

    assert "Auto pipeline completed" in output
    assert "Product status: completed by Ralph loop" in output
    assert "Run handoff status: ralph_retry_after_blocker" not in output


def test_print_result_plugin_ralph_completion_remains_external_pending() -> None:
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_plugin_complete_product",
        phase="complete",
        run_handoff_status="started",
        run_handoff_guidance=(
            "Ralph loop delegated to the OpenCode plugin child session. "
            "Track progress through the OpenCode Task widget."
        ),
        job_id="job_123",
        execution_id="exec_123",
        run_session_id="orch_123",
        ralph_lineage_id="lineage_123",
        ralph_dispatch_mode="plugin",
        resume_capability=AutoResumeCapability.NONE,
    )

    output = _capture_result(result)

    assert "Auto pipeline completed" in output
    assert "Status: complete" in output
    assert "Product status: not verified complete; Ralph loop is external/pending" in output
    assert "Product status: completed by Ralph loop" not in output
    assert "Run handoff status: started" in output
    assert "Run handoff guidance: Ralph loop delegated to the OpenCode plugin" in output


def test_print_result_attached_completion_remains_product_complete() -> None:
    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_attached",
        phase="complete",
        run_handoff_status="attached",
        attached_run_handle="exec_existing",
        attached_run_source="operator",
        attached_at="2026-05-07T00:00:00+00:00",
        resume_capability=AutoResumeCapability.NONE,
    )

    output = _capture_result(result)

    assert "Auto pipeline completed" in output
    assert "Status: complete" in output
    assert "Status: run_handoff_started" not in output
    assert "Product status: not verified complete" not in output
