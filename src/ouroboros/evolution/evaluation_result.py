"""Normalize evaluator return values for evolutionary generations."""

from __future__ import annotations

from typing import Any

from ouroboros.core.lineage import EvaluationSummary


def evaluation_error_summary(failure_text: str) -> EvaluationSummary:
    """Represent evaluator failures as explicit rejected summaries."""

    return EvaluationSummary(
        final_approved=False,
        highest_stage_passed=1,
        score=0.0,
        failure_reason=f"evaluation errored: {failure_text}",
        approval_status="rejected",
        execution_completion_status="completed",
    )


def normalize_evaluator_result(result: Any) -> EvaluationSummary:
    """Unwrap supported evaluator results and fail closed on invalid values."""

    if hasattr(result, "is_ok") and result.is_ok:
        return result.value
    if hasattr(result, "is_err") and result.is_err:
        error = getattr(result, "error", "unknown evaluation error")
        rendered = error.format_details() if hasattr(error, "format_details") else str(error)
        return evaluation_error_summary(rendered)
    if isinstance(result, EvaluationSummary):
        return result
    return evaluation_error_summary(f"unexpected evaluator result type: {type(result).__name__}")
