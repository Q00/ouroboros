"""Project spec-verification results into formal evaluation summaries."""

from __future__ import annotations

from typing import Any, TypeGuard


def _valid_ac_index(value: Any) -> TypeGuard[int]:
    """Whether an untrusted identity is a raw non-negative integer.

    ``bool`` is an ``int`` subclass and equality-coincides with indices 0/1;
    using exact type identity prevents model-construction bypasses from
    laundering booleans or coercible strings/floats into AC authority.
    """
    return type(value) is int and value >= 0


def _report_identity_error(
    reports: tuple[Any, ...],
    seed_criteria: tuple[Any, ...],
) -> str | None:
    """Return why verifier reports cannot carry Seed-bound authority.

    Pydantic models validate ordinary construction and replay, but this adapter
    is a public authority boundary and also receives compatibility objects in
    tests and integrations. Revalidate identity here before any report is
    indexed: a dictionary projection must never turn ordering into authority.
    """
    from ouroboros.core.seed import AcceptanceCriterionSpec

    seen_indices: set[int] = set()
    for position, report in enumerate(reports):
        ac_index = getattr(report, "ac_index", None)
        if not _valid_ac_index(ac_index):
            return f"report {position + 1} has invalid ac_index={ac_index!r}"
        if ac_index in seen_indices:
            return f"duplicate report ac_index={ac_index}"
        seen_indices.add(ac_index)

        report_text = getattr(report, "ac_text", None)
        if not isinstance(report_text, str):
            return f"report AC {ac_index + 1} has invalid ac_text"
        if seed_criteria:
            if ac_index >= len(seed_criteria):
                return f"report ac_index={ac_index} is outside Seed AC coverage"
            criterion = seed_criteria[ac_index]
            expected_text: str | None = None
            if isinstance(criterion, str):
                expected_text = criterion
            elif isinstance(criterion, AcceptanceCriterionSpec):
                expected_text = criterion.description
            if expected_text is not None and report_text != expected_text:
                return f"report AC {ac_index + 1} text does not match the authoritative Seed AC"

        results = getattr(report, "results", ())
        if not isinstance(results, tuple | list):
            return f"report AC {ac_index + 1} has invalid results"
        for result_position, result in enumerate(results):
            assertion = getattr(result, "assertion", None)
            assertion_index = getattr(assertion, "ac_index", None)
            assertion_text = getattr(assertion, "ac_text", None)
            if not _valid_ac_index(assertion_index):
                return (
                    f"report AC {ac_index + 1} result {result_position + 1} "
                    f"has invalid assertion ac_index={assertion_index!r}"
                )
            if assertion_index != ac_index:
                return (
                    f"report AC {ac_index + 1} result {result_position + 1} "
                    f"has assertion ac_index={assertion_index!r}"
                )
            if assertion_text != report_text:
                return (
                    f"report AC {ac_index + 1} result {result_position + 1} "
                    "assertion text does not match its parent report"
                )
    return None


