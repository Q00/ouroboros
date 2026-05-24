"""Tests for the :class:`Watchdog` runner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.runtime.controls import RuntimeControls
from ouroboros.runtime.watchdog import (
    WATCHDOG_AGGREGATE_TYPE,
    WATCHDOG_CANCEL_EVENT_TYPE,
    WATCHDOG_STOP_REASON_CODE,
    Watchdog,
    WatchdogDecision,
)


class _CapturingAppender:
    """Test double for ``EventStore.append`` — records every appended event."""

    def __init__(self) -> None:
        self.events: list[BaseEvent] = []

    async def append(self, event: BaseEvent) -> None:
        self.events.append(event)

    async def query_events(
        self,
        aggregate_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        del offset
        events = [
            event
            for event in self.events
            if (aggregate_id is None or event.aggregate_id == aggregate_id)
            and (event_type is None or event.type == event_type)
        ]
        return events[:limit]


def _fixed_now(value: datetime):
    return lambda: value


@pytest.mark.asyncio
async def test_under_budget_does_not_fire() -> None:
    started = datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    now = started + timedelta(seconds=30)
    appender = _CapturingAppender()
    watchdog = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=600),
        event_appender=appender,
        now=_fixed_now(now),
    )

    decision = await watchdog.check(session_id="auto_001", session_started_at=started)

    assert decision is None
    assert appender.events == []
    assert not watchdog.has_fired_for("auto_001")


@pytest.mark.asyncio
async def test_exact_budget_does_not_fire() -> None:
    """At ``elapsed == budget`` the watchdog must NOT fire — strict
    inequality reserves the precise budget boundary for the session's
    own clean exit. Off-by-one mistakes here would cancel sessions that
    finish *exactly* on budget."""
    started = datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    now = started + timedelta(seconds=600)
    appender = _CapturingAppender()
    watchdog = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=600),
        event_appender=appender,
        now=_fixed_now(now),
    )

    decision = await watchdog.check(session_id="auto_001", session_started_at=started)

    assert decision is None
    assert appender.events == []


@pytest.mark.asyncio
async def test_over_budget_fires_and_appends_event() -> None:
    started = datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    now = started + timedelta(seconds=601)
    appender = _CapturingAppender()
    watchdog = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=600),
        event_appender=appender,
        now=_fixed_now(now),
    )

    decision = await watchdog.check(session_id="auto_001", session_started_at=started)

    assert isinstance(decision, WatchdogDecision)
    assert decision.session_id == "auto_001"
    assert decision.fired_at == now
    assert decision.elapsed_seconds == 601
    assert decision.configured_budget_seconds == 600
    assert decision.stop_reason_code == WATCHDOG_STOP_REASON_CODE

    # Exactly one event appended; shape matches the documented contract.
    assert len(appender.events) == 1
    event = appender.events[0]
    assert event.type == WATCHDOG_CANCEL_EVENT_TYPE
    assert event.aggregate_type == WATCHDOG_AGGREGATE_TYPE
    assert event.aggregate_id == "auto_001"
    assert event.data["reason"] == "wall_clock_exceeded"
    assert event.data["elapsed_seconds"] == 601
    assert event.data["configured_budget_seconds"] == 600
    assert event.data["fired_at"] == now.isoformat()
    assert event.data["session_started_at"] == started.isoformat()


@pytest.mark.asyncio
async def test_idempotent_within_instance() -> None:
    """A second check on the same session does not append a duplicate
    event — the in-memory ``_fired_sessions`` guard catches it."""
    started = datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    appender = _CapturingAppender()
    watchdog = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=10),
        event_appender=appender,
        now=_fixed_now(started + timedelta(seconds=60)),
    )

    first = await watchdog.check(session_id="auto_dup", session_started_at=started)
    second = await watchdog.check(session_id="auto_dup", session_started_at=started)

    assert first is not None
    assert second is None
    assert len(appender.events) == 1
    assert watchdog.has_fired_for("auto_dup")


@pytest.mark.asyncio
async def test_idempotent_across_new_instance_when_event_exists() -> None:
    started = datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    appender = _CapturingAppender()
    first = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=10),
        event_appender=appender,
        now=_fixed_now(started + timedelta(seconds=60)),
    )
    second = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=10),
        event_appender=appender,
        now=_fixed_now(started + timedelta(seconds=120)),
    )

    assert await first.check(session_id="auto_restart", session_started_at=started) is not None
    assert await second.check(session_id="auto_restart", session_started_at=started) is None

    assert len(appender.events) == 1
    assert second.has_fired_for("auto_restart")


@pytest.mark.asyncio
async def test_multiple_sessions_tracked_separately() -> None:
    started = datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    appender = _CapturingAppender()
    watchdog = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=10),
        event_appender=appender,
        now=_fixed_now(started + timedelta(seconds=60)),
    )

    a = await watchdog.check(session_id="auto_A", session_started_at=started)
    b = await watchdog.check(session_id="auto_B", session_started_at=started)

    assert a is not None
    assert b is not None
    assert len(appender.events) == 2
    assert {e.aggregate_id for e in appender.events} == {"auto_A", "auto_B"}


@pytest.mark.asyncio
async def test_disabled_watchdog_never_fires() -> None:
    """``session_wall_clock_seconds == 0`` opts out — no event, no
    decision, no internal state change."""
    started = datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    appender = _CapturingAppender()
    watchdog = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=0),
        event_appender=appender,
        now=_fixed_now(started + timedelta(days=7)),
    )

    decision = await watchdog.check(session_id="auto_off", session_started_at=started)

    assert decision is None
    assert appender.events == []
    assert not watchdog.has_fired_for("auto_off")


@pytest.mark.asyncio
async def test_resume_re_fires_when_elapsed_exceeds_after_paused_gap() -> None:
    """Resume semantics: a session that was paused inside the budget but
    resumed *after* it elapses must fire on the next check. The
    ``Watchdog`` instance does not persist state across pipeline runs;
    a *new* ``Watchdog`` instance after resume will see a fresh
    ``_fired_sessions`` set and fire on the next check past budget."""
    started = datetime(2026, 5, 22, 10, 0, 0, tzinfo=UTC)
    appender = _CapturingAppender()

    # First instance (pre-pause): under budget, does nothing.
    pre = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=600),
        event_appender=appender,
        now=_fixed_now(started + timedelta(seconds=120)),
    )
    assert await pre.check(session_id="auto_resume", session_started_at=started) is None

    # Process pauses, resumes a long time later → new ``Watchdog`` instance.
    post = Watchdog(
        controls=RuntimeControls(session_wall_clock_seconds=600),
        event_appender=appender,
        now=_fixed_now(started + timedelta(seconds=900)),
    )
    decision = await post.check(session_id="auto_resume", session_started_at=started)

    assert decision is not None
    assert decision.elapsed_seconds == 900
    assert len(appender.events) == 1


def test_constants_documented() -> None:
    """Pin the public constants — the L2-2 pipeline-integration PR
    imports them by name, so a rename here is a contract break."""
    assert WATCHDOG_AGGREGATE_TYPE == "runtime_control"
    assert WATCHDOG_CANCEL_EVENT_TYPE == "runtime.watchdog.cancel"
    assert WATCHDOG_STOP_REASON_CODE == "watchdog_wall_clock_exceeded"
