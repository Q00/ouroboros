"""Unit tests for ConvergenceCriteria — oscillation detection and convergence gating."""

from __future__ import annotations

import pytest

from ouroboros.core.lineage import (
    ACResult,
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    LineageStatus,
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


def _satisfied_wonder() -> WonderOutput:
    """Wonder output that confirms 'no remaining gap'.

    Convergence requires positive Wonder evidence (not just absence) before
    any stability/idea-contract branch can fire. Tests that exercise those
    positive paths must hand in an explicit satisfied Wonder.
    """
    return WonderOutput(
        questions=(),
        ontology_tensions=(),
        should_continue=False,
        reasoning="Test: no remaining gap.",
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
        """A,B,A,B pattern (4 gens, both half-periods verified) -> stagnation route."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_B, SCHEMA_A, SCHEMA_B)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=30,
        )
        signal = criteria.evaluate(lineage)
        assert not signal.converged
        assert "Oscillation" in signal.reason

    def test_oscillation_period2_partial_3gens(self) -> None:
        """A,B,A pattern (3 gens, simple N~N-2 check) -> stagnation route."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_B, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=30,
        )
        signal = criteria.evaluate(lineage)
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
            ),
        )
        loop._run_generation = AsyncMock(return_value=Result.ok(gen_result))

        result = await loop.evolve_step("lin_osc")
        assert result.is_ok
        assert result.value.action == StepAction.STAGNATED
        assert result.value.lineage.status == LineageStatus.STAGNATED

        from ouroboros.evolution.projector import LineageProjector

        projected = LineageProjector().project(await store.replay_lineage("lin_osc"))
        assert projected is not None
        assert projected.status == LineageStatus.STAGNATED

    def test_loop_routes_repetitive_feedback_reason_to_stagnated(self) -> None:
        """Repetitive Wonder feedback should use the same STAGNATED route."""
        from ouroboros.evolution.loop import EvolutionaryLoop

        lineage = _lineage_with_generations(
            GenerationRecord(
                generation_number=1,
                seed_id="seed_1",
                ontology_snapshot=SCHEMA_A,
                phase=GenerationPhase.COMPLETED,
                wonder_questions=("same question", "same gap"),
            ),
            GenerationRecord(
                generation_number=2,
                seed_id="seed_2",
                ontology_snapshot=SCHEMA_B,
                phase=GenerationPhase.COMPLETED,
                wonder_questions=("same question", "same gap"),
            ),
            GenerationRecord(
                generation_number=3,
                seed_id="seed_3",
                ontology_snapshot=SCHEMA_C,
                phase=GenerationPhase.COMPLETED,
                wonder_questions=("different question",),
            ),
        )
        latest_wonder = WonderOutput(
            questions=("same question", "same gap"),
            ontology_tensions=(),
            should_continue=True,
            reasoning="Repeated unresolved gap",
        )
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=30,
        )

        signal = criteria.evaluate(lineage, latest_wonder=latest_wonder)

        assert not signal.converged
        assert "Repetitive feedback" in signal.reason
        assert EvolutionaryLoop._is_stagnation_route(signal.reason)


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
            latest_wonder=_satisfied_wonder(),
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
        assert "final approval" in signal.reason

    def test_gate_failure_stagnates_when_ontology_is_stalled(self) -> None:
        """Repeated unchanged ontology plus failing evaluation should terminate as stagnated."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)
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
        assert "Stagnation detected" in signal.reason
        assert "Evaluation gate: final approval is false" in signal.reason

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
        assert "score" in signal.reason

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
            latest_wonder=_satisfied_wonder(),
            latest_evaluation=EvaluationSummary(
                final_approved=True, highest_stage_passed=2, score=0.9
            ),
        )
        assert signal.converged

    def test_gate_blocks_when_no_result(self) -> None:
        """Gate enabled but no result provided -> stability does not converge."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )
        signal = criteria.evaluate(lineage, latest_evaluation=None)
        assert not signal.converged
        assert "no evaluation summary" in signal.reason

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
            latest_wonder=_satisfied_wonder(),
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
        assert "final approval" in signal.reason

    def test_gate_blocks_when_ac_fails_before_ontology_signal(self) -> None:
        """Gate enabled + failing AC -> converged=False even with stable ontology."""
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
                final_approved=True,
                highest_stage_passed=2,
                score=0.9,
                ac_results=(
                    ACResult(ac_index=0, ac_content="Works", passed=True),
                    ACResult(ac_index=1, ac_content="Still works", passed=False),
                ),
            ),
        )
        assert not signal.converged
        assert "Per-AC gate" in signal.reason
        assert signal.failed_acs == (1,)

    def test_gate_blocks_when_drift_high(self) -> None:
        """Gate enabled + drift_score > 0.30 -> converged=False."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=EvaluationSummary(
                final_approved=True,
                highest_stage_passed=2,
                score=0.9,
                drift_score=0.31,
            ),
        )
        assert not signal.converged
        assert "drift score" in signal.reason

    def test_gate_blocks_when_reward_hacking_risk_high(self) -> None:
        """Gate enabled + reward_hacking_risk > 0.30 -> converged=False."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            latest_evaluation=EvaluationSummary(
                final_approved=True,
                highest_stage_passed=2,
                score=0.9,
                reward_hacking_risk=0.31,
            ),
        )
        assert not signal.converged
        assert "reward hacking risk" in signal.reason

    def test_regression_stagnates_when_ontology_is_stalled(self) -> None:
        """Repeated unchanged ontology plus AC regression should terminate as stagnated."""
        lineage = _lineage_with_generations(
            GenerationRecord(
                generation_number=1,
                seed_id="seed_1",
                ontology_snapshot=SCHEMA_A,
                phase=GenerationPhase.COMPLETED,
                evaluation_summary=EvaluationSummary(
                    final_approved=True,
                    highest_stage_passed=2,
                    ac_results=(ACResult(ac_index=0, ac_content="Works", passed=True),),
                ),
            ),
            GenerationRecord(
                generation_number=2,
                seed_id="seed_2",
                ontology_snapshot=SCHEMA_A,
                phase=GenerationPhase.COMPLETED,
                evaluation_summary=EvaluationSummary(
                    final_approved=True,
                    highest_stage_passed=2,
                    ac_results=(ACResult(ac_index=0, ac_content="Works", passed=True),),
                ),
            ),
            GenerationRecord(
                generation_number=3,
                seed_id="seed_3",
                ontology_snapshot=SCHEMA_A,
                phase=GenerationPhase.COMPLETED,
                evaluation_summary=EvaluationSummary(
                    final_approved=False,
                    highest_stage_passed=2,
                    ac_results=(ACResult(ac_index=0, ac_content="Works", passed=False),),
                ),
            ),
        )
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
            regression_gate_enabled=True,
        )

        signal = criteria.evaluate(lineage)

        assert not signal.converged
        assert "Stagnation detected" in signal.reason
        assert "Regression detected" in signal.reason
        assert signal.failed_acs == (0,)


