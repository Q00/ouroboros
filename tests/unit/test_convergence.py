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


# -- Feature 2: Convergence Gating via Evaluation --


class TestConvergenceGating:
    """Tests for eval_gate_enabled convergence gating."""

    def _converging_lineage(self) -> OntologyLineage:
        """Create a 2-gen lineage where latest two are identical (similarity=1.0)."""
        return _lineage_with_schemas(SCHEMA_A, SCHEMA_A)

    def test_gate_disabled_by_default(self) -> None:
        """Default config: gate disabled, convergence proceeds normally."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
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
