"""Unit tests for ConvergenceCriteria — oscillation detection and convergence gating."""

from __future__ import annotations

import json

import pytest

from ouroboros.core.lineage import (
    ACResult,
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    OntologyDelta,
    OntologyLineage,
)
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.evolution.convergence import ConvergenceCriteria
from ouroboros.evolution.evaluation_coverage import validate_seed_ac_coverage
from ouroboros.evolution.wonder import WonderOutput

# -- Helpers --


def _schema(fields: tuple[str, ...]) -> OntologySchema:
    """Create an OntologySchema with the given field names."""
    return OntologySchema(
        name="Test",
        description="Test schema",
        fields=tuple(
            OntologyField(name=n, field_type="string", description=n, required=True) for n in fields
        ),
    )


SCHEMA_A = _schema(("alpha", "beta"))
SCHEMA_B = _schema(("gamma", "delta"))
SCHEMA_C = _schema(("epsilon", "zeta"))
SCHEMA_D = _schema(("eta", "theta"))


def _seed(*acs: str | AcceptanceCriterionSpec) -> Seed:
    return Seed(
        metadata=SeedMetadata(ambiguity_score=0.1),
        goal="test goal",
        constraints=("stay deterministic",),
        acceptance_criteria=acs,
        ontology_schema=SCHEMA_A,
    )


def _approved_evaluation(seed: Seed, *, score: float | None = 0.9) -> EvaluationSummary:
    return EvaluationSummary(
        final_approved=True,
        highest_stage_passed=2,
        score=score,
        ac_results=tuple(
            ACResult(
                ac_index=index,
                ac_content=criterion.description,
                passed=True,
            )
            for index, criterion in enumerate(seed.acceptance_criteria)
        ),
    )


def _lineage_with_schemas(*schemas: OntologySchema) -> OntologyLineage:
    """Build an OntologyLineage with generations using the given schemas."""
    gens = tuple(
        GenerationRecord(
            generation_number=i + 1,
            seed_id=f"seed_{i + 1}",
            ontology_snapshot=s,
            phase=GenerationPhase.COMPLETED,
        )
        for i, s in enumerate(schemas)
    )
    return OntologyLineage(
        lineage_id="test_lin",
        goal="test goal",
        generations=gens,
    )


def _generation(
    number: int,
    schema: OntologySchema,
    phase: GenerationPhase = GenerationPhase.COMPLETED,
) -> GenerationRecord:
    return GenerationRecord(
        generation_number=number,
        seed_id=f"seed_{number}",
        ontology_snapshot=schema,
        phase=phase,
    )


def _lineage_with_generations(*generations: GenerationRecord) -> OntologyLineage:
    return OntologyLineage(
        lineage_id="test_lin",
        goal="test goal",
        generations=tuple(generations),
    )


# -- Feature 1: Oscillation Detection --


