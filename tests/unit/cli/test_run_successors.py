"""Terminal ``ouroboros run`` continues into formal evaluation and Ralph like MCP ``ooo run``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner
import yaml

from ouroboros.auto.runtime_routing import AutoStageRuntimePlan, StageRuntime
from ouroboros.cli.commands import run_successors
from ouroboros.cli.commands.run import _run_orchestrator
from ouroboros.cli.formatters import console
from ouroboros.cli.main import app
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.session import SessionStatus, SessionTracker

VALID_SEED_DATA = {
    "goal": "Test task",
    "constraints": [],
    "acceptance_criteria": ["All tests pass"],
    "ontology_schema": {"name": "T", "description": "T", "fields": []},
    "evaluation_principles": [],
    "exit_conditions": [],
    "metadata": {
        "seed_id": "test-seed-run-successors",
        "version": "1.0.0",
        "created_at": "2024-01-01T00:00:00Z",
        "ambiguity_score": 0.1,
        "interview_id": None,
    },
}
SEED_YAML = yaml.safe_dump(VALID_SEED_DATA)


@pytest.fixture(autouse=True)
def _no_run_evaluation_chain() -> None:
    """Override the CLI conftest no-op: these tests exercise the real chain."""


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep panel text on one line so printed commands can be asserted."""
    monkeypatch.setattr(console, "width", 400)


def _tool_result(text: str, **meta: Any) -> MCPToolResult:
    return MCPToolResult(content=(MCPContentItem(type=ContentType.TEXT, text=text),), meta=meta)


def _execution(*, success: bool, session_id: str = "orch_1") -> SimpleNamespace:
    return SimpleNamespace(
        success=success,
        session_id=session_id,
        execution_id="exec_1",
        summary={},
        final_message="All criteria implemented.",
        messages_processed=1,
        duration_seconds=1.0,
    )


def _session_repo(status: SessionStatus | None = None) -> MagicMock:
    repo = MagicMock()
    if status is None:
        repo.reconstruct_session = AsyncMock(return_value=Result.err(RuntimeError("missing")))
    else:
        tracker = SimpleNamespace(status=status)
        repo.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
    return repo


class _Chain:
    """Fake composition-root handler plus job handlers, recording call order."""

    def __init__(self, *, ralph: bool = True) -> None:
        self.calls: list[str] = []
        self.evaluate_arguments: dict[str, Any] | None = None
        self.job_manager = MagicMock()
        self.job_manager.cancel_job = AsyncMock()
        self.job_manager.has_live_job_task = MagicMock(return_value=False)
        self.ralph = ralph
        self.block_wait = False
        self.wait_started = asyncio.Event()
        chain = self

        class Handler:
            _job_manager = chain.job_manager
            _event_store = MagicMock()

            async def handle(self, arguments: dict[str, Any]) -> Any:
                chain.evaluate_arguments = arguments
                chain.calls.append("enqueue")
                return Result.ok(_tool_result("started", job_id="job_eval"))

        class Wait:
            def __init__(self, **_: Any) -> None:
                pass

            async def handle(self, arguments: dict[str, Any]) -> Any:
                chain.calls.append(f"wait:{arguments['job_id']}")
                chain.wait_started.set()
                if chain.block_wait:
                    await asyncio.Event().wait()
                return Result.ok(
                    _tool_result(
                        f"{arguments['job_id']} progress", cursor=1, changed=True, is_terminal=True
                    )
                )

        class Receipt:
            def __init__(self, **_: Any) -> None:
                pass

            async def handle(self, arguments: dict[str, Any]) -> Any:
                job_id = arguments["job_id"]
                chain.calls.append(f"result:{job_id}")
                if job_id == "job_eval":
                    meta = {"chained_ralph_job_id": "job_ralph"} if chain.ralph else {}
                    return Result.ok(_tool_result("EVALUATION RECEIPT rejected", **meta))
                return Result.ok(_tool_result("RALPH RECEIPT done"))

        self.handler = Handler()
        self.wait_cls = Wait
        self.receipt_cls = Receipt

    def patches(self, *, auto_evaluate: bool = True, auto_evolve: bool = True) -> Any:
        return _Patches(self, auto_evaluate, auto_evolve)


