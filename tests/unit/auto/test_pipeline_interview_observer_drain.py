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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.pipeline import (
    _INTERVIEW_OBSERVER_DRAIN_TIMEOUT_SECONDS,
    AutoPipeline,
)
from ouroboros.auto.state import AutoPipelineState, AutoStore
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


async def _unused_seed_generator_integration(_session_id: str):  # pragma: no cover
    raise AssertionError("seed generator should not be reached when the interview driver raises")


@pytest.mark.asyncio
async def test_pipeline_drains_observer_events_on_ordinary_exception(tmp_path) -> None:
    """Bot-review blocker (commit ``34fd7ee8``,
    ``supersede-requeue-pr:1260-34fd7ee``): when ``_run_inner`` raises
    an ordinary ``Exception``, ``AutoInterviewDriver.run`` schedules
    ``auto.interview.failed`` as a background ``asyncio.Task`` before
    re-raising. If the pipeline drained only on the clean-exit and
    timeout paths, that failed evidence would silently disappear the
    moment the composition root unwinds.

    The fix puts the drain in a ``try / finally`` around the interview
    ``wait_for`` so every exit path drains: clean completion, timeout
    translated to BLOCKED, AND any exception propagating out of the
    driver. This test pins the contract end-to-end via
    ``AutoPipeline.run`` with a real ``AutoInterviewDriver`` and a
    recording EventStore.
    """
    store = _RecordingEventStore(sleep_seconds=0.01)
    driver = _build_driver(store=store)
    auto_store = AutoStore(tmp_path)
    pipeline = AutoPipeline(driver, _unused_seed_generator_integration, store=auto_store)
    state = AutoPipelineState(goal="exercise drain on exception", cwd=str(tmp_path))
    auto_store.save(state)

    class _Boom(RuntimeError):
        pass

    # Patch ``_run_inner`` to raise; the driver wrapper's
    # ``except Exception`` schedules ``auto.interview.failed`` and
    # re-raises into the pipeline.
    with patch.object(
        AutoInterviewDriver,
        "_run_inner",
        AsyncMock(side_effect=_Boom("backend crashed mid-interview")),
    ):
        with pytest.raises(_Boom):
            await pipeline.run(state)

    # Pipeline's ``try / finally`` ran the drain before the exception
    # unwound past the interview phase, so both lifecycle events are
    # durable on the recording store — exactly what
    # ``ouroboros_query_events(auto_session_id)`` needs to reconstruct
    # the failed interview.
    types = [event.type for event in store.appended]
    assert types == ["auto.interview.opened", "auto.interview.failed"]
    failed = store.appended[-1]
    assert failed.aggregate_id == state.auto_session_id
    assert failed.data["exception_type"] == "_Boom"
    assert "backend crashed" in failed.data["exception_message"]
    # The driver's pending set is empty — drain consumed every task.
    assert not driver._pending_emit_tasks