class TestZeroMutationConvergence:
    """Tests for Idea-first convergence without mandatory ontology mutation."""

    def test_allows_when_ontology_never_evolved_and_contract_passes(self) -> None:
        """Identical ontology across generations can converge when evaluation passes."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            latest_wonder=_satisfied_wonder(),
            latest_evaluation=EvaluationSummary(
                final_approved=True,
                highest_stage_passed=3,
                score=0.95,
                drift_score=0.0,
                reward_hacking_risk=0.0,
            ),
        )
        assert signal.converged
        assert "Idea contract converged" in signal.reason

    def test_allows_when_ontology_evolved_at_least_once(self) -> None:
        """Ontology evolved once then stabilized -> genuine convergence."""
        lineage = _lineage_with_schemas(SCHEMA_B, SCHEMA_A, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
        )
        signal = criteria.evaluate(lineage, latest_wonder=_satisfied_wonder())
        assert signal.converged
        assert "converged" in signal.reason.lower()

    def test_eval_gate_disabled_preserves_stability_convergence(self) -> None:
        """Disabling the eval gate keeps old stability-based convergence semantics."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            max_generations=30,
            eval_gate_enabled=False,
        )

        signal = criteria.evaluate(lineage, latest_wonder=_satisfied_wonder())

        assert signal.converged
        assert "Ontology converged" in signal.reason

    def test_allows_two_gen_identical_when_contract_passes(self) -> None:
        """Two identical generations with no evolution can converge."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            latest_wonder=_satisfied_wonder(),
            latest_evaluation=EvaluationSummary(
                final_approved=True, highest_stage_passed=3, score=0.95
            ),
        )
        assert signal.converged
        assert "Idea contract converged" in signal.reason

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

    def test_stagnates_when_validation_blocks_stalled_ontology(self) -> None:
        """Repeated unchanged ontology plus validation failure should terminate as stagnated."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)
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
        assert "Stagnation detected" in signal.reason
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
            latest_wonder=_satisfied_wonder(),
            validation_output="Validation passed: all checks green",
        )
        assert signal.converged

    def test_allows_non_code_validation_skip_when_contract_passes(self) -> None:
        """Non-code tasks can converge when only code validation was skipped."""
        lineage = self._converging_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
            validation_gate_enabled=True,
        )
        signal = criteria.evaluate(
            lineage,
            latest_wonder=_satisfied_wonder(),
            latest_evaluation=EvaluationSummary(
                final_approved=True,
                highest_stage_passed=3,
                score=0.95,
                drift_score=0.0,
                reward_hacking_risk=0.0,
            ),
            validation_output="Validation skipped: task_type=analysis does not require code validation",
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
        signal = criteria.evaluate(
            lineage,
            latest_wonder=_satisfied_wonder(),
            validation_output=None,
        )
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
            latest_wonder=_satisfied_wonder(),
            validation_output="Validation skipped: no project directory",
        )
        assert signal.converged


