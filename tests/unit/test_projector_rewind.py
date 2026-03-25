"""Unit tests for LineageProjector rewind handling.

Covers:
- find_resume_point skipping legacy "rewound" phase events
- find_resume_point returning correct state after rewind
- project() truncating generations on lineage.rewound event
- rewind_history populated with discarded generations
- failure_error preserved in projected GenerationRecord
- multiple rewinds produce multiple RewindRecords
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
        """Generations after the rewind point are removed; discarded gens in rewind_history."""
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

        # Verify discarded generations captured in rewind_history
        assert len(lineage.rewind_history) == 1
        rr = lineage.rewind_history[0]
        assert rr.from_generation == 3
        assert rr.to_generation == 1
        assert len(rr.discarded_generations) == 2
        assert rr.discarded_generations[0].generation_number == 2
        assert rr.discarded_generations[1].generation_number == 3

    def test_rewind_sets_status_active(self) -> None:
        """After rewind, lineage status is ACTIVE and rewind_history is populated."""
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
        assert lineage.termination_reason is None, (
            "Rewind must clear termination_reason — stale terminal metadata "
            "on an ACTIVE lineage is internally inconsistent"
        )
        # Rewind from gen 1 to gen 1 means no discarded generations
        assert len(lineage.rewind_history) == 1
        assert lineage.rewind_history[0].discarded_generations == ()

    def test_failure_error_preserved(self) -> None:
        """failure_error from lineage.generation.failed event is stored in GenerationRecord."""
        projector = LineageProjector()

        events = [
            _make_event("lineage.created", {"goal": "Build something"}),
            _make_event(
                "lineage.generation.started",
                {
                    "generation_number": 1,
                    "phase": "executing",
                },
            ),
            _make_event(
                "lineage.generation.failed",
                {
                    "generation_number": 1,
                    "phase": "failed",
                    "error": "MCP transport disconnect",
                },
            ),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        assert len(lineage.generations) == 1
        gen = lineage.generations[0]
        assert gen.phase == GenerationPhase.FAILED
        assert gen.failure_error == "MCP transport disconnect"

    def test_failure_error_without_prior_started(self) -> None:
        """failure_error preserved even when no generation.started event preceded it."""
        projector = LineageProjector()

        events = [
            _make_event("lineage.created", {"goal": "Build something"}),
            _make_event(
                "lineage.generation.failed",
                {
                    "generation_number": 1,
                    "phase": "failed",
                    "error": "task cancellation",
                },
            ),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        gen = lineage.generations[0]
        assert gen.failure_error == "task cancellation"

    def test_multiple_rewinds_produce_multiple_records(self) -> None:
        """Each rewind event creates a separate RewindRecord."""
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
            # First rewind: gen 2 -> gen 1
            _make_event(
                "lineage.rewound",
                {
                    "from_generation": 2,
                    "to_generation": 1,
                },
            ),
            # Resume and complete gen 3
            _make_event(
                "lineage.generation.completed",
                {
                    "generation_number": 3,
                    "seed_id": "seed_3",
                    "ontology_snapshot": ontology,
                },
            ),
            _make_event(
                "lineage.generation.completed",
                {
                    "generation_number": 4,
                    "seed_id": "seed_4",
                    "ontology_snapshot": ontology,
                },
            ),
            # Second rewind: gen 4 -> gen 1
            _make_event(
                "lineage.rewound",
                {
                    "from_generation": 4,
                    "to_generation": 1,
                },
            ),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        assert len(lineage.generations) == 1
        assert len(lineage.rewind_history) == 2

        # First rewind discarded gen 2
        rr1 = lineage.rewind_history[0]
        assert rr1.from_generation == 2
        assert rr1.to_generation == 1
        assert len(rr1.discarded_generations) == 1
        assert rr1.discarded_generations[0].generation_number == 2

        # Second rewind discarded gen 3 and gen 4
        rr2 = lineage.rewind_history[1]
        assert rr2.from_generation == 4
        assert rr2.to_generation == 1
        assert len(rr2.discarded_generations) == 2
        assert rr2.discarded_generations[0].generation_number == 3
        assert rr2.discarded_generations[1].generation_number == 4

    def test_rewind_captures_failed_gen_with_error(self) -> None:
        """Discarded generations in rewind_history preserve failure_error."""
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
                "lineage.generation.started",
                {
                    "generation_number": 2,
                    "phase": "executing",
                },
            ),
            _make_event(
                "lineage.generation.failed",
                {
                    "generation_number": 2,
                    "phase": "failed",
                    "error": "MCP transport disconnect",
                },
            ),
            # Rewind to gen 1
            _make_event(
                "lineage.rewound",
                {
                    "from_generation": 2,
                    "to_generation": 1,
                },
            ),
        ]

        lineage = projector.project(events)

        assert lineage is not None
        assert len(lineage.rewind_history) == 1
        rr = lineage.rewind_history[0]
        assert len(rr.discarded_generations) == 1
        discarded = rr.discarded_generations[0]
        assert discarded.generation_number == 2
        assert discarded.phase == GenerationPhase.FAILED
        assert discarded.failure_error == "MCP transport disconnect"
