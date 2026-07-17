"""Tests for the direct `ouroboros auto` CLI surface."""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
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
    assert "--efficiency-mode" in output
    assert "--frugality-assurance" in output
    assert "--codex-recovery" in output


def test_auto_cli_forwards_execution_preferences() -> None:
    """Typer must forward both recovery preference flags unchanged."""
    captured: dict[str, object] = {}

    async def fake_run_auto(**kwargs: object) -> AutoPipelineResult:
        captured.update(kwargs)
        return AutoPipelineResult(
            status="complete",
            auto_session_id="auto_preferences",
            phase="complete",
            grade="A",
        )

    with patch("ouroboros.cli.commands.auto._run_auto", new=fake_run_auto):
        result = runner.invoke(
            app,
            [
                "auto",
                "safe preference goal",
                "--skip-run",
                "--efficiency-mode",
                "quality_first",
                "--frugality-assurance",
                "strict",
            ],
        )

    assert result.exit_code == 0
    assert captured["efficiency_mode"] == "quality_first"
    assert captured["frugality_assurance"] == "strict"


@pytest.mark.parametrize(
    ("efficiency", "assurance", "expected_efficiency", "expected_assurance", "explicit"),
    [
        (None, None, "adaptive", "observe", False),
        ("adaptive", None, "adaptive", "observe", False),
        ("quality_first", None, "quality_first", "off", False),
        ("quality_first", "observe", "quality_first", "observe", True),
        ("adaptive", "strict", "adaptive", "strict", True),
    ],
)
def test_run_auto_persists_execution_preferences(
    tmp_path,
    monkeypatch,
    efficiency,
    assurance,
    expected_efficiency,
    expected_assurance,
    explicit,
) -> None:
    """Every supported preference combination round-trips through AutoStore."""
    import asyncio

    from ouroboros.auto.state import AutoStore
    from ouroboros.cli.commands.auto import _run_auto

    monkeypatch.chdir(tmp_path)
    store = AutoStore(tmp_path / "auto")
    captured: dict[str, str] = {}

    async def fake_pipeline_run(self, state):  # noqa: ARG001
        captured["id"] = state.auto_session_id
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            grade="A",
            efficiency_mode=state.efficiency_mode,
            frugality_assurance=state.frugality_assurance,
        )

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=fake_pipeline_run),
    ):
        store_cls.return_value = store
        asyncio.run(
            _run_auto(
                goal="safe preference goal",
                resume=None,
                runtime="codex",
                max_interview_rounds=2,
                max_repair_rounds=1,
                skip_run=True,
                efficiency_mode=efficiency,
                frugality_assurance=assurance,
            )
        )

    reloaded = store.load(captured["id"])
    assert reloaded.efficiency_mode == expected_efficiency
    assert reloaded.frugality_assurance == expected_assurance
    assert reloaded.frugality_assurance_explicit is explicit


def test_resume_preference_override_rejected_without_state_mutation(tmp_path) -> None:
    """Rejected mutable resume flags must not rewrite the durable session."""
    import asyncio

    from ouroboros.cli.commands.auto import _run_auto

    state, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=2, max_repair_rounds=1
    )
    path = store.path_for(session_id)
    before = path.read_bytes()

    with patch("ouroboros.cli.commands.auto.AutoStore") as store_cls:
        store_cls.return_value = store
        with pytest.raises(ValueError, match="cannot be changed on resume"):
            asyncio.run(
                _run_auto(
                    goal=None,
                    resume=session_id,
                    runtime=None,
                    max_interview_rounds=None,
                    max_repair_rounds=None,
                    skip_run=False,
                    efficiency_mode="quality_first",
                )
            )

    assert path.read_bytes() == before
    assert store.load(session_id).efficiency_mode == state.efficiency_mode


