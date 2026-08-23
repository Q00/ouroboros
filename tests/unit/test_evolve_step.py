"""Unit tests for evolve_step() — single-generation stepping API."""

import asyncio
from dataclasses import fields, replace
import inspect
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.config.models import RuntimeControlsConfig
from ouroboros.core.directive import Directive
from ouroboros.core.errors import OuroborosError
from ouroboros.core.lineage import (
    ACResult,
    EvaluationSummary,
    FeedbackMetadata,
    GenerationPhase,
    GenerationRecord,
    LineageStatus,
    OntologyDelta,
    OntologyLineage,
)
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.core.worktree import TaskWorkspace, WorktreeError
from ouroboros.events.base import BaseEvent
from ouroboros.events.lineage import (
    lineage_created,
    lineage_generation_completed,
    lineage_generation_failed,
    lineage_generation_interrupted,
    lineage_generation_phase_changed,
    lineage_generation_started,
)
from ouroboros.evolution import loop_support
from ouroboros.evolution.convergence import ConvergenceSignal
from ouroboros.evolution.loop import (
    EvolutionaryLoop,
    EvolutionaryLoopConfig,
    GenerationResult,
    StepAction,
    StepResult,
)
from ouroboros.evolution.projector import LineageProjector
from ouroboros.evolution.reflect import ACPatch, ReflectOutput
from ouroboros.evolution.watchdog import GenerationWatchdogTimeout
from ouroboros.evolution.wonder import WonderOutput
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.job_manager import JobManager, JobStatus
from ouroboros.mcp.server.adapter import _extract_feedback_metadata_from_artifact
from ouroboros.mcp.tools.evolution_handlers import EvolveStepHandler, StartEvolveStepHandler
from ouroboros.mcp.tools.job_observer import extract_job_observer_inline_handoff
from ouroboros.mcp.tools.synapse_handler import SynapseTargetsHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.synapse import EventStoreSessionSignalTargetResolver
from ouroboros.persistence import lineage_claims
from ouroboros.persistence.event_store import EventStore

# -- Helpers --

_RUNTIME_CONTROL_ENV_KEYS = (
    "OUROBOROS_MCP_TOOL_TIMEOUT_SECONDS",
    "OUROBOROS_GENERATION_IDLE_TIMEOUT_SECONDS",
    "OUROBOROS_GENERATION_NO_PROGRESS_TIMEOUT_SECONDS",
    "OUROBOROS_GENERATION_SAFETY_TIMEOUT_SECONDS",
    "OUROBOROS_WATCHDOG_POLL_SECONDS",
    "OUROBOROS_GENERATION_TIMEOUT",
)


def _clear_runtime_control_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_key in _RUNTIME_CONTROL_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)


def make_seed(
    goal: str = "Build a task manager",
    seed_id: str = "seed_001",
    parent_seed_id: str | None = None,
    ontology_name: str = "TaskManager",
    fields: tuple[OntologyField, ...] | None = None,
) -> Seed:
    """Create a test Seed."""
    if fields is None:
        fields = (
            OntologyField(
                name="tasks",
                field_type="array",
                description="List of task objects",
                required=True,
            ),
        )
    return Seed(
        goal=goal,
        task_type="code",
        constraints=("Python 3.14+",),
        acceptance_criteria=("Tasks can be created",),
        ontology_schema=OntologySchema(
            name=ontology_name,
            description=f"{ontology_name} domain model",
            fields=fields,
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="completeness",
                description="All requirements implemented",
                weight=1.0,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="all_criteria_met",
                description="All acceptance criteria pass",
                evaluation_criteria="100% criteria satisfied",
            ),
        ),
        metadata=SeedMetadata(
            seed_id=seed_id,
            parent_seed_id=parent_seed_id,
            ambiguity_score=0.1,
        ),
    )


def make_eval_summary(approved: bool = True, score: float = 0.85) -> EvaluationSummary:
    """Create a test EvaluationSummary."""
    return EvaluationSummary(
        final_approved=approved,
        highest_stage_passed=2,
        score=score,
        ac_results=(
            ACResult(
                ac_index=0,
                ac_content="Tasks can be created",
                passed=approved,
            ),
        ),
    )


def make_wonder_output(
    questions: tuple[str, ...] = ("What about edge cases?",),
    should_continue: bool = True,
) -> WonderOutput:
    """Create a test WonderOutput."""
    return WonderOutput(
        questions=questions,
        ontology_tensions=(),
        should_continue=should_continue,
        reasoning="Test reasoning",
    )


async def create_event_store() -> EventStore:
    """Create an in-memory EventStore for testing."""
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    return store


def make_loop(
    event_store: EventStore,
    gen_result: GenerationResult | None = None,
    gen_error: OuroborosError | None = None,
    config: EvolutionaryLoopConfig | None = None,
) -> EvolutionaryLoop:
    """Create an EvolutionaryLoop with mocked engines.

    Args:
        event_store: The event store to use.
        gen_result: If provided, _run_generation returns this.
        gen_error: If provided, _run_generation returns this error.
    """
    loop = EvolutionaryLoop(
        event_store=event_store,
        config=config
        or EvolutionaryLoopConfig(
            max_generations=30,
            convergence_threshold=0.95,
            stagnation_window=3,
            min_generations=2,
        ),
    )

    if gen_result is not None:
        loop._run_generation = AsyncMock(return_value=Result.ok(gen_result))
    elif gen_error is not None:
        loop._run_generation = AsyncMock(return_value=Result.err(gen_error))

    return loop


def make_watchdog_timeout(
    timeout_kind: str,
    lineage_id: str,
    generation_number: int = 1,
) -> GenerationWatchdogTimeout:
    """Create a watchdog timeout with the fields emitted by the real watchdog."""
    return GenerationWatchdogTimeout(
        timeout_kind=timeout_kind,
        reason=f"synthetic {timeout_kind}",
        details={
            "timeout_kind": timeout_kind,
            "lineage_id": lineage_id,
            "generation_number": generation_number,
            "execution_id": f"evolve:{lineage_id}:generation:{generation_number}",
            "elapsed_seconds": 10.0,
            "idle_seconds": 7.0,
            "no_material_progress_seconds": 5.0,
            "activity_event_count": 2,
            "material_event_count": 0,
        },
    )


async def seed_events_for_gen1(
    event_store: EventStore,
    lineage_id: str,
    seed: Seed,
    eval_summary: EvaluationSummary | None = None,
) -> None:
    """Populate EventStore with Gen 1 events."""
    await event_store.append(lineage_created(lineage_id, seed.goal))
    await event_store.append(
        lineage_generation_completed(
            lineage_id,
            generation_number=1,
            seed_id=seed.metadata.seed_id,
            ontology_snapshot=seed.ontology_schema.model_dump(mode="json"),
            evaluation_summary=eval_summary.model_dump(mode="json") if eval_summary else None,
            wonder_questions=["Initial question"],
            seed_json=json.dumps(seed.to_dict()),
        )
    )


@pytest.mark.asyncio
async def test_start_evolve_links_generation_selected_after_interleaving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public job identity follows the durable core claim, not stale planning."""
    store = await create_event_store()
    lineage_id = "lin_start_claim_interleaving"
    initial = make_seed(seed_id="seed_start_claim_initial")
    successor = make_seed(
        seed_id="seed_start_claim_successor",
        parent_seed_id=initial.metadata.seed_id,
    )
    generation_result = GenerationResult(
        generation_number=2,
        seed=successor,
        evaluation_summary=make_eval_summary(),
        phase=GenerationPhase.COMPLETED,
        success=True,
    )
    loop = make_loop(store)
    generation_started = asyncio.Event()
    release_generation = asyncio.Event()

    async def run_generation(**kwargs):  # type: ignore[no-untyped-def]
        generation_number = kwargs["generation_number"]
        execution_id = loop_support.generation_execution_id(lineage_id, generation_number)
        await store.append(
            BaseEvent(
                type="execution.session.started",
                aggregate_type="execution",
                aggregate_id=f"{execution_id}:ac:0",
                data={
                    "execution_id": execution_id,
                    "session_scope_id": f"{execution_id}:ac:0",
                    "session_attempt_id": f"{execution_id}:ac:0:attempt:1",
                    "runtime_backend": "codex",
                    "ac_index": 0,
                    "acceptance_criterion": "Tasks can be created",
                },
            )
        )
        generation_started.set()
        await release_generation.wait()
        return Result.ok(generation_result)

    loop._run_generation = AsyncMock(side_effect=run_generation)
    manager = JobManager(store)
    evolve = EvolveStepHandler(
        evolutionary_loop=loop,
        event_store=store,
        agent_runtime_backend="codex",
    )
    start = StartEvolveStepHandler(
        evolve_handler=evolve,
        event_store=store,
        job_manager=manager,
        agent_runtime_backend="codex",
    )
    original_planned = loop_support.planned_evolve_generation
    core_planning = asyncio.Event()
    release_core_planning = asyncio.Event()
    planning_calls = 0

    async def interleaved_planning(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal planning_calls
        planning_calls += 1
        if planning_calls == 2:
            core_planning.set()
            await release_core_planning.wait()
        return await original_planned(*args, **kwargs)

    monkeypatch.setattr(loop_support, "planned_evolve_generation", interleaved_planning)
    start_task = asyncio.create_task(
        start.handle(
            {
                "lineage_id": lineage_id,
                "seed_content": json.dumps(initial.to_dict()),
                "execute": False,
            }
        )
    )
    try:
        await asyncio.wait_for(core_planning.wait(), timeout=1.0)
        await seed_events_for_gen1(store, lineage_id, initial, make_eval_summary(False))
        release_core_planning.set()
        started = await asyncio.wait_for(start_task, timeout=2.0)

        assert started.is_ok
        expected = f"evolve:{lineage_id}:generation:2"
        stale = f"evolve:{lineage_id}:generation:1"
        assert started.value.meta["execution_id"] == expected
        assert started.value.meta["job_observer"]["execution_id"] == expected
        assert (
            extract_job_observer_inline_handoff(
                started.value.text_content,
                expected_job_id=started.value.meta["job_id"],
                expected_execution_id=expected,
            )
            == started.value.meta["job_observer"]
        )
        assert started.value.structured_content["execution_id"] == expected

        await asyncio.wait_for(generation_started.wait(), timeout=1.0)
        targets = SynapseTargetsHandler(EventStoreSessionSignalTargetResolver(store))
        discovered = await targets.handle({"execution_id": expected})
        assert discovered.is_ok
        assert discovered.value.meta["active_target_count"] == 1
        assert discovered.value.meta["targets"][0]["execution_id"] == expected

        release_generation.set()

        job_id = started.value.meta["job_id"]
        snapshot = await asyncio.wait_for(manager.get_snapshot(job_id), timeout=5.0)
        for _ in range(100):
            if snapshot.is_terminal:
                break
            await asyncio.sleep(0.01)
            snapshot = await asyncio.wait_for(manager.get_snapshot(job_id), timeout=5.0)
        assert snapshot.status == JobStatus.COMPLETED
        assert snapshot.links.execution_id == expected
        assert loop._run_generation.call_args.kwargs["generation_number"] == 2

        job_events, _ = await store.get_events_after("job", job_id, last_row_id=0)
        created = next(event for event in job_events if event.type == "mcp.job.created")
        assert created.data["links"]["execution_id"] == expected
        assert stale not in json.dumps(created.data)

        handler_receipt = await lineage_claims.observe(
            store,
            scope="evolve-handler",
            lineage_id=lineage_id,
        )
        core_receipt = await lineage_claims.observe(
            store,
            scope="evolve-core",
            lineage_id=lineage_id,
        )
        assert handler_receipt is not None and handler_receipt.completed
        assert core_receipt is not None and core_receipt.completed
        assert handler_receipt.generation_number == core_receipt.generation_number == 2
    finally:
        release_core_planning.set()
        release_generation.set()
        if not start_task.done():
            start_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start_task
        await manager.drain(grace_seconds=0.1)
        await store.close()


@pytest.mark.asyncio
async def test_start_evolve_malformed_seed_preserves_typed_preclaim_error() -> None:
    """A pre-claim validation error stays typed and leaves no active job or claim."""
    store = await create_event_store()
    lineage_id = "lin_start_malformed_seed"
    loop = make_loop(store)
    loop.evolve_step = AsyncMock(side_effect=AssertionError("malformed seed must not execute"))
    evolve = EvolveStepHandler(evolutionary_loop=loop, event_store=store)
    manager = JobManager(store)
    start = StartEvolveStepHandler(
        evolve_handler=evolve,
        event_store=store,
        job_manager=manager,
    )

    try:
        result = await start.handle(
            {
                "lineage_id": lineage_id,
                "seed_content": "goal: [unterminated",
                "execute": False,
            }
        )

        assert result.is_err
        assert isinstance(result.error, MCPToolError)
        assert result.error.tool_name == "ouroboros_evolve_step"
        assert result.error.message.startswith("Failed to parse seed_content:")
        loop.evolve_step.assert_not_awaited()
        assert manager._reserved_job_ids == set()
        assert await store.query_events(event_type="mcp.job.created") == []

        handler_claim = await lineage_claims.observe(
            store,
            scope="evolve-handler",
            lineage_id=lineage_id,
        )
        core_claim = await lineage_claims.observe(
            store,
            scope="evolve-core",
            lineage_id=lineage_id,
        )
        assert handler_claim is not None and handler_claim.completed
        assert core_claim is None
    finally:
        await manager.drain(grace_seconds=0.1)
        await store.close()


@pytest.mark.asyncio
async def test_start_evolve_rejects_active_same_lineage_owner_without_waiting() -> None:
    """The background Start receipt never waits behind a long-running owner."""
    store = await create_event_store()
    lineage_id = "lin_start_existing_owner"
    seed = make_seed(seed_id="seed_start_existing_owner")
    loop = make_loop(store)
    loop.evolve_step = AsyncMock(side_effect=AssertionError("contender must not execute"))
    evolve = EvolveStepHandler(evolutionary_loop=loop, event_store=store)
    manager = JobManager(store)
    start = StartEvolveStepHandler(
        evolve_handler=evolve,
        event_store=store,
        job_manager=manager,
    )
    claim = await lineage_claims.try_acquire(
        store,
        scope="evolve-handler",
        lineage_id=lineage_id,
        generation_number=1,
        owner_id="existing-long-running-owner",
        request_key="existing-request",
    )
    assert claim is not None and claim.acquired
    try:
        result = await asyncio.wait_for(
            start.handle(
                {
                    "lineage_id": lineage_id,
                    "seed_content": json.dumps(seed.to_dict()),
                    "execute": False,
                }
            ),
            timeout=0.25,
        )

        assert result.is_err
        assert result.error.error_code == "evolve_lineage_busy"
        assert result.error.is_retriable is True
        loop.evolve_step.assert_not_awaited()
        observed = await lineage_claims.observe(
            store,
            scope="evolve-handler",
            lineage_id=lineage_id,
        )
        assert observed is not None
        assert observed.owner_id == "existing-long-running-owner"
        assert not observed.completed
    finally:
        await lineage_claims.release(
            store,
            scope="evolve-handler",
            lineage_id=lineage_id,
            owner_id="existing-long-running-owner",
        )
        await manager.drain(grace_seconds=0.1)
        await store.close()


@pytest.mark.asyncio
async def test_start_evolve_contention_race_returns_bounded_structured_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim acquired after the precheck cannot stall or leak a Start job."""
    from ouroboros.mcp.tools import evolve_start_claim

    store = await create_event_store()
    lineage_id = "lin_start_claim_race"
    seed = make_seed(seed_id="seed_start_claim_race")
    loop = make_loop(store)
    loop.evolve_step = AsyncMock(side_effect=AssertionError("contender must not execute"))
    evolve = EvolveStepHandler(evolutionary_loop=loop, event_store=store)
    manager = JobManager(store)
    start = StartEvolveStepHandler(
        evolve_handler=evolve,
        event_store=store,
        job_manager=manager,
    )
    original_observe = lineage_claims.observe
    raced = False

    async def acquire_after_precheck(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal raced
        if kwargs.get("scope") == "evolve-handler" and not raced:
            raced = True
            claim = await lineage_claims.try_acquire(
                store,
                scope="evolve-handler",
                lineage_id=lineage_id,
                generation_number=1,
                owner_id="race-winner",
                request_key="race-winner-request",
            )
            assert claim is not None and claim.acquired
            return None
        return await original_observe(*args, **kwargs)

    monkeypatch.setattr(lineage_claims, "observe", acquire_after_precheck)
    monkeypatch.setattr(evolve_start_claim, "_CLAIM_PREPARATION_TIMEOUT_SECONDS", 0.02)
    try:
        result = await asyncio.wait_for(
            start.handle(
                {
                    "lineage_id": lineage_id,
                    "seed_content": json.dumps(seed.to_dict()),
                    "execute": False,
                }
            ),
            timeout=0.25,
        )

        assert result.is_err
        assert result.error.error_code == "evolve_lineage_busy"
        assert result.error.is_retriable is True
        loop.evolve_step.assert_not_awaited()
        observed = await original_observe(
            store,
            scope="evolve-handler",
            lineage_id=lineage_id,
        )
        assert observed is not None and observed.owner_id == "race-winner"
        assert manager._reserved_job_ids == set()
        assert await store.query_events(event_type="mcp.job.created") == []
    finally:
        await lineage_claims.release(
            store,
            scope="evolve-handler",
            lineage_id=lineage_id,
            owner_id="race-winner",
        )
        await manager.drain(grace_seconds=0.1)
        await store.close()


@pytest.mark.asyncio
async def test_start_evolve_preacceptance_cancellation_aborts_prepared_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller cancellation before enqueue ownership releases the held claim."""
    store = await create_event_store()
    lineage_id = "lin_start_cancel_before_acceptance"
    manager = JobManager(store)
    claim_prepared = asyncio.Event()
    claim_cancelled = asyncio.Event()
    release_claim_cleanup = asyncio.Event()
    enqueue_started = asyncio.Event()

    class ControlledEvolveHandler:
        evolutionary_loop = object()

        async def handle(self, _arguments, *, on_generation_claimed=None):  # type: ignore[no-untyped-def]
            assert on_generation_claimed is not None
            try:
                claim_prepared.set()
                await on_generation_claimed(7)
                raise AssertionError("unaccepted prepared claim must not run")
            except asyncio.CancelledError:
                claim_cancelled.set()
                await release_claim_cleanup.wait()
                raise

    async def block_before_acceptance(**_kwargs):  # type: ignore[no-untyped-def]
        enqueue_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(manager, "start_job", block_before_acceptance)
    start = StartEvolveStepHandler(
        evolve_handler=ControlledEvolveHandler(),  # type: ignore[arg-type]
        event_store=store,
        job_manager=manager,
        agent_runtime_backend="codex",
    )
    pending = asyncio.create_task(start.handle({"lineage_id": lineage_id, "execute": False}))
    try:
        await asyncio.wait_for(claim_prepared.wait(), timeout=1.0)
        await asyncio.wait_for(enqueue_started.wait(), timeout=1.0)
        pending.cancel()
        await asyncio.wait_for(claim_cancelled.wait(), timeout=1.0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
        release_claim_cleanup.set()

        with pytest.raises(asyncio.CancelledError):
            await pending

        assert manager._started_job_ids == set()
        assert manager._tasks == {}
        assert manager._runner_tasks == {}
        assert await store.query_events(event_type="mcp.job.created") == []
    finally:
        release_claim_cleanup.set()
        if not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        await manager.drain(grace_seconds=0.1)
        await store.close()


@pytest.mark.asyncio
async def test_start_evolve_postacceptance_cancellation_preserves_claim_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once accepted, caller cancellation cannot abort the runner-owned claim."""
    store = await create_event_store()
    lineage_id = "lin_start_cancel_after_acceptance"
    expected_execution_id = f"evolve:{lineage_id}:generation:7"
    manager = JobManager(store)
    accepted = asyncio.Event()
    work_started = asyncio.Event()
    release_work = asyncio.Event()
    claim_cancelled = asyncio.Event()
    original_start_job = manager.start_job

    class ControlledEvolveHandler:
        evolutionary_loop = object()

        async def handle(self, _arguments, *, on_generation_claimed=None):  # type: ignore[no-untyped-def]
            assert on_generation_claimed is not None
            try:
                await on_generation_claimed(7)
                work_started.set()
                await release_work.wait()
                return Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT,
                                text="generation completed",
                            ),
                        ),
                        is_error=False,
                        meta={"generation_number": 7},
                    )
                )
            except asyncio.CancelledError:
                claim_cancelled.set()
                raise

    async def block_after_acceptance(**kwargs):  # type: ignore[no-untyped-def]
        await original_start_job(**kwargs)
        accepted.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(manager, "start_job", block_after_acceptance)
    start = StartEvolveStepHandler(
        evolve_handler=ControlledEvolveHandler(),  # type: ignore[arg-type]
        event_store=store,
        job_manager=manager,
        agent_runtime_backend="codex",
    )
    pending = asyncio.create_task(start.handle({"lineage_id": lineage_id, "execute": False}))
    try:
        await asyncio.wait_for(accepted.wait(), timeout=1.0)
        await asyncio.wait_for(work_started.wait(), timeout=1.0)
        job_id = next(iter(manager._started_job_ids))
        job_task = manager._tasks[job_id]
        runner_task = manager._runner_tasks[job_id]

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        await asyncio.sleep(0)
        assert not claim_cancelled.is_set()
        assert not runner_task.cancelled()
        assert not runner_task.done()
        assert not job_task.cancelled()
        snapshot = await asyncio.wait_for(manager.get_snapshot(job_id), timeout=5.0)
        assert snapshot.links.execution_id == expected_execution_id

        job_events, _ = await store.get_events_after("job", job_id, last_row_id=0)
        created = next(event for event in job_events if event.type == "mcp.job.created")
        assert created.data["links"]["execution_id"] == expected_execution_id

        release_work.set()
        await asyncio.wait_for(asyncio.shield(job_task), timeout=1.0)
        completed = await asyncio.wait_for(manager.get_snapshot(job_id), timeout=5.0)
        assert completed.status == JobStatus.COMPLETED
        assert completed.links.execution_id == expected_execution_id
        assert not claim_cancelled.is_set()
    finally:
        release_work.set()
        if not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        await manager.drain(grace_seconds=0.1)
        await store.close()


