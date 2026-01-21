"""Unit tests for escalation on failure.

Tests cover:
- FailureTracker dataclass behavior
- EscalationManager escalation logic
- Tier escalation path (Frugal -> Standard -> Frontier)
- Stagnation detection at Frontier
- Success resetting failure counter
- StagnationEvent creation
- Logging verification
"""

import pytest

from ouroboros.routing.escalation import (
    FAILURE_THRESHOLD,
    EscalationAction,
    EscalationManager,
    FailureTracker,
    StagnationEvent,
)
from ouroboros.routing.tiers import Tier


class TestFailureTracker:
    """Test the FailureTracker dataclass."""

    def test_default_values(self) -> None:
        """Test FailureTracker default initialization."""
        tracker = FailureTracker()

        assert tracker.consecutive_failures == 0
        assert tracker.current_tier == Tier.FRUGAL
        assert tracker.last_failure_time is None

    def test_custom_initialization(self) -> None:
        """Test FailureTracker with custom values."""
        tracker = FailureTracker(
            consecutive_failures=3,
            current_tier=Tier.STANDARD,
        )

        assert tracker.consecutive_failures == 3
        assert tracker.current_tier == Tier.STANDARD

    def test_record_failure_increments_counter(self) -> None:
        """Test that record_failure increments the counter."""
        tracker = FailureTracker()

        tracker.record_failure()
        assert tracker.consecutive_failures == 1
        assert tracker.last_failure_time is not None

        tracker.record_failure()
        assert tracker.consecutive_failures == 2

    def test_reset_on_success_clears_counter(self) -> None:
        """Test that reset_on_success clears the failure counter."""
        tracker = FailureTracker(consecutive_failures=5)
        tracker.record_failure()  # Set last_failure_time

        tracker.reset_on_success()

        assert tracker.consecutive_failures == 0
        assert tracker.last_failure_time is None

    def test_reset_on_success_preserves_tier(self) -> None:
        """Test that reset_on_success does not change the current tier."""
        tracker = FailureTracker(
            consecutive_failures=5,
            current_tier=Tier.STANDARD,
        )

        tracker.reset_on_success()

        assert tracker.current_tier == Tier.STANDARD

    def test_record_failure_updates_timestamp(self) -> None:
        """Test that record_failure updates the timestamp."""
        tracker = FailureTracker()

        tracker.record_failure()
        first_time = tracker.last_failure_time

        # Record another failure
        tracker.record_failure()
        second_time = tracker.last_failure_time

        assert first_time is not None
        assert second_time is not None
        assert second_time >= first_time


class TestEscalationAction:
    """Test the EscalationAction dataclass."""

    def test_no_escalation_action(self) -> None:
        """Test EscalationAction for no escalation case."""
        action = EscalationAction(
            should_escalate=False,
            is_stagnation=False,
            target_tier=None,
            previous_tier=Tier.FRUGAL,
            failure_count=1,
        )

        assert action.should_escalate is False
        assert action.is_stagnation is False
        assert action.target_tier is None
        assert action.previous_tier == Tier.FRUGAL
        assert action.failure_count == 1

    def test_escalation_action(self) -> None:
        """Test EscalationAction for escalation case."""
        action = EscalationAction(
            should_escalate=True,
            is_stagnation=False,
            target_tier=Tier.STANDARD,
            previous_tier=Tier.FRUGAL,
            failure_count=2,
        )

        assert action.should_escalate is True
        assert action.is_stagnation is False
        assert action.target_tier == Tier.STANDARD
        assert action.previous_tier == Tier.FRUGAL
        assert action.failure_count == 2

    def test_stagnation_action(self) -> None:
        """Test EscalationAction for stagnation case."""
        action = EscalationAction(
            should_escalate=False,
            is_stagnation=True,
            target_tier=None,
            previous_tier=Tier.FRONTIER,
            failure_count=2,
        )

        assert action.should_escalate is False
        assert action.is_stagnation is True
        assert action.target_tier is None
        assert action.previous_tier == Tier.FRONTIER


