"""Per-AC checklist aggregation for evaluation pipeline (#366).

The evaluation pipeline already evaluates one acceptance criterion at a time
via ``EvaluationContext.current_ac``.  Orchestration layers
(``parallel_executor``, ``runner``) already dispatch one evaluation per AC.

What was missing is a lightweight, reusable aggregator that:

1. Collects individual ``EvaluationResult`` objects into a single
   checklist view (pass/fail per AC, evidence, questions used).
2. Computes aggregate metrics the user can read at a glance
   (pass rate, failed items, overall verdict).
3. Generates actionable feedback for the Run stage when items fail.

This module is **purely additive**.  Nothing in the evaluation pipeline
is required to use it — existing callers remain unchanged.  Callers that
want the checklist view simply pass their collected results through
``aggregate_results``.

Ties in with Phase 1 (#363) milestones: when the interview reaches the
READY milestone (ambiguity <= 0.2), every AC from that Seed should pass
when routed through this checklist.  That is the concrete quality gate
the milestones promise.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.evaluation.models import EvaluationResult


@dataclass(frozen=True, slots=True)
class ACCheckItem:
    """Per-AC checklist entry.

    Attributes:
        ac_text: The acceptance criterion as written in the Seed.
        passed: True when the evaluation pipeline approved this AC.
        reasoning: Evaluator's explanation (from Stage 2 reasoning).
        evidence: Concrete evidence the evaluator relied on (#367 field).
        questions_used: Socratic questions the evaluator asked (#367 field).
        failure_reason: Human-readable reason when ``passed`` is False.
            This is the feedback a Run re-attempt should address.
    """

    ac_text: str
    passed: bool
    reasoning: str = ""
    evidence: tuple[str, ...] = ()
    questions_used: tuple[str, ...] = ()
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ACChecklistResult:
    """Aggregated checklist across multiple AC evaluations.

    Attributes:
        items: Per-AC entries in the same order they were provided.
        total: Total number of AC items.
        passed_count: Number of items with ``passed == True``.
    """

    items: tuple[ACCheckItem, ...]
    total: int
    passed_count: int

    @property
    def failed_items(self) -> tuple[ACCheckItem, ...]:
        """Return only items whose evaluation did not pass."""
        return tuple(item for item in self.items if not item.passed)

    @property
    def all_passed(self) -> bool:
        """True when every AC passed. Empty checklist returns False.

        An empty checklist is treated as "not ready" — a Seed with zero
        ACs is definitionally incomplete (see Phase 1 milestone READY
        criteria), so we refuse to mark it complete.
        """
        return self.total > 0 and self.passed_count == self.total

    @property
    def pass_rate(self) -> float:
        """Ratio of passed items to total (0.0 for empty checklist)."""
        if self.total == 0:
            return 0.0
        return self.passed_count / self.total


def _evaluation_passed(result: EvaluationResult) -> bool:
    """Return True when an EvaluationResult represents an overall pass."""
    return result.final_approved


def _derive_failure_reason(result: EvaluationResult) -> str | None:
    """Extract a concise failure reason from an EvaluationResult."""
    if result.final_approved:
        return None
    reason = result.failure_reason
    if reason:
        return reason
    # Fallback: Stage 2 reasoning if available.
    if result.stage2_result and result.stage2_result.reasoning:
        return result.stage2_result.reasoning
    return "Evaluation did not approve this AC."


def _extract_stage2_fields(
    result: EvaluationResult,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Pull reasoning / evidence / questions_used from Stage 2, if present."""
    if result.stage2_result is None:
        return "", (), ()
    s2 = result.stage2_result
    return (
        s2.reasoning,
        getattr(s2, "evidence", ()),
        getattr(s2, "questions_used", ()),
    )


def aggregate_results(
    ac_texts: tuple[str, ...],
    results: tuple[EvaluationResult, ...],
) -> ACChecklistResult:
    """Aggregate per-AC EvaluationResult objects into a checklist.

    Args:
        ac_texts: The acceptance criteria, in the same order their
            evaluations were performed.
        results: Parallel evaluation results, one per AC.

    Returns:
        Aggregated ``ACChecklistResult``.

    Raises:
        ValueError: If ``ac_texts`` and ``results`` have different lengths.
    """
    if len(ac_texts) != len(results):
        msg = (
            f"ac_texts and results must be the same length "
            f"(got {len(ac_texts)} ACs vs {len(results)} results)"
        )
        raise ValueError(msg)

    items: list[ACCheckItem] = []
    for ac, result in zip(ac_texts, results, strict=True):
        passed = _evaluation_passed(result)
        reasoning, evidence, questions_used = _extract_stage2_fields(result)
        items.append(
            ACCheckItem(
                ac_text=ac,
                passed=passed,
                reasoning=reasoning,
                evidence=evidence,
                questions_used=questions_used,
                failure_reason=_derive_failure_reason(result),
            )
        )

    passed_count = sum(1 for item in items if item.passed)
    return ACChecklistResult(
        items=tuple(items),
        total=len(items),
        passed_count=passed_count,
    )


def format_checklist(checklist: ACChecklistResult) -> str:
    """Render a human-readable checklist for display to the user.

    Output is intentionally plain text so callers can embed it in MCP
    responses, CLI output, or TUI panels without extra formatting.

    Args:
        checklist: Aggregated checklist to render.

    Returns:
        Multi-line string suitable for direct display.
    """
    lines: list[str] = []
    header_status = "ALL PASSED" if checklist.all_passed else "INCOMPLETE"
    lines.append(
        f"Acceptance Criteria Checklist [{header_status}] "
        f"({checklist.passed_count}/{checklist.total} passed, "
        f"{checklist.pass_rate:.0%})"
    )
    lines.append("=" * 60)

    for index, item in enumerate(checklist.items, start=1):
        marker = "[x]" if item.passed else "[ ]"
        lines.append(f"{marker} {index}. {item.ac_text}")
        if not item.passed and item.failure_reason:
            lines.append(f"    Reason: {item.failure_reason}")
        if item.questions_used:
            lines.append("    Questions Used:")
            for question in item.questions_used:
                lines.append(f"      - {question}")
        if item.evidence:
            lines.append("    Evidence:")
            for evidence in item.evidence:
                lines.append(f"      - {evidence}")

    if not checklist.all_passed and checklist.failed_items:
        lines.append("")
        lines.append("Next Steps:")
        lines.append(f"  {len(checklist.failed_items)} AC(s) need to be re-addressed in Run.")

    return "\n".join(lines)


def build_run_feedback(checklist: ACChecklistResult) -> tuple[str, ...]:
    """Build structured feedback to hand back to the Run stage.

    Only failed items contribute feedback.  Each entry identifies the
    AC text and the specific failure reason so the Run re-attempt can
    target the gap instead of regenerating everything.

    Args:
        checklist: Aggregated checklist.

    Returns:
        Tuple of feedback strings, one per failed AC.
    """
    feedback: list[str] = []
    for item in checklist.failed_items:
        reason = item.failure_reason or "Evaluation did not approve this AC."
        feedback.append(f"AC not met: {item.ac_text} — {reason}")
    return tuple(feedback)


__all__ = [
    "ACCheckItem",
    "ACChecklistResult",
    "aggregate_results",
    "build_run_feedback",
    "format_checklist",
]