class TestValidationGateRealValidatorOutputs:
    """Validation gate must block on every failure-y string the real MCP
    validator emits, not only "skipped"/"error".

    Regression for PR #497 sixth-round review: the actual validator
    (src/ouroboros/mcp/server/adapter.py) returns strings like
    "Validation fix failed ...", "Validation: no fixable errors detected ...",
    and "Validation: N errors remain after attempts ..." when validation
    fails. The previous gate matched only "skipped" or "error" substrings,
    so "Validation fix failed (attempt 2): ..." (which contains neither)
    silently bypassed the gate and could converge on a failed run.
    """

    def _stable_lineage(self) -> OntologyLineage:
        return _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)

    def _criteria(self) -> ConvergenceCriteria:
        return ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            validation_gate_enabled=True,
        )

    def test_blocks_when_validation_fix_failed(self) -> None:
        """`Validation fix failed (attempt N): ...` must block convergence."""
        signal = self._criteria().evaluate(
            self._stable_lineage(),
            latest_wonder=_satisfied_wonder(),
            validation_output="Validation fix failed (attempt 2): subprocess timeout",
        )
        assert not signal.converged
        assert "Validation gate blocked" in signal.reason

    def test_blocks_when_no_fixable_errors(self) -> None:
        """`Validation: no fixable errors detected ...` must block convergence.

        This case happens to also contain the substring "errors" so the
        previous matcher caught it, but this test pins it explicitly so a
        future refactor doesn't drop the coverage.
        """
        signal = self._criteria().evaluate(
            self._stable_lineage(),
            latest_wonder=_satisfied_wonder(),
            validation_output="Validation: no fixable errors detected (exit code 1)",
        )
        assert not signal.converged
        assert "Validation gate blocked" in signal.reason

    def test_blocks_when_errors_remain_after_attempts(self) -> None:
        """`Validation: N errors remain after N attempts. Remaining: ...` blocks."""
        signal = self._criteria().evaluate(
            self._stable_lineage(),
            latest_wonder=_satisfied_wonder(),
            validation_output=(
                "Validation: 3 errors remain after 2 attempts. "
                "Remaining: missing module foo, missing module bar"
            ),
        )
        assert not signal.converged
        assert "Validation gate blocked" in signal.reason

    def test_passes_after_successful_fix_attempts(self) -> None:
        """`Validation passed after N fix attempts` must NOT block (real success)."""
        signal = self._criteria().evaluate(
            self._stable_lineage(),
            latest_wonder=_satisfied_wonder(),
            validation_output="Validation passed after 2 fix attempts",
        )
        assert signal.converged