def test_auto_status_prints_execution_preferences(tmp_path) -> None:
    from ouroboros.auto.state import AutoStore

    state = AutoPipelineState(goal="status", cwd=str(tmp_path))
    state.efficiency_mode = "quality_first"
    state.frugality_assurance = "strict"
    state.frugality_assurance_explicit = True
    store = AutoStore(tmp_path)
    store.save(state)

    with patch("ouroboros.cli.commands.auto.AutoStore") as store_cls:
        store_cls.return_value = store
        result = runner.invoke(app, ["auto", "--status", "--resume", state.auto_session_id])

    output = _plain(result.output)
    assert result.exit_code == 0
    assert "Efficiency mode: quality_first" in output
    assert "Frugality assurance: strict (explicit)" in output


def test_auto_help_documents_detached_wait_and_retrieve_commands() -> None:
    result = runner.invoke(app, ["auto", "--help"])

    assert result.exit_code == 0
    output = _plain(result.output)
    assert "Detached auto background work is non-terminal tracked work" in output
    assert "ouroboros job wait JOB_ID" in output
    assert "Retrieve completed results with:" in output
    assert "ouroboros job result JOB_ID" in output


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


def test_auto_detached_start_output_includes_handles_and_wait_retrieve_guidance() -> None:
    def result_value() -> AutoPipelineResult:
        return AutoPipelineResult(
            status="detached",
            auto_session_id="auto_detached_123",
            phase=AutoPhase.RALPH_HANDOFF.value,
            grade="A",
            seed_path="/tmp/seed.yaml",
            job_id="job_execute_123",
            execution_id="exec_detached_123",
            run_session_id="orch_detached_123",
            run_handoff_status="started",
            ralph_job_id="job_ralph_456",
            ralph_lineage_id="lineage_ralph_789",
            ralph_dispatch_mode="job",
        )

    def consume(coro):
        coro.close()
        return result_value()

    with patch("ouroboros.cli.commands.auto.asyncio.run", side_effect=consume):
        first = runner.invoke(app, ["auto", "safe detached goal", "--complete-product"])
        second = runner.invoke(app, ["auto", "safe detached goal", "--complete-product"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    output = _plain(first.output)
    assert output == _plain(second.output)
    assert not re.search(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]", output)
    assert "Last progress at:" not in output
    assert "Attached at:" not in output
    assert "Run reconciled at:" not in output
    assert output == (
        "┌───────── Info ─────────┐\n"
        "│ Auto pipeline detached │\n"
        "└────────────────────────┘\n"
        "Auto session: auto_detached_123\n"
        "Status: detached\n"
        "Product status: not verified complete; background work is still running\n"
        "Authoring backend: in-process (unspecified)\n"
        "Run backend: unspecified\n"
        "Efficiency mode: adaptive\n"
        "Frugality assurance: observe\n"
        "Seed grade: A\n"
        "Seed: /tmp/seed.yaml\n"
        "Seed origin: none\n"
        "Execution started:\n"
        "  Job ID: job_execute_123\n"
        "  Execution ID: exec_detached_123\n"
        "  Session ID: orch_detached_123\n"
        "Run handoff status: started\n"
        "Detached result handles:\n"
        "  Auto session ID: auto_detached_123\n"
        "  Execution job ID: job_execute_123\n"
        "  Ralph job ID: job_ralph_456\n"
        "  Ralph lineage ID: lineage_ralph_789\n"
        "Wait: ooo auto --resume auto_detached_123\n"
        "Retrieve: ooo auto --status --resume auto_detached_123\n"
        "Wait job (CLI): ouroboros job wait job_ralph_456\n"
        "Retrieve job (CLI): ouroboros job result job_ralph_456\n"
        'Wait job (MCP): ouroboros_job_wait(job_id="job_ralph_456")\n'
        'Retrieve job (MCP): ouroboros_job_result(job_id="job_ralph_456")\n'
        "Resume: ooo auto --resume auto_detached_123\n"
    )
    assert "Auto pipeline detached" in output
    assert "Status: detached" in output
    assert "Product status: not verified complete; background work is still running" in output
    assert "Auto pipeline completed" not in output
    assert "Auto session: auto_detached_123" in output
    assert "Execution started:" in output
    assert "Job ID: job_execute_123" in output
    assert "Execution ID: exec_detached_123" in output
    assert "Session ID: orch_detached_123" in output
    assert "Run handoff status: started" in output
    assert "Detached result handles:" in output
    assert "Auto session ID: auto_detached_123" in output
    assert "Execution job ID: job_execute_123" in output
    assert "Ralph job ID: job_ralph_456" in output
    assert "Ralph lineage ID: lineage_ralph_789" in output
    assert "Wait: ooo auto --resume auto_detached_123" in output
    assert "Retrieve: ooo auto --status --resume auto_detached_123" in output
    assert "Wait job (CLI): ouroboros job wait job_ralph_456" in output
    assert "Retrieve job (CLI): ouroboros job result job_ralph_456" in output
    assert 'Wait job (MCP): ouroboros_job_wait(job_id="job_ralph_456")' in output
    assert 'Retrieve job (MCP): ouroboros_job_result(job_id="job_ralph_456")' in output


def test_auto_detached_start_output_includes_pollable_cli_job_handle() -> None:
    """Runnable CLI check for the detached auto job handle printed at start."""
    result_value = AutoPipelineResult(
        status="detached",
        auto_session_id="auto_pollable_start",
        phase=AutoPhase.RALPH_HANDOFF.value,
        grade="A",
        seed_path="/tmp/seed.yaml",
        job_id="job_execute_pollable",
        ralph_job_id="job_ralph_pollable",
        ralph_lineage_id="lineage_pollable",
        ralph_dispatch_mode="job",
    )

    def consume(coro):
        coro.close()
        return result_value

    with patch("ouroboros.cli.commands.auto.asyncio.run", side_effect=consume):
        result = runner.invoke(app, ["auto", "safe detached goal", "--complete-product"])

    output = _plain(result.output)
    assert result.exit_code == 0
    assert "Status: detached" in output
    assert "Detached result handles:" in output
    assert "Ralph job ID: job_ralph_pollable" in output
    assert "Wait job (CLI): ouroboros job wait job_ralph_pollable" in output
    assert "Retrieve job (CLI): ouroboros job result job_ralph_pollable" in output


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


def test_run_auto_policy_args_override_detected_coding_defaults(tmp_path, monkeypatch) -> None:
    import asyncio

    from ouroboros.auto.state import AutoCommitPolicy, AutoWorktreePolicy
    from ouroboros.cli.commands.auto import _run_auto

    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    async def fake_pipeline_run(self, run_state):  # noqa: ARG001
        captured["domain"] = run_state.active_domain_profile_name
        captured["commit_policy"] = run_state.commit_policy
        captured["worktree_policy"] = run_state.worktree_policy
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
                commit_policy="none",
                worktree_policy="current",
            )
        )

    assert result.status == "complete"
    assert captured["domain"] == "coding"
    assert captured["commit_policy"] is AutoCommitPolicy.NONE
    assert captured["worktree_policy"] is AutoWorktreePolicy.CURRENT