class TestEscalationManager:
    """Test the EscalationManager class."""

    def test_manager_initialization(self) -> None:
        """Test EscalationManager initializes with empty trackers."""
        manager = EscalationManager()

        assert len(manager._trackers) == 0

    def test_first_failure_no_escalation(self) -> None:
        """Test that first failure does not trigger escalation."""
        manager = EscalationManager()

        result = manager.record_failure("pattern_1", Tier.FRUGAL)

        assert result.is_ok
        action = result.value
        assert action.should_escalate is False
        assert action.is_stagnation is False
        assert action.failure_count == 1

    def test_two_failures_trigger_escalation(self) -> None:
        """Test that 2 consecutive failures trigger escalation."""
        manager = EscalationManager()

        # First failure - no escalation
        result1 = manager.record_failure("pattern_1", Tier.FRUGAL)
        assert result1.value.should_escalate is False

        # Second failure - should escalate
        result2 = manager.record_failure("pattern_1", Tier.FRUGAL)
        assert result2.is_ok
        action = result2.value
        assert action.should_escalate is True
        assert action.target_tier == Tier.STANDARD
        assert action.previous_tier == Tier.FRUGAL
        assert action.failure_count == 2

    def test_escalation_path_frugal_to_standard(self) -> None:
        """Test escalation from Frugal to Standard."""
        manager = EscalationManager()

        manager.record_failure("pattern_1", Tier.FRUGAL)
        result = manager.record_failure("pattern_1", Tier.FRUGAL)

        assert result.value.should_escalate is True
        assert result.value.target_tier == Tier.STANDARD

    def test_escalation_path_standard_to_frontier(self) -> None:
        """Test escalation from Standard to Frontier."""
        manager = EscalationManager()

        manager.record_failure("pattern_1", Tier.STANDARD)
        result = manager.record_failure("pattern_1", Tier.STANDARD)

        assert result.value.should_escalate is True
        assert result.value.target_tier == Tier.FRONTIER

    def test_frontier_failure_triggers_stagnation(self) -> None:
        """Test that Frontier failures trigger stagnation detection."""
        manager = EscalationManager()

        manager.record_failure("pattern_1", Tier.FRONTIER)
        result = manager.record_failure("pattern_1", Tier.FRONTIER)

        assert result.is_ok
        action = result.value
        assert action.should_escalate is False
        assert action.is_stagnation is True
        assert action.target_tier is None
        assert action.previous_tier == Tier.FRONTIER

    def test_success_resets_failure_counter(self) -> None:
        """Test that success resets the failure counter."""
        manager = EscalationManager()

        # Record one failure
        manager.record_failure("pattern_1", Tier.FRUGAL)
        assert manager.get_tracker("pattern_1").consecutive_failures == 1

        # Record success
        manager.record_success("pattern_1")
        assert manager.get_tracker("pattern_1").consecutive_failures == 0

    def test_success_after_failure_prevents_escalation(self) -> None:
        """Test that success after failure prevents escalation."""
        manager = EscalationManager()

        # Record one failure
        manager.record_failure("pattern_1", Tier.FRUGAL)

        # Record success
        manager.record_success("pattern_1")

        # Record another failure - should be first failure again
        result = manager.record_failure("pattern_1", Tier.FRUGAL)

        assert result.value.should_escalate is False
        assert result.value.failure_count == 1

    def test_success_on_unknown_pattern(self) -> None:
        """Test that success on unknown pattern does nothing."""
        manager = EscalationManager()

        # Should not raise
        manager.record_success("unknown_pattern")

        assert manager.get_tracker("unknown_pattern") is None

    def test_escalation_resets_failure_counter(self) -> None:
        """Test that escalation resets the failure counter."""
        manager = EscalationManager()

        # Trigger escalation
        manager.record_failure("pattern_1", Tier.FRUGAL)
        manager.record_failure("pattern_1", Tier.FRUGAL)

        # Counter should be reset after escalation
        tracker = manager.get_tracker("pattern_1")
        assert tracker.consecutive_failures == 0

    def test_multiple_patterns_tracked_separately(self) -> None:
        """Test that different patterns are tracked independently."""
        manager = EscalationManager()

        # Failures for pattern 1
        manager.record_failure("pattern_1", Tier.FRUGAL)

        # Failures for pattern 2
        manager.record_failure("pattern_2", Tier.STANDARD)
        manager.record_failure("pattern_2", Tier.STANDARD)

        # Pattern 1 should not have escalated
        tracker1 = manager.get_tracker("pattern_1")
        assert tracker1.consecutive_failures == 1

        # Pattern 2 should have escalated (counter reset)
        tracker2 = manager.get_tracker("pattern_2")
        assert tracker2.consecutive_failures == 0

    def test_get_tracker_returns_none_for_unknown(self) -> None:
        """Test get_tracker returns None for unknown pattern."""
        manager = EscalationManager()

        assert manager.get_tracker("unknown") is None

    def test_clear_tracker_removes_pattern(self) -> None:
        """Test clear_tracker removes the pattern from tracking."""
        manager = EscalationManager()

        manager.record_failure("pattern_1", Tier.FRUGAL)
        assert manager.get_tracker("pattern_1") is not None

        manager.clear_tracker("pattern_1")
        assert manager.get_tracker("pattern_1") is None

    def test_clear_tracker_unknown_pattern(self) -> None:
        """Test clear_tracker on unknown pattern does nothing."""
        manager = EscalationManager()

        # Should not raise
        manager.clear_tracker("unknown")

    def test_failure_threshold_constant(self) -> None:
        """Test that FAILURE_THRESHOLD is 2."""
        assert FAILURE_THRESHOLD == 2


class TestStagnationEvent:
    """Test the StagnationEvent class."""

    def test_stagnation_event_creation(self) -> None:
        """Test StagnationEvent is created correctly."""
        event = StagnationEvent(
            pattern_id="pattern_123",
            failure_count=5,
        )

        assert event.type == "escalation.stagnation.detected"
        assert event.aggregate_type == "routing"
        assert event.aggregate_id == "pattern_123"
        assert event.data["pattern_id"] == "pattern_123"
        assert event.data["failure_count"] == 5
        assert event.data["tier"] == "frontier"

    def test_stagnation_event_with_additional_data(self) -> None:
        """Test StagnationEvent with additional context data."""
        event = StagnationEvent(
            pattern_id="pattern_456",
            failure_count=3,
            task_description="complex refactoring",
            seed_id="seed_789",
        )

        assert event.data["task_description"] == "complex refactoring"
        assert event.data["seed_id"] == "seed_789"

    def test_stagnation_event_has_unique_id(self) -> None:
        """Test that each StagnationEvent has a unique ID."""
        event1 = StagnationEvent(pattern_id="p1", failure_count=1)
        event2 = StagnationEvent(pattern_id="p1", failure_count=1)

        assert event1.id != event2.id

    def test_stagnation_event_has_timestamp(self) -> None:
        """Test that StagnationEvent has a timestamp."""
        event = StagnationEvent(pattern_id="p1", failure_count=1)

        assert event.timestamp is not None

    def test_create_stagnation_event_via_manager(self) -> None:
        """Test creating StagnationEvent through EscalationManager."""
        manager = EscalationManager()

        event = manager.create_stagnation_event(
            pattern_id="pattern_123",
            failure_count=5,
            reason="Model consistently failing",
        )

        assert isinstance(event, StagnationEvent)
        assert event.data["pattern_id"] == "pattern_123"
        assert event.data["failure_count"] == 5
        assert event.data["reason"] == "Model consistently failing"


class TestEscalationIntegration:
    """Integration tests for escalation scenarios."""

    def test_full_escalation_path(self) -> None:
        """Test complete escalation from Frugal to Frontier to stagnation."""
        manager = EscalationManager()
        pattern = "complex_task"

        # Frugal tier failures -> escalate to Standard
        manager.record_failure(pattern, Tier.FRUGAL)
        result = manager.record_failure(pattern, Tier.FRUGAL)
        assert result.value.should_escalate is True
        assert result.value.target_tier == Tier.STANDARD

        # Standard tier failures -> escalate to Frontier
        manager.record_failure(pattern, Tier.STANDARD)
        result = manager.record_failure(pattern, Tier.STANDARD)
        assert result.value.should_escalate is True
        assert result.value.target_tier == Tier.FRONTIER

        # Frontier tier failures -> stagnation
        manager.record_failure(pattern, Tier.FRONTIER)
        result = manager.record_failure(pattern, Tier.FRONTIER)
        assert result.value.should_escalate is False
        assert result.value.is_stagnation is True

    def test_success_breaks_escalation_chain(self) -> None:
        """Test that success at any point breaks escalation chain."""
        manager = EscalationManager()
        pattern = "task_with_success"

        # One failure at Frugal
        manager.record_failure(pattern, Tier.FRUGAL)

        # Success resets
        manager.record_success(pattern)

        # Need 2 more failures to escalate
        result = manager.record_failure(pattern, Tier.FRUGAL)
        assert result.value.should_escalate is False
        assert result.value.failure_count == 1

        result = manager.record_failure(pattern, Tier.FRUGAL)
        assert result.value.should_escalate is True
        assert result.value.target_tier == Tier.STANDARD

    def test_never_infinite_retry(self) -> None:
        """Test that system never infinitely retries at Frontier."""
        manager = EscalationManager()
        pattern = "stuck_task"

        # Get to Frontier
        manager.record_failure(pattern, Tier.FRUGAL)
        manager.record_failure(pattern, Tier.FRUGAL)  # -> Standard

        manager.record_failure(pattern, Tier.STANDARD)
        manager.record_failure(pattern, Tier.STANDARD)  # -> Frontier

        # Multiple failures at Frontier should consistently return stagnation
        for i in range(10):
            manager.record_failure(pattern, Tier.FRONTIER)
            result = manager.record_failure(pattern, Tier.FRONTIER)

            # Every second failure should signal stagnation
            assert result.value.is_stagnation is True
            assert result.value.should_escalate is False
            assert result.value.target_tier is None

    def test_cost_impact_tracking(self) -> None:
        """Test that cost impact is correctly calculated."""
        # Frugal (1x) -> Standard (10x) = 10x increase
        assert Tier.FRUGAL.cost_multiplier == 1
        assert Tier.STANDARD.cost_multiplier == 10
        assert Tier.FRONTIER.cost_multiplier == 30

        # Cost impact ratios
        frugal_to_standard = Tier.STANDARD.cost_multiplier / Tier.FRUGAL.cost_multiplier
        assert frugal_to_standard == 10

        standard_to_frontier = Tier.FRONTIER.cost_multiplier / Tier.STANDARD.cost_multiplier
        assert standard_to_frontier == 3

    def test_tier_change_updates_tracker(self) -> None:
        """Test that recording failure updates the tracker's current tier."""
        manager = EscalationManager()
        pattern = "tier_change_test"

        manager.record_failure(pattern, Tier.FRUGAL)
        assert manager.get_tracker(pattern).current_tier == Tier.FRUGAL

        manager.record_failure(pattern, Tier.STANDARD)
        assert manager.get_tracker(pattern).current_tier == Tier.STANDARD


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_empty_pattern_id(self) -> None:
        """Test that empty pattern ID works."""
        manager = EscalationManager()

        result = manager.record_failure("", Tier.FRUGAL)

        assert result.is_ok
        assert manager.get_tracker("") is not None

    def test_whitespace_pattern_id(self) -> None:
        """Test that whitespace pattern ID works."""
        manager = EscalationManager()

        result = manager.record_failure("  ", Tier.FRUGAL)

        assert result.is_ok
        assert manager.get_tracker("  ") is not None

    def test_unicode_pattern_id(self) -> None:
        """Test that unicode pattern ID works."""
        manager = EscalationManager()
        pattern = "\ud83d\ude80_\ub85c\ucf13_\u4efb\u52a1"

        result = manager.record_failure(pattern, Tier.FRUGAL)

        assert result.is_ok
        assert manager.get_tracker(pattern) is not None

    def test_very_long_pattern_id(self) -> None:
        """Test that very long pattern ID works."""
        manager = EscalationManager()
        pattern = "a" * 10000

        result = manager.record_failure(pattern, Tier.FRUGAL)

        assert result.is_ok
        assert manager.get_tracker(pattern) is not None

    def test_escalation_action_immutability(self) -> None:
        """Test that EscalationAction is immutable (frozen)."""
        action = EscalationAction(
            should_escalate=True,
            is_stagnation=False,
            target_tier=Tier.STANDARD,
            previous_tier=Tier.FRUGAL,
            failure_count=2,
        )

        # Attempting to modify should raise
        with pytest.raises(AttributeError):
            action.should_escalate = False  # type: ignore

    def test_stagnation_continues_to_be_detected(self) -> None:
        """Test that stagnation is detected every time threshold is reached at Frontier.

        At Frontier, since there's no next tier, every failure after reaching
        threshold continues to report stagnation (no counter reset happens).
        """
        manager = EscalationManager()
        pattern = "perpetual_stagnation"

        stagnation_count = 0
        for _ in range(20):
            result = manager.record_failure(pattern, Tier.FRONTIER)
            if result.value.is_stagnation:
                stagnation_count += 1

        # First failure: count=1, no stagnation
        # Failures 2-20: count >= 2, all stagnation (19 times)
        assert stagnation_count == 19
