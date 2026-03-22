"""Lineage state transition matrix for Gate Guard validation.

Defines which events are allowed in each LineageStatus, and the mapping
from terminal events to their resulting status. Both guard.py and
projector.py reference these definitions as the single source of truth.
"""

from __future__ import annotations

from ouroboros.core.lineage import LineageStatus

# Terminal event → resulting LineageStatus mapping.
# Used by LineageProjector.resolve_status() and project() to determine
# lineage status from events. Single source of truth for both guard and projector.
TERMINAL_EVENT_STATUS: dict[str, LineageStatus] = {
    "lineage.converged": LineageStatus.CONVERGED,
    "lineage.stagnated": LineageStatus.CONVERGED,  # stagnation is a form of convergence
    "lineage.exhausted": LineageStatus.EXHAUSTED,
    "lineage.rewound": LineageStatus.ACTIVE,
}

# LineageStatus × event_type → allowed?
# lineage.created is handled separately (always allowed as the first event).
# Non-lineage events (aggregate_type != "lineage") bypass the guard entirely.
# Observation events (ontology.evolved, wonder.degraded) don't change status
# but are only valid while the lineage is ACTIVE.
ALLOWED_TRANSITIONS: dict[LineageStatus, frozenset[str]] = {
    LineageStatus.ACTIVE: frozenset(
        {
            "lineage.generation.started",
            "lineage.generation.completed",
            "lineage.generation.phase_changed",
            "lineage.generation.failed",
            "lineage.ontology.evolved",
            "lineage.converged",
            "lineage.exhausted",
            "lineage.stagnated",
            "lineage.rewound",
            "lineage.wonder.degraded",
        }
    ),
    LineageStatus.CONVERGED: frozenset(
        {
            "lineage.rewound",  # rewind restores ACTIVE status
        }
    ),
    LineageStatus.EXHAUSTED: frozenset(
        {
            "lineage.rewound",
        }
    ),
    # ABORTED is a terminal state with no exit transitions.
    # Entry event (lineage.aborted) is not yet implemented.
    LineageStatus.ABORTED: frozenset(),
}


def is_transition_allowed(status: LineageStatus, event_type: str) -> bool:
    """Check if an event type is allowed in the given lineage status."""
    allowed = ALLOWED_TRANSITIONS.get(status)
    if allowed is None:
        return False
    return event_type in allowed