def test_run_auto_accepts_current_worktree_policy_alias(tmp_path, monkeypatch) -> None:
    import asyncio

    from ouroboros.auto.state import AutoWorktreePolicy
    from ouroboros.cli.commands.auto import _run_auto

    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    async def fake_pipeline_run(self, run_state):  # noqa: ARG001
        captured["worktree_policy"] = run_state.worktree_policy
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
                worktree_policy="reuse_current",
            )
        )

    assert result.status == "complete"
    assert captured["worktree_policy"] is AutoWorktreePolicy.CURRENT


def test_run_auto_rejects_complete_product_short_timeout(tmp_path, monkeypatch) -> None:
    import asyncio

    import pytest

    from ouroboros.cli.commands.auto import _run_auto

    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="complete_product=true requires --timeout >= 1800"):
        asyncio.run(
            _run_auto(
                goal="Build a product",
                resume=None,
                runtime="claude",
                max_interview_rounds=None,
                max_repair_rounds=None,
                skip_run=False,
                complete_product=True,
                pipeline_timeout_seconds=100,
            )
        )


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


def test_print_status_renders_full_pending_question_without_truncation() -> None:
    state = _state_in_phase(AutoPhase.INTERVIEW)
    state.pending_question = (
        "This is a deliberately long pending question that should remain visible "
        "because operators need to understand what the autonomous interview is "
        "asking before the answerer responds, even when the question is longer "
        "than the old compact one-line preview limit."
    )

    output = _capture_status(state)

    assert "Pending question:" in output
    assert "old" in output
    assert "compact one-line preview limit." in output
    assert "..." not in output


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
    assert ("source=assumption[ledger]; confidence=0.70; text=Use [project] defaults") in output


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


