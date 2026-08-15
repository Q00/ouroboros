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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ouroboros.auto.state import AutoPipelineState
    from ouroboros.core.seed import Seed

SEED_QA_ADVISORY_EVENT = "auto.seed_qa.advisory_override"

# Bound on the evidence lists carried by the event payload.
_MAX_EVIDENCE = 5


async def publish_advisory(
    *,
    state: AutoPipelineState,
    seed: Seed,
    reason: str,
    detail: str,
    attempts: int,
    score: float | None,
    emit: Callable[[str, str, dict[str, Any]], Awaitable[None]],
) -> None:
    """Publish the advisory event on a path that has already decided to continue.

    Every gate that can still stop the session runs *before* this — the grade
    gate and the pipeline deadline — so the event is only ever written on the
    continuation path. It is not re-checked afterwards: the append is awaited
    and its latency is real, but a budget that expires during it is the next
    phase's business (RUN enforces the deadline at entry, and skip-run
    completion now does too), not a reason to publish a continuation claim and
    then retract it. Retraction cannot be made reliable anyway — the correction
    append can fail exactly when the original succeeded.
    """
    await emit(
        SEED_QA_ADVISORY_EVENT,
        state.auto_session_id,
        seed_qa_advisory_payload(
            state,
            seed,
            reason=reason,
            detail=detail,
            attempts=attempts,
            score=score,
        ),
    )


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
    detail: str,
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
        "detail": " ".join(detail.split())[:200],
    }


def seed_qa_advisory_progress(reason: str, detail: str) -> str:
    """Return the non-terminal progress message recorded for the override."""
    return f"Seed QA advisory ({reason}): {detail}; continuing with the current Seed"
