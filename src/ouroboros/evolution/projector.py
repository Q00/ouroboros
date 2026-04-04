"""LineageProjector - reconstructs OntologyLineage from event replay.

This is the defined fold/reduce function for lineage events. Given a list
of BaseEvents from the EventStore, it produces an OntologyLineage instance.
"""

from __future__ import annotations

import logging

from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    LineageStatus,
    OntologyLineage,
    RewindRecord,
)
from ouroboros.core.seed import OntologySchema
from ouroboros.events.base import BaseEvent

# Sentinel for generations that haven't completed (started/failed).
# These don't have a real ontology yet, but GenerationRecord requires one.
_PENDING_ONTOLOGY = OntologySchema(name="(pending)", description="(pending)", fields=())


logger = logging.getLogger(__name__)


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
        rewind_history: list[RewindRecord] = []

        for event in events:
            if event.type == "lineage.created":
                lineage = OntologyLineage(
                    lineage_id=event.aggregate_id,
                    goal=event.data.get("goal", ""),
                    created_at=event.timestamp,
                )

            elif event.type == "lineage.generation.started":
                data = event.data
                gen_num = data.get("generation_number", 0)
                if gen_num and gen_num not in generations:
                    # Track started-but-not-yet-completed generations
                    # so they appear in lineage status (e.g., stuck at wondering)
                    try:
                        phase = GenerationPhase(data.get("phase", "wondering"))
                    except ValueError:
                        continue  # Skip events with invalid/legacy phase values
                    generations[gen_num] = GenerationRecord(
                        generation_number=gen_num,
                        seed_id=data.get("seed_id") or "",
                        ontology_snapshot=_PENDING_ONTOLOGY,
                        phase=phase,
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
                    seed_quality_canary_feedback=data.get("seed_quality_canary_feedback", ()),
                    wonder_questions=tuple(data.get("wonder_questions", [])),
                    phase=GenerationPhase.COMPLETED,
                    created_at=event.timestamp,
                    seed_json=data.get("seed_json"),
                    execution_output=data.get("execution_output"),
                )
                generations[gen_num] = record

            elif event.type == "lineage.generation.phase_changed":
                data = event.data
                gen_num = data.get("generation_number", 0)
                if gen_num and gen_num in generations:
                    try:
                        phase = GenerationPhase(data.get("phase", "wondering"))
                    except ValueError:
                        continue  # Skip events with invalid/legacy phase values
                    old = generations[gen_num]
                    generations[gen_num] = old.model_copy(update={"phase": phase})

            elif event.type == "lineage.generation.failed":
                data = event.data
                gen_num = data["generation_number"]
                error_msg = data.get("error")
                try:
                    phase = GenerationPhase(data.get("phase", "failed"))
                except ValueError:
                    phase = GenerationPhase.FAILED

                if gen_num in generations:
                    old = generations[gen_num]
                    generations[gen_num] = old.model_copy(
                        update={"phase": phase, "failure_error": error_msg}
                    )
                else:
                    # Generation failed before completion record existed
                    generations[gen_num] = GenerationRecord(
                        generation_number=gen_num,
                        seed_id=data.get("seed_id") or "",
                        ontology_snapshot=_PENDING_ONTOLOGY,
                        phase=phase,
                        created_at=event.timestamp,
                        failure_error=error_msg,
                    )

            elif event.type == "lineage.generation.interrupted":
                data = event.data
                gen_num = data["generation_number"]
                interrupt_update: dict = {
                    "phase": GenerationPhase.INTERRUPTED,
                    "last_completed_phase": data.get("last_completed_phase"),
                    "partial_state": data.get("partial_state"),
                }
                if data.get("seed_json"):
                    interrupt_update["seed_json"] = data["seed_json"]
                if gen_num in generations:
                    old = generations[gen_num]
                    generations[gen_num] = old.model_copy(update=interrupt_update)
                else:
                    logger.warning(
                        "projector.interrupted_without_started",
                        extra={"generation_number": gen_num},
                    )
                    generations[gen_num] = GenerationRecord(
                        generation_number=gen_num,
                        seed_id="",
                        ontology_snapshot=_PENDING_ONTOLOGY,
                        created_at=event.timestamp,
                        **interrupt_update,
                    )

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
                from_gen = data["from_generation"]
                to_gen = data["to_generation"]
                # Capture discarded generations before truncating
                discarded = tuple(generations[k] for k in sorted(generations.keys()) if k > to_gen)
                rewind_history.append(
                    RewindRecord(
                        from_generation=from_gen,
                        to_generation=to_gen,
                        rewound_at=event.timestamp,
                        discarded_generations=discarded,
                    )
                )
                # Remove generations after the rewind point
                generations = {k: v for k, v in generations.items() if k <= to_gen}
                if lineage is not None:
                    lineage = lineage.with_status(LineageStatus.ACTIVE)

        if lineage is None:
            return None

        # Build final lineage with sorted generations and rewind history
        sorted_records = tuple(generations[k] for k in sorted(generations.keys()))
        return lineage.model_copy(
            update={
                "generations": sorted_records,
                "rewind_history": tuple(rewind_history),
            }
        )

    def find_resume_point(self, events: list[BaseEvent]) -> tuple[int, GenerationPhase, str | None]:
        """Determine where to resume from event history.

        Returns:
            Tuple of (generation_number, terminal_phase, last_completed_phase_within_gen).
            - last_completed_phase_within_gen is set for INTERRUPTED generations
              to enable phase-level resume (skip already-completed phases).
            Returns (0, COMPLETED, None) if no generations started.
        """
        last_gen = 0
        last_phase = GenerationPhase.COMPLETED
        interrupted_phase: str | None = None

        for event in events:
            if event.type == "lineage.generation.started":
                gen = event.data.get("generation_number", 0)
                phase_str = event.data.get("phase", "wondering")
                try:
                    phase = GenerationPhase(phase_str)
                except ValueError:
                    continue  # Skip unknown phases (e.g., legacy "rewound" events)
                if gen > last_gen:
                    last_gen = gen
                    last_phase = phase
                    interrupted_phase = None

            elif event.type == "lineage.generation.phase_changed":
                gen = event.data.get("generation_number", 0)
                phase_str = event.data.get("phase", "wondering")
                try:
                    phase = GenerationPhase(phase_str)
                except ValueError:
                    continue
                if gen >= last_gen:
                    last_gen = gen
                    last_phase = phase
                    interrupted_phase = None

            elif event.type == "lineage.generation.completed":
                gen = event.data.get("generation_number", 0)
                if gen >= last_gen:
                    last_gen = gen
                    last_phase = GenerationPhase.COMPLETED
                    interrupted_phase = None

            elif event.type == "lineage.generation.failed":
                gen = event.data.get("generation_number", 0)
                if gen >= last_gen:
                    last_gen = gen
                    last_phase = GenerationPhase.FAILED
                    interrupted_phase = None

            elif event.type == "lineage.generation.interrupted":
                gen = event.data.get("generation_number", 0)
                if gen >= last_gen:
                    last_gen = gen
                    last_phase = GenerationPhase.INTERRUPTED
                    interrupted_phase = event.data.get("last_completed_phase")

            elif event.type == "lineage.rewound":
                target_gen = event.data.get("to_generation", 0)
                if target_gen > 0:
                    last_gen = target_gen
                    last_phase = GenerationPhase.COMPLETED
                    interrupted_phase = None

        return last_gen, last_phase, interrupted_phase