def create_clean_git_project(path: Path) -> Path:
    """Create one committed project for public benchmark-control tests."""
    path.mkdir()
    commands = (
        ("init",),
        ("config", "user.email", "tests@example.com"),
        ("config", "user.name", "Ouroboros Tests"),
    )
    for command in commands:
        subprocess.run(
            ["git", *command],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        )
    (path / "README.md").write_text("benchmark fixture\n", encoding="utf-8")
    for command in (("add", "README.md"), ("commit", "-m", "test baseline")):
        subprocess.run(
            ["git", *command],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        )
    return path


# -- Test Classes --


class TestEvolutionaryLoopConfig:
    """Test evolutionary loop config compatibility behavior."""

    def test_default_runtime_controls_honor_legacy_generation_timeout_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_runtime_control_env(monkeypatch)
        monkeypatch.setenv("OUROBOROS_GENERATION_TIMEOUT", "43200")

        config = EvolutionaryLoopConfig()

        assert config.runtime_controls.generation_no_progress_timeout_seconds == 43200

    def test_explicit_generation_timeout_overrides_runtime_controls(self) -> None:
        config = EvolutionaryLoopConfig(
            generation_timeout_seconds=123,
            runtime_controls=RuntimeControlsConfig(
                generation_no_progress_timeout_seconds=456,
            ),
        )

        assert config.runtime_controls.generation_no_progress_timeout_seconds == 123

    def test_every_execution_policy_field_changes_core_and_handler_request_identity(self) -> None:
        """Rolling workers with any execution-significant config drift cannot coalesce."""
        from pathlib import Path

        from ouroboros.mcp.tools.evolution_handlers import _evolve_handler_request_key

        base = EvolutionaryLoopConfig(runtime_controls=RuntimeControlsConfig())
        alternatives = {
            "max_generations": 31,
            "convergence_threshold": 0.9,
            "stagnation_window": 4,
            "min_generations": 2,
            "generation_timeout_seconds": 1,
            "runtime_controls": RuntimeControlsConfig(mcp_tool_timeout_seconds=1),
            "enable_oscillation_detection": False,
            "eval_gate_enabled": False,
            "eval_min_score": 0.8,
            "outcome_gate_enabled": False,
            "evaluation_plateau_epsilon": 0.02,
            "scoped_reexecution": False,
            "focused_evolution": False,
        }
        declared_fields = {item.name for item in fields(EvolutionaryLoopConfig)}
        assert set(alternatives) == declared_fields

        base_policy = loop_support.evolution_execution_policy(base)
        assert base_policy is not None
        effective_fields = {
            "benchmark_control",
            "effective_focused_evolution",
            "effective_scoped_reexecution",
        }
        assert set(base_policy) == declared_fields | effective_fields
        core_key = loop_support.evolve_request_key(
            None,
            execute=True,
            parallel=True,
            conductor_directive=None,
            generation_number=2,
            execution_policy=base_policy,
        )
        handler_key = _evolve_handler_request_key(
            {"lineage_id": "lin_policy"},
            configured_project_dir=None,
            fallback_cwd=Path.cwd().resolve(),
            execution_policy=base_policy,
        )
        control_policy = loop_support.evolution_execution_policy(base, True)
        assert control_policy is not None
        assert control_policy["benchmark_control"] is True
        assert control_policy["effective_focused_evolution"] is False
        assert control_policy["effective_scoped_reexecution"] is False
        assert control_policy != base_policy
        assert (
            loop_support.evolve_request_key(
                None,
                execute=True,
                parallel=True,
                conductor_directive=None,
                generation_number=2,
                execution_policy=control_policy,
            )
            != core_key
        )
        assert (
            _evolve_handler_request_key(
                {"lineage_id": "lin_policy", "benchmark_control": True},
                configured_project_dir=None,
                fallback_cwd=Path.cwd().resolve(),
                execution_policy=control_policy,
            )
            != handler_key
        )
        assert (
            _evolve_handler_request_key(
                {"lineage_id": "lin_policy", "benchmark_control": False},
                configured_project_dir=None,
                fallback_cwd=Path.cwd().resolve(),
                execution_policy=base_policy,
            )
            == handler_key
        )

        for field_name, alternative in alternatives.items():
            changed_policy = loop_support.evolution_execution_policy(
                replace(base, **{field_name: alternative})
            )
            assert changed_policy is not None
            assert changed_policy != base_policy, field_name
            assert (
                loop_support.evolve_request_key(
                    None,
                    execute=True,
                    parallel=True,
                    conductor_directive=None,
                    generation_number=2,
                    execution_policy=changed_policy,
                )
                != core_key
            ), field_name
            assert (
                _evolve_handler_request_key(
                    {"lineage_id": "lin_policy"},
                    configured_project_dir=None,
                    fallback_cwd=Path.cwd().resolve(),
                    execution_policy=changed_policy,
                )
                != handler_key
            ), field_name

        declared_runtime_fields = set(RuntimeControlsConfig.model_fields)
        assert set(base_policy["runtime_controls"]) == declared_runtime_fields
        for field_name in declared_runtime_fields:
            runtime_values = base.runtime_controls.model_dump()
            runtime_values[field_name] = float(runtime_values[field_name]) + 1.0
            changed_policy = loop_support.evolution_execution_policy(
                replace(
                    base,
                    runtime_controls=RuntimeControlsConfig.model_validate(runtime_values),
                )
            )
            assert changed_policy is not None
            assert (
                loop_support.evolve_request_key(
                    None,
                    execute=True,
                    parallel=True,
                    conductor_directive=None,
                    generation_number=2,
                    execution_policy=changed_policy,
                )
                != core_key
            ), f"runtime_controls.{field_name}"
            assert (
                _evolve_handler_request_key(
                    {"lineage_id": "lin_policy"},
                    configured_project_dir=None,
                    fallback_cwd=Path.cwd().resolve(),
                    execution_policy=changed_policy,
                )
                != handler_key
            ), f"runtime_controls.{field_name}"

    def test_explicit_runtime_controls_are_preserved(self) -> None:
        controls = RuntimeControlsConfig(
            generation_idle_timeout_seconds=77,
            generation_no_progress_timeout_seconds=88,
            watchdog_poll_seconds=2,
        )

        config = EvolutionaryLoopConfig(runtime_controls=controls)

        assert config.runtime_controls == controls

    @pytest.mark.asyncio
    async def test_benchmark_execution_policy_is_task_local(self) -> None:
        config = EvolutionaryLoopConfig(focused_evolution=True, scoped_reexecution=True)
        barrier = asyncio.Barrier(2)

        async def observe(benchmark_control: bool) -> tuple[bool, bool, bool]:
            with loop_support.evolution_execution_policy_context(config, benchmark_control):
                await barrier.wait()
                policy = loop_support.current_evolution_execution_policy(config)
                return (
                    policy.benchmark_control,
                    policy.focused_evolution,
                    policy.scoped_reexecution,
                )

        normal, control = await asyncio.gather(observe(False), observe(True))

        assert normal == (False, True, True)
        assert control == (True, False, False)
        assert config.focused_evolution is True
        assert config.scoped_reexecution is True

    def test_explicit_absolute_project_shadows_config_and_cwd_in_handler_identity(
        self,
        tmp_path: Path,
    ) -> None:
        """Only the effective workspace participates in the durable request key."""
        from ouroboros.mcp.tools.evolution_handlers import _evolve_handler_request_key

        explicit = (tmp_path / "explicit").resolve()
        arguments = {
            "lineage_id": "lin_explicit_workspace_identity",
            "project_dir": str(explicit),
        }

        first = _evolve_handler_request_key(
            arguments,
            configured_project_dir=str(tmp_path / "configured-a"),
            fallback_cwd=(tmp_path / "cwd-a").resolve(),
        )
        second = _evolve_handler_request_key(
            arguments,
            configured_project_dir=str(tmp_path / "configured-b"),
            fallback_cwd=(tmp_path / "cwd-b").resolve(),
        )

        assert first == second

    def test_configured_project_shadows_cwd_in_handler_identity(
        self,
        tmp_path: Path,
    ) -> None:
        """Configured workspace identity matches the handler's fallback target."""
        from ouroboros.mcp.tools.evolution_handlers import _evolve_handler_request_key

        configured = (tmp_path / "configured").resolve()
        arguments = {"lineage_id": "lin_configured_workspace_identity"}

        first = _evolve_handler_request_key(
            arguments,
            configured_project_dir=str(configured),
            fallback_cwd=(tmp_path / "cwd-a").resolve(),
        )
        second = _evolve_handler_request_key(
            arguments,
            configured_project_dir=str(configured),
            fallback_cwd=(tmp_path / "cwd-b").resolve(),
        )
        different = _evolve_handler_request_key(
            arguments,
            configured_project_dir=str((tmp_path / "configured-other").resolve()),
            fallback_cwd=(tmp_path / "cwd-a").resolve(),
        )

        assert first == second
        assert first != different

    def test_non_introspectable_executor_preserves_legacy_call_shape(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = object()

        def raise_for_executor(callable_obj: object) -> inspect.Signature:
            if callable_obj is executor:
                raise ValueError("no signature")
            return inspect.Signature()

        monkeypatch.setattr("ouroboros.evolution.loop.inspect.signature", raise_for_executor)

        assert EvolutionaryLoop._callable_accepts_keyword(executor, "execution_id") is False


class TestStepTypes:
    """Test StepAction and StepResult types."""

    def test_step_action_values(self) -> None:
        """StepAction has all expected values."""
        assert StepAction.CONTINUE == "continue"
        assert StepAction.CONVERGED == "converged"
        assert StepAction.ONTOLOGY_STABLE == "ontology_stable"
        assert StepAction.STAGNATED == "stagnated"
        assert StepAction.EXHAUSTED == "exhausted"
        assert StepAction.FAILED == "failed"
        assert StepAction.INTERRUPTED == "interrupted"
        assert len(StepAction) == 7

    def test_step_result_is_frozen(self) -> None:
        """StepResult is frozen dataclass."""
        seed = make_seed()
        gen_result = GenerationResult(
            generation_number=1,
            seed=seed,
        )
        signal = ConvergenceSignal(
            converged=False,
            reason="Test",
            ontology_similarity=0.5,
            generation=1,
        )
        lineage = OntologyLineage(lineage_id="test", goal="test")
        step = StepResult(
            generation_result=gen_result,
            convergence_signal=signal,
            lineage=lineage,
            action=StepAction.CONTINUE,
            next_generation=2,
        )
        assert step.action == StepAction.CONTINUE
        assert step.next_generation == 2

        with pytest.raises(AttributeError):
            step.action = StepAction.CONVERGED  # type: ignore[misc]


class TestEvolveStepGen1:
    """Test evolve_step for Gen 1 (new lineage)."""

    @pytest.mark.asyncio
    async def test_gen1_creates_lineage(self) -> None:
        """Gen 1 with initial_seed creates lineage and runs."""
        store = await create_event_store()
        seed = make_seed()

        gen_result = GenerationResult(
            generation_number=1,
            seed=seed,
            evaluation_summary=make_eval_summary(),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop = make_loop(store, gen_result=gen_result)

        result = await loop.evolve_step("lin_test_001", initial_seed=seed)

        assert result.is_ok
        step = result.value
        assert step.action == StepAction.CONVERGED
        assert step.generation_result.generation_number == 1
        assert step.lineage.lineage_id == "lin_test_001"
        assert step.lineage.current_generation == 1
        assert step.next_generation == 2

    @pytest.mark.asyncio
    async def test_gen1_emits_events(self) -> None:
        """Gen 1 emits lineage_created and lineage_generation_completed with seed_json."""
        store = await create_event_store()
        seed = make_seed()

        gen_result = GenerationResult(
            generation_number=1,
            seed=seed,
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop = make_loop(store, gen_result=gen_result)

        await loop.evolve_step("lin_test_events", initial_seed=seed)

        events = await store.replay_lineage("lin_test_events")
        event_types = [e.type for e in events]
        assert "lineage.created" in event_types
        assert "lineage.generation.completed" in event_types

        # Verify seed_json is in the completed event
        completed = [e for e in events if e.type == "lineage.generation.completed"][0]
        assert "seed_json" in completed.data
        assert completed.data["seed_json"] is not None

        # Verify round-trip
        reconstructed = Seed.from_dict(json.loads(completed.data["seed_json"]))
        assert reconstructed.goal == seed.goal
        assert reconstructed.metadata.seed_id == seed.metadata.seed_id

    @pytest.mark.asyncio
    async def test_gen1_executor_receives_deterministic_execution_id(self) -> None:
        """Evolve execution events are scoped so the watchdog can monitor them."""
        store = await create_event_store()
        seed = make_seed()
        observed: dict[str, object] = {}

        async def executor(
            received_seed: Seed,
            *,
            parallel: bool = True,
            execution_id: str | None = None,
        ) -> str:
            observed["seed"] = received_seed
            observed["parallel"] = parallel
            observed["execution_id"] = execution_id
            return "Execution complete"

        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(
                min_generations=1,
                runtime_controls=RuntimeControlsConfig(
                    generation_idle_timeout_seconds=0,
                    generation_no_progress_timeout_seconds=0,
                    watchdog_poll_seconds=0.01,
                ),
            ),
            executor=executor,
        )

        result = await loop.evolve_step(
            lineage_id="lin_exec_scope",
            initial_seed=seed,
            execute=True,
            parallel=False,
        )

        assert result.is_ok
        assert observed["seed"] == seed
        assert observed["parallel"] is False
        assert observed["execution_id"] == "evolve:lin_exec_scope:generation:1"

    @pytest.mark.asyncio
    async def test_gen1_records_depth_warning_as_seed_quality_canary_feedback(self) -> None:
        """Evaluate-stage depth warnings should reach loop canary state unchanged."""
        store = await create_event_store()
        seed = make_seed()
        expected_warning = FeedbackMetadata(
            code="decomposition_depth_warning",
            severity="warning",
            message="Depth safety net forced atomic execution.",
            source="parallel_executor",
            details={"max_depth": 3, "affected_count": 2},
        )
        artifact = """
Parallel Execution Verification Report
Success: 1/1

## Feedback Metadata
Feedback Metadata JSON: {"feedback_metadata": [{"code": "decomposition_depth_warning", "details": {"affected_count": 2, "max_depth": 3}, "message": "Depth safety net forced atomic execution.", "severity": "warning", "source": "parallel_executor"}]}

## AC Results
### AC 1: [PASS] Tasks can be created
""".strip()

        async def fake_executor(*_args, **_kwargs):
            return Result.ok(
                SimpleNamespace(
                    summary={"verification_report": artifact},
                    final_message="unused",
                    duration_seconds=0.01,
                    messages_processed=1,
                    success=True,
                )
            )

        async def fake_evaluator(_seed: Seed, execution_output: str | None) -> EvaluationSummary:
            assert execution_output == artifact
            return EvaluationSummary(
                final_approved=True,
                highest_stage_passed=3,
                score=1.0,
                feedback_metadata=_extract_feedback_metadata_from_artifact(execution_output or ""),
            )

        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(
                max_generations=30,
                convergence_threshold=0.95,
                stagnation_window=3,
                min_generations=2,
            ),
            executor=fake_executor,
            evaluator=fake_evaluator,
        )

        result = await loop.evolve_step("lin_test_canary", initial_seed=seed)

        assert result.is_ok
        step = result.value
        assert step.generation_result.evaluation_summary is not None
        assert step.generation_result.evaluation_summary.feedback_metadata == (expected_warning,)
        assert step.lineage.generations[0].seed_quality_canary_feedback == (expected_warning,)

        events = await store.replay_lineage("lin_test_canary")
        completed = [e for e in events if e.type == "lineage.generation.completed"][0]
        assert completed.data["seed_quality_canary_feedback"] == [
            expected_warning.model_dump(mode="json")
        ]

    @pytest.mark.asyncio
    async def test_gen1_omits_seed_quality_canary_feedback_without_depth_warning(self) -> None:
        """Loop canary state stays empty when evaluation emits no depth warning."""
        store = await create_event_store()
        seed = make_seed()
        artifact = """
Parallel Execution Verification Report
Success: 1/1

## AC Results
### AC 1: [PASS] Tasks can be created
""".strip()

        async def fake_executor(*_args, **_kwargs):
            return Result.ok(
                SimpleNamespace(
                    summary={"verification_report": artifact},
                    final_message="unused",
                    duration_seconds=0.01,
                    messages_processed=1,
                    success=True,
                )
            )

        async def fake_evaluator(_seed: Seed, execution_output: str | None) -> EvaluationSummary:
            assert execution_output == artifact
            return EvaluationSummary(
                final_approved=True,
                highest_stage_passed=3,
                score=1.0,
                feedback_metadata=_extract_feedback_metadata_from_artifact(execution_output or ""),
            )

        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(
                max_generations=30,
                convergence_threshold=0.95,
                stagnation_window=3,
                min_generations=2,
            ),
            executor=fake_executor,
            evaluator=fake_evaluator,
        )

        result = await loop.evolve_step("lin_test_no_canary", initial_seed=seed)

        assert result.is_ok
        step = result.value
        assert step.generation_result.evaluation_summary is not None
        assert step.generation_result.evaluation_summary.feedback_metadata == ()
        assert step.lineage.generations[0].seed_quality_canary_feedback == ()

        events = await store.replay_lineage("lin_test_no_canary")
        completed = [e for e in events if e.type == "lineage.generation.completed"][0]
        assert "seed_quality_canary_feedback" not in completed.data


