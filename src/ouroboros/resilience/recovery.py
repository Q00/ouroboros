"""Recovery planning shared by run loops and lateral thinking tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ouroboros.events.base import BaseEvent
from ouroboros.resilience.lateral import LateralThinker, ThinkingPersona
from ouroboros.resilience.stagnation import StagnationPattern


class RecoveryActionKind(StrEnum):
    """Planner actions for a stalled or failed execution."""

    CONTINUE = "continue"
    INJECT_LATERAL_DIRECTIVE = "inject_lateral_directive"
    STAGNATED = "stagnated"


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """Execution state used to decide whether to intervene."""

    problem_context: str
    current_approach: str
    messages_processed: int = 0
    completed_count: int = 0
    total_count: int = 0
    final_error: str = ""
    stagnation_pattern: StagnationPattern | None = None
    failed_attempts: tuple[str, ...] = ()
    used_personas: tuple[ThinkingPersona, ...] = ()
    interventions_used: int = 0


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    """A concrete recovery decision."""

    kind: RecoveryActionKind
    reason: str
    pattern: StagnationPattern | None = None
    persona: ThinkingPersona | None = None
    directive: str = ""

    @classmethod
    def continue_(cls, reason: str) -> RecoveryAction:
        """Return a no-op recovery action."""
        return cls(kind=RecoveryActionKind.CONTINUE, reason=reason)

    @classmethod
    def stagnated(cls, reason: str, pattern: StagnationPattern | None = None) -> RecoveryAction:
        """Return a terminal stagnation action."""
        return cls(kind=RecoveryActionKind.STAGNATED, reason=reason, pattern=pattern)


class RecoveryPlanner:
    """Choose bounded lateral recovery actions for execution loops."""

    def __init__(
        self,
        *,
        lateral_thinker: LateralThinker | None = None,
        max_interventions: int = 1,
    ) -> None:
        self._lateral_thinker = lateral_thinker or LateralThinker()
        self._max_interventions = max_interventions

    def plan(self, snapshot: RecoverySnapshot) -> RecoveryAction:
        """Return the next recovery action for the supplied execution snapshot."""
        if snapshot.interventions_used >= self._max_interventions:
            return RecoveryAction.stagnated(
                "Recovery intervention budget exhausted",
                snapshot.stagnation_pattern,
            )

        pattern = snapshot.stagnation_pattern or self._infer_pattern(snapshot)
        persona = suggest_lateral_persona_for_pattern(
            pattern,
            failed_attempts=snapshot.failed_attempts,
            used_personas=snapshot.used_personas,
            lateral_thinker=self._lateral_thinker,
        )
        if persona is None:
            return RecoveryAction.stagnated(
                "No available lateral thinking persona remains after exclusions",
                pattern,
            )

        lateral_result = self._lateral_thinker.generate_alternative(
            persona=persona,
            problem_context=snapshot.problem_context,
            current_approach=snapshot.current_approach,
            failed_attempts=snapshot.failed_attempts,
        )
        if lateral_result.is_err:
            return RecoveryAction.stagnated(str(lateral_result.error), pattern)

        lateral = lateral_result.unwrap()
        reason = self._reason_for(pattern, snapshot)
        directive = (
            "## Lateral Recovery Directive\n"
            f"Detected pattern: {pattern.value}\n"
            f"Selected persona: {persona.value}\n"
            f"Reason: {reason}\n\n"
            "Do not repeat the failed path. Continue the same task, but switch "
            "strategy using the lateral prompt below. Produce a concrete patch "
            "or verification step that advances the acceptance criteria.\n\n"
            f"{lateral.prompt}"
        )
        return RecoveryAction(
            kind=RecoveryActionKind.INJECT_LATERAL_DIRECTIVE,
            reason=reason,
            pattern=pattern,
            persona=persona,
            directive=directive,
        )

    @staticmethod
    def _infer_pattern(snapshot: RecoverySnapshot) -> StagnationPattern:
        if snapshot.final_error.strip():
            return StagnationPattern.SPINNING
        if (
            snapshot.total_count
            and snapshot.completed_count <= 0
            and snapshot.messages_processed >= 10
        ):
            return StagnationPattern.NO_DRIFT
        return StagnationPattern.DIMINISHING_RETURNS

    @staticmethod
    def _reason_for(pattern: StagnationPattern, snapshot: RecoverySnapshot) -> str:
        if snapshot.final_error.strip():
            return "The previous run ended in a final error without satisfying the seed."
        if pattern == StagnationPattern.NO_DRIFT:
            return "No acceptance criteria have completed after sustained execution."
        if pattern == StagnationPattern.DIMINISHING_RETURNS:
            return "Execution appears to be making weaker progress and needs a strategy shift."
        if pattern == StagnationPattern.OSCILLATION:
            return "Execution appears to be alternating between approaches."
        return "Execution appears to be repeating an unproductive path."


def coerce_failed_attempt_personas(
    failed_attempts: tuple[str, ...],
    *,
    used_personas: tuple[ThinkingPersona, ...] = (),
) -> tuple[ThinkingPersona, ...]:
    """Convert failed attempt strings into persona exclusions."""
    excluded: list[ThinkingPersona] = list(used_personas)
    for attempt in failed_attempts:
        try:
            persona = ThinkingPersona(attempt.strip().lower())
        except ValueError:
            continue
        if persona not in excluded:
            excluded.append(persona)
    return tuple(excluded)


def suggest_lateral_persona_for_pattern(
    pattern: StagnationPattern,
    *,
    failed_attempts: tuple[str, ...] = (),
    used_personas: tuple[ThinkingPersona, ...] = (),
    lateral_thinker: LateralThinker | None = None,
) -> ThinkingPersona | None:
    """Suggest a persona for a stagnation pattern with shared exclusion semantics."""
    thinker = lateral_thinker or LateralThinker()
    return thinker.suggest_persona_for_pattern(
        pattern,
        exclude_personas=coerce_failed_attempt_personas(
            failed_attempts,
            used_personas=used_personas,
        ),
    )


def get_run_recovery_protocol_prompt() -> str:
    """Return system prompt instructions for in-run self recovery."""
    return """## Self-Recovery Protocol
