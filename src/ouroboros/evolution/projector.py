"""LineageProjector - reconstructs OntologyLineage from event replay.

This is the defined fold/reduce function for lineage events. Given a list
of BaseEvents from the EventStore, it produces an OntologyLineage instance.
"""

from __future__ import annotations

from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    LineageStatus,
    OntologyLineage,
)
from ouroboros.core.seed import OntologySchema
from ouroboros.events.base import BaseEvent


class LineageProjector:
    """Reconstructs OntologyLineage state from event replay.

    Usage:
        events = await event_store.replay_lineage(lineage_id)
        projector = LineageProjector()
        lineage = projector.project(events)
    """

    def project(self, events: list[BaseEvent]) -> OntologyLineage | None:
        """Fold events into OntologyLineage state.

        Args:
            events: Ordered list of lineage events from EventStore.replay().

        Returns:
            Reconstructed OntologyLineage, or None if no events.
        """
        if not events:
            return None

        lineage: OntologyLineage | None = None
        generations: dict[int, GenerationRecord] = {}

        for event in events:
            if event.type == "lineage.created":
                lineage = OntologyLineage(
                    lineage_id=event.aggregate_id,
                    goal=event.data.get("goal", ""),
                    created_at=event.timestamp,
                )

            elif event.type == "lineage.generation.completed":
                data = event.data
                gen_num = data["generation_number"]

                ontology_data = data.get("ontology_snapshot", {})
                ontology = OntologySchema.model_validate(ontology_data)

                eval_data = data.get("evaluation_summary")
                eval_summary = EvaluationSummary.model_validate(eval_data) if eval_data else None

                record = GenerationRecord(
                    generation_number=gen_num,
                    seed_id=data["seed_id"],
                    parent_seed_id=data.get("parent_seed_id"),
                    ontology_snapshot=ontology,
                    evaluation_summary=eval_summary,
                    wonder_questions=tuple(data.get("wonder_questions", [])),
                    phase=GenerationPhase.COMPLETED,
                    created_at=event.timestamp,
                    seed_json=data.get("seed_json"),
                )
                generations[gen_num] = record

            elif event.type == "lineage.generation.failed":
                data = event.data
                gen_num = data["generation_number"]
                phase = GenerationPhase(data.get("phase", "failed"))

                if gen_num in generations:
                    # Update existing record to failed
                    old = generations[gen_num]
                    generations[gen_num] = old.model_copy(update={"phase": GenerationPhase.FAILED})

            elif event.type == "lineage.converged":
                if lineage is not None:
                    lineage = lineage.with_status(LineageStatus.CONVERGED)

            elif event.type == "lineage.exhausted":
                if lineage is not None:
                    lineage = lineage.with_status(LineageStatus.EXHAUSTED)

            elif event.type == "lineage.stagnated":
                if lineage is not None:
                    lineage = lineage.with_status(LineageStatus.CONVERGED)

            elif event.type == "lineage.rewound":
                data = event.data
                to_gen = data["to_generation"]
                # Remove generations after the rewind point
                generations = {k: v for k, v in generations.items() if k <= to_gen}
                if lineage is not None:
                    lineage = lineage.with_status(LineageStatus.ACTIVE)

        if lineage is None:
            return None

        # Build final lineage with sorted generations
        sorted_records = tuple(generations[k] for k in sorted(generations.keys()))
        return lineage.model_copy(update={"generations": sorted_records})

    def find_resume_point(self, events: list[BaseEvent]) -> tuple[int, GenerationPhase]:
        """Determine where to resume from event history.

        Returns:
            Tuple of (generation_number, last_completed_phase).
            Returns (0, COMPLETED) if no generations started.
        """
        last_gen = 0
        last_phase = GenerationPhase.COMPLETED

        for event in events:
            if event.type == "lineage.generation.started":
                gen = event.data.get("generation_number", 0)
                phase_str = event.data.get("phase", "wondering")
                if gen > last_gen:
                    last_gen = gen
                    last_phase = GenerationPhase(phase_str)

            elif event.type == "lineage.generation.completed":
                gen = event.data.get("generation_number", 0)
                if gen >= last_gen:
                    last_gen = gen
                    last_phase = GenerationPhase.COMPLETED

            elif event.type == "lineage.generation.failed":
                gen = event.data.get("generation_number", 0)
                if gen >= last_gen:
                    last_gen = gen
                    last_phase = GenerationPhase.FAILED

        return last_gen, last_phase
