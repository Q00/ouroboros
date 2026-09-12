"""Regression tests for dependency-cycle failures at the runner boundary."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.orchestrator.dependency_analyzer import DependencyAnalysisError, DependencyAnalyzer
from ouroboros.orchestrator.parallel_executor import ACExecutionResult, ParallelExecutionResult
from ouroboros.orchestrator.runner import OrchestratorRunner
from ouroboros.orchestrator.session import SessionRepository, SessionStatus
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers.base import CompletionResponse, UsageInfo


@pytest.fixture
def seed() -> Seed:
    return Seed(
        goal="Build two cooperating components",
        acceptance_criteria=("Build component A", "Build component B"),
        ontology_schema=OntologySchema(name="Components", description="Two components"),
        metadata=SeedMetadata(),
    )


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "0")
    runtime = MagicMock()
    runtime.runtime_backend = "opencode"
    runtime.llm_backend = "test-llm"
    runtime._model = "test-model"
    runtime.working_directory = str(tmp_path)
    runtime.permission_mode = "acceptEdits"
    runtime.aclose = AsyncMock()
    return runtime


@pytest.mark.asyncio
async def test_execute_seed_cycle_persists_failure_without_dispatch(
    seed: Seed, adapter: MagicMock, tmp_path: Path
) -> None:
    """Real analysis and runner setup reject a cycle before publishing a plan."""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'cycle.db'}"
    store = EventStore(database_url)
    await store.initialize()
    runner = OrchestratorRunner(adapter, store, MagicMock(), enable_decomposition=False)
    analyzer = DependencyAnalyzer(llm_adapter=MagicMock(), model="test-model")
    completion = AsyncMock(
        return_value=Result.ok(
            CompletionResponse(
                content=json.dumps(
                    {
                        "dependencies": [
                            {"ac_index": 0, "depends_on": [1]},
                            {"ac_index": 1, "depends_on": [0]},
                        ]
                    }
                ),
                model="test-model",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )
    )
    try:
        with (
            patch.object(runner, "_build_dependency_analyzer", return_value=analyzer),
            patch("ouroboros.orchestrator.dependency_analyzer.tracked_complete", completion),
            patch("ouroboros.orchestrator.parallel_executor.ParallelACExecutor") as executor,
        ):
            result = await runner.execute_seed(
                seed, execution_id="execution-dependencies", session_id="session-dependencies"
            )

        assert result.is_err
        executor.assert_not_called()
        adapter.execute_task.assert_not_called()
        assert "Circular AC dependencies" in result.error.message
        completion.assert_awaited_once()
        adapter.aclose.assert_awaited_once()
    finally:
        await store.close()

    # Reopen the database so the assertions prove durable state, not a mock or cache.
    persisted_store = EventStore(database_url)
    await persisted_store.initialize()
    try:
        reconstructed = await SessionRepository(persisted_store).reconstruct_session(
            "session-dependencies"
        )
        assert reconstructed.is_ok
        assert reconstructed.value.status is SessionStatus.FAILED
        failures = await persisted_store.query_events(
            aggregate_id="session-dependencies", event_type="orchestrator.session.failed"
        )
        assert len(failures) == 1
        assert failures[0].data["error_type"] == "DependencyCycleError"
        assert "Circular AC dependencies" in failures[0].data["error"]
        plans = await persisted_store.query_events(
            aggregate_id="execution-dependencies", event_type="execution.plan.created"
        )
        assert plans == []
    finally:
        await persisted_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("generic_error", [False, True], ids=["acyclic", "ordinary-analysis-error"])
async def test_execute_seed_preserves_noncycle_planning(
    seed: Seed, adapter: MagicMock, tmp_path: Path, generic_error: bool
) -> None:
    """A DAG keeps its edges; an ordinary analysis error keeps the legacy fallback."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'noncycle.db'}")
    await store.initialize()
    runner = OrchestratorRunner(adapter, store, MagicMock(), enable_decomposition=False)
    completion = AsyncMock(
        return_value=Result.ok(
            CompletionResponse(
                content=json.dumps(
                    {
                        "dependencies": [
                            {"ac_index": 0, "depends_on": []},
                            {"ac_index": 1, "depends_on": [0]},
                        ]
                    }
                ),
                model="test-model",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )
    )
    analyzer = DependencyAnalyzer(llm_adapter=MagicMock(), model="test-model")
    if generic_error:
        analyzer.analyze = AsyncMock(
            return_value=Result.err(DependencyAnalysisError("unavailable"))
        )
    parallel_result = ParallelExecutionResult(
        results=tuple(
            ACExecutionResult(
                ac_index=index, ac_content=criterion, success=True, final_message="done"
            )
            for index, criterion in enumerate(seed.acceptance_criteria)
        ),
        success_count=2,
        failure_count=0,
        total_messages=2,
    )
    dispatch = AsyncMock(return_value=parallel_result)
    try:
        with (
            patch.object(runner, "_build_dependency_analyzer", return_value=analyzer),
            patch("ouroboros.orchestrator.dependency_analyzer.tracked_complete", completion),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                dispatch,
            ),
        ):
            result = await runner.execute_seed(
                seed, execution_id="execution-noncycle", session_id="session-noncycle"
            )

        assert result.is_ok, result.error if result.is_err else None
        assert result.value.success
        dispatch.assert_awaited_once()
        execution_plan = dispatch.await_args.kwargs["execution_plan"]
        expected_levels = ((0, 1),) if generic_error else ((0,), (1,))
        assert execution_plan.execution_levels == expected_levels
        assert execution_plan.get_dependencies(0) == ()
        assert execution_plan.get_dependencies(1) == (() if generic_error else (0,))
        if generic_error:
            completion.assert_not_awaited()
        else:
            completion.assert_awaited_once()
        persisted = await runner._session_repo.reconstruct_session("session-noncycle")
        assert persisted.is_ok
        assert persisted.value.status is SessionStatus.COMPLETED
        plans = await store.query_events(
            aggregate_id="execution-noncycle", event_type="execution.plan.created"
        )
        assert len(plans) == 1
    finally:
        await store.close()
