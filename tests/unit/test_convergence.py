"""Unit tests for ConvergenceCriteria — oscillation detection and convergence gating."""

from __future__ import annotations

import pytest

from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    OntologyDelta,
    OntologyLineage,
)
from ouroboros.core.seed import OntologyField, OntologySchema
from ouroboros.evolution.convergence import ConvergenceCriteria
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
        assert signal.converged
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
        assert signal.converged
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
        assert signal.converged
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
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=EvaluationSummary(
                final_approved=False, highest_stage_passed=1, score=0.3
            ),
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
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
            eval_min_score=0.7,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=EvaluationSummary(
                final_approved=True, highest_stage_passed=2, score=0.9
            ),
        )
        assert signal.converged

    def test_gate_ignores_when_no_result(self) -> None:
        """Gate enabled but no result provided -> converges normally."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )
        signal = criteria.evaluate(lineage, latest_evaluation=None)
        assert signal.converged

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
        assert signal.converged
        assert "Max generations" in signal.reason

    def test_gate_approved_true_score_none(self) -> None:
        """approved=True + score=None -> convergence allowed (no score to block)."""
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
                final_approved=True, highest_stage_passed=2, score=None
            ),
        )
        assert signal.converged

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


class TestEvolutionGateDetection:
    """Tests for evolution gate detection (P1-5).

    When the ontology never changes across generations, the system should
    block convergence — whether due to conservative Reflect or errors.
    """

    def test_blocks_when_ontology_never_evolved(self) -> None:
        """Identical ontology across all generations -> convergence withheld."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
        )
        signal = criteria.evaluate(lineage)
        assert not signal.converged
        assert "Convergence withheld" in signal.reason

    def test_allows_when_ontology_evolved_at_least_once(self) -> None:
        """Ontology evolved once then stabilized -> genuine convergence."""
        lineage = _lineage_with_schemas(SCHEMA_B, SCHEMA_A, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
        )
        signal = criteria.evaluate(lineage)
        assert signal.converged
        assert "converged" in signal.reason.lower()

    def test_blocks_two_gen_identical(self) -> None:
        """Two identical generations with no evolution -> blocked."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
        )
        signal = criteria.evaluate(lineage)
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
        assert signal.converged
        assert "Max generations" in signal.reason


class TestValidationGate:
    """Tests for validation_gate_enabled convergence gating."""

    def _converging_lineage(self) -> OntologyLineage:
        """Create a 3-gen lineage that evolved once then converged (B→A→A)."""
        return _lineage_with_schemas(SCHEMA_B, SCHEMA_A, SCHEMA_A)

    def test_blocks_when_validation_skipped(self) -> None:
        """Validation gate blocks convergence when validation was skipped."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            validation_output="Validation skipped: no project directory found",
        )
        assert not signal.converged
        assert "Validation gate blocked" in signal.reason

    def test_blocks_when_validation_error(self) -> None:
        """Validation gate blocks convergence when validation had an error."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            validation_output="Validation error: subprocess failed",
        )
        assert not signal.converged
        assert "Validation gate blocked" in signal.reason

    def test_passes_when_validation_succeeded(self) -> None:
        """Validation gate allows convergence when validation passed."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            validation_output="Validation passed: all checks green",
        )
        assert signal.converged

    def test_passes_when_validation_output_none(self) -> None:
        """Validation gate allows convergence when no validation output."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=True,
        )
        signal = criteria.evaluate(lineage, validation_output=None)
        assert signal.converged

    def test_disabled_allows_skipped_validation(self) -> None:
        """Disabled validation gate allows convergence even with skipped validation."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=False,
        )
        signal = criteria.evaluate(
            lineage,
            validation_output="Validation skipped: no project directory",
        )
        assert signal.converged
