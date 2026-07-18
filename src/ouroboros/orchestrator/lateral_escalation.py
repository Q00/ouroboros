"""Lateral-persona escalation ladder for a root AC stuck at maximum strength.

Blindly retrying an identical configuration forever cannot tell "needs one
more nudge" from "structurally impossible", and gives an operator no
visibility. This module is the pure, deterministic decision point for what
to do once a root AC keeps failing verification even at its MOST expensive
configuration: atomic (never decomposed), the frontier model tier, and max
reasoning effort.

It deliberately reuses two things that already exist rather than inventing
parallel machinery:

* "Terminal state" is not a new concept — :func:`is_terminal_state_failure`
  reads it straight off :mod:`ouroboros.orchestrator.model_routing`'s
  ``DEFAULT_TIER_CEILING`` ("frontier") and
  :mod:`ouroboros.orchestrator.effort_routing`'s ``DEFAULT_EFFORT_CEILING``
  ("xhigh") — the exact ceilings those modules already climb retries toward.
* Persona selection reuses
  :func:`ouroboros.auto.lateral_routing.select_persona_for_qa_failure`, the
  SAME stagnation-pattern-to-persona routing convention the UNSTUCK_LATERAL
  auto-pipeline phase already uses, rather than a second persona-selection
  policy.

After :data:`_LATERAL_ESCALATION_THRESHOLD` consecutive terminal-state
failures, :func:`advance_lateral_escalation` starts handing back a NEW
persona (never one already tried for this AC) instead of "retry the same
thing again". Once all 5 personas are exhausted, it flags the AC ``parked``:
the caller must never surface a final FAILED status for a parked AC, but
should keep retrying at a much longer, operator-visible backoff cadence
(:class:`~ouroboros.config.models.EconomicsConfig.parked_retry_backoff_seconds`)
so the AC is never silently abandoned.

**The actual persona-cycling order is PATTERN-AWARE, not a fixed linear
list** (Fix 8, round 2 review — clarifying a docs/behavior mismatch, not a
functional bug). Because persona selection reuses
:func:`~ouroboros.auto.lateral_routing.select_persona_for_qa_failure`
verbatim, this ladder inherits that function's REAL selection order:

1. The failure text is classified into a
   :class:`~ouroboros.resilience.stagnation.StagnationPattern` (SPINNING /
   OSCILLATION / NO_DRIFT / DIMINISHING_RETURNS) and routed to that
   pattern's affinity persona first (``hacker`` / ``architect`` /
   ``researcher`` / ``simplifier`` respectively).
2. ``contrarian`` next, as that function's universal fallback — REGARDLESS
   OF PATTERN — once the primary has already been tried.
3. Then whatever remains of the deterministic chain
   ``hacker → architect → researcher → simplifier``.

For a GENERIC/unclassified failure (the common case here: this ladder only
ever passes a single free-form failure-text string, no structured QA
differences/suggestions, so an ambiguous or keyword-free failure classifies
as the SPINNING fallback), the resulting order is therefore
``hacker → contrarian → architect → researcher → simplifier`` — CONTRARIAN
SECOND, not last. An earlier PR description for this ladder stated a
simplified fixed order (hacker → architect → researcher → simplifier →
contrarian) that does not match this actual, intentional behavior; that
description was corrected rather than this selector, because
``select_persona_for_qa_failure`` is a SHARED, already-tested contract the
UNSTUCK_LATERAL auto-pipeline (RFC #809 Phase 2.2b) also depends on —
changing its order here would be a behavior change for that other caller
too, not a bug fix.

A genuinely infra-fatal failure (adapter crash, auth failure, an uncaught
exception — anything that is not a structured, retryable verify-gate/quality
failure) must never enter this ladder at all. That distinction already exists
(``ParallelACExecutor._is_retryable_failure``); this module has no opinion on
it and trusts the caller to gate entry accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.auto.lateral_routing import select_persona_for_qa_failure
from ouroboros.orchestrator.effort_routing import DEFAULT_EFFORT_CEILING
from ouroboros.orchestrator.model_routing import DEFAULT_TIER_CEILING
from ouroboros.resilience.lateral import LateralThinker, LateralThinkingResult, ThinkingPersona

# After this many CONSECUTIVE terminal-state failures for the same root AC,
# stop retrying the identical configuration and start cycling personas.
_LATERAL_ESCALATION_THRESHOLD = 2

_ALL_PERSONAS: tuple[ThinkingPersona, ...] = tuple(ThinkingPersona)
TOTAL_PERSONA_COUNT = len(_ALL_PERSONAS)


def is_terminal_state_failure(
    *,
    success: bool,
    is_decomposed: bool,
    model_tier: str | None,
    effort_level: str | None,
    tier_ceiling: str = DEFAULT_TIER_CEILING,
    effort_ceiling: str = DEFAULT_EFFORT_CEILING,
) -> bool:
    """Whether a failed AC ran at its MOST expensive configuration already.

    "Most expensive" means: atomic (``is_decomposed`` is ``False`` — a
    decomposed node has a cheaper lever left to pull, namely NOT
    decomposing), the frontier model tier, and max reasoning effort.

    This ladder only applies when at least one real escalation dial is
    actually configured (``model_tier`` and/or ``effort_level`` is not
    ``None``). When BOTH are ``None`` — model routing and effort routing are
    fully dormant, the common case for a run with no economics/effort config
    at all — there is no "maximum strength" to have exhausted, so this is
    deliberately ``False`` rather than vacuously ``True``: an executor with no
    escalation dial configured must keep its unmodified give-up-after-N-retries
    behavior, not silently retry every ordinary failure forever. When exactly
    one axis is dormant and the other is configured, the dormant axis is
    treated as trivially "at ceiling" (it has nowhere higher to climb), so the
    ladder can still engage once the ACTIVE axis alone maxes out.

    Args:
        success: Whether the AC's last attempt succeeded. A successful AC is
            never in a terminal-state-FAILURE (trivially ``False``).
        is_decomposed: Whether the last attempt was a decomposition round
            rather than an atomic dispatch.
        model_tier: The model tier the last attempt actually ran at, or
            ``None`` when model routing is dormant for this run.
        effort_level: The reasoning-effort level the last attempt actually
            ran at, or ``None`` when effort routing is dormant.
        tier_ceiling: The strongest model tier (see
            :data:`ouroboros.orchestrator.model_routing.DEFAULT_TIER_CEILING`).
        effort_ceiling: The strongest effort level (see
            :data:`ouroboros.orchestrator.effort_routing.DEFAULT_EFFORT_CEILING`).

    Returns:
        ``True`` only for a genuine failure at the top of an ACTUALLY
        configured ladder.
    """
    if success or is_decomposed:
        return False
    if model_tier is None and effort_level is None:
        return False
    at_frontier_tier = model_tier is None or model_tier == tier_ceiling
    at_max_effort = effort_level is None or effort_level == effort_ceiling
    return at_frontier_tier and at_max_effort


@dataclass(frozen=True, slots=True)
class LateralEscalationState:
    """Per-root-AC escalation ladder state, carried across retries.

    Attributes:
        consecutive_terminal_failures: How many terminal-state failures in a
            row this root AC has produced. Reset to 0 by any non-terminal
            (or successful) attempt — the ladder only engages for an AC that
            is STUCK at maximum strength, not one merely mid-ladder.
        personas_tried: Personas already handed to this AC, in the order
            they were tried. Never repeated.
        parked: Whether all personas have been exhausted and this AC has
            transitioned to the long-backoff "parked for operator" state.
    """

    consecutive_terminal_failures: int = 0
    personas_tried: tuple[ThinkingPersona, ...] = ()
    parked: bool = False

    @property
    def escalation_active(self) -> bool:
        """Whether persona cycling (or parking) governs the NEXT retry."""
        return self.consecutive_terminal_failures >= _LATERAL_ESCALATION_THRESHOLD


@dataclass(frozen=True, slots=True)
class LateralEscalationStep:
    """What :func:`advance_lateral_escalation` decided for the NEXT retry.

    Attributes:
        state: The updated state to carry into the following call.
        persona: A newly-selected persona to frame the next retry prompt
            with, or ``None`` when the ladder has not engaged yet, or the AC
            is already parked (no new persona to offer).
        just_parked: ``True`` exactly on the transition step where the last
            persona was exhausted and ``state.parked`` became ``True``.
        apply_long_backoff: ``True`` once (and for every step after) the AC
            is parked — the caller must sleep the configured
            ``parked_retry_backoff_seconds`` before its next redispatch
            instead of the ordinary short retry cadence.
    """

    state: LateralEscalationState
    persona: ThinkingPersona | None
    just_parked: bool
    apply_long_backoff: bool


def advance_lateral_escalation(
    state: LateralEscalationState,
    *,
    terminal_state_failure: bool,
    failure_text: str = "",
) -> LateralEscalationStep:
    """Compute the next escalation step from the latest attempt's outcome.

    Args:
        state: The AC's current ladder state (start from
            ``LateralEscalationState()`` for a fresh root AC).
        terminal_state_failure: Whether the attempt that just finished was a
            failure at maximum strength (see :func:`is_terminal_state_failure`).
            A ``False`` value resets the streak — this AC is not stuck at the
            top of the ladder, so ordinary retry/escalation still applies.
        failure_text: Free-form failure text (error/reason/failure_class)
            used only to classify the stagnation pattern for persona
            selection — the SAME classification
            :func:`ouroboros.auto.lateral_routing.select_persona_for_qa_failure`
            already performs.

    Returns:
        A :class:`LateralEscalationStep` describing what the caller should do
        next.
    """
    if not terminal_state_failure:
        return LateralEscalationStep(
            state=LateralEscalationState(),
            persona=None,
            just_parked=False,
            apply_long_backoff=False,
        )

    streak = state.consecutive_terminal_failures + 1

    if state.parked:
        # Already exhausted every persona in an earlier round: stay parked,
        # keep retrying at the long backoff cadence, no new persona to offer.
        parked_state = LateralEscalationState(
            consecutive_terminal_failures=streak,
            personas_tried=state.personas_tried,
            parked=True,
        )
        return LateralEscalationStep(
            state=parked_state, persona=None, just_parked=False, apply_long_backoff=True
        )

    if streak < _LATERAL_ESCALATION_THRESHOLD:
        return LateralEscalationStep(
            state=LateralEscalationState(
                consecutive_terminal_failures=streak,
                personas_tried=state.personas_tried,
                parked=False,
            ),
            persona=None,
            just_parked=False,
            apply_long_backoff=False,
        )

    # Pattern-aware selection (see the module docstring's "actual
    # persona-cycling order" section): the primary pattern-affinity persona
    # first, then ``contrarian`` as the universal fallback, then whatever
    # remains of hacker/architect/researcher/simplifier. NOT a fixed linear
    # list ending in contrarian.
    next_persona = select_persona_for_qa_failure(
        (failure_text,) if failure_text else (),
        (),
        already_tried_personas=state.personas_tried,
    )
    if next_persona is None:
        parked_state = LateralEscalationState(
            consecutive_terminal_failures=streak,
            personas_tried=state.personas_tried,
            parked=True,
        )
        return LateralEscalationStep(
            state=parked_state, persona=None, just_parked=True, apply_long_backoff=True
        )

    new_state = LateralEscalationState(
        consecutive_terminal_failures=streak,
        personas_tried=(*state.personas_tried, next_persona),
        parked=False,
    )
    return LateralEscalationStep(
        state=new_state, persona=next_persona, just_parked=False, apply_long_backoff=False
    )


def build_persona_retry_prompt(
    *,
    persona: ThinkingPersona,
    ac_content: str,
    current_approach: str,
    failed_attempts: tuple[str, ...] = (),
    thinker: LateralThinker | None = None,
) -> str:
    """Build a genuinely different, persona-framed retry prompt section.

    Thin wrapper over :class:`~ouroboros.resilience.lateral.LateralThinker`
    (the existing persona-prompt machinery) so the escalation ladder never
    falls back to "just try again" once it engages.
    """
    result = (thinker or LateralThinker()).generate_alternative(
        persona=persona,
        problem_context=ac_content,
        current_approach=current_approach,
        failed_attempts=failed_attempts,
    )
    if result.is_err:
        # Deterministic, always-available fallback: never raise out of a
        # retry-prompt builder. ``LateralThinker`` is pure/local (loads
        # bundled persona .md files) so this path is defensive, not expected.
        return (
            f"## Persona: {persona.value.title()}\n"
            f"{persona.description}\n\n"
            "## Problem Context\n" + ac_content
        )
    thinking: LateralThinkingResult = result.value
    return thinking.prompt


__all__ = [
    "TOTAL_PERSONA_COUNT",
    "LateralEscalationState",
    "LateralEscalationStep",
    "advance_lateral_escalation",
    "build_persona_retry_prompt",
    "is_terminal_state_failure",
]
