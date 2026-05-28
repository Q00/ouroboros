"""RFC #1256 §I4 — pipeline-side composition-root drain contract.

The interview driver intentionally schedules typed
``auto.interview.*`` EventStore appends as background tasks and never
awaits them so observability work cannot weaken
``AutoPipeline.run``'s interview ``asyncio.wait_for`` budget (bot
review on commit ``c5549124``, req_1779938459_153). The pipeline is
the §I4 composition root for that contract — it owns
``_drain_interview_observer_events``, called OUTSIDE the interview
``wait_for`` boundary so:

* Lifecycle events scheduled before / during the interview reach the
  EventStore for ``ouroboros_query_events`` inspection.
* A degraded / slow EventStore cannot stall the pipeline past
  ``_INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS``.
* The drain itself cannot turn a completed interview into a phase
  timeout (it runs after the inner ``wait_for`` has already
  succeeded or already raised ``TimeoutError``).

These tests pin the pipeline-side half of that contract by exercising
``_drain_interview_observer_events`` directly against a real
``AutoInterviewDriver``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.pipeline import (
    _INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS,
    AutoPipeline,
)
from ouroboros.events.base import BaseEvent


class _RecordingEventStore:
    """Minimal in-memory EventStore stub mirroring the driver's expected surface."""

    def __init__(self, *, sleep_seconds: float = 0.0) -> None:
        self.appended: list[BaseEvent] = []
        self._sleep_seconds = sleep_seconds

    async def append(self, event: BaseEvent, **_: Any) -> None:
        if self._sleep_seconds > 0:
            await asyncio.sleep(self._sleep_seconds)
        self.appended.append(event)


async def _unused_seed_generator(_session_id: str):  # pragma: no cover
    raise AssertionError("seed generator should not be invoked in drain tests")


def _build_driver(*, store: _RecordingEventStore) -> AutoInterviewDriver:
    return AutoInterviewDriver(backend=MagicMock(), event_store=store)


def _build_pipeline(driver: AutoInterviewDriver) -> AutoPipeline:
    return AutoPipeline(driver, _unused_seed_generator)


@pytest.mark.asyncio
async def test_drain_persists_pending_emits_outside_wait_for(tmp_path) -> None:
    """Pipeline drain persists scheduled lifecycle events to the EventStore.

    Mirrors the production sequence: ``driver.run`` would have
    scheduled ``opened`` + ``finalized`` as background tasks during
    its inner ``wait_for``; the pipeline's post-wait_for drain
    persists them before continuing to SEED_GENERATION.
    """
    _ = tmp_path  # state fixture parity
    store = _RecordingEventStore(sleep_seconds=0.01)
    driver = _build_driver(store=store)
    pipeline = _build_pipeline(driver)

    # Simulate the driver having scheduled lifecycle events during a
    # completed interview, without actually running the interview
    # loop. ``_emit_event`` is the same path ``driver.run`` uses.
    await driver._emit_event(
        "auto.interview.opened",
        "auto_test_session",
        goal="probe",
        max_rounds=1,
        cwd=str(tmp_path),
        resumed=False,
    )
    await driver._emit_event(
        "auto.interview.finalized",
        "auto_test_session",
        status="ready",
        rounds=1,
        interview_session_id="iv_probe",
        blocker="",
    )
    # ``run()`` returns without awaiting; the pipeline drain owns
    # durability OUTSIDE its critical wait_for. Exercise that surface
    # directly here.
    assert driver._pending_emit_tasks, (
        "Pre-condition: driver must have scheduled background emit tasks "
        "for the composition root to drain."
    )

    await pipeline._drain_interview_observer_events()

    assert [event.type for event in store.appended] == [
        "auto.interview.opened",
        "auto.interview.finalized",
    ]
    assert not driver._pending_emit_tasks


