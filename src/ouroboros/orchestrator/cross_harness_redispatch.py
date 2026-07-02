"""Cross-harness redispatch decision + event layer (PR-X X1).

When an AC has spent its same-runtime recovery budget and is about to be marked
FAILED, the meta-harness gets one move no single-vendor harness has: hand the
*same* AC, unchanged, to a *different* runtime backend. This module is the pure,
testable brain of that move — the ``parallel_executor`` hook is a thin imperative
shell that (1) asks :func:`decide_alt_harness_redispatch` whether/where to go,
(2) re-runs the AC once on the chosen backend, and (3) emits
:func:`create_alt_harness_redispatch_event` so the from→to switch is observable.

Keeping the logic here honors the scope wall: the concurrently-edited retry area
of ``parallel_executor`` only gains a few lines that call into this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.failure_taxonomy import (
    FailureClass,
    RecoveryAction,
    RecoveryPolicy,
    alt_harness_policy,
)
from ouroboros.orchestrator.runtime_picker import pick_alternative_runtime

# One alt-harness redispatch per AC, ever. A second failure on the alternative
# is a genuine failure, not a reason to keep hopping harnesses.
MAX_ALT_HARNESS_REDISPATCHES_PER_AC = 1

ALT_HARNESS_REDISPATCH_EVENT = "execution.ac.alt_harness_redispatched"

# Markers that a terminal failure came from a provider that stayed overloaded
# after the providers-retry core (adapter.py) exhausted its transient budget.
# Matched case-insensitively against the surfaced error text.
_TRANSIENT_EXHAUSTION_MARKERS: tuple[str, ...] = (
    "429",
    "529",
    "overloaded",
    "rate limit",
    "rate_limit",
    "too many requests",
    "service unavailable",
    "503",
)


def looks_transient_exhausted(error: str | None) -> bool:
    """Best-effort: does this terminal error read like exhausted transient retries?

    The exec adapter retries transient 429/529 a few times, then yields a
    terminal failure whose message still names the overload/rate-limit condition.
    We can only pattern-match that surfaced text here; a miss simply means no
    alt-harness redispatch for the transient case (today's path), never a crash.
    """
    if not error:
        return False
    lowered = error.lower()
    return any(marker in lowered for marker in _TRANSIENT_EXHAUSTION_MARKERS)


@dataclass(frozen=True, slots=True)
class AltHarnessDecision:
    """Outcome of the cross-harness redispatch decision for one failed AC."""

    should_redispatch: bool
    from_backend: str | None
    to_backend: str | None
    policy: RecoveryPolicy | None
    reason: str

    @property
    def failure_action(self) -> RecoveryAction | None:
        """The recovery action chosen, if any."""
        return self.policy.action if self.policy is not None else None


def decide_alt_harness_redispatch(
    *,
    enabled: bool,
    from_backend: str | None,
    failure: FailureClass | None,
    already_redispatched: bool,
    stall_retries_exhausted: bool = False,
    transient_exhausted: bool = False,
    exclude: set[str] | None = None,
    weights: Mapping[str, float] | None = None,
) -> AltHarnessDecision:
    """Decide whether — and to which backend — to redispatch a failed AC.

    Pure and deterministic given its inputs. Precedence:

    1. ``enabled`` gate (config flag) — off ⇒ never redispatch.
    2. Cap — ``already_redispatched`` ⇒ never redispatch again (max 1 per AC).
    3. Policy — :func:`alt_harness_policy` must yield ``REDISPATCH_ALT_HARNESS``
       for the (failure class, stall-exhaustion, transient-exhaustion) inputs.
    4. Availability/capability — :func:`pick_alternative_runtime` must return a
       healthy, distinct, runtime-capable alternative (weights break ties only).

    Any gate failing yields ``should_redispatch=False`` with a human-readable
    ``reason``, so the caller keeps today's failure path untouched.
    """
    if not enabled:
        return AltHarnessDecision(False, from_backend, None, None, "disabled_by_config")
    if already_redispatched:
        return AltHarnessDecision(False, from_backend, None, None, "alt_harness_cap_reached")

    policy = alt_harness_policy(
        failure,
        stall_retries_exhausted=stall_retries_exhausted,
        transient_exhausted=transient_exhausted,
    )
    if policy is None or policy.action is not RecoveryAction.REDISPATCH_ALT_HARNESS:
        return AltHarnessDecision(False, from_backend, None, None, "failure_class_not_eligible")

    excluded = set(exclude or set())
    if from_backend:
        excluded.add(from_backend)
    alternative = pick_alternative_runtime(from_backend or "", exclude=excluded, weights=weights)
    if alternative is None:
        return AltHarnessDecision(False, from_backend, None, policy, "no_alternative_runtime")

    return AltHarnessDecision(
        should_redispatch=True,
        from_backend=from_backend,
        to_backend=alternative,
        policy=policy,
        reason="alt_harness_redispatch_selected",
    )


def create_alt_harness_redispatch_event(
    *,
    session_id: str | None,
    ac_index: int,
    ac_id: str,
    execution_id: str | None,
    decision: AltHarnessDecision,
    redispatch_index: int = 1,
    failure_class: str | None = None,
) -> BaseEvent:
    """Build the observable from→to cross-harness redispatch event.

    Emitted before the same AC is re-run on ``decision.to_backend`` so an
    operator can see exactly which harness handed off to which, and why.
    """
    return BaseEvent(
        type=ALT_HARNESS_REDISPATCH_EVENT,
        aggregate_type="execution",
        aggregate_id=ac_id,
        data={
            "session_id": session_id,
            "execution_id": execution_id,
            "ac_index": ac_index,
            "ac_id": ac_id,
            "from_backend": decision.from_backend,
            "to_backend": decision.to_backend,
            "failure_class": failure_class,
            "recovery_action": (decision.failure_action.value if decision.failure_action else None),
            "rationale": decision.policy.rationale if decision.policy else None,
            "reason": decision.reason,
            "redispatch_index": redispatch_index,
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


__all__ = [
    "ALT_HARNESS_REDISPATCH_EVENT",
    "MAX_ALT_HARNESS_REDISPATCHES_PER_AC",
    "AltHarnessDecision",
    "create_alt_harness_redispatch_event",
    "decide_alt_harness_redispatch",
    "looks_transient_exhausted",
]
