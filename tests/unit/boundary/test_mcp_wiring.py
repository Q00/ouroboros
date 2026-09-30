"""The check package on the MCP ``ouroboros_execute_seed`` path (plugin ``ooo run``)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ouroboros.boundary.events import (
    ACCEPTANCE_RECONCILED,
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    BOUNDARY_AGGREGATE_TYPE,
    PACKAGE_FROZEN,
)
from ouroboros.core.types import Result
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler
from ouroboros.orchestrator.session import SessionStatus, SessionTracker
from ouroboros.persistence.event_store import EventStore

from .calc_fixtures import BUGFIX_SCRIPT, FIXED
from .fake_constructors import _constructor_factory
from .test_run_wiring import _parallel_result

SEED_YAML = """\
goal: Fix add
constraints: []
acceptance_criteria:
  - add(2, 3) returns 5
ontology_schema:
  name: calc
  description: calculator
  fields: []
evaluation_principles: []
exit_conditions: []
metadata:
  seed_id: seed-mcp-boundary
  version: "1.0.0"
  created_at: "2024-01-01T00:00:00Z"
  ambiguity_score: 0.1
  interview_id: null
"""


@pytest.fixture
async def memory_event_store():
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    yield store
    await store.close()


def _patch_execute_path(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    switch: str | None,
    legacy_success: bool,
    worker_edit: str | None,
    calls: list[dict[str, Any]],
    terminal_first: bool = False,
) -> tuple[SessionTracker, dict[str, Any]]:
    """Patch the real ``ExecuteSeedHandler`` path around a fake runner.

    With ``terminal_first`` the runner appends ``execution.terminal`` and then
    keeps running, so a job monitor sees the terminal event before the
    handler returns its result.
    """
    seen: dict[str, Any] = {}
    running = SessionTracker.create("exec_mcp_cp", "seed-mcp-boundary", session_id="orch_mcp_cp")
    workspace = SimpleNamespace(
        effective_cwd=str(repo),
        worktree_path=str(repo),
        branch="ooo/test",
        lock_path=str(tmp_path / "lock"),
    )

    class FakeSessionRepository:
        def __init__(self, _event_store: EventStore) -> None:
            pass

        async def reconstruct_session(self, _session_id: str) -> Result:
            status = SessionStatus.COMPLETED if seen.get("success") else SessionStatus.FAILED
            return Result.ok(seen.get("tracker", running).with_status(status))

    class FakeRunner:
        """Honors the runner contract: calls the acceptance authority on the executor result."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.acceptance_authority: Any = None

        def _has_live_process_local_authority(self, *args: object, **kwargs: object) -> bool:
            return False

        async def prepare_session(
            self, *args: object, execution_id: str, session_id: str, **kwargs: object
        ) -> Result:
            seen["tracker"] = SessionTracker.create(
                execution_id, "seed-mcp-boundary", session_id=session_id
            )
            return Result.ok(seen["tracker"])

        async def execute_precreated_session(
            self, *, seed: Any, tracker: Any, **kwargs: Any
        ) -> Result:
            seen["events_at_dispatch"] = [
                event.type
                for event in await store.replay(
                    BOUNDARY_AGGREGATE_TYPE, f"{tracker.execution_id}/check_package/v1"
                )
            ]
            seen["authority_installed"] = self.acceptance_authority is not None
            if worker_edit is not None:
                (repo / "calc.py").write_text(worker_edit)
            parallel_result = _parallel_result(legacy_success)
            if self.acceptance_authority is not None:
                parallel_result = await self.acceptance_authority(
                    seed=seed, execution_id=tracker.execution_id, parallel_result=parallel_result
                )
            seen["success"] = parallel_result.all_succeeded
            if terminal_first:
                await _append_terminal(store, tracker.execution_id, tracker.session_id)
                await asyncio.sleep(2.0)
            return Result.ok(
                SimpleNamespace(
                    success=parallel_result.all_succeeded,
                    execution_id=tracker.execution_id,
                    summary={},
                    final_message="done",
                )
            )

    if switch is None:
        monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    else:
        monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", switch)
    prefix = "ouroboros.mcp.tools.execution_handlers"
    monkeypatch.setattr(f"{prefix}.SessionRepository", FakeSessionRepository)
    monkeypatch.setattr(f"{prefix}.OrchestratorRunner", FakeRunner)
    monkeypatch.setattr(
        f"{prefix}.create_agent_runtime", lambda **_kwargs: SimpleNamespace(runtime_backend="codex")
    )
    monkeypatch.setattr(f"{prefix}.maybe_prepare_task_workspace", lambda *_a, **_k: workspace)
    monkeypatch.setattr(f"{prefix}.release_lock", lambda *_args: None)
    monkeypatch.setattr(
        "ouroboros.boundary.constructor.CheckConstructor",
        _constructor_factory(BUGFIX_SCRIPT, calls),
    )
    monkeypatch.setattr(
        "ouroboros.boundary.run_wiring.default_store_dir",
        lambda execution_id: tmp_path / "store" / execution_id,
    )
    return running, seen


