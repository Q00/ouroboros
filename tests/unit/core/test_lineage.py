"""Unit tests for ouroboros.core.lineage module."""

from ouroboros.core.lineage import (
    ACResult,
    EvaluationSummary,
    FeedbackMetadata,
    GenerationRecord,
    TaskResult,
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

    def test_summary_serializes_task_results_separately_from_ac_results(self) -> None:
        """Worker task completion should not masquerade as formal AC verdicts."""
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Implement feature",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    evidence="Worker finished the implementation task",
                    execution_method="legacy_parallel_report",
                ),
            ),
            ac_results=(),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )

        payload = summary.model_dump(mode="json")

        assert payload["task_results"][0]["status"] == "completed"
        assert payload["task_results"][0]["source_ac_index"] == 0
        assert payload["ac_results"] == []
        assert summary.run_verdict_passed is False

    def test_run_verdict_fails_when_approval_not_evaluated(self) -> None:
        """Completion without formal approval must not become a passing run verdict."""
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )

        assert summary.run_verdict_passed is False
        assert summary.run_verdict == "FAIL"

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

    def test_ac_results_cannot_override_final_rejection(self) -> None:
        """Detailed AC PASSes are necessary but cannot mint final approval."""
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

        assert summary.run_verdict_passed is False
        assert summary.run_verdict == "FAIL"
        assert summary.approval_status == "rejected"

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
        """Execution failure overrides AC results and legacy approval fields."""
        summary = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            execution_completion_status="failed",
            approval_status="approved",
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
        assert summary.final_approved is False
        assert summary.approval_status == "rejected"

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

    def test_run_verdict_preserves_rejection_when_acs_pass(self) -> None:
        """AC results may veto but cannot upgrade the independent final gate."""
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

        assert summary.run_verdict_passed is False
        assert summary.run_verdict == "FAIL"
        assert summary.approval_status == "rejected"

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

    def test_rejection_preserved_at_construction_not_lazily(self) -> None:
        """model_dump() must preserve the final gate without lazy property access."""
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
        assert payload["approval_status"] == "rejected"
        assert payload["final_approved"] is False

    def test_serialized_approval_is_execution_aware_with_passing_acs(self) -> None:
        """model_dump() must not expose approved legacy fields after execution failure."""
        summary = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            execution_completion_status="failed",
            approval_status="approved",
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

        payload = summary.model_dump(mode="json")
        assert payload["approval_status"] == "rejected"
        assert payload["final_approved"] is False
        assert summary.run_verdict == "FAIL"


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

    def test_not_evaluated_pass_is_not_authoritative_approval(self) -> None:
        """A contradictory PASS label cannot upgrade unknown evidence to approval."""
        summary = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Ship feature",
                    passed=True,
                    ac_verdict_state="not_evaluated",
                    verification_method="unknown",
                ),
            ),
        )

        assert summary.ac_results[0].passed is True
        assert not summary.ac_results[0].verdict_is_authoritative
        assert not summary.ac_results[0].authoritative_pass
        assert summary.ac_results[0].unresolved
        assert summary.ac_results[0].authority_state == "unresolved"
        assert not summary.final_approved
        assert summary.approval_status == "rejected"
        assert not summary.run_verdict_passed

    def test_opaque_override_cannot_mint_approval(self) -> None:
        """An override needs a concrete verifier source and reason."""
        result = ACResult(
            ac_index=0,
            ac_content="Ship feature",
            passed=True,
            ac_verdict_state="overridden",
            verification_method="unknown",
            evidence="",
        )
        summary = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            ac_results=(result,),
        )

        assert not result.verdict_is_authoritative
        assert not summary.final_approved
        assert not summary.run_verdict_passed

    def test_ac_passes_cannot_upgrade_explicit_final_rejection(self) -> None:
        """Final approval and AC approval are conjunctive authorities."""
        summary = EvaluationSummary(
            final_approved=False,
            approval_status="rejected",
            highest_stage_passed=3,
            failure_reason="Stage 3 consensus rejected the artifact",
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Ship feature",
                    passed=True,
                    verification_method="semantic",
                ),
            ),
        )

        assert not summary.final_approved
        assert summary.approval_status == "rejected"
        assert summary.failure_reason == "Stage 3 consensus rejected the artifact"
        assert not summary.run_verdict_passed

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
