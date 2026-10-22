"""Operator-facing reports for :class:`ParallelACExecutor` results.

Moved verbatim from ``parallel_executor`` (module-size budget); the executor
module re-exports both functions, so existing imports keep working.
"""

from __future__ import annotations

import json

from ouroboros.orchestrator.decomposition_limits import DEFAULT_MAX_DECOMPOSITION_DEPTH
from ouroboros.orchestrator.evidence.formatting import _render_ac_section
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ParallelExecutionResult,
    collect_decomposition_depth_warning_paths,
)
from ouroboros.orchestrator.verifier import VerifierStatus
from ouroboros.orchestrator.verify_gate_outcome import _VerifyGateOutcome
from ouroboros.orchestrator.verify_quarantine import render_unverifiable_summary


def render_parallel_verification_report(
    parallel_result: ParallelExecutionResult,
    total_acceptance_criteria: int,
    *,
    max_decomposition_depth: int = DEFAULT_MAX_DECOMPOSITION_DEPTH,
) -> str:
    """Build the canonical QA artifact for parallel execution results."""
    total_satisfied = parallel_result.success_count + parallel_result.externally_satisfied_count
    lines = [
        "Parallel Execution Verification Report",
        f"Success: {total_satisfied}/{total_acceptance_criteria}",
    ]
    if parallel_result.externally_satisfied_count > 0:
        lines.append(f"Externally Satisfied: {parallel_result.externally_satisfied_count}")
    if parallel_result.failure_count > 0:
        lines.append(f"Failed: {parallel_result.failure_count}")
    if parallel_result.blocked_count > 0:
        lines.append(f"Blocked: {parallel_result.blocked_count}")
    if parallel_result.invalid_count > 0:
        lines.append(f"Invalid: {parallel_result.invalid_count}")
    unverifiable_indices = [
        result.ac_index + 1
        for result in parallel_result.results
        if (
            isinstance(result.verify_gate_outcome, _VerifyGateOutcome)
            and result.verify_gate_outcome.environment_unverifiable
            and result.success  # a no-evidence attempt is quarantined but BLOCKED
        )
        or (
            result.atomic_verifier_verdict is not None
            and result.atomic_verifier_verdict.status is VerifierStatus.UNAVAILABLE
        )
    ]
    if unverifiable_indices:
        lines.append(render_unverifiable_summary(unverifiable_indices))

    warning_paths: list[str] = []
    for user_facing_idx, result in enumerate(parallel_result.results, start=1):
        warning_paths.extend(
            collect_decomposition_depth_warning_paths(
                result,
                index_path=(user_facing_idx,),
            )
        )

    if warning_paths:
        feedback_metadata = {
            "feedback_metadata": [
                {
                    "code": "decomposition_depth_warning",
                    "severity": "warning",
                    "message": (
                        "Recursive decomposition reached the soft depth safety net; "
                        "affected leaves were forced to atomic execution."
                    ),
                    "source": "parallel_executor",
                    "details": {
                        "max_depth": max_decomposition_depth,
                        "affected_count": len(warning_paths),
                        "affected_ac_paths": warning_paths,
                    },
                }
            ]
        }
        lines.append("")
        lines.append("## Feedback Metadata")
        lines.append(f"Feedback Metadata JSON: {json.dumps(feedback_metadata, sort_keys=True)}")

    lines.append("")
    lines.append("## Task Results")
    for result in parallel_result.results:
        lines.append("")
        lines.extend(
            _render_ac_section(
                result,
                index_path=(result.ac_index + 1,),
                heading_level=3,
            )
        )
    return "\n".join(lines)


def render_parallel_completion_message(
    parallel_result: ParallelExecutionResult,
    total_acceptance_criteria: int,
) -> str:
    """Build a concise operator-facing completion summary."""
    total_satisfied = parallel_result.success_count + parallel_result.externally_satisfied_count
    lines = [
        "Parallel Execution Complete",
        f"Success: {total_satisfied}/{total_acceptance_criteria}",
    ]
    if parallel_result.externally_satisfied_count > 0:
        lines.append(f"Externally Satisfied: {parallel_result.externally_satisfied_count}")
    if parallel_result.failure_count > 0:
        lines.append(f"Failed: {parallel_result.failure_count}")
    if parallel_result.blocked_count > 0:
        lines.append(f"Blocked: {parallel_result.blocked_count}")
    if parallel_result.invalid_count > 0:
        lines.append(f"Invalid: {parallel_result.invalid_count}")

    lines.append("")
    lines.append("Task Status:")
    for result in parallel_result.results:
        if result.outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY:
            status = "COMPLETED"
            suffix = " (externally satisfied)"
        elif result.outcome == ACExecutionOutcome.SETTLED_FROM_SHARED_ATTEMPT:
            status = "COMPLETED"
            suffix = " (judged from the one shared worker attempt)"
        elif result.outcome == ACExecutionOutcome.BLOCKED:
            status = "BLOCKED"
            suffix = f" — {result.error}" if result.error else ""
        elif result.outcome == ACExecutionOutcome.INVALID:
            status = "INVALID"
            suffix = f" — {result.error}" if result.error else ""
        else:
            status = "COMPLETED" if result.success else "FAILED"
            suffix = f" ({len(result.sub_results)} subtasks)" if result.is_decomposed else ""
            if not result.success and result.error:
                suffix += f" — {result.error}"
        lines.append(f"- Task {result.ac_index + 1}: [{status}] {result.ac_content}{suffix}")
    return "\n".join(lines)


__all__ = [
    "render_parallel_completion_message",
    "render_parallel_verification_report",
]
