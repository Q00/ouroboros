"""Unit tests for ouroboros.core.lineage module."""

from ouroboros.core.lineage import (
    ACResult,
    EvaluationSummary,
    FeedbackMetadata,
    GenerationRecord,
)
from ouroboros.core.seed import OntologySchema


class TestEvaluationSummary:
    """Tests for EvaluationSummary schema metadata."""

    def test_schema_describes_drift_and_reward_hacking_separately(self) -> None:
        """Schema descriptions should keep drift and reward-hacking distinct."""
        schema = EvaluationSummary.model_json_schema()
        drift_description = schema["properties"]["drift_score"]["description"].lower()
        risk_description = schema["properties"]["reward_hacking_risk"]["description"].lower()

        assert "goal/constraint/ontology divergence" in drift_description
        assert "distinct from reward_hacking_risk" in drift_description
        assert "optimized for evaluator, rubric, or test approval" in risk_description
        assert "distinct from drift_score" in risk_description

    def test_schema_requires_run_level_status_fields(self) -> None:
        """Approval and completion statuses should be required in the schema."""
        schema = EvaluationSummary.model_json_schema()
        required = set(schema["required"])

        assert "execution_completion_status" in required
        assert "approval_status" in required
        assert (
            "approval_status"
            in schema["properties"]["execution_completion_status"]["description"].lower()
        )
        assert (
            "execution_completion_status"
            in schema["properties"]["approval_status"]["description"].lower()
        )

    def test_legacy_inputs_backfill_required_status_fields_in_serialized_output(self) -> None:
        """Legacy callers can omit statuses but serialized output must still include both."""
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
        )
        payload = summary.model_dump(mode="json")

        assert payload["execution_completion_status"] == "completed"
        assert payload["approval_status"] == "rejected"

    def test_feedback_metadata_serializes_as_structured_output(self) -> None:
        """Structured feedback metadata should survive round-trip serialization."""
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
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

        payload = summary.model_dump(mode="json")

        assert payload["feedback_metadata"] == [
            {
                "code": "decomposition_depth_warning",
                "severity": "warning",
                "message": "Depth safety net forced atomic execution.",
                "source": "parallel_executor",
                "details": {"max_depth": 3, "affected_count": 2},
            }
        ]

    def test_seed_quality_canary_feedback_filters_depth_warning(self) -> None:
        """Depth warnings should be exposed as seed-quality canary feedback."""
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            feedback_metadata=(
                FeedbackMetadata(
                    code="decomposition_depth_warning",
                    severity="warning",
                    message="Depth safety net forced atomic execution.",
                    source="parallel_executor",
                    details={"max_depth": 3, "affected_count": 2},
                ),
                FeedbackMetadata(
                    code="unrelated_warning",
                    severity="warning",
                    message="Ignore me",
                    source="evaluation",
                ),
            ),
        )

        assert [feedback.code for feedback in summary.seed_quality_canary_feedback] == [
            "decomposition_depth_warning"
        ]

    def test_generation_record_backfills_seed_quality_canary_feedback(self) -> None:
        """Generation records should derive canary feedback from evaluation summaries."""
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
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

        record = GenerationRecord(
            generation_number=1,
            seed_id="seed_1",
            ontology_snapshot=OntologySchema(
                name="test",
                description="test",
                fields=(),
            ),
            evaluation_summary=summary,
        )

        assert [feedback.code for feedback in record.seed_quality_canary_feedback] == [
            "decomposition_depth_warning"
        ]

    def test_run_verdict_prefers_ac_results_over_final_approved(self) -> None:
        """Summary verdict should follow the detailed AC verdict source."""
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Ship feature",
                    passed=True,
                    score=1.0,
                    evidence="All checks passed",
                    verification_method="semantic",
                ),
            ),
        )

        assert summary.run_verdict_passed is True
        assert summary.run_verdict == "PASS"
        # approval_status must be reconciled to avoid contradictory serialization
        assert summary.approval_status == "approved"

    def test_run_verdict_uses_canonical_final_verdict_over_legacy_passed_flag(self) -> None:
        """Summary verdict should aggregate from final_verdict even for legacy-shaped inputs."""
        summary = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Ship feature",
                    passed=True,
                    final_verdict="fail",
                    score=0.0,
                    evidence="Spec verification override",
                    verification_method="semantic",
                ),
            ),
        )

        assert summary.ac_results[0].passed is False
        assert summary.ac_results[0].verdict_label == "FAIL"
        assert summary.run_verdict_passed is False
        assert summary.run_verdict == "FAIL"

    def test_run_verdict_fails_when_execution_incomplete(self) -> None:
        """Execution failure overrides AC results — run must be FAIL."""
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            execution_completion_status="failed",
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Ship feature",
                    passed=True,
                    score=1.0,
                    evidence="All checks passed",
                    verification_method="semantic",
                ),
            ),
        )

        assert summary.run_verdict_passed is False
        assert summary.run_verdict == "FAIL"

    def test_run_verdict_fails_when_approval_rejected_no_ac_results(self) -> None:
        """Rejected approval without AC results must be FAIL."""
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            execution_completion_status="completed",
            approval_status="rejected",
        )

        assert summary.run_verdict_passed is False
        assert summary.run_verdict == "FAIL"

    def test_run_verdict_reconciles_approval_when_acs_override(self) -> None:
        """When AC results all pass, approval_status must be reconciled to 'approved'."""
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            execution_completion_status="completed",
            approval_status="rejected",
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Feature works",
                    passed=True,
                    score=1.0,
                    evidence="All checks passed",
                    verification_method="semantic",
                ),
            ),
        )

        assert summary.run_verdict_passed is True
        assert summary.run_verdict == "PASS"
        assert summary.approval_status == "approved"

    def test_run_verdict_reconciles_approval_when_acs_fail(self) -> None:
        """When AC results fail, approval_status must be reconciled to 'rejected'."""
        summary = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            execution_completion_status="completed",
            approval_status="approved",
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Feature works",
                    passed=True,
                    final_verdict="fail",
                    score=0.0,
                    evidence="Spec override",
                    verification_method="spec_verifier",
                ),
            ),
        )

        assert summary.run_verdict_passed is False
        assert summary.run_verdict == "FAIL"
        assert summary.approval_status == "rejected"

    def test_approval_reconciled_at_construction_not_lazily(self) -> None:
        """model_dump() must show reconciled approval_status without reading run_verdict first."""
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Feature works",
                    passed=True,
                    score=1.0,
                    evidence="OK",
                    verification_method="semantic",
                ),
            ),
        )

        # Deliberately do NOT access run_verdict_passed before serializing
        payload = summary.model_dump(mode="json")
        assert payload["approval_status"] == "approved"
        assert payload["final_approved"] is True


class TestACResult:
    """Tests for ACResult verdict backfill."""

    def test_legacy_inputs_backfill_canonical_verdict_fields(self) -> None:
        """Legacy AC results should still expose the canonical verdict lifecycle."""
        result = ACResult(
            ac_index=0,
            ac_content="Ship feature",
            passed=False,
            ac_verdict_state="overridden",
            evidence="Spec verification override: hidden regression",
            verification_method="spec_verifier",
        )

        assert result.provisional_verdict == "pass"
        assert result.override_source == "spec_verifier"
        assert result.override_reason == "Spec verification override: hidden regression"
        assert result.final_verdict == "fail"
        assert result.rendered_verdict == "FAIL"

    def test_verdict_label_ignores_stale_rendered_verdict(self) -> None:
        """Rendered labels should keep following final_verdict once normalized."""
        result = ACResult(
            ac_index=0,
            ac_content="Ship feature",
            passed=False,
            final_verdict="fail",
            score=0.0,
            evidence="Spec verification override",
            verification_method="semantic",
        ).model_copy(update={"rendered_verdict": "PASS"})

        assert result.verdict_label == "FAIL"