async def _append_terminal(store: EventStore, execution_id: str, session_id: str) -> None:
    from ouroboros.events.base import BaseEvent

    await store.append(
        BaseEvent(
            type="execution.terminal",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={"session_id": session_id, "status": "completed"},
        )
    )


async def _run_handler(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    switch: str | None,
    legacy_success: bool,
    worker_edit: str | None,
    calls: list[dict[str, Any]],
) -> tuple[Any, dict[str, Any]]:
    running, seen = _patch_execute_path(
        store,
        repo,
        tmp_path,
        monkeypatch,
        switch=switch,
        legacy_success=legacy_success,
        worker_edit=worker_edit,
        calls=calls,
    )
    handler = ExecuteSeedHandler(event_store=store, agent_runtime_backend="codex")
    result = await handler.handle(
        {"seed_content": SEED_YAML, "skip_qa": True},
        execution_id=running.execution_id,
        session_id_override=running.session_id,
        synchronous=True,
    )
    return result, seen


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    return root


async def test_mcp_execute_seed_prepares_verifies_and_reconciles(
    memory_event_store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    result, seen = await _run_handler(
        memory_event_store,
        repo,
        tmp_path,
        monkeypatch,
        switch="on",
        legacy_success=False,
        worker_edit=FIXED,
        calls=calls,
    )

    assert result.is_ok
    assert len(calls) == 1 and calls[0]["runtime_backend"] == "codex"
    assert seen["events_at_dispatch"] == [PACKAGE_FROZEN, ADMISSION_COMPLETED, ACTOR_STARTED]
    assert seen["authority_installed"] is True
    events = await memory_event_store.replay(
        BOUNDARY_AGGREGATE_TYPE, "exec_mcp_cp/check_package/v1"
    )
    assert events[-1].type == ACCEPTANCE_RECONCILED
    tool = result.value
    # Script-check package: its pass is advisory (only oracle checks verify),
    # so the criterion is unverified and the legacy rejection decides it
    # The run fails.
    assert tool.is_error is True
    assert tool.meta["status"] == "failed"
    assert "Check package verdict: unverified" in tool.text_content
    assert tool.meta["check_package"] == "on"
    assert tool.meta["check_package_status"] == "admitted"
    assert tool.meta["package_verdict"] == "unverified"
    assert tool.meta["legacy_verdict"] == "reject"
    assert tool.meta["reconciliation"] == "legacy_decided_unverified"
    assert tool.meta["verification_coverage"] == "low"


async def test_mcp_execute_seed_with_the_switch_off_is_the_legacy_run(
    memory_event_store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    result, seen = await _run_handler(
        memory_event_store,
        repo,
        tmp_path,
        monkeypatch,
        switch="off",
        legacy_success=False,
        worker_edit=FIXED,
        calls=calls,
    )

    assert result.is_ok
    assert calls == []
    assert seen["events_at_dispatch"] == []
    assert seen["authority_installed"] is False
    tool = result.value
    assert tool.is_error is True
    assert "Check package" not in tool.text_content
    assert tool.meta["check_package"] == "off"
    assert tool.meta["reconciliation"] == "none"
    assert tool.meta["legacy_verdict"] == "reject"
    assert not (tmp_path / "store").exists()


@pytest.mark.parametrize("tool", ["execute_seed", "start_execute_seed"])
async def test_a_plugin_dispatched_execution_reports_the_check_package_as_not_applied(
    memory_event_store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    # The OpenCode plugin runs the execution in a child session outside this
    # process: the check package does not govern it, and the result says so
    # instead of implying it ran. No constructor call is made.
    from unittest.mock import MagicMock

    from ouroboros.mcp.job_manager import JobManager
    from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler

    calls: list[dict[str, Any]] = []
    monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "on")
    monkeypatch.setattr(
        "ouroboros.boundary.constructor.CheckConstructor",
        _constructor_factory(BUGFIX_SCRIPT, calls),
    )
    arguments = {"seed_content": SEED_YAML, "cwd": str(tmp_path)}
    if tool == "execute_seed":
        handler: Any = ExecuteSeedHandler(
            event_store=memory_event_store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
    else:
        handler = StartExecuteSeedHandler(
            execute_handler=MagicMock(),
            event_store=memory_event_store,
            job_manager=MagicMock(spec=JobManager),
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
    result = await handler.handle(arguments)

    assert result.is_ok
    meta = result.value.meta
    assert meta["dispatch_mode"] == "plugin"
    assert meta["check_package"] == "not_applied"
    assert meta["check_package_reason"] == "plugin_dispatch"
    assert calls == []
    assert not (tmp_path / "store").exists()


@pytest.mark.parametrize(("switch", "expected"), [("on", "pending"), ("off", "not_run")])
async def test_the_public_background_receipt_reports_the_check_package_as_pending(
    memory_event_store: EventStore,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    switch: str,
    expected: str,
) -> None:
    # The registered ouroboros_execute_seed runs in the background: its receipt
    # has no package outcome yet, and says so in closed values instead of
    # leaving the fields out.
    running = SessionTracker.create("exec_mcp_bg", "seed-mcp-boundary", session_id="orch_mcp_bg")
    workspace = SimpleNamespace(
        effective_cwd=str(repo),
        worktree_path=str(repo),
        branch="ooo/test",
        lock_path=str(tmp_path / "lock"),
    )

    class FakeRunner:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.acceptance_authority: Any = None

        def _has_live_process_local_authority(self, *args: object, **kwargs: object) -> bool:
            return False

        async def prepare_session(self, *args: object, **kwargs: object) -> Result:
            return Result.ok(running)

        async def execute_precreated_session(self, **kwargs: object) -> Result:
            await asyncio.Event().wait()  # still running when the receipt is read
            raise AssertionError("unreachable")

    monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", switch)
    prefix = "ouroboros.mcp.tools.execution_handlers"
    monkeypatch.setattr(f"{prefix}.OrchestratorRunner", FakeRunner)
    monkeypatch.setattr(
        f"{prefix}.create_agent_runtime", lambda **_kwargs: SimpleNamespace(runtime_backend="codex")
    )
    monkeypatch.setattr(f"{prefix}.maybe_prepare_task_workspace", lambda *_a, **_k: workspace)
    monkeypatch.setattr(f"{prefix}.release_lock", lambda *_args: None)
    monkeypatch.setattr(
        "ouroboros.boundary.constructor.CheckConstructor",
        _constructor_factory(BUGFIX_SCRIPT, []),
    )
    monkeypatch.setattr(
        "ouroboros.boundary.run_wiring.default_store_dir",
        lambda execution_id: tmp_path / "store" / execution_id,
    )
    before = asyncio.all_tasks()
    handler = ExecuteSeedHandler(event_store=memory_event_store, agent_runtime_backend="codex")
    try:
        result = await handler.handle(
            {"seed_content": SEED_YAML, "skip_qa": True},
            execution_id=running.execution_id,
            session_id_override=running.session_id,
        )
    finally:
        background = asyncio.all_tasks() - before - {asyncio.current_task()}
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)

    assert result.is_ok
    meta = result.value.meta
    assert meta["check_package"] == switch
    assert meta["check_package_status"] == expected
    assert "package_verdict" not in meta and "reconciliation" not in meta


class _TerminalFirstExecuteHandler:
    """An execute handler whose run reaches its terminal event before it returns.

    Post-terminal work (QA, rendering) delays the handler's result past the
    job monitor's first look at the execution's terminal event.
    """

    agent_runtime_backend = None
    llm_backend = None

    def __init__(self, event_store: EventStore, summary: dict[str, str]) -> None:
        self.event_store = event_store
        self.summary = summary

    async def handle(
        self,
        arguments: dict[str, Any],
        *,
        execution_id: str | None = None,
        session_id_override: str | None = None,
        synchronous: bool = False,
        check_package: Any = None,
    ) -> Result:
        from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

        assert synchronous is True
        await _append_terminal(self.event_store, str(execution_id), str(session_id_override))
        await asyncio.sleep(2.0)
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="run finished"),),
                is_error=False,
                meta={"session_id": session_id_override, "status": "completed", **self.summary},
            )
        )