class TestIdeaContractWonderNoGap:
    """Tests for the Idea-contract / Wonder fast-path convergence signal."""

    def _evolved_lineage(self) -> OntologyLineage:
        """Lineage that evolved once (B→A) and then drifted (A→C) — no stable pair."""
        return _lineage_with_schemas(SCHEMA_B, SCHEMA_A, SCHEMA_C)

    def _passing_evaluation(self) -> EvaluationSummary:
        return EvaluationSummary(
            final_approved=True,
            highest_stage_passed=3,
            score=0.95,
            drift_score=0.0,
            reward_hacking_risk=0.0,
        )

    def test_converges_when_wonder_has_no_gap_and_contract_passes(self) -> None:
        """should_continue=False with no questions/tensions converges immediately."""
        lineage = self._evolved_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )
        wonder = WonderOutput(
            questions=(),
            ontology_tensions=(),
            should_continue=False,
            reasoning="Contract satisfied; no remaining ontological gap.",
        )

        signal = criteria.evaluate(
            lineage,
            latest_wonder=wonder,
            latest_evaluation=self._passing_evaluation(),
        )

        assert signal.converged
        assert "Idea contract converged" in signal.reason

    def test_does_not_converge_when_wonder_has_questions_despite_should_stop(self) -> None:
        """Contradictory Wonder (stop=False but with questions) must NOT converge here.

        Mirrors the loop.py override: should_continue=False with non-empty
        questions represents unresolved gaps, so the loop continues to Reflect.
        Convergence must align with that decision.
        """
        lineage = self._evolved_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )
        wonder = WonderOutput(
            questions=("What about edge case X?",),
            ontology_tensions=(),
            should_continue=False,
            reasoning="Contradictory: said stop but still has questions.",
        )

        signal = criteria.evaluate(
            lineage,
            latest_wonder=wonder,
            latest_evaluation=self._passing_evaluation(),
        )

        assert not signal.converged
        assert "Idea contract converged" not in signal.reason

    def test_does_not_converge_when_wonder_has_tensions_despite_should_stop(self) -> None:
        """Tensions also block the Idea-contract fast path even if should_continue is False."""
        lineage = self._evolved_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )
        wonder = WonderOutput(
            questions=(),
            ontology_tensions=("Field A contradicts field B.",),
            should_continue=False,
            reasoning="Has unresolved tension.",
        )

        signal = criteria.evaluate(
            lineage,
            latest_wonder=wonder,
            latest_evaluation=self._passing_evaluation(),
        )

        assert not signal.converged
        assert "Idea contract converged" not in signal.reason


