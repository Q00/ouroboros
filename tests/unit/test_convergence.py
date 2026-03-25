"""Unit tests for ConvergenceCriteria — oscillation detection and convergence gating."""

from __future__ import annotations

import pytest

from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    OntologyDelta,
    OntologyLineage,
    TerminationReason,
)
from ouroboros.core.seed import OntologyField, OntologySchema
from ouroboros.evolution.convergence import ConvergenceCriteria, ConvergenceSignal
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
        """Identical ontology across 2 generations -> convergence withheld.

        Uses stagnation_window=4 to avoid stagnation safety firing first.
        """
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            stagnation_window=4,
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


# --- TerminationReason enum tests ---


class TestTerminationReasonEnum:
    """Verify TerminationReason is correctly set for all converged=True paths."""

    def test_converged_true_requires_termination_reason(self) -> None:
        """__post_init__ enforces that converged=True has termination_reason."""
        with pytest.raises(ValueError, match="requires termination_reason"):
            ConvergenceSignal(
                converged=True,
                reason="test",
                ontology_similarity=0.99,
                generation=1,
            )

    def test_converged_false_allows_none(self) -> None:
        """converged=False can have termination_reason=None."""
        signal = ConvergenceSignal(
            converged=False,
            reason="test",
            ontology_similarity=0.5,
            generation=1,
        )
        assert signal.termination_reason is None

    def test_max_generations_returns_exhausted(self) -> None:
        schema_a = _schema(("name",))
        lineage = OntologyLineage(
            lineage_id="test",
            goal="test",
            generations=tuple(
                GenerationRecord(
                    generation_number=i,
                    seed_id=f"s{i}",
                    ontology_snapshot=schema_a,
                )
                for i in range(1, 4)
            ),
        )
        criteria = ConvergenceCriteria(max_generations=3, min_generations=2)
        signal = criteria.evaluate(lineage)
        assert signal.converged
        assert signal.termination_reason == TerminationReason.EXHAUSTED

    def test_ontology_stable_returns_converged(self) -> None:
        # Gen 1: different schema, Gen 2-3: same schema → evolution happened, then stable
        schema_a = _schema(("name",))
        schema_b = _schema(("name", "age"))
        lineage = OntologyLineage(
            lineage_id="test",
            goal="test",
            generations=(
                GenerationRecord(generation_number=1, seed_id="s1", ontology_snapshot=schema_a),
                GenerationRecord(generation_number=2, seed_id="s2", ontology_snapshot=schema_b),
                GenerationRecord(generation_number=3, seed_id="s3", ontology_snapshot=schema_b),
            ),
        )
        criteria = ConvergenceCriteria(convergence_threshold=0.95, min_generations=2)
        signal = criteria.evaluate(lineage)
        assert signal.converged
        assert signal.termination_reason == TerminationReason.CONVERGED

    def test_stagnation_returns_stagnated(self) -> None:
        # Gen 1: different, Gen 2-5: same → evolution gate passes, stagnation detected
        schema_a = _schema(("x",))
        schema_b = _schema(("name", "age"))
        lineage = OntologyLineage(
            lineage_id="test",
            goal="test",
            generations=(
                GenerationRecord(generation_number=1, seed_id="s1", ontology_snapshot=schema_a),
                *(
                    GenerationRecord(
                        generation_number=i,
                        seed_id=f"s{i}",
                        ontology_snapshot=schema_b,
                    )
                    for i in range(2, 6)
                ),
            ),
        )
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            stagnation_window=3,
            min_generations=2,
        )
        signal = criteria.evaluate(lineage)
        assert signal.converged
        assert signal.termination_reason in (
            TerminationReason.CONVERGED,
            TerminationReason.STAGNATED,
        )

    def test_oscillation_returns_oscillated(self) -> None:
        schema_a = _schema(("name",))
        schema_b = _schema(("title",))
        lineage = OntologyLineage(
            lineage_id="test",
            goal="test",
            generations=(
                GenerationRecord(generation_number=1, seed_id="s1", ontology_snapshot=schema_a),
                GenerationRecord(generation_number=2, seed_id="s2", ontology_snapshot=schema_b),
                GenerationRecord(generation_number=3, seed_id="s3", ontology_snapshot=schema_a),
                GenerationRecord(generation_number=4, seed_id="s4", ontology_snapshot=schema_b),
            ),
        )
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            enable_oscillation_detection=True,
        )
        signal = criteria.evaluate(lineage)
        assert signal.converged
        assert signal.termination_reason == TerminationReason.OSCILLATED
        assert "Oscillation" in signal.reason


