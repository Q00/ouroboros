"""Event factories for the example-questions candidate step.

The candidate step (``ouroboros.examples.candidates``) states readings of a
goal and writes candidate solutions under them. These events record what it
did. No event carries a candidate patch, a prompt, or probe source: a
candidate is described by its runtime pair, its reading id, its wall-clock
time, its observed cost, and the sha256 of its patch.

Event types (aggregate type ``auto_session``, aggregate id the step's
session id):

- ``examples.readings.stated``: the readings that passed the structural check.
- ``examples.candidate.started``: a candidate's runtime was dispatched.
- ``examples.candidate.completed``: a candidate finished with a patch.
- ``examples.candidate.skipped``: a candidate (or a whole pair) was not used,
  with a reason.
"""

from __future__ import annotations

from collections.abc import Sequence

from ouroboros.events.base import BaseEvent

EXAMPLES_AGGREGATE_TYPE = "auto_session"


def create_readings_stated_event(
    session_id: str,
    *,
    readings: Sequence[dict[str, str]],
    dropped: int,
    call_failed: str | None,
) -> BaseEvent:
    """Readings kept after the structural check (``reading_id``, ``span``, ``text`` each)."""
    return BaseEvent(
        type="examples.readings.stated",
        aggregate_type=EXAMPLES_AGGREGATE_TYPE,
        aggregate_id=session_id,
        data={
            "readings": [dict(reading) for reading in readings],
            "dropped": dropped,
            "call_failed": call_failed,
        },
    )


def create_candidate_started_event(
    session_id: str,
    *,
    candidate_id: str,
    backend: str,
    model: str | None,
    reading_id: str | None,
) -> BaseEvent:
    """A candidate runtime was dispatched in its own worktree."""
    return BaseEvent(
        type="examples.candidate.started",
        aggregate_type=EXAMPLES_AGGREGATE_TYPE,
        aggregate_id=session_id,
        data={
            "candidate_id": candidate_id,
            "backend": backend,
            "model": model,
            "reading_id": reading_id,
        },
    )


def create_candidate_completed_event(
    session_id: str,
    *,
    candidate_id: str,
    backend: str,
    model: str | None,
    reading_id: str | None,
    wall_clock_seconds: float,
    cost_usd: float | None,
    patch_digest: str,
    probe_count: int,
    probes_refused: int,
) -> BaseEvent:
    """A candidate finished: its patch digest, probe counts, time and cost."""
    return BaseEvent(
        type="examples.candidate.completed",
        aggregate_type=EXAMPLES_AGGREGATE_TYPE,
        aggregate_id=session_id,
        data={
            "candidate_id": candidate_id,
            "backend": backend,
            "model": model,
            "reading_id": reading_id,
            "wall_clock_seconds": wall_clock_seconds,
            "cost_usd": cost_usd,
            "patch_digest": patch_digest,
            "probe_count": probe_count,
            "probes_refused": probes_refused,
        },
    )


def create_candidate_skipped_event(
    session_id: str,
    *,
    candidate_id: str | None,
    backend: str,
    model: str | None,
    reading_id: str | None,
    reason: str,
    wall_clock_seconds: float | None,
    cost_usd: float | None,
    detail: str | None = None,
) -> BaseEvent:
    """A candidate, or a pair before any candidate (``candidate_id`` null), was not used.

    ``detail`` is the exception class name or the preflight reason, when there is one.
    """
    return BaseEvent(
        type="examples.candidate.skipped",
        aggregate_type=EXAMPLES_AGGREGATE_TYPE,
        aggregate_id=session_id,
        data={
            "candidate_id": candidate_id,
            "backend": backend,
            "model": model,
            "reading_id": reading_id,
            "reason": reason,
            "wall_clock_seconds": wall_clock_seconds,
            "cost_usd": cost_usd,
            "detail": detail,
        },
    )


__all__ = [
    "EXAMPLES_AGGREGATE_TYPE",
    "create_candidate_completed_event",
    "create_candidate_skipped_event",
    "create_candidate_started_event",
    "create_readings_stated_event",
]
