"""Semantic fallback evaluation for evolve generations."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from ouroboros.core.lineage import ACResult, EvaluationSummary
from ouroboros.core.seed import AcceptanceCriterionSpec, ac_text
from ouroboros.evaluation.models import EvaluationContext, EvaluationResult

log = structlog.get_logger(__name__)


def warn_if_seed_has_no_success_contract(seed: Any) -> None:
    """Expose when evolve has no fixed, structured verification anchor."""
    criteria = tuple(getattr(seed, "acceptance_criteria", ()) or ())
    if any(
        isinstance(criterion, AcceptanceCriterionSpec) and criterion.has_success_contract
        for criterion in criteria
    ):
        return

    metadata = getattr(seed, "metadata", None)
    log.warning(
        "evolution.acceptance_contract.unstructured",
        seed_id=getattr(metadata, "seed_id", None),
        acceptance_criteria=len(criteria),
        consequence=(
            (
                "no AC declares verify_command, expected_artifacts, or output_assertion; "
                "evolve has no fixed structured verification anchor"
            )
            if criteria
            else "the Seed declares no acceptance criteria; evolve cannot establish coverage"
        ),
    )


def _rejected_summary(reason: str) -> EvaluationSummary:
    return EvaluationSummary(
        final_approved=False,
        highest_stage_passed=1,
        score=0.0,
        drift_score=1.0,
        failure_reason=reason,
        approval_status="rejected",
        execution_completion_status="completed",
    )


def _render_evidence(result: EvaluationResult) -> str:
    stage2 = result.stage2_result
    if stage2 is None:
        return result.failure_reason or "Evaluation did not produce a semantic verdict."
    parts = [*stage2.evidence]
    if stage2.reasoning and stage2.reasoning not in parts:
        parts.append(stage2.reasoning)
    if result.failure_reason and result.failure_reason not in parts:
        parts.append(result.failure_reason)
    return "; ".join(parts) or "Semantic evaluation produced no evidence details."


def _failure_reason(
    criteria: tuple[Any, ...],
    results: tuple[EvaluationResult, ...],
) -> str | None:
    failed = [
        (
            index,
            ac_text(criterion),
            (
                result.failure_reason
                if result.stage2_result is not None
                else "semantic evaluation did not run"
            ),
        )
        for index, (criterion, result) in enumerate(zip(criteria, results, strict=True), start=1)
        if result.stage2_result is None or not result.final_approved
    ]
    if not failed:
        return None
    details = "; ".join(
        f"AC {index} ({content}): {reason or 'semantic evaluation rejected this criterion'}"
        for index, content, reason in failed
    )
    return f"{len(failed)}/{len(criteria)} acceptance criteria failed: {details}"


async def evaluate_seed_criteria(
    *,
    seed: Any,
    artifact: str,
    artifact_bundle: Any,
    pipeline: Any,
) -> EvaluationSummary:
    """Evaluate evolve's semantic fallback once per acceptance criterion."""
    criteria = tuple(getattr(seed, "acceptance_criteria", ()) or ())
    metadata = getattr(seed, "metadata", None)
    seed_id = str(getattr(metadata, "seed_id", "unknown"))
    goal = str(getattr(seed, "goal", ""))
    constraints = tuple(getattr(seed, "constraints", ()) or ())

    if not criteria:
        return _rejected_summary(
            "Seed has no acceptance criteria; semantic evaluation cannot establish coverage"
        )

    def context_for(index: int, criterion: Any) -> EvaluationContext:
        return EvaluationContext(
            execution_id=f"eval_{seed_id}_ac_{index + 1}",
            seed_id=seed_id,
            current_ac=ac_text(criterion),
            current_ac_spec=(criterion if isinstance(criterion, AcceptanceCriterionSpec) else None),
            artifact=artifact,
            artifact_type="code",
            goal=goal,
            constraints=constraints,
            artifact_bundle=artifact_bundle,
        )

    try:
        first = await pipeline.evaluate(context_for(0, criteria[0]))
    except Exception as exc:
        return _rejected_summary(f"AC 1 evaluation raised {type(exc).__name__}: {exc}")
    if first.is_err:
        return _rejected_summary(f"AC 1 evaluation failed: {first.error}")

    shared_stage1 = first.value.stage1_result

    async def evaluate_one(index: int, criterion: Any) -> Any:
        return await pipeline.evaluate(
            context_for(index, criterion),
            stage1_result=shared_stage1,
        )

    remaining = await asyncio.gather(
        *(evaluate_one(index, criterion) for index, criterion in enumerate(criteria[1:], start=1)),
        return_exceptions=True,
    )
    gathered = (first, *remaining)
    for index, entry in enumerate(gathered, start=1):
        if isinstance(entry, BaseException):
            return _rejected_summary(
                f"AC {index} evaluation raised {type(entry).__name__}: {entry}"
            )
        if entry.is_err:
            return _rejected_summary(f"AC {index} evaluation failed: {entry.error}")

    results = tuple(entry.value for entry in gathered)
    stage2_results = tuple(
        result.stage2_result for result in results if result.stage2_result is not None
    )
    approved = all(result.stage2_result is not None and result.final_approved for result in results)
    scores = tuple(result.score for result in stage2_results)
    drift_scores = tuple(result.drift_score for result in stage2_results)
    reward_hacking_risks = tuple(result.reward_hacking_risk for result in stage2_results)
    ac_results = tuple(
        ACResult(
            ac_index=index,
            ac_content=ac_text(criterion),
            semantic_ac_key=getattr(criterion, "semantic_ac_key", None),
            passed=result.stage2_result is not None and result.final_approved,
            score=result.stage2_result.score if result.stage2_result else 0.0,
            evidence=_render_evidence(result),
            verification_method=(
                "semantic_evaluation" if result.stage2_result is not None else "unknown"
            ),
            ac_verdict_state=("evaluated" if result.stage2_result is not None else "not_evaluated"),
            final_verdict=(
                "pass" if result.stage2_result is not None and result.final_approved else "fail"
            ),
            rendered_verdict=(
                "PASS"
                if result.stage2_result is not None and result.final_approved
                else "FAIL"
                if result.stage2_result is not None
                else "NOT_EVALUATED"
            ),
        )
        for index, (criterion, result) in enumerate(zip(criteria, results, strict=True))
    )

    return EvaluationSummary(
        final_approved=approved,
        highest_stage_passed=min(max(1, result.highest_stage_completed) for result in results),
        score=sum(scores) / len(scores) if scores else 0.0,
        drift_score=max(drift_scores) if drift_scores else None,
        reward_hacking_risk=max(reward_hacking_risks) if reward_hacking_risks else None,
        failure_reason=_failure_reason(criteria, results),
        ac_results=ac_results,
        approval_status="approved" if approved else "rejected",
        execution_completion_status="completed",
    )


__all__ = ["evaluate_seed_criteria", "warn_if_seed_has_no_success_contract"]