# --- Ontology Completeness Gate ---


class TestOntologyCompletenessGate:
    """Tests for ontology_completeness_gate in convergence criteria."""

    @staticmethod
    def _stable_lineage(
        fields: tuple[str, ...],
        descriptions: tuple[str, ...] | None = None,
    ) -> OntologyLineage:
        """Create a lineage where the last 2 gens have identical ontology."""
        if descriptions is None:
            descriptions = tuple(f"Description of {f}" for f in fields)
        schema_init = _schema(("initial_different_field",))
        schema = OntologySchema(
            name="Test",
            description="Test schema",
            fields=tuple(
                OntologyField(name=n, field_type="string", description=d, required=True)
                for n, d in zip(fields, descriptions, strict=True)
            ),
        )
        return OntologyLineage(
            lineage_id="test",
            goal="test",
            generations=(
                GenerationRecord(generation_number=1, seed_id="s1", ontology_snapshot=schema_init),
                GenerationRecord(generation_number=2, seed_id="s2", ontology_snapshot=schema),
                GenerationRecord(generation_number=3, seed_id="s3", ontology_snapshot=schema),
            ),
        )

    def test_blocks_when_too_few_fields(self) -> None:
        """Completeness gate blocks when field count < min_fields."""
        lineage = self._stable_lineage(("name", "age"))
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            ontology_completeness_gate_enabled=True,
            ontology_min_fields=3,
        )
        signal = criteria.evaluate(lineage)
        assert not signal.converged
        assert "completeness gate" in signal.reason.lower()
        assert "2 fields" in signal.reason

    def test_blocks_when_trivial_descriptions(self) -> None:
        """Completeness gate blocks when majority of descriptions are trivial."""
        lineage = self._stable_lineage(
            ("name", "age", "email"),
            descriptions=("name", "age", "Detailed email address for contact"),
        )
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            ontology_completeness_gate_enabled=True,
            ontology_min_fields=3,
        )
        signal = criteria.evaluate(lineage)
        assert not signal.converged
        assert "trivial descriptions" in signal.reason.lower()

    def test_passes_when_sufficient_fields_and_descriptions(self) -> None:
        """Completeness gate passes with enough fields and good descriptions."""
        lineage = self._stable_lineage(
            ("name", "age", "email"),
            descriptions=(
                "Full legal name of the person",
                "Age in years since birth",
                "Primary email address for contact",
            ),
        )
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            ontology_completeness_gate_enabled=True,
            ontology_min_fields=3,
        )
        signal = criteria.evaluate(lineage)
        assert signal.converged
        assert signal.termination_reason == TerminationReason.CONVERGED

    def test_disabled_allows_convergence(self) -> None:
        """Disabled completeness gate allows convergence regardless."""
        lineage = self._stable_lineage(("x",))
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            ontology_completeness_gate_enabled=False,
        )
        signal = criteria.evaluate(lineage)
        assert signal.converged

    def test_stagnation_safety_overrides_gate_blocking(self) -> None:
        """When stagnation_window is reached, stagnation terminates even if gate blocks."""
        schema_init = _schema(("different",))
        schema = OntologySchema(
            name="T",
            description="T",
            fields=(OntologyField(name="x", field_type="string", description="x", required=True),),
        )
        # 4 gens: gen1 different, gen2-4 identical → stagnation_window=3 reached
        lineage = OntologyLineage(
            lineage_id="test",
            goal="test",
            generations=(
                GenerationRecord(generation_number=1, seed_id="s1", ontology_snapshot=schema_init),
                GenerationRecord(generation_number=2, seed_id="s2", ontology_snapshot=schema),
                GenerationRecord(generation_number=3, seed_id="s3", ontology_snapshot=schema),
                GenerationRecord(generation_number=4, seed_id="s4", ontology_snapshot=schema),
            ),
        )
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            stagnation_window=3,
            ontology_completeness_gate_enabled=True,
            ontology_min_fields=5,  # would block: only 1 field
        )
        signal = criteria.evaluate(lineage)
        # Stagnation safety should override the completeness gate
        assert signal.converged
        assert signal.termination_reason == TerminationReason.STAGNATED