class _Patches:
    def __init__(self, chain: _Chain, auto_evaluate: bool, auto_evolve: bool) -> None:
        self._stack = [
            patch.object(run_successors, "_build_successor_handler", return_value=chain.handler),
            patch("ouroboros.mcp.tools.job_handlers.JobWaitHandler", chain.wait_cls),
            patch("ouroboros.mcp.tools.job_handlers.JobResultHandler", chain.receipt_cls),
            patch("ouroboros.config.loader.get_auto_evaluate_enabled", return_value=auto_evaluate),
            patch("ouroboros.config.loader.get_auto_evolve_enabled", return_value=auto_evolve),
        ]

    def __enter__(self) -> None:
        for item in self._stack:
            item.__enter__()

    def __exit__(self, *exc: object) -> None:
        for item in reversed(self._stack):
            item.__exit__(*exc)


async def _continue(
    execution: Any,
    tmp_path: Path,
    *,
    worktree: str | None = None,
    working_dir: Path | None = None,
    auto_evaluate: bool | None = None,
    auto_evolve: bool | None = None,
    session_repo: Any = None,
) -> None:
    await run_successors.continue_run_into_evaluation(
        execution,
        session_repo=session_repo if session_repo is not None else _session_repo(),
        seed_content=SEED_YAML,
        worktree_path=worktree,
        working_dir=working_dir if working_dir is not None else tmp_path,
        runtime_override=None,
        auto_evaluate=auto_evaluate,
        auto_evolve=auto_evolve,
    )


@pytest.mark.asyncio
async def test_auto_evaluate_off_prints_and_enqueues_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chain = _Chain()
    with chain.patches(auto_evaluate=False):
        await _continue(_execution(success=True), tmp_path)

    assert chain.calls == []
    assert "Formal evaluation is off" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_completed_run_evaluates_worktree_then_follows_evaluate_and_ralph(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chain = _Chain()
    worktree = str(tmp_path / "wt")
    executed_in = tmp_path / "wt" / "pkg"
    with chain.patches():
        await _continue(
            _execution(success=True), tmp_path, worktree=worktree, working_dir=executed_in
        )

    assert chain.calls == [
        "enqueue",
        "wait:job_eval",
        "result:job_eval",
        "wait:job_ralph",
        "result:job_ralph",
    ]
    arguments = chain.evaluate_arguments
    assert arguments is not None
    # Evaluation judges where the run executed, not the worktree root.
    assert arguments["working_dir"] == str(executed_in)
    assert arguments["session_id"] == "orch_1"
    assert arguments["auto_evolve"] is True
    assert arguments["_source_execution_status"] == "completed"
    assert arguments["acceptance_criteria"] == ["All tests pass"]
    assert "All criteria implemented." in arguments["artifact"]
    out = capsys.readouterr().out
    assert "job_eval" in out
    assert out.index("EVALUATION RECEIPT") < out.index("RALPH RECEIPT")
    chain.job_manager.cancel_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_approved_evaluation_does_not_follow_ralph(tmp_path: Path) -> None:
    chain = _Chain(ralph=False)
    with chain.patches():
        await _continue(_execution(success=True), tmp_path)

    assert chain.calls == ["enqueue", "wait:job_eval", "result:job_eval"]
    assert chain.evaluate_arguments is not None
    assert chain.evaluate_arguments["working_dir"] == str(tmp_path)


@pytest.mark.asyncio
async def test_failed_run_is_still_evaluated(tmp_path: Path) -> None:
    chain = _Chain()
    with chain.patches():
        await _continue(_execution(success=False), tmp_path)

    assert chain.evaluate_arguments is not None
    assert chain.evaluate_arguments["_source_execution_status"] == "failed"
    assert chain.calls[0] == "enqueue"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [SessionStatus.PAUSED, SessionStatus.CANCELLED])
async def test_paused_or_cancelled_run_is_not_evaluated(
    tmp_path: Path, status: SessionStatus
) -> None:
    chain = _Chain()
    with chain.patches():
        await _continue(_execution(success=False), tmp_path, session_repo=_session_repo(status))

    assert chain.calls == []


@pytest.mark.asyncio
async def test_cli_overrides_win_over_config(tmp_path: Path) -> None:
    chain = _Chain()
    with chain.patches(auto_evaluate=False, auto_evolve=True):
        await _continue(_execution(success=True), tmp_path, auto_evaluate=True, auto_evolve=False)
    assert chain.evaluate_arguments is not None
    assert chain.evaluate_arguments["auto_evolve"] is False

    chain = _Chain()
    with chain.patches(auto_evaluate=True):
        await _continue(_execution(success=True), tmp_path, auto_evaluate=False)
    assert chain.calls == []


@pytest.mark.asyncio
async def test_enqueue_failure_is_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chain = _Chain()
    chain.handler.handle = AsyncMock(  # type: ignore[method-assign]
        return_value=Result.err(MCPToolError("no evaluator", tool_name="t"))
    )
    with chain.patches():
        await _continue(_execution(success=True), tmp_path)

    out = capsys.readouterr().out
    assert "enqueue failed" in out
    assert "ooo evaluate orch_1" in out
    assert chain.calls == []


