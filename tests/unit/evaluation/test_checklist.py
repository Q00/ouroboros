"""Unit tests for the per-AC checklist aggregator (#366)."""

import pytest

from ouroboros.evaluation.checklist import (
    ACCheckItem,
    ACChecklistResult,
    aggregate_results,
    build_run_feedback,
    format_checklist,
)
from ouroboros.evaluation.models import (
    CheckResult,
    CheckType,
    EvaluationResult,
    MechanicalResult,
    SemanticResult,
)


def _build_semantic_result(
    *,
    score: float,
    ac_compliance: bool,
    goal_alignment: float,
    drift_score: float,
    uncertainty: float,
    reasoning: str,
    evidence: tuple[str, ...] = (),
    questions_used: tuple[str, ...] = (),
) -> SemanticResult:
    """Build a SemanticResult, tolerating codebases without #367 fields.

    This keeps the checklist tests runnable both on branches where
    ``questions_used``/``evidence`` already exist on ``SemanticResult``
    (PR #383) and on branches where they do not (plain main).
    """
    kwargs = {
        "score": score,
        "ac_compliance": ac_compliance,
        "goal_alignment": goal_alignment,
        "drift_score": drift_score,
        "uncertainty": uncertainty,
        "reasoning": reasoning,
    }
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(SemanticResult)}
    if "evidence" in field_names:
        kwargs["evidence"] = evidence
    if "questions_used" in field_names:
        kwargs["questions_used"] = questions_used
    return SemanticResult(**kwargs)


def _passing_evaluation(
    *,
    execution_id: str = "exec",
    reasoning: str = "",
    evidence: tuple[str, ...] = (),
    questions_used: tuple[str, ...] = (),
) -> EvaluationResult:
    """Build an EvaluationResult that passes end-to-end."""
    return EvaluationResult(
        execution_id=execution_id,
        stage1_result=MechanicalResult(
            passed=True,
            checks=(
                CheckResult(
                    check_type=CheckType.LINT,
                    passed=True,
                    message="ok",
                ),
            ),
        ),
        stage2_result=_build_semantic_result(
            score=0.9,
            ac_compliance=True,
            goal_alignment=0.95,
            drift_score=0.05,
            uncertainty=0.1,
            reasoning=reasoning,
            evidence=evidence,
            questions_used=questions_used,
        ),
        final_approved=True,
    )


def _failing_evaluation(
    *,
    execution_id: str = "exec",
    reasoning: str = "Stage 2 found a gap",
) -> EvaluationResult:
    """Build an EvaluationResult that fails at Stage 2."""
    return EvaluationResult(
        execution_id=execution_id,
        stage2_result=_build_semantic_result(
            score=0.4,
            ac_compliance=False,
            goal_alignment=0.5,
            drift_score=0.5,
            uncertainty=0.3,
            reasoning=reasoning,
        ),
        final_approved=False,
    )


class TestACCheckItem:
    """Tests for ACCheckItem dataclass."""

    def test_defaults(self) -> None:
        """ACCheckItem has sensible defaults for optional fields."""
        item = ACCheckItem(ac_text="User can login", passed=True)

        assert item.ac_text == "User can login"
        assert item.passed is True
        assert item.reasoning == ""
        assert item.evidence == ()
        assert item.questions_used == ()
        assert item.failure_reason is None

    def test_with_evidence_and_questions(self) -> None:
        """ACCheckItem preserves evidence and question tuples."""
        item = ACCheckItem(
            ac_text="Login must be secure",
            passed=True,
            reasoning="Uses bcrypt",
            evidence=("src/auth.py:42",),
            questions_used=("Does it reject empty passwords?",),
        )

        assert item.evidence == ("src/auth.py:42",)
        assert item.questions_used == ("Does it reject empty passwords?",)