# --- Wonder Gate ---


class TestWonderGate:
    """Tests for wonder_gate in convergence criteria."""

    @staticmethod
    def _stable_lineage_with_wonder(
        prev_questions: tuple[str, ...] = (),
    ) -> OntologyLineage:
        """Create a stable lineage where gen2-3 have identical ontology."""
        schema_a = _schema(("x",))
        schema_b = _schema(("name", "age", "email"))
        return OntologyLineage(
            lineage_id="test",
            goal="test",
            generations=(
                GenerationRecord(
                    generation_number=1,
                    seed_id="s1",
                    ontology_snapshot=schema_a,
                    wonder_questions=prev_questions,
                ),
                GenerationRecord(
                    generation_number=2,
                    seed_id="s2",
                    ontology_snapshot=schema_b,
                    wonder_questions=prev_questions,
                ),
                GenerationRecord(
                    generation_number=3,
                    seed_id="s3",
                    ontology_snapshot=schema_b,
                    wonder_questions=prev_questions,
                ),
            ),
        )

    def test_blocks_when_novel_questions_exceed_threshold(self) -> None:
        """Wonder gate blocks when majority of questions are novel."""
        lineage = self._stable_lineage_with_wonder(("old question 1", "old question 2"))
        wonder = WonderOutput(
            questions=("brand new question A", "brand new question B", "old question 1"),
            should_continue=True,
        )
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            wonder_gate_enabled=True,
            wonder_novelty_threshold=0.5,
        )
        signal = criteria.evaluate(lineage, latest_wonder=wonder)
        assert not signal.converged
        assert "Wonder gate" in signal.reason
        assert "novel questions" in signal.reason

    def test_blocks_when_latest_gen_has_same_questions(self) -> None:
        """Wonder gate still blocks even when latest generation already contains the questions.

        Regression test: the gate must exclude the latest generation when building
        the set of previously-seen questions, otherwise every question appears
        "already seen" and the gate can never fire.
        """
        schema_a = _schema(("x",))
        schema_b = _schema(("name", "age", "email"))
        novel_qs = ("brand new A", "brand new B")
        # Gen1-2 have old questions; gen3 (latest) has the novel questions
        lineage = OntologyLineage(
            lineage_id="test",
            goal="test",
            generations=(
                GenerationRecord(
                    generation_number=1,
                    seed_id="s1",
                    ontology_snapshot=schema_a,
                    wonder_questions=("old Q1",),
                ),
                GenerationRecord(
                    generation_number=2,
                    seed_id="s2",
                    ontology_snapshot=schema_b,
                    wonder_questions=("old Q2",),
                ),
                GenerationRecord(
                    generation_number=3,
                    seed_id="s3",
                    ontology_snapshot=schema_b,
                    wonder_questions=novel_qs,
                ),
            ),
        )
        wonder = WonderOutput(questions=novel_qs, should_continue=True)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            wonder_gate_enabled=True,
            wonder_novelty_threshold=0.5,
        )
        signal = criteria.evaluate(lineage, latest_wonder=wonder)
        assert not signal.converged
        assert "Wonder gate" in signal.reason

    def test_allows_when_all_questions_are_repeated(self) -> None:
        """Wonder gate allows convergence when all questions are old."""
        prev = ("question A", "question B")
        lineage = self._stable_lineage_with_wonder(prev)
        wonder = WonderOutput(
            questions=("question A", "question B"),
            should_continue=True,
        )
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            wonder_gate_enabled=True,
            wonder_novelty_threshold=0.5,
        )
        signal = criteria.evaluate(lineage, latest_wonder=wonder)
        assert signal.converged

    def test_blocks_when_wonder_is_none(self) -> None:
        """Wonder gate blocks when no wonder output is available.

        "Unable to generate questions" is not the same as "no questions remain".
        When wonder_gate is enabled, absence of wonder output should block
        convergence to avoid false convergence.
        """
        lineage = self._stable_lineage_with_wonder()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            wonder_gate_enabled=True,
        )
        signal = criteria.evaluate(lineage, latest_wonder=None)
        assert not signal.converged
        assert signal.blocking_gate == "wonder"
        assert "unavailable" in signal.reason

    def test_disabled_allows_convergence(self) -> None:
        """Disabled wonder gate allows convergence regardless of novelty."""
        lineage = self._stable_lineage_with_wonder()
        wonder = WonderOutput(
            questions=("completely new question",),
            should_continue=True,
        )
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            wonder_gate_enabled=False,
        )
        signal = criteria.evaluate(lineage, latest_wonder=wonder)
        assert signal.converged


