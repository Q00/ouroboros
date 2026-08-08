"""Project trusted spec-verifier evidence into formal evaluation verdicts."""

from __future__ import annotations

from typing import Any

from ouroboros.core.lineage import ACResult, EvaluationSummary


def _agent_results_from_execution_summary(mechanical: Any) -> dict[int, bool]:
    """Return legacy agent-reported AC outcomes for spec verification."""
    agent_results = {ac.ac_index: ac.authoritative_pass for ac in mechanical.ac_results}
    for task in mechanical.task_results:
        source_ac_index = task.source_ac_index
        if source_ac_index is None:
            source_ac_index = task.task_index
        agent_results.setdefault(source_ac_index, task.completed)
    return agent_results


def _trusted_ac_content_sources(
    mechanical: Any,
    seed: Any | None,
) -> dict[int, tuple[tuple[str, str], ...]]:
    """Collect the caller-owned criterion text for each verifier report index."""
    sources: dict[int, list[tuple[str, str]]] = {}

    def add(ac_index: int, source: str, content: Any) -> None:
        if isinstance(content, str):
            sources.setdefault(ac_index, []).append((source, content))

    seed_criteria = tuple(getattr(seed, "acceptance_criteria", ()) or ())
    for ac_index, criterion in enumerate(seed_criteria):
        if isinstance(criterion, str):
            add(ac_index, "seed", criterion)
        else:
            add(ac_index, "seed", getattr(criterion, "description", None))
    for result in mechanical.ac_results:
        add(result.ac_index, "mechanical AC", result.ac_content)
    for task in mechanical.task_results:
        source_ac_index = task.source_ac_index
        if source_ac_index is None:
            source_ac_index = task.task_index
        add(source_ac_index, "mechanical task", task.task_content)
    return {ac_index: tuple(values) for ac_index, values in sources.items()}


def _verification_provenance_mismatch(
    report: Any,
    trusted_sources: tuple[tuple[str, str], ...],
) -> str | None:
    """Explain why a verifier report is not bound to the trusted AC input."""
    report_text = getattr(report, "ac_text", None)
    if not trusted_sources:
        return (
            f"Spec verification provenance mismatch: report criterion {report_text!r} "
            "has no trusted acceptance-criterion input."
        )
    mismatches = [
        (source, content) for source, content in trusted_sources if report_text != content
    ]
    if not mismatches:
        return None
    rendered = "; ".join(f"{source} criterion {content!r}" for source, content in mismatches)
    return (
        f"Spec verification provenance mismatch: report criterion {report_text!r} "
        f"does not match {rendered}."
    )


def _evaluation_summary_from_spec_verification(
    mechanical: Any,
    verification_summary: Any,
    seed: Any | None = None,
) -> Any | None:
    """Promote complete, input-bound verifier coverage into formal AC verdicts."""
    reports = tuple(getattr(verification_summary, "reports", ()) or ())
    if not reports:
        return None
    seed_criteria = tuple(getattr(seed, "acceptance_criteria", ()) or ())

    def semantic_key(ac_index: int) -> str | None:
        if 0 <= ac_index < len(seed_criteria):
            return getattr(seed_criteria[ac_index], "semantic_ac_key", None)
        return None

    trusted_content = _trusted_ac_content_sources(mechanical, seed)
    expected_ac_content = {
        ac_index: sources[0][1] for ac_index, sources in trusted_content.items() if sources
    }
    expected_agent_results = _agent_results_from_execution_summary(mechanical)

    reports_by_index = {report.ac_index: report for report in reports}
    expected_indices = set(trusted_content) | set(expected_agent_results)
    result_indices = sorted(expected_indices | set(reports_by_index))

    ac_results: list[ACResult] = []
    missing_indices: list[int] = []
    unverifiable_indices: list[int] = []
    provenance_mismatch_indices: list[int] = []
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

        provenance_mismatch = _verification_provenance_mismatch(
            report, trusted_content.get(ac_index, ())
        )
        if provenance_mismatch is not None:
            provenance_mismatch_indices.append(ac_index)
            ac_results.append(
                ACResult(
                    ac_index=ac_index,
                    ac_content=expected_ac_content.get(
                        ac_index, f"Acceptance criterion {ac_index + 1}"
                    ),
                    semantic_ac_key=semantic_key(ac_index),
                    passed=False,
                    score=0.0,
                    evidence=provenance_mismatch,
                    verification_method="spec_verifier",
                    ac_verdict_state="overridden",
                    final_verdict="fail",
                    rendered_verdict="FAIL",
                )
            )
            continue

        details = [result.detail for result in report.results if result.detail]
        evidence = "; ".join(details)
        if not report.results:
            unverifiable_indices.append(ac_index)
            evidence = "No independently verifiable assertions; formal AC verdict not evaluated."
            passed = False
            verdict_state = "not_evaluated"
            rendered_verdict = "NOT_EVALUATED"
        else:
            passed = bool(report.verified_pass)
            verifier_overrode_pass = bool(report.agent_reported_pass) and not passed
            verdict_state = "overridden" if verifier_overrode_pass else "evaluated"
            rendered_verdict = "PASS" if passed else "FAIL"
            if not evidence:
                evidence = "Spec verifier produced no evidence details."

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
        failed_indices = [result.ac_index + 1 for result in ac_results if result.unresolved]
        discrepancy_count = getattr(verification_summary, "discrepancy_count", 0)
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
                "no independently verifiable assertions for AC "
                + ", ".join(str(i + 1) for i in unverifiable_indices)
            )
        if provenance_mismatch_indices:
            reason_parts.append(
                "verifier provenance mismatch for AC "
                + ", ".join(str(i + 1) for i in provenance_mismatch_indices)
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
