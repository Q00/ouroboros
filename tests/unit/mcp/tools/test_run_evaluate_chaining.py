"""Run-to-evaluate chaining for PR-D.

Successful background ``ooo run`` results should enqueue the formal evaluator
as a separate bounded job.  Disabling the flag must leave the legacy run result
byte-for-byte at the metadata boundary.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.job_manager import JobManager, JobSnapshot, JobStatus
from ouroboros.mcp.tools import evaluation_handlers, execution_handlers
from ouroboros.mcp.tools.evaluation_handlers import StartEvaluateHandler
from ouroboros.mcp.tools.execution_handlers import (
    StartExecuteSeedHandler,
    _run_only_verification_meta,
    _run_only_verification_text,
)
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def event_store():
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    yield store
    await store.close()


async def _wait_terminal(job_manager: JobManager, job_id: str) -> JobSnapshot:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        snapshot = await job_manager.get_snapshot(job_id)
        if snapshot.is_terminal:
            return snapshot
        task = job_manager._tasks.get(job_id)
        if task is not None and not task.done():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=min(0.05, remaining))
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


async def _wait_for_call(calls: list[Any]) -> None:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if calls:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("expected chained evaluate handler to be called")


class _SuccessfulExecuteHandler:
    agent_runtime_backend = None
    llm_backend = None

    def __init__(self, *, text: str = "run finished", worktree_path: str | None = None) -> None:
        self.text = text
        self.worktree_path = worktree_path
        self.returned_meta: dict[str, Any] | None = None

    async def handle(
        self,
        arguments: dict[str, Any],
        *,
        execution_id: str | None = None,
        session_id_override: str | None = None,
        synchronous: bool = False,
    ) -> Result[MCPToolResult, Any]:
        assert synchronous is True
        session_id = session_id_override or arguments.get("session_id") or "orch_fake"
        meta = {
            "seed_id": "seed-test",
            "session_id": session_id,
            "execution_id": execution_id,
            "launched": True,
            "status": "completed",
            "success": True,
            **_run_only_verification_meta(session_id),
        }
        if self.worktree_path is not None:
            meta["worktree_path"] = self.worktree_path
        self.returned_meta = dict(meta)
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=self.text + "\n" + _run_only_verification_text(session_id),
                    ),
                ),
                is_error=False,
                meta=meta,
            )
        )


async def test_successful_run_enqueues_chained_evaluate_job(
    event_store,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_handlers, "get_auto_evaluate_enabled", lambda: True)
    evaluate_calls: list[dict[str, Any]] = []

    class FakeEvaluateHandler:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, Any]:
            evaluate_calls.append({"arguments": arguments, "kwargs": self.kwargs})
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="approved"),),
                    is_error=False,
                    meta={
                        "final_approved": True,
                        "session_id": arguments["session_id"],
                    },
                )
            )

    monkeypatch.setattr(evaluation_handlers, "EvaluateHandler", FakeEvaluateHandler)

    job_manager = JobManager(event_store)
    handler = StartExecuteSeedHandler(
        execute_handler=_SuccessfulExecuteHandler(text="execution artifact"),  # type: ignore[arg-type]
        event_store=event_store,
        job_manager=job_manager,
    )

    seed_content = (
        "goal: Build a CLI task manager\n"
        "acceptance_criteria:\n"
        "  - Tasks can be created\n"
        "  - Tasks can be listed\n"
        "ontology_schema:\n"
        "  name: TaskManager\n"
        "  description: Task management domain\n"
        "metadata:\n"
        "  ambiguity_score: 0.15\n"
    )
    started = await handler.handle({"seed_content": seed_content, "cwd": str(tmp_path)})
    assert started.is_ok

    snapshot = await _wait_terminal(job_manager, started.value.meta["job_id"])

    assert snapshot.status == JobStatus.COMPLETED
    assert snapshot.result_meta["success"] is True
    evaluation_job_id = snapshot.result_meta["chained_evaluate_job_id"]
    assert isinstance(evaluation_job_id, str)
    assert evaluation_job_id.startswith("job_")
    assert snapshot.result_meta["verification_status"] == "evaluation_enqueued"
    assert snapshot.result_meta["evaluation_status"] == "enqueued"
    assert snapshot.result_meta["evaluated"] is False
    assert "Manual Retry: ooo evaluate" in (snapshot.result_text or "")
    await _wait_for_call(evaluate_calls)
    evaluate_snapshot = await job_manager.get_snapshot(evaluation_job_id)
    assert evaluate_snapshot.job_type == "evaluate"
    assert evaluate_calls
    assert evaluate_calls[0]["arguments"]["session_id"] == snapshot.result_meta["session_id"]
    assert evaluate_calls[0]["arguments"]["seed_content"] == seed_content
    assert evaluate_calls[0]["arguments"]["acceptance_criteria"] == [
        "Tasks can be created",
        "Tasks can be listed",
    ]
    assert evaluate_calls[0]["arguments"]["working_dir"] == str(tmp_path)
    assert "execution artifact" in evaluate_calls[0]["arguments"]["artifact"]


async def test_chained_evaluate_uses_execution_worktree_when_present(
    event_store,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_handlers, "get_auto_evaluate_enabled", lambda: True)
    evaluate_calls: list[dict[str, Any]] = []

    class FakeEvaluateHandler:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, Any]:
            evaluate_calls.append(arguments)
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="approved"),),
                    is_error=False,
                    meta={"final_approved": True, "session_id": arguments["session_id"]},
                )
            )

    monkeypatch.setattr(evaluation_handlers, "EvaluateHandler", FakeEvaluateHandler)
    execution_worktree = tmp_path / "task-worktree"
    execution_worktree.mkdir()
    original_cwd = tmp_path / "original"
    original_cwd.mkdir()

    job_manager = JobManager(event_store)
    handler = StartExecuteSeedHandler(
        execute_handler=_SuccessfulExecuteHandler(
            text="execution artifact",
            worktree_path=str(execution_worktree),
        ),  # type: ignore[arg-type]
        event_store=event_store,
        job_manager=job_manager,
    )

    started = await handler.handle({"seed_content": "goal: chain\n", "cwd": str(original_cwd)})
    assert started.is_ok

    await _wait_for_call(evaluate_calls)
    snapshot = await job_manager.get_snapshot(started.value.meta["job_id"])
    assert snapshot.job_type == "execute_seed"

    assert evaluate_calls
    assert evaluate_calls[0]["working_dir"] == str(execution_worktree)


async def test_auto_evaluate_override_false_preserves_legacy_run_meta_exactly(
    event_store,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_handlers, "get_auto_evaluate_enabled", lambda: True)

    class UnexpectedStartEvaluateHandler:
        def __init__(self, **_: Any) -> None:
            pass

        async def handle(self, _: dict[str, Any]) -> Result[MCPToolResult, Any]:
            raise AssertionError("auto_evaluate=false must not enqueue evaluation")

    monkeypatch.setattr(
        evaluation_handlers,
        "StartEvaluateHandler",
        UnexpectedStartEvaluateHandler,
    )

    execute_handler = _SuccessfulExecuteHandler(text="legacy")
    job_manager = JobManager(event_store)
    handler = StartExecuteSeedHandler(
        execute_handler=execute_handler,  # type: ignore[arg-type]
        event_store=event_store,
        job_manager=job_manager,
    )

    started = await handler.handle(
        {
            "seed_content": "goal: legacy\n",
            "cwd": str(tmp_path),
            "auto_evaluate": False,
        }
    )
    assert started.is_ok

    snapshot = await _wait_terminal(job_manager, started.value.meta["job_id"])

    assert snapshot.status == JobStatus.COMPLETED
    assert execute_handler.returned_meta is not None
    assert snapshot.result_meta == execute_handler.returned_meta
    assert "chained_evaluate_job_id" not in snapshot.result_meta
    assert snapshot.result_meta["verification_status"] == "executed_unverified"


async def test_auto_evaluate_config_false_preserves_legacy_run_meta_exactly(
    event_store,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_handlers, "get_auto_evaluate_enabled", lambda: False)

    class UnexpectedStartEvaluateHandler:
        def __init__(self, **_: Any) -> None:
            pass

        async def handle(self, _: dict[str, Any]) -> Result[MCPToolResult, Any]:
            raise AssertionError("execution.auto_evaluate=false must not enqueue evaluation")

    monkeypatch.setattr(
        evaluation_handlers,
        "StartEvaluateHandler",
        UnexpectedStartEvaluateHandler,
    )

    execute_handler = _SuccessfulExecuteHandler(text="legacy config")
    job_manager = JobManager(event_store)
    handler = StartExecuteSeedHandler(
        execute_handler=execute_handler,  # type: ignore[arg-type]
        event_store=event_store,
        job_manager=job_manager,
    )

    started = await handler.handle({"seed_content": "goal: config-off\n", "cwd": str(tmp_path)})
    assert started.is_ok

    snapshot = await _wait_terminal(job_manager, started.value.meta["job_id"])

    assert snapshot.status == JobStatus.COMPLETED
    assert execute_handler.returned_meta is not None
    assert snapshot.result_meta == execute_handler.returned_meta
    assert "chained_evaluate_job_id" not in snapshot.result_meta
    assert snapshot.result_meta["verification_status"] == "executed_unverified"


async def test_evaluate_enqueue_failure_keeps_run_completed(
    event_store,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution_handlers, "get_auto_evaluate_enabled", lambda: True)

    class FailingStartEvaluateHandler:
        def __init__(self, **_: Any) -> None:
            pass

        async def handle(self, _: dict[str, Any]) -> Result[MCPToolResult, Any]:
            return Result.err(MCPToolError("enqueue boom", tool_name="ouroboros_start_evaluate"))

    monkeypatch.setattr(evaluation_handlers, "StartEvaluateHandler", FailingStartEvaluateHandler)

    job_manager = JobManager(event_store)
    handler = StartExecuteSeedHandler(
        execute_handler=_SuccessfulExecuteHandler(),  # type: ignore[arg-type]
        event_store=event_store,
        job_manager=job_manager,
    )

    started = await handler.handle({"seed_content": "goal: failure\n", "cwd": str(tmp_path)})
    assert started.is_ok

    snapshot = await _wait_terminal(job_manager, started.value.meta["job_id"])

    assert snapshot.status == JobStatus.COMPLETED
    assert snapshot.result_meta["success"] is True
    assert snapshot.result_meta["evaluation_status"] == "enqueue_failed"
    assert snapshot.result_meta["evaluation_error"] == "enqueue boom"
    assert snapshot.result_meta["next_step"].startswith("ooo evaluate orch_")
    assert "chained_evaluate_job_id" not in snapshot.result_meta
    assert "run result remains successful" in (snapshot.result_text or "")


async def test_start_evaluate_timeout_writes_terminal_event(
    event_store,
) -> None:
    class SlowEvaluateHandler:
        async def handle(self, _: dict[str, Any]) -> Result[MCPToolResult, Any]:
            await asyncio.sleep(1)
            return Result.ok(MCPToolResult())

    job_manager = JobManager(event_store)
    handler = StartEvaluateHandler(
        evaluate_handler=SlowEvaluateHandler(),  # type: ignore[arg-type]
        event_store=event_store,
        job_manager=job_manager,
        deadline_seconds=0.01,
    )

    started = await handler.handle({"session_id": "orch_timeout", "artifact": "code"})
    assert started.is_ok

    snapshot = await _wait_terminal(job_manager, started.value.meta["job_id"])

    assert snapshot.status == JobStatus.FAILED
    assert snapshot.result_meta["session_id"] == "orch_timeout"
    assert snapshot.result_meta["evaluation_status"] == "timed_out"
    assert snapshot.result_meta["status"] == "timed_out"
    assert "Evaluation timed out" in (snapshot.result_text or "")

    events, _ = await event_store.get_events_after("job", started.value.meta["job_id"])
    terminal = [event for event in events if event.type == "mcp.job.failed"]
    assert terminal
    assert terminal[-1].data["result_meta"]["evaluation_status"] == "timed_out"