def _agent_claimed_pass(
    report: Any,
    expected_agent_results: dict[int, bool],
    ac_index: int,
) -> bool:
    """Whether the agent itself claimed this AC passed.

    Two records answer this and only one of them is authoritative. The
    mechanical summary is the execution's own account of what the agent
    reported; the report's ``agent_reported_pass`` is a copy the verifier was
    handed, and a replayed, stale or externally built report carries whatever
    copy it was constructed with — including a `True` that contradicts the
    execution, or no field at all, which reads as `True` by default. Trusting
    the copy alone lets either shape turn a mechanically failed AC into a
    formal PASS.

    So both are consulted and either one saying "not a pass" settles it.
    Disagreement is not resolved in favour of the more permissive record: two
    accounts of the same fact that do not match are not evidence that the
    agent claimed a pass.
    """
    if not bool(getattr(report, "agent_reported_pass", True)):
        return False
    return bool(expected_agent_results.get(ac_index, True))


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
    from ouroboros.core.seed import ac_texts
    from ouroboros.verification.models import VerificationOutcome

    reports = tuple(getattr(verification_summary, "reports", ()) or ())
    if not reports:
        return None
    seed_criteria = tuple(getattr(seed, "acceptance_criteria", ()) or ())
    identity_error = _report_identity_error(reports, seed_criteria)
    if identity_error is not None:
        return evaluation_summary_for_unavailable_spec_verification(
            mechanical,
            seed,
            f"Spec verification report identity invalid: {identity_error}",
        )

    def semantic_key(ac_index: int) -> str | None:
        if 0 <= ac_index < len(seed_criteria):
            return getattr(seed_criteria[ac_index], "semantic_ac_key", None)
        return None

    # Seed criteria are the authority boundary. Mechanical execution records
    # and verifier reports may add diagnostic indices, but they cannot narrow
    # the set of ACs that must be independently verified before PASS.
    expected_ac_content: dict[int, str] = dict(enumerate(ac_texts(seed_criteria)))
    for ac in mechanical.ac_results:
        expected_ac_content.setdefault(ac.ac_index, ac.ac_content)
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
        elif outcomes == {VerificationOutcome.VERIFIED} and not _agent_claimed_pass(
            report, expected_agent_results, ac_index
        ):
            # Source-scan evidence may confirm a claimed PASS; it may not
            # reverse a reported FAIL. `SpecVerifier.verify_all` already
            # refuses to emit this combination, but this adapter is a public
            # authority boundary that also accepts replayed, legacy and
            # compatibility summaries built elsewhere. Enforcing the polarity
            # only in the producer would let a rehydrated report encode the
            # exact transition the producer forbids, so the boundary that
            # mints the formal verdict checks it too.
            unverifiable_indices.append(ac_index)
            passed = False
            verdict_state = "not_evaluated"
            rendered_verdict = "NOT_EVALUATED"
            evidence = (
                "Spec verification reported all assertions verified against an "
                "agent-reported FAIL; source-scan evidence cannot overturn a "
                "reported FAIL, so the formal AC verdict is not evaluated."
            )
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


def evaluation_summary_for_unavailable_spec_verification(
    mechanical: Any,
    seed: Any,
    reason: str,
) -> Any:
    """Reject a mechanically completed run when formal evidence is unavailable."""
    from ouroboros.core.lineage import ACResult, EvaluationSummary
    from ouroboros.core.seed import ac_texts

    seed_criteria = tuple(getattr(seed, "acceptance_criteria", ()) or ())
    seed_texts = ac_texts(seed_criteria)
    expected_content = dict(enumerate(seed_texts))
    for ac in mechanical.ac_results:
        expected_content.setdefault(ac.ac_index, ac.ac_content)
    for task in mechanical.task_results:
        source_ac_index = task.source_ac_index
        if source_ac_index is None:
            source_ac_index = task.task_index
        expected_content.setdefault(source_ac_index, task.task_content)

    def semantic_key(ac_index: int) -> str | None:
        if 0 <= ac_index < len(seed_criteria):
            return getattr(seed_criteria[ac_index], "semantic_ac_key", None)
        return None

    ac_results = tuple(
        ACResult(
            ac_index=ac_index,
            ac_content=ac_content,
            semantic_ac_key=semantic_key(ac_index),
            passed=False,
            score=0.0,
            evidence=reason,
            verification_method="spec_verifier",
            ac_verdict_state="not_evaluated",
            final_verdict="fail",
            rendered_verdict="NOT_EVALUATED",
        )
        for ac_index, ac_content in sorted(expected_content.items())
    )

    return EvaluationSummary(
        final_approved=False,
        highest_stage_passed=2 if mechanical.execution_completion_status == "completed" else 1,
        score=0.0,
        drift_score=None,
        failure_reason=reason,
        ac_results=ac_results,
        task_results=mechanical.task_results,
        feedback_metadata=mechanical.feedback_metadata,
        execution_completion_status=mechanical.execution_completion_status,
        approval_status="rejected",
    )
