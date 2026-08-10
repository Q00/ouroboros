"""Project spec-verification results into formal evaluation summaries."""

from __future__ import annotations

from typing import Any


def agent_results_from_execution_summary(mechanical: Any) -> dict[int, bool]:
    """Return legacy agent-reported AC outcomes for spec verification.

    Formal ``ACResult`` values take precedence when present. For legacy
    execution-only summaries, preserve the worker task completion signal via
    ``source_ac_index`` so skipped or unverifiable assertions cannot convert a
    worker-reported failure into formal approval.
    """
    agent_results = {ac.ac_index: ac.authoritative_pass for ac in mechanical.ac_results}
    for task in mechanical.task_results:
        source_ac_index = task.source_ac_index
        if source_ac_index is None:
            source_ac_index = task.task_index
        agent_results.setdefault(source_ac_index, task.completed)
    return agent_results


def evaluation_summary_from_spec_verification(
    mechanical: Any,
    verification_summary: Any,
    seed: Any | None = None,
) -> Any | None:
    """Promote complete verifier coverage into Seed-bound formal AC verdicts."""
    from ouroboros.core.lineage import ACResult, EvaluationSummary
    from ouroboros.verification.models import VerificationOutcome

    reports = tuple(getattr(verification_summary, "reports", ()) or ())
    if not reports:
        return None
    seed_criteria = tuple(getattr(seed, "acceptance_criteria", ()) or ())

    def semantic_key(ac_index: int) -> str | None:
        if 0 <= ac_index < len(seed_criteria):
            return getattr(seed_criteria[ac_index], "semantic_ac_key", None)
        return None

    expected_ac_content: dict[int, str] = {
        ac.ac_index: ac.ac_content for ac in mechanical.ac_results
    }
    expected_agent_results = agent_results_from_execution_summary(mechanical)
    for task in mechanical.task_results:
        source_ac_index = task.source_ac_index
        if source_ac_index is None:
            source_ac_index = task.task_index
        expected_ac_content.setdefault(source_ac_index, task.task_content)

    reports_by_index = {report.ac_index: report for report in reports}
    expected_indices = set(expected_ac_content) | set(expected_agent_results)
    result_indices = sorted(expected_indices | set(reports_by_index))

    ac_results: list[ACResult] = []
    missing_indices: list[int] = []
    unverifiable_indices: list[int] = []
    skipped_indices: list[int] = []
    for ac_index in result_indices:
        report = reports_by_index.get(ac_index)
        if report is None:
            missing_indices.append(ac_index)
            ac_results.append(
                ACResult(
                    ac_index=ac_index,
                    ac_content=expected_ac_content.get(
                        ac_index, f"Acceptance criterion {ac_index + 1}"
                    ),
                    semantic_ac_key=semantic_key(ac_index),
                    passed=False,
                    score=0.0,
                    evidence="No spec verification report was produced for this AC.",
                    verification_method="spec_verifier",
                    ac_verdict_state="not_evaluated",
                    final_verdict="fail",
                    rendered_verdict="NOT_EVALUATED",
                )
            )
            continue

        details = [result.detail for result in report.results if result.detail]
        evidence = "; ".join(details)
        outcomes = {result.outcome for result in report.results}
        if not report.results:
            unverifiable_indices.append(ac_index)
            evidence = "No independently verifiable assertions; formal AC verdict not evaluated."
            passed = False
            verdict_state = "not_evaluated"
            rendered_verdict = "NOT_EVALUATED"
        elif VerificationOutcome.DISCREPANCY in outcomes:
            passed = False
            verifier_overrode_pass = bool(report.agent_reported_pass)
            verdict_state = "overridden" if verifier_overrode_pass else "evaluated"
            rendered_verdict = "FAIL"
            if VerificationOutcome.UNVERIFIABLE in outcomes:
                unverifiable_indices.append(ac_index)
            if VerificationOutcome.SKIPPED in outcomes:
                skipped_indices.append(ac_index)
            if not evidence:
                evidence = "Spec verifier found a discrepancy without evidence details."
        elif outcomes == {VerificationOutcome.VERIFIED}:
            passed = True
            verdict_state = "evaluated"
            rendered_verdict = "PASS"
            if not evidence:
                evidence = "Spec verifier produced no evidence details."
        else:
            # Missing evidence is not a discrepancy, but this formal adapter is
            # a strict gate: only an all-VERIFIED report may mint a PASS.
            passed = False
            verdict_state = "not_evaluated"
            rendered_verdict = "NOT_EVALUATED"
            if VerificationOutcome.UNVERIFIABLE in outcomes:
                unverifiable_indices.append(ac_index)
            if VerificationOutcome.SKIPPED in outcomes:
                skipped_indices.append(ac_index)
            if not evidence:
                evidence = "Spec verification did not produce independently usable evidence."

        ac_results.append(
            ACResult(
                ac_index=report.ac_index,
                ac_content=report.ac_text,
                semantic_ac_key=semantic_key(report.ac_index),
                passed=passed,
                score=1.0 if passed else 0.0,
                evidence=evidence,
                verification_method="spec_verifier",
                ac_verdict_state=verdict_state,
                final_verdict="pass" if passed else "fail",
                rendered_verdict=rendered_verdict,
            )
        )

    total = len(ac_results)
    passed_count = sum(1 for result in ac_results if result.authoritative_pass)
    score = passed_count / total if total > 0 else 0.0
    complete_coverage = bool(expected_indices) and expected_indices.issubset(reports_by_index)
    execution_completed = mechanical.execution_completion_status == "completed"
    approved = complete_coverage and passed_count == total and total > 0 and execution_completed

    failure_reason = None
    if not approved:
        failed_indices = [
            result.ac_index + 1 for result in ac_results if result.rendered_verdict == "FAIL"
        ]
        discrepancy_count = sum(
            1
            for report in reports
            if getattr(report, "has_confirmed_discrepancy", report.has_discrepancy)
        )
        reason_parts = []
        if failed_indices:
            reason_parts.append(
                f"{len(failed_indices)}/{total} ACs failed "
                f"(AC {', '.join(str(i) for i in failed_indices)})"
            )
        if discrepancy_count:
            reason_parts.append(f"{discrepancy_count} spec verification override(s)")
        if missing_indices:
            reason_parts.append(
                "missing verifier report for AC " + ", ".join(str(i + 1) for i in missing_indices)
            )
        if unverifiable_indices:
            reason_parts.append(
                "unverifiable assertion evidence for AC "
                + ", ".join(str(i + 1) for i in unverifiable_indices)
            )
        if skipped_indices:
            reason_parts.append(
                "source verification skipped for AC "
                + ", ".join(str(i + 1) for i in skipped_indices)
            )
        if not execution_completed:
            reason_parts.append(
                f"execution_completion_status={mechanical.execution_completion_status}"
            )
        if not reason_parts:
            reason_parts.append("spec verification did not approve the run")
        failure_reason = reason_parts[0]
        if len(reason_parts) > 1:
            failure_reason += f" [{'; '.join(reason_parts[1:])}]"

    return EvaluationSummary(
        final_approved=approved,
        highest_stage_passed=3 if approved else 2,
        score=score,
        drift_score=None,
        failure_reason=failure_reason,
        ac_results=tuple(ac_results),
        task_results=mechanical.task_results,
        feedback_metadata=mechanical.feedback_metadata,
        execution_completion_status=mechanical.execution_completion_status,
        approval_status="approved" if approved else "rejected",
    )