async def test_the_start_tool_reports_the_check_package_in_its_receipt_and_final_result(
    memory_event_store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ouroboros_start_execute_seed with auto_evaluate=false: the job monitor sees
    # execution.terminal before the handler returns its check package summary.
    # The receipt says the package applies and is pending; the job's final
    # result carries the closed summary the handler returned.
    from ouroboros.mcp.job_manager import JobManager
    from ouroboros.mcp.tools import execution_handlers
    from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler

    monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "on")
    monkeypatch.setattr(execution_handlers, "get_auto_evaluate_enabled", lambda: False)
    summary = {
        "check_package": "on",
        "check_package_status": "admitted",
        "package_verdict": "pass",
        "legacy_verdict": "accept",
        "reconciliation": "agree",
        "verification_coverage": "full",
    }
    manager = JobManager(memory_event_store)
    handler = StartExecuteSeedHandler(
        execute_handler=_TerminalFirstExecuteHandler(memory_event_store, summary),  # type: ignore[arg-type]
        event_store=memory_event_store,
        job_manager=manager,
    )
    started = await handler.handle(
        {"seed_content": SEED_YAML, "cwd": str(tmp_path), "auto_evaluate": False}
    )
    assert started.is_ok
    receipt = started.value.meta
    assert receipt["check_package"] == "on"
    assert receipt["check_package_status"] == "pending"

    job_id = receipt["job_id"]
    for _ in range(300):
        snapshot = await manager.get_snapshot(job_id)
        if snapshot.is_terminal:
            break
        await asyncio.sleep(0.05)
    assert snapshot.is_terminal
    for key, value in summary.items():
        assert snapshot.result_meta.get(key) == value, key


