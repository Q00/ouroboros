from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ouroboros.core.types import Result
from ouroboros.mcp.job_manager import JobLinks, JobSnapshot, JobStatus
from ouroboros.mcp.tools import background
from ouroboros.mcp.tools.evaluation_handlers import StartEvaluateHandler
from ouroboros.mcp.tools.evolution_handlers import StartEvolveStepHandler
from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler
from ouroboros.mcp.tools.ralph_handlers import RalphHandler
from ouroboros.mcp.types import MCPToolResult


class _EventStore:
    async def initialize(self) -> None:
        return None


class _JobManager:
    def __init__(self) -> None:
        self._counter = 0
        self.started: list[dict[str, Any]] = []

    async def allocate_job_id(self) -> str:
        self._counter += 1
        return f"job_{self._counter:012d}"

    async def start_job(
        self,
        *,
        job_type: str,
        initial_message: str,
        runner: Any,
        links: JobLinks | None = None,
        job_id: str | None = None,
    ) -> JobSnapshot:
        job_id = job_id or await self.allocate_job_id()
        self.started.append({"job_type": job_type, "job_id": job_id, "links": links})
        try:
            runner.close()
        except AttributeError:
            pass
        now = datetime.now(UTC)
        return JobSnapshot(
            job_id=job_id,
            job_type=job_type,
            status=JobStatus.QUEUED,
            message=initial_message,
            created_at=now,
            updated_at=now,
            links=links or JobLinks(),
        )


class _EvolveHandler:
    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, Any]:
        return Result.ok(MCPToolResult(meta={"lineage_id": arguments["lineage_id"]}))


class _ExecuteHandler:
    agent_runtime_backend: str | None = None
    llm_backend: str | None = None

    async def handle(self, *args: Any, **kwargs: Any) -> Result[MCPToolResult, Any]:
        return Result.ok(MCPToolResult())


class _EvaluateHandler:
    async def handle(self, *args: Any, **kwargs: Any) -> Result[MCPToolResult, Any]:
        return Result.ok(MCPToolResult())


def _capture_run_with_agent_process(monkeypatch) -> list[dict[str, Any]]:
    """Capture the ``run_with_agent_process`` calls the shared background helper makes.

    Since the ``Start*`` handlers were consolidated onto
    ``mcp.tools.background.start_background_tool_job``, the
    ``run_with_agent_process`` call site lives in the ``background`` module,
    so that is the patch target for every handler under test.
    """
    calls: list[dict[str, Any]] = []

    def _fake_run_with_agent_process(**kwargs: Any):
        calls.append(kwargs)

        async def _runner() -> MCPToolResult:
            return MCPToolResult()

        return _runner()

    monkeypatch.setattr(background, "run_with_agent_process", _fake_run_with_agent_process)
    return calls


async def test_ralph_jobs_on_same_lineage_use_distinct_job_scoped_cancel_keys(monkeypatch) -> None:
    calls = _capture_run_with_agent_process(monkeypatch)
    job_manager = _JobManager()
    handler = RalphHandler(
        evolve_handler=_EvolveHandler(),
        event_store=_EventStore(),  # type: ignore[arg-type]
        job_manager=job_manager,  # type: ignore[arg-type]
    )

    first = await handler.handle({"lineage_id": "lin_same"})
    second = await handler.handle({"lineage_id": "lin_same"})

    assert first.is_ok and second.is_ok
    assert [call["cancel_key"] for call in calls] == [
        "mcp_job:job_000000000001",
        "mcp_job:job_000000000002",
    ]
    assert calls[0]["cancel_key"] != "ralph:lin_same"
    assert calls[0]["process_id"] != calls[1]["process_id"]
    assert first.value.meta["job_observer"]["job_id"] == "job_000000000001"
    assert second.value.meta["job_observer"]["job_id"] == "job_000000000002"


async def test_evolve_jobs_on_same_lineage_use_distinct_job_scoped_cancel_keys(monkeypatch) -> None:
    calls = _capture_run_with_agent_process(monkeypatch)
    job_manager = _JobManager()
    handler = StartEvolveStepHandler(
        evolve_handler=_EvolveHandler(),  # type: ignore[arg-type]
        event_store=_EventStore(),  # type: ignore[arg-type]
        job_manager=job_manager,  # type: ignore[arg-type]
    )

    first = await handler.handle({"lineage_id": "lin_same"})
    second = await handler.handle({"lineage_id": "lin_same"})

    assert first.is_ok and second.is_ok
    assert [call["cancel_key"] for call in calls] == [
        "mcp_job:job_000000000001",
        "mcp_job:job_000000000002",
    ]
    assert calls[0]["cancel_key"] != "evolve_step:lin_same"
    assert calls[0]["process_id"] != calls[1]["process_id"]
    assert first.value.meta["job_observer"]["job_id"] == "job_000000000001"
    assert second.value.meta["job_observer"]["job_id"] == "job_000000000002"


async def test_execute_seed_uses_job_scoped_cancel_key(monkeypatch, tmp_path) -> None:
    calls = _capture_run_with_agent_process(monkeypatch)
    job_manager = _JobManager()
    handler = StartExecuteSeedHandler(
        execute_handler=_ExecuteHandler(),  # type: ignore[arg-type]
        event_store=_EventStore(),  # type: ignore[arg-type]
        job_manager=job_manager,  # type: ignore[arg-type]
    )

    result = await handler.handle({"seed_content": "goal: test\n", "cwd": str(tmp_path)})

    assert result.is_ok
    assert calls[0]["cancel_key"] == "mcp_job:job_000000000001"
    assert calls[0]["process_id"].startswith("execute_seed:exec_")
    assert calls[0]["cancel_key"] != calls[0]["process_id"]


async def test_start_evaluate_now_passes_durable_job_scoped_cancel_key(monkeypatch) -> None:
    """Behaviour fix: StartEvaluate must bind the durable ``mcp_job:{job_id}`` cancel key.

    Before the background-pipeline consolidation StartEvaluate wrapped its
    runner with ``lambda _handle: _runner()`` and passed neither ``process_id``
    nor ``cancel_key``.  ``run_with_agent_process`` only builds the
    :class:`CheckpointStore` that loads a persisted cancel marker when
    ``cancel_key or process_id`` is set, so the durable ``mcp_job:{job_id}``
    marker written by ``JobManager.cancel_job`` was never observable for
    evaluate jobs — a restart-visible cancel was silently dropped.  Routing
    through ``start_background_tool_job`` now binds it, so cancelling an
    evaluate job is durably honoured across a restart, matching evolve/
    execute/ralph.
    """
    calls = _capture_run_with_agent_process(monkeypatch)
    job_manager = _JobManager()
    handler = StartEvaluateHandler(
        evaluate_handler=_EvaluateHandler(),  # type: ignore[arg-type]
        event_store=_EventStore(),  # type: ignore[arg-type]
        job_manager=job_manager,  # type: ignore[arg-type]
    )

    result = await handler.handle({"session_id": "orch_eval", "artifact": "code"})

    assert result.is_ok
    # The durable cancel key is now bound (previously absent), so the
    # AgentProcess will load the persisted mcp_job:{job_id} marker on spawn.
    assert calls[0]["cancel_key"] == "mcp_job:job_000000000001"
    assert calls[0]["process_id"] == "evaluate:orch_eval:job_000000000001"
    assert calls[0]["cancel_key"] != calls[0]["process_id"]
    assert result.value.meta["job_observer"]["session_id"] == "orch_eval"