# --- Drift Trend Gate ---


class TestDriftTrendGate:
    """Tests for drift_trend_gate in convergence criteria."""

    @staticmethod
    def _stable_lineage_with_drift(drift_scores: list[float | None]) -> OntologyLineage:
        """Create a stable lineage with specified drift_scores per generation."""
        schema_init = _schema(("different",))
        schema = _schema(("name", "age", "email"))
        gens: list[GenerationRecord] = [
            GenerationRecord(
                generation_number=1,
                seed_id="s1",
                ontology_snapshot=schema_init,
            )
        ]
        for i, ds in enumerate(drift_scores, start=2):
            eval_summary = (
                EvaluationSummary(
                    final_approved=True,
                    highest_stage_passed=2,
                    score=0.8,
                    drift_score=ds,
                )
                if ds is not None
                else None
            )
            gens.append(
                GenerationRecord(
                    generation_number=i,
                    seed_id=f"s{i}",
                    ontology_snapshot=schema,
                    evaluation_summary=eval_summary,
                )
            )
        return OntologyLineage(
            lineage_id="test",
            goal="test",
            generations=tuple(gens),
        )

    def test_blocks_when_drift_monotonically_increasing(self) -> None:
        """Drift trend gate blocks when drift_score increases every generation."""
        lineage = self._stable_lineage_with_drift([0.2, 0.4, 0.6])
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            stagnation_window=5,  # avoid stagnation safety firing first
            drift_trend_gate_enabled=True,
            drift_trend_window=3,
        )
        signal = criteria.evaluate(lineage)
        assert not signal.converged
        assert "Drift trend gate" in signal.reason

    def test_allows_when_drift_decreasing(self) -> None:
        """Drift trend gate allows convergence when drift is improving."""
        lineage = self._stable_lineage_with_drift([0.6, 0.4, 0.2])
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            drift_trend_gate_enabled=True,
            drift_trend_window=3,
        )
        signal = criteria.evaluate(lineage)
        assert signal.converged

    def test_allows_when_drift_mixed(self) -> None:
        """Drift trend gate allows when drift fluctuates (not monotonic)."""
        lineage = self._stable_lineage_with_drift([0.3, 0.5, 0.4])
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            drift_trend_gate_enabled=True,
            drift_trend_window=3,
        )
        signal = criteria.evaluate(lineage)
        assert signal.converged

    def test_passes_when_drift_scores_none(self) -> None:
        """Gate passes when drift_score is None (fewer than 2 valid scores)."""
        lineage = self._stable_lineage_with_drift([None, None, 0.5])
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            drift_trend_gate_enabled=True,
            drift_trend_window=3,
        )
        signal = criteria.evaluate(lineage)
        assert signal.converged

    def test_disabled_allows_convergence(self) -> None:
        """Disabled drift trend gate allows convergence regardless."""
        lineage = self._stable_lineage_with_drift([0.2, 0.4, 0.6])
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            drift_trend_gate_enabled=False,
        )
        signal = criteria.evaluate(lineage)
        assert signal.converged
