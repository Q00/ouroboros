"""Material-progress taxonomy for the Ouroboros watchdog contract.

"Material progress" means an event whose persistence proves that a lineage
moved forward — not merely that the system was alive.  Contrast with
*activity* events (heartbeats, idle pings, intermediate log lines) that
confirm liveness but do not advance the evolutionary state machine.

The watchdog uses this taxonomy to distinguish two timeout clocks:

* ``generation_idle_timeout_seconds`` — no *activity* at all (any event)
* ``generation_no_progress_timeout_seconds`` — no *material* event

Each surface defines its own set because what counts as "forward motion"
differs per layer:

* **lineage** — generation lifecycle transitions and ontology mutations.
* **execution** — coordinator / decomposition / session terminal events.
* **session** — orchestrator-level task and session lifecycle transitions.
* **auto** — (intentionally empty) no per-surface material-event vocabulary
  defined yet for auto-mode; contribution welcome.
* **agent_process** — (intentionally empty) AgentProcess surface has no
  dedicated event schema yet; will be populated once the surface matures.

The ``MATERIAL_EVENTS_BY_SURFACE`` mapping makes this taxonomy explicit and
machine-queryable, surfacing the gaps in auto and agent_process rather than
hiding them inside private frozensets.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Per-surface material event sets
# ---------------------------------------------------------------------------

LINEAGE_MATERIAL_EVENTS: frozenset[str] = frozenset(
    {
        "lineage.generation.started",
        "lineage.generation.phase_changed",
        "lineage.generation.completed",
        "lineage.generation.interrupted",
        "lineage.generation.failed",
        "lineage.ontology.evolved",
        "lineage.converged",
        "lineage.stagnated",
        "lineage.exhausted",
    }
)

EXECUTION_MATERIAL_EVENTS: frozenset[str] = frozenset(
    {
        "execution.ac.stall_detected",
        "execution.coordinator.completed",
        "execution.coordinator.failed",
        "execution.decomposition.level_started",
        "execution.decomposition.level_completed",
        "execution.session.completed",
        "execution.session.failed",
        "execution.session.started",
        "execution.terminal",
    }
)

SESSION_MATERIAL_EVENTS: frozenset[str] = frozenset(
    {
        "orchestrator.session.cancelled",
        "orchestrator.session.completed",
        "orchestrator.session.failed",
        "orchestrator.session.started",
        "orchestrator.task.completed",
        "orchestrator.task.started",
    }
)

# ---------------------------------------------------------------------------
# Terminal acceptance-criteria status vocabulary
# ---------------------------------------------------------------------------

TERMINAL_AC_STATUSES: frozenset[str] = frozenset(
    {
        "completed",
        "failed",
        "skipped",
        "blocked",
        "invalid",
        "satisfied",
    }
)

# ---------------------------------------------------------------------------
# Unified surface map — makes gaps explicit
# ---------------------------------------------------------------------------

#: Maps surface name → frozenset of event type strings that constitute
#: material progress for that surface.  Surfaces with an empty frozenset are
#: *intentionally* empty: their material-event vocabulary has not yet been
#: formalised.  The empty entries are kept here to make the gap visible to
#: contributors rather than silently absent.
MATERIAL_EVENTS_BY_SURFACE: dict[str, frozenset[str]] = {
    "lineage": LINEAGE_MATERIAL_EVENTS,
    "execution": EXECUTION_MATERIAL_EVENTS,
    "session": SESSION_MATERIAL_EVENTS,
    # auto-mode surface: no dedicated material-event schema yet.
    "auto": frozenset(),
    # AgentProcess surface: event schema not yet formalised.
    "agent_process": frozenset(),
}

__all__ = [
    "EXECUTION_MATERIAL_EVENTS",
    "LINEAGE_MATERIAL_EVENTS",
    "MATERIAL_EVENTS_BY_SURFACE",
    "SESSION_MATERIAL_EVENTS",
    "TERMINAL_AC_STATUSES",
]
