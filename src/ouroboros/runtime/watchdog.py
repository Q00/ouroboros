"""Wall-clock watchdog for ``ooo auto`` sessions (L2 v1 of #1157 / #1172).

The :class:`Watchdog` is checked at phase boundaries in the auto
pipeline's main loop (integration lands in L2-2, this PR ships the
runner). On each check it compares ``now() - session_started_at``
against :attr:`RuntimeControls.session_wall_clock_seconds`; if exceeded
*and* the watchdog has not yet fired for this session, it appends a
:data:`WATCHDOG_CANCEL_EVENT_TYPE` event to the EventStore and returns
a :class:`WatchdogDecision`.

Resume semantics
----------------

Timer state is *implicit* in the session's persisted ``created_at``
timestamp (``AutoPipelineState.created_at``) and the EventStore. On
resume, the watchdog reads the same ``created_at`` value and re-checks
against ``now()`` — a session paused inside the budget but resumed
*after* it elapses fires the cancellation immediately at the next
check, with no separate serialization step. See #1172 for the
fuller rationale (the earlier draft proposed restarting from the
resume moment; that was wrong and the redesign explicitly corrects
it).

Idempotency
-----------

The watchdog records its decision in-memory after firing
(``_fired_sessions``) so a follow-up check on the same session does
not append a duplicate event. The cancellation is recoverable across
process restarts via EventStore replay; the in-memory guard only
protects against duplicates within a single pipeline run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from ouroboros.events.base import BaseEvent
from ouroboros.runtime.controls import RuntimeControls

__all__ = [
    "WATCHDOG_AGGREGATE_TYPE",
    "WATCHDOG_CANCEL_EVENT_TYPE",
    "WATCHDOG_STOP_REASON_CODE",
    "Watchdog",
    "WatchdogDecision",
]


WATCHDOG_AGGREGATE_TYPE: str = "runtime_control"
"""EventStore ``aggregate_type`` for watchdog events. Confirmed additive
to the v1 projection vocabulary via #1172 / #946 informational comment."""

WATCHDOG_CANCEL_EVENT_TYPE: str = "runtime.watchdog.cancel"
"""Sole event family for the watchdog in v1. The v2 expansion path
(WAIT / RETRY / UNSTUCK directives) is intentionally absent."""

WATCHDOG_STOP_REASON_CODE: str = "watchdog_wall_clock_exceeded"
"""Adds the 9th canonical stop_reason_code. The auto pipeline
(L2-2 follow-up) sets this on the result envelope when a watchdog
cancel event is observed."""


class _EventAppender(Protocol):
    """Minimal protocol the watchdog needs from the EventStore.

    Only ``append`` is consumed. Real production code passes the live
    :class:`ouroboros.persistence.event_store.EventStore` instance;
    tests pass a stub that captures appended events.
    """

    async def append(self, event: BaseEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class WatchdogDecision:
    """Outcome of a single :meth:`Watchdog.check` call when the budget
    was exceeded.

    Attributes
    ----------
    session_id:
        The cancelled session's ``auto_session_id``.
    fired_at:
        Wall-clock UTC moment of the firing decision.
    elapsed_seconds:
        Integer floor of ``fired_at - session_started_at``.
    configured_budget_seconds:
        The :attr:`RuntimeControls.session_wall_clock_seconds` that
        was active when the watchdog fired.
    """

    session_id: str
    fired_at: datetime
    elapsed_seconds: int
    configured_budget_seconds: int

    @property
    def stop_reason_code(self) -> str:
        """The 9th canonical stop_reason_code for the envelope surface."""
        return WATCHDOG_STOP_REASON_CODE


class Watchdog:
    """Wall-clock watchdog for ``ooo auto`` sessions.

    Lifecycle:

    1. ``ooo auto`` instantiates one ``Watchdog`` per pipeline run
       with the active :class:`RuntimeControls` and a reference to the
       EventStore.
    2. At each phase boundary the pipeline calls
       :meth:`check`. If the budget has elapsed and the session has
       not already fired, the watchdog appends a
       ``runtime.watchdog.cancel`` event and returns the decision.
    3. The pipeline transitions to BLOCKED with
       ``stop_reason_code = "watchdog_wall_clock_exceeded"`` (L2-2).

    The watchdog never re-fires for the same ``session_id`` within a
    single ``Watchdog`` instance.
    """

    def __init__(
        self,
        controls: RuntimeControls,
        event_appender: _EventAppender,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Parameters
        ----------
        controls:
            The active runtime-control configuration.
        event_appender:
            Anything satisfying the :class:`_EventAppender` protocol —
            in production this is the live EventStore.
        now:
            Optional clock injection point (``() -> datetime``). Defaults
            to ``datetime.now(UTC)``. Tests pass a frozen ``now`` to
            exercise the budget-exceeded path without sleeping.
        """
        self._controls = controls
        self._appender = event_appender
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))
        self._fired_sessions: set[str] = set()

    @property
    def controls(self) -> RuntimeControls:
        return self._controls

    async def check(
        self,
        *,
        session_id: str,
        session_started_at: datetime,
    ) -> WatchdogDecision | None:
        """Return a :class:`WatchdogDecision` iff the watchdog fires now.

        Returns ``None`` for any of the following:

        - The watchdog is disabled (``session_wall_clock_seconds == 0``).
        - The elapsed time is still under budget.
        - The watchdog has already fired for *session_id* on this
          ``Watchdog`` instance.

        Side effect: when the watchdog *does* fire, exactly one
        ``runtime.watchdog.cancel`` event is appended to the
        configured event appender.
        """
        if not self._controls.watchdog_enabled:
            return None
        if session_id in self._fired_sessions:
            return None
        now = self._now()
        elapsed_seconds = int((now - session_started_at).total_seconds())
        budget = self._controls.session_wall_clock_seconds
        if elapsed_seconds <= budget:
            return None
        decision = WatchdogDecision(
            session_id=session_id,
            fired_at=now,
            elapsed_seconds=elapsed_seconds,
            configured_budget_seconds=budget,
        )
        event = BaseEvent(
            type=WATCHDOG_CANCEL_EVENT_TYPE,
            aggregate_type=WATCHDOG_AGGREGATE_TYPE,
            aggregate_id=session_id,
            data={
                "reason": "wall_clock_exceeded",
                "session_started_at": session_started_at.isoformat(),
                "fired_at": now.isoformat(),
                "elapsed_seconds": elapsed_seconds,
                "configured_budget_seconds": budget,
            },
        )
        await self._appender.append(event)
        self._fired_sessions.add(session_id)
        return decision

    def has_fired_for(self, session_id: str) -> bool:
        """Test/inspection helper — True iff this instance has already
        fired the watchdog for *session_id*."""
        return session_id in self._fired_sessions
