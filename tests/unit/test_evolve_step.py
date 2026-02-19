"""Unit tests for evolve_step() — single-generation stepping API."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.errors import OuroborosError
from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    LineageStatus,
    OntologyDelta,
    OntologyLineage,
)
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.events.lineage import lineage_created, lineage_generation_completed
from ouroboros.evolution.convergence import ConvergenceSignal
from ouroboros.evolution.loop import (
    EvolutionaryLoop,
    EvolutionaryLoopConfig,
    EvolutionaryResult,
    GenerationResult,
    StepAction,
    StepResult,
)
from ouroboros.evolution.reflect import ReflectOutput
from ouroboros.evolution.wonder import WonderOutput
from ouroboros.persistence.event_store import EventStore


# -- Helpers --


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
) -> EvolutionaryLoop:
    """Create an EvolutionaryLoop with mocked engines.

    Args:
        event_store: The event store to use.
        gen_result: If provided, _run_generation returns this.
        gen_error: If provided, _run_generation returns this error.
    """
    loop = EvolutionaryLoop(
        event_store=event_store,
        config=EvolutionaryLoopConfig(
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


async def seed_events_for_gen1(
    event_store: EventStore,
    lineage_id: str,
    seed: Seed,
    eval_summary: EvaluationSummary | None = None,
) -> None:
    """Populate EventStore with Gen 1 events."""
    await event_store.append(
        lineage_created(lineage_id, seed.goal)
    )
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


# -- Test Classes --


class TestStepTypes:
    """Test StepAction and StepResult types."""

    def test_step_action_values(self) -> None:
        """StepAction has all expected values."""
        assert StepAction.CONTINUE == "continue"
        assert StepAction.CONVERGED == "converged"
        assert StepAction.STAGNATED == "stagnated"
        assert StepAction.EXHAUSTED == "exhausted"
        assert StepAction.FAILED == "failed"
        assert len(StepAction) == 5

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
        assert step.action == StepAction.CONTINUE
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
                OntologyField(name="projects", field_type="array", description="Projects", required=True),
            ),
        )
        gen_result = GenerationResult(
            generation_number=2,
            seed=seed_v2,
            evaluation_summary=make_eval_summary(),
            wonder_output=make_wonder_output(),
            ontology_delta=OntologyDelta(
                added_fields=(OntologyField(name="projects", field_type="array", description="Projects", required=True),),
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


class TestEvolveStepConvergence:
    """Test convergence/stagnation/exhaustion detection."""

    @pytest.mark.asyncio
    async def test_convergence_detected(self) -> None:
        """When ontology similarity >= threshold, action=CONVERGED."""
        store = await create_event_store()
        seed_v1 = make_seed(seed_id="seed_conv_1")

        # Need at least 2 generations for convergence (min_generations=2)
        await event_store_with_n_generations(store, "lin_conv", seed_v1, n=2)

        # Gen 3 returns identical ontology (similarity=1.0)
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
        await store.append(
            lineage_converged("lin_done", 1, "Ontology stable", 0.98)
        )

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

        await store.append(
            lineage_generation_started("lin_resume", 2, "wondering")
        )
        await store.append(
            lineage_generation_failed("lin_resume", 2, "reflecting", "LLM timeout")
        )

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


class TestRunGenerationFailures:
    """Test failure event emission inside _run_generation()."""

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
        result = await handler.handle({
            "lineage_id": "lin_handler_test",
            "seed_content": yaml.dump(seed.to_dict()),
        })

        assert result.is_ok
        assert "Generation 1" in result.value.text_content
        assert result.value.meta["action"] == "continue"

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
