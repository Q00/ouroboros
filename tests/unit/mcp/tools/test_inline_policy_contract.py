"""Accepted inline policy reaches the real execute handler and runner."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.job_manager import JobManager
from ouroboros.mcp.tools import execution_handlers
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler, StartExecuteSeedHandler
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.event_store import EventStore

SEED = """\
goal: Synthetic policy handoff
acceptance_criteria: [Task completes]
constraints: []
ontology_schema: {name: Synthetic, description: Synthetic, fields: []}
metadata:
  seed_id: synthetic-policy
  version: '1.0.0'
  created_at: '2024-01-01T00:00:00Z'
  ambiguity_score: 0.1
"""


@pytest.mark.parametrize(
    "selected,later",
    [("adaptive", "quality_first"), ("quality_first", "adaptive"), (None, "quality_first")],
)
async def test_start_execute_policy_reaches_runner_and_job_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: str | None, later: str
) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()
    manager = JobManager(store)
    observed = []
    sessions = SessionRepository(store)

    class Runner:
        def __init__(self, *args, **kwargs):
            observed.append(kwargs["efficiency_mode"])

        def _has_live_process_local_authority(self, *args, **kwargs):
            return False

        async def prepare_session(self, seed, *, execution_id, session_id):
            return await sessions.create_session(
                execution_id, seed.metadata.seed_id, session_id=session_id
            )

        async def execute_precreated_session(self, *, seed, tracker, parallel):
            completed = await sessions.mark_completed(tracker.session_id)
            assert completed.is_ok and completed.value
            return Result.ok(
                SimpleNamespace(
                    success=True,
                    execution_id=tracker.execution_id,
                    summary={},
                    final_message="done",
                )
            )

    monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "off")
    real_start = execution_handlers.start_background_tool_job

    async def change_default_before_enqueue(**kwargs):
        monkeypatch.setattr(execution_handlers, "default_execution_efficiency_mode", lambda: later)
        return await real_start(**kwargs)

    monkeypatch.setattr(execution_handlers, "default_execution_efficiency_mode", lambda: selected)
    monkeypatch.setattr(
        execution_handlers, "start_background_tool_job", change_default_before_enqueue
    )
    monkeypatch.setattr(
        execution_handlers,
        "create_agent_runtime",
        lambda **_: SimpleNamespace(runtime_backend="pi"),
    )
    monkeypatch.setattr("ouroboros.orchestrator.runner_factory.OrchestratorRunner", Runner)
    execute = ExecuteSeedHandler(
        event_store=store, agent_runtime_backend="pi", llm_backend="claude_code"
    )
    start = StartExecuteSeedHandler(
        execute_handler=execute, event_store=store, job_manager=manager, agent_runtime_backend="pi"
    )
    try:
        args = {
            "seed_content": SEED,
            "cwd": str(tmp_path),
            "use_worktree": False,
            "skip_qa": True,
            "auto_evaluate": False,
            "idempotency_key": "synthetic-policy",
        }
        receipt = await start.handle(args)
        assert receipt.is_ok
        job_id = receipt.value.meta["job_id"]
        for _ in range(100):
            snapshot = await manager.get_snapshot(job_id)
            if snapshot.is_terminal:
                break
            await asyncio.sleep(0.01)
        assert snapshot.status.value == "completed", snapshot
        expected = selected or "adaptive"
        assert observed == [selected]
        assert f"Efficiency Mode: {expected}" in receipt.value.text_content
        assert f"Efficiency Mode: {expected}" in snapshot.result_text
        replay = await start.handle(args)
        assert replay.is_ok and replay.value.meta["job_id"] == job_id
        assert observed == [selected]
    finally:
        await manager.drain()
        await store.close()