@pytest.mark.parametrize(
    "extra_args",
    [
        ["changed goal"],
        ["--runtime", "codex"],
        ["--max-interview-rounds", "9"],
        ["--max-repair-rounds", "9"],
        ["--skip-run"],
        ["--efficiency-mode", "adaptive"],
        ["--frugality-assurance", "observe"],
        ["--timeout", "900"],
        ["--complete-product"],
        ["--domain", "coding"],
        ["--commit-policy", "none"],
        ["--worktree-policy", "current"],
        ["--attach-execution", "exec_external"],
        ["--attach-job", "job_external"],
        ["--attach-session", "session_external"],
        ["--attach-source", "operator"],
        ["--reconcile-run"],
        ["--reconcile-source", "operator"],
    ],
)
def test_codex_recovery_resume_rejects_mutating_arguments_before_state_change(
    tmp_path, extra_args
) -> None:
    """Recovery resume is an immutable replay command, not a retarget surface."""
    state, store, session_id = _persisted_state_with_bounds(
        tmp_path, max_interview_rounds=2, max_repair_rounds=1
    )
    path = store.path_for(session_id)
    before = path.read_bytes()

    async def must_not_run(**_kwargs):  # noqa: ANN202
        raise AssertionError("invalid recovery resume reached execution")

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto._run_auto", new=must_not_run),
    ):
        store_cls.return_value = store
        result = runner.invoke(
            app,
            ["auto", *extra_args, "--resume", session_id, "--codex-recovery"],
        )

    assert result.exit_code == 1
    assert "immutable" in _plain(result.output).lower()
    assert path.read_bytes() == before
    assert store.load(session_id).goal == state.goal


def test_codex_recovery_flag_is_forwarded_to_run_auto() -> None:
    captured: dict[str, object] = {}

    async def fake_run_auto(**kwargs):  # noqa: ANN202
        captured.update(kwargs)
        return AutoPipelineResult(
            status="complete",
            auto_session_id="auto_recovery",
            phase="complete",
            grade="A",
            seed_path="seed.yaml",
            recovery_terminal_proof=True,
        )

    with patch("ouroboros.cli.commands.auto._run_auto", new=fake_run_auto):
        result = runner.invoke(
            app,
            [
                "auto",
                "safe recovery",
                "--runtime",
                "codex",
                "--codex-recovery",
                "--skip-run",
            ],
        )

    assert result.exit_code == 0
    assert captured["codex_recovery"] is True


@pytest.mark.parametrize("handoff_status", ["attached", "started"])
def test_codex_recovery_rejects_unverified_complete_handoff(tmp_path, handoff_status) -> None:
    """A COMPLETE phase cannot launder attached/pending work into recovery success."""

    async def fake_run_auto(**_kwargs):  # noqa: ANN202
        return AutoPipelineResult(
            status="complete",
            auto_session_id="auto_unverified",
            phase="complete",
            job_id="job_unverified",
            execution_id="exec_unverified",
            run_handoff_status=handoff_status,
        )

    from ouroboros.auto.state import AutoStore

    store = AutoStore(tmp_path)
    with (
        patch("ouroboros.cli.commands.auto._run_auto", new=fake_run_auto),
        patch("ouroboros.cli.commands.auto.AutoStore", return_value=store),
    ):
        result = runner.invoke(
            app,
            ["auto", "--resume", "auto_unverified", "--codex-recovery"],
        )

    assert result.exit_code == 1
    assert "Auto pipeline completed" not in _plain(result.output)


