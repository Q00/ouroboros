"""Unit tests for ouroboros.events.control module.

Tests cover:
- Event factory function
- Event type naming convention
- Payload shape (directive value, terminality, optional fields)
"""

from ouroboros.core.directive import Directive
from ouroboros.events.control import create_control_directive_emitted_event


class TestControlDirectiveEmittedEvent:
    """Tests for create_control_directive_emitted_event factory."""

    def test_event_type(self):
        """Event should have type 'control.directive.emitted'."""
        event = create_control_directive_emitted_event(
            execution_id="exec_123",
            emitted_by="evaluator",
            directive=Directive.RETRY,
            reason="Stage 1 failed; retry budget remains.",
        )

        assert event.type == "control.directive.emitted"

    def test_event_aggregate(self):
        """Event should be aggregated by execution id under the control aggregate."""
        event = create_control_directive_emitted_event(
            execution_id="exec_123",
            emitted_by="evaluator",
            directive=Directive.CONTINUE,
            reason="All checks passed.",
        )

        assert event.aggregate_type == "control"
        assert event.aggregate_id == "exec_123"

    def test_payload_serializes_directive_string_value(self):
        """The payload stores the directive's string value (StrEnum .value),
        not the Python member, so the event is JSON-safe through BaseEvent."""
        event = create_control_directive_emitted_event(
            execution_id="exec_456",
            emitted_by="evolver",
            directive=Directive.EVOLVE,
            reason="Evaluation fed critique; advancing generation.",
        )

        assert event.data["directive"] == "evolve"

    def test_payload_records_terminality(self):
        """Terminality is denormalized into the payload so downstream consumers
        do not need to import the Directive enum to classify events."""
        terminal_event = create_control_directive_emitted_event(
            execution_id="exec_789",
            emitted_by="evolver",
            directive=Directive.CONVERGE,
            reason="Ontology similarity threshold reached.",
        )
        non_terminal_event = create_control_directive_emitted_event(
            execution_id="exec_789",
            emitted_by="evaluator",
            directive=Directive.CONTINUE,
            reason="Stage 2 passed.",
        )

        assert terminal_event.data["is_terminal"] is True
        assert non_terminal_event.data["is_terminal"] is False

    def test_payload_records_reason_and_source(self):
        """The emission source and rationale are preserved verbatim."""
        event = create_control_directive_emitted_event(
            execution_id="exec_abc",
            emitted_by="resilience.lateral",
            directive=Directive.UNSTUCK,
            reason="Stagnation pattern 3 of 4 detected.",
        )

        assert event.data["emitted_by"] == "resilience.lateral"
        assert event.data["reason"] == "Stagnation pattern 3 of 4 detected."

    def test_context_snapshot_id_is_optional(self):
        """When the emission site has no snapshot to link, the key is absent —
        not None — to keep stored payloads compact."""
        event = create_control_directive_emitted_event(
            execution_id="exec_def",
            emitted_by="evaluator",
            directive=Directive.CONTINUE,
            reason="No snapshot needed.",
        )

        assert "context_snapshot_id" not in event.data

    def test_context_snapshot_id_is_recorded_when_given(self):
        """A linked snapshot id lands in the payload."""
        event = create_control_directive_emitted_event(
            execution_id="exec_def",
            emitted_by="evaluator",
            directive=Directive.COMPACT,
            reason="Context nearing window limit.",
            context_snapshot_id="snap_01",
        )

        assert event.data["context_snapshot_id"] == "snap_01"

    def test_extra_is_merged_when_given(self):
        """Forward-compatibility knob: extra fields land under 'extra'."""
        event = create_control_directive_emitted_event(
            execution_id="exec_xyz",
            emitted_by="evolver",
            directive=Directive.EVOLVE,
            reason="Generation advance.",
            extra={"generation": 3},
        )

        assert event.data["extra"] == {"generation": 3}

    def test_extra_absent_when_empty(self):
        """Empty or omitted 'extra' keeps the payload compact."""
        event = create_control_directive_emitted_event(
            execution_id="exec_xyz",
            emitted_by="evolver",
            directive=Directive.EVOLVE,
            reason="Generation advance.",
        )

        assert "extra" not in event.data


class TestControlDirectiveCoversEveryDirective:
    """Smoke-level check that every Directive member serializes through the
    factory without surprises. Guards against new members that add hidden
    fields incompatible with BaseEvent's JSON-friendly payload contract."""

    def test_every_directive_produces_event(self):
        for directive in Directive:
            event = create_control_directive_emitted_event(
                execution_id="exec_all",
                emitted_by="test",
                directive=directive,
                reason=f"coverage for {directive.value}",
            )

            assert event.data["directive"] == directive.value
            assert event.data["is_terminal"] == directive.is_terminal