class TestACChecklistResultProperties:
    """Tests for ACChecklistResult computed properties."""

    def test_empty_checklist_is_not_all_passed(self) -> None:
        """An empty checklist is treated as 'not ready', never as all-passed."""
        checklist = ACChecklistResult(items=(), total=0, passed_count=0)

        assert checklist.all_passed is False
        assert checklist.pass_rate == 0.0
        assert checklist.failed_items == ()

    def test_all_passed_true_when_every_item_passes(self) -> None:
        """all_passed True when passed_count == total and total > 0."""
        items = (
            ACCheckItem(ac_text="AC1", passed=True),
            ACCheckItem(ac_text="AC2", passed=True),
        )
        checklist = ACChecklistResult(items=items, total=2, passed_count=2)

        assert checklist.all_passed is True
        assert checklist.pass_rate == 1.0
        assert checklist.failed_items == ()

    def test_partial_pass_rate(self) -> None:
        """pass_rate is a plain ratio."""
        items = (
            ACCheckItem(ac_text="AC1", passed=True),
            ACCheckItem(ac_text="AC2", passed=False, failure_reason="bad"),
            ACCheckItem(ac_text="AC3", passed=True),
            ACCheckItem(ac_text="AC4", passed=False),
        )
        checklist = ACChecklistResult(items=items, total=4, passed_count=2)

        assert checklist.pass_rate == 0.5
        assert checklist.all_passed is False
        assert len(checklist.failed_items) == 2


class TestAggregateResults:
    """Tests for aggregate_results()."""

    def test_length_mismatch_raises(self) -> None:
        """Mismatched ac_texts and results tuples raise ValueError."""
        with pytest.raises(ValueError, match="same length"):
            aggregate_results(
                ac_texts=("AC1", "AC2"),
                results=(_passing_evaluation(),),
            )

    def test_empty_inputs_produce_empty_checklist(self) -> None:
        """Empty inputs produce an empty (and not-ready) checklist."""
        checklist = aggregate_results(ac_texts=(), results=())

        assert checklist.total == 0
        assert checklist.all_passed is False

    def test_all_passing_produces_all_passed_checklist(self) -> None:
        """When every result is final_approved, checklist is all-passed."""
        results = (
            _passing_evaluation(execution_id="e1"),
            _passing_evaluation(execution_id="e2"),
        )
        checklist = aggregate_results(
            ac_texts=("AC1", "AC2"),
            results=results,
        )

        assert checklist.total == 2
        assert checklist.passed_count == 2
        assert checklist.all_passed is True
        assert all(item.failure_reason is None for item in checklist.items)

    def test_mixed_outcomes_capture_failure_reason(self) -> None:
        """Failing items record a failure_reason derived from Stage 2 reasoning."""
        results = (
            _passing_evaluation(execution_id="e1"),
            _failing_evaluation(
                execution_id="e2",
                reasoning="Webhook signature validation missing",
            ),
        )
        checklist = aggregate_results(
            ac_texts=("Charge processed", "Webhook handled"),
            results=results,
        )

        assert checklist.passed_count == 1
        assert checklist.all_passed is False
        failed = checklist.failed_items
        assert len(failed) == 1
        assert failed[0].ac_text == "Webhook handled"
        assert failed[0].failure_reason is not None
        assert "AC non-compliance" in failed[0].failure_reason or (
            "Webhook signature" in (failed[0].failure_reason or "")
        )

    def test_preserves_evidence_and_questions(self) -> None:
        """Stage 2 evidence and questions_used carry through to checklist items.

        Only meaningful once #367 (PR #383) lands and ``SemanticResult``
        exposes those fields.  On earlier code the aggregator falls back
        to empty tuples — we skip instead of asserting, because the
        behaviour is provably correct for that code state.
        """
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(SemanticResult)}
        if "evidence" not in field_names or "questions_used" not in field_names:
            pytest.skip("SemanticResult does not yet carry #367 fields")

        results = (
            _passing_evaluation(
                execution_id="e1",
                reasoning="Bcrypt verified",
                evidence=("src/auth.py:42", "tests/test_auth.py covers empty pwd"),
                questions_used=("Does it reject empty passwords?",),
            ),
        )
        checklist = aggregate_results(
            ac_texts=("Login must be secure",),
            results=results,
        )

        item = checklist.items[0]
        assert item.evidence == (
            "src/auth.py:42",
            "tests/test_auth.py covers empty pwd",
        )
        assert item.questions_used == ("Does it reject empty passwords?",)

    def test_preserves_input_order(self) -> None:
        """Items appear in the exact order provided."""
        results = (
            _passing_evaluation(execution_id="e1"),
            _failing_evaluation(execution_id="e2"),
            _passing_evaluation(execution_id="e3"),
        )
        checklist = aggregate_results(
            ac_texts=("first", "second", "third"),
            results=results,
        )

        assert [item.ac_text for item in checklist.items] == [
            "first",
            "second",
            "third",
        ]