def test_codex_recovery_rejects_stale_complete_with_handles_and_blocker(tmp_path) -> None:
    """Missing handoff proof plus a durable error can never exit recovery zero."""

    async def fake_run_auto(**_kwargs):  # noqa: ANN202
        return AutoPipelineResult(
            status="complete",
            auto_session_id="auto_stale_complete",
            phase="complete",
            job_id="job_unknown",
            run_handoff_status=None,
            blocker="snapshot transport failed",
        )

    from ouroboros.auto.state import AutoStore

    with (
        patch("ouroboros.cli.commands.auto._run_auto", new=fake_run_auto),
        patch("ouroboros.cli.commands.auto.AutoStore", return_value=AutoStore(tmp_path)),
    ):
        result = runner.invoke(
            app,
            ["auto", "--resume", "auto_stale_complete", "--codex-recovery"],
        )

    assert result.exit_code == 1
    assert "Auto pipeline completed" not in _plain(result.output)


@pytest.mark.parametrize("extra_args", [["--attach-job", "job_external"], ["--no-wait"]])
def test_codex_recovery_status_rejects_mutating_options_before_load(tmp_path, extra_args) -> None:
    """The read-only status early return cannot bypass immutable recovery parsing."""
    from ouroboros.auto.state import AutoStore

    state = AutoPipelineState(goal="status guard", cwd=str(tmp_path))
    store = AutoStore(tmp_path)
    path = store.save(state)
    before = path.read_bytes()

    with patch("ouroboros.cli.commands.auto.AutoStore", return_value=store):
        result = runner.invoke(
            app,
            [
                "auto",
                "--status",
                "--resume",
                state.auto_session_id,
                "--codex-recovery",
                *extra_args,
            ],
        )

    assert result.exit_code == 1
    assert path.read_bytes() == before
    assert (
        "immutable" in _plain(result.output).lower() or "no-wait" in _plain(result.output).lower()
    )


def test_safe_default_cwd_preserves_root(monkeypatch) -> None:
    """The resolved host cwd is never silently retargeted to HOME."""
    from pathlib import Path

    from ouroboros.cli.commands.auto import _safe_default_cwd

    monkeypatch.setattr("ouroboros.cli.commands.auto.Path.cwd", lambda: Path("/"))
    monkeypatch.setattr("ouroboros.cli.commands.auto.os.access", lambda *_args: True)

    assert _safe_default_cwd() == Path("/")


def test_run_auto_rejects_stale_complete_before_checkpoint_or_handler_build(
    tmp_path,
) -> None:
    """Loaded COMPLETE is proof-checked before terminal side effects."""
    import asyncio

    from ouroboros.auto.state import AutoCommitPolicy, AutoStore
    from ouroboros.cli.commands.auto import _run_auto

    state = AutoPipelineState(goal="stale complete", cwd=str(tmp_path))
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.transition(AutoPhase.COMPLETE, "legacy handoff")
    state.job_id = "job_unknown"
    state.run_handoff_status = None
    state.last_error = "snapshot transport failed"
    state.commit_policy = AutoCommitPolicy.FINAL_ONLY
    store = AutoStore(tmp_path)
    path = store.save(state)
    before = path.read_bytes()

    with (
        patch("ouroboros.cli.commands.auto.AutoStore", return_value=store),
        patch("ouroboros.auto.pipeline.checkpoint_final_auto") as checkpoint,
    ):
        with pytest.raises(ValueError, match="unverified persisted COMPLETE"):
            asyncio.run(
                _run_auto(
                    goal=None,
                    resume=state.auto_session_id,
                    runtime=None,
                    max_interview_rounds=None,
                    max_repair_rounds=None,
                    skip_run=False,
                    codex_recovery=True,
                )
            )

    checkpoint.assert_not_called()
    assert path.read_bytes() == before


