"""Three-surface AgentProcess acceptance test — issue #518.

This is an acceptance boundary over the *production start surfaces* that own
long-running work:

* ``StartEvolveStepHandler`` for evolve_step
* ``RalphHandler`` for ralph
* ``StartExecuteSeedHandler`` for execute_seed

The inner work is kept cheap with fake downstream handlers/job manager, but the
test calls the real public handler methods.  It therefore fails if any surface
stops routing its background runner through ``AgentProcess.spawn``.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.tools.evolution_handlers import StartEvolveStepHandler
from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler
from ouroboros.mcp.tools.ralph_handlers import RalphHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.agent_process import run_with_agent_process
from ouroboros.persistence.event_store import EventStore


class _FakeEventStore:
    def __init__(self) -> None:
        self.appended: list[Any] = []
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True

    async def append(self, event: Any) -> None:
        self.appended.append(event)


def _directives(store: _FakeEventStore) -> list[str]:
    return [
        e.data["directive"]
        for e in store.appended
        if getattr(e, "type", None) == "control.directive.emitted"
    ]


def _directive_intents(store: _FakeEventStore) -> list[str]:
    return [
        e.data["extra"]["intent"]
        for e in store.appended
        if getattr(e, "type", None) == "control.directive.emitted"
    ]


def _lifecycle_statuses(store: _FakeEventStore) -> list[str]:
    return [
        e.data["extra"]["lifecycle_status"]
        for e in store.appended
        if getattr(e, "type", None) == "control.directive.emitted"
    ]


@dataclass
class _CompletedJobSnapshot:
    job_id: str
    links: JobLinks
    status: JobStatus = JobStatus.COMPLETED
    cursor: int = 1


class _CancellableJobManager:
    """JobManager test double that starts the runner and lets tests cancel it."""

    def __init__(self) -> None:
        self.runner_task: asyncio.Task[Any] | None = None
        self.job_types: list[str] = []
        self._counter = 0

    async def allocate_job_id(self) -> str:
        self._counter += 1
        return f"job_{uuid4().hex[:12]}"

    async def start_job(
        self,
        *,
        job_type: str,
        initial_message: str,  # noqa: ARG002 - mirrors JobManager API
        runner: Any,
        links: JobLinks | None = None,
        job_id: str | None = None,
    ) -> _CompletedJobSnapshot:
        job_id = job_id or await self.allocate_job_id()
        self.job_types.append(job_type)
        self.runner_task = asyncio.create_task(runner)
        return _CompletedJobSnapshot(
            job_id=job_id,
            links=links or JobLinks(),
            status=JobStatus.RUNNING,
        )

    async def cancel_runner(self) -> None:
        assert self.runner_task is not None
        self.runner_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.runner_task


class _InlineJobManager:
    """JobManager test double that executes the supplied production runner."""

    def __init__(self) -> None:
        self.runner_results: list[MCPToolResult] = []
        self.job_types: list[str] = []
        self._counter = 0

    async def allocate_job_id(self) -> str:
        self._counter += 1
        return f"job_{uuid4().hex[:12]}"

    async def start_job(
        self,
        *,
        job_type: str,
        initial_message: str,  # noqa: ARG002 - mirrors JobManager API
        runner: Any,
        links: JobLinks | None = None,
        job_id: str | None = None,
    ) -> _CompletedJobSnapshot:
        job_id = job_id or await self.allocate_job_id()
        self.job_types.append(job_type)
        self.runner_results.append(await runner)
        return _CompletedJobSnapshot(
            job_id=job_id,
            links=links or JobLinks(),
        )


class _FakeEvolveHandler:
    def __init__(self, *, is_error: bool = False, action: str = "converged") -> None:
        self.is_error = is_error
        self.action = action

    async def handle(self, arguments: dict[str, Any]):
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="evolve result"),),
                is_error=self.is_error,
                meta={
                    "lineage_id": arguments["lineage_id"],
                    "generation": 1,
                    "action": self.action,
                },
            )
        )


class _BlockingEvolveHandler:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def handle(self, arguments: dict[str, Any]):  # noqa: ARG002 - protocol fixture
        self.started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return Result.ok(MCPToolResult())


class _FakeExecuteHandler:
    agent_runtime_backend: str | None = None
    llm_backend: str | None = None

    def __init__(
        self,
        *,
        is_error: bool = False,
        action: str | None = None,
        status: str | None = None,
        include_action: bool = True,
    ) -> None:
        self.is_error = is_error
        self.action = action or ("failed" if is_error else "completed")
        self.status = status
        self.include_action = include_action

    async def handle(
        self,
        arguments: dict[str, Any],
        *,
        execution_id: str | None = None,
        session_id_override: str | None = None,
        synchronous: bool = False,
    ):
        assert synchronous is True
        meta = {
            "status": self.status or self.action,
            "seed_content": arguments["seed_content"],
            "execution_id": execution_id,
            "session_id": session_id_override,
        }
        if self.include_action:
            meta["action"] = self.action
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="execute ok"),),
                is_error=self.is_error,
                meta=meta,
            )
        )


class _BlockingExecuteHandler:
    agent_runtime_backend: str | None = None
    llm_backend: str | None = None

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def handle(
        self,
        arguments: dict[str, Any],  # noqa: ARG002 - protocol fixture
        *,
        execution_id: str | None = None,  # noqa: ARG002 - protocol fixture
        session_id_override: str | None = None,  # noqa: ARG002 - protocol fixture
        synchronous: bool = False,  # noqa: ARG002 - protocol fixture
    ):
        self.started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return Result.ok(MCPToolResult())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "expected_intent", "expected_job_type"),
    [
        ("evolve_step", "evolve_step", "evolve_step"),
        ("ralph", "ralph", "ralph"),
        ("execute_seed", "execute_seed", "execute_seed"),
    ],
)
async def test_production_start_surfaces_emit_running_then_converge(
    surface: str,
    expected_intent: str,
    expected_job_type: str,
) -> None:
    """Each real start surface routes its background runner through AgentProcess."""
    store = _FakeEventStore()
    job_manager = _InlineJobManager()

    if surface == "evolve_step":
        handler = StartEvolveStepHandler(
            evolve_handler=_FakeEvolveHandler(),  # type: ignore[arg-type]
            event_store=store,  # type: ignore[arg-type]
            job_manager=job_manager,  # type: ignore[arg-type]
        )
        result = await handler.handle({"lineage_id": "lin_accept", "seed_content": "goal: test"})
    elif surface == "ralph":
        handler = RalphHandler(
            evolve_handler=_FakeEvolveHandler(),  # type: ignore[arg-type]
            event_store=store,  # type: ignore[arg-type]
            job_manager=job_manager,  # type: ignore[arg-type]
        )
        result = await handler.handle(
            {
                "lineage_id": "lin_accept",
                "seed_content": "goal: test",
                "max_generations": 1,
                "per_iteration_timeout_seconds": 30,
                "max_total_seconds": 30,
            }
        )
    else:
        handler = StartExecuteSeedHandler(
            execute_handler=_FakeExecuteHandler(),  # type: ignore[arg-type]
            event_store=store,  # type: ignore[arg-type]
            job_manager=job_manager,  # type: ignore[arg-type]
        )
        result = await handler.handle({"seed_content": "goal: test"})

    assert result.is_ok, surface
    directives = _directives(store)
    assert directives[0] == "continue", surface
    assert directives[-1] == "converge", surface
    assert _directive_intents(store) == [expected_intent, expected_intent]
    assert job_manager.job_types == [expected_job_type]
    assert len(job_manager.runner_results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "expected_intent"),
    [
        ("evolve_step", "evolve_step"),
        ("ralph", "ralph"),
        ("execute_seed", "execute_seed"),
    ],
)
async def test_production_start_surfaces_emit_cancel_when_job_runner_is_cancelled(
    surface: str, expected_intent: str
) -> None:
    """Job-runner cancellation is mirrored into the AgentProcess lifecycle."""
    store = _FakeEventStore()
    job_manager = _CancellableJobManager()

    if surface == "evolve_step":
        blocking = _BlockingEvolveHandler()
        handler = StartEvolveStepHandler(
            evolve_handler=blocking,  # type: ignore[arg-type]
            event_store=store,  # type: ignore[arg-type]
            job_manager=job_manager,  # type: ignore[arg-type]
        )
        result = await handler.handle({"lineage_id": "lin_cancel", "seed_content": "goal: test"})
        await asyncio.wait_for(blocking.started.wait(), timeout=2.0)
        blocking_handler = blocking
    elif surface == "ralph":
        blocking = _BlockingEvolveHandler()
        handler = RalphHandler(
            evolve_handler=blocking,  # type: ignore[arg-type]
            event_store=store,  # type: ignore[arg-type]
            job_manager=job_manager,  # type: ignore[arg-type]
        )
        result = await handler.handle(
            {
                "lineage_id": "lin_cancel",
                "seed_content": "goal: test",
                "max_generations": 1,
                "per_iteration_timeout_seconds": 30,
                "max_total_seconds": 30,
            }
        )
        await asyncio.wait_for(blocking.started.wait(), timeout=2.0)
        blocking_handler = blocking
    else:
        blocking = _BlockingExecuteHandler()
        handler = StartExecuteSeedHandler(
            execute_handler=blocking,  # type: ignore[arg-type]
            event_store=store,  # type: ignore[arg-type]
            job_manager=job_manager,  # type: ignore[arg-type]
        )
        result = await handler.handle({"seed_content": "goal: test"})
        await asyncio.wait_for(blocking.started.wait(), timeout=2.0)
        blocking_handler = blocking

    assert result.is_ok, surface
    await job_manager.cancel_runner()

    directives = _directives(store)
    assert directives[0] == "continue", surface
    assert directives[-1] == "cancel", surface
    assert _directive_intents(store)[-1] == expected_intent
    assert blocking_handler.cancelled is True


@pytest.mark.asyncio
async def test_run_with_agent_process_preserves_original_failure_message() -> None:
    store = _FakeEventStore()

    async def _failing_work(_handle: Any) -> MCPToolResult:
        raise RuntimeError("evolve_step exploded with actionable detail")

    with pytest.raises(RuntimeError, match="actionable detail"):
        await run_with_agent_process(
            event_store=store,
            intent="evolve_step",
            work_fn=_failing_work,
        )

    directives = _directives(store)
    assert directives[0] == "continue"
    assert directives[-1] == "cancel"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "expected_intent"),
    [
        ("evolve_step", "evolve_step"),
        ("ralph", "ralph"),
        ("execute_seed", "execute_seed"),
    ],
)
async def test_production_start_surfaces_do_not_converge_error_tool_results(
    surface: str, expected_intent: str
) -> None:
    """MCPToolResult(is_error=True) is a lifecycle failure, not convergence."""
    store = _FakeEventStore()
    job_manager = _InlineJobManager()

    if surface == "evolve_step":
        handler = StartEvolveStepHandler(
            evolve_handler=_FakeEvolveHandler(is_error=True, action="failed"),  # type: ignore[arg-type]
            event_store=store,  # type: ignore[arg-type]
            job_manager=job_manager,  # type: ignore[arg-type]
        )
        result = await handler.handle({"lineage_id": "lin_error", "seed_content": "goal: test"})
    elif surface == "ralph":
        handler = RalphHandler(
            evolve_handler=_FakeEvolveHandler(is_error=True, action="failed"),  # type: ignore[arg-type]
            event_store=store,  # type: ignore[arg-type]
            job_manager=job_manager,  # type: ignore[arg-type]
        )
        result = await handler.handle(
            {
                "lineage_id": "lin_error",
                "seed_content": "goal: test",
                "max_generations": 1,
                "per_iteration_timeout_seconds": 30,
                "max_total_seconds": 30,
            }
        )
    else:
        handler = StartExecuteSeedHandler(
            execute_handler=_FakeExecuteHandler(is_error=True),  # type: ignore[arg-type]
            event_store=store,  # type: ignore[arg-type]
            job_manager=job_manager,  # type: ignore[arg-type]
        )
        result = await handler.handle({"seed_content": "goal: test"})

    assert result.is_ok, surface
    assert job_manager.runner_results[0].is_error is True
    directives = _directives(store)
    assert directives[0] == "continue", surface
    assert directives[-1] == "cancel", surface
    assert "converge" not in directives, surface
    assert _directive_intents(store)[-1] == expected_intent


@pytest.mark.asyncio
async def test_run_with_agent_process_cleans_up_work_task_on_timeout() -> None:
    store = _FakeEventStore()
    started = asyncio.Event()
    cancelled = False

    async def _slow_work(_handle: Any) -> MCPToolResult:
        nonlocal cancelled
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return MCPToolResult()

    with pytest.raises(TimeoutError):
        await run_with_agent_process(
            event_store=store,
            intent="timeout_surface",
            work_fn=_slow_work,
            timeout=0.01,
        )

    assert started.is_set()
    assert cancelled is True
    directives = _directives(store)
    assert directives[0] == "continue"
    assert directives[-1] == "cancel"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "expected_intent"),
    [
        ("evolve_step", "evolve_step"),
        ("ralph", "ralph"),
        ("execute_seed", "execute_seed"),
    ],
)
async def test_production_start_surfaces_classify_interrupted_results_as_cancelled(
    surface: str, expected_intent: str
) -> None:
    """Interrupted result-level errors stay distinct from failed work."""
    store = _FakeEventStore()
    job_manager = _InlineJobManager()

    if surface == "evolve_step":
        handler = StartEvolveStepHandler(
            evolve_handler=_FakeEvolveHandler(is_error=True, action="interrupted"),  # type: ignore[arg-type]
            event_store=store,  # type: ignore[arg-type]
            job_manager=job_manager,  # type: ignore[arg-type]
        )
        result = await handler.handle(
            {"lineage_id": "lin_interrupted", "seed_content": "goal: test"}
        )
    elif surface == "ralph":
        handler = RalphHandler(
            evolve_handler=_FakeEvolveHandler(is_error=True, action="interrupted"),  # type: ignore[arg-type]
            event_store=store,  # type: ignore[arg-type]
            job_manager=job_manager,  # type: ignore[arg-type]
        )
        result = await handler.handle(
            {
                "lineage_id": "lin_interrupted",
                "seed_content": "goal: test",
                "max_generations": 1,
                "per_iteration_timeout_seconds": 30,
                "max_total_seconds": 30,
            }
        )
    else:
        handler = StartExecuteSeedHandler(
            execute_handler=_FakeExecuteHandler(
                is_error=True, status="cancelled", include_action=False
            ),  # type: ignore[arg-type]
            event_store=store,  # type: ignore[arg-type]
            job_manager=job_manager,  # type: ignore[arg-type]
        )
        result = await handler.handle({"seed_content": "goal: test"})

    assert result.is_ok, surface
    assert job_manager.runner_results[0].is_error is True
    assert _directives(store)[-1] == "cancel", surface
    assert _directive_intents(store)[-1] == expected_intent
    assert _lifecycle_statuses(store)[-1] == "cancelled"


@pytest.mark.asyncio
async def test_job_manager_classifies_status_only_cancelled_result_as_cancelled() -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    job_manager = JobManager(store)

    try:

        async def _cancelled_result() -> MCPToolResult:
            return MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="cancelled"),),
                is_error=True,
                meta={"status": "cancelled"},
            )

        snapshot = await job_manager.start_job(
            job_type="execute_seed",
            initial_message="Queued execution",
            runner=_cancelled_result(),
        )

        deadline = asyncio.get_running_loop().time() + 2.0
        while not snapshot.is_terminal and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
            snapshot = await job_manager.get_snapshot(snapshot.job_id)

        assert snapshot.status is JobStatus.CANCELLED
        assert snapshot.result_meta["status"] == "cancelled"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_run_with_agent_process_timeout_does_not_publish_terminal_cancel_for_stubborn_worker() -> (
    None
):
    store = _FakeEventStore()
    release = asyncio.Event()
    swallowed_cancel = asyncio.Event()

    async def _stubborn_work(_handle: Any) -> MCPToolResult:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            swallowed_cancel.set()
            await release.wait()
        return MCPToolResult()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            run_with_agent_process(
                event_store=store,
                intent="stubborn_timeout_surface",
                work_fn=_stubborn_work,
                timeout=0.01,
            ),
            timeout=2.0,
        )

    assert swallowed_cancel.is_set()
    assert _directives(store) == ["continue"]

    release.set()
    deadline = asyncio.get_running_loop().time() + 2.0
    while _directives(store)[-1] != "cancel" and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)

    assert _directives(store)[-1] == "cancel"
    assert _lifecycle_statuses(store)[-1] == "cancelled"


@pytest.mark.asyncio
async def test_run_with_agent_process_external_cancel_waits_for_stubborn_worker_to_stop() -> None:
    store = _FakeEventStore()
    release = asyncio.Event()
    swallowed_cancel = asyncio.Event()

    async def _stubborn_work(_handle: Any) -> MCPToolResult:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            swallowed_cancel.set()
            await release.wait()
        return MCPToolResult()

    task = asyncio.create_task(
        run_with_agent_process(
            event_store=store,
            intent="stubborn_cancel_surface",
            work_fn=_stubborn_work,
        )
    )
    await asyncio.sleep(0)

    task.cancel()
    await asyncio.wait_for(swallowed_cancel.wait(), timeout=2.0)
    task.cancel()
    await asyncio.sleep(1.1)

    assert not task.done()
    assert _directives(store) == ["continue"]

    release.set()
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    assert task.cancelled()
    assert _directives(store)[-1] == "cancel"
    assert _lifecycle_statuses(store)[-1] == "cancelled"