class TestEvolveStepGen2:
    """Test evolve_step for Gen 2+ (reconstructed from events)."""

    @pytest.mark.asyncio
    async def test_gen2_reconstructs_from_events(self) -> None:
        """Gen 2+ reconstructs seed from events and runs next generation."""
        store = await create_event_store()
        seed_v1 = make_seed(seed_id="seed_v1")
        eval_summary = make_eval_summary()

        # Seed Gen 1 events
        await seed_events_for_gen1(store, "lin_gen2_test", seed_v1, eval_summary)

        # Gen 2 result with evolved seed
        seed_v2 = make_seed(
            seed_id="seed_v2",
            parent_seed_id="seed_v1",
            fields=(
                OntologyField(name="tasks", field_type="array", description="Tasks", required=True),
                OntologyField(
                    name="projects", field_type="array", description="Projects", required=True
                ),
            ),
        )
        gen_result = GenerationResult(
            generation_number=2,
            seed=seed_v2,
            evaluation_summary=make_eval_summary(),
            wonder_output=make_wonder_output(),
            ontology_delta=OntologyDelta(
                added_fields=(
                    OntologyField(
                        name="projects", field_type="array", description="Projects", required=True
                    ),
                ),
                removed_fields=(),
                modified_fields=(),
                similarity=0.7,
            ),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop = make_loop(store, gen_result=gen_result)

        result = await loop.evolve_step("lin_gen2_test")

        assert result.is_ok
        step = result.value
        assert step.generation_result.generation_number == 2
        assert step.next_generation == 3

        # Verify _run_generation was called with reconstructed seed
        loop._run_generation.assert_called_once()
        call_args = loop._run_generation.call_args
        passed_seed = call_args.kwargs.get("current_seed") or call_args[0][2]
        assert passed_seed.metadata.seed_id == "seed_v1"

    @pytest.mark.asyncio
    async def test_ontology_only_generations_keep_reflecting_without_evaluation(self) -> None:
        """No-execute exploration must keep mutating after evaluation disappears."""
        store = await create_event_store()
        seed_v1 = make_seed(
            seed_id="seed_ontology_1",
            fields=(OntologyField(name="task", field_type="entity", description="task"),),
        )
        seed_v2 = make_seed(
            seed_id="seed_ontology_2",
            parent_seed_id="seed_ontology_1",
            fields=(OntologyField(name="owner", field_type="entity", description="owner"),),
        )
        seed_v3 = make_seed(
            seed_id="seed_ontology_3",
            parent_seed_id="seed_ontology_2",
            fields=(OntologyField(name="policy", field_type="entity", description="policy"),),
        )
        await seed_events_for_gen1(store, "lin_ontology_reflect", seed_v1)

        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock(
            side_effect=(
                Result.ok(make_wonder_output(("Who owns each task?",))),
                Result.ok(make_wonder_output(("Which policy governs the owner?",))),
            )
        )
        reflect_engine = MagicMock()
        reflect_engine.reflect = AsyncMock(
            side_effect=(
                Result.ok(
                    ReflectOutput(
                        refined_goal=seed_v1.goal,
                        refined_constraints=seed_v1.constraints,
                        refined_acs=("Tasks can be created",),
                        reasoning="add owner",
                    )
                ),
                Result.ok(
                    ReflectOutput(
                        refined_goal=seed_v2.goal,
                        refined_constraints=seed_v2.constraints,
                        refined_acs=("Tasks can be created",),
                        reasoning="add policy",
                    )
                ),
            )
        )
        seed_generator = MagicMock()
        seed_generator.generate_from_reflect = MagicMock(
            side_effect=(Result.ok(seed_v2), Result.ok(seed_v3))
        )
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(min_generations=2, convergence_threshold=0.99),
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            seed_generator=seed_generator,
        )

        generation_2 = await loop.evolve_step("lin_ontology_reflect", execute=False)
        generation_3 = await loop.evolve_step("lin_ontology_reflect", execute=False)

        assert generation_2.is_ok and generation_3.is_ok
        assert generation_2.value.action is StepAction.CONTINUE
        assert generation_3.value.action is StepAction.CONTINUE
        assert generation_2.value.generation_result.seed.metadata.seed_id == "seed_ontology_2"
        assert generation_3.value.generation_result.seed.metadata.seed_id == "seed_ontology_3"
        assert reflect_engine.reflect.await_count == 2
        assert all(
            call.kwargs["evaluation_summary"] is None
            for call in reflect_engine.reflect.await_args_list
        )
        assert seed_generator.generate_from_reflect.call_count == 2

    @pytest.mark.asyncio
    async def test_ontology_stable_handoff_verifies_current_seed_without_wonder(self) -> None:
        """The durable EVALUATE handoff must force Execute -> Evaluate on the stable Seed."""
        store = await create_event_store()
        seed_v1 = make_seed(
            seed_id="seed_handoff_1",
            fields=(OntologyField(name="task", field_type="entity", description="task"),),
        )
        seed_v2 = make_seed(
            seed_id="seed_handoff_2",
            parent_seed_id="seed_handoff_1",
            fields=(OntologyField(name="owner", field_type="entity", description="owner"),),
        )
        await seed_events_for_gen1(store, "lin_verification_handoff", seed_v1)
        await store.append(
            lineage_generation_completed(
                "lin_verification_handoff",
                generation_number=2,
                seed_id=seed_v2.metadata.seed_id,
                ontology_snapshot=seed_v2.ontology_schema.model_dump(mode="json"),
                evaluation_summary=None,
                wonder_questions=["ownership clarified"],
                seed_json=json.dumps(seed_v2.to_dict()),
                parent_seed_id=seed_v2.metadata.parent_seed_id,
            )
        )

        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock(
            return_value=Result.ok(make_wonder_output(questions=(), should_continue=False))
        )
        reflect_engine = MagicMock()
        reflect_engine.reflect = AsyncMock()
        seed_generator = MagicMock()
        seed_generator.generate_from_reflect = MagicMock()
        executor = AsyncMock(return_value="verified output")
        evaluator = AsyncMock(return_value=make_eval_summary(approved=True, score=1.0))
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(min_generations=2, convergence_threshold=0.95),
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            seed_generator=seed_generator,
            executor=executor,
            evaluator=evaluator,
        )

        stable = await loop.evolve_step("lin_verification_handoff", execute=False)

        assert stable.is_ok
        assert stable.value.action is StepAction.ONTOLOGY_STABLE
        assert stable.value.convergence_signal.converged is False
        assert stable.value.lineage.status is LineageStatus.ACTIVE
        wonder_engine.wonder.reset_mock()
        reflect_engine.reflect.reset_mock()
        seed_generator.generate_from_reflect.reset_mock()

        verified = await loop.evolve_step("lin_verification_handoff", execute=True)

        assert verified.is_ok
        assert verified.value.action is StepAction.CONVERGED
        assert verified.value.generation_result.seed == seed_v2
        assert verified.value.generation_result.active_ac_indices == (0,)
        assert verified.value.generation_result.frozen_ac_indices == ()
        wonder_engine.wonder.assert_not_awaited()
        reflect_engine.reflect.assert_not_awaited()
        seed_generator.generate_from_reflect.assert_not_called()
        executor.assert_awaited_once()
        assert executor.await_args.args[0] == seed_v2
        evaluator.assert_awaited_once_with(seed_v2, "verified output")

    @pytest.mark.asyncio
    async def test_hard_crash_after_reflect_without_provenance_fails_closed(self) -> None:
        """A truncated pre-Seed checkpoint cannot trigger provider replay."""
        store = await create_event_store()
        parent = make_seed(seed_id="seed_truncated_reflect_parent")
        lineage_id = "lin_truncated_reflect_checkpoint"
        await seed_events_for_gen1(store, lineage_id, parent, make_eval_summary(False))
        await store.append(
            lineage_generation_started(
                lineage_id,
                2,
                GenerationPhase.WONDERING.value,
                parent.metadata.seed_id,
                json.dumps(parent.to_dict()),
            )
        )
        await store.append(
            lineage_generation_phase_changed(
                lineage_id,
                2,
                GenerationPhase.SEEDING.value,
                last_completed_phase=GenerationPhase.REFLECTING.value,
                seed_id=parent.metadata.seed_id,
                seed_json=json.dumps(parent.to_dict()),
                active_ac_indices=[0],
                frozen_ac_indices=[],
                partial_state={
                    "focus_checkpointed": True,
                    "wonder_output_complete": True,
                    "wonder_output": make_wonder_output().model_dump(mode="json"),
                    "reflect_output_complete": True,
                    "reflect_output": {
                        "refined_goal": parent.goal,
                        "refined_constraints": list(parent.constraints),
                        "refined_acs": ["Tasks can be created"],
                        "ac_patches": [{"op": "keep", "index": 0}],
                    },
                    # reflect_patch_identity_explicit was truncated.
                },
            )
        )
        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock()
        reflect_engine = MagicMock()
        reflect_engine.reflect = AsyncMock()
        loop = EvolutionaryLoop(
            event_store=store,
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            seed_generator=SeedGenerator(llm_adapter=AsyncMock()),
        )

        result = await loop.evolve_step(lineage_id, execute=False)

        assert result.is_err
        assert "patch provenance" in str(result.error)
        wonder_engine.wonder.assert_not_awaited()
        reflect_engine.reflect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ontology_stable_handoff_survives_directive_append_failure(self) -> None:
        """Completed generation atomically retains the verification handoff authority."""
        store = await create_event_store()
        seed_v1 = make_seed(
            seed_id="seed_atomic_handoff_1",
            fields=(OntologyField(name="task", field_type="entity", description="task"),),
        )
        seed_v2 = make_seed(
            seed_id="seed_atomic_handoff_2",
            parent_seed_id=seed_v1.metadata.seed_id,
            fields=(OntologyField(name="owner", field_type="entity", description="owner"),),
        )
        await seed_events_for_gen1(store, "lin_atomic_handoff", seed_v1)
        await store.append(
            lineage_generation_completed(
                "lin_atomic_handoff",
                generation_number=2,
                seed_id=seed_v2.metadata.seed_id,
                ontology_snapshot=seed_v2.ontology_schema.model_dump(mode="json"),
                evaluation_summary=None,
                wonder_questions=["ownership clarified"],
                seed_json=json.dumps(seed_v2.to_dict()),
                parent_seed_id=seed_v2.metadata.parent_seed_id,
            )
        )

        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock(
            return_value=Result.ok(make_wonder_output(questions=(), should_continue=False))
        )
        reflect_engine = MagicMock()
        reflect_engine.reflect = AsyncMock()
        executor = AsyncMock(return_value="verified output")
        evaluator = AsyncMock(return_value=make_eval_summary(approved=True, score=1.0))
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(min_generations=2, convergence_threshold=0.95),
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            executor=executor,
            evaluator=evaluator,
        )
        original_append = store.append

        async def fail_ontology_stable_directive(event):  # type: ignore[no-untyped-def]
            if (
                event.type == "control.directive.emitted"
                and event.data.get("extra", {}).get("step_action") == "ontology_stable"
            ):
                raise OSError("injected directive append failure")
            return await original_append(event)

        with patch.object(store, "append", side_effect=fail_ontology_stable_directive):
            first = await loop.evolve_step("lin_atomic_handoff", execute=False)

        assert first.is_ok
        assert first.value.action is StepAction.ONTOLOGY_STABLE
        events = await store.replay_lineage("lin_atomic_handoff")
        completed = [event for event in events if event.type == "lineage.generation.completed"][-1]
        assert completed.data["verification_handoff_pending"] is True
        replayed = LineageProjector().project(events)
        assert replayed is not None
        assert replayed.verification_handoff_pending is True

        wonder_engine.wonder.reset_mock()
        reflect_engine.reflect.reset_mock()
        repeated = await loop.evolve_step("lin_atomic_handoff", execute=False)

        assert repeated.is_ok
        assert repeated.value.action is StepAction.ONTOLOGY_STABLE
        assert repeated.value.generation_result.seed == seed_v2
        wonder_engine.wonder.assert_not_awaited()
        reflect_engine.reflect.assert_not_awaited()

        override = make_seed(seed_id="seed_must_not_replace_pending_handoff")
        repeated_with_override = await loop.evolve_step(
            "lin_atomic_handoff",
            initial_seed=override,
            execute=False,
        )

        assert repeated_with_override.is_ok
        assert repeated_with_override.value.generation_result.seed == seed_v2
        verified = await loop.evolve_step(
            "lin_atomic_handoff",
            initial_seed=override,
            execute=True,
        )

        assert verified.is_ok
        assert verified.value.generation_result.seed == seed_v2
        wonder_engine.wonder.assert_not_awaited()
        reflect_engine.reflect.assert_not_awaited()
        executor.assert_awaited_once()
        evaluator.assert_awaited_once_with(seed_v2, "verified output")

    @pytest.mark.asyncio
    async def test_concurrent_same_lineage_replays_one_ontology_stable_winner(self) -> None:
        """Overlapping retries must share one provider path and durable generation."""
        store = await create_event_store()
        lineage_id = "lin_concurrent_evolve"
        seed_v1 = make_seed(
            seed_id="seed_concurrent_1",
            fields=(OntologyField(name="task", field_type="entity", description="task"),),
        )
        seed_v2 = make_seed(
            seed_id="seed_concurrent_2",
            parent_seed_id=seed_v1.metadata.seed_id,
            fields=(OntologyField(name="owner", field_type="entity", description="owner"),),
        )
        seed_v3 = make_seed(
            seed_id="seed_concurrent_3",
            parent_seed_id=seed_v2.metadata.seed_id,
            fields=seed_v2.ontology_schema.fields,
        )
        await seed_events_for_gen1(store, lineage_id, seed_v1)
        await store.append(
            lineage_generation_completed(
                lineage_id,
                generation_number=2,
                seed_id=seed_v2.metadata.seed_id,
                ontology_snapshot=seed_v2.ontology_schema.model_dump(mode="json"),
                evaluation_summary=None,
                wonder_questions=["ownership clarified"],
                seed_json=json.dumps(seed_v2.to_dict()),
                parent_seed_id=seed_v2.metadata.parent_seed_id,
            )
        )

        wonder_entered = asyncio.Event()
        release_wonder = asyncio.Event()

        async def stable_wonder(*args, **kwargs):  # type: ignore[no-untyped-def]
            wonder_entered.set()
            await release_wonder.wait()
            return Result.ok(make_wonder_output(("What remains unresolved?",)))

        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock(side_effect=stable_wonder)
        reflect_engine = MagicMock()
        reflect_engine.reflect = AsyncMock(
            return_value=Result.ok(
                ReflectOutput(
                    refined_goal=seed_v2.goal,
                    refined_constraints=seed_v2.constraints,
                    refined_acs=("Tasks can be created",),
                    reasoning="stable ontology",
                )
            )
        )
        seed_generator = MagicMock()
        seed_generator.generate_from_reflect = MagicMock(return_value=Result.ok(seed_v3))
        config = EvolutionaryLoopConfig(min_generations=2, convergence_threshold=0.95)
        first_loop = EvolutionaryLoop(
            event_store=store,
            config=config,
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            seed_generator=seed_generator,
        )
        retry_loop = EvolutionaryLoop(
            event_store=store,
            config=config,
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            seed_generator=seed_generator,
        )

        first_task = asyncio.create_task(first_loop.evolve_step(lineage_id, execute=False))
        await wonder_entered.wait()
        retry_task = asyncio.create_task(retry_loop.evolve_step(lineage_id, execute=False))
        await asyncio.sleep(0)
        assert not retry_task.done()
        release_wonder.set()
        first, retry = await asyncio.gather(first_task, retry_task)

        assert first.is_ok and retry.is_ok
        assert first is retry
        assert first.value.action is StepAction.ONTOLOGY_STABLE
        assert retry.value.action is StepAction.ONTOLOGY_STABLE
        assert first.value.generation_result.generation_number == 3
        assert retry.value.generation_result.generation_number == 3
        assert first.value.generation_result.seed == retry.value.generation_result.seed
        assert first.value.next_generation == retry.value.next_generation == 4
        wonder_engine.wonder.assert_awaited_once()
        reflect_engine.reflect.assert_awaited_once()
        seed_generator.generate_from_reflect.assert_called_once()

        events = await store.replay_lineage(lineage_id)
        generation_started = [
            event
            for event in events
            if event.type == "lineage.generation.started"
            and event.data.get("generation_number") == 3
        ]
        generation_completed = [
            event
            for event in events
            if event.type == "lineage.generation.completed"
            and event.data.get("generation_number") == 3
        ]
        stable_directives = [
            event
            for event in events
            if event.type == "control.directive.emitted"
            and event.data.get("generation_number") == 3
            and event.data.get("extra", {}).get("step_action") == "ontology_stable"
        ]
        assert len(generation_started) == 1
        assert len(generation_completed) == 1
        assert len(stable_directives) == 1

    @pytest.mark.asyncio
    async def test_different_single_flight_request_waits_before_retrying(self) -> None:
        """A changed evolve request starts only after the winner is durable."""
        store = await create_event_store()
        winner_entered = asyncio.Event()
        release_winner = asyncio.Event()
        order: list[str] = []

        async def winner() -> str:
            order.append("winner_started")
            winner_entered.set()
            await release_winner.wait()
            order.append("winner_completed")
            return "winner"

        async def retry() -> str:
            order.append("retry_started")
            return "retry"

        first = asyncio.create_task(
            loop_support.run_lineage_single_flight(store, "lineage", "request-a", winner)
        )
        await winner_entered.wait()
        second = asyncio.create_task(
            loop_support.run_lineage_single_flight(store, "lineage", "request-b", retry)
        )
        await asyncio.sleep(0)

        assert order == ["winner_started"]
        release_winner.set()
        assert await asyncio.gather(first, second) == ["winner", "retry"]
        assert order == ["winner_started", "winner_completed", "retry_started"]

    @pytest.mark.asyncio
    async def test_cancelled_duplicate_does_not_cancel_single_flight_winner(self) -> None:
        """One retry caller cannot abort provider work shared by another caller."""
        store = await create_event_store()
        winner_entered = asyncio.Event()
        release_winner = asyncio.Event()

        async def winner() -> str:
            winner_entered.set()
            await release_winner.wait()
            return "durable"

        first = asyncio.create_task(
            loop_support.run_lineage_single_flight(store, "lineage", "same-request", winner)
        )
        await winner_entered.wait()
        duplicate = asyncio.create_task(
            loop_support.run_lineage_single_flight(store, "lineage", "same-request", winner)
        )
        await asyncio.sleep(0)
        duplicate.cancel()

        with pytest.raises(asyncio.CancelledError):
            await duplicate
        release_winner.set()
        assert await first == "durable"

    @pytest.mark.asyncio
    async def test_failed_generation_same_request_retries_without_second_started_event(
        self,
    ) -> None:
        """A transient failure deduplicates overlap but does not pin the lineage forever."""
        store = await create_event_store()
        seed = make_seed(seed_id="seed_retryable_failure")
        executor = AsyncMock(side_effect=[RuntimeError("transient"), "recovered output"])
        evaluator = AsyncMock(return_value=make_eval_summary(approved=True, score=1.0))
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(min_generations=1),
            executor=executor,
            evaluator=evaluator,
        )

        failed = await loop.evolve_step("lin_retryable_failure", initial_seed=seed)
        recovered = await loop.evolve_step("lin_retryable_failure", initial_seed=seed)

        assert failed.is_ok and failed.value.action is StepAction.FAILED
        assert recovered.is_ok and recovered.value.action is StepAction.CONVERGED
        assert executor.await_count == 2
        events = await store.replay_lineage("lin_retryable_failure")
        started = [
            event
            for event in events
            if event.type == "lineage.generation.started"
            and event.data.get("generation_number") == 1
        ]
        completed = [
            event
            for event in events
            if event.type == "lineage.generation.completed"
            and event.data.get("generation_number") == 1
        ]
        assert len(started) == 1
        assert len(completed) == 1

    @pytest.mark.asyncio
    async def test_no_question_wonder_reverifies_non_authoritative_pass(self) -> None:
        """Wonder may skip Reflect, but execute=True must still verify every unknown AC."""
        store = await create_event_store()
        seed = make_seed(seed_id="seed_unknown_pass")
        unknown_pass = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Tasks can be created",
                    passed=True,
                    ac_verdict_state="not_evaluated",
                ),
            ),
        )
        await seed_events_for_gen1(store, "lin_unknown_pass", seed, unknown_pass)
        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock(
            return_value=Result.ok(make_wonder_output(questions=(), should_continue=False))
        )
        reflect_engine = MagicMock()
        reflect_engine.reflect = AsyncMock()
        executor = AsyncMock(return_value="fresh execution")
        evaluator = AsyncMock(return_value=make_eval_summary(approved=True, score=1.0))
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(min_generations=2),
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            executor=executor,
            evaluator=evaluator,
        )

        result = await loop.evolve_step("lin_unknown_pass", execute=True)

        assert result.is_ok
        assert result.value.generation_result.active_ac_indices == (0,)
        assert result.value.generation_result.frozen_ac_indices == ()
        reflect_engine.reflect.assert_not_awaited()
        executor.assert_awaited_once()
        evaluator.assert_awaited_once_with(seed, "fresh execution")

    @pytest.mark.asyncio
    async def test_resume_preserves_ambiguous_legacy_unstructured_ac_list(self) -> None:
        """Old interrupted Reflect output cannot delete/reorder parent prose ACs on resume."""
        store = await create_event_store()
        seed = make_seed(seed_id="seed_legacy_resume").model_copy(
            update={
                "acceptance_criteria": (
                    "Tasks can be created",
                    "Tasks can be deleted",
                    "Tasks can be listed",
                )
            }
        )
        await seed_events_for_gen1(store, "lin_legacy_resume", seed, make_eval_summary(False))
        await store.append(lineage_generation_started("lin_legacy_resume", 2, "wondering"))
        await store.append(lineage_generation_phase_changed("lin_legacy_resume", 2, "reflecting"))
        await store.append(
            lineage_generation_interrupted(
                "lin_legacy_resume",
                2,
                last_completed_phase="reflecting",
                partial_state={
                    "wonder_questions": ["What remains unresolved?"],
                    "reflect_output": {
                        "refined_goal": seed.goal,
                        "refined_constraints": list(seed.constraints),
                        "refined_acs": ["Tasks can be created"],
                        "ontology_mutations": [],
                        "reasoning": "legacy persisted output",
                    },
                },
                seed_json=json.dumps(seed.to_dict()),
            )
        )
        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock()
        reflect_engine = MagicMock()
        reflect_engine.reflect = AsyncMock()
        loop = EvolutionaryLoop(
            event_store=store,
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            seed_generator=SeedGenerator(llm_adapter=AsyncMock()),
        )

        result = await loop.evolve_step("lin_legacy_resume", execute=False)

        assert result.is_ok
        assert tuple(
            criterion.description
            for criterion in result.value.generation_result.seed.acceptance_criteria
        ) == (
            "Tasks can be created",
            "Tasks can be deleted",
            "Tasks can be listed",
        )
        wonder_engine.wonder.assert_not_awaited()
        reflect_engine.reflect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_discards_unsafe_legacy_structured_ac_rewrite(self) -> None:
        """Resume keeps structured parent authority instead of failing or rebinding it."""
        store = await create_event_store()
        seed = make_seed(seed_id="seed_structured_resume").model_copy(
            update={
                "acceptance_criteria": (
                    AcceptanceCriterionSpec(
                        description="Tasks can be created",
                        verify_command="pytest -q tests/test_create.py",
                    ),
                    AcceptanceCriterionSpec(
                        description="Tasks can be deleted",
                        verify_command="pytest -q tests/test_delete.py",
                    ),
                )
            }
        )
        seed = Seed.from_dict(seed.to_dict())
        await seed_events_for_gen1(store, "lin_structured_resume", seed, make_eval_summary(False))
        await store.append(lineage_generation_started("lin_structured_resume", 2, "wondering"))
        await store.append(
            lineage_generation_phase_changed("lin_structured_resume", 2, "reflecting")
        )
        await store.append(
            lineage_generation_interrupted(
                "lin_structured_resume",
                2,
                last_completed_phase="reflecting",
                partial_state={
                    "wonder_questions": ["What remains unresolved?"],
                    "reflect_output": {
                        "refined_goal": seed.goal,
                        "refined_constraints": list(seed.constraints),
                        "refined_acs": ["Rewrite structured task creation"],
                        "ontology_mutations": [],
                        "reasoning": "legacy persisted output",
                    },
                },
                seed_json=json.dumps(seed.to_dict()),
            )
        )
        loop = EvolutionaryLoop(
            event_store=store,
            seed_generator=SeedGenerator(llm_adapter=AsyncMock()),
        )

        result = await loop.evolve_step("lin_structured_resume", execute=False)

        assert result.is_ok
        assert result.value.generation_result.seed.acceptance_criteria == seed.acceptance_criteria

    @pytest.mark.asyncio
    async def test_resume_reruns_patch_bearing_structured_ac_rewrite(self) -> None:
        """Serialized patches cannot restore process-local parser provenance."""
        store = await create_event_store()
        seed = make_seed(seed_id="seed_patch_resume").model_copy(
            update={
                "acceptance_criteria": (
                    AcceptanceCriterionSpec(
                        description="Tasks can be created",
                        verify_command="pytest -q tests/test_create.py",
                    ),
                    AcceptanceCriterionSpec(
                        description="Tasks can be deleted",
                        verify_command="pytest -q tests/test_delete.py",
                    ),
                )
            }
        )
        seed = Seed.from_dict(seed.to_dict())
        await seed_events_for_gen1(store, "lin_patch_resume", seed, make_eval_summary(False))
        await store.append(lineage_generation_started("lin_patch_resume", 2, "wondering"))
        await store.append(lineage_generation_phase_changed("lin_patch_resume", 2, "reflecting"))
        await store.append(
            lineage_generation_interrupted(
                "lin_patch_resume",
                2,
                last_completed_phase="reflecting",
                partial_state={
                    "wonder_questions": ["What remains unresolved?"],
                    "reflect_output": {
                        "refined_goal": seed.goal,
                        "refined_constraints": list(seed.constraints),
                        "refined_acs": [
                            "Rewrite structured task creation",
                            "Tasks can be deleted",
                        ],
                        "ac_patches": [
                            {
                                "op": "revise",
                                "index": 0,
                                "content": "Rewrite structured task creation",
                            },
                            {"op": "keep", "index": 1},
                        ],
                        "ontology_mutations": [],
                        "reasoning": "pre-upgrade persisted output",
                    },
                },
                seed_json=json.dumps(seed.to_dict()),
            )
        )
        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock()
        reflect_engine = MagicMock()
        reflect_engine.reflect = AsyncMock(
            return_value=Result.ok(
                ReflectOutput(
                    refined_goal=seed.goal,
                    refined_constraints=seed.constraints,
                    refined_acs=tuple(
                        criterion.description for criterion in seed.acceptance_criteria
                    ),
                    ontology_mutations=(),
                    reasoning="safe replay",
                )
            )
        )
        loop = EvolutionaryLoop(
            event_store=store,
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            seed_generator=SeedGenerator(llm_adapter=AsyncMock()),
        )

        result = await loop.evolve_step("lin_patch_resume", execute=False)

        assert result.is_ok
        assert result.value.generation_result.seed.acceptance_criteria == seed.acceptance_criteria
        wonder_engine.wonder.assert_not_awaited()
        reflect_engine.reflect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resume_after_seeding_preserves_focused_parent_authority(self) -> None:
        """A resumed candidate must not reactivate an unrelated authoritative PASS."""
        store = await create_event_store()
        parent = make_seed(seed_id="seed_resume_focus_parent").model_copy(
            update={
                "acceptance_criteria": (
                    "Stable AC must remain frozen",
                    "Failed AC original contract",
                )
            }
        )
        parent = Seed.from_dict(parent.to_dict())
        prior_evaluation = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            score=0.5,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Stable AC must remain frozen",
                    passed=True,
                ),
                ACResult(
                    ac_index=1,
                    ac_content="Failed AC original contract",
                    passed=False,
                ),
            ),
        )
        await seed_events_for_gen1(store, "lin_seeding_resume_focus", parent, prior_evaluation)

        candidate = parent.model_copy(
            update={
                "acceptance_criteria": (
                    "Stable AC must remain frozen",
                    "Failed AC revised contract",
                ),
                "metadata": parent.metadata.model_copy(
                    update={
                        "seed_id": "seed_resume_focus_candidate",
                        "parent_seed_id": parent.metadata.seed_id,
                    }
                ),
            }
        )
        candidate = Seed.from_dict(candidate.to_dict())
        await store.append(lineage_generation_started("lin_seeding_resume_focus", 2, "wondering"))
        await store.append(
            lineage_generation_phase_changed("lin_seeding_resume_focus", 2, "reflecting")
        )
        await store.append(
            lineage_generation_phase_changed("lin_seeding_resume_focus", 2, "seeding")
        )
        await store.append(
            lineage_generation_interrupted(
                "lin_seeding_resume_focus",
                2,
                last_completed_phase="seeding",
                partial_state={
                    "wonder_questions": ["What remains unresolved?"],
                    "reflect_output": {
                        "refined_goal": candidate.goal,
                        "refined_constraints": list(candidate.constraints),
                        "refined_acs": [
                            criterion.description for criterion in candidate.acceptance_criteria
                        ],
                        "ontology_mutations": [],
                        "reasoning": "persisted focused revision",
                    },
                },
                seed_json=json.dumps(candidate.to_dict()),
            )
        )

        captured: dict[str, object] = {}

        async def _capture_executor(
            seed: Seed,
            *,
            parallel: bool = True,
            execution_id: str | None = None,
            externally_satisfied_acs: dict[int, dict[str, object]] | None = None,
        ) -> str:
            captured["seed"] = seed
            captured["parallel"] = parallel
            captured["execution_id"] = execution_id
            captured["externally_satisfied_acs"] = externally_satisfied_acs
            return "fresh execution"

        candidate_evaluation = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            score=1.0,
            ac_results=tuple(
                ACResult(
                    ac_index=index,
                    ac_content=criterion.description,
                    passed=True,
                )
                for index, criterion in enumerate(candidate.acceptance_criteria)
            ),
        )
        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock()
        reflect_engine = MagicMock()
        reflect_engine.reflect = AsyncMock()
        seed_generator = MagicMock()
        seed_generator.generate_from_reflect = MagicMock()
        evaluator = AsyncMock(return_value=candidate_evaluation)
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(focused_evolution=True, scoped_reexecution=True),
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            seed_generator=seed_generator,
            executor=_capture_executor,
            evaluator=evaluator,
        )

        result = await loop.evolve_step("lin_seeding_resume_focus", execute=True)

        assert result.is_ok
        generation = result.value.generation_result
        assert generation.seed == candidate
        assert generation.active_ac_indices == (1,)
        assert generation.frozen_ac_indices == (0,)
        assert captured["externally_satisfied_acs"] == {
            0: {
                "reason": (
                    "frozen by prior PASS evidence; excluded from evolve and "
                    "reverified at the final boundary"
                )
            }
        }
        wonder_engine.wonder.assert_not_awaited()
        reflect_engine.reflect.assert_not_awaited()
        seed_generator.generate_from_reflect.assert_not_called()
        evaluator.assert_awaited_once_with(candidate, "fresh execution")


