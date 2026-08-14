"""Advisory (non-blocking) surface for the auto Seed-QA gate.

The Seed-QA gate is *optional*: when no ``seed_qa_evaluator`` is wired the auto
pipeline runs the Seed unconditionally. A wired evaluator must therefore never
leave the session in a worse place than having no evaluator at all — it may
repair, annotate and warn, but it may not dead-end a run that would otherwise
have started. Blocking handed the session to a human holding exactly the same
unmapped prose the repair mapper could not act on, which made ``blocked`` a dead
end rather than a decision point.

When the gate gives up (repair budget exhausted, feedback the repair mapper
cannot map, evaluator timeout / error) it therefore emits
:data:`SEED_QA_ADVISORY_EVENT` and lets the pipeline continue. The unresolved
findings stay on ``state.last_qa_*`` — surfaced by the result envelope and by
the event — so the real verdict is deferred to run → evaluate, which judges
execution evidence instead of a static read of the Seed.

The deterministic grade gate keeps its authority: a repaired Seed that no longer
meets ``required_grade`` (or is no longer cleared to run) is still blocked by
``grade_gate``, not by this advisory path. So does the pipeline deadline.

**Emission order.** The event is durable orchestration evidence that the engine
kept driving — ``attention_relay`` classifies it as engine ownership ``active``
with no successor action offered. Emitting it before the retained gates have
spoken would make that evidence lie whenever a gate then stops the session, so
every gate that can still block runs *first* and the event is emitted only on
the true continuation path. The deadline is re-checked after the append as well:
the append is awaited under the observer-drain timeout, which is not charged
against ``deadline_at``, and the skip-run COMPLETE transition downstream
performs no deadline check of its own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ouroboros.auto.state import AutoPipelineState
    from ouroboros.core.seed import Seed

SEED_QA_ADVISORY_EVENT = "auto.seed_qa.advisory_override"
# Correction event for the one case the pre-charge cannot prevent: the append
# completed but the budget is gone anyway (a clock jump, an append that
# overran its own bound). ``attention_relay`` already classifies this type as
# ownership *closed* with a successor action, which is exactly the record the
# session needs once it has stopped — so the durable stream self-corrects
# rather than leaving an "active" claim standing.
SEED_QA_CLOSURE_EVENT = "auto.seed_qa.blocked"

# Bound on the evidence lists carried by the event payload.
_MAX_EVIDENCE = 5


def append_fits_in_deadline(state: AutoPipelineState, *, append_budget_seconds: float) -> bool:
    """Return whether the advisory append can complete inside the remaining budget.

    The event is durable evidence that the engine kept going, and the append is
    itself awaited, so a deadline can only be re-checked *after* it. Charging
    the append's own bound up front closes that window: when the remaining
    budget cannot cover a worst-case append, the session blocks without
    publishing a continuation it will not perform.
    """
    if state.deadline_at is None:
        return True
    return (state.deadline_at - time.monotonic()) > append_budget_seconds


async def publish_advisory_or_block(
    *,
    state: AutoPipelineState,
    seed: Seed,
    reason: str,
    attempts: int,
    score: float | None,
    emit: Callable[[str, str, dict[str, Any]], Awaitable[None]],
    enforce_deadline: Callable[[AutoPipelineState], bool],
    save: Callable[[AutoPipelineState], None],
    append_budget_seconds: float,
    deadline_tool_name: str,
) -> bool:
    """Publish the advisory event. Return True when the session must stop instead.

    Ordering is the whole point. The event is durable evidence that the engine
    kept driving, so it may only exist on a path that actually continues:

    1. an expired budget — or one too small to cover a worst-case append —
       blocks *without* publishing anything, since publishing "still going" and
       then stopping inside that append would leave the conductor an
       active-ownership claim for a Seed that never ran;
    2. if the post-append check trips anyway (a clock jump, an append that
       overran its bound), the stream self-corrects with
       :data:`SEED_QA_CLOSURE_EVENT`, which is classified as ownership closed.
    """
    payload = seed_qa_advisory_payload(state, seed, reason=reason, attempts=attempts, score=score)
    if enforce_deadline(state) or not append_fits_in_deadline(
        state, append_budget_seconds=append_budget_seconds
    ):
        if not state.is_terminal():
            state.mark_blocked(
                "pipeline_timeout: remaining budget cannot cover the advisory append",
                tool_name=deadline_tool_name,
            )
            save(state)
        return True
    await emit(SEED_QA_ADVISORY_EVENT, state.auto_session_id, payload)
    if enforce_deadline(state):
        await emit(
            SEED_QA_CLOSURE_EVENT,
            state.auto_session_id,
            {**payload, "reason": "pipeline_deadline_after_advisory"},
        )
        return True
    return False


def clear_seed_qa_verdict(state: AutoPipelineState) -> None:
    """Drop the previous attempt's verdict before a new Seed-QA attempt runs.

    ``state.last_qa_*`` is durable and is read by surfaces that decide whether a
    product was verified (a persisted ``last_qa_passed=True`` alone makes a
    terminal report ``artifact_state="complete_verified"``). An evaluator that
    times out or raises produces no verdict at all, so without this reset a
    resumed session would keep publishing the *previous* attempt's pass — inside
    an ``evaluator_error`` advisory event, and into its own completion state.
    """
    state.last_qa_score = None
    state.last_qa_verdict = None
    state.last_qa_passed = None
    state.last_qa_differences = []
    state.last_qa_suggestions = []


def seed_qa_advisory_payload(
    state: AutoPipelineState,
    seed: Seed,
    *,
    reason: str,
    attempts: int,
    score: float | None,
) -> dict[str, Any]:
    """Build the :data:`SEED_QA_ADVISORY_EVENT` payload.

    ``reason`` distinguishes a genuine non-passing verdict
    (``repair_budget_exhausted`` / ``seed_qa_feedback_unmapped``) from an
    evaluator-side failure (``evaluator_timeout`` / ``evaluator_error`` /
    ``evaluator_transient_error``), which carries no verdict of its own.
    """
    return {
        "schema_version": 1,
        "auto_session_id": state.auto_session_id,
        "seed_id": seed.metadata.seed_id,
        "attempts": attempts,
        "verdict": state.last_qa_verdict,
        "score": score,
        "differences": list(state.last_qa_differences[:_MAX_EVIDENCE]),
        "suggestions": list(state.last_qa_suggestions[:_MAX_EVIDENCE]),
        "reason": reason,
    }


def seed_qa_advisory_progress(reason: str, detail: str) -> str:
    """Return the non-terminal progress message recorded for the override."""
    return f"Seed QA advisory ({reason}): {detail}; continuing with the current Seed"