@pytest.mark.asyncio
async def test_handler_build_failure_is_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch.object(
            run_successors, "_build_successor_handler", side_effect=RuntimeError("no server")
        ),
        patch("ouroboros.config.loader.get_auto_evaluate_enabled", return_value=True),
        patch("ouroboros.config.loader.get_auto_evolve_enabled", return_value=True),
    ):
        await _continue(_execution(success=True), tmp_path)

    assert "could not start" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_ctrl_c_during_wait_stops_waiting_without_cancelling_jobs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chain = _Chain()
    chain.block_wait = True
    with chain.patches():
        task = asyncio.create_task(_continue(_execution(success=True), tmp_path))
        await asyncio.wait_for(chain.wait_started.wait(), timeout=5)
        task.cancel()  # what asyncio.run does on SIGINT
        await task  # the run's own exit path continues; no CancelledError

    assert not task.cancelled()
    chain.job_manager.cancel_job.assert_not_awaited()
    out = capsys.readouterr().out
    assert "continues in the background" in out
    assert "ouroboros job wait job_eval" in out


@pytest.mark.asyncio
async def test_build_successor_handler_resolves_evaluate_stage_and_demotes_plugin() -> None:
    plugin = StageRuntime(runtime_backend="opencode", opencode_mode="plugin")
    other = StageRuntime(runtime_backend="claude", opencode_mode=None)
    plan = AutoStageRuntimePlan(
        default=other, interview=other, execute=other, evaluate=plugin, reflect=other
    )
    with (
        patch(
            "ouroboros.auto.runtime_routing.resolve_auto_stage_runtime_plan", return_value=plan
        ) as resolve,
        patch("ouroboros.mcp.tools.run_successors.build_run_successor_handler") as build,
    ):
        run_successors._build_successor_handler("codex")

    assert resolve.call_args.kwargs["runtime_override"] == "codex"
    build.assert_called_once_with(agent_runtime_backend="opencode", opencode_mode="subprocess")


def _orchestrator_patches(
    mock_runner: MagicMock, tracker: SessionTracker, workspace: Any = None
) -> list[Any]:
    return [
        patch("ouroboros.cli.commands.run._load_seed_from_yaml", return_value=VALID_SEED_DATA),
        patch("ouroboros.orchestrator.create_agent_runtime"),
        patch("ouroboros.orchestrator.OrchestratorRunner", return_value=mock_runner),
        patch("ouroboros.persistence.event_store.EventStore"),
        patch("ouroboros.orchestrator.session.SessionRepository"),
        patch("ouroboros.cli.commands.run.maybe_restore_task_workspace", return_value=workspace),
        patch("ouroboros.cli.commands.run.maybe_prepare_task_workspace", return_value=workspace),
    ]


async def _resume_run(
    tmp_path: Path, execution: SimpleNamespace, *, workspace: Any = None, **kwargs: Any
) -> tuple[MagicMock, type[BaseException] | None]:
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text("goal: ignored\n", encoding="utf-8")
    tracker = SessionTracker.create("exec_1", "test-seed-run-successors", session_id="orch_1")
    mock_runner = MagicMock()
    mock_runner.resume_session = AsyncMock(return_value=Result.ok(execution))
    started = [p.start() for p in _orchestrator_patches(mock_runner, tracker, workspace)]
    event_store_cls, repo_cls = started[3], started[4]
    event_store_cls.return_value.initialize = AsyncMock()
    event_store_cls.return_value.replay = AsyncMock(return_value=[])
    event_store_cls.return_value.query_events = AsyncMock(return_value=[])
    repo_cls.return_value.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
    raised: type[BaseException] | None = None
    try:
        await _run_orchestrator(seed_file, resume_session="orch_1", no_qa=True, **kwargs)
    except typer.Exit as exc:
        assert exc.exit_code == 1
        raised = typer.Exit
    finally:
        patch.stopall()
    return mock_runner, raised


@pytest.mark.asyncio
async def test_failed_cli_run_is_evaluated_and_still_exits_1(tmp_path: Path) -> None:
    successor = AsyncMock()
    with patch.object(run_successors, "continue_run_into_evaluation", successor):
        _, raised = await _resume_run(
            tmp_path, _execution(success=False), auto_evaluate=True, auto_evolve=False
        )

    assert raised is typer.Exit
    successor.assert_awaited_once()
    assert successor.await_args.args[0].success is False
    assert successor.await_args.kwargs["auto_evaluate"] is True
    assert successor.await_args.kwargs["auto_evolve"] is False