class TestEvolveStepConvergence:
    """Test convergence/stagnation/exhaustion detection."""

    @pytest.mark.asyncio
    async def test_convergence_detected(self) -> None:
        """When ontology similarity >= threshold after genuine evolution, action=CONVERGED."""
        store = await create_event_store()

        # Gen 1: different ontology (to show genuine evolution occurred)
        seed_v1 = make_seed(
            seed_id="seed_conv_1",
            ontology_name="TaskManagerV1",
            fields=(
                OntologyField(
                    name="items",
                    field_type="array",
                    description="List of items",
                    required=True,
                ),
            ),
        )
        # Gen 2: evolved ontology (standard schema)
        seed_v2 = make_seed(seed_id="seed_conv_2", parent_seed_id="seed_conv_1")

        await store.append(lineage_created("lin_conv", seed_v1.goal))
        await store.append(
            lineage_generation_completed(
                "lin_conv",
                1,
                seed_v1.metadata.seed_id,
                seed_v1.ontology_schema.model_dump(mode="json"),
                make_eval_summary().model_dump(mode="json"),
                ["Q1"],
                json.dumps(seed_v1.to_dict()),
            )
        )
        await store.append(
            lineage_generation_completed(
                "lin_conv",
                2,
                seed_v2.metadata.seed_id,
                seed_v2.ontology_schema.model_dump(mode="json"),
                make_eval_summary().model_dump(mode="json"),
                ["Q2"],
                json.dumps(seed_v2.to_dict()),
            )
        )

        # Gen 3 returns identical ontology to Gen 2 (similarity=1.0)
        seed_v3 = make_seed(seed_id="seed_conv_3", parent_seed_id="seed_conv_2")
        gen_result = GenerationResult(
            generation_number=3,
            seed=seed_v3,
            evaluation_summary=make_eval_summary(),
            wonder_output=make_wonder_output(should_continue=False),
            ontology_delta=OntologyDelta(similarity=1.0),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop = make_loop(store, gen_result=gen_result)

        result = await loop.evolve_step("lin_conv")

        assert result.is_ok
        step = result.value
        assert step.action == StepAction.CONVERGED
        assert step.convergence_signal.converged

    @pytest.mark.asyncio
    async def test_stagnation_emits_unstuck_and_keeps_lineage_resumable(self) -> None:
        """Stagnation routes to UNSTUCK rather than terminal convergence."""
        store = await create_event_store()
        seed_v1 = make_seed(
            seed_id="seed_stag_1",
            ontology_name="TaskManagerA",
            fields=(
                OntologyField(
                    name="items",
                    field_type="array",
                    description="List of items",
                    required=True,
                ),
            ),
        )
        seed_v2 = make_seed(
            seed_id="seed_stag_2",
            parent_seed_id="seed_stag_1",
            ontology_name="TaskManagerB",
            fields=(
                OntologyField(
                    name="tasks",
                    field_type="array",
                    description="List of tasks",
                    required=True,
                ),
            ),
        )
        seed_v3 = make_seed(
            seed_id="seed_stag_3",
            parent_seed_id="seed_stag_2",
            ontology_name="TaskManagerA",
            fields=seed_v1.ontology_schema.fields,
        )

        await store.append(lineage_created("lin_stag", seed_v1.goal))
        for generation, seed in enumerate((seed_v1, seed_v2, seed_v3), 1):
            await store.append(
                lineage_generation_completed(
                    "lin_stag",
                    generation,
                    seed.metadata.seed_id,
                    seed.ontology_schema.model_dump(mode="json"),
                    make_eval_summary(score=0.85).model_dump(mode="json"),
                    [f"Q{generation}"],
                    json.dumps(seed.to_dict()),
                )
            )

        seed_v4 = make_seed(
            seed_id="seed_stag_4",
            parent_seed_id="seed_stag_3",
            ontology_name="TaskManagerB",
            fields=seed_v2.ontology_schema.fields,
        )
        gen_result = GenerationResult(
            generation_number=4,
            seed=seed_v4,
            evaluation_summary=make_eval_summary(score=0.85),
            wonder_output=make_wonder_output(),
            ontology_delta=OntologyDelta(similarity=0.0),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop = make_loop(
            store,
            gen_result=gen_result,
            config=EvolutionaryLoopConfig(
                max_generations=30,
                convergence_threshold=0.95,
                stagnation_window=3,
                min_generations=2,
                outcome_gate_enabled=False,
            ),
        )

        result = await loop.evolve_step("lin_stag")

        assert result.is_ok
        step = result.value
        assert step.action == StepAction.STAGNATED
        assert step.lineage.status == LineageStatus.ACTIVE

        events = await store.replay_lineage("lin_stag")
        directive = [event for event in events if event.type == "control.directive.emitted"][-1]
        assert directive.data["directive"] == Directive.UNSTUCK.value
        assert directive.data["is_terminal"] is False
        assert directive.data["extra"]["step_action"] == StepAction.STAGNATED.value
        assert directive.data["extra"]["is_terminal"] is False

        replayed = LineageProjector().project(events)
        assert replayed is not None
        assert replayed.status == LineageStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_exhaustion_at_max_generations(self) -> None:
        """When max_generations reached, action=EXHAUSTED."""
        store = await create_event_store()
        seed = make_seed(seed_id="seed_exh_1")

        # Config with max_generations=3 for faster test
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(
                max_generations=3,
                min_generations=1,
                convergence_threshold=0.95,
                stagnation_window=3,
            ),
        )

        # Seed 2 completed generations
        await event_store_with_n_generations(store, "lin_exh", seed, n=2)

        # Gen 3 = max_generations
        seed_v3 = make_seed(seed_id="seed_exh_3", parent_seed_id="seed_exh_2")
        gen_result = GenerationResult(
            generation_number=3,
            seed=seed_v3,
            evaluation_summary=make_eval_summary(),
            wonder_output=make_wonder_output(),
            ontology_delta=OntologyDelta(similarity=1.0),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop._run_generation = AsyncMock(return_value=Result.ok(gen_result))

        result = await loop.evolve_step("lin_exh")

        assert result.is_ok
        step = result.value
        assert step.action in (StepAction.EXHAUSTED, StepAction.CONVERGED)


class TestEvolveStepErrors:
    """Test error cases."""

    @pytest.mark.asyncio
    async def test_error_no_events_no_seed(self) -> None:
        """No events + no initial_seed → error."""
        store = await create_event_store()
        loop = make_loop(store)

        result = await loop.evolve_step("lin_empty")

        assert result.is_err
        assert "initial_seed" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_error_terminated_lineage(self) -> None:
        """Calling evolve_step on a converged lineage → error."""
        store = await create_event_store()
        seed = make_seed()

        # Create events including convergence
        await seed_events_for_gen1(store, "lin_done", seed, make_eval_summary())
        from ouroboros.events.lineage import lineage_converged

        await store.append(lineage_converged("lin_done", 1, "Ontology stable", 0.98))

        loop = make_loop(store)
        result = await loop.evolve_step("lin_done")

        assert result.is_err
        assert "terminated" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_error_no_seed_json_in_events(self) -> None:
        """Events without seed_json → error for Gen 2+."""
        store = await create_event_store()
        seed = make_seed()

        # Manually create events WITHOUT seed_json (simulating old version)
        await store.append(lineage_created("lin_old", seed.goal))
        await store.append(
            lineage_generation_completed(
                "lin_old",
                generation_number=1,
                seed_id=seed.metadata.seed_id,
                ontology_snapshot=seed.ontology_schema.model_dump(mode="json"),
                # No seed_json!
            )
        )

        loop = make_loop(store)
        result = await loop.evolve_step("lin_old")

        assert result.is_err
        assert "seed_json" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_error_oversized_persisted_artifact_contract_is_explicit(self) -> None:
        store = await create_event_store()
        seed = make_seed()
        persisted = seed.to_dict()
        persisted["acceptance_criteria"] = [
            {
                "description": "Materialize an oversized persisted artifact path",
                "expected_artifacts": ["a" * 256],
            }
        ]
        await store.append(lineage_created("lin_oversized_seed", seed.goal))
        await store.append(
            lineage_generation_completed(
                "lin_oversized_seed",
                generation_number=1,
                seed_id=seed.metadata.seed_id,
                ontology_snapshot=seed.ontology_schema.model_dump(mode="json"),
                seed_json=json.dumps(persisted),
            )
        )

        result = await make_loop(store).evolve_step("lin_oversized_seed")

        assert result.is_err
        assert "failed to reconstruct seed from seed_json" in str(result.error).lower()
        assert "longer than 255 filesystem bytes" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_interrupted_replay_reports_invalid_interrupted_seed(self) -> None:
        store = await create_event_store()
        seed = make_seed()
        persisted = seed.to_dict()
        persisted["acceptance_criteria"] = [
            {
                "description": "Materialize an oversized persisted artifact path",
                "expected_artifacts": ["a" * 256],
            }
        ]
        invalid_seed_json = json.dumps(persisted)
        await store.append(lineage_created("lin_invalid_fallback", seed.goal))
        await store.append(
            lineage_generation_completed(
                "lin_invalid_fallback",
                generation_number=1,
                seed_id=seed.metadata.seed_id,
                ontology_snapshot=seed.ontology_schema.model_dump(mode="json"),
                seed_json=invalid_seed_json,
            )
        )
        await store.append(lineage_generation_started("lin_invalid_fallback", 2, "wondering"))
        await store.append(
            lineage_generation_interrupted(
                "lin_invalid_fallback",
                2,
                last_completed_phase="wondering",
                seed_json=invalid_seed_json,
            )
        )

        result = await make_loop(store).evolve_step("lin_invalid_fallback")

        assert result.is_err
        assert "failed to reconstruct interrupted seed from seed_json" in str(result.error).lower()
        assert "longer than 255 filesystem bytes" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_invalid_interrupted_seed_never_rolls_back_or_redispatches(self) -> None:
        store = await create_event_store()
        seed = make_seed()
        invalid = seed.to_dict()
        invalid["acceptance_criteria"] = [
            {
                "description": "Invalid interrupted contract",
                "expected_artifacts": ["a" * 256],
            }
        ]
        await store.append(lineage_created("lin_no_rollback", seed.goal))
        await store.append(
            lineage_generation_completed(
                "lin_no_rollback",
                generation_number=1,
                seed_id=seed.metadata.seed_id,
                ontology_snapshot=seed.ontology_schema.model_dump(mode="json"),
                seed_json=json.dumps(seed.to_dict()),
            )
        )
        await store.append(lineage_generation_started("lin_no_rollback", 2, "wondering"))
        await store.append(
            lineage_generation_interrupted(
                "lin_no_rollback",
                2,
                last_completed_phase="wondering",
                seed_json=json.dumps(invalid),
            )
        )
        loop = make_loop(store)
        loop._run_generation = AsyncMock()

        result = await loop.evolve_step("lin_no_rollback")

        assert result.is_err
        assert "failed to reconstruct interrupted seed" in str(result.error).lower()
        loop._run_generation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_generation_returns_failed_action(self) -> None:
        """_run_generation error → StepResult with action=FAILED."""
        store = await create_event_store()
        seed = make_seed()

        loop = make_loop(
            store,
            gen_error=OuroborosError("Reflect failed: timeout"),
        )

        result = await loop.evolve_step("lin_fail", initial_seed=seed)

        assert result.is_ok  # evolve_step wraps errors in StepResult
        step = result.value
        assert step.action == StepAction.FAILED

    @pytest.mark.asyncio
    async def test_watchdog_timeout_without_directive_metadata_emits_no_fallback_directive(
        self,
    ) -> None:
        """Watchdog timeout without persisted directive metadata must not synthesize one."""
        store = await create_event_store()
        seed = make_seed()

        timeout = GenerationWatchdogTimeout(
            timeout_kind="no_material_progress_timeout",
            reason="Generation had no material progress",
            details={
                "timeout_kind": "no_material_progress_timeout",
                "lineage_id": "lin_watchdog_no_metadata",
                "generation_number": 1,
            },
        )
        loop = make_loop(store, gen_error=timeout)

        result = await loop.evolve_step("lin_watchdog_no_metadata", initial_seed=seed)

        assert result.is_ok
        step = result.value
        assert step.action == StepAction.FAILED
        assert step.generation_result.success is False

        events = await store.replay_lineage("lin_watchdog_no_metadata")
        assert [event for event in events if event.type == "control.directive.emitted"] == []


class TestWatchdogDirectiveEmission:
    """Watchdog timeouts must land on the control-plane directive stream."""

    @pytest.mark.asyncio
    async def test_run_watchdog_no_material_progress_emits_unstuck_directive(self) -> None:
        store = await create_event_store()
        seed = make_seed(seed_id="seed_run_watchdog")
        timeout = make_watchdog_timeout(
            "no_material_progress_timeout",
            "lin_run_watchdog",
        )
        loop = EvolutionaryLoop(event_store=store)
        loop._run_generation_with_watchdog = AsyncMock(return_value=Result.err(timeout))

        result = await loop.run(seed, lineage_id="lin_run_watchdog")

        assert result.is_err
        events = await store.replay_lineage("lin_run_watchdog")
        directives = [event for event in events if event.type == "control.directive.emitted"]
        assert len(directives) == 1
        directive = directives[0]
        assert directive.data["directive"] == Directive.UNSTUCK.value
        assert directive.data["is_terminal"] is False
        assert directive.data["emitted_by"] == "evolver.watchdog"
        assert directive.data["reason"] == timeout.message
        assert directive.data["execution_id"] == "evolve:lin_run_watchdog:generation:1"
        assert directive.data["extra"]["timeout_kind"] == "no_material_progress_timeout"
        assert directive.data["extra"]["is_terminal"] is False
        assert directive.data["extra"]["step_action"] == StepAction.STAGNATED.value
        assert directive.data["extra"]["watchdog_details"]["timeout_kind"] == (
            "no_material_progress_timeout"
        )

    @pytest.mark.asyncio
    async def test_evolve_step_watchdog_no_material_progress_returns_stagnated(self) -> None:
        store = await create_event_store()
        seed = make_seed(seed_id="seed_step_no_material")
        timeout = make_watchdog_timeout(
            "no_material_progress_timeout",
            "lin_step_no_material",
        )
        loop = EvolutionaryLoop(event_store=store)
        loop._run_generation_with_watchdog = AsyncMock(return_value=Result.err(timeout))

        result = await loop.evolve_step("lin_step_no_material", initial_seed=seed)

        assert result.is_ok
        assert result.value.action is StepAction.STAGNATED
        events = await store.replay_lineage("lin_step_no_material")
        directives = [event for event in events if event.type == "control.directive.emitted"]
        assert len(directives) == 1
        directive = directives[0]
        assert directive.data["directive"] == Directive.UNSTUCK.value
        assert directive.data["is_terminal"] is False
        assert directive.data["extra"]["timeout_kind"] == "no_material_progress_timeout"
        assert directive.data["extra"]["step_action"] == StepAction.STAGNATED.value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("timeout_kind", ["safety_timeout", "idle_timeout"])
    async def test_evolve_step_watchdog_terminal_timeouts_emit_cancel_directive(
        self,
        timeout_kind: str,
    ) -> None:
        store = await create_event_store()
        seed = make_seed(seed_id=f"seed_{timeout_kind}")
        timeout = make_watchdog_timeout(timeout_kind, "lin_step_watchdog")
        loop = EvolutionaryLoop(event_store=store)
        loop._run_generation_with_watchdog = AsyncMock(return_value=Result.err(timeout))

        result = await loop.evolve_step("lin_step_watchdog", initial_seed=seed)

        assert result.is_ok
        assert result.value.action is StepAction.EXHAUSTED
        events = await store.replay_lineage("lin_step_watchdog")
        directives = [event for event in events if event.type == "control.directive.emitted"]
        assert len(directives) == 1
        directive = directives[0]
        assert directive.data["directive"] == Directive.CANCEL.value
        assert directive.data["is_terminal"] is True
        assert directive.data["emitted_by"] == "evolver.watchdog"
        assert directive.data["reason"] == timeout.message
        assert directive.data["execution_id"] == "evolve:lin_step_watchdog:generation:1"
        assert directive.data["extra"]["timeout_kind"] == timeout_kind
        assert directive.data["extra"]["is_terminal"] is True
        assert directive.data["extra"]["step_action"] == StepAction.EXHAUSTED.value
        assert directive.data["extra"]["watchdog_details"]["timeout_kind"] == timeout_kind


class TestRunEmitsSeedJson:
    """Test that run() now emits seed_json in events."""

    @pytest.mark.asyncio
    async def test_run_events_include_seed_json(self) -> None:
        """run() method emits seed_json in lineage_generation_completed events."""
        store = await create_event_store()
        seed = make_seed()

        gen_result = GenerationResult(
            generation_number=1,
            seed=seed,
            evaluation_summary=make_eval_summary(score=0.99),
            wonder_output=make_wonder_output(should_continue=False),
            ontology_delta=OntologyDelta(similarity=1.0),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )

        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(min_generations=1),
        )
        loop._run_generation = AsyncMock(return_value=Result.ok(gen_result))

        result = await loop.run(seed)
        assert result.is_ok

        events = await store.replay_lineage(result.value.lineage.lineage_id)
        completed_events = [e for e in events if e.type == "lineage.generation.completed"]
        assert len(completed_events) >= 1

        for ev in completed_events:
            assert "seed_json" in ev.data
            # Verify the seed_json round-trips
            reconstructed = Seed.from_dict(json.loads(ev.data["seed_json"]))
            assert reconstructed.goal == seed.goal


class TestEvolveStepResume:
    """Test resumption after failures."""

    @pytest.mark.asyncio
    async def test_resume_after_failed_generation(self) -> None:
        """Failed Gen 2 → evolve_step resumes at Gen 2 (not Gen 3)."""
        store = await create_event_store()
        seed = make_seed(seed_id="seed_resume_1")

        # Gen 1 completed
        await seed_events_for_gen1(store, "lin_resume", seed, make_eval_summary())

        # Gen 2 started but failed
        from ouroboros.events.lineage import (
            lineage_generation_failed,
            lineage_generation_started,
        )

        await store.append(lineage_generation_started("lin_resume", 2, "wondering"))
        await store.append(lineage_generation_failed("lin_resume", 2, "reflecting", "LLM timeout"))

        # Now resume — should retry Gen 2
        seed_v2 = make_seed(seed_id="seed_resume_2", parent_seed_id="seed_resume_1")
        gen_result = GenerationResult(
            generation_number=2,
            seed=seed_v2,
            evaluation_summary=make_eval_summary(),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop = make_loop(store, gen_result=gen_result)

        result = await loop.evolve_step("lin_resume")

        assert result.is_ok
        step = result.value
        assert step.generation_result.generation_number == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "hard_crash_phase",
        [
            GenerationPhase.WONDERING,
            GenerationPhase.REFLECTING,
            GenerationPhase.SEEDING,
            GenerationPhase.EVALUATING,
        ],
    )
    async def test_hard_crash_replays_the_unfinished_generation(
        self,
        hard_crash_phase: GenerationPhase,
    ) -> None:
        """A recovered nonterminal generation never advances under a new identity."""
        store = await create_event_store()
        parent = make_seed(seed_id="seed_hard_crash_parent")
        candidate = make_seed(
            seed_id=f"seed_checkpointed_{hard_crash_phase.value}",
            parent_seed_id=parent.metadata.seed_id,
        )
        lineage_id = f"lin_hard_crash_{hard_crash_phase.value}"
        await seed_events_for_gen1(store, lineage_id, parent, make_eval_summary(False))
        await store.append(
            lineage_generation_started(
                lineage_id,
                2,
                GenerationPhase.WONDERING.value,
                parent.metadata.seed_id,
                json.dumps(parent.to_dict()),
            )
        )
        last_completed = {
            GenerationPhase.REFLECTING: GenerationPhase.WONDERING.value,
            GenerationPhase.SEEDING: GenerationPhase.REFLECTING.value,
            GenerationPhase.EVALUATING: GenerationPhase.EXECUTING.value,
        }.get(hard_crash_phase)
        if last_completed is not None:
            checkpoint_seed = (
                candidate if hard_crash_phase == GenerationPhase.EVALUATING else parent
            )
            await store.append(
                loop_support.generation_phase_checkpoint(
                    lineage_id=lineage_id,
                    generation_number=2,
                    phase=hard_crash_phase,
                    last_completed_phase=last_completed,
                    seed=checkpoint_seed,
                    active_ac_indices=(0,),
                    frozen_ac_indices=(),
                    wonder_output=(
                        make_wonder_output()
                        if hard_crash_phase == GenerationPhase.SEEDING
                        else None
                    ),
                    reflect_output=(
                        ReflectOutput(
                            refined_goal=parent.goal,
                            refined_constraints=parent.constraints,
                            refined_acs=tuple(
                                criterion.description for criterion in parent.acceptance_criteria
                            ),
                            reasoning="durable reflect",
                        )
                        if hard_crash_phase == GenerationPhase.SEEDING
                        else None
                    ),
                    execution_output=(
                        "durable execution"
                        if hard_crash_phase == GenerationPhase.EVALUATING
                        else None
                    ),
                    execution_boundary_completed=(hard_crash_phase == GenerationPhase.EVALUATING),
                    validation_boundary_completed=(hard_crash_phase == GenerationPhase.EVALUATING),
                )
            )

        successor = make_seed(
            seed_id=f"seed_recovered_{hard_crash_phase.value}",
            parent_seed_id=parent.metadata.seed_id,
        )
        loop = make_loop(
            store,
            gen_result=GenerationResult(
                generation_number=2,
                seed=successor,
                evaluation_summary=make_eval_summary(),
                phase=GenerationPhase.COMPLETED,
                success=True,
            ),
        )

        planned = await loop_support.planned_evolve_generation(store, lineage_id, execute=True)
        result = await loop.evolve_step(lineage_id)

        assert planned == 2
        assert result.is_ok
        assert result.value.generation_result.generation_number == 2
        call = loop._run_generation.call_args
        assert call.kwargs["generation_number"] == 2
        expected_seed = candidate if hard_crash_phase == GenerationPhase.EVALUATING else parent
        assert call.kwargs["current_seed"] == expected_seed
        assert call.kwargs["resume_after_phase"] == last_completed
        replayed = await store.replay_lineage(lineage_id)
        assert (
            sum(
                event.type == "lineage.generation.started"
                and event.data.get("generation_number") == 2
                for event in replayed
            )
            == 1
        )
        assert (
            sum(
                event.type == "lineage.generation.completed"
                and event.data.get("generation_number") == 2
                for event in replayed
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_hard_crash_after_reflect_restores_exact_outputs_without_model_replay(
        self,
    ) -> None:
        """A complete Reflect checkpoint resumes at Seed with identical authority."""
        store = await create_event_store()
        parent = make_seed(seed_id="seed_reflect_checkpoint_parent").model_copy(
            update={
                "acceptance_criteria": (
                    "Tasks can be created",
                    "Tasks can be deleted",
                )
            }
        )
        parent = Seed.from_dict(parent.to_dict())
        prior_evaluation = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            score=0.5,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Tasks can be created",
                    passed=True,
                ),
                ACResult(
                    ac_index=1,
                    ac_content="Tasks can be deleted",
                    passed=False,
                ),
            ),
        )
        lineage_id = "lin_reflect_checkpoint"
        await seed_events_for_gen1(store, lineage_id, parent, prior_evaluation)
        await store.append(
            lineage_generation_started(
                lineage_id,
                2,
                GenerationPhase.WONDERING.value,
                parent.metadata.seed_id,
                json.dumps(parent.to_dict()),
            )
        )
        wonder_output = WonderOutput(
            questions=(),
            grounded_questions=(),
            ontology_tensions=("Deletion semantics remain underspecified",),
            should_continue=True,
            reasoning="Continue despite the empty question list",
        )
        reflect_output = ReflectOutput(
            refined_goal=parent.goal,
            refined_constraints=parent.constraints,
            refined_acs=("Tasks can be created", "Tasks can be archived"),
            ac_patches=(
                ACPatch(op="keep", index=0, reason="authoritative pass"),
                ACPatch(
                    op="revise",
                    index=1,
                    content="Tasks can be archived",
                    reason="repair failed node",
                ),
            ),
            settled_ac_indices=(0,),
            ontology_mutations=(),
            reasoning="Patch only the failed node",
        )
        reflect_output.restore_durable_patch_identity(parent)
        await store.append(
            loop_support.generation_phase_checkpoint(
                lineage_id=lineage_id,
                generation_number=2,
                phase=GenerationPhase.SEEDING,
                last_completed_phase=GenerationPhase.REFLECTING.value,
                seed=parent,
                active_ac_indices=(1,),
                frozen_ac_indices=(0,),
                wonder_output=wonder_output,
                reflect_output=reflect_output,
            )
        )

        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock()
        reflect_engine = MagicMock()
        reflect_engine.reflect = AsyncMock()
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(focused_evolution=True),
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            seed_generator=SeedGenerator(llm_adapter=AsyncMock()),
        )

        result = await loop.evolve_step(lineage_id, execute=False)

        assert result.is_ok
        generation = result.value.generation_result
        assert generation.wonder_output == wonder_output
        assert generation.reflect_output is not None
        assert generation.reflect_output.ac_patch_identity_explicit is True
        assert tuple(
            criterion.description for criterion in generation.seed.acceptance_criteria
        ) == ("Tasks can be created", "Tasks can be archived")
        wonder_engine.wonder.assert_not_awaited()
        reflect_engine.reflect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hard_crash_during_execute_fails_closed_without_redispatch(self) -> None:
        """Unknown executor outcome requires reconciliation, never a duplicate call."""
        store = await create_event_store()
        parent = make_seed(seed_id="seed_execute_crash_parent")
        candidate = make_seed(
            seed_id="seed_execute_crash_candidate",
            parent_seed_id=parent.metadata.seed_id,
        )
        lineage_id = "lin_execute_crash"
        await seed_events_for_gen1(store, lineage_id, parent, make_eval_summary(False))
        await store.append(
            lineage_generation_started(
                lineage_id,
                2,
                GenerationPhase.WONDERING.value,
                parent.metadata.seed_id,
                json.dumps(parent.to_dict()),
            )
        )
        await store.append(
            loop_support.generation_phase_checkpoint(
                lineage_id=lineage_id,
                generation_number=2,
                phase=GenerationPhase.EXECUTING,
                last_completed_phase=GenerationPhase.SEEDING.value,
                seed=candidate,
                active_ac_indices=(0,),
                frozen_ac_indices=(),
            )
        )
        loop = make_loop(
            store,
            gen_result=GenerationResult(
                generation_number=2,
                seed=candidate,
                evaluation_summary=make_eval_summary(),
            ),
        )

        result = await loop.evolve_step(lineage_id, execute=True)

        assert result.is_err
        assert "Cannot safely redispatch" in str(result.error)
        loop._run_generation.assert_not_awaited()
        replayed = await store.replay_lineage(lineage_id)
        assert not any(
            event.type == "lineage.generation.completed"
            and event.data.get("generation_number") == 2
            for event in replayed
        )

    @pytest.mark.asyncio
    async def test_hard_crash_during_evaluation_resumes_without_executor(self) -> None:
        """Durable candidate/output authority lets evaluation continue exactly once."""
        store = await create_event_store()
        parent = make_seed(seed_id="seed_eval_crash_parent")
        candidate = make_seed(
            seed_id="seed_eval_crash_candidate",
            parent_seed_id=parent.metadata.seed_id,
        )
        lineage_id = "lin_eval_crash"
        await seed_events_for_gen1(store, lineage_id, parent, make_eval_summary(False))
        await store.append(
            lineage_generation_started(
                lineage_id,
                2,
                GenerationPhase.WONDERING.value,
                parent.metadata.seed_id,
                json.dumps(parent.to_dict()),
            )
        )
        await store.append(
            loop_support.generation_phase_checkpoint(
                lineage_id=lineage_id,
                generation_number=2,
                phase=GenerationPhase.EVALUATING,
                last_completed_phase=GenerationPhase.EXECUTING.value,
                seed=candidate,
                active_ac_indices=(0,),
                frozen_ac_indices=(),
                execution_output="durable execution",
                execution_boundary_completed=True,
                validation_boundary_completed=True,
            )
        )
        projected = LineageProjector().project(await store.replay_lineage(lineage_id))
        assert projected is not None
        checkpoint = projected.generations[-1]
        assert checkpoint.phase == GenerationPhase.EVALUATING
        assert checkpoint.last_completed_phase == GenerationPhase.EXECUTING.value
        assert Seed.from_dict(json.loads(checkpoint.seed_json or "{}")) == candidate
        assert checkpoint.active_ac_indices == (0,)
        assert checkpoint.frozen_ac_indices == ()
        assert checkpoint.partial_state is not None
        assert checkpoint.partial_state["execution_output"] == "durable execution"
        executor = AsyncMock(return_value="must not run")
        evaluator = AsyncMock(return_value=make_eval_summary(True))
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(min_generations=3),
            executor=executor,
            evaluator=evaluator,
        )

        result = await loop.evolve_step(lineage_id, execute=True)

        assert result.is_ok
        assert result.value.generation_result.seed == candidate
        assert result.value.generation_result.execution_output == "durable execution"
        executor.assert_not_awaited()
        evaluator.assert_awaited_once_with(candidate, "durable execution")
        replayed = await store.replay_lineage(lineage_id)
        assert (
            sum(
                event.type == "lineage.generation.started"
                and event.data.get("generation_number") == 2
                for event in replayed
            )
            == 1
        )
        assert (
            sum(
                event.type == "lineage.generation.completed"
                and event.data.get("generation_number") == 2
                for event in replayed
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_hard_crash_after_empty_evaluation_does_not_redispatch_evaluator(self) -> None:
        """A completed evaluation boundary is authoritative even without a summary."""
        store = await create_event_store()
        parent = make_seed(seed_id="seed_empty_eval_parent")
        candidate = make_seed(
            seed_id="seed_empty_eval_candidate",
            parent_seed_id=parent.metadata.seed_id,
        )
        lineage_id = "lin_empty_eval_checkpoint"
        await seed_events_for_gen1(store, lineage_id, parent, make_eval_summary(False))
        await store.append(
            lineage_generation_started(
                lineage_id,
                2,
                GenerationPhase.WONDERING.value,
                parent.metadata.seed_id,
                json.dumps(parent.to_dict()),
            )
        )
        await store.append(
            loop_support.generation_phase_checkpoint(
                lineage_id=lineage_id,
                generation_number=2,
                phase=GenerationPhase.EVALUATING,
                last_completed_phase=GenerationPhase.EVALUATING.value,
                seed=candidate,
                active_ac_indices=(0,),
                frozen_ac_indices=(),
                execution_output="durable execution",
                execution_boundary_completed=True,
                validation_boundary_completed=True,
                evaluation_summary=None,
            )
        )
        executor = AsyncMock(return_value="must not run")
        evaluator = AsyncMock(return_value=make_eval_summary(True))
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(min_generations=3),
            executor=executor,
            evaluator=evaluator,
        )

        result = await loop.evolve_step(lineage_id, execute=True)

        assert result.is_ok
        assert result.value.generation_result.seed == candidate
        assert result.value.generation_result.execution_output == "durable execution"
        assert result.value.generation_result.evaluation_summary is None
        executor.assert_not_awaited()
        evaluator.assert_not_awaited()
        replayed = await store.replay_lineage(lineage_id)
        assert (
            sum(
                event.type == "lineage.generation.completed"
                and event.data.get("generation_number") == 2
                for event in replayed
            )
            == 1
        )


class TestRunGenerationFailures:
    """Test failure event emission inside _run_generation()."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure_mode", ["exception", "err_result", "unexpected"])
    async def test_evaluator_failures_persist_rejected_summary(self, failure_mode: str) -> None:
        store = await create_event_store()
        seed = make_seed(seed_id=f"seed_eval_{failure_mode}")

        async def evaluator(*_args, **_kwargs):
            if failure_mode == "exception":
                raise RuntimeError("semantic evaluator exploded")
            if failure_mode == "err_result":
                return Result.err("semantic evaluator rejected its input")
            return object()

        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(min_generations=2),
            executor=AsyncMock(return_value="execution completed"),
            evaluator=evaluator,
        )

        result = await loop.evolve_step(
            f"lin_eval_{failure_mode}",
            initial_seed=seed,
            execute=True,
        )

        assert result.is_ok
        summary = result.value.generation_result.evaluation_summary
        assert summary is not None
        assert summary.final_approved is False
        assert summary.score == 0.0
        assert summary.approval_status == "rejected"
        assert summary.execution_completion_status == "completed"
        assert summary.failure_reason is not None
        assert summary.failure_reason.startswith("evaluation errored:")
        events = await store.replay_lineage(f"lin_eval_{failure_mode}")
        completed = [event for event in events if event.type == "lineage.generation.completed"]
        assert completed[-1].data["evaluation_summary"]["final_approved"] is False
        assert (
            completed[-1]
            .data["evaluation_summary"]["failure_reason"]
            .startswith("evaluation errored:")
        )

    @pytest.mark.asyncio
    async def test_seed_generation_failure_emits_failed_event(self) -> None:
        """Seed generation errors should emit lineage.generation.failed(seeding)."""
        store = await create_event_store()
        seed_v1 = make_seed(seed_id="seed_seedfail_1")

        # Build lineage with one completed generation so Gen 2 triggers Wonder/Reflect path
        lineage = OntologyLineage(
            lineage_id="lin_seedgen_fail",
            goal=seed_v1.goal,
            generations=(
                GenerationRecord(
                    generation_number=1,
                    seed_id=seed_v1.metadata.seed_id,
                    ontology_snapshot=seed_v1.ontology_schema,
                    evaluation_summary=make_eval_summary(),
                    phase=GenerationPhase.COMPLETED,
                    seed_json=json.dumps(seed_v1.to_dict()),
                ),
            ),
        )

        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock(return_value=Result.ok(make_wonder_output()))

        reflect_engine = MagicMock()
        reflect_engine.reflect = AsyncMock(
            return_value=Result.ok(
                ReflectOutput(
                    refined_goal=seed_v1.goal,
                    refined_constraints=seed_v1.constraints,
                    refined_acs=seed_v1.acceptance_criteria,
                    ontology_mutations=(),
                    reasoning="test",
                )
            )
        )

        seed_generator = MagicMock()
        seed_generator.generate_from_reflect = MagicMock(
            return_value=Result.err("synthetic seed generation failure")
        )

        loop = EvolutionaryLoop(
            event_store=store,
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            seed_generator=seed_generator,
        )

        result = await loop._run_generation(
            lineage=lineage,
            generation_number=2,
            current_seed=seed_v1,
        )
        assert result.is_err

        events = await store.replay_lineage("lin_seedgen_fail")
        failed = [e for e in events if e.type == "lineage.generation.failed"]
        assert len(failed) == 1
        assert failed[0].data["phase"] == GenerationPhase.SEEDING.value
        assert "synthetic seed generation failure" in failed[0].data["error"]

    @pytest.mark.asyncio
    async def test_failed_directive_phase_recovers_cancelled_runtime_phase(self) -> None:
        """Timeout cancellation reports the last real runtime phase, not cancelled."""
        store = await create_event_store()
        seed = make_seed(seed_id="seed_timeout_phase_1")
        await store.append(lineage_created("lin_timeout_phase", seed.goal))
        await store.append(
            lineage_generation_started(
                "lin_timeout_phase",
                2,
                GenerationPhase.WONDERING.value,
                seed.metadata.seed_id,
            )
        )
        await store.append(
            lineage_generation_phase_changed(
                "lin_timeout_phase",
                2,
                GenerationPhase.REFLECTING.value,
            )
        )
        await store.append(
            lineage_generation_failed(
                "lin_timeout_phase",
                2,
                GenerationPhase.CANCELLED.value,
                "Generation cancelled by timeout",
            )
        )
        phase = await loop_support.phase_for_failed_step_directive(
            store,
            lineage_id="lin_timeout_phase",
            generation_number=2,
        )

        assert phase == GenerationPhase.REFLECTING.value

    async def test_evolve_step_failed_directive_uses_generation_failure_phase(self) -> None:
        """StepAction.FAILED directive should preserve the failed runtime phase."""
        store = await create_event_store()
        seed_v1 = make_seed(seed_id="seed_step_seedfail_1")
        await seed_events_for_gen1(store, "lin_step_seedgen_fail", seed_v1, make_eval_summary())

        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock(return_value=Result.ok(make_wonder_output()))

        reflect_engine = MagicMock()
        reflect_engine.reflect = AsyncMock(
            return_value=Result.ok(
                ReflectOutput(
                    refined_goal=seed_v1.goal,
                    refined_constraints=seed_v1.constraints,
                    refined_acs=seed_v1.acceptance_criteria,
                    ontology_mutations=(),
                    reasoning="test",
                )
            )
        )

        seed_generator = MagicMock()
        seed_generator.generate_from_reflect = MagicMock(
            return_value=Result.err("synthetic seed generation failure")
        )

        loop = EvolutionaryLoop(
            event_store=store,
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            seed_generator=seed_generator,
        )

        result = await loop.evolve_step("lin_step_seedgen_fail")
        assert result.is_ok
        assert result.value.action is StepAction.FAILED

        events = await store.replay_lineage("lin_step_seedgen_fail")
        directive = [e for e in events if e.type == "control.directive.emitted"][-1]
        assert directive.data["phase"] == GenerationPhase.SEEDING.value
        assert directive.data["directive"] == "retry"


class TestLineageStatusHandler:
    """Test LineageStatusHandler MCP tool."""

    @pytest.mark.asyncio
    async def test_returns_status(self) -> None:
        """Handler returns formatted lineage status."""
        from ouroboros.mcp.tools.definitions import LineageStatusHandler

        store = await create_event_store()
        seed = make_seed()
        await seed_events_for_gen1(store, "lin_status", seed, make_eval_summary())

        handler = LineageStatusHandler(event_store=store)
        handler._event_store = store
        handler._initialized = True

        result = await handler.handle({"lineage_id": "lin_status"})

        assert result.is_ok
        text = result.value.text_content
        assert "lin_status" in text
        assert "Build a task manager" in text
        assert result.value.meta["generations"] == 1

    @pytest.mark.asyncio
    async def test_missing_lineage_returns_error(self) -> None:
        """Handler returns error for non-existent lineage."""
        from ouroboros.mcp.tools.definitions import LineageStatusHandler

        store = await create_event_store()
        handler = LineageStatusHandler(event_store=store)
        handler._event_store = store
        handler._initialized = True

        result = await handler.handle({"lineage_id": "nonexistent"})

        assert result.is_err

    @pytest.mark.asyncio
    async def test_returns_latest_frugality_proof(self) -> None:
        """Status exposes proof state without treating node shrinkage as PASS."""
        from ouroboros.events.base import BaseEvent
        from ouroboros.evolution.frugality import (
            PROOF_EVENT,
            EvolutionFrugalityProof,
            EvolutionFrugalityStatus,
        )
        from ouroboros.mcp.tools.definitions import LineageStatusHandler

        store = await create_event_store()
        seed = make_seed()
        await seed_events_for_gen1(store, "lin_status_frugal", seed, make_eval_summary())
        proof = EvolutionFrugalityProof(
            status=EvolutionFrugalityStatus.INSUFFICIENT_DATA,
            comparison_key="sha256:test",
            treatment_receipt_id="lin_status_frugal:generation:2",
            reason="matching isolated full-graph/focused receipt not found",
        )
        await store.append(
            BaseEvent(
                type=PROOF_EVENT,
                aggregate_type="lineage",
                aggregate_id="lin_status_frugal",
                data={"generation_number": 2, "proof": proof.model_dump(mode="json")},
            )
        )
        handler = LineageStatusHandler(event_store=store)
        handler._event_store = store
        handler._initialized = True

        result = await handler.handle({"lineage_id": "lin_status_frugal"})

        assert result.is_ok
        assert "Frugality proof**: insufficient_data" in result.value.text_content
        assert result.value.meta["frugality"]["status"] == "insufficient_data"


class TestEvolveStepHandler:
    """Test EvolveStepHandler MCP tool."""

    @pytest.mark.asyncio
    async def test_handler_gen1(self) -> None:
        """Handler runs Gen 1 with seed_content."""
        from ouroboros.mcp.tools.definitions import EvolveStepHandler

        store = await create_event_store()
        seed = make_seed()

        gen_result = GenerationResult(
            generation_number=1,
            seed=seed,
            evaluation_summary=make_eval_summary(),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop = make_loop(store, gen_result=gen_result)

        handler = EvolveStepHandler(evolutionary_loop=loop)

        import yaml

        with patch(
            "ouroboros.mcp.tools.evolution_handlers.maybe_restore_task_workspace",
            return_value=None,
        ):
            result = await handler.handle(
                {
                    "lineage_id": "lin_handler_test",
                    "seed_content": yaml.dump(seed.to_dict()),
                    "skip_qa": True,
                }
            )

        assert result.is_ok
        assert "Generation 1" in result.value.text_content
        assert result.value.meta["action"] == "converged"
        assert result.value.meta["converged"] is True
        assert result.value.meta["qa_attempted"] is False

    @pytest.mark.asyncio
    async def test_public_benchmark_control_isolation_gates_fail_closed(
        self,
        tmp_path: Path,
    ) -> None:
        from ouroboros.mcp.tools.definitions import EvolveStepHandler

        store = await create_event_store()
        seed = make_seed(seed_id="seed_benchmark_gates")
        for lineage_id in ("lin_missing_project", "lin_no_execute", "lin_dirty_project"):
            await seed_events_for_gen1(store, lineage_id, seed, make_eval_summary(False))
        clean_project = create_clean_git_project(tmp_path / "clean-project")
        dirty_project = create_clean_git_project(tmp_path / "dirty-project")
        (dirty_project / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        handler = EvolveStepHandler(evolutionary_loop=make_loop(store), event_store=store)

        missing_project = await handler.handle(
            {"lineage_id": "lin_missing_project", "benchmark_control": True}
        )
        no_execute = await handler.handle(
            {
                "lineage_id": "lin_no_execute",
                "benchmark_control": True,
                "execute": False,
                "project_dir": str(clean_project),
            }
        )
        gen_one = await handler.handle(
            {
                "lineage_id": "lin_gen_one_control",
                "benchmark_control": True,
                "project_dir": str(clean_project),
            }
        )
        dirty = await handler.handle(
            {
                "lineage_id": "lin_dirty_project",
                "benchmark_control": True,
                "project_dir": str(dirty_project),
            }
        )

        assert missing_project.is_err
        assert "explicit project_dir" in str(missing_project.error)
        assert no_execute.is_err
        assert "execute=true" in str(no_execute.error)
        assert gen_one.is_err
        assert "Gen 2+" in str(gen_one.error)
        assert dirty.is_err
        assert "clean Git baseline" in str(dirty.error)
        await store.close()

    @pytest.mark.asyncio
    async def test_public_handler_emits_full_graph_benchmark_control_observation(
        self,
        tmp_path: Path,
    ) -> None:
        from ouroboros.mcp.tools.definitions import EvolveStepHandler

        source = create_clean_git_project(tmp_path / "source")
        control_worktree = tmp_path / "control-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", "benchmark-control", str(control_worktree)],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        )
        workspace = TaskWorkspace(
            durable_id="lin_public_control",
            repo_root=str(source),
            repo_name=source.name,
            original_cwd=str(source),
            effective_cwd=str(control_worktree),
            worktree_path=str(control_worktree),
            branch="benchmark-control",
            lock_path=str(tmp_path / "control.lock"),
        )
        seed = make_seed(seed_id="seed_public_control").model_copy(
            update={"acceptance_criteria": ("First criterion", "Second criterion")}
        )
        previous = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=1,
            score=0.5,
            ac_results=(
                ACResult(ac_index=0, ac_content="First criterion", passed=True),
                ACResult(ac_index=1, ac_content="Second criterion", passed=False),
            ),
        )
        current = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            score=1.0,
            ac_results=(
                ACResult(ac_index=0, ac_content="First criterion", passed=True),
                ACResult(ac_index=1, ac_content="Second criterion", passed=True),
            ),
        )
        store = await create_event_store()
        await seed_events_for_gen1(store, "lin_public_control", seed, previous)
        externally_satisfied: list[object] = []

        async def executor(
            _seed: Seed,
            *,
            parallel: bool,
            execution_id: str | None = None,
            externally_satisfied_acs: object = None,
        ) -> Result[SimpleNamespace, OuroborosError]:
            del parallel, execution_id
            externally_satisfied.append(externally_satisfied_acs)
            return Result.ok(
                SimpleNamespace(
                    summary={},
                    final_message="executed full graph",
                    duration_seconds=1.0,
                    messages_processed=1,
                )
            )

        async def evaluator(_seed: Seed, _output: str):
            return Result.ok(current)

        executor.frugality_provider_tracking = True  # type: ignore[attr-defined]
        evaluator.frugality_provider_tracking = True  # type: ignore[attr-defined]
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(
                min_generations=2,
                focused_evolution=True,
                scoped_reexecution=True,
            ),
            executor=executor,
            evaluator=evaluator,
        )
        handler = EvolveStepHandler(evolutionary_loop=loop, event_store=store)

        with (
            patch(
                "ouroboros.mcp.tools.evolution_handlers.maybe_restore_task_workspace",
                return_value=workspace,
            ),
            patch("ouroboros.mcp.tools.evolution_handlers.release_lock"),
        ):
            result = await handler.handle(
                {
                    "lineage_id": "lin_public_control",
                    "benchmark_control": True,
                    "execute": True,
                    "project_dir": str(source),
                    "skip_qa": True,
                }
            )

        assert result.is_ok
        assert result.value.meta["benchmark_control"] is True
        assert result.value.meta["active_ac_indices"] == [0, 1]
        assert result.value.meta["frozen_ac_indices"] == []
        assert result.value.meta["frugality"]["observation"]["arm"] == "full_graph"
        assert externally_satisfied == [None]
        assert loop.config.focused_evolution is True
        assert loop.config.scoped_reexecution is True
        await store.close()

    @pytest.mark.asyncio
    async def test_handler_uses_configured_project_when_explicit_project_is_absent(
        self,
        tmp_path: Path,
    ) -> None:
        """The workspace hashed into the request key is the workspace used by evolve."""
        from ouroboros.mcp.tools.definitions import EvolveStepHandler

        store = await create_event_store()
        seed = make_seed(seed_id="seed_configured_handler_workspace")
        generation = GenerationResult(
            generation_number=1,
            seed=seed,
            evaluation_summary=make_eval_summary(),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        step = StepResult(
            generation_result=generation,
            convergence_signal=ConvergenceSignal(
                converged=True,
                reason="verified",
                ontology_similarity=1.0,
                generation=1,
                should_stop=True,
            ),
            lineage=OntologyLineage(
                lineage_id="lin_configured_handler_workspace",
                goal=seed.goal,
            ),
            action=StepAction.CONVERGED,
            next_generation=2,
        )
        loop = make_loop(store)
        configured = str((tmp_path / "configured-project").resolve())
        configured_token = loop.set_project_dir(configured)
        observed_project_dirs: list[str | None] = []

        async def observe_workspace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            observed_project_dirs.append(loop.get_project_dir())
            return Result.ok(step)

        loop.evolve_step = AsyncMock(side_effect=observe_workspace)
        handler = EvolveStepHandler(evolutionary_loop=loop)
        try:
            result = await handler.handle(
                {
                    "lineage_id": "lin_configured_handler_workspace",
                    "execute": False,
                    "skip_qa": True,
                }
            )
        finally:
            loop.reset_project_dir(configured_token)
            await store.close()

        assert result.is_ok
        assert observed_project_dirs == [configured]

    @pytest.mark.asyncio
    async def test_concurrent_public_handler_reuses_workspace_evidence_and_qa(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Distinct processes share the full handler result, not only the core step."""
        from ouroboros.core.errors import ProviderError
        from ouroboros.mcp.tools.definitions import EvolveStepHandler
        from ouroboros.persistence import lineage_claims

        database_url = f"sqlite+aiosqlite:///{tmp_path / 'public-handler.db'}"
        owner_store = EventStore(database_url)
        duplicate_store = EventStore(database_url)
        await owner_store.initialize()
        await duplicate_store.initialize()
        seed = make_seed(seed_id="seed_public_single_flight")
        generation = GenerationResult(
            generation_number=1,
            seed=seed,
            execution_output="verified output",
            validation_output="validated\n" + ("x" * 60_000),
            evaluation_summary=make_eval_summary(),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        step = StepResult(
            generation_result=generation,
            convergence_signal=ConvergenceSignal(
                converged=True,
                reason="verified",
                ontology_similarity=1.0,
                generation=1,
                should_stop=True,
            ),
            lineage=OntologyLineage(lineage_id="lin_public_single_flight", goal=seed.goal),
            action=StepAction.CONVERGED,
            next_generation=2,
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_evolve(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            entered.set()
            await release.wait()
            return Result.ok(step)

        owner_loop = make_loop(owner_store)
        owner_loop.evolve_step = AsyncMock(side_effect=blocked_evolve)
        duplicate_loop = make_loop(duplicate_store)
        duplicate_loop.evolve_step = AsyncMock(side_effect=AssertionError("duplicate core step"))
        owner_project_token = owner_loop.set_project_dir(str(tmp_path / "configured-owner"))
        duplicate_project_token = duplicate_loop.set_project_dir(
            str(tmp_path / "configured-duplicate")
        )
        owner_handler = EvolveStepHandler(evolutionary_loop=owner_loop)
        duplicate_handler = EvolveStepHandler(evolutionary_loop=duplicate_loop)
        workspace_calls = 0
        qa_calls = 0
        waiter_registered = asyncio.Event()
        original_try_acquire = lineage_claims.try_acquire

        async def observe_waiter(*args, **kwargs):  # type: ignore[no-untyped-def]
            claim = await original_try_acquire(*args, **kwargs)
            if (
                kwargs.get("scope") == "evolve-handler"
                and claim is not None
                and claim.waiter_registered
            ):
                waiter_registered.set()
            return claim

        monkeypatch.setattr(lineage_claims, "try_acquire", observe_waiter)
        monkeypatch.setattr(
            loop_support,
            "_event_store_authority",
            lambda event_store: f"object:{id(event_store)}",
        )

        def restore_workspace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal workspace_calls
            workspace_calls += 1
            return None

        class _CountingQAHandler:
            async def handle(self, _arguments):  # noqa: ANN001
                nonlocal qa_calls
                qa_calls += 1
                return Result.err(ProviderError(message="counted QA"))

        import yaml

        arguments = {
            "lineage_id": "lin_public_single_flight",
            "seed_content": yaml.safe_dump(seed.to_dict()),
            "project_dir": str((tmp_path / "explicit-project").resolve()),
        }
        equivalent_arguments = {
            "lineage_id": "lin_public_single_flight",
            "seed_content": yaml.safe_dump(seed.to_dict(), default_flow_style=True),
            "execute": True,
            "parallel": True,
            "skip_qa": False,
            "project_dir": str((tmp_path / "explicit-project").resolve()),
            "recover_expired_claim": False,
        }
        assert arguments["seed_content"] != equivalent_arguments["seed_content"]
        verification = SimpleNamespace(artifact="artifact", reference="reference")
        try:
            with (
                patch(
                    "ouroboros.mcp.tools.evolution_handlers.maybe_restore_task_workspace",
                    side_effect=restore_workspace,
                ),
                patch(
                    "ouroboros.mcp.tools.evolution_handlers.is_git_repo",
                    return_value=True,
                ),
                patch("ouroboros.mcp.tools.qa.QAHandler", _CountingQAHandler),
                patch(
                    "ouroboros.mcp.tools.evolution_handlers.build_verification_artifacts",
                    new=AsyncMock(return_value=verification),
                ) as verification_mock,
            ):
                owner = asyncio.create_task(owner_handler.handle(dict(arguments)))
                await entered.wait()
                duplicate = asyncio.create_task(duplicate_handler.handle(equivalent_arguments))
                await waiter_registered.wait()
                assert not duplicate.done()
                release.set()
                owner_result, duplicate_result = await asyncio.gather(owner, duplicate)

            assert owner_result.is_ok
            assert duplicate_result.is_ok
            assert duplicate_result is not owner_result
            assert duplicate_result.value == owner_result.value
            owner_loop.evolve_step.assert_awaited_once()
            duplicate_loop.evolve_step.assert_not_awaited()
            verification_mock.assert_awaited_once()
            assert workspace_calls == 1
            assert qa_calls == 1
        finally:
            owner_loop.reset_project_dir(owner_project_token)
            duplicate_loop.reset_project_dir(duplicate_project_token)
            await owner_store.close()
            await duplicate_store.close()

    @pytest.mark.asyncio
    async def test_handler_does_not_publish_stagnation_as_verified_convergence(self) -> None:
        from ouroboros.mcp.tools.definitions import EvolveStepHandler

        store = await create_event_store()
        seed = make_seed()
        generation = GenerationResult(
            generation_number=3,
            seed=seed,
            evaluation_summary=make_eval_summary(approved=False),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop = make_loop(store, gen_result=generation)
        loop.evolve_step = AsyncMock(
            return_value=Result.ok(
                StepResult(
                    generation_result=generation,
                    convergence_signal=ConvergenceSignal(
                        converged=False,
                        reason="Stagnation detected: repetitive feedback questions",
                        ontology_similarity=0.0,
                        generation=3,
                        should_stop=True,
                    ),
                    lineage=OntologyLineage(lineage_id="lin_stagnated_meta", goal=seed.goal),
                    action=StepAction.STAGNATED,
                    next_generation=4,
                )
            )
        )
        handler = EvolveStepHandler(evolutionary_loop=loop)

        with patch(
            "ouroboros.mcp.tools.evolution_handlers.maybe_restore_task_workspace",
            return_value=None,
        ):
            result = await handler.handle(
                {
                    "lineage_id": "lin_stagnated_meta",
                    "skip_qa": True,
                }
            )

        assert result.is_ok
        assert result.value.meta["action"] == "stagnated"
        assert result.value.meta["converged"] is False

    @pytest.mark.asyncio
    async def test_handler_publishes_qa_attempt_when_qa_returns_no_result(self) -> None:
        """Consumers can fail closed without reconstructing producer policy."""
        from ouroboros.core.errors import ProviderError
        from ouroboros.mcp.tools.definitions import EvolveStepHandler

        store = await create_event_store()
        seed = make_seed()
        gen_result = GenerationResult(
            generation_number=1,
            seed=seed,
            evaluation_summary=make_eval_summary(),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        handler = EvolveStepHandler(evolutionary_loop=make_loop(store, gen_result=gen_result))

        class _FailingQAHandler:
            async def handle(self, _arguments):  # noqa: ANN001
                return Result.err(ProviderError(message="unparseable QA response"))

        import yaml

        with (
            patch(
                "ouroboros.mcp.tools.evolution_handlers.maybe_restore_task_workspace",
                return_value=None,
            ),
            patch("ouroboros.mcp.tools.qa.QAHandler", _FailingQAHandler),
            patch(
                "ouroboros.mcp.tools.evolution_handlers.build_verification_artifacts",
                new=AsyncMock(side_effect=RuntimeError("no artifact")),
            ),
        ):
            result = await handler.handle(
                {
                    "lineage_id": "lin_handler_qa_attempt",
                    "seed_content": yaml.dump(seed.to_dict()),
                }
            )

        assert result.is_ok
        assert result.value.meta["qa_attempted"] is True
        assert "qa" not in result.value.meta

    @pytest.mark.asyncio
    async def test_handler_qa_uses_canonical_generation_seed_not_caller_override(
        self,
        tmp_path,
    ) -> None:
        """Post-execution QA must follow the Seed actually executed by the core loop."""
        from ouroboros.core.errors import ProviderError
        from ouroboros.mcp.tools.definitions import EvolveStepHandler

        store = await create_event_store()
        stable_seed = make_seed(seed_id="seed_qa_stable")
        override_seed = make_seed(seed_id="seed_qa_override").model_copy(
            update={"acceptance_criteria": ("ATTACKER-ONLY ACCEPTANCE CRITERION",)}
        )
        generation = GenerationResult(
            generation_number=3,
            seed=stable_seed,
            execution_output="verified output",
            evaluation_summary=make_eval_summary(),
        )
        loop = make_loop(store, gen_result=generation)
        loop.evolve_step = AsyncMock(
            return_value=Result.ok(
                StepResult(
                    generation_result=generation,
                    convergence_signal=ConvergenceSignal(
                        converged=True,
                        reason="verified",
                        ontology_similarity=1.0,
                        generation=3,
                        should_stop=True,
                    ),
                    lineage=OntologyLineage(lineage_id="lin_qa_authority", goal=stable_seed.goal),
                    action=StepAction.CONVERGED,
                    next_generation=4,
                )
            )
        )
        captured: dict[str, object] = {}
        parent_dir = tmp_path / "parent"
        worktree_dir = tmp_path / "worktree"
        parent_dir.mkdir()
        worktree_dir.mkdir()
        epoch_file = worktree_dir / "generation.txt"
        epoch_file.write_text("generation-3", encoding="utf-8")
        workspace = SimpleNamespace(
            effective_cwd=str(worktree_dir),
            lock_path=tmp_path / "task.lock",
            worktree_path=str(worktree_dir),
            branch="codex/test-worktree",
        )
        order: list[str] = []

        class _CapturingQAHandler:
            async def handle(self, arguments):  # type: ignore[no-untyped-def]
                order.append("qa")
                captured.update(arguments)
                return Result.err(ProviderError(message="stop after capture"))

        def _capture_checkpoint(*arguments):  # type: ignore[no-untyped-def]
            order.append("checkpoint")
            captured["checkpoint_dir"] = arguments[2]
            return [], []

        async def _capture_verification_dir(*arguments):  # type: ignore[no-untyped-def]
            order.append("build_verification")
            captured["verification_dir"] = arguments[2]
            captured["filesystem_epoch"] = epoch_file.read_text(encoding="utf-8")
            raise RuntimeError("stop after working-dir capture")

        def _release_and_advance_epoch(*_arguments):  # type: ignore[no-untyped-def]
            order.append("release_lock")
            epoch_file.write_text("generation-4", encoding="utf-8")

        handler = EvolveStepHandler(evolutionary_loop=loop)

        import yaml

        with (
            patch(
                "ouroboros.mcp.tools.evolution_handlers.maybe_restore_task_workspace",
                return_value=workspace,
            ),
            patch("ouroboros.mcp.tools.evolution_handlers.is_git_repo", return_value=True),
            patch(
                "ouroboros.mcp.tools.evolution_handlers.release_lock",
                side_effect=_release_and_advance_epoch,
            ),
            patch(
                "ouroboros.mcp.tools.evolution_handlers._checkpoint_passed_generation_acs",
                side_effect=_capture_checkpoint,
            ),
            patch("ouroboros.mcp.tools.qa.QAHandler", _CapturingQAHandler),
            patch(
                "ouroboros.mcp.tools.evolution_handlers.build_verification_artifacts",
                new=AsyncMock(side_effect=_capture_verification_dir),
            ),
        ):
            result = await handler.handle(
                {
                    "lineage_id": "lin_qa_authority",
                    "seed_content": yaml.safe_dump(override_seed.to_dict()),
                    "project_dir": str(parent_dir),
                }
            )

        assert result.is_ok
        assert "Tasks can be created" in str(captured["quality_bar"])
        assert "ATTACKER-ONLY" not in str(captured["quality_bar"])
        qa_seed = yaml.safe_load(str(captured["seed_content"]))
        assert qa_seed["metadata"]["seed_id"] == stable_seed.metadata.seed_id
        assert captured["checkpoint_dir"] == worktree_dir
        assert captured["verification_dir"] == worktree_dir
        assert captured["filesystem_epoch"] == "generation-3"
        assert order == ["checkpoint", "build_verification", "release_lock", "qa"]

    @pytest.mark.asyncio
    async def test_handler_resets_project_dir_after_call(self) -> None:
        """Handler should not leak project_dir between evolve_step calls."""
        from ouroboros.mcp.tools.definitions import EvolveStepHandler

        store = await create_event_store()
        seed = make_seed()

        gen_result = GenerationResult(
            generation_number=1,
            seed=seed,
            evaluation_summary=make_eval_summary(),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop = make_loop(store, gen_result=gen_result)
        handler = EvolveStepHandler(evolutionary_loop=loop)

        import yaml

        result = await handler.handle(
            {
                "lineage_id": "lin_handler_project_dir",
                "seed_content": yaml.dump(seed.to_dict()),
                "project_dir": "/tmp/test-project",
                "skip_qa": True,
            }
        )

        assert result.is_ok
        assert loop.get_project_dir() is None

    @pytest.mark.asyncio
    async def test_handler_without_project_dir_succeeds_outside_git_repo(
        self, tmp_path, monkeypatch
    ) -> None:
        """Handler should still run when server cwd is not a git repo."""
        from ouroboros.mcp.tools.definitions import EvolveStepHandler

        monkeypatch.chdir(tmp_path)

        store = await create_event_store()
        seed = make_seed()

        gen_result = GenerationResult(
            generation_number=1,
            seed=seed,
            evaluation_summary=make_eval_summary(),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop = make_loop(store, gen_result=gen_result)
        handler = EvolveStepHandler(evolutionary_loop=loop)

        import yaml

        result = await handler.handle(
            {
                "lineage_id": "lin_handler_non_git_cwd",
                "seed_content": yaml.dump(seed.to_dict()),
                "skip_qa": True,
            }
        )

        assert result.is_ok
        assert loop.get_project_dir() is None

    @pytest.mark.asyncio
    async def test_handler_returns_task_workspace_error_for_invalid_lineage_id(self) -> None:
        """Invalid worktree-backed lineage IDs should fail as structured task workspace errors."""
        from ouroboros.mcp.tools.definitions import EvolveStepHandler

        store = await create_event_store()
        seed = make_seed()
        gen_result = GenerationResult(
            generation_number=1,
            seed=seed,
            evaluation_summary=make_eval_summary(),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop = make_loop(store, gen_result=gen_result)
        handler = EvolveStepHandler(evolutionary_loop=loop)

        with (
            patch("ouroboros.mcp.tools.evolution_handlers.is_git_repo", return_value=True),
            patch(
                "ouroboros.mcp.tools.evolution_handlers.maybe_restore_task_workspace",
                side_effect=WorktreeError("Invalid durable task identifier for git worktree"),
            ),
        ):
            result = await handler.handle(
                {
                    "lineage_id": "bad id",
                    "project_dir": "/tmp/test-project",
                    "skip_qa": True,
                }
            )

        assert result.is_err
        assert "Task workspace error" in str(result.error)

    @pytest.mark.asyncio
    async def test_handler_explicitly_recovers_expired_writer_claim(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Recovery is operator-authorized and limited to an actually expired owner."""
        from ouroboros.mcp.tools.definitions import EvolveStepHandler
        from ouroboros.persistence import lineage_claims

        store = await create_event_store()
        seed = make_seed(seed_id="seed_explicit_recovery")
        await store.append(lineage_created("lin_explicit_recovery", seed.goal))
        await store.append(
            lineage_generation_started(
                "lin_explicit_recovery",
                1,
                GenerationPhase.EXECUTING.value,
                seed.metadata.seed_id,
                json.dumps(seed.to_dict()),
            )
        )
        await store.append(
            loop_support.generation_phase_checkpoint(
                lineage_id="lin_explicit_recovery",
                generation_number=1,
                phase=GenerationPhase.EVALUATING,
                last_completed_phase=GenerationPhase.EXECUTING.value,
                seed=seed,
                active_ac_indices=(0,),
                frozen_ac_indices=(),
                execution_output="durable execution",
                execution_boundary_completed=True,
                validation_boundary_completed=True,
            )
        )
        generation = GenerationResult(
            generation_number=1,
            seed=seed,
            evaluation_summary=make_eval_summary(),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        loop = make_loop(store, gen_result=generation)
        handler = EvolveStepHandler(evolutionary_loop=loop)
        default_lease_seconds = lineage_claims.DEFAULT_LEASE_SECONDS
        monkeypatch.setattr(lineage_claims, "DEFAULT_LEASE_SECONDS", 0.01)
        claim = await lineage_claims.try_acquire(
            store,
            scope="evolve-core",
            lineage_id="lin_explicit_recovery",
            generation_number=1,
            owner_id="dead-owner",
            request_key="dead-request",
        )
        assert claim is not None and claim.acquired
        await asyncio.sleep(0.02)
        monkeypatch.setattr(
            lineage_claims,
            "DEFAULT_LEASE_SECONDS",
            default_lease_seconds,
        )

        import yaml

        with patch(
            "ouroboros.mcp.tools.evolution_handlers.maybe_restore_task_workspace",
            return_value=None,
        ):
            result = await handler.handle(
                {
                    "lineage_id": "lin_explicit_recovery",
                    "seed_content": yaml.safe_dump(seed.to_dict()),
                    "recover_expired_claim": True,
                    "skip_qa": True,
                }
            )

        assert result.is_ok
        assert result.value.meta["generation"] == 1
        loop._run_generation.assert_awaited_once()
        assert loop._run_generation.call_args.kwargs["generation_number"] == 1
        assert loop._run_generation.call_args.kwargs["current_seed"] == seed
        assert (
            loop._run_generation.call_args.kwargs["resume_after_phase"]
            == GenerationPhase.EXECUTING.value
        )
        replayed = await store.replay_lineage("lin_explicit_recovery")
        assert not any(
            event.data.get("generation_number") == 2
            for event in replayed
            if event.type.startswith("lineage.generation.")
        )
        assert (
            sum(
                event.type == "lineage.generation.started"
                and event.data.get("generation_number") == 1
                for event in replayed
            )
            == 1
        )
        assert (
            sum(
                event.type == "lineage.generation.completed"
                and event.data.get("generation_number") == 1
                for event in replayed
            )
            == 1
        )
        current = await lineage_claims.observe(
            store,
            scope="evolve-core",
            lineage_id="lin_explicit_recovery",
        )
        assert current is not None and current.completed
        assert current.owner_id != "dead-owner"

    @pytest.mark.asyncio
    async def test_handler_recovery_replays_completed_core_before_advancing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A crash after core commit resumes handler evidence for that generation."""
        from pathlib import Path

        from ouroboros.mcp.tools.definitions import EvolveStepHandler
        from ouroboros.mcp.tools.evolution_handlers import _evolve_handler_request_key
        from ouroboros.persistence import lineage_claims

        store = await create_event_store()
        seed = make_seed(seed_id="seed_handler_recovery")
        generation = GenerationResult(
            generation_number=1,
            seed=seed,
            evaluation_summary=make_eval_summary(),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        step = StepResult(
            generation_result=generation,
            convergence_signal=ConvergenceSignal(
                converged=True,
                reason="verified",
                ontology_similarity=1.0,
                generation=1,
                should_stop=True,
            ),
            lineage=OntologyLineage(lineage_id="lin_handler_recovery", goal=seed.goal),
            action=StepAction.CONVERGED,
            next_generation=2,
        )
        loop = make_loop(store)
        loop.evolve_step = AsyncMock(side_effect=AssertionError("must replay core receipt"))
        handler = EvolveStepHandler(evolutionary_loop=loop)

        import yaml

        original_arguments = {
            "lineage_id": "lin_handler_recovery",
            "seed_content": yaml.safe_dump(seed.to_dict()),
            "skip_qa": True,
        }
        request_key = _evolve_handler_request_key(
            original_arguments,
            configured_project_dir=None,
            fallback_cwd=Path.cwd().resolve(),
            execution_policy=loop_support.evolution_execution_policy(loop.config),
        )
        default_lease_seconds = lineage_claims.DEFAULT_LEASE_SECONDS
        core_claim = await lineage_claims.try_acquire(
            store,
            scope="evolve-core",
            lineage_id="lin_handler_recovery",
            generation_number=1,
            owner_id="completed-core",
            request_key="core-request",
        )
        assert core_claim is not None and core_claim.acquired
        assert await lineage_claims.complete(
            store,
            scope="evolve-core",
            lineage_id="lin_handler_recovery",
            owner_id="completed-core",
            result_payload={"durable": "core-result"},
        )
        monkeypatch.setattr(lineage_claims, "DEFAULT_LEASE_SECONDS", 0.01)
        handler_claim = await lineage_claims.try_acquire(
            store,
            scope="evolve-handler",
            lineage_id="lin_handler_recovery",
            generation_number=1,
            owner_id="dead-handler",
            request_key=request_key,
        )
        assert handler_claim is not None and handler_claim.acquired
        await asyncio.sleep(0.02)
        monkeypatch.setattr(
            lineage_claims,
            "DEFAULT_LEASE_SECONDS",
            default_lease_seconds,
        )

        with (
            patch(
                "ouroboros.evolution.step_receipt.decode_step_result",
                new=AsyncMock(return_value=Result.ok(step)),
            ) as decode_mock,
            patch(
                "ouroboros.mcp.tools.evolution_handlers.maybe_restore_task_workspace",
                return_value=None,
            ),
        ):
            result = await handler.handle(
                {
                    **original_arguments,
                    "recover_expired_claim": True,
                }
            )

        assert result.is_ok
        assert result.value.meta["generation"] == 1
        decode_mock.assert_awaited_once()
        loop.evolve_step.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handler_no_loop_returns_error(self) -> None:
        """Handler without evolutionary_loop returns error."""
        from ouroboros.mcp.tools.definitions import EvolveStepHandler

        handler = EvolveStepHandler(evolutionary_loop=None)
        result = await handler.handle({"lineage_id": "test"})

        assert result.is_err


# -- Helper for multi-generation seeding --


async def event_store_with_n_generations(
    store: EventStore,
    lineage_id: str,
    initial_seed: Seed,
    n: int,
) -> None:
    """Populate EventStore with n completed generations."""
    await store.append(lineage_created(lineage_id, initial_seed.goal))

    current_seed = initial_seed
    for i in range(1, n + 1):
        seed_id = f"{initial_seed.metadata.seed_id.rsplit('_', 1)[0]}_{i}"
        parent_id = current_seed.metadata.seed_id if i > 1 else None
        gen_seed = make_seed(
            seed_id=seed_id,
            parent_seed_id=parent_id,
            goal=initial_seed.goal,
        )
        await store.append(
            lineage_generation_completed(
                lineage_id,
                generation_number=i,
                seed_id=gen_seed.metadata.seed_id,
                ontology_snapshot=gen_seed.ontology_schema.model_dump(mode="json"),
                evaluation_summary=make_eval_summary().model_dump(mode="json"),
                wonder_questions=[f"Question from gen {i}"],
                seed_json=json.dumps(gen_seed.to_dict()),
            )
        )
        current_seed = gen_seed