If you notice that the run is stalled, repeating the same failed edit, or making
no acceptance-criterion progress, switch strategy before continuing:
- spinning: stop retrying the same fix; isolate or bypass the blocker.
- no_drift: gather the missing fact or inspect the source of truth.
- diminishing_returns: simplify the task and remove unnecessary moving parts.
- oscillation: choose one architecture and make the smallest coherent step.

When you switch strategy, state the detected pattern and the new concrete next
step briefly, then continue implementing and verifying the acceptance criteria."""


def create_recovery_applied_event(
    *,
    execution_id: str,
    session_id: str,
    action: RecoveryAction,
    seed_id: str | None = None,
    messages_processed: int = 0,
    completed_count: int = 0,
    total_count: int = 0,
) -> BaseEvent:
    """Create an event recording an automatic recovery intervention."""
    return BaseEvent(
        type="resilience.recovery.applied",
        aggregate_type="execution",
        aggregate_id=execution_id,
        data={
            "session_id": session_id,
            "seed_id": seed_id,
            "kind": action.kind.value,
            "pattern": action.pattern.value if action.pattern else None,
            "persona": action.persona.value if action.persona else None,
            "reason": action.reason,
            "messages_processed": messages_processed,
            "completed_count": completed_count,
            "total_count": total_count,
        },
    )


__all__ = [
    "RecoveryAction",
    "RecoveryActionKind",
    "RecoveryPlanner",
    "RecoverySnapshot",
    "coerce_failed_attempt_personas",
    "create_recovery_applied_event",
    "get_run_recovery_protocol_prompt",
    "suggest_lateral_persona_for_pattern",
]
