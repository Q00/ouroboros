"""Unit tests for ouroboros.events.control module.

Tests cover:
- Event type + target-oriented aggregation
- Payload basics (directive value, terminality, emitter, reason)
- Optional correlation fields (appear iff provided)
- Forward-compatible target types
- Coverage across every Directive member
"""

from ouroboros.core.directive import Directive
from ouroboros.events.control import create_control_directive_emitted_event


class TestControlDirectiveEmittedCoreShape:
    """Event type + target aggregation + payload basics."""

    def test_event_type(self):
        """Event type is the dot.notation.past_tense string."""
        event = create_control_directive_emitted_event(
            target_type="execution",
            target_id="exec_123",
            emitted_by="evaluator",
            directive=Directive.RETRY,
            reason="Stage 1 failed; retry budget remains.",
        )

        assert event.type == "control.directive.emitted"

    def test_aggregate_mirrors_target(self):
        """aggregate_(type, id) = (target_type, target_id) so that
        projectors filtering by aggregate naturally include directives."""
        event = create_control_directive_emitted_event(
            target_type="lineage",
            target_id="lin_abc",
            emitted_by="evolver",
            directive=Directive.EVOLVE,
            reason="Advance generation.",
        )

        assert event.aggregate_type == "lineage"
        assert event.aggregate_id == "lin_abc"

    def test_payload_includes_target(self):
        """target_type and target_id are duplicated into the payload
        for query convenience (no JOIN required to inspect)."""
        event = create_control_directive_emitted_event(
            target_type="execution",
            target_id="exec_xyz",
            emitted_by="evaluator",
            directive=Directive.CONTINUE,
            reason="All checks passed.",
        )

        assert event.data["target_type"] == "execution"
        assert event.data["target_id"] == "exec_xyz"

    def test_payload_serializes_directive_string_value(self):
        """Payload stores the StrEnum value so downstream consumers
        classify events without importing the Directive enum."""
        event = create_control_directive_emitted_event(
            target_type="execution",
            target_id="exec_456",
            emitted_by="evolver",
            directive=Directive.EVOLVE,
            reason="Evaluation fed critique; advancing generation.",
        )

        assert event.data["directive"] == "evolve"

    def test_payload_records_terminality(self):
        """Denormalized is_terminal flag lets consumers classify
        terminal vs non-terminal events without the enum."""
        terminal = create_control_directive_emitted_event(
            target_type="lineage",
            target_id="lin_1",
            emitted_by="evolver",
            directive=Directive.CONVERGE,
            reason="Ontology similarity threshold reached.",
        )
        non_terminal = create_control_directive_emitted_event(
            target_type="execution",
            target_id="exec_1",
            emitted_by="evaluator",
            directive=Directive.CONTINUE,
            reason="Stage 2 passed.",
        )

        assert terminal.data["is_terminal"] is True
        assert non_terminal.data["is_terminal"] is False

    def test_payload_records_emitter_and_reason(self):
        """Emitter and reason are preserved verbatim."""
        event = create_control_directive_emitted_event(
            target_type="execution",
            target_id="exec_abc",
            emitted_by="resilience.lateral",
            directive=Directive.UNSTUCK,
            reason="Stagnation pattern 3 of 4 detected.",
        )

        assert event.data["emitted_by"] == "resilience.lateral"
        assert event.data["reason"] == "Stagnation pattern 3 of 4 detected."


