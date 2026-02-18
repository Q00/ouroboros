"""Lineage event definitions for evolutionary loop tracking.

Events follow the BaseEvent pattern (frozen pydantic, to_db_dict()) and use
the dot.notation.past_tense naming convention.

These events carry enough data to reconstruct OntologyLineage state via
LineageProjector.project().
"""

from ouroboros.events.base import BaseEvent


def lineage_created(
    lineage_id: str,
    goal: str,
) -> BaseEvent:
    """Create event for new lineage creation."""
    return BaseEvent(
        type="lineage.created",
        aggregate_type="lineage",
        aggregate_id=lineage_id,
        data={
            "goal": goal,
        },
    )


def lineage_generation_started(
    lineage_id: str,
    generation_number: int,
    phase: str,
    seed_id: str | None = None,
) -> BaseEvent:
    """Create event when a generation begins."""
    return BaseEvent(
        type="lineage.generation.started",
        aggregate_type="lineage",
        aggregate_id=lineage_id,
        data={
            "generation_number": generation_number,
            "phase": phase,
            "seed_id": seed_id,
        },
    )


def lineage_generation_completed(
    lineage_id: str,
    generation_number: int,
    seed_id: str,
    ontology_snapshot: dict,
    evaluation_summary: dict | None = None,
    wonder_questions: list[str] | None = None,
    seed_json: str | None = None,
) -> BaseEvent:
    """Create event when a generation completes successfully."""
    data = {
        "generation_number": generation_number,
        "seed_id": seed_id,
        "ontology_snapshot": ontology_snapshot,
        "evaluation_summary": evaluation_summary,
        "wonder_questions": wonder_questions or [],
    }
    if seed_json is not None:
        data["seed_json"] = seed_json
    return BaseEvent(
        type="lineage.generation.completed",
        aggregate_type="lineage",
        aggregate_id=lineage_id,
        data=data,
    )


def lineage_generation_failed(
    lineage_id: str,
    generation_number: int,
    phase: str,
    error: str,
) -> BaseEvent:
    """Create event when a generation fails mid-lifecycle."""
    return BaseEvent(
        type="lineage.generation.failed",
        aggregate_type="lineage",
        aggregate_id=lineage_id,
        data={
            "generation_number": generation_number,
            "phase": phase,
            "error": error,
        },
    )


def lineage_ontology_evolved(
    lineage_id: str,
    generation_number: int,
    delta: dict,
) -> BaseEvent:
    """Create event when ontology changes between generations."""
    return BaseEvent(
        type="lineage.ontology.evolved",
        aggregate_type="lineage",
        aggregate_id=lineage_id,
        data={
            "generation_number": generation_number,
            "delta": delta,
        },
    )


def lineage_converged(
    lineage_id: str,
    generation_number: int,
    reason: str,
    similarity: float,
) -> BaseEvent:
    """Create event when convergence is detected."""
    return BaseEvent(
        type="lineage.converged",
        aggregate_type="lineage",
        aggregate_id=lineage_id,
        data={
            "generation_number": generation_number,
            "reason": reason,
            "similarity": similarity,
        },
    )


def lineage_exhausted(
    lineage_id: str,
    generation_number: int,
    max_generations: int,
) -> BaseEvent:
    """Create event when max generations is reached."""
    return BaseEvent(
        type="lineage.exhausted",
        aggregate_type="lineage",
        aggregate_id=lineage_id,
        data={
            "generation_number": generation_number,
            "max_generations": max_generations,
        },
    )


def lineage_rewound(
    lineage_id: str,
    from_generation: int,
    to_generation: int,
) -> BaseEvent:
    """Create event when user rewinds to a previous generation."""
    return BaseEvent(
        type="lineage.rewound",
        aggregate_type="lineage",
        aggregate_id=lineage_id,
        data={
            "from_generation": from_generation,
            "to_generation": to_generation,
        },
    )


def lineage_wonder_degraded(
    lineage_id: str,
    generation_number: int,
    error: str,
) -> BaseEvent:
    """Create event when WonderEngine fails and falls back to defaults."""
    return BaseEvent(
        type="lineage.wonder.degraded",
        aggregate_type="lineage",
        aggregate_id=lineage_id,
        data={
            "generation_number": generation_number,
            "error": error,
        },
    )


def lineage_stagnated(
    lineage_id: str,
    generation_number: int,
    reason: str,
    window: int,
) -> BaseEvent:
    """Create event when stagnation is detected (repeated feedback/unchanged ontology)."""
    return BaseEvent(
        type="lineage.stagnated",
        aggregate_type="lineage",
        aggregate_id=lineage_id,
        data={
            "generation_number": generation_number,
            "reason": reason,
            "stagnation_window": window,
        },
    )