def test_codex_recovery_rejects_legacy_completed_marker_without_terminal_proof() -> None:
    """A legacy success label cannot stand in for durable lifecycle evidence."""
    from ouroboros.cli.commands.auto import _codex_recovery_state_has_terminal_proof

    state = AutoPipelineState(goal="legacy completed marker", cwd=".")
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.transition(AutoPhase.COMPLETE, "legacy success")
    state.job_id = "job_legacy"
    state.execution_id = "exec_legacy"
    state.run_handoff_status = "completed"

    assert state.run_terminal_job_status is None
    assert state.run_terminal_status is None
    assert state.run_terminal_success is None
    assert _codex_recovery_state_has_terminal_proof(state) is False


def test_codex_recovery_rejects_handleless_partial_product_even_with_seed_shape(
    monkeypatch,
) -> None:
    """A degraded Seed is not an explicit verified skip-run terminal."""
    from ouroboros.cli.commands.auto import _codex_recovery_state_result_has_terminal_proof

    state = AutoPipelineState(goal="partial recovery", cwd=".")
    state.skip_run = True
    state.last_grade = "A"
    state.seed_path = "seed.yaml"
    state.seed_artifact = {"validated": "by patched receipt boundary"}
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.COMPLETE, "partial")
    monkeypatch.setattr(
        "ouroboros.cli.commands.auto._codex_recovery_seed_receipt_is_valid",
        lambda _state: True,
    )
    result = AutoPipelineResult(
        status="complete",
        auto_session_id=state.auto_session_id,
        phase="complete",
        grade="A",
        required_grade="A",
        skip_run=True,
        seed_path=state.seed_path,
        partial_product=True,
        artifact_state="complete_unverified",
    )

    assert _codex_recovery_state_result_has_terminal_proof(state, result) is False


def test_codex_recovery_accepts_only_explicit_verified_skip_run(
    monkeypatch,
) -> None:
    """Handleless success is bound to durable state intent and Seed receipt."""
    from ouroboros.cli.commands.auto import _codex_recovery_state_result_has_terminal_proof

    state = AutoPipelineState(goal="verified seed only", cwd=".")
    state.skip_run = True
    state.last_grade = "A"
    state.seed_path = "seed.yaml"
    state.seed_artifact = {"validated": "by patched receipt boundary"}
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.COMPLETE, "skip run")
    monkeypatch.setattr(
        "ouroboros.cli.commands.auto._codex_recovery_seed_receipt_is_valid",
        lambda _state: True,
    )
    result = AutoPipelineResult(
        status="complete",
        auto_session_id=state.auto_session_id,
        phase="complete",
        grade="A",
        required_grade="A",
        skip_run=True,
        seed_path=state.seed_path,
        artifact_state="complete_verified",
    )

    assert _codex_recovery_state_result_has_terminal_proof(state, result) is True


def test_codex_recovery_rejects_skip_run_below_required_grade(monkeypatch) -> None:
    """A nonempty grade is not enough when it misses the persisted floor."""
    from ouroboros.cli.commands.auto import _codex_recovery_state_result_has_terminal_proof

    state = AutoPipelineState(goal="low grade seed", cwd=".")
    state.skip_run = True
    state.required_grade = "A"
    state.last_grade = "C"
    state.seed_path = "seed.yaml"
    state.seed_artifact = {"validated": "by patched receipt boundary"}
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.COMPLETE, "skip run")
    monkeypatch.setattr(
        "ouroboros.cli.commands.auto._codex_recovery_seed_receipt_is_valid",
        lambda _state: True,
    )
    result = AutoPipelineResult(
        status="complete",
        auto_session_id=state.auto_session_id,
        phase="complete",
        grade="C",
        required_grade="A",
        skip_run=True,
        seed_path=state.seed_path,
        artifact_state="complete_verified",
    )

    assert _codex_recovery_state_result_has_terminal_proof(state, result) is False


