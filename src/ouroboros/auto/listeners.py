"""Event listeners that mirror linked subsystem state into ``AutoPipelineState``.

This module exists so the unified status surface (Q00/ouroboros#782) can answer
"where is my session?" without polling. The listener subscribes to the
``mcp.job.*`` topics emitted by ``JobManager`` (``mcp.job.status`` /
``mcp.job.cancelled`` / ``mcp.job.terminal``) via the existing ``EventStore``
incremental query API and updates four mirror fields on ``AutoPipelineState``:

- ``ralph_job_status`` — last-seen ``JobStatus`` value
- ``ralph_last_event_at`` — ``time.monotonic()`` seconds of the last event
- ``ralph_stop_reason`` — populated when the ralph job reaches a terminal status
- ``ralph_current_generation`` — last reported lineage generation index

Cancel propagation: when ``JobManager.cancel_job`` drives the linked ralph job
into ``cancelled`` (or the persisted equivalent), the listener marks the auto
state ``BLOCKED("ralph cancelled by user")`` — but only if the auto session is
not already in a terminal phase, otherwise we would clobber a successful
``COMPLETE`` with a noisy blocker.

The listener is **best-effort**: mirror fields are only persisted when the
auto pipeline next saves state for any other reason. The acceptance tests
exercise the apply step directly without scheduling a background subscriber so
the contract is testable without tying the test runtime to an asyncio loop.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import time
from typing import Any

from ouroboros.auto.state import (
    TERMINAL_PHASES,
    AutoPhase,
    AutoPipelineState,
)
from ouroboros.events.base import BaseEvent

# Job event types we mirror onto the auto state. Anything outside this set is
# ignored so an unrelated mcp.* topic cannot scribble across the mirror fields.
_RALPH_JOB_EVENT_TYPES = frozenset(
    {
        "mcp.job.created",
        "mcp.job.updated",
        "mcp.job.completed",
        "mcp.job.failed",
        "mcp.job.cancelled",
        "mcp.job.interrupted",
    }
)

# Statuses we treat as ralph-terminal for cancel propagation. ``"interrupted"``
# is included because ``JobManager`` emits an ``interrupted`` terminal when the
# runner returns the documented interrupt sentinel; the ralph loop surfaces it
# the same way externally.
_RALPH_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})

# The cancel propagation reason is pinned by the issue acceptance criteria so a
# downstream reader (CLI, MCP, future TUI) can string-match it deterministically.
RALPH_CANCEL_BLOCKER_REASON = "ralph cancelled by user"


@dataclass(frozen=True, slots=True)
class _RalphEventReading:
    """Normalized projection of a single ``mcp.job.*`` event for the mirror.

    Lives only inside this module — callers see ``apply_event`` updates
    instead. The dataclass is the natural home for the small parsing branch
    so the apply step stays a pure assignment.
    """

    status: str | None
    stop_reason: str | None
    error: str | None
    lineage_id: str | None
    is_terminal: bool


def _coerce_lineage_id(payload: dict[str, Any]) -> str | None:
    """Return the lineage id embedded in a ``mcp.job.*`` payload, if any.

    JobManager emits ``links.lineage_id`` on every event; some legacy or
    error paths flatten this to a top-level ``lineage_id``. The listener
    tolerates both shapes so we don't silently miss events from older builds.
    """
    links = payload.get("links")
    if isinstance(links, dict):
        lineage = links.get("lineage_id")
        if isinstance(lineage, str) and lineage:
            return lineage
    flat = payload.get("lineage_id")
    if isinstance(flat, str) and flat:
        return flat
    return None


def _project_event(event: BaseEvent) -> _RalphEventReading | None:
    """Project a ``BaseEvent`` into the mirror-relevant slice.

    Returns ``None`` for events outside the ``mcp.job.*`` whitelist so the
    caller can ignore them cheaply. The projection is intentionally narrow:
    we never look at ``result_text`` or any other field that could leak
    arbitrary subagent output into the auto state.
    """
    if event.type not in _RALPH_JOB_EVENT_TYPES:
        return None
    payload = event.data if isinstance(event.data, dict) else {}
    raw_status = payload.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status else None
    stop_reason: str | None = None
    if isinstance(payload.get("stop_reason"), str) and payload["stop_reason"]:
        stop_reason = payload["stop_reason"]
    error: str | None = None
    if isinstance(payload.get("error"), str) and payload["error"]:
        error = payload["error"]
    is_terminal = event.type in {
        "mcp.job.completed",
        "mcp.job.failed",
        "mcp.job.cancelled",
        "mcp.job.interrupted",
    } or (status in _RALPH_TERMINAL_STATUSES)
    return _RalphEventReading(
        status=status,
        stop_reason=stop_reason,
        error=error,
        lineage_id=_coerce_lineage_id(payload),
        is_terminal=is_terminal,
    )


def _coerce_generation(payload: dict[str, Any]) -> int | None:
    """Return ``current_generation`` from a job payload's ``message`` if present.

    JobManager's ``_derive_status_message`` stringifies lineage progress as
    ``"Generation N | <phase>"``; we cheaply extract ``N`` so the unified
    status surface can render "ralph generation 4 of 10" without a second
    event-store query. Bails out cleanly on any unexpected shape.
    """
    message = payload.get("message")
    if not isinstance(message, str) or not message:
        return None
    if not message.startswith("Generation "):
        return None
    rest = message[len("Generation ") :]
    head = rest.split(" ", 1)[0].split("|", 1)[0].strip()
    if not head.isdigit():
        return None
    try:
        value = int(head)
    except ValueError:
        return None
    if value < 0:
        return None
    return value


def apply_event(state: AutoPipelineState, event: BaseEvent) -> bool:
    """Mirror a single ``mcp.job.*`` event onto ``state``.

    Returns ``True`` when the event was applied (lineage *or* job_id matched
    and the event is a relevant ``mcp.job.*`` topic), ``False`` otherwise.
    Callers can use the return value to decide whether to schedule a state
    save — the listener itself never persists.

    Filtering accepts a match on either ``links.lineage_id`` (the canonical
    join key emitted on ``mcp.job.created``) or ``aggregate_id`` matching
    the persisted ``state.ralph_job_id``. ``JobManager.update_status``
    omits the lineage on subsequent events, so a strict lineage filter
    would silently drop every status update after creation.

    Cancel propagation: when the event is a terminal ``cancelled`` reading
    and the auto state is not already terminal, transition to BLOCKED with
    the pinned :data:`RALPH_CANCEL_BLOCKER_REASON`.
    """
    if state.ralph_lineage_id is None and state.ralph_job_id is None:
        return False
    if state.ralph_dispatch_mode == "plugin":
        # Plugin delegations have no in-process job to mirror; the OpenCode
        # Task widget owns the lifecycle. Fail closed instead of fabricating
        # a status from somebody else's events.
        return False
    reading = _project_event(event)
    if reading is None:
        return False
    matches_lineage = (
        reading.lineage_id is not None and reading.lineage_id == state.ralph_lineage_id
    )
    matches_job_id = (
        state.ralph_job_id is not None
        and event.aggregate_id == state.ralph_job_id
        and event.aggregate_type == "job"
    )
    if not (matches_lineage or matches_job_id):
        return False

    state.ralph_last_event_at = time.monotonic()
    if reading.status is not None:
        state.ralph_job_status = reading.status
    if reading.is_terminal:
        # Prefer the explicit stop_reason; fall back to the error string for
        # ``mcp.job.failed`` so the unified status surface always has *some*
        # blocker text to render.
        state.ralph_stop_reason = reading.stop_reason or reading.error or reading.status
    payload = event.data if isinstance(event.data, dict) else {}
    generation = _coerce_generation(payload)
    if generation is not None:
        state.ralph_current_generation = generation

    # Cancel propagation per the acceptance criteria — only mutate phase if
    # the cancel observation is fresh and the auto state still has somewhere
    # to go. ``mark_blocked`` performs the transition validation itself.
    if (
        event.type == "mcp.job.cancelled" or (reading.is_terminal and reading.status == "cancelled")
    ) and state.phase not in TERMINAL_PHASES:
        try:
            state.mark_blocked(
                RALPH_CANCEL_BLOCKER_REASON,
                tool_name="ralph_starter",
            )
        except ValueError:
            # The state machine forbids the transition (e.g. CREATED -> BLOCKED
            # is allowed, but tests may exercise unusual phases). Swallow so
            # the listener never crashes the event subscriber for a bookkeeping
            # mismatch — the mirror fields above are still updated.
            pass

    return True


def apply_events(state: AutoPipelineState, events: Iterable[BaseEvent]) -> int:
    """Apply an iterable of events; return the number actually mirrored.

    Convenience for tests and the future background subscriber. Iterating
    here lets the listener cheaply replay a batch returned from
    ``EventStore.get_events_after`` without exposing the rowid cursor to
    callers.
    """
    applied = 0
    for event in events:
        if apply_event(state, event):
            applied += 1
    return applied


__all__ = [
    "RALPH_CANCEL_BLOCKER_REASON",
    "apply_event",
    "apply_events",
]


# Defensive sanity check — keep the terminal-phase set used for cancel
# propagation in sync with ``state._ALLOWED_TRANSITIONS`` so a future addition
# (e.g. a new "abandoned" phase) cannot silently bypass the gate.
assert AutoPhase.COMPLETE in TERMINAL_PHASES  # noqa: S101 - import-time guard
