"""Unit tests for WonderEngine._parse_response and _degraded_output.

Tests the JSON parsing logic and degraded mode heuristics without LLM calls.
"""

from __future__ import annotations

from ouroboros.core.lineage import EvaluationSummary
from ouroboros.core.seed import OntologyField, OntologySchema
from ouroboros.evolution.wonder import WonderEngine


def _make_engine() -> WonderEngine:
    """Create WonderEngine with a dummy adapter (not used in these tests)."""

    class DummyAdapter:
        async def complete(self, messages, config):
            raise NotImplementedError

    return WonderEngine(llm_adapter=DummyAdapter())  # type: ignore[arg-type]


class TestParseResponse:
    """Tests for WonderEngine._parse_response."""

    def test_valid_json(self) -> None:
        engine = _make_engine()
        result = engine._parse_response(
            '{"questions": ["q1", "q2"], "ontology_tensions": ["t1"], '
            '"should_continue": true, "reasoning": "analysis"}'
        )
        assert result.questions == ("q1", "q2")
        assert result.ontology_tensions == ("t1",)
        assert result.should_continue is True
        assert result.reasoning == "analysis"

    def test_json_with_markdown_fences(self) -> None:
        engine = _make_engine()
        result = engine._parse_response(
            '```json\n{"questions": ["q1"], "ontology_tensions": [], '
            '"should_continue": false, "reasoning": "done"}\n```'
        )
        assert result.questions == ("q1",)
        assert result.should_continue is False

    def test_invalid_json_returns_fallback(self) -> None:
        engine = _make_engine()
        result = engine._parse_response("not valid json at all")
        assert len(result.questions) > 0
        assert result.should_continue is True
        assert "Parse error" in result.reasoning

    def test_empty_json_object(self) -> None:
        engine = _make_engine()
        result = engine._parse_response("{}")
        assert result.questions == ()
        assert result.ontology_tensions == ()
        assert result.should_continue is True

    def test_partial_json_missing_fields(self) -> None:
        engine = _make_engine()
        result = engine._parse_response('{"questions": ["only questions"]}')
        assert result.questions == ("only questions",)
        assert result.ontology_tensions == ()
        assert result.should_continue is True

    def test_should_continue_false(self) -> None:
        engine = _make_engine()
        result = engine._parse_response(
            '{"questions": [], "ontology_tensions": [], '
            '"should_continue": false, "reasoning": "ontology is complete"}'
        )
        assert result.should_continue is False


class TestDegradedOutput:
    """Tests for WonderEngine._degraded_output heuristics."""

    def _make_ontology(self, num_fields: int = 5) -> OntologySchema:
        return OntologySchema(
            name="TestOntology",
            description="Test",
            fields=tuple(
                OntologyField(name=f"field_{i}", field_type="string", description=f"Field {i}")
                for i in range(num_fields)
            ),
        )

    def test_no_eval_summary(self) -> None:
        engine = _make_engine()
        result = engine._degraded_output(None, self._make_ontology())
        assert len(result.questions) > 0
        assert result.should_continue is True
        assert "Degraded mode" in result.reasoning

    def test_failed_evaluation(self) -> None:
        engine = _make_engine()
        eval_summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=1,
            failure_reason="Stage 1 failed: lint errors",
        )
        result = engine._degraded_output(eval_summary, self._make_ontology())
        assert any("fundamental" in q.lower() or "missing" in q.lower() for q in result.questions)

    def test_high_drift(self) -> None:
        engine = _make_engine()
        eval_summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            drift_score=0.5,
        )
        result = engine._degraded_output(eval_summary, self._make_ontology())
        assert any("drift" in q.lower() for q in result.questions)
        assert len(result.ontology_tensions) > 0

    def test_sparse_ontology(self) -> None:
        engine = _make_engine()
        eval_summary = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=3,
        )
        result = engine._degraded_output(eval_summary, self._make_ontology(num_fields=2))
        assert any("missing" in q.lower() or "entities" in q.lower() for q in result.questions)

    def test_fully_approved_rich_ontology(self) -> None:
        engine = _make_engine()
        eval_summary = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=3,
            score=0.95,
        )
        result = engine._degraded_output(eval_summary, self._make_ontology(num_fields=10))
        assert result.should_continue is True
        assert len(result.questions) > 0  # always at least a fallback question