class TestStabilityBranchRequiresWonderEvidence:
    """Stability/Idea-contract convergence must NOT fire without Wonder evidence
    when the loop has a WonderEngine wired in (`wonder_required=True`).

    Regression for PR #497 fourth-round review: when Wonder degrades, the loop
    swallows the error and returns wonder_output=None ([loop.py:1134]). With
    `wonder_has_gap` previously derived only from `should_continue`, a None
    Wonder was indistinguishable from "no gap", so the stability branches
    could declare a terminal CONVERGED/Idea-contract result with zero Wonder
    evidence.

    Loops without a WonderEngine (`wonder_required=False`) keep the legacy
    permissive behavior — see `TestWonderOptionalLoops` below — so this guard
    does not regress the constructor contract that allows
    `wonder_engine=None`.
    """

    def _stable_lineage(self) -> OntologyLineage:
        return _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)

    def _passing_evaluation(self) -> EvaluationSummary:
        return EvaluationSummary(
            final_approved=True,
            highest_stage_passed=3,
            score=0.95,
            drift_score=0.0,
            reward_hacking_risk=0.0,
        )

    def test_idea_first_stability_blocked_without_wonder(self) -> None:
        """eval_gate on + stable ontology + Wonder=None must NOT converge."""
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
            wonder_required=True,
        )

        signal = criteria.evaluate(
            self._stable_lineage(),
            latest_wonder=None,
            latest_evaluation=self._passing_evaluation(),
        )

        assert not signal.converged
        assert "Idea contract converged" not in signal.reason
        assert "stable ontology lens" not in signal.reason

    def test_legacy_stability_blocked_without_wonder(self) -> None:
        """eval_gate off + stable ontology + Wonder=None must NOT converge.

        The loop's degraded-Wonder path applies symmetrically to the legacy
        stability branch — without positive Wonder evidence, "Ontology
        converged" is not safe to declare.
        """
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
            wonder_required=True,
        )

        signal = criteria.evaluate(self._stable_lineage(), latest_wonder=None)

        assert not signal.converged
        assert "Ontology converged" not in signal.reason