class TestOscillationDetection:
    """Tests for _check_oscillation and its integration in the convergence check."""

    def test_oscillation_period2_full_detected(self) -> None:
        """A,B,A,B pattern (4 gens, both half-periods verified) -> converged=True."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_B, SCHEMA_A, SCHEMA_B)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=30,
        )
        signal = criteria.evaluate(lineage)
        assert signal.should_stop
        assert not signal.converged
        assert "Oscillation" in signal.reason

    def test_oscillation_period2_partial_3gens(self) -> None:
        """A,B,A pattern (3 gens, simple N~N-2 check) -> converged=True."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_B, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=30,
        )
        signal = criteria.evaluate(lineage)
        assert signal.should_stop
        assert not signal.converged
        assert "Oscillation" in signal.reason

    def test_oscillation_not_detected_different(self) -> None:
        """Four completely different schemas -> no oscillation."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_B, SCHEMA_C, SCHEMA_D)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=30,
        )
        signal = criteria.evaluate(lineage)
        # Should not converge via oscillation (may not converge at all)
        if signal.converged:
            assert "Oscillation" not in signal.reason

    def test_oscillation_below_min_gens(self) -> None:
        """Only 2 generations -> oscillation check not triggered."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_B)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=30,
        )
        # With 2 gens, oscillation requires >= 3, so it won't trigger oscillation
        signal = criteria.evaluate(lineage)
        if signal.converged:
            assert "Oscillation" not in signal.reason

    def test_oscillation_disabled_via_config(self) -> None:
        """enable_oscillation_detection=False -> oscillation skipped."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_B, SCHEMA_A, SCHEMA_B)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=30,
            enable_oscillation_detection=False,
        )
        signal = criteria.evaluate(lineage)
        # Should not converge via oscillation
        if signal.converged:
            assert "Oscillation" not in signal.reason

    def test_oscillation_reason_contains_keyword(self) -> None:
        """Oscillation signal reason must contain 'Oscillation' for loop.py routing."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_B, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=30,
        )
        signal = criteria.evaluate(lineage)
        assert signal.should_stop
        assert not signal.converged
        assert "Oscillation" in signal.reason

    def test_oscillation_no_indexerror_3gens(self) -> None:
        """Exactly 3 gens must not raise IndexError (regression guard)."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_B, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=30,
        )
        # Should not raise
        signal = criteria.evaluate(lineage)
        assert isinstance(signal.converged, bool)


class TestOscillationLoopRouting:
    """Test that loop.py routes oscillation to STAGNATED action."""

    @pytest.mark.asyncio
    async def test_loop_routes_oscillation_to_stagnated(self) -> None:
        """Oscillation signal should map to StepAction.STAGNATED in evolve_step."""
        import json
        from unittest.mock import AsyncMock

        from ouroboros.core.seed import (
            EvaluationPrinciple,
            ExitCondition,
            Seed,
            SeedMetadata,
        )
        from ouroboros.core.types import Result
        from ouroboros.events.lineage import (
            lineage_created,
            lineage_generation_completed,
        )
        from ouroboros.evolution.loop import (
            EvolutionaryLoop,
            EvolutionaryLoopConfig,
            GenerationResult,
            StepAction,
        )
        from ouroboros.persistence.event_store import EventStore

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()

        def _seed(
            sid: str,
            parent: str | None = None,
            schema: OntologySchema | None = None,
        ) -> Seed:
            return Seed(
                goal="test",
                task_type="code",
                constraints=("Python",),
                acceptance_criteria=("Works",),
                ontology_schema=schema or SCHEMA_A,
                evaluation_principles=(EvaluationPrinciple(name="c", description="c", weight=1.0),),
                exit_conditions=(
                    ExitCondition(name="e", description="e", evaluation_criteria="e"),
                ),
                metadata=SeedMetadata(seed_id=sid, parent_seed_id=parent, ambiguity_score=0.1),
            )

        # Seed 3 completed generations: A, B, A (oscillation pattern)
        s1 = _seed("s1", schema=SCHEMA_A)
        s2 = _seed("s2", schema=SCHEMA_B)
        s3 = _seed("s3", schema=SCHEMA_A)

        await store.append(lineage_created("lin_osc", "test"))
        for i, s in enumerate([s1, s2, s3], 1):
            eval_sum = EvaluationSummary(final_approved=True, highest_stage_passed=2, score=0.85)
            await store.append(
                lineage_generation_completed(
                    "lin_osc",
                    i,
                    s.metadata.seed_id,
                    s.ontology_schema.model_dump(mode="json"),
                    eval_sum.model_dump(mode="json"),
                    [f"q{i}"],
                    seed_json=json.dumps(s.to_dict()),
                )
            )

        # Gen 4 returns SCHEMA_B (A,B,A,B pattern)
        s4 = _seed("s4", parent="s3", schema=SCHEMA_B)
        gen_result = GenerationResult(
            generation_number=4,
            seed=s4,
            evaluation_summary=EvaluationSummary(
                final_approved=True, highest_stage_passed=2, score=0.85
            ),
            wonder_output=WonderOutput(
                questions=("q?",),
                ontology_tensions=(),
                should_continue=True,
                reasoning="r",
            ),
            ontology_delta=OntologyDelta(similarity=0.0),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )

        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(
                max_generations=30,
                convergence_threshold=0.95,
                min_generations=2,
                outcome_gate_enabled=False,
            ),
        )
        loop._run_generation = AsyncMock(return_value=Result.ok(gen_result))

        result = await loop.evolve_step("lin_osc")
        assert result.is_ok
        assert result.value.action == StepAction.STAGNATED


class TestCompletedGenerationFiltering:
    """Regression guards for interrupted generations with pending ontologies."""

    def test_latest_similarity_ignores_pending_tail(self) -> None:
        lineage = _lineage_with_generations(
            _generation(1, SCHEMA_B),
            _generation(2, SCHEMA_A),
            _generation(3, SCHEMA_C, phase=GenerationPhase.WONDERING),
        )
        criteria = ConvergenceCriteria(convergence_threshold=0.95, min_generations=2)

        assert criteria._latest_similarity(lineage) == pytest.approx(0.0)

    def test_stagnation_ignores_pending_tail(self) -> None:
        lineage = _lineage_with_generations(
            _generation(1, SCHEMA_A),
            _generation(2, SCHEMA_A),
            _generation(3, SCHEMA_B, phase=GenerationPhase.WONDERING),
        )
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            stagnation_window=2,
        )

        assert criteria._check_stagnation(lineage) is True

    def test_oscillation_ignores_pending_tail(self) -> None:
        lineage = _lineage_with_generations(
            _generation(1, SCHEMA_A),
            _generation(2, SCHEMA_B),
            _generation(3, SCHEMA_A),
            _generation(4, SCHEMA_C, phase=GenerationPhase.WONDERING),
        )
        criteria = ConvergenceCriteria(convergence_threshold=0.95, min_generations=2)

        assert criteria._check_oscillation(lineage) is True

    def test_evolution_count_ignores_pending_tail(self) -> None:
        lineage = _lineage_with_generations(
            _generation(1, SCHEMA_A),
            _generation(2, SCHEMA_B),
            _generation(3, SCHEMA_C, phase=GenerationPhase.WONDERING),
        )
        criteria = ConvergenceCriteria(convergence_threshold=0.95, min_generations=2)

        assert criteria._count_evolved_generations(lineage) == 1

    def test_evaluate_max_generations_ignores_pending(self) -> None:
        """max_generations should only count completed generations."""
        # 29 completed + 1 pending = 30 total, but only 29 completed
        completed_gens = [_generation(i, SCHEMA_A) for i in range(1, 30)]
        pending = _generation(30, SCHEMA_B, phase=GenerationPhase.WONDERING)
        lineage = _lineage_with_generations(*completed_gens, pending)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=30,
        )
        signal = criteria.evaluate(lineage, None)
        # Should NOT hit max_generations because only 29 are completed
        assert "Max generations" not in signal.reason

    def test_evaluate_min_generations_ignores_pending(self) -> None:
        """min_generations guard should only count completed generations."""
        lineage = _lineage_with_generations(
            _generation(1, SCHEMA_A),
            _generation(2, SCHEMA_B, phase=GenerationPhase.WONDERING),
        )
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
        )
        signal = criteria.evaluate(lineage, None)
        assert "Below minimum" in signal.reason
        assert "1/2" in signal.reason  # Only 1 completed out of 2 required


# -- Feature 2: Convergence Gating via Evaluation --


class TestConvergenceGating:
    """Tests for eval_gate_enabled convergence gating."""

    def _converging_lineage(self) -> OntologyLineage:
        """Create a 3-gen lineage that evolved once then converged (B→A→A).

        Gen 1→2: B→A = genuine evolution (similarity < threshold).
        Gen 2→3: A→A = stable (similarity = 1.0).
        This passes the evolution gate because evolution DID occur.
        """
        return _lineage_with_schemas(SCHEMA_B, SCHEMA_A, SCHEMA_A)

    def test_gate_disabled_explicitly(self) -> None:
        """Explicitly disabled gate: convergence proceeds despite bad eval."""
        lineage = self._converging_lineage()
        seed = _seed("AC zero")
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=EvaluationSummary(
                final_approved=False,
                highest_stage_passed=1,
                score=0.3,
                ac_results=(ACResult(ac_index=0, ac_content="AC zero", passed=False),),
            ),
            latest_seed=seed,
        )
        # Gate disabled -> converges despite bad result
        assert signal.converged

    def test_gate_blocks_when_not_approved(self) -> None:
        """Gate enabled + approved=False -> converged=False."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
            eval_min_score=0.7,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=EvaluationSummary(
                final_approved=False, highest_stage_passed=1, score=0.9
            ),
        )
        assert not signal.converged
        assert "unsatisfactory" in signal.reason

    def test_gate_blocks_when_score_low(self) -> None:
        """Gate enabled + score < min -> converged=False."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
            eval_min_score=0.7,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=EvaluationSummary(
                final_approved=True, highest_stage_passed=2, score=0.5
            ),
        )
        assert not signal.converged
        assert "unsatisfactory" in signal.reason

    def test_gate_passes_when_satisfactory(self) -> None:
        """Gate enabled + approved=True + score >= min -> converged=True."""
        lineage = self._converging_lineage()
        seed = _seed("AC zero")
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
            eval_min_score=0.7,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=EvaluationSummary(
                final_approved=True,
                highest_stage_passed=2,
                score=0.9,
                ac_results=(ACResult(ac_index=0, ac_content="AC zero", passed=True),),
            ),
            latest_seed=seed,
        )
        assert signal.converged

    def test_gate_blocks_when_no_result(self) -> None:
        """Gate enabled but no result provided -> fail closed."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=None,
            evaluation_expected=True,
        )
        assert not signal.converged
        assert "evaluation unavailable" in signal.reason

    def test_gate_does_not_affect_max_generations(self) -> None:
        """Hard cap (max_generations) still works even with gate."""
        # Build lineage with max_generations=3 and 3 different schemas
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_B, SCHEMA_C)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=3,
            eval_gate_enabled=True,
            eval_min_score=0.7,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=EvaluationSummary(
                final_approved=False, highest_stage_passed=1, score=0.1
            ),
        )
        assert signal.should_stop
        assert not signal.converged
        assert "Max generations" in signal.reason

    def test_verified_outcome_wins_on_final_generation(self) -> None:
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_B, SCHEMA_C)
        seed = _seed("AC zero")

        signal = ConvergenceCriteria(
            min_generations=2,
            max_generations=3,
            eval_gate_enabled=True,
        ).evaluate(
            lineage,
            latest_evaluation=_approved_evaluation(seed),
            latest_seed=seed,
            validation_output="Validation passed (attempt 1/3)",
        )

        assert signal.should_stop
        assert signal.converged
        assert "Outcome gate passed" in signal.reason

    def test_gate_approved_true_score_none(self) -> None:
        """approved=True + score=None -> convergence allowed (no score to block)."""
        lineage = self._converging_lineage()
        seed = _seed("AC zero")
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
            eval_min_score=0.7,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=EvaluationSummary(
                final_approved=True,
                highest_stage_passed=2,
                score=None,
                ac_results=(ACResult(ac_index=0, ac_content="AC zero", passed=True),),
            ),
            latest_seed=seed,
        )
        assert signal.converged

    def test_gate_blocks_when_current_seed_is_unavailable(self) -> None:
        lineage = self._converging_lineage()
        evaluation = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            score=0.9,
            ac_results=(ACResult(ac_index=0, ac_content="AC zero", passed=True),),
        )

        signal = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        ).evaluate(lineage, latest_evaluation=evaluation)

        assert not signal.converged
        assert signal.reason == "Per-AC gate: current Seed unavailable for coverage validation"

    def test_gate_approved_false_score_none(self) -> None:
        """approved=False + score=None -> convergence blocked."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
            eval_min_score=0.7,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=EvaluationSummary(
                final_approved=False, highest_stage_passed=1, score=None
            ),
        )
        assert not signal.converged
        assert "unsatisfactory" in signal.reason


class TestLoopEngineeringExitSignals:
    """Outcome PASS and no-drift are bounded loop exit gates."""

    @staticmethod
    def _scored_lineage(*scores: float) -> OntologyLineage:
        schemas = (SCHEMA_A, SCHEMA_B, SCHEMA_C, SCHEMA_D)
        generations = tuple(
            GenerationRecord(
                generation_number=index + 1,
                seed_id=f"seed_{index + 1}",
                ontology_snapshot=schemas[index],
                evaluation_summary=EvaluationSummary(
                    final_approved=False,
                    highest_stage_passed=2,
                    score=score,
                ),
                phase=GenerationPhase.COMPLETED,
            )
            for index, score in enumerate(scores)
        )
        return _lineage_with_generations(*generations)

    def test_outcome_pass_stops_after_first_generation(self) -> None:
        seed = _seed("AC zero", "AC one")
        evaluation = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            score=0.9,
            ac_results=(
                ACResult(ac_index=0, ac_content="AC zero", passed=True),
                ACResult(ac_index=1, ac_content="AC one", passed=True),
            ),
        )
        lineage = _lineage_with_generations(
            GenerationRecord(
                generation_number=1,
                seed_id="seed_1",
                ontology_snapshot=SCHEMA_A,
                evaluation_summary=evaluation,
                phase=GenerationPhase.COMPLETED,
                seed_json=json.dumps(seed.to_dict()),
            )
        )
        criteria = ConvergenceCriteria(
            min_generations=3,
            eval_gate_enabled=True,
            outcome_gate_enabled=True,
        )

        signal = criteria.evaluate(
            lineage,
            latest_evaluation=evaluation,
            validation_output="Validation passed (attempt 1/3)",
            latest_seed=seed,
        )

        assert signal.converged
        assert "Outcome gate passed" in signal.reason
        assert signal.generation == 1

    @pytest.mark.asyncio
    async def test_ontology_only_evolve_step_converges_after_real_change(self) -> None:
        from unittest.mock import AsyncMock

        from ouroboros.core.types import Result
        from ouroboros.events.lineage import lineage_created, lineage_generation_completed
        from ouroboros.evolution.loop import (
            EvolutionaryLoop,
            EvolutionaryLoopConfig,
            GenerationResult,
            StepAction,
        )
        from ouroboros.persistence.event_store import EventStore

        def ontology_seed(
            seed_id: str,
            parent_seed_id: str | None,
            schema: OntologySchema,
        ) -> Seed:
            return Seed(
                metadata=SeedMetadata(
                    seed_id=seed_id,
                    parent_seed_id=parent_seed_id,
                    ambiguity_score=0.1,
                ),
                goal="test goal",
                constraints=("stay deterministic",),
                acceptance_criteria=("AC zero",),
                ontology_schema=schema,
            )

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        seed_1 = ontology_seed("seed_1", None, SCHEMA_B)
        seed_2 = ontology_seed("seed_2", "seed_1", SCHEMA_A)
        seed_3 = ontology_seed("seed_3", "seed_2", SCHEMA_A)

        await store.append(lineage_created("lin_ontology_only", "test goal"))
        for generation_number, seed in enumerate((seed_1, seed_2), start=1):
            await store.append(
                lineage_generation_completed(
                    "lin_ontology_only",
                    generation_number,
                    seed.metadata.seed_id,
                    seed.ontology_schema.model_dump(mode="json"),
                    None,
                    [f"question {generation_number}"],
                    seed_json=json.dumps(seed.to_dict()),
                )
            )

        generation = GenerationResult(
            generation_number=3,
            seed=seed_3,
            ontology_delta=OntologyDelta.compute(SCHEMA_A, SCHEMA_A),
            phase=GenerationPhase.COMPLETED,
            success=True,
        )
        validator = AsyncMock()
        loop = EvolutionaryLoop(
            event_store=store,
            config=EvolutionaryLoopConfig(
                min_generations=2,
                convergence_threshold=0.95,
                eval_gate_enabled=True,
            ),
            validator=validator,
        )
        run_generation = AsyncMock(return_value=Result.ok(generation))
        loop._run_generation = run_generation

        result = await loop.evolve_step("lin_ontology_only", execute=False)

        assert result.is_ok
        assert result.value.action == StepAction.CONVERGED
        assert result.value.convergence_signal.ontology_similarity == pytest.approx(1.0)
        assert run_generation.await_args.kwargs["execute"] is False
        validator.assert_not_awaited()

    @pytest.mark.parametrize(
        "results",
        [
            (ACResult(ac_index=0, ac_content="AC zero", passed=True),),
            (
                ACResult(ac_index=0, ac_content="AC zero", passed=True),
                ACResult(ac_index=0, ac_content="AC zero", passed=True),
            ),
            (
                ACResult(ac_index=0, ac_content="AC zero", passed=True),
                ACResult(ac_index=2, ac_content="outside", passed=True),
            ),
            (
                ACResult(ac_index=0, ac_content="not AC zero", passed=True),
                ACResult(ac_index=1, ac_content="AC one", passed=True),
            ),
        ],
        ids=("missing", "duplicate", "out-of-range", "content-mismatch"),
    )
    def test_outcome_gate_requires_exact_seed_wide_verdict_coverage(
        self,
        results: tuple[ACResult, ...],
    ) -> None:
        seed = _seed("AC zero", "AC one")
        evaluation = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            score=0.9,
            ac_results=results,
        )
        lineage = _lineage_with_generations(
            GenerationRecord(
                generation_number=1,
                seed_id="seed_1",
                ontology_snapshot=SCHEMA_A,
                evaluation_summary=evaluation,
                seed_json=json.dumps(seed.to_dict()),
                phase=GenerationPhase.COMPLETED,
            )
        )

        signal = ConvergenceCriteria(
            min_generations=3,
            eval_gate_enabled=True,
            outcome_gate_enabled=True,
        ).evaluate(
            lineage,
            latest_evaluation=evaluation,
            latest_seed=seed,
            validation_output="Validation passed (attempt 1/3)",
        )

        assert not signal.converged
        assert "Below minimum generations" in signal.reason

    def test_structured_coverage_rejects_stale_semantic_identity(self) -> None:
        prior_seed = _seed(
            AcceptanceCriterionSpec(
                description="Artifact exists",
                verify_command="test -f artifact.txt",
            )
        )
        current_seed = _seed(
            AcceptanceCriterionSpec(
                description="Artifact exists",
                verify_command="python scripts/verify_artifact.py",
            )
        )
        stale_evaluation = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            score=1.0,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Artifact exists",
                    semantic_ac_key=prior_seed.acceptance_criteria[0].semantic_ac_key,
                    passed=True,
                ),
            ),
        )

        coverage = validate_seed_ac_coverage(current_seed, stale_evaluation)

        assert not coverage.complete
        assert "semantic identity differs" in coverage.reason

    @pytest.mark.parametrize("ac_gate_mode", ("all", "off"))
    def test_stability_gate_rejects_empty_verdict_coverage_for_current_seed(
        self,
        ac_gate_mode: str,
    ) -> None:
        seed = _seed("AC zero", "AC one")
        evaluation = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            score=0.9,
        )
        lineage = _lineage_with_generations(
            GenerationRecord(
                generation_number=1,
                seed_id="seed_1",
                ontology_snapshot=SCHEMA_B,
                evaluation_summary=evaluation,
                phase=GenerationPhase.COMPLETED,
            ),
            GenerationRecord(
                generation_number=2,
                seed_id="seed_2",
                ontology_snapshot=SCHEMA_A,
                evaluation_summary=evaluation,
                phase=GenerationPhase.COMPLETED,
            ),
            GenerationRecord(
                generation_number=3,
                seed_id="seed_3",
                ontology_snapshot=SCHEMA_A,
                evaluation_summary=evaluation,
                phase=GenerationPhase.COMPLETED,
            ),
        )

        signal = ConvergenceCriteria(
            min_generations=2,
            eval_gate_enabled=True,
            outcome_gate_enabled=False,
            ac_gate_mode=ac_gate_mode,
        ).evaluate(
            lineage,
            latest_evaluation=evaluation,
            latest_seed=seed,
            evaluation_expected=True,
        )

        assert not signal.converged
        assert signal.reason == "Per-AC gate: missing per-AC verdict indices: [0, 1]"

    def test_validation_failure_blocks_first_generation_outcome_gate(self) -> None:
        evaluation = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            score=0.9,
        )
        lineage = _lineage_with_generations(
            GenerationRecord(
                generation_number=1,
                seed_id="seed_1",
                ontology_snapshot=SCHEMA_A,
                evaluation_summary=evaluation,
                phase=GenerationPhase.COMPLETED,
            )
        )
        criteria = ConvergenceCriteria(
            min_generations=3,
            eval_gate_enabled=True,
            outcome_gate_enabled=True,
        )

        signal = criteria.evaluate(
            lineage,
            latest_evaluation=evaluation,
            validation_output="Validation error: tests did not collect",
        )

        assert not signal.converged
        assert "Below minimum generations" in signal.reason

    def test_rejected_score_plateau_stops_after_window(self) -> None:
        lineage = self._scored_lineage(0.50, 0.505, 0.504)
        latest = lineage.generations[-1].evaluation_summary
        criteria = ConvergenceCriteria(
            min_generations=2,
            stagnation_window=3,
            evaluation_plateau_epsilon=0.01,
        )

        signal = criteria.evaluate(lineage, latest_evaluation=latest)

        assert signal.should_stop
        assert not signal.converged
        assert "evaluation score plateau" in signal.reason

    def test_meaningful_score_improvement_keeps_running(self) -> None:
        lineage = self._scored_lineage(0.4, 0.5, 0.6)
        latest = lineage.generations[-1].evaluation_summary
        criteria = ConvergenceCriteria(
            min_generations=2,
            stagnation_window=3,
            evaluation_plateau_epsilon=0.01,
        )

        signal = criteria.evaluate(lineage, latest_evaluation=latest)

        assert not signal.converged
        assert "Continuing" in signal.reason

    def test_repetitive_feedback_is_an_explicit_non_success_stop(self) -> None:
        seed = _seed("AC zero", "AC one")
        partial_evaluation = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            score=1.0,
            ac_results=(ACResult(ac_index=0, ac_content="AC zero", passed=True),),
        )
        lineage = _lineage_with_generations(
            *(
                GenerationRecord(
                    generation_number=index,
                    seed_id=f"seed_{index}",
                    ontology_snapshot=schema,
                    wonder_questions=("same unresolved question",),
                    phase=GenerationPhase.COMPLETED,
                )
                for index, schema in enumerate((SCHEMA_A, SCHEMA_B, SCHEMA_C), start=1)
            )
        )

        signal = ConvergenceCriteria(min_generations=2, eval_gate_enabled=True).evaluate(
            lineage,
            latest_wonder=WonderOutput(
                questions=("same unresolved question",),
                ontology_tensions=(),
                should_continue=True,
                reasoning="still unresolved",
            ),
            latest_evaluation=partial_evaluation,
            latest_seed=seed,
        )

        assert signal.should_stop
        assert not signal.converged
        assert signal.reason.startswith("Stagnation detected:")


class TestEvolutionGateDetection:
    """Tests for evolution gate detection (P1-5).

    When the ontology never changes across generations, the system should
    block convergence — whether due to conservative Reflect or errors.
    """

    def test_stops_when_ontology_never_evolved_for_full_window(self) -> None:
        """Identical ontology for the full window stops instead of running to 30."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
        )
        signal = criteria.evaluate(lineage)
        assert signal.should_stop
        assert not signal.converged
        assert "Stagnation" in signal.reason

    def test_allows_when_ontology_evolved_at_least_once(self) -> None:
        """Ontology evolved once then stabilized -> genuine convergence."""
        lineage = _lineage_with_schemas(SCHEMA_B, SCHEMA_A, SCHEMA_A)
        seed = _seed("AC zero")
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=_approved_evaluation(seed),
            latest_seed=seed,
        )
        assert signal.converged
        assert "converged" in signal.reason.lower()

    def test_blocks_two_gen_identical(self) -> None:
        """Two identical generations with no evolution -> blocked."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A)
        seed = _seed("AC zero")
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=_approved_evaluation(seed),
            latest_seed=seed,
        )
        assert not signal.converged
        assert "Convergence withheld" in signal.reason

    def test_max_generations_overrides_withheld_convergence(self) -> None:
        """Hard cap still terminates even with withheld convergence."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=3,
            eval_gate_enabled=False,
        )
        signal = criteria.evaluate(lineage)
        assert signal.should_stop
        assert not signal.converged
        assert "Max generations" in signal.reason


