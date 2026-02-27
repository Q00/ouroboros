"""Unit tests for LineageProjector rewind handling.

Covers:
- find_resume_point skipping legacy "rewound" phase events
- find_resume_point returning correct state after rewind
- project() truncating generations on lineage.rewound event
"""

from ouroboros.core.lineage import GenerationPhase, LineageStatus
from ouroboros.events.base import BaseEvent
from ouroboros.evolution.projector import LineageProjector

LINEAGE_ID = "lin_rewind_test"


def _make_event(event_type: str, data: dict | None = None) -> BaseEvent:
    """Create a BaseEvent for testing."""
    return BaseEvent(
        type=event_type,
        aggregate_type="lineage",
        aggregate_id=LINEAGE_ID,
        data=data or {},
    )


class TestFindResumePointRewind:
    """Test find_resume_point handles rewind-related events."""

    def test_skips_legacy_rewound_phase(self) -> None:
        """A generation.started event with phase='rewound' is skipped without error."""
        projector = LineageProjector()
        events = [
            _make_event(
                "lineage.generation.started",
                {
                    "generation_number": 1,
                    "phase": "wondering",
                },
            ),
            _make_event(
                "lineage.generation.completed",
                {
                    "generation_number": 1,
                },
            ),
            # Legacy bug: rewind_to() used to emit this invalid phase
            _make_event(
                "lineage.generation.started",
                {
                    "generation_number": 1,
                    "phase": "rewound",
                },
            ),
        ]

        gen, phase = projector.find_resume_point(events)

        # The "rewound" event should be skipped; last valid state is gen 1 completed
        assert gen == 1
        assert phase == GenerationPhase.COMPLETED

    def test_resume_after_rewind_returns_completed(self) -> None:
        """After rewind, find_resume_point returns the rewind target generation as COMPLETED."""
        projector = LineageProjector()
        events = [
            _make_event(
                "lineage.generation.started",
                {
                    "generation_number": 1,
                    "phase": "wondering",
                },
            ),
            _make_event(
                "lineage.generation.completed",
                {
                    "generation_number": 1,
                },
            ),
            _make_event(
                "lineage.generation.started",
                {
                    "generation_number": 2,
                    "phase": "wondering",
                },
            ),
            _make_event(
                "lineage.generation.completed",
                {
                    "generation_number": 2,
                },
            ),
            # Rewind to gen 1 — no generation.started with "rewound" anymore
            # but the completed event for gen 2 was the last, so gen=2 COMPLETED
        ]

        gen, phase = projector.find_resume_point(events)

        assert gen == 2
        assert phase == GenerationPhase.COMPLETED

    def test_unknown_phase_does_not_crash(self) -> None:
        """Any unknown phase string is gracefully skipped."""
        projector = LineageProjector()
        events = [
            _make_event(
                "lineage.generation.started",
                {
                    "generation_number": 5,
                    "phase": "totally_invalid_phase",
                },
            ),
        ]

        gen, phase = projector.find_resume_point(events)

        # Unknown phase skipped; defaults remain
        assert gen == 0
        assert phase == GenerationPhase.COMPLETED


class TestProjectRewind:
    """Test project() handling of lineage.rewound events."""

    def test_rewind_truncates_generations(self) -> None:
        """Generations after the rewind point are removed from the projection."""
        projector = LineageProjector()

        ontology = {
            "name": "Test",
            "description": "Test model",
            "fields": [
                {"name": "x", "field_type": "string", "description": "field", "required": True},
            ],
        }

        events = [
            _make_event("lineage.created", {"goal": "Build something"}),
            _make_event(
                "lineage.generation.completed",
                {
                    "generation_number": 1,
                    "seed_id": "seed_1",
                    "ontology_snapshot": ontology,
                },
            ),
            _make_event(
                "lineage.generation.completed",
                {
                    "generation_number": 2,
                    "seed_id": "seed_2",
                    "ontology_snapshot": ontology,
                },
            ),
            _make_event(
                "lineage.generation.completed",
                {
                    "generation_number": 3,
                    "seed_id": "seed_3",
                    "ontology_snapshot": ontology,
                },
            ),
            # Rewind to generation 1
            _make_event(
                "lineage.rewound",
                {
                    "from_generation": 3,
                    "to_generation": 1,
                },
            ),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        assert len(lineage.generations) == 1
        assert lineage.generations[0].generation_number == 1
        assert lineage.status == LineageStatus.ACTIVE

    def test_rewind_sets_status_active(self) -> None:
        """After rewind, lineage status is set back to ACTIVE."""
        projector = LineageProjector()

        ontology = {
            "name": "Test",
            "description": "Test model",
            "fields": [
                {"name": "x", "field_type": "string", "description": "field", "required": True},
            ],
        }

        events = [
            _make_event("lineage.created", {"goal": "Build something"}),
            _make_event(
                "lineage.generation.completed",
                {
                    "generation_number": 1,
                    "seed_id": "seed_1",
                    "ontology_snapshot": ontology,
                },
            ),
            _make_event("lineage.exhausted", {}),
            # Rewind to gen 1 from exhausted state
            _make_event(
                "lineage.rewound",
                {
                    "from_generation": 1,
                    "to_generation": 1,
                },
            ),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        assert lineage.status == LineageStatus.ACTIVE
