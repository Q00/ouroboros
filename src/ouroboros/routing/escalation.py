"""Escalation on Failure for Ouroboros.

This module implements automatic tier escalation when tasks fail consecutively.
The escalation policy follows: Frugal -> Standard -> Frontier.

After reaching Frontier, if failures continue, a STAGNATION_DETECTED event
is emitted for the resilience system to handle (lateral thinking path).

Escalation Rules:
- 2 consecutive failures trigger escalation to next tier
- Success resets the failure counter
- Never infinite retry (Frontier failure emits event for resilience)

Usage:
    from ouroboros.routing.escalation import EscalationManager, FailureTracker
    from ouroboros.routing.tiers import Tier

    # Create manager
    manager = EscalationManager()

    # Record failures and check for escalation
    result = manager.record_failure("pattern_123", Tier.FRUGAL)
    if result.is_ok:
        action = result.value
        if action.should_escalate:
            new_tier = action.target_tier
        elif action.is_stagnation:
            # Handle stagnation - emit event

    # Reset on success
    manager.record_success("pattern_123")
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.observability.logging import get_logger
from ouroboros.routing.tiers import Tier

log = get_logger(__name__)

# Number of consecutive failures before escalation
FAILURE_THRESHOLD = 2


@dataclass
class FailureTracker:
    """Tracks consecutive failures per task pattern.

    Attributes:
        consecutive_failures: Count of consecutive failures for the pattern.
        current_tier: The current tier being used for the pattern.
        last_failure_time: Timestamp of the most recent failure.
    """

    consecutive_failures: int = 0
    current_tier: Tier = Tier.FRUGAL
    last_failure_time: datetime | None = None

    def reset_on_success(self) -> None:
        """Reset failure tracking on success.

        Clears the consecutive failure counter while preserving the current tier.
        """
        self.consecutive_failures = 0
        self.last_failure_time = None

    def record_failure(self) -> None:
        """Record a failure occurrence.

        Increments the consecutive failure counter and updates the timestamp.
        """
        self.consecutive_failures += 1
        self.last_failure_time = datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class EscalationAction:
    """Result of an escalation decision.

    Attributes:
        should_escalate: Whether escalation to a higher tier is needed.
        is_stagnation: Whether we've reached Frontier and still failing.
        target_tier: The tier to escalate to (if should_escalate is True).
        previous_tier: The tier before escalation decision.
        failure_count: Number of consecutive failures.
    """

    should_escalate: bool
    is_stagnation: bool
    target_tier: Tier | None
    previous_tier: Tier
    failure_count: int


class StagnationEvent(BaseEvent):
    """Event emitted when Frontier tier still fails.

    This event signals the resilience system to engage lateral thinking
    paths instead of continuing vertical escalation.
    """

    def __init__(
        self,
        pattern_id: str,
        failure_count: int,
        **kwargs,
    ) -> None:
        """Create a stagnation event.

        Args:
            pattern_id: The task pattern identifier.
            failure_count: Number of consecutive failures at Frontier.
            **kwargs: Additional event data.
        """
        super().__init__(
            type="escalation.stagnation.detected",
            aggregate_type="routing",
            aggregate_id=pattern_id,
            data={
                "pattern_id": pattern_id,
                "failure_count": failure_count,
                "tier": Tier.FRONTIER.value,
                **kwargs,
            },
        )


@dataclass
class EscalationManager:
    """Manages tier escalation based on consecutive failures.

    The manager tracks failures per task pattern and determines when
    escalation is needed. It follows the escalation path:
    Frugal -> Standard -> Frontier -> Stagnation Event

    Design:
    - Stateful: Maintains failure tracking state per pattern
    - Thread-safe operations should use external synchronization
    - Uses Result type for consistent error handling

    Usage:
        manager = EscalationManager()

        # Record failures
        action = manager.record_failure("pattern_123", Tier.FRUGAL)
        if action.is_ok:
            if action.value.should_escalate:
                # Use action.value.target_tier
            elif action.value.is_stagnation:
                # Emit event for resilience system

        # Record success to reset counter
        manager.record_success("pattern_123")
    """

    # Internal state: pattern_id -> FailureTracker
    _trackers: dict[str, FailureTracker] = field(default_factory=dict)

    def _get_next_tier(self, current: Tier) -> Tier | None:
        """Get the next tier in escalation path.

        Args:
            current: The current tier.

        Returns:
            The next tier, or None if at Frontier (highest tier).
        """
        escalation_path = {
            Tier.FRUGAL: Tier.STANDARD,
            Tier.STANDARD: Tier.FRONTIER,
            Tier.FRONTIER: None,
        }
        return escalation_path.get(current)

    def _get_or_create_tracker(self, pattern_id: str, current_tier: Tier) -> FailureTracker:
        """Get or create a failure tracker for a pattern.

        Args:
            pattern_id: The task pattern identifier.
            current_tier: The current tier being used.

        Returns:
            The FailureTracker for the pattern.
        """
        if pattern_id not in self._trackers:
            self._trackers[pattern_id] = FailureTracker(current_tier=current_tier)
        return self._trackers[pattern_id]

    def record_failure(self, pattern_id: str, current_tier: Tier) -> Result[EscalationAction, None]:
        """Record a failure and determine if escalation is needed.

        Args:
            pattern_id: Unique identifier for the task pattern.
            current_tier: The tier that just failed.

        Returns:
            Result containing EscalationAction with escalation decision.
        """
        tracker = self._get_or_create_tracker(pattern_id, current_tier)
        tracker.current_tier = current_tier
        tracker.record_failure()

        failure_count = tracker.consecutive_failures

        log.debug(
            "escalation.failure.recorded",
            pattern_id=pattern_id,
            tier=current_tier.value,
            consecutive_failures=failure_count,
        )

        # Check if we've hit the threshold
        if failure_count >= FAILURE_THRESHOLD:
            next_tier = self._get_next_tier(current_tier)

            if next_tier is not None:
                # Escalate to next tier
                cost_impact = f"{current_tier.cost_multiplier}x -> {next_tier.cost_multiplier}x"
                log.info(
                    "escalation.tier.upgraded",
                    pattern_id=pattern_id,
                    from_tier=current_tier.value,
                    to_tier=next_tier.value,
                    failure_count=failure_count,
                    cost_impact=cost_impact,
                )

                # Reset failure counter after escalation
                tracker.consecutive_failures = 0

                return Result.ok(
                    EscalationAction(
                        should_escalate=True,
                        is_stagnation=False,
                        target_tier=next_tier,
                        previous_tier=current_tier,
                        failure_count=failure_count,
                    )
                )
            else:
                # Already at Frontier - stagnation detected
                log.warning(
                    "escalation.stagnation.detected",
                    pattern_id=pattern_id,
                    tier=current_tier.value,
                    failure_count=failure_count,
                    message="Frontier tier failing, lateral thinking needed",
                )

                return Result.ok(
                    EscalationAction(
                        should_escalate=False,
                        is_stagnation=True,
                        target_tier=None,
                        previous_tier=current_tier,
                        failure_count=failure_count,
                    )
                )

        # Not enough failures yet
        return Result.ok(
            EscalationAction(
                should_escalate=False,
                is_stagnation=False,
                target_tier=None,
                previous_tier=current_tier,
                failure_count=failure_count,
            )
        )

    def record_success(self, pattern_id: str) -> None:
        """Record a success, resetting the failure counter.

        Args:
            pattern_id: Unique identifier for the task pattern.
        """
        if pattern_id in self._trackers:
            tracker = self._trackers[pattern_id]
            previous_failures = tracker.consecutive_failures
            tracker.reset_on_success()

            log.debug(
                "escalation.success.recorded",
                pattern_id=pattern_id,
                previous_failures=previous_failures,
            )

    def get_tracker(self, pattern_id: str) -> FailureTracker | None:
        """Get the failure tracker for a pattern.

        Args:
            pattern_id: The task pattern identifier.

        Returns:
            The FailureTracker if exists, None otherwise.
        """
        return self._trackers.get(pattern_id)

    def clear_tracker(self, pattern_id: str) -> None:
        """Remove the failure tracker for a pattern.

        Args:
            pattern_id: The task pattern identifier.
        """
        if pattern_id in self._trackers:
            del self._trackers[pattern_id]

    def create_stagnation_event(
        self, pattern_id: str, failure_count: int, **additional_data
    ) -> StagnationEvent:
        """Create a stagnation event for the resilience system.

        This method creates the event; the caller is responsible for
        persisting it to the EventStore.

        Args:
            pattern_id: The task pattern identifier.
            failure_count: Number of consecutive failures.
            **additional_data: Additional context to include in the event.

        Returns:
            StagnationEvent ready for persistence.
        """
        event = StagnationEvent(
            pattern_id=pattern_id,
            failure_count=failure_count,
            **additional_data,
        )

        log.info(
            "escalation.stagnation.event_created",
            event_id=event.id,
            pattern_id=pattern_id,
            failure_count=failure_count,
        )

        return event