@pytest.mark.asyncio
async def test_drain_is_bounded_by_pipeline_timeout_constant(tmp_path) -> None:
    """A slow EventStore cannot stall the pipeline past the drain budget.

    Bot review on ``c5549124`` (req_1779938459_153) demanded that
    observability work be moved off the interview-critical path. The
    pipeline drain runs outside the interview ``wait_for``, but it
    must itself remain bounded so a pathologically slow EventStore
    cannot stall the next phase indefinitely.
    """
    _ = tmp_path
    # Sleep well past the drain budget AND the per-event fail-open
    # bound (1.0 s inside the driver), so the pipeline drain times out
    # and downgrades to a structlog warning.
    store = _RecordingEventStore(sleep_seconds=10.0)
    driver = _build_driver(store=store)
    pipeline = _build_pipeline(driver)

    await driver._emit_event(
        "auto.interview.opened",
        "auto_test_session",
        goal="probe",
        max_rounds=1,
        cwd=str(tmp_path),
        resumed=False,
    )

    started = time.monotonic()
    await pipeline._drain_interview_observer_events()
    elapsed = time.monotonic() - started

    # The drain must not exceed its declared budget by more than a
    # small scheduler tolerance. Without the bound, this would block
    # for the full 10 s sleep.
    assert elapsed < _INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS + 0.5, (
        f"drain elapsed {elapsed:.3f}s, expected <= "
        f"{_INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS + 0.5:.3f}s "
        "(bound + scheduler slack)"
    )

    # The slow append never reached the recording list — fail-open
    # semantics preserved at the composition-root boundary.
    assert store.appended == []
    # Clean up the still-in-flight background task so it does not
    # leak into the next test.
    for task in list(driver._pending_emit_tasks):
        task.cancel()
    await driver.wait_for_pending_emits()


@pytest.mark.asyncio
async def test_drain_is_no_op_when_no_pending_tasks(tmp_path) -> None:
    """The drain short-circuits when no background tasks are pending.

    Composition roots can call it unconditionally after every
    interview ``wait_for`` (clean exit or timeout) without worrying
    about latency overhead on the empty case.
    """
    _ = tmp_path
    store = _RecordingEventStore()
    driver = _build_driver(store=store)
    pipeline = _build_pipeline(driver)

    assert not driver._pending_emit_tasks

    started = time.monotonic()
    await pipeline._drain_interview_observer_events()
    elapsed = time.monotonic() - started

    # No tasks scheduled, no append attempted, no measurable wait.
    assert elapsed < 0.05
    assert store.appended == []


@pytest.mark.asyncio
async def test_drain_shields_pending_tasks_from_outer_cancellation(tmp_path) -> None:
    """Bot guidance — the drain uses ``asyncio.shield`` so an outer
    deadline cancellation racing the drain does not also cancel the
    persistence path mid-append. Even when the awaiter is cancelled,
    background appends already in flight continue to completion (or
    the event loop closes), so the EventStore never observes
    half-written events.
    """
    _ = tmp_path
    # 0.1 s append latency — well inside the drain budget — but we
    # cancel the awaiter at 0.02 s to prove the shield kept the task
    # alive long enough to record the event.
    store = _RecordingEventStore(sleep_seconds=0.1)
    driver = _build_driver(store=store)
    pipeline = _build_pipeline(driver)

    await driver._emit_event(
        "auto.interview.opened",
        "auto_test_session",
        goal="probe",
        max_rounds=1,
        cwd=str(tmp_path),
        resumed=False,
    )

    drain_task = asyncio.create_task(pipeline._drain_interview_observer_events())
    await asyncio.sleep(0.02)
    drain_task.cancel()
    # The drain coroutine may either swallow the cancel (TimeoutError
    # branch under shield) or propagate it; both are acceptable. The
    # invariant under test is the BACKGROUND task: it must complete
    # under shield even though the drain awaiter was cancelled.
    try:
        await drain_task
    except (asyncio.CancelledError, TimeoutError):
        pass

    # Allow the shielded background append to complete.
    await driver.wait_for_pending_emits()

    assert [event.type for event in store.appended] == ["auto.interview.opened"]
