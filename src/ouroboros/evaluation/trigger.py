"""Consensus Trigger Matrix.

Implements the 6 trigger conditions for Stage 3 consensus:
1. Seed Modification - Any change to immutable Seed
2. Ontology Evolution - Schema changes
3. Goal Interpretation - Reinterpretation of goal
4. Seed Drift Alert - drift > 0.3
5. Stage 2 Uncertainty - uncertainty > 0.3
6. Lateral Thinking Adoption - Persona suggestion accepted

The ConsensusTrigger is stateless and returns trigger decisions.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ouroboros.core.errors import ValidationError
from ouroboros.core.types import Result
from ouroboros.evaluation.models import SemanticResult
from ouroboros.events.base import BaseEvent
from ouroboros.events.evaluation import create_consensus_triggered_event


class TriggerType(StrEnum):
    """Types of consensus triggers.

    FR16: Consensus Trigger Matrix - 6 trigger conditions
    """

    SEED_MODIFICATION = "seed_modification"
    ONTOLOGY_EVOLUTION = "ontology_evolution"
    GOAL_INTERPRETATION = "goal_interpretation"
    SEED_DRIFT_ALERT = "seed_drift_alert"
    STAGE2_UNCERTAINTY = "stage2_uncertainty"
    LATERAL_THINKING_ADOPTION = "lateral_thinking_adoption"


@dataclass(frozen=True, slots=True)
class TriggerContext:
    """Context for evaluating consensus triggers.

    Attributes:
        execution_id: Execution identifier
        seed_modified: Whether seed was modified
        ontology_changed: Whether ontology schema changed
        goal_reinterpreted: Whether goal was reinterpreted
        drift_score: Current drift score (0.0-1.0)
        uncertainty_score: Stage 2 uncertainty (0.0-1.0)
        lateral_thinking_adopted: Whether lateral thinking was adopted
        semantic_result: Optional Stage 2 result
    """

    execution_id: str
    seed_modified: bool = False
    ontology_changed: bool = False
    goal_reinterpreted: bool = False
    drift_score: float = 0.0
    uncertainty_score: float = 0.0
    lateral_thinking_adopted: bool = False
    semantic_result: SemanticResult | None = None


@dataclass(frozen=True, slots=True)
class TriggerResult:
    """Result of trigger evaluation.

    Attributes:
        should_trigger: Whether consensus should be triggered
        trigger_type: Type of trigger activated (if any)
        reason: Human-readable reason
        details: Additional context
    """

    should_trigger: bool
    trigger_type: TriggerType | None = None
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TriggerConfig:
    """Configuration for trigger thresholds.

    Attributes:
        drift_threshold: Drift score above which to trigger (default 0.3)
        uncertainty_threshold: Uncertainty above which to trigger (default 0.3)
    """

    drift_threshold: float = 0.3
    uncertainty_threshold: float = 0.3


class ConsensusTrigger:
    """Evaluates whether consensus should be triggered.

    Implements FR16: Consensus Trigger Matrix with 6 conditions.
    Stateless - all state passed via TriggerContext.

    Example:
        trigger = ConsensusTrigger()
        result = trigger.evaluate(context)
        if result.should_trigger:
            # Run Stage 3 consensus
    """

    def __init__(self, config: TriggerConfig | None = None) -> None:
        """Initialize trigger evaluator.

        Args:
            config: Trigger configuration
        """
        self._config = config or TriggerConfig()

    def evaluate(
        self,
        context: TriggerContext,
    ) -> Result[tuple[TriggerResult, list[BaseEvent]], ValidationError]:
        """Evaluate all trigger conditions.

        Checks triggers in priority order and returns on first match.

        Args:
            context: Trigger evaluation context

        Returns:
            Result containing TriggerResult and events
        """
        events: list[BaseEvent] = []

        # Check each trigger condition in priority order
        checks = [
            self._check_seed_modification,
            self._check_ontology_evolution,
            self._check_goal_interpretation,
            self._check_seed_drift,
            self._check_stage2_uncertainty,
            self._check_lateral_thinking,
        ]

        for check in checks:
            result = check(context)
            if result.should_trigger:
                # Emit trigger event
                events.append(
                    create_consensus_triggered_event(
                        execution_id=context.execution_id,
                        trigger_type=result.trigger_type.value if result.trigger_type else "unknown",
                        trigger_details=result.details,
                    )
                )
                return Result.ok((result, events))

        # No trigger condition met
        return Result.ok((
            TriggerResult(
                should_trigger=False,
                reason="No trigger conditions met",
            ),
            events,
        ))

    def _check_seed_modification(self, context: TriggerContext) -> TriggerResult:
        """Check for seed modification trigger.

        Seeds are immutable, any modification requires consensus.
        """
        if context.seed_modified:
            return TriggerResult(
                should_trigger=True,
                trigger_type=TriggerType.SEED_MODIFICATION,
                reason="Seed modification detected - requires consensus",
                details={"seed_modified": True},
            )
        return TriggerResult(should_trigger=False)

    def _check_ontology_evolution(self, context: TriggerContext) -> TriggerResult:
        """Check for ontology evolution trigger.

        Schema changes affect output structure and require validation.
        """
        if context.ontology_changed:
            return TriggerResult(
                should_trigger=True,
                trigger_type=TriggerType.ONTOLOGY_EVOLUTION,
                reason="Ontology schema changed - requires consensus",
                details={"ontology_changed": True},
            )
        return TriggerResult(should_trigger=False)

    def _check_goal_interpretation(self, context: TriggerContext) -> TriggerResult:
        """Check for goal interpretation change trigger.

        Reinterpretation of the goal needs diverse verification.
        """
        if context.goal_reinterpreted:
            return TriggerResult(
                should_trigger=True,
                trigger_type=TriggerType.GOAL_INTERPRETATION,
                reason="Goal interpretation changed - requires consensus",
                details={"goal_reinterpreted": True},
            )
        return TriggerResult(should_trigger=False)

    def _check_seed_drift(self, context: TriggerContext) -> TriggerResult:
        """Check for seed drift alert trigger.

        High drift from original seed intent needs verification.
        """
        # Use semantic result drift if available, otherwise use context drift
        drift = context.drift_score
        if context.semantic_result:
            drift = context.semantic_result.drift_score

        if drift > self._config.drift_threshold:
            return TriggerResult(
                should_trigger=True,
                trigger_type=TriggerType.SEED_DRIFT_ALERT,
                reason=f"Drift score {drift:.2f} exceeds threshold {self._config.drift_threshold}",
                details={
                    "drift_score": drift,
                    "threshold": self._config.drift_threshold,
                },
            )
        return TriggerResult(should_trigger=False)

    def _check_stage2_uncertainty(self, context: TriggerContext) -> TriggerResult:
        """Check for Stage 2 uncertainty trigger.

        High uncertainty in semantic evaluation needs multi-model verification.
        """
        # Use semantic result uncertainty if available
        uncertainty = context.uncertainty_score
        if context.semantic_result:
            uncertainty = context.semantic_result.uncertainty

        if uncertainty > self._config.uncertainty_threshold:
            return TriggerResult(
                should_trigger=True,
                trigger_type=TriggerType.STAGE2_UNCERTAINTY,
                reason=f"Uncertainty {uncertainty:.2f} exceeds threshold {self._config.uncertainty_threshold}",
                details={
                    "uncertainty": uncertainty,
                    "threshold": self._config.uncertainty_threshold,
                },
            )
        return TriggerResult(should_trigger=False)

    def _check_lateral_thinking(self, context: TriggerContext) -> TriggerResult:
        """Check for lateral thinking adoption trigger.

        Adopting alternative approaches from personas needs verification.
        """
        if context.lateral_thinking_adopted:
            return TriggerResult(
                should_trigger=True,
                trigger_type=TriggerType.LATERAL_THINKING_ADOPTION,
                reason="Lateral thinking approach adopted - requires consensus",
                details={"lateral_thinking_adopted": True},
            )
        return TriggerResult(should_trigger=False)


def check_consensus_trigger(
    context: TriggerContext,
    config: TriggerConfig | None = None,
) -> Result[tuple[TriggerResult, list[BaseEvent]], ValidationError]:
    """Convenience function for checking consensus triggers.

    Args:
        context: Trigger evaluation context
        config: Optional configuration

    Returns:
        Result with TriggerResult and events
    """
    trigger = ConsensusTrigger(config)
    return trigger.evaluate(context)