class TestFormatChecklist:
    """Tests for format_checklist()."""

    def test_format_all_passed(self) -> None:
        """All-passed checklist renders with ALL PASSED header and [x] markers."""
        results = (_passing_evaluation(),)
        checklist = aggregate_results(
            ac_texts=("Login works",),
            results=results,
        )

        output = format_checklist(checklist)

        assert "ALL PASSED" in output
        assert "(1/1 passed, 100%)" in output
        assert "[x] 1. Login works" in output

    def test_format_incomplete_includes_next_steps(self) -> None:
        """Failing checklist includes INCOMPLETE, [ ] marker, and Next Steps."""
        results = (
            _passing_evaluation(execution_id="e1"),
            _failing_evaluation(
                execution_id="e2",
                reasoning="Missing signature check",
            ),
        )
        checklist = aggregate_results(
            ac_texts=("Charge processed", "Webhook validated"),
            results=results,
        )

        output = format_checklist(checklist)

        assert "INCOMPLETE" in output
        assert "(1/2 passed, 50%)" in output
        assert "[x] 1. Charge processed" in output
        assert "[ ] 2. Webhook validated" in output
        assert "Next Steps:" in output

    def test_format_renders_evidence_and_questions(self) -> None:
        """Evidence and questions_used render under each item when present.

        Only meaningful once #367 (PR #383) exposes those fields.
        """
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(SemanticResult)}
        if "evidence" not in field_names or "questions_used" not in field_names:
            pytest.skip("SemanticResult does not yet carry #367 fields")

        results = (
            _passing_evaluation(
                reasoning="ok",
                evidence=("src/auth.py:42",),
                questions_used=("Does it reject empty passwords?",),
            ),
        )
        checklist = aggregate_results(
            ac_texts=("Login works",),
            results=results,
        )

        output = format_checklist(checklist)

        assert "Questions Used:" in output
        assert "Does it reject empty passwords?" in output
        assert "Evidence:" in output
        assert "src/auth.py:42" in output


class TestBuildRunFeedback:
    """Tests for build_run_feedback()."""

    def test_empty_when_all_passed(self) -> None:
        """No feedback when everything passed."""
        results = (_passing_evaluation(),)
        checklist = aggregate_results(
            ac_texts=("Login works",),
            results=results,
        )

        assert build_run_feedback(checklist) == ()

    def test_feedback_per_failed_item(self) -> None:
        """Each failed item produces one feedback string."""
        results = (
            _passing_evaluation(execution_id="e1"),
            _failing_evaluation(
                execution_id="e2",
                reasoning="Missing signature check",
            ),
            _failing_evaluation(
                execution_id="e3",
                reasoning="Refund path not implemented",
            ),
        )
        checklist = aggregate_results(
            ac_texts=("Charge processed", "Webhook validated", "Refund works"),
            results=results,
        )

        feedback = build_run_feedback(checklist)

        assert len(feedback) == 2
        assert any("Webhook validated" in entry for entry in feedback)
        assert any("Refund works" in entry for entry in feedback)