class TestValidationGate:
    """Tests for validation_gate_enabled convergence gating."""

    def _converging_lineage(self) -> OntologyLineage:
        """Create a 3-gen lineage that evolved once then converged (B→A→A)."""
        return _lineage_with_schemas(SCHEMA_B, SCHEMA_A, SCHEMA_A)

    def test_blocks_when_validation_skipped(self) -> None:
        """Validation gate blocks convergence when validation was skipped."""
        lineage = self._converging_lineage()
        seed = _seed("AC zero")
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            validation_output="Validation skipped: no project directory found",
            latest_evaluation=_approved_evaluation(seed),
            latest_seed=seed,
        )
        assert not signal.converged
        assert "Validation gate blocked" in signal.reason

    def test_blocks_when_validation_error(self) -> None:
        """Validation gate blocks convergence when validation had an error."""
        lineage = self._converging_lineage()
        seed = _seed("AC zero")
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            validation_output="Validation error: subprocess failed",
            latest_evaluation=_approved_evaluation(seed),
            latest_seed=seed,
        )
        assert not signal.converged
        assert "Validation gate blocked" in signal.reason

    def test_passes_when_validation_succeeded(self) -> None:
        """Validation gate allows convergence when validation passed."""
        lineage = self._converging_lineage()
        seed = _seed("AC zero")
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            validation_output="Validation passed (attempt 1/3)",
            latest_evaluation=_approved_evaluation(seed),
            latest_seed=seed,
        )
        assert signal.converged

    def test_passes_when_validation_output_none(self) -> None:
        """Validation gate allows convergence when no validation output."""
        lineage = self._converging_lineage()
        seed = _seed("AC zero")
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            validation_output=None,
            latest_evaluation=_approved_evaluation(seed),
            latest_seed=seed,
        )
        assert signal.converged

    @pytest.mark.parametrize("validation_output", (None, "", "   "))
    def test_blocks_when_configured_validator_returns_no_result(
        self,
        validation_output: str | None,
    ) -> None:
        lineage = self._converging_lineage()
        seed = _seed("AC zero")
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=True,
        )

        signal = criteria.evaluate(
            lineage,
            validation_output=validation_output,
            validation_expected=True,
            latest_evaluation=_approved_evaluation(seed),
            latest_seed=seed,
        )

        assert not signal.converged
        assert not signal.should_stop
        assert signal.reason == "Validation gate blocked: configured validator returned no result"

    @pytest.mark.parametrize(
        "validation_output",
        (
            "Validation fix failed (attempt 1): timeout",
            "Validation: no fixable errors detected (exit code 2)",
            "False",
            "Validation passed: false",
            "Validation passed but 3 errors remain",
            "Validation passed\nValidation error: tests failed",
        ),
    )
    def test_blocks_configured_validator_without_explicit_pass(
        self,
        validation_output: str,
    ) -> None:
        lineage = self._converging_lineage()
        seed = _seed("AC zero")

        signal = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=True,
        ).evaluate(
            lineage,
            validation_output=validation_output,
            validation_expected=True,
            latest_evaluation=_approved_evaluation(seed),
            latest_seed=seed,
        )

        assert not signal.converged
        assert not signal.should_stop
        assert signal.reason == f"Validation gate blocked: {validation_output}"

    def test_disabled_allows_skipped_validation(self) -> None:
        """Disabled validation gate allows convergence even with skipped validation."""
        lineage = self._converging_lineage()
        seed = _seed("AC zero")
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=False,
        )
        signal = criteria.evaluate(
            lineage,
            validation_output="Validation skipped: no project directory",
            latest_evaluation=_approved_evaluation(seed),
            latest_seed=seed,
        )
        assert signal.converged