async def test_the_start_tool_receipt_says_the_check_package_is_off(
    memory_event_store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.mcp.job_manager import JobManager
    from ouroboros.mcp.tools import execution_handlers
    from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler

    monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "off")
    monkeypatch.setattr(execution_handlers, "get_auto_evaluate_enabled", lambda: False)
    manager = JobManager(memory_event_store)
    handler = StartExecuteSeedHandler(
        execute_handler=_TerminalFirstExecuteHandler(memory_event_store, {}),  # type: ignore[arg-type]
        event_store=memory_event_store,
        job_manager=manager,
    )
    started = await handler.handle(
        {"seed_content": SEED_YAML, "cwd": str(tmp_path), "auto_evaluate": False}
    )
    assert started.is_ok
    assert started.value.meta["check_package"] == "off"
    assert started.value.meta["check_package_status"] == "not_run"
    job = manager._runner_tasks.get(started.value.meta["job_id"])
    if job is not None:
        job.cancel()


async def _wait_terminal(manager: Any, job_id: str) -> Any:
    for _ in range(300):
        snapshot = await manager.get_snapshot(job_id)
        if snapshot.is_terminal:
            return snapshot
        await asyncio.sleep(0.05)
    raise AssertionError("the job never finished")


async def test_the_start_tool_off_mode_final_result_carries_the_closed_summary(
    memory_event_store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off switch, ``auto_evaluate=false``, and the terminal event before the result.

    Start and the Execute run it wraps share one run control: it is resolved
    once, and the job keeps the result that carries its closed summary. Before
    the fix each handler resolved its own, and the job completed from
    ``execution.terminal`` with none of the summary fields.
    """
    from ouroboros.boundary.run_control import CheckPackageRun
    from ouroboros.mcp.job_manager import JobManager
    from ouroboros.mcp.tools import execution_handlers
    from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler

    calls: list[dict[str, Any]] = []
    _patch_execute_path(
        memory_event_store,
        repo,
        tmp_path,
        monkeypatch,
        switch="off",
        legacy_success=True,
        worker_edit=None,
        calls=calls,
        terminal_first=True,
    )
    monkeypatch.setattr(execution_handlers, "get_auto_evaluate_enabled", lambda: False)
    resolved: list[CheckPackageRun] = []
    real_resolve = CheckPackageRun.resolve

    def _resolve(cli_value: bool | None = None) -> CheckPackageRun:
        resolved.append(real_resolve(cli_value))
        return resolved[-1]

    monkeypatch.setattr(CheckPackageRun, "resolve", _resolve)
    manager = JobManager(memory_event_store)
    handler = StartExecuteSeedHandler(
        execute_handler=ExecuteSeedHandler(
            event_store=memory_event_store, agent_runtime_backend="codex"
        ),
        event_store=memory_event_store,
        job_manager=manager,
    )
    started = await handler.handle(
        {"seed_content": SEED_YAML, "cwd": str(repo), "auto_evaluate": False, "skip_qa": True}
    )
    assert started.is_ok
    receipt = started.value.meta
    assert (receipt["check_package"], receipt["check_package_status"]) == ("off", "not_run")

    snapshot = await _wait_terminal(manager, receipt["job_id"])
    assert {
        key: snapshot.result_meta.get(key)
        for key in (
            "check_package",
            "check_package_status",
            "package_verdict",
            "legacy_verdict",
            "reconciliation",
        )
    } == {
        "check_package": "off",
        "check_package_status": "not_run",
        "package_verdict": "none",
        "legacy_verdict": "accept",
        "reconciliation": "none",
    }
    assert "verification_coverage" not in snapshot.result_meta
    assert len(resolved) == 1
    assert calls == []


@pytest.mark.parametrize(
    ("recorded", "mode", "status"), [(True, "on", "pending"), (False, "off", "not_run")]
)
async def test_the_start_tool_receipt_of_a_resume_reports_the_run_s_recorded_mode(
    memory_event_store: EventStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded: bool,
    mode: str,
    status: str,
) -> None:
    """A resume's receipt carries ``check_package`` from the run's journal.

    The live switch says the opposite. Before the fix the resumed receipt left
    ``check_package`` out.
    """
    from ouroboros.boundary.events import RunContract
    from ouroboros.boundary.ledger import BoundaryLedger
    from ouroboros.mcp.job_manager import JobManager
    from ouroboros.mcp.tools import execution_handlers
    from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler

    paused = SessionTracker.create(
        "exec_resumed", "seed-mcp-boundary", session_id="orch_resumed"
    ).with_status(SessionStatus.PAUSED)

    class FakeSessionRepository:
        def __init__(self, _event_store: EventStore) -> None:
            pass

        async def reconstruct_session(self, _session_id: str) -> Result:
            return Result.ok(paused)

    monkeypatch.setattr(execution_handlers, "SessionRepository", FakeSessionRepository)
    monkeypatch.setattr(execution_handlers, "get_auto_evaluate_enabled", lambda: False)
    monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "off" if recorded else "on")
    if recorded:
        await BoundaryLedger(memory_event_store).record_check_package_enabled(
            "exec_resumed", RunContract(check_timeout_seconds=30)
        )
    manager = JobManager(memory_event_store)
    handler = StartExecuteSeedHandler(
        execute_handler=_TerminalFirstExecuteHandler(memory_event_store, {}),  # type: ignore[arg-type]
        event_store=memory_event_store,
        job_manager=manager,
    )
    started = await handler.handle(
        {
            "seed_content": SEED_YAML,
            "cwd": str(tmp_path),
            "session_id": "orch_resumed",
            "auto_evaluate": False,
        }
    )
    assert started.is_ok
    receipt = started.value.meta
    await _wait_terminal(manager, receipt["job_id"])
    assert (receipt.get("check_package"), receipt["check_package_status"]) == (mode, status)
