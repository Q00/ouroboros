"""Tests for AC-scoped re-execution plumbing in the evolutionary loop.

Covers RFC §5.3: settled dict built and forwarded; executor without the kwarg →
not forwarded (signature guard); ``scoped_reexecution=False`` → not forwarded;
empty settled → not forwarded; regression exclusion end-to-end (AC passed gen
N-1, regressed gen N → executes in gen N+1).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from ouroboros.core.lineage import (
    ACResult,
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
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
from ouroboros.evolution.loop import EvolutionaryLoop, EvolutionaryLoopConfig
from ouroboros.evolution.reflect import ReflectEngine, ReflectOutput
from ouroboros.evolution.wonder import WonderOutput
from ouroboros.providers.base import CompletionResponse, UsageInfo


async def _store() -> object:
    from ouroboros.persistence.event_store import EventStore

    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    return store


def _seed(acs: tuple[str, ...] = ("AC0", "AC1"), seed_id: str = "seed_1") -> Seed:
    return Seed(
        goal="Build a thing",
        task_type="code",
        constraints=("c1",),
        acceptance_criteria=acs,
        ontology_schema=OntologySchema(
            name="o",
            description="d",
            fields=(OntologyField(name="f", field_type="entity", description="a field"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="completeness", description="done", weight=1.0),
        ),
        exit_conditions=(ExitCondition(name="done", description="d", evaluation_criteria="100%"),),
        metadata=SeedMetadata(seed_id=seed_id, ambiguity_score=0.1),
    )


def _eval(passed: dict[int, bool], acs: tuple[str, ...] = ("AC0", "AC1")) -> EvaluationSummary:
    return EvaluationSummary(
        final_approved=all(passed.values()),
        highest_stage_passed=2,
        score=0.8,
        ac_results=tuple(
            ACResult(ac_index=i, ac_content=acs[i], passed=p) for i, p in passed.items()
        ),
    )


def _gen(number: int, seed: Seed, eval_summary: EvaluationSummary) -> GenerationRecord:
    return GenerationRecord(
        generation_number=number,
        seed_id=seed.metadata.seed_id,
        ontology_snapshot=seed.ontology_schema,
        evaluation_summary=eval_summary,
        phase=GenerationPhase.COMPLETED,
        execution_output="prev output",
        seed_json=json.dumps(seed.to_dict()),
    )


class _CaptureExecutor:
    """Executor spy that records the externally_satisfied_acs it was passed."""

    def __init__(self, accept_kwarg: bool = True) -> None:
        self.accept_kwarg = accept_kwarg
        self.captured: dict[int, dict[str, object]] | None = None
        self.called = False

    async def __call__(self, seed, **kwargs):  # type: ignore[no-untyped-def]
        self.called = True
        self.captured = kwargs.get("externally_satisfied_acs")
        return Result.ok(SimpleNamespace(final_message="done", summary={}))


# A spy with an explicit signature so _callable_accepts_keyword can introspect it.
async def _executor_with_kwarg(
    seed, *, parallel=True, execution_id=None, externally_satisfied_acs=None
):  # type: ignore[no-untyped-def]
    _executor_with_kwarg.captured = externally_satisfied_acs  # type: ignore[attr-defined]
    return Result.ok(SimpleNamespace(final_message="done", summary={}))


async def _executor_without_kwarg(seed, *, parallel=True, execution_id=None):  # type: ignore[no-untyped-def]
    _executor_without_kwarg.called = True  # type: ignore[attr-defined]
    return Result.ok(SimpleNamespace(final_message="done", summary={}))


class TestCallExecutorGuard:
    async def test_forwards_when_executor_accepts_kwarg(self) -> None:
        store = await _store()
        loop = EvolutionaryLoop(event_store=store, executor=_executor_with_kwarg)
        _executor_with_kwarg.captured = "unset"  # type: ignore[attr-defined]

        await loop._call_executor(
            _seed(),
            parallel=True,
            execution_id=None,
            externally_satisfied_acs={0: {"reason": "x"}},
        )
        assert _executor_with_kwarg.captured == {0: {"reason": "x"}}  # type: ignore[attr-defined]

    async def test_signature_guard_skips_unaccepting_executor(self) -> None:
        store = await _store()
        loop = EvolutionaryLoop(event_store=store, executor=_executor_without_kwarg)
        _executor_without_kwarg.called = False  # type: ignore[attr-defined]

        # Must not raise even though we pass a settled dict.
        await loop._call_executor(
            _seed(),
            parallel=True,
            execution_id=None,
            externally_satisfied_acs={0: {"reason": "x"}},
        )
        assert _executor_without_kwarg.called is True  # type: ignore[attr-defined]

    async def test_empty_settled_not_forwarded(self) -> None:
        store = await _store()
        loop = EvolutionaryLoop(event_store=store, executor=_executor_with_kwarg)
        _executor_with_kwarg.captured = "unset"  # type: ignore[attr-defined]

        await loop._call_executor(
            _seed(),
            parallel=True,
            execution_id=None,
            externally_satisfied_acs={},
        )
        # Falsy dict → not forwarded → default None seen by executor.
        assert _executor_with_kwarg.captured is None  # type: ignore[attr-defined]


class TestConfig:
    def test_scoped_reexecution_defaults_true(self) -> None:
        assert EvolutionaryLoopConfig().scoped_reexecution is True

    def test_scoped_reexecution_can_disable(self) -> None:
        assert EvolutionaryLoopConfig(scoped_reexecution=False).scoped_reexecution is False


def _make_loop_for_gen2(
    store, executor: _CaptureExecutor, config: EvolutionaryLoopConfig, settled: tuple[int, ...]
) -> EvolutionaryLoop:
    seed_v1 = _seed()
    new_seed = _seed(seed_id="seed_2")

    wonder_engine = MagicMock()
    wonder_engine.wonder = AsyncMock(
        return_value=Result.ok(
            WonderOutput(questions=("q",), grounded_questions=(), should_continue=True)
        )
    )
    reflect_engine = MagicMock()
    reflect_engine.reflect = AsyncMock(
        return_value=Result.ok(
            ReflectOutput(
                refined_goal=seed_v1.goal,
                refined_constraints=seed_v1.constraints,
                refined_acs=seed_v1.acceptance_criteria,
                settled_ac_indices=settled,
                ontology_mutations=(),
                reasoning="r",
            )
        )
    )
    seed_generator = MagicMock()
    seed_generator.generate_from_reflect = MagicMock(return_value=Result.ok(new_seed))
    evaluator = AsyncMock(return_value=Result.ok(_eval({0: True, 1: True})))

    return EvolutionaryLoop(
        event_store=store,
        config=config,
        wonder_engine=wonder_engine,
        reflect_engine=reflect_engine,
        seed_generator=seed_generator,
        executor=executor,
        evaluator=evaluator,
    )


class TestScopedForwardingIntegration:
    async def test_settled_dict_built_and_forwarded(self) -> None:
        store = await _store()
        seed_v1 = _seed()
        lineage = OntologyLineage(
            lineage_id="lin_forward",
            goal=seed_v1.goal,
            generations=(_gen(1, seed_v1, _eval({0: True, 1: True})),),
        )
        executor = _CaptureExecutor()
        loop = _make_loop_for_gen2(store, executor, EvolutionaryLoopConfig(), settled=(0, 1))

        result = await loop._run_generation(
            lineage=lineage, generation_number=2, current_seed=seed_v1
        )
        assert result.is_ok
        assert executor.captured is not None
        assert set(executor.captured.keys()) == {0, 1}
        assert "satisficed" in executor.captured[0]["reason"]

    async def test_config_off_not_forwarded(self) -> None:
        store = await _store()
        seed_v1 = _seed()
        lineage = OntologyLineage(
            lineage_id="lin_off",
            goal=seed_v1.goal,
            generations=(_gen(1, seed_v1, _eval({0: True, 1: True})),),
        )
        executor = _CaptureExecutor()
        loop = _make_loop_for_gen2(
            store, executor, EvolutionaryLoopConfig(scoped_reexecution=False), settled=(0, 1)
        )

        result = await loop._run_generation(
            lineage=lineage, generation_number=2, current_seed=seed_v1
        )
        assert result.is_ok
        assert executor.called is True
        assert executor.captured is None

    async def test_empty_settled_not_forwarded_integration(self) -> None:
        store = await _store()
        seed_v1 = _seed()
        lineage = OntologyLineage(
            lineage_id="lin_empty",
            goal=seed_v1.goal,
            generations=(_gen(1, seed_v1, _eval({0: True, 1: True})),),
        )
        executor = _CaptureExecutor()
        loop = _make_loop_for_gen2(store, executor, EvolutionaryLoopConfig(), settled=())

        result = await loop._run_generation(
            lineage=lineage, generation_number=2, current_seed=seed_v1
        )
        assert result.is_ok
        assert executor.called is True
        assert executor.captured is None


class TestRegressionExclusionEndToEnd:
    async def test_regressed_ac_re_executes_next_generation(self) -> None:
        """AC1 passes gen1, regresses gen2 → not settled → executes gen3.

        Uses the real ReflectEngine so the satisficing backstop computes settled
        indices from evaluation + regression, exercising the full path.
        """
        store = await _store()
        seed_v1 = _seed()

        # Gen1: both pass. Gen2: AC0 passes, AC1 regresses.
        lineage = OntologyLineage(
            lineage_id="lin_regress",
            goal=seed_v1.goal,
            generations=(
                _gen(1, seed_v1, _eval({0: True, 1: True})),
                _gen(2, seed_v1, _eval({0: True, 1: False})),
            ),
        )

        # Real Wonder-less path: mock wonder to a no-challenge gap question.
        wonder_engine = MagicMock()
        wonder_engine.wonder = AsyncMock(
            return_value=Result.ok(
                WonderOutput(questions=("gap q",), grounded_questions=(), should_continue=True)
            )
        )

        # Real reflect engine; fake adapter returns keep-0, revise-1 (fix the fail).
        reflect_response = json.dumps(
            {
                "refined_goal": seed_v1.goal,
                "refined_constraints": ["c1"],
                "ac_patches": [
                    {"op": "keep", "index": 0},
                    {"op": "revise", "index": 1, "content": "AC1 fixed"},
                ],
                "ontology_mutations": [],
                "reasoning": "fix regression",
            }
        )
        reflect_engine = ReflectEngine(llm_adapter=_FakeAdapter(reflect_response), model="test")

        new_seed = _seed(acs=("AC0", "AC1 fixed"), seed_id="seed_3")
        seed_generator = MagicMock()
        seed_generator.generate_from_reflect = MagicMock(return_value=Result.ok(new_seed))
        evaluator = AsyncMock(return_value=Result.ok(_eval({0: True, 1: True})))

        executor = _CaptureExecutor()
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(),
            wonder_engine=wonder_engine,
            reflect_engine=reflect_engine,
            seed_generator=seed_generator,
            executor=executor,
            evaluator=evaluator,
        )

        result = await loop._run_generation(
            lineage=lineage, generation_number=3, current_seed=seed_v1
        )
        assert result.is_ok
        # AC0 kept + passed → settled (skipped). AC1 failed/regressed → executes.
        assert executor.captured is not None
        assert set(executor.captured.keys()) == {0}
        assert 1 not in executor.captured


class _FakeAdapter:
    def __init__(self, content: str) -> None:
        self.content = content
        self._max_turns = 1

    async def complete(self, messages, config):  # type: ignore[no-untyped-def]
        return Result.ok(
            CompletionResponse(
                content=self.content,
                model=config.model,
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )
