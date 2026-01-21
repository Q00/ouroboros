"""Unit tests for ouroboros.events.decomposition module.

Tests cover:
- Event factory functions
- Event structure and data
- Event type naming conventions
"""

from ouroboros.events.base import BaseEvent


class TestAtomicityCheckedEvent:
    """Tests for create_ac_atomicity_checked_event factory."""

    def test_event_type(self):
        """Event should have type 'ac.atomicity.checked'."""
        from ouroboros.events.decomposition import create_ac_atomicity_checked_event

        event = create_ac_atomicity_checked_event(
            ac_id="ac_test",
            execution_id="exec_123",
            is_atomic=True,
            complexity_score=0.3,
            tool_count=2,
            estimated_duration=60,
            reasoning="Simple task",
        )

        assert event.type == "ac.atomicity.checked"

    def test_event_aggregate(self):
        """Event should have ac_decomposition aggregate type."""
        from ouroboros.events.decomposition import create_ac_atomicity_checked_event

        event = create_ac_atomicity_checked_event(
            ac_id="ac_test",
            execution_id="exec_123",
            is_atomic=True,
            complexity_score=0.3,
            tool_count=2,
            estimated_duration=60,
            reasoning="Simple task",
        )

        assert event.aggregate_type == "ac_decomposition"
        assert event.aggregate_id == "ac_test"

    def test_event_data_atomic(self):
        """Event data should reflect atomic decision."""
        from ouroboros.events.decomposition import create_ac_atomicity_checked_event

        event = create_ac_atomicity_checked_event(
            ac_id="ac_test",
            execution_id="exec_123",
            is_atomic=True,
            complexity_score=0.3,
            tool_count=2,
            estimated_duration=60,
            reasoning="Simple task",
        )

        assert event.data["is_atomic"] is True
        assert event.data["complexity_score"] == 0.3
        assert event.data["tool_count"] == 2
        assert event.data["estimated_duration"] == 60
        assert event.data["reasoning"] == "Simple task"
        assert event.data["execution_id"] == "exec_123"

    def test_event_data_non_atomic(self):
        """Event data should reflect non-atomic decision."""
        from ouroboros.events.decomposition import create_ac_atomicity_checked_event

        event = create_ac_atomicity_checked_event(
            ac_id="ac_complex",
            execution_id="exec_456",
            is_atomic=False,
            complexity_score=0.9,
            tool_count=7,
            estimated_duration=600,
            reasoning="Complex multi-step task",
        )

        assert event.data["is_atomic"] is False
        assert event.data["complexity_score"] == 0.9
        assert event.data["tool_count"] == 7

    def test_event_is_base_event(self):
        """Factory should return BaseEvent instance."""
        from ouroboros.events.decomposition import create_ac_atomicity_checked_event

        event = create_ac_atomicity_checked_event(
            ac_id="ac_test",
            execution_id="exec_123",
            is_atomic=True,
            complexity_score=0.5,
            tool_count=1,
            estimated_duration=30,
            reasoning="Test",
        )

        assert isinstance(event, BaseEvent)


class TestDecomposedEvent:
    """Tests for create_ac_decomposed_event factory."""

    def test_event_type(self):
        """Event should have type 'ac.decomposition.completed'."""
        from ouroboros.events.decomposition import create_ac_decomposed_event

        event = create_ac_decomposed_event(
            parent_ac_id="ac_parent",
            execution_id="exec_123",
            child_ac_ids=["ac_c1", "ac_c2"],
            child_contents=["Child 1", "Child 2"],
            depth=0,
            reasoning="Split by domain",
        )

        assert event.type == "ac.decomposition.completed"

    def test_event_aggregate(self):
        """Event should have parent AC as aggregate ID."""
        from ouroboros.events.decomposition import create_ac_decomposed_event

        event = create_ac_decomposed_event(
            parent_ac_id="ac_parent",
            execution_id="exec_123",
            child_ac_ids=["ac_c1", "ac_c2"],
            child_contents=["Child 1", "Child 2"],
            depth=0,
            reasoning="Test",
        )

        assert event.aggregate_type == "ac_decomposition"
        assert event.aggregate_id == "ac_parent"

    def test_event_data_children(self):
        """Event data should include child IDs and contents."""
        from ouroboros.events.decomposition import create_ac_decomposed_event

        child_ids = ["ac_c1", "ac_c2", "ac_c3"]
        child_contents = ["Task A", "Task B", "Task C"]

        event = create_ac_decomposed_event(
            parent_ac_id="ac_parent",
            execution_id="exec_123",
            child_ac_ids=child_ids,
            child_contents=child_contents,
            depth=1,
            reasoning="Functional split",
        )

        assert event.data["child_ac_ids"] == child_ids
        assert event.data["child_contents"] == child_contents
        assert event.data["child_count"] == 3
        assert event.data["depth"] == 1
        assert event.data["reasoning"] == "Functional split"

    def test_event_child_count_matches(self):
        """child_count should match the number of children."""
        from ouroboros.events.decomposition import create_ac_decomposed_event

        event = create_ac_decomposed_event(
            parent_ac_id="ac_parent",
            execution_id="exec_123",
            child_ac_ids=["a", "b", "c", "d", "e"],
            child_contents=["1", "2", "3", "4", "5"],
            depth=0,
            reasoning="Test",
        )

        assert event.data["child_count"] == 5


class TestMarkedAtomicEvent:
    """Tests for create_ac_marked_atomic_event factory."""

    def test_event_type(self):
        """Event should have type 'ac.marked_atomic'."""
        from ouroboros.events.decomposition import create_ac_marked_atomic_event

        event = create_ac_marked_atomic_event(
            ac_id="ac_atomic",
            execution_id="exec_123",
            depth=2,
        )

        assert event.type == "ac.marked_atomic"

    def test_event_aggregate(self):
        """Event should have AC as aggregate ID."""
        from ouroboros.events.decomposition import create_ac_marked_atomic_event

        event = create_ac_marked_atomic_event(
            ac_id="ac_atomic",
            execution_id="exec_123",
            depth=2,
        )

        assert event.aggregate_type == "ac_decomposition"
        assert event.aggregate_id == "ac_atomic"

    def test_event_data(self):
        """Event data should include execution_id and depth."""
        from ouroboros.events.decomposition import create_ac_marked_atomic_event

        event = create_ac_marked_atomic_event(
            ac_id="ac_atomic",
            execution_id="exec_456",
            depth=3,
        )

        assert event.data["execution_id"] == "exec_456"
        assert event.data["depth"] == 3


class TestDecompositionFailedEvent:
    """Tests for create_ac_decomposition_failed_event factory."""

    def test_event_type(self):
        """Event should have type 'ac.decomposition.failed'."""
        from ouroboros.events.decomposition import create_ac_decomposition_failed_event

        event = create_ac_decomposition_failed_event(
            ac_id="ac_test",
            execution_id="exec_123",
            error_message="Max depth reached",
            error_type="max_depth_reached",
            depth=5,
        )

        assert event.type == "ac.decomposition.failed"

    def test_event_aggregate(self):
        """Event should have AC as aggregate ID."""
        from ouroboros.events.decomposition import create_ac_decomposition_failed_event

        event = create_ac_decomposition_failed_event(
            ac_id="ac_failed",
            execution_id="exec_123",
            error_message="Error",
            error_type="test_error",
            depth=3,
        )

        assert event.aggregate_type == "ac_decomposition"
        assert event.aggregate_id == "ac_failed"

    def test_event_data_error(self):
        """Event data should include error details."""
        from ouroboros.events.decomposition import create_ac_decomposition_failed_event

        event = create_ac_decomposition_failed_event(
            ac_id="ac_test",
            execution_id="exec_789",
            error_message="LLM timeout occurred",
            error_type="llm_failure",
            depth=2,
        )

        assert event.data["execution_id"] == "exec_789"
        assert event.data["error_message"] == "LLM timeout occurred"
        assert event.data["error_type"] == "llm_failure"
        assert event.data["depth"] == 2

    def test_event_error_types(self):
        """Various error types should be supported."""
        from ouroboros.events.decomposition import create_ac_decomposition_failed_event

        error_types = [
            "max_depth_reached",
            "cyclic_decomposition",
            "llm_failure",
            "parse_failure",
            "insufficient_children",
            "too_many_children",
        ]

        for error_type in error_types:
            event = create_ac_decomposition_failed_event(
                ac_id="ac_test",
                execution_id="exec_123",
                error_message=f"Error: {error_type}",
                error_type=error_type,
                depth=0,
            )

            assert event.data["error_type"] == error_type


class TestEventNamingConventions:
    """Tests for event naming convention compliance."""

    def test_all_events_use_dot_notation(self):
        """All events should use dot.notation.past_tense."""
        from ouroboros.events.decomposition import (
            create_ac_atomicity_checked_event,
            create_ac_decomposed_event,
            create_ac_decomposition_failed_event,
            create_ac_marked_atomic_event,
        )

        events = [
            create_ac_atomicity_checked_event(
                ac_id="test",
                execution_id="exec",
                is_atomic=True,
                complexity_score=0.5,
                tool_count=1,
                estimated_duration=30,
                reasoning="Test",
            ),
            create_ac_decomposed_event(
                parent_ac_id="test",
                execution_id="exec",
                child_ac_ids=["a", "b"],
                child_contents=["A", "B"],
                depth=0,
                reasoning="Test",
            ),
            create_ac_marked_atomic_event(
                ac_id="test",
                execution_id="exec",
                depth=0,
            ),
            create_ac_decomposition_failed_event(
                ac_id="test",
                execution_id="exec",
                error_message="Error",
                error_type="test",
                depth=0,
            ),
        ]

        for event in events:
            # Check dot notation
            assert "." in event.type
            # Check starts with domain
            assert event.type.startswith("ac.")

    def test_all_events_have_consistent_aggregate_type(self):
        """All decomposition events should use 'ac_decomposition' aggregate type."""
        from ouroboros.events.decomposition import (
            create_ac_atomicity_checked_event,
            create_ac_decomposed_event,
            create_ac_decomposition_failed_event,
            create_ac_marked_atomic_event,
        )

        events = [
            create_ac_atomicity_checked_event(
                ac_id="test",
                execution_id="exec",
                is_atomic=True,
                complexity_score=0.5,
                tool_count=1,
                estimated_duration=30,
                reasoning="Test",
            ),
            create_ac_decomposed_event(
                parent_ac_id="test",
                execution_id="exec",
                child_ac_ids=["a", "b"],
                child_contents=["A", "B"],
                depth=0,
                reasoning="Test",
            ),
            create_ac_marked_atomic_event(
                ac_id="test",
                execution_id="exec",
                depth=0,
            ),
            create_ac_decomposition_failed_event(
                ac_id="test",
                execution_id="exec",
                error_message="Error",
                error_type="test",
                depth=0,
            ),
        ]

        for event in events:
            assert event.aggregate_type == "ac_decomposition"