class TestWonderOptionalLoops:
    """Loops without a WonderEngine still converge on stability + gates.

    The EvolutionaryLoop constructor accepts `wonder_engine=None`, so
    convergence cannot mandate Wonder evidence in those configurations.
    `wonder_required=False` (default) keeps Wonder=None permissive while
    every other gate still applies.
    """

    def test_stability_converges_without_wonder_when_not_required(self) -> None:
        """No Wonder + stable ontology + eval gate off -> legacy convergence."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
            wonder_required=False,
        )

        signal = criteria.evaluate(lineage, latest_wonder=None)

        assert signal.converged
        assert "Ontology converged" in signal.reason

    def test_idea_first_converges_without_wonder_when_not_required(self) -> None:
        """No Wonder + eval contract passes -> Idea-contract stability path fires."""
        lineage = _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
            wonder_required=False,
        )

        signal = criteria.evaluate(
            lineage,
            latest_wonder=None,
            latest_evaluation=EvaluationSummary(
                final_approved=True,
                highest_stage_passed=3,
                score=0.95,
                drift_score=0.0,
                reward_hacking_risk=0.0,
            ),
        )

        assert signal.converged
        assert "stable ontology lens" in signal.reason


class TestStabilityBranchHonorsWonderGaps:
    """Stability convergence (similarity >= threshold) must respect Wonder gaps.

    Regression for PR #497 second-round review: previously `wonder_has_gap` was
    derived only from `should_continue`, so a stable ontology plus passing
    evaluation could terminate as converged even when Wonder was still
    surfacing questions or ontology_tensions.
    """

    def _stable_lineage(self) -> OntologyLineage:
        return _lineage_with_schemas(SCHEMA_A, SCHEMA_A, SCHEMA_A)

    def _passing_evaluation(self) -> EvaluationSummary:
        return EvaluationSummary(
            final_approved=True,
            highest_stage_passed=3,
            score=0.95,
            drift_score=0.0,
            reward_hacking_risk=0.0,
        )

    def test_stable_ontology_with_lingering_questions_does_not_converge(self) -> None:
        """should_continue=False but with questions must keep stability branch closed."""
        lineage = self._stable_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )
        wonder = WonderOutput(
            questions=("Is field Z still ambiguous?",),
            ontology_tensions=(),
            should_continue=False,
            reasoning="Stop requested but unresolved questions remain.",
        )

        signal = criteria.evaluate(
            lineage,
            latest_wonder=wonder,
            latest_evaluation=self._passing_evaluation(),
        )

        assert not signal.converged
        assert "stable ontology lens" not in signal.reason
        assert "Idea contract converged" not in signal.reason

    def test_stable_ontology_with_lingering_tensions_does_not_converge(self) -> None:
        """Lingering ontology_tensions must also block the stability branch."""
        lineage = self._stable_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )
        wonder = WonderOutput(
            questions=(),
            ontology_tensions=("Field A contradicts field B.",),
            should_continue=False,
            reasoning="Tension unresolved.",
        )

        signal = criteria.evaluate(
            lineage,
            latest_wonder=wonder,
            latest_evaluation=self._passing_evaluation(),
        )

        assert not signal.converged
        assert "stable ontology lens" not in signal.reason
        assert "Idea contract converged" not in signal.reason

    def test_stable_ontology_with_lingering_questions_blocks_eval_off_path(self) -> None:
        """Even with eval gate off, stability branch must honor unresolved Wonder gaps."""
        lineage = self._stable_lineage()
        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=False,
        )
        wonder = WonderOutput(
            questions=("Is field Z still ambiguous?",),
            ontology_tensions=(),
            should_continue=False,
            reasoning="Stop requested but unresolved questions remain.",
        )

        signal = criteria.evaluate(lineage, latest_wonder=wonder)

        assert not signal.converged
        assert "Ontology converged" not in signal.reason


class TestWonderFastPathLoopIntegration:
    """Loop short-circuit on Wonder no-gap must produce a convergeable result.

    Regression for the dead-lock identified in PR #497 review:
    - loop.py:1092 returns a completed generation early when Wonder has no gap.
    - Previously that result carried no evaluation_summary, so convergence with
      eval_gate_enabled rejected it as "no evaluation summary available", and the
      lineage looped until stagnation/max_generations.
    The fix carries prev_gen.evaluation_summary forward on the fast path.
    """

    def test_fast_path_carries_validation_output_so_validation_gate_still_blocks(
        self,
    ) -> None:
        """Fast-path GenerationResult must forward validation_output so the
        validation gate cannot be silently bypassed.

        Regression for PR #497 third-round review: previously the fast path
        forwarded only evaluation_summary and execution_output, leaving
        validation_output=None. Convergence's validation gate only fires when
        validation_output is non-None, so a lineage with a failed/skipped
        validation could converge as soon as Wonder said "no gap". The fix
        carries the prior generation's validation_output forward as well.
        """
        from ouroboros.core.seed import (
            EvaluationPrinciple,
            ExitCondition,
            Seed,
            SeedMetadata,
        )
        from ouroboros.evolution.loop import GenerationResult

        passing_eval = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=3,
            score=0.95,
            drift_score=0.0,
            reward_hacking_risk=0.0,
        )
        # Previous generation completed but its validation was skipped/errored.
        bad_validation = "Validation skipped: subprocess failed"

        prev_gen = GenerationRecord(
            generation_number=1,
            seed_id="seed_1",
            ontology_snapshot=SCHEMA_A,
            phase=GenerationPhase.COMPLETED,
            evaluation_summary=passing_eval,
            validation_output=bad_validation,
        )
        current_gen = GenerationRecord(
            generation_number=2,
            seed_id="seed_2",
            ontology_snapshot=SCHEMA_A,
            phase=GenerationPhase.COMPLETED,
            evaluation_summary=passing_eval,
            validation_output=bad_validation,
        )
        lineage = _lineage_with_generations(prev_gen, current_gen)

        seed = Seed(
            goal="test",
            task_type="code",
            constraints=("Python",),
            acceptance_criteria=("Works",),
            ontology_schema=SCHEMA_A,
            evaluation_principles=(EvaluationPrinciple(name="c", description="c", weight=1.0),),
            exit_conditions=(ExitCondition(name="e", description="e", evaluation_criteria="e"),),
            metadata=SeedMetadata(seed_id="seed_2", parent_seed_id="seed_1", ambiguity_score=0.1),
        )

        wonder_no_gap = WonderOutput(
            questions=(),
            ontology_tensions=(),
            should_continue=False,
            reasoning="Contract satisfied per evaluation; nothing more to learn.",
        )

        # Fast-path GenerationResult must forward validation_output too.
        fast_path_result = GenerationResult(
            generation_number=2,
            seed=seed,
            execution_output="prev output",
            evaluation_summary=passing_eval,
            validation_output=bad_validation,
            wonder_output=wonder_no_gap,
            phase=GenerationPhase.COMPLETED,
            success=True,
        )

        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
            validation_gate_enabled=True,
        )

        signal = criteria.evaluate(
            lineage,
            latest_wonder=fast_path_result.wonder_output,
            latest_evaluation=fast_path_result.evaluation_summary,
            validation_output=fast_path_result.validation_output,
        )

        # Validation gate must block convergence even though eval and Wonder
        # both signal "done".
        assert not signal.converged
        assert "Validation gate blocked" in signal.reason
        assert "Idea contract converged" not in signal.reason

    def test_fast_path_result_inherits_prev_evaluation_for_convergence(self) -> None:
        """Carry-forward evaluation summary makes Idea-contract convergence reachable."""
        from ouroboros.core.seed import (
            EvaluationPrinciple,
            ExitCondition,
            Seed,
            SeedMetadata,
        )
        from ouroboros.evolution.loop import GenerationResult

        passing_eval = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=3,
            score=0.95,
            drift_score=0.0,
            reward_hacking_risk=0.0,
        )

        prev_gen = GenerationRecord(
            generation_number=1,
            seed_id="seed_1",
            ontology_snapshot=SCHEMA_A,
            phase=GenerationPhase.COMPLETED,
            evaluation_summary=passing_eval,
        )
        current_gen = GenerationRecord(
            generation_number=2,
            seed_id="seed_2",
            ontology_snapshot=SCHEMA_A,
            phase=GenerationPhase.COMPLETED,
            evaluation_summary=passing_eval,
        )
        lineage = _lineage_with_generations(prev_gen, current_gen)

        seed = Seed(
            goal="test",
            task_type="code",
            constraints=("Python",),
            acceptance_criteria=("Works",),
            ontology_schema=SCHEMA_A,
            evaluation_principles=(EvaluationPrinciple(name="c", description="c", weight=1.0),),
            exit_conditions=(ExitCondition(name="e", description="e", evaluation_criteria="e"),),
            metadata=SeedMetadata(seed_id="seed_2", parent_seed_id="seed_1", ambiguity_score=0.1),
        )

        wonder_no_gap = WonderOutput(
            questions=(),
            ontology_tensions=(),
            should_continue=False,
            reasoning="Contract satisfied; nothing more to learn.",
        )

        # Fast-path GenerationResult as produced by loop.py after the fix.
        fast_path_result = GenerationResult(
            generation_number=2,
            seed=seed,
            execution_output="prev output",
            evaluation_summary=passing_eval,
            wonder_output=wonder_no_gap,
            phase=GenerationPhase.COMPLETED,
            success=True,
        )

        criteria = ConvergenceCriteria(
            convergence_threshold=0.95,
            min_generations=2,
            eval_gate_enabled=True,
        )

        signal = criteria.evaluate(
            lineage,
            latest_wonder=fast_path_result.wonder_output,
            latest_evaluation=fast_path_result.evaluation_summary,
        )

        assert signal.converged
        assert "Idea contract converged" in signal.reason