@pytest.mark.asyncio
async def test_enqueue_failure_keeps_successful_cli_exit_code(tmp_path: Path) -> None:
    with (
        patch.object(
            run_successors, "_build_successor_handler", side_effect=RuntimeError("no server")
        ),
        patch("ouroboros.config.loader.get_auto_evaluate_enabled", return_value=True),
        patch("ouroboros.config.loader.get_auto_evolve_enabled", return_value=True),
    ):
        mock_runner, raised = await _resume_run(tmp_path, _execution(success=True))

    assert raised is None
    mock_runner.resume_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_cli_run_with_enqueue_failure_still_exits_1(tmp_path: Path) -> None:
    with (
        patch.object(
            run_successors, "_build_successor_handler", side_effect=RuntimeError("no server")
        ),
        patch("ouroboros.config.loader.get_auto_evaluate_enabled", return_value=True),
        patch("ouroboros.config.loader.get_auto_evolve_enabled", return_value=True),
    ):
        _, raised = await _resume_run(tmp_path, _execution(success=False))

    assert raised is typer.Exit


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([], (None, None)),
        (["--no-auto-evaluate"], (False, None)),
        (["--auto-evaluate", "--no-auto-evolve"], (True, False)),
    ],
)
def test_run_flags_reach_the_orchestrator(
    tmp_path: Path, flags: list[str], expected: tuple[bool | None, bool | None]
) -> None:
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text("goal: test\nacceptance_criteria:\n  - criterion: test\n")
    run_orchestrator = AsyncMock()
    with patch("ouroboros.cli.commands.run._run_orchestrator", new=run_orchestrator):
        result = CliRunner().invoke(app, ["run", str(seed_file), *flags])

    assert result.exit_code == 0, result.output
    kwargs = run_orchestrator.await_args.kwargs
    assert (kwargs["auto_evaluate"], kwargs["auto_evolve"]) == expected


@pytest.mark.asyncio
async def test_new_attempt_keeps_the_evaluation_choices(tmp_path: Path) -> None:
    from ouroboros.orchestrator.runner import OrchestratorError

    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text("goal: ignored\n", encoding="utf-8")
    tracker = SessionTracker.create("exec_1", "test-seed-run-successors", session_id="orch_1")
    lost = OrchestratorError(
        message="start a new attempt",
        details={"resume_blocked": "process_local_resume_unavailable"},
    )
    mock_runner = MagicMock()
    mock_runner.resume_session = AsyncMock(return_value=Result.err(lost))
    mock_runner.execute_seed = AsyncMock(
        return_value=Result.ok(_execution(success=True, session_id="orch_2"))
    )
    successor = AsyncMock()
    started = [p.start() for p in _orchestrator_patches(mock_runner, tracker)]
    event_store_cls, repo_cls = started[3], started[4]
    event_store_cls.return_value.initialize = AsyncMock()
    event_store_cls.return_value.replay = AsyncMock(return_value=[])
    event_store_cls.return_value.query_events = AsyncMock(return_value=[])
    repo_cls.return_value.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))
    try:
        with patch.object(run_successors, "continue_run_into_evaluation", successor):
            await _run_orchestrator(
                seed_file,
                resume_session="orch_1",
                no_qa=True,
                auto_evaluate=False,
                auto_evolve=True,
            )
    finally:
        patch.stopall()

    mock_runner.execute_seed.assert_awaited_once()
    successor.assert_awaited_once()
    assert successor.await_args.kwargs["auto_evaluate"] is False
    assert successor.await_args.kwargs["auto_evolve"] is True


@pytest.mark.asyncio
async def test_a_task_worktree_run_is_evaluated_where_it_executed(tmp_path: Path) -> None:
    """A project in a repository subdirectory runs in that subdirectory of the worktree."""
    root = tmp_path / "worktrees" / "repo" / "orch_1"
    workspace = SimpleNamespace(
        worktree_path=str(root),
        effective_cwd=str(root / "pkg"),
        branch="ooo/orch_1",
        lock_path=str(tmp_path / "lock"),
    )
    successor = AsyncMock()
    with patch.object(run_successors, "continue_run_into_evaluation", successor):
        await _resume_run(tmp_path, _execution(success=True), workspace=workspace)

    successor.assert_awaited_once()
    assert successor.await_args.kwargs["working_dir"] == root / "pkg"
    assert successor.await_args.kwargs["worktree_path"] == str(root)
