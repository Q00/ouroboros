"""Unit tests for LineageProjector folding control.directive.emitted events.

Covers (issue #514, closes #473):
- A `control.directive.emitted` event with `aggregate_type="lineage"` is
  folded onto the projected `OntologyLineage.directive_emissions` view.
- Multiple directives appear in event-order on the projection.
- Optional correlation fields (generation_number, phase, is_terminal) are
  preserved when present and tolerate absence.
- Malformed directive events (missing/empty `directive` payload) are
  skipped silently rather than corrupting the projection.
- Existing lineage state (generations, status) projects unchanged when
  directive events are interleaved.
"""

from datetime import UTC, datetime

from ouroboros.core.lineage import LineageStatus
from ouroboros.events.base import BaseEvent
from ouroboros.evolution.projector import LineageProjector

LINEAGE_ID = "lin_directive_projector_test"


def _event(
    event_type: str,
    data: dict | None = None,
    *,
    timestamp: datetime | None = None,
) -> BaseEvent:
    """Build a `BaseEvent` aggregated by the lineage under test."""
    payload: dict = data or {}
    return BaseEvent(
        type=event_type,
        aggregate_type="lineage",
        aggregate_id=LINEAGE_ID,
        data=payload,
        timestamp=timestamp or datetime.now(UTC),
    )


class TestDirectiveProjection:
    def test_directive_appears_on_projected_lineage(self) -> None:
        """A control.directive.emitted event is folded onto directive_emissions."""
        projector = LineageProjector()
        events = [
            _event("lineage.created", {"goal": "intent under test"}),
            _event(
                "control.directive.emitted",
                {
                    "directive": "evolve",
                    "reason": "Advance generation.",
                    "emitted_by": "evolver",
                    "lineage_id": LINEAGE_ID,
                    "generation_number": 1,
                    "phase": "reflecting",
                    "is_terminal": False,
                },
            ),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        assert len(lineage.directive_emissions) == 1
        emission = lineage.directive_emissions[0]
        assert emission.directive == "evolve"
        assert emission.reason == "Advance generation."
        assert emission.emitted_by == "evolver"
        assert emission.generation_number == 1
        assert emission.phase == "reflecting"
        assert emission.is_terminal is False

    def test_directives_preserve_event_order(self) -> None:
        """Multiple directives arrive on the projection in event-replay order."""
        projector = LineageProjector()
        events = [
            _event("lineage.created", {"goal": "ordered emissions"}),
            _event(
                "control.directive.emitted",
                {
                    "directive": "evolve",
                    "reason": "First emission.",
                    "emitted_by": "evolver",
                },
            ),
            _event(
                "control.directive.emitted",
                {
                    "directive": "retry",
                    "reason": "Second emission.",
                    "emitted_by": "evolver",
                    "is_terminal": False,
                },
            ),
            _event(
                "control.directive.emitted",
                {
                    "directive": "converge",
                    "reason": "Third emission.",
                    "emitted_by": "evolver",
                    "is_terminal": True,
                },
            ),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        emitted = [e.directive for e in lineage.directive_emissions]
        assert emitted == ["evolve", "retry", "converge"]
        assert lineage.directive_emissions[-1].is_terminal is True

    def test_optional_correlation_fields_default_to_none(self) -> None:
        """generation_number and phase are None when absent from the payload."""
        projector = LineageProjector()
        events = [
            _event("lineage.created", {"goal": "no correlations"}),
            _event(
                "control.directive.emitted",
                {
                    "directive": "wait",
                    "reason": "External input pending.",
                    "emitted_by": "orchestrator",
                },
            ),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        emission = lineage.directive_emissions[0]
        assert emission.generation_number is None
        assert emission.phase is None

    def test_malformed_directive_event_is_skipped(self) -> None:
        """A directive event without a usable directive payload is silently skipped."""
        projector = LineageProjector()
        events = [
            _event("lineage.created", {"goal": "malformed input"}),
            _event(
                "control.directive.emitted",
                {
                    # 'directive' missing on purpose
                    "reason": "Bad event row.",
                    "emitted_by": "evolver",
                },
            ),
            _event(
                "control.directive.emitted",
                {
                    "directive": "",  # empty string also rejected
                    "reason": "Empty directive value.",
                    "emitted_by": "evolver",
                },
            ),
            _event(
                "control.directive.emitted",
                {
                    "directive": "continue",
                    "reason": "Healthy event row.",
                    "emitted_by": "evolver",
                },
            ),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        # Only the healthy row survives; malformed rows are absorbed silently.
        assert [e.directive for e in lineage.directive_emissions] == ["continue"]

    def test_directives_do_not_corrupt_existing_lineage_state(self) -> None:
        """Lineage state events project unchanged when interleaved with directives."""
        projector = LineageProjector()
        events = [
            _event("lineage.created", {"goal": "interleaved replay"}),
            _event(
                "control.directive.emitted",
                {
                    "directive": "evolve",
                    "reason": "Pre-converge directive.",
                    "emitted_by": "evolver",
                },
            ),
            _event(
                "lineage.converged",
                {
                    "generation_number": 1,
                    "reason": "All ACs passed.",
                    "ontology_similarity": 1.0,
                },
            ),
            _event(
                "control.directive.emitted",
                {
                    "directive": "converge",
                    "reason": "Post-converge directive.",
                    "emitted_by": "evolver",
                    "is_terminal": True,
                },
            ),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        assert lineage.status == LineageStatus.CONVERGED
        assert len(lineage.directive_emissions) == 2
        assert lineage.directive_emissions[-1].directive == "converge"

    def test_rewind_retains_directives_for_discarded_generation_audit(self) -> None:
        """Directive audit timeline still explains discarded rewind branches."""
        projector = LineageProjector()
        events = [
            _event("lineage.created", {"goal": "rewind directive pruning"}),
            _event(
                "control.directive.emitted",
                {
                    "directive": "evolve",
                    "reason": "Gen 1 decision.",
                    "emitted_by": "evolver",
                    "generation_number": 1,
                },
            ),
            _event(
                "control.directive.emitted",
                {
                    "directive": "retry",
                    "reason": "Discarded Gen 2 decision.",
                    "emitted_by": "evolver",
                    "generation_number": 2,
                },
            ),
            _event(
                "control.directive.emitted",
                {
                    "directive": "wait",
                    "reason": "Unscoped audit note.",
                    "emitted_by": "orchestrator",
                },
            ),
            _event("lineage.rewound", {"from_generation": 2, "to_generation": 1}),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        assert [e.directive for e in lineage.directive_emissions] == ["evolve", "retry", "wait"]

    def test_invalid_directive_correlation_shape_is_skipped(self) -> None:
        """Malformed optional fields should not abort lineage projection."""
        projector = LineageProjector()
        events = [
            _event("lineage.created", {"goal": "invalid directive shape"}),
            _event(
                "control.directive.emitted",
                {
                    "directive": "retry",
                    "reason": "Bad generation number.",
                    "emitted_by": "evolver",
                    "generation_number": "two",
                },
            ),
            _event(
                "control.directive.emitted",
                {
                    "directive": "continue",
                    "reason": "Healthy row.",
                    "emitted_by": "evolver",
                    "generation_number": 1,
                },
            ),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        assert [e.directive for e in lineage.directive_emissions] == ["continue"]
