"""Per-surface summary of how interviews close, read from the local EventStore.

The baseline for model-judged closure: for each calling surface, how many
interviews started, how many completed and after how many questions, how many
were closed early by forcing a Seed, and how many were abandoned. The summary
reads only labels and counts (``surface``, ``host``, ``total_rounds``,
``answered_rounds``) and never question or answer text. It reads the local
EventStore and sends nothing anywhere.

This module also records the one closure event the interview handler cannot
emit itself, ``interview.seed_forced``, on a best-effort basis.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import mean, median
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.bigbang.ambiguity import AmbiguityScore
    from ouroboros.bigbang.interview import InterviewState
    from ouroboros.events.base import BaseEvent
    from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger()

#: Interviews with no completion or forced Seed whose last event is older than
#: this count as abandoned; newer ones are still open.
DEFAULT_IDLE_AFTER = timedelta(hours=24)

#: Surface label for interviews started before ``interview.started`` carried one.
UNRECORDED_SURFACE = "unrecorded"

_PAGE_SIZE = 1000

#: Upper bound for one closure-event append. It runs on the path that returns a
#: Seed to the user, so a locked database must not hold that up for SQLite's
#: default 30 s busy timeout.
APPEND_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class SurfaceClosureSummary:
    """Closure outcomes for the interviews one surface started.

    Each interview with an ``interview.started`` event has exactly one outcome:
    ``forced_early`` (a Seed was generated with the gate bypassed), else
    ``completed``, else ``abandoned`` or ``open`` by idle time.
    """

    surface: str
    started: int
    completed: int
    forced_early: int
    abandoned: int
    open: int
    median_questions_completed: float | None
    mean_questions_completed: float | None
    median_questions_forced: float | None

    @property
    def abandon_rate(self) -> float | None:
        """Abandoned share of interviews that have reached an outcome."""
        settled = self.completed + self.forced_early + self.abandoned
        return self.abandoned / settled if settled else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "started": self.started,
            "completed": self.completed,
            "forced_early": self.forced_early,
            "abandoned": self.abandoned,
            "open": self.open,
            "abandon_rate": self.abandon_rate,
            "median_questions_completed": self.median_questions_completed,
            "mean_questions_completed": self.mean_questions_completed,
            "median_questions_forced": self.median_questions_forced,
        }


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _surface_label(data: dict[str, Any]) -> str:
    surface = data.get("surface")
    if not isinstance(surface, str) or not surface:
        return UNRECORDED_SURFACE
    host = data.get("host")
    if surface == "mcp" and isinstance(host, str) and host:
        return f"mcp:{host}"
    return surface


@dataclass(slots=True)
class _Interview:
    surface: str
    last_seen: datetime
    completed_rounds: int | None = None
    completed: bool = False
    forced: bool = False
    forced_rounds: int | None = None


def summarize_interview_closure(
    events: Iterable[BaseEvent],
    *,
    now: datetime | None = None,
    idle_after: timedelta = DEFAULT_IDLE_AFTER,
) -> list[SurfaceClosureSummary]:
    """Fold interview events into one :class:`SurfaceClosureSummary` per surface.

    Accepts events in any order. Interviews without an ``interview.started``
    event are skipped, since their surface is unknown. Missing new fields (rows
    written before this summary existed) are tolerated: such interviews report
    under ``unrecorded`` and still count as started, completed or abandoned.
    """
    ordered = sorted(
        (event for event in events if event.aggregate_type == "interview"),
        key=lambda event: _as_utc(event.timestamp),
    )
    interviews: dict[str, _Interview] = {}
    for event in ordered:
        if event.type == "interview.started" and event.aggregate_id not in interviews:
            interviews[event.aggregate_id] = _Interview(
                surface=_surface_label(event.data if isinstance(event.data, dict) else {}),
                last_seen=_as_utc(event.timestamp),
            )
    for event in ordered:
        interview = interviews.get(event.aggregate_id)
        if interview is None:
            continue
        data = event.data if isinstance(event.data, dict) else {}
        interview.last_seen = max(interview.last_seen, _as_utc(event.timestamp))
        if event.type == "interview.completed":
            interview.completed = True
            interview.completed_rounds = _count(data.get("total_rounds"))
        elif event.type == "interview.seed_forced" and not interview.forced:
            interview.forced = True
            interview.forced_rounds = _count(data.get("answered_rounds"))

    cutoff = _as_utc(now or datetime.now(UTC)) - idle_after
    grouped: dict[str, list[_Interview]] = {}
    for interview in interviews.values():
        grouped.setdefault(interview.surface, []).append(interview)

    summaries: list[SurfaceClosureSummary] = []
    for surface in sorted(grouped):
        members = grouped[surface]
        forced = [item for item in members if item.forced]
        completed = [item for item in members if item.completed and not item.forced]
        pending = [item for item in members if not item.completed and not item.forced]
        abandoned = [item for item in pending if item.last_seen < cutoff]
        completed_rounds = [
            item.completed_rounds for item in completed if item.completed_rounds is not None
        ]
        forced_rounds = [item.forced_rounds for item in forced if item.forced_rounds is not None]
        summaries.append(
            SurfaceClosureSummary(
                surface=surface,
                started=len(members),
                completed=len(completed),
                forced_early=len(forced),
                abandoned=len(abandoned),
                open=len(pending) - len(abandoned),
                median_questions_completed=_median(completed_rounds),
                mean_questions_completed=_mean(completed_rounds),
                median_questions_forced=_median(forced_rounds),
            )
        )
    return summaries


def _median(values: Sequence[int]) -> float | None:
    return float(median(values)) if values else None


def _mean(values: Sequence[int]) -> float | None:
    return round(float(mean(values)), 2) if values else None


async def load_interview_events(event_store: EventStore) -> list[BaseEvent]:
    """Read every ``interview`` aggregate event from an initialized store."""
    events: list[BaseEvent] = []
    offset = 0
    while True:
        page = await event_store.query_events(
            aggregate_type="interview",
            limit=_PAGE_SIZE,
            offset=offset,
        )
        events.extend(page)
        if len(page) < _PAGE_SIZE:
            return events
        offset += _PAGE_SIZE


async def record_interview_event(event: BaseEvent, event_store: EventStore | None = None) -> None:
    """Append ``event`` without ever failing the caller.

    With no ``event_store``, a default store is opened for this one append and
    closed again; opening it is bounded by SQLite's busy timeout, like any
    EventStore open. A store that is already open is not re-initialized. The
    append itself is bounded by :data:`APPEND_TIMEOUT_SECONDS`
    (``EventStore.append_durable``). Errors and timeouts are logged and
    swallowed: closure telemetry must not break or stall an interview or a Seed.
    """
    owned = event_store is None
    store = event_store
    try:
        if store is None:
            from ouroboros.persistence.event_store import EventStore

            store = EventStore()
        if not store.is_initialized:
            # Re-initializing an open store still touches the database, so it
            # runs only when the store is not open yet.
            await store.initialize()
        await store.append_durable(event, timeout=APPEND_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - telemetry is best effort
        log.warning("interview.closure_event_failed", event_type=event.type, error=str(exc))
    finally:
        if owned and store is not None:
            try:
                await store.close()
            except Exception:  # noqa: BLE001 - nothing left to report to
                pass


async def record_forced_seed(
    event_store: EventStore | None,
    state: InterviewState,
    score: AmbiguityScore | None,
    *,
    force: bool,
) -> None:
    """Record ``interview.seed_forced`` after a forced Seed; no-op unless ``force``."""
    if not force:
        return
    from ouroboros.bigbang.interview_closure import closure_snapshot
    from ouroboros.events.interview import interview_seed_forced

    snapshot = closure_snapshot(state, score)
    await record_interview_event(
        interview_seed_forced(
            state.interview_id,
            answered_rounds=snapshot["answered_rounds"],
            interview_completed=state.is_complete,
            closure=snapshot,
        ),
        event_store,
    )


__all__ = [
    "APPEND_TIMEOUT_SECONDS",
    "DEFAULT_IDLE_AFTER",
    "UNRECORDED_SURFACE",
    "SurfaceClosureSummary",
    "load_interview_events",
    "record_forced_seed",
    "record_interview_event",
    "summarize_interview_closure",
]