@pytest.mark.asyncio
async def test_drain_refunds_elapsed_time_to_pipeline_deadline(tmp_path) -> None:
    """Bot-review blocker (commit ``769cdfeb``, req_1779940568_155):
    a slow but successful EventStore can change the pipeline outcome
    post-interview by consuming the remaining top-level deadline.
    Bot probe: ``_run_inner`` returns immediately, two appends sleep
    0.2 s each, ``deadline_at = now + 0.1`` — without observability
    the pipeline advances past the interview phase; with observability
    the drain consumes the 100 ms budget and the next
    ``_enforce_deadline`` gate translates the completed interview
    into a ``pipeline_timeout`` BLOCKED.

    The fix: the drain measures elapsed wall-clock time and refunds
    it to ``state.deadline_at`` / ``state.deadline_at_epoch`` so the
    deadline machinery sees observability as an invisible no-op. This
    test pins that contract by asserting the absolute deadline shifts
    forward by approximately the slow append latency after the drain.
    """
    started_epoch = time.time()
    started_monotonic = time.monotonic()

    # Slow store: each append sleeps 0.2 s. Two events (opened,
    # finalized) → drain runs ~0.2 s for the bounded shield since
    # ``wait_for_pending_emits`` gathers tasks concurrently and the
    # per-event ``_append_with_fail_open`` is bounded by the 1.0 s
    # observer timeout. The drain budget is 1.5 s so it completes.
    store = _RecordingEventStore(sleep_seconds=0.2)
    driver = _build_driver(store=store)
    pipeline = _build_pipeline(driver)

    # Tight remaining deadline — 0.5 s of headroom. Without a refund,
    # 0.2 s of drain elapsed pushes the post-drain wall-clock past
    # 0.5 s relative to the deadline pivot, but we're asserting the
    # ABSOLUTE deadline values shifted forward by the drain elapsed.
    deadline_horizon = 0.5
    state = MagicMock(spec=["deadline_at", "deadline_at_epoch"])
    state.deadline_at = started_monotonic + deadline_horizon
    state.deadline_at_epoch = started_epoch + deadline_horizon
    deadline_at_before = state.deadline_at
    deadline_at_epoch_before = state.deadline_at_epoch

    # Schedule two lifecycle events the way driver.run would.
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

    drain_started = time.monotonic()
    await pipeline._drain_interview_observer_events(state)
    drain_elapsed = time.monotonic() - drain_started

    # Both events persisted within the drain budget.
    assert [event.type for event in store.appended] == [
        "auto.interview.opened",
        "auto.interview.finalized",
    ]

    # Refund contract: the deadline must have advanced by at least
    # the measured drain elapsed (allowing a tiny scheduler tolerance
    # below for the elapsed-vs-clock discrepancy).
    deadline_shift = state.deadline_at - deadline_at_before
    deadline_epoch_shift = state.deadline_at_epoch - deadline_at_epoch_before
    # The refund is computed inside the helper using its own
    # ``time.monotonic()`` window, so the helper's elapsed is bounded
    # above by ``drain_elapsed`` measured here.
    assert deadline_shift >= drain_elapsed * 0.9, (
        f"deadline_at shifted by {deadline_shift:.3f}s, expected >= "
        f"{drain_elapsed * 0.9:.3f}s (90% of measured drain elapsed)"
    )
    assert deadline_shift <= drain_elapsed + 0.05, (
        f"deadline_at shifted by {deadline_shift:.3f}s, expected <= "
        f"{drain_elapsed + 0.05:.3f}s (drain elapsed + scheduler slack)"
    )
    # ``deadline_at_epoch`` tracks the wall-clock companion of
    # ``deadline_at`` for resume across processes; both must shift by
    # the same amount so the absolute target stays consistent.
    assert abs(deadline_shift - deadline_epoch_shift) < 0.01


@pytest.mark.asyncio
async def test_drain_does_not_touch_state_when_deadline_unarmed(tmp_path) -> None:
    """When no top-level deadline is armed (``deadline_at is None``),
    the drain must NOT introduce any state mutation. Pre-deadline
    sessions and feature-flagged opt-out paths rely on the deadline
    machinery staying inert when its inputs are inert.
    """
    _ = tmp_path
    store = _RecordingEventStore(sleep_seconds=0.01)
    driver = _build_driver(store=store)
    pipeline = _build_pipeline(driver)

    state = MagicMock(spec=["deadline_at", "deadline_at_epoch"])
    state.deadline_at = None
    state.deadline_at_epoch = None

    await driver._emit_event(
        "auto.interview.opened",
        "auto_test_session",
        goal="probe",
        max_rounds=1,
        cwd=str(tmp_path),
        resumed=False,
    )

    await pipeline._drain_interview_observer_events(state)

    assert state.deadline_at is None
    assert state.deadline_at_epoch is None
    assert [event.type for event in store.appended] == ["auto.interview.opened"]