def test_codex_recovery_validates_seed_file_against_state_receipt(tmp_path) -> None:
    """The durable Seed file must parse and exactly match the state artifact."""
    from pathlib import Path

    from ouroboros.auto.adapters import save_seed
    from ouroboros.cli.commands.auto import _codex_recovery_seed_receipt_is_valid
    from ouroboros.core.seed import (
        EvaluationPrinciple,
        ExitCondition,
        OntologyField,
        OntologySchema,
        Seed,
        SeedMetadata,
    )

    seed = Seed(
        goal="Build a local CLI",
        constraints=("Use existing patterns",),
        acceptance_criteria=("Command prints stable output",),
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior"),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )
    state = AutoPipelineState(goal=seed.goal, cwd=str(tmp_path))
    state.seed_artifact = seed.to_dict()
    state.seed_path = save_seed(seed, seeds_dir=tmp_path)

    assert _codex_recovery_seed_receipt_is_valid(state) is True

    Path(state.seed_path).write_text("not: the same seed\n", encoding="utf-8")
    assert _codex_recovery_seed_receipt_is_valid(state) is False


def test_codex_recovery_partial_seed_exits_nonzero_without_completion_message(
    tmp_path,
) -> None:
    """An unverified handleless Seed cannot be rendered as recovery success."""

    async def fake_run_auto(**_kwargs):  # noqa: ANN202
        return AutoPipelineResult(
            status="complete",
            auto_session_id="auto_partial_seed",
            phase="complete",
            grade="A",
            required_grade="A",
            skip_run=True,
            seed_path="seed.yaml",
            partial_product=True,
            artifact_state="complete_unverified",
            recovery_terminal_proof=False,
        )

    from ouroboros.auto.state import AutoStore

    with (
        patch("ouroboros.cli.commands.auto._run_auto", new=fake_run_auto),
        patch("ouroboros.cli.commands.auto.AutoStore", return_value=AutoStore(tmp_path)),
    ):
        result = runner.invoke(
            app,
            ["auto", "--resume", "auto_partial_seed", "--codex-recovery"],
        )

    assert result.exit_code == 1
    assert "Auto pipeline completed" not in _plain(result.output)


@pytest.mark.parametrize(
    ("job_status", "run_status", "run_success"),
    [
        ("failed", "completed", True),
        ("completed", "failed", True),
        ("completed", "completed", False),
        ("completed", "completed", None),
    ],
)
def test_codex_recovery_rejects_contradictory_execution_envelope(
    job_status: str,
    run_status: str,
    run_success: bool | None,
) -> None:
    """Every terminal lifecycle signal must independently prove success."""
    from ouroboros.cli.commands.auto import _codex_recovery_state_result_has_terminal_proof

    state = AutoPipelineState(goal="contradictory execution", cwd=".")
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.transition(AutoPhase.COMPLETE, "execution complete")
    state.job_id = "job_contradictory"
    state.execution_id = "exec_contradictory"
    state.run_handoff_status = "completed"
    state.run_terminal_job_status = "completed"
    state.run_terminal_status = "completed"
    state.run_terminal_success = True

    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_contradictory",
        phase="complete",
        job_id="job_contradictory",
        execution_id="exec_contradictory",
        run_handoff_status="completed",
        execution_job_status=job_status,
        execution_run_status=run_status,
        execution_run_success=run_success,
        artifact_state="complete_verified",
    )

    assert _codex_recovery_state_result_has_terminal_proof(state, result) is False


def test_codex_recovery_accepts_completed_execution_envelope(tmp_path) -> None:
    """The exact completed/true snapshot projection exits recovery zero."""

    async def fake_run_auto(**_kwargs):  # noqa: ANN202
        return AutoPipelineResult(
            status="complete",
            auto_session_id="auto_verified",
            phase="complete",
            job_id="job_verified",
            execution_id="exec_verified",
            run_handoff_status="completed",
            execution_job_status="completed",
            execution_run_status="completed",
            execution_run_success=True,
            artifact_state="complete_verified",
            recovery_terminal_proof=True,
        )

    from ouroboros.auto.state import AutoStore

    with (
        patch("ouroboros.cli.commands.auto._run_auto", new=fake_run_auto),
        patch("ouroboros.cli.commands.auto.AutoStore", return_value=AutoStore(tmp_path)),
    ):
        result = runner.invoke(
            app,
            ["auto", "--resume", "auto_verified", "--codex-recovery"],
        )

    assert result.exit_code == 0
    assert "Auto pipeline completed" in _plain(result.output)
