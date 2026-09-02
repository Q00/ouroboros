"""The auto pipeline's grade gate: its verdict, and the codes its terminals stop with.

Grade-gate terminals are the pipeline's most common BLOCKED outcome and were
the last blocker family still leaving ``stop_reason_code`` at ``None``. The
consequence is downstream: ``ooo auto`` prints ``-``, the MCP envelope omits
the key, harness traces write null, and ``attention_relay`` has nothing to
relay -- so every consumer had to read English prose to tell "the grade is too
low" (raise the grade) from "the review withheld ``may_run``" (the grade bar
was already cleared, so raising it is the wrong next step) from "a degraded
Seed still carries hard safety blockers" (resolve the markers).

Module-level consts, so every call site emits the same string and the alphabet
of valid codes stays discoverable by reading a module surface -- the same
convention as the interview layer's ``UNSTUCK_EXHAUSTED_STOP_REASON_CODE`` and
the runtime's ``watchdog_wall_clock_exceeded``.

The gate's own verdict lives here beside them rather than in ``pipeline``,
because a code that is decided in one module and named in another is a code
that can drift from what it names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.auto.seed_reviewer import SeedReview
    from ouroboros.auto.state import AutoPipelineState

SEED_GRADE_BELOW_REQUIRED_STOP_REASON_CODE = "seed_grade_below_required"
SEED_REVIEW_WITHHELD_RUN_STOP_REASON_CODE = "seed_review_withheld_run"
DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE = "degraded_seed_safety_blockers"

_GRADE_RANK = {"A": 0, "B": 1, "C": 2}


def grade_meets_required(actual: str | None, required: str) -> bool:
    """Whether ``actual`` is at least as good as ``required``; unknown never is."""
    if actual not in _GRADE_RANK or required not in _GRADE_RANK:
        return False
    return _GRADE_RANK[actual] <= _GRADE_RANK[required]


def degraded_seed_safety_blocker(review: SeedReview) -> tuple[str, str] | None:
    """Return ``(blocker, stop_reason_code)`` if a degraded Seed still has blockers.

    A Seed synthesized under deadline pressure may surface as a partial product,
    but never while hard safety markers remain unresolved -- so this terminal is
    typed beside the other two rather than left as prose at the call site.
    """
    if not review.grade_result.blockers:
        return None
    codes = ", ".join(finding.code for finding in review.grade_result.blockers)
    return (
        "Degraded seed retains hard safety blockers; "
        f"unsafe/destructive markers must be resolved before run: {codes}",
        DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE,
    )


def seed_review_gate_blocker(
    state: AutoPipelineState, review: SeedReview | None, *, skip_run: bool
) -> tuple[str, str] | None:
    """Return ``(blocker, stop_reason_code)`` for the deterministic review gate.

    The code travels with the message because these are the *same* terminals
    the inline REVIEW-phase gate raises: returning the prose alone would force
    callers to re-derive the classification the gate has already computed,
    which is exactly the prose-parsing this typing exists to remove.
    """
    if review is None:
        return None
    if not grade_meets_required(review.grade_result.grade.value, state.required_grade):
        return (
            f"Seed grade {review.grade_result.grade.value} did not meet "
            f"required grade {state.required_grade}",
            SEED_GRADE_BELOW_REQUIRED_STOP_REASON_CODE,
        )
    if not review.may_run and not skip_run:
        return (
            "Seed review did not clear the Seed for execution",
            SEED_REVIEW_WITHHELD_RUN_STOP_REASON_CODE,
        )
    return None