class TestOptionalCorrelationFields:
    """Correlation fields appear in the payload iff provided, so
    projections can filter by lineage / generation / phase without
    reading None sentinels."""

    def test_session_id_omitted_when_absent(self):
        event = create_control_directive_emitted_event(
            target_type="execution",
            target_id="exec_def",
            emitted_by="evaluator",
            directive=Directive.CONTINUE,
            reason="n/a",
        )

        assert "session_id" not in event.data

    def test_session_id_recorded_when_present(self):
        event = create_control_directive_emitted_event(
            target_type="execution",
            target_id="exec_def",
            emitted_by="evaluator",
            directive=Directive.CONTINUE,
            reason="n/a",
            session_id="sess_99",
        )

        assert event.data["session_id"] == "sess_99"

    def test_execution_id_correlation(self):
        """A lineage-targeted directive can still correlate back to the
        execution it ran inside."""
        event = create_control_directive_emitted_event(
            target_type="lineage",
            target_id="lin_xyz",
            emitted_by="evolver",
            directive=Directive.EVOLVE,
            reason="next gen",
            execution_id="exec_from_which_this_ran",
        )

        assert event.data["execution_id"] == "exec_from_which_this_ran"

    def test_lineage_generation_and_phase_correlation(self):
        """Lineage-targeted directives carry generation_number + phase
        so TUI lineage rendering places them exactly in the timeline."""
        event = create_control_directive_emitted_event(
            target_type="lineage",
            target_id="lin_ralph",
            emitted_by="evolver",
            directive=Directive.RETRY,
            reason="Reflect failed; retry budget remains.",
            lineage_id="lin_ralph",
            generation_number=2,
            phase="reflecting",
        )

        assert event.data["lineage_id"] == "lin_ralph"
        assert event.data["generation_number"] == 2
        assert event.data["phase"] == "reflecting"

    def test_context_snapshot_id_is_optional(self):
        event = create_control_directive_emitted_event(
            target_type="execution",
            target_id="e",
            emitted_by="x",
            directive=Directive.CONTINUE,
            reason="n",
        )

        assert "context_snapshot_id" not in event.data

    def test_context_snapshot_id_is_recorded_when_given(self):
        event = create_control_directive_emitted_event(
            target_type="execution",
            target_id="e",
            emitted_by="x",
            directive=Directive.COMPACT,
            reason="nearing window limit",
            context_snapshot_id="snap_01",
        )

        assert event.data["context_snapshot_id"] == "snap_01"

    def test_extra_is_merged_when_given(self):
        """Forward-compatibility slot; prefer promoting to named args as
        fields stabilize, but extra keeps new data flowing in the mean
        time."""
        event = create_control_directive_emitted_event(
            target_type="lineage",
            target_id="lin_1",
            emitted_by="evolver",
            directive=Directive.EVOLVE,
            reason="adv",
            extra={"branch_hint": "a"},
        )

        assert event.data["extra"] == {"branch_hint": "a"}

    def test_extra_absent_when_empty(self):
        event = create_control_directive_emitted_event(
            target_type="lineage",
            target_id="lin_1",
            emitted_by="evolver",
            directive=Directive.EVOLVE,
            reason="adv",
        )

        assert "extra" not in event.data


class TestTargetTypeIsForwardCompatible:
    """target_type is free-form so new target types (e.g. Phase 3's
    agent_process) land without a schema change."""

    def test_agent_process_target(self):
        event = create_control_directive_emitted_event(
            target_type="agent_process",
            target_id="proc_42",
            emitted_by="scheduler",
            directive=Directive.WAIT,
            reason="awaiting external input",
        )

        assert event.aggregate_type == "agent_process"
        assert event.aggregate_id == "proc_42"
        assert event.data["target_type"] == "agent_process"

    def test_session_target(self):
        event = create_control_directive_emitted_event(
            target_type="session",
            target_id="sess_001",
            emitted_by="orchestrator",
            directive=Directive.CANCEL,
            reason="user interrupt",
        )

        assert event.aggregate_type == "session"


class TestControlDirectiveCoversEveryDirective:
    """Smoke check: every Directive member serializes through the
    factory without surprises. Guards against vocabulary additions
    introducing payload regressions."""

    def test_every_directive_produces_event(self):
        for directive in Directive:
            event = create_control_directive_emitted_event(
                target_type="execution",
                target_id="exec_all",
                emitted_by="test",
                directive=directive,
                reason=f"coverage for {directive.value}",
            )

            assert event.data["directive"] == directive.value
            assert event.data["is_terminal"] == directive.is_terminal
