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
convention as the runtime's ``watchdog_wall_clock_exceeded``.

The gate's own verdict lives here beside them rather than in ``pipeline``,
because a code that is decided in one module and named in another is a code
that can drift from what it names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.auto.seed_reviewer import SeedReview
    from ouroboros.auto.state import AutoPipelineState
    from ouroboros.core.seed import Seed

SEED_GRADE_BELOW_REQUIRED_STOP_REASON_CODE = "seed_grade_below_required"
SEED_REVIEW_WITHHELD_RUN_STOP_REASON_CODE = "seed_review_withheld_run"
DEGRADED_SEED_SAFETY_BLOCKERS_STOP_REASON_CODE = "degraded_seed_safety_blockers"
# A degraded (deadline-recovery) Seed without the user's goal on record is
# emitted for the user but never executed, so the session stops here instead
# of reporting complete.
DEGRADED_SEED_NOT_EXECUTED_STOP_REASON_CODE = "degraded_seed_not_executed"

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


def degraded_seed_runs_as_stated(seed: Seed) -> bool:
    """Whether a degraded Seed runs on the user's request as stated.

    A degraded Seed comes from the interview deadline, so some slots were never
    answered. When it carries ``sourced_scope`` (the user's goal verbatim,
    stamped from ledger provenance), the worker is told only that record, with
    "Resolve the request as stated in the goal." as the objective when nothing
    binds: the Seed's smoke criterion and unresolved-slot constraints never
    reach it. Such a Seed runs like any auto Seed, and its unresolved slots stay
    in ``metadata.unresolved_slots`` and ``open_questions``. Without the record
    there is no sourced goal to run, so the Seed stays a partial product.
    """
    return bool(getattr(seed.metadata, "degraded", False)) and seed.sourced_scope is not None


def degraded_seed_not_executed_blocker(unresolved_slots: int) -> tuple[str, str]:
    """Return ``(blocker, stop_reason_code)`` for the partial-product stop.

    The degraded Seed is emitted for the user but never executed, so the
    session must not report complete.
    """
    return (
        "Degraded seed emitted as a partial product and not executed; "
        f"{unresolved_slots} unresolved slot(s) need an answer before a run",
        DEGRADED_SEED_NOT_EXECUTED_STOP_REASON_CODE,
    )


def is_degraded_seed_not_executed_stop(state: AutoPipelineState) -> bool:
    """Whether ``state`` stopped at the partial-product terminal."""
    return (
        state.phase.value == "blocked"
        and state.last_error_code == DEGRADED_SEED_NOT_EXECUTED_STOP_REASON_CODE
    )


def seed_review_gate_blocker(
    state: AutoPipelineState,
    review: SeedReview | None,
    *,
    skip_run: bool,
    runs_degraded: bool = False,
) -> tuple[str, str] | None:
    """Return ``(blocker, stop_reason_code)`` for the deterministic review gate.

    The code travels with the message because these are the *same* terminals
    the inline REVIEW-phase gate raises: returning the prose alone would force
    callers to re-derive the classification the gate has already computed,
    which is exactly the prose-parsing this typing exists to remove.

    ``runs_degraded`` (see :func:`degraded_seed_runs_as_stated`) keeps only the
    safety bar: the grade gate reports each unresolved slot of a degraded Seed
    as a finding, so its grade stays below A by construction and the grade and
    ``may_run`` bars would stop every such Seed.
    """
    if review is None:
        return None
    if runs_degraded:
        return degraded_seed_safety_blocker(review)
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
