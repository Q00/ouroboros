"""Unit tests for ouroboros.core.lineage module."""

from ouroboros.core.lineage import ACResult, EvaluationSummary


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
        assert "approval_status" in schema["properties"]["execution_completion_status"][
            "description"
        ].lower()
        assert "execution_completion_status" in schema["properties"]["approval_status"][
            "description"
        ].lower()

    def test_legacy_inputs_backfill_required_status_fields_in_serialized_output(self) -> None:
        """Legacy callers can omit statuses but serialized output must still include both."""
        summary = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
        )
        payload = summary.model_dump(mode="json")

        assert payload["execution_completion_status"] == "completed"
        assert payload["approval_status"] == "rejected"

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
