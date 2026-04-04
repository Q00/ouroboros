"""Tests for Wonder scope guard and degraded-mode convergence."""

from ouroboros.core.lineage import EvaluationSummary, FeedbackMetadata, OntologyLineage
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.evolution.wonder import WonderEngine

_ONTOLOGY = OntologySchema(
    name="login",
    description="Login system ontology",
    fields=(
        OntologyField(name="user", field_type="entity", description="A user"),
        OntologyField(name="session", field_type="entity", description="A session"),
        OntologyField(name="provider", field_type="enum", description="OAuth provider"),
    ),
)


def _make_seed() -> Seed:
    return Seed(
        metadata=SeedMetadata(ambiguity_score=0.1),
        goal="Build a login system",
        constraints=("Must use OAuth",),
        acceptance_criteria=("User can log in via Google",),
        ontology_schema=_ONTOLOGY,
        evaluation_principles=(
            EvaluationPrinciple(name="completeness", description="All requirements met"),
        ),
        exit_conditions=(
            ExitCondition(
                name="done",
                description="All ACs pass",
                evaluation_criteria="100% pass",
            ),
        ),
    )


def _make_approved_summary() -> EvaluationSummary:
    return EvaluationSummary(
        final_approved=True,
        highest_stage_passed=3,
        score=0.95,
        drift_score=0.05,
    )


def _make_failed_summary() -> EvaluationSummary:
    return EvaluationSummary(
        final_approved=False,
        highest_stage_passed=2,
        score=0.4,
        drift_score=0.6,
        failure_reason="1/1 ACs failed",
    )


class TestWonderDegradedModeConvergence:
    """Degraded mode must allow convergence when evaluation passed."""

    def test_degraded_mode_converges_when_evaluation_approved(self) -> None:
        """should_continue=False when eval approved and no in-scope gaps."""
        from unittest.mock import AsyncMock

        engine = WonderEngine(llm_adapter=AsyncMock(), model="test")
        seed = _make_seed()
        summary = _make_approved_summary()

        output = engine._degraded_output(summary, seed.ontology_schema, seed)

        assert output.should_continue is False
        assert len(output.questions) == 0

    def test_degraded_mode_continues_when_evaluation_failed(self) -> None:
        """should_continue=True when eval failed — there are gaps to address."""
        from unittest.mock import AsyncMock

        engine = WonderEngine(llm_adapter=AsyncMock(), model="test")
        seed = _make_seed()
        summary = _make_failed_summary()

        output = engine._degraded_output(summary, seed.ontology_schema, seed)

        assert output.should_continue is True
        assert len(output.questions) > 0

    def test_degraded_mode_questions_are_seed_scoped(self) -> None:
        """Fallback questions must reference the seed goal, not generic domain."""
        from unittest.mock import AsyncMock

        engine = WonderEngine(llm_adapter=AsyncMock(), model="test")
        seed = _make_seed()
        summary = _make_failed_summary()

        output = engine._degraded_output(summary, seed.ontology_schema, seed)

        for q in output.questions:
            # No generic "what are we not modeling?" questions
            assert "aspects of this domain" not in q.lower()

    def test_prompt_includes_structured_feedback_signals(self) -> None:
        """Prompt context should surface evaluation feedback canaries to Wonder."""
        from unittest.mock import AsyncMock

        engine = WonderEngine(llm_adapter=AsyncMock(), model="test")
        seed = _make_seed()
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            score=0.4,
            drift_score=0.6,
            feedback_metadata=(
                FeedbackMetadata(
                    code="decomposition_depth_warning",
                    severity="warning",
                    message="Depth safety net forced atomic execution.",
                    source="parallel_executor",
                    details={"max_depth": 3, "affected_count": 2},
                ),
            ),
        )

        prompt = engine._build_prompt(
            seed.ontology_schema,
            summary,
            execution_output=None,
            lineage=OntologyLineage(lineage_id="lineage-1", goal=seed.goal),
            seed=seed,
        )

        assert "Feedback Signals" in prompt
        assert "decomposition_depth_warning" in prompt
        assert "max_depth=3" in prompt


class TestWonderParseResponseFallback:
    """Parse error fallback must be seed-scoped."""

    def test_malformed_json_fallback_is_seed_scoped(self) -> None:
        """Parse failure should produce seed-scoped question, not generic."""
        from unittest.mock import AsyncMock

        engine = WonderEngine(llm_adapter=AsyncMock(), model="test")
        seed = _make_seed()

        output = engine._parse_response("not valid json at all", seed)

        assert len(output.questions) == 1
        assert (
            "login system" in output.questions[0].lower() or "goal" in output.questions[0].lower()
        )
        assert "aspects of this domain" not in output.questions[0].lower()

    def test_malformed_json_fallback_without_seed(self) -> None:
        """Parse failure without seed still produces a question."""
        from unittest.mock import AsyncMock

        engine = WonderEngine(llm_adapter=AsyncMock(), model="test")

        output = engine._parse_response("not valid json", None)

        assert len(output.questions) == 1
        assert output.should_continue is True
