"""RFC #1256 §I4 — `auto.interview.*` lifecycle events reach EventStore.

These tests pin the public contract added by the first-slice wiring:

1. ``AutoInterviewDriver.run`` emits ``auto.interview.opened`` to the
   wired EventStore before the inner loop starts.
2. On a clean return, ``auto.interview.finalized`` is emitted with the
   inner result's ``status`` / ``rounds`` / ``session_id`` / ``blocker``.
3. If the inner loop raises, ``auto.interview.failed`` is emitted before
   the exception propagates and the ``finalized`` event is **not**
   emitted (the wrapper does not swallow exceptions).
4. Without an EventStore the driver behaves exactly as before — no
   appends, no errors. This is the back-compat guarantee that lets every
   pre-existing call site (CLI, MCP handler, unit tests) continue to
   construct the driver without observability wiring.
5. EventStore failures must not break the interview loop — the driver
   logs and continues so the interview surface stays available even when
   the persistence layer is degraded.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    AutoInterviewResult,
)
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.state import AutoPipelineState
from ouroboros.events.base import BaseEvent


class _RecordingEventStore:
    """Minimal in-memory EventStore stub for the §I4 wiring tests.

    Mirrors only the ``append`` surface the driver uses. ``failures``
    seeded with exceptions are raised on subsequent ``append`` calls so a
    single fixture covers both happy-path and degraded-store scenarios.
    """

    def __init__(self, *, failures: list[Exception] | None = None) -> None:
        self.appended: list[BaseEvent] = []
        self._failures = list(failures or [])

    async def append(self, event: BaseEvent, **_: Any) -> None:
        if self._failures:
            raise self._failures.pop(0)
        self.appended.append(event)


def _build_state(tmp_path) -> AutoPipelineState:
    """Construct an interview-phase state with a deterministic session id."""
    state = AutoPipelineState(goal="emit observable lifecycle events", cwd=str(tmp_path))
    # AutoPipelineState already auto-generates an auto_session_id; we just
    # need a deterministic state instance with the goal/cwd set.
    return state


def _result(
    *,
    status: str = "ready",
    session_id: str | None = "iv_abc123",
    rounds: int = 3,
    blocker: str | None = None,
) -> AutoInterviewResult:
    return AutoInterviewResult(
        status=status,
        session_id=session_id,
        ledger=MagicMock(spec=SeedDraftLedger),
        rounds=rounds,
        blocker=blocker,
    )


def _patch_inner(*, return_value: Any = None, side_effect: Any = None):
    """Class-level patch of ``_run_inner`` (slots-friendly).

    ``AutoInterviewDriver`` is a ``@dataclass(slots=True)`` so instance
    attribute assignment is rejected. Patching at the class level swaps
    the unbound method, which works regardless of slots.
    """
    return patch.object(
        AutoInterviewDriver,
        "_run_inner",
        AsyncMock(return_value=return_value, side_effect=side_effect),
    )


@pytest.mark.asyncio
async def test_run_emits_opened_and_finalized_on_clean_exit(tmp_path) -> None:
    """Happy path: both lifecycle events reach the wired EventStore."""
    store = _RecordingEventStore()
    driver = AutoInterviewDriver(backend=MagicMock(), event_store=store)
    stub_result = _result(status="ready", session_id="iv_xyz", rounds=4)
    state = _build_state(tmp_path)
    ledger = MagicMock(spec=SeedDraftLedger)

    with _patch_inner(return_value=stub_result):
        result = await driver.run(state, ledger)

    # §I4 dispatch is fire-and-forget: drain background emit tasks
    # before asserting against ``store.appended``.
    await driver.wait_for_pending_emits()

    assert result.status == "ready"
    assert [event.type for event in store.appended] == [
        "auto.interview.opened",
        "auto.interview.finalized",
    ]
    opened, finalized = store.appended
    assert opened.aggregate_type == "auto_interview"
    assert opened.aggregate_id == state.auto_session_id
    assert opened.data["goal"] == state.goal
    assert opened.data["max_rounds"] == driver.max_rounds
    assert opened.data["cwd"] == state.cwd
    assert opened.data["resumed"] is False
    assert finalized.aggregate_id == state.auto_session_id
    assert finalized.data["status"] == "ready"
    assert finalized.data["rounds"] == 4
    assert finalized.data["interview_session_id"] == "iv_xyz"
    assert finalized.data["blocker"] == ""


@pytest.mark.asyncio
async def test_run_marks_resumed_when_state_has_interview_session_id(tmp_path) -> None:
    """``opened.data.resumed`` must reflect a pre-existing interview id."""
    store = _RecordingEventStore()
    driver = AutoInterviewDriver(backend=MagicMock(), event_store=store)

    state = _build_state(tmp_path)
    state.interview_session_id = "iv_already_running"

    with _patch_inner(return_value=_result()):
        await driver.run(state, MagicMock(spec=SeedDraftLedger))

    await driver.wait_for_pending_emits()

    opened = store.appended[0]
    assert opened.data["resumed"] is True


@pytest.mark.asyncio
async def test_run_emits_failed_event_and_reraises_on_inner_exception(tmp_path) -> None:
    """Exceptions escaping the inner loop emit ``auto.interview.failed``
    and propagate; ``auto.interview.finalized`` is NOT emitted because no
    result is available to describe."""
    store = _RecordingEventStore()
    driver = AutoInterviewDriver(backend=MagicMock(), event_store=store)

    class _Boom(RuntimeError):
        pass

    state = _build_state(tmp_path)
    with _patch_inner(side_effect=_Boom("backend offline")):
        with pytest.raises(_Boom):
            await driver.run(state, MagicMock(spec=SeedDraftLedger))

    await driver.wait_for_pending_emits()

    types = [event.type for event in store.appended]
    assert types == ["auto.interview.opened", "auto.interview.failed"]
    failed = store.appended[-1]
    assert failed.aggregate_id == state.auto_session_id
    assert failed.data["exception_type"] == "_Boom"
    assert "backend offline" in failed.data["exception_message"]


@pytest.mark.asyncio
async def test_run_emits_nothing_without_event_store(tmp_path) -> None:
    """Back-compat: a driver without an ``event_store`` makes no appends.

    Every pre-existing call site (CLI, MCP handler, hundreds of unit
    tests) constructs the driver without observability wiring; they must
    continue to behave exactly as before.
    """
    driver = AutoInterviewDriver(backend=MagicMock())  # no event_store
    assert driver.event_store is None

    state = _build_state(tmp_path)
    with _patch_inner(return_value=_result()):
        result = await driver.run(state, MagicMock(spec=SeedDraftLedger))

    assert result.status == "ready"  # behavior preserved


@pytest.mark.asyncio
async def test_event_store_failure_does_not_break_interview_loop(tmp_path) -> None:
    """A degraded EventStore must not raise into the interview surface.

    Per RFC #1256 §I4, observability is an observer — its failures may
    not propagate into the loop. The driver downgrades them to a
    structlog warning and returns the inner result unchanged.
    """
    store = _RecordingEventStore(
        failures=[RuntimeError("opened append failed"), RuntimeError("finalized append failed")]
    )
    driver = AutoInterviewDriver(backend=MagicMock(), event_store=store)
    expected = _result(status="ready", rounds=2)

    state = _build_state(tmp_path)
    with _patch_inner(return_value=expected):
        result = await driver.run(state, MagicMock(spec=SeedDraftLedger))

    await driver.wait_for_pending_emits()

    assert result is expected
    # Both append attempts failed → nothing recorded, no exception leaked.
    assert store.appended == []


@pytest.mark.asyncio
async def test_cancellation_from_wait_for_propagates_without_emitting_failed(tmp_path) -> None:
    """Bot-review blocker (commit 0a1a9c34 → req_1779886484_124):
    ``asyncio.CancelledError`` is the cancellation primitive
    ``AutoPipeline.run`` delivers via
    ``asyncio.wait_for(self.interview_driver.run(...), timeout=...)``.
    If the §I4 wrapper caught it as a generic exception and awaited the
    best-effort ``_emit_event`` append before re-raising, a slow
    EventStore could blow through the phase deadline by whatever the
    append latency happens to be — exactly the contract failure the
    bot reproduced with a 0.05 s wait_for and a 0.2 s blocking append.

    The fix narrowed the catch to ``Exception``; this test pins that:

    * ``CancelledError`` from ``_run_inner`` propagates immediately.
    * The ``failed`` event is NOT emitted — cancellation is a control
      signal, not an interview failure.
    * The pipeline-side ``asyncio.wait_for`` timeout window is
      respected (we assert the whole run completes inside it).
    """

    # The cancellation contract probes the CATCH shape, not the
    # latency bound — so ``opened`` must be FAST (so it does not eat
    # the outer wait_for budget by itself) but ``failed`` must be
    # slow enough that a regression to ``except BaseException``
    # (which would await the ``failed`` append before re-raising)
    # would surface as a ``TimeoutError`` instead of a
    # ``CancelledError``. A regression-vs-bot-blocker proof needs
    # both halves: the ``opened`` write completes in microseconds,
    # but a hypothetical ``failed`` write would block past the
    # outer 0.1 s budget.
    class _FastOpenSlowFailedStore:
        def __init__(self) -> None:
            self.appended: list[BaseEvent] = []

        async def append(self, event: BaseEvent, **_: Any) -> None:
            if event.type == "auto.interview.failed":
                # The regression would await this for the full 5 s,
                # exceeding the 0.1 s outer wait_for budget below.
                await asyncio.sleep(5.0)
            self.appended.append(event)

    store = _FastOpenSlowFailedStore()
    driver = AutoInterviewDriver(backend=MagicMock(), event_store=store)
    state = _build_state(tmp_path)

    async def _inner_raises_cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    with patch.object(
        AutoInterviewDriver,
        "_run_inner",
        AsyncMock(side_effect=_inner_raises_cancelled),
    ):
        # The fast path here is sub-ms — ``CancelledError`` must
        # propagate WITHOUT awaiting any ``failed`` append. ``timeout``
        # is set well under the slow ``failed`` append's 5 s so a
        # regression to ``except BaseException`` (which awaited the
        # append on cancel and added ~5 s before re-raising) would
        # surface as a ``TimeoutError`` here instead of a
        # ``CancelledError``.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                driver.run(state, MagicMock(spec=SeedDraftLedger)),
                timeout=0.1,
            )

    # No ``failed`` event emitted — cancellation is a control signal,
    # not an observable interview failure. The slow-store would have
    # been mid-append for ``finalized`` if the catch had widened back
    # to BaseException; that did not happen.
    assert all(event.type != "auto.interview.failed" for event in store.appended)


@pytest.mark.asyncio
async def test_keyboard_interrupt_propagates_without_emitting_failed(tmp_path) -> None:
    """Conjugate of the cancellation test: ``KeyboardInterrupt`` is
    the other commonly-encountered ``BaseException`` subclass. Like
    ``CancelledError``, it MUST propagate immediately so the operator's
    Ctrl-C is honored — the EventStore append path must not delay it.
    """
    store = _RecordingEventStore()
    driver = AutoInterviewDriver(backend=MagicMock(), event_store=store)
    state = _build_state(tmp_path)

    with patch.object(
        AutoInterviewDriver,
        "_run_inner",
        AsyncMock(side_effect=KeyboardInterrupt()),
    ):
        with pytest.raises(KeyboardInterrupt):
            await driver.run(state, MagicMock(spec=SeedDraftLedger))

    # Drain any pending background emit (``opened``); ``failed`` was
    # NOT scheduled because the catch is narrowed to ``Exception``.
    await driver.wait_for_pending_emits()

    assert [event.type for event in store.appended] == ["auto.interview.opened"]


@pytest.mark.asyncio
async def test_slow_opened_append_does_not_block_interview_loop(tmp_path) -> None:
    """Bot-review blocker (commit 2ca22628 → req_1779889312_138): the
    ``opened`` event is awaited inline BEFORE ``_run_inner`` runs, so
    a slow or lock-contended EventStore could consume the
    ``AutoPipeline.run`` interview ``asyncio.wait_for(interview_timeout)``
    budget and cause the phase to time out even though the inner
    interview would have completed.

    The bot reproduced this with a 0.05 s wait_for + a 0.2 s blocking
    append: ``TimeoutError`` raised before ``_run_inner`` ran.
    Production ``EventStore.append`` can also wait on SQLite locks
    (``busy_timeout=30000``), so the failure mode is not synthetic.

    Fix: ``_emit_event`` now wraps the append with a 1.0 s fail-open
    ``asyncio.wait_for``; a stuck observer is downgraded to a
    ``auto.interview.event_store_emit_timed_out`` warning and the loop
    proceeds.

    This test pins that contract: a 5 s slow ``opened`` append no
    longer holds up the interview — the inner result is returned
    promptly, the ``opened`` event is dropped (fail-open), and the
    ``finalized`` event still records the inner result.
    """

    class _StuckOpenedStore:
        """EventStore where ``opened`` hangs past the fail-open
        timeout but ``finalized`` is fast — mimics a transient SQLite
        lock contention that clears after the first append."""

        def __init__(self) -> None:
            self.appended: list[BaseEvent] = []
            self._calls = 0

        async def append(self, event: BaseEvent, **_: Any) -> None:
            self._calls += 1
            if self._calls == 1:
                # Sleep well past the 1.0 s fail-open bound to prove
                # the wrapper does not wait for us. A regression to
                # the unbounded shape would block here for the full
                # 5 s and the surrounding wait_for would fire.
                await asyncio.sleep(5.0)
            self.appended.append(event)

    store = _StuckOpenedStore()
    driver = AutoInterviewDriver(backend=MagicMock(), event_store=store)
    expected = _result(status="ready", rounds=1, session_id="iv_slow")
    state = _build_state(tmp_path)

    # The bot's reproducer shape: outer wait_for budget is well below
    # the slow append's 5 s. After the dispatch fix, the inner result
    # returns essentially instantly because the slow ``opened`` append
    # runs as a BACKGROUND task — it never touches the wait_for
    # boundary. Before the dispatch fix, even a 1.0 s fail-open
    # inline await would have exceeded a sub-second budget.
    with _patch_inner(return_value=expected):
        result = await asyncio.wait_for(
            driver.run(state, MagicMock(spec=SeedDraftLedger)),
            timeout=0.5,
        )
    # Drain background emits; the slow ``opened`` append will time out
    # (5 s sleep, 1.0 s fail-open bound), the fast ``finalized`` will
    # complete.
    await driver.wait_for_pending_emits()

    assert result is expected
    # ``opened`` was dropped (fail-open on latency); ``finalized``
    # succeeded because the second append was fast. The interview
    # proceeded — observability did not block the loop.
    assert [event.type for event in store.appended] == ["auto.interview.finalized"]


@pytest.mark.asyncio
async def test_slow_finalized_append_does_not_block_pipeline_deadline(tmp_path) -> None:
    """Conjugate of the slow-opened test: a slow ``finalized`` append
    must also be bounded, otherwise an interview that completed inside
    the phase deadline could still trip ``asyncio.wait_for`` on the
    way out because the wrapper awaits ``finalized`` after
    ``_run_inner`` returns."""

    class _StuckFinalizedStore:
        def __init__(self) -> None:
            self.appended: list[BaseEvent] = []
            self._calls = 0

        async def append(self, event: BaseEvent, **_: Any) -> None:
            self._calls += 1
            if self._calls == 2:
                await asyncio.sleep(5.0)
            self.appended.append(event)

    store = _StuckFinalizedStore()
    driver = AutoInterviewDriver(backend=MagicMock(), event_store=store)
    expected = _result(status="ready", rounds=1)
    state = _build_state(tmp_path)

    with _patch_inner(return_value=expected):
        result = await asyncio.wait_for(
            driver.run(state, MagicMock(spec=SeedDraftLedger)),
            timeout=0.5,
        )
    await driver.wait_for_pending_emits()

    assert result is expected
    # ``opened`` recorded; ``finalized`` dropped (fail-open on latency).
    assert [event.type for event in store.appended] == ["auto.interview.opened"]


@pytest.mark.asyncio
async def test_deadline_capped_outer_timeout_below_observer_timeout(tmp_path) -> None:
    """Bot-review blocker (commit 4fd6cfc1 → req_1779890159_141): the
    ``AutoPipeline.run`` interview ``asyncio.wait_for`` budget is
    ``_deadline_capped_timeout(...)``, which can validly fall BELOW
    the observer's own fail-open bound (1.0 s) when the top-level
    deadline is nearly expired, or when ``phase_timeout_seconds``
    policy sets the interview phase to 1 s. Under those conditions, a
    bounded INLINE await would still spend the entire pipeline
    deadline before its own bound fires — the bot's exact probe
    (slow 5 s ``opened`` + outer 0.1 s wait_for) raised
    ``TimeoutError`` after ~0.102 s on commit 4fd6cfc1.

    The dispatch fix moves the append off the critical path entirely:
    ``_emit_event`` schedules a background ``asyncio.Task`` and
    returns immediately. The pipeline's wait_for never sees the
    observer at all. This test pins that contract with the bot's
    exact deadline-capped reproducer shape.
    """

    class _StuckOpenedStore:
        def __init__(self) -> None:
            self.appended: list[BaseEvent] = []

        async def append(self, event: BaseEvent, **_: Any) -> None:
            await asyncio.sleep(5.0)
            self.appended.append(event)

    store = _StuckOpenedStore()
    driver = AutoInterviewDriver(backend=MagicMock(), event_store=store)
    expected = _result(status="ready", rounds=1)
    state = _build_state(tmp_path)

    # Outer timeout 0.1 s — BELOW the 1.0 s observer fail-open bound.
    # Before the dispatch fix, even the bounded-await shape exceeded
    # this. After the dispatch fix, driver.run returns essentially
    # instantly because the append never blocks the wait_for boundary.
    with _patch_inner(return_value=expected):
        result = await asyncio.wait_for(
            driver.run(state, MagicMock(spec=SeedDraftLedger)),
            timeout=0.1,
        )

    assert result is expected
    # The background tasks are still in flight; they will eventually
    # time out (5 s sleep, 1.0 s observer bound). We do NOT drain
    # them here — the contract under test is that the OUTER wait_for
    # was honored, not that the appends succeeded. Cancel pending
    # tasks so they don't leak into the next test.
    for task in list(driver._pending_emit_tasks):
        task.cancel()
    await driver.wait_for_pending_emits()
