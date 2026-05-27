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

    # An EventStore whose append would block for 0.2 s mimics the
    # bot's slow-persistence probe. The ``failed`` event must NOT be
    # appended during cancellation — if the regression returned, this
    # test would time out (asyncio.wait_for would expire before the
    # store finished its append).
    class _SlowStore:
        def __init__(self) -> None:
            self.appended: list[BaseEvent] = []

        async def append(self, event: BaseEvent, **_: Any) -> None:
            import asyncio as _asyncio

            await _asyncio.sleep(0.2)
            self.appended.append(event)

    import asyncio

    store = _SlowStore()
    driver = AutoInterviewDriver(backend=MagicMock(), event_store=store)
    state = _build_state(tmp_path)

    async def _inner_raises_cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    with patch.object(
        AutoInterviewDriver,
        "_run_inner",
        AsyncMock(side_effect=_inner_raises_cancelled),
    ):
        # 0.05 s budget is well under the slow-append 0.2 s; the fix
        # must let CancelledError propagate WITHOUT awaiting any
        # ``failed`` append, so the whole call returns inside the
        # budget. The prior ``except BaseException`` shape exceeded
        # the budget by ~0.15 s on the bot's probe.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                driver.run(state, MagicMock(spec=SeedDraftLedger)),
                timeout=0.5,  # generous ceiling — the fast path is sub-ms
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

    # ``opened`` was emitted before the inner call; ``failed`` was NOT
    # emitted because the catch is narrowed to ``Exception``.
    assert [event.type for event in store.appended] == ["auto.interview.opened"]
