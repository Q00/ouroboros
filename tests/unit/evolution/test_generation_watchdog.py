"""Tests for progress-aware generation watchdog controls."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from ouroboros.config.models import RuntimeControlsConfig
from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.events.lineage import lineage_generation_failed
import ouroboros.evolution.watchdog as watchdog_module
from ouroboros.evolution.watchdog import (
    WATCHDOG_CANCELLATION_MODE,
    GenerationProgressWatchdog,
    GenerationWatchdogTimeout,
)
from ouroboros.orchestrator.execution_runtime_scope import build_ac_runtime_scope
from ouroboros.persistence.event_store import EventStore


class _FakeMonotonicClock:
    def __init__(self) -> None:
        self.current = 0.0

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


async def _store() -> EventStore:
    db_path = Path(tempfile.gettempdir()) / f"ouroboros-watchdog-{uuid4().hex}.db"
    event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
    await event_store.initialize()
    return event_store


def _workflow_progress(
    execution_id: str,
    *,
    completed_count: int,
    status: str = "executing",
    session_id: str = "session-1",
) -> BaseEvent:
    return BaseEvent(
        type="workflow.progress.updated",
        aggregate_type="execution",
        aggregate_id=execution_id,
        data={
            "session_id": session_id,
            "acceptance_criteria": [
                {
                    "index": 1,
                    "content": "AC 1",
                    "status": "completed" if completed_count else status,
                },
                {
                    "index": 2,
                    "content": "AC 2",
                    "status": status,
                },
            ],
            "completed_count": completed_count,
            "total_count": 2,
            "current_phase": "Deliver",
            "activity": "Monitoring",
        },
    )


def _session_started(session_id: str, execution_id: str) -> BaseEvent:
    return BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id=session_id,
        data={
            "execution_id": execution_id,
            "seed_id": "seed-watch",
            "start_time": "2026-01-01T00:00:00+00:00",
        },
    )


def _session_tool_called(session_id: str) -> BaseEvent:
    return BaseEvent(
        type="orchestrator.tool.called",
        aggregate_type="session",
        aggregate_id=session_id,
        data={"tool_name": "Bash", "called_at": "2026-01-01T00:00:00+00:00"},
    )


def _ac_heartbeat(session_id: str, ac_id: str, message_count: int) -> BaseEvent:
    return BaseEvent(
        type="execution.ac.heartbeat",
        aggregate_type="execution",
        aggregate_id=ac_id,
        data={
            "session_id": session_id,
            "ac_index": 0,
            "elapsed_seconds": float(message_count),
            "message_count": message_count,
            "timestamp": "2026-01-01T00:00:00+00:00",
        },
    )


def _subagent_started(child_execution_id: str, parent_execution_id: str) -> BaseEvent:
    return BaseEvent(
        type="execution.subagent.started",
        aggregate_type="execution",
        aggregate_id=child_execution_id,
        data={
            "parent_execution_id": parent_execution_id,
            "child_ac": "child task",
            "depth": 1,
        },
    )


def _decomposition_level_event(session_id: str, event_type: str, level: int) -> BaseEvent:
    return BaseEvent(
        type=event_type,
        aggregate_type="execution",
        aggregate_id=session_id,
        data={
            "level": level,
            "total_levels": 2,
            "child_indices": [0],
            "ac_count": 1,
            "successful": 1,
            "failed": 0,
            "blocked": 0,
            "total": 1,
        },
    )


def _watchdog(
    event_store: EventStore,
    *,
    lineage_id: str = "lin-watch",
    generation_number: int = 1,
    execution_id: str = "exec-watch",
    **control_overrides: Any,
) -> GenerationProgressWatchdog:
    control_values = {
        "generation_idle_timeout_seconds": 1.0,
        "generation_no_progress_timeout_seconds": 1.0,
        "generation_safety_timeout_seconds": 0,
        "watchdog_poll_seconds": 0.02,
        **control_overrides,
    }
    controls = RuntimeControlsConfig(**control_values)
    return GenerationProgressWatchdog(
        event_store=event_store,
        lineage_id=lineage_id,
        generation_number=generation_number,
        execution_id=execution_id,
        controls=controls,
    )


@pytest.mark.asyncio
async def test_productive_long_run_resets_material_progress_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Material progress keeps a generation alive past the no-progress window."""
    clock = _FakeMonotonicClock()
    monkeypatch.setattr(watchdog_module, "time", SimpleNamespace(monotonic=clock))
    event_store = await _store()
    execution_id = "exec-productive"
    watchdog = _watchdog(
        event_store,
        execution_id=execution_id,
        # Keep this above one fake-clock progress interval so a scheduler
        # poll that lands immediately before the next persisted progress event
        # does not make the test flaky in the full-suite run.
        generation_no_progress_timeout_seconds=0.12,
        watchdog_poll_seconds=0.005,
    )

    async def productive_work() -> str:
        for completed in (0, 1, 2):
            await asyncio.sleep(0.01)
            clock.advance(0.05)
            await event_store.append(_workflow_progress(execution_id, completed_count=completed))
        await asyncio.sleep(0.01)
        clock.advance(0.05)
        return "done"

    assert await watchdog.watch(productive_work()) == "done"


@pytest.mark.asyncio
async def test_busy_run_without_material_progress_times_out() -> None:
    """Activity alone does not count as material progress."""
    event_store = await _store()
    lineage_id = "lin-busy"
    execution_id = "exec-busy"
    watchdog = _watchdog(
        event_store,
        lineage_id=lineage_id,
        execution_id=execution_id,
        generation_no_progress_timeout_seconds=0.07,
    )

    async def busy_work() -> str:
        await event_store.append(_workflow_progress(execution_id, completed_count=0))
        try:
            while True:
                await asyncio.sleep(0.02)
                await event_store.append(_workflow_progress(execution_id, completed_count=0))
        except asyncio.CancelledError:
            try:
                await event_store.append(
                    lineage_generation_failed(
                        lineage_id,
                        1,
                        "cancelled",
                        "Generation cancelled",
                    )
                )
            except PersistenceError:
                # The cancellation cleanup event is incidental to this watchdog
                # test.  Python 3.14 can cancel while the in-memory SQLite
                # connection is being recycled, so do not let cleanup
                # persistence mask the expected watchdog timeout.
                pass
            raise

    with pytest.raises(GenerationWatchdogTimeout) as exc_info:
        await watchdog.watch(busy_work())

    assert exc_info.value.timeout_kind == "no_material_progress_timeout"
    events = await event_store.replay("lineage", lineage_id)
    # The watchdog_decision event is the legacy contract — kept so
    # status surfaces that already filter by this type continue
    # working. Per #578, the watchdog also now emits a
    # control.directive.emitted event after the decision, so we no
    # longer assert the decision event is *last* — only that it is
    # present and ordered before the directive event.
    decision_idx = next(
        (
            i
            for i, event in enumerate(events)
            if event.type == "lineage.generation.watchdog_decision"
        ),
        None,
    )
    assert decision_idx is not None
    directive_idx = next(
        (i for i, event in enumerate(events) if event.type == "control.directive.emitted"),
        None,
    )
    assert directive_idx is not None
    assert decision_idx < directive_idx


@pytest.mark.asyncio
async def test_no_progress_timeout_emits_retry_directive() -> None:
    """Issue #578 directive-mapping contract.

    A ``no_material_progress_timeout`` is surfaced as a failed generation
    outcome, so the watchdog must emit the same directive the evolution loop
    would emit for ``StepAction.FAILED`` under the default retry budget.

    Pins three things:

    1. The legacy ``lineage.generation.watchdog_decision`` event
       still lands (existing consumers keep working), and its
       ``details`` now carries the resolved directive so single-
       stream consumers do not have to subscribe to a second event.
    2. A dedicated ``control.directive.emitted`` event lands keyed
       on the lineage aggregate, with ``emitted_by="generation.watchdog"`` so
       projectors can attribute the directive to its source.
    3. ``is_terminal`` matches the directive (RETRY is non-terminal).
    """
    event_store = await _store()
    lineage_id = "lin-578-unstuck"
    execution_id = "exec-578-unstuck"
    watchdog = _watchdog(
        event_store,
        lineage_id=lineage_id,
        execution_id=execution_id,
        generation_no_progress_timeout_seconds=0.07,
    )

    async def busy_work() -> str:
        await event_store.append(_workflow_progress(execution_id, completed_count=0))
        try:
            while True:
                await asyncio.sleep(0.02)
                await event_store.append(_workflow_progress(execution_id, completed_count=0))
        except asyncio.CancelledError:
            raise

    with pytest.raises(GenerationWatchdogTimeout):
        await watchdog.watch(busy_work())

    events = await event_store.replay("lineage", lineage_id)

    # Legacy event still present and now carries the directive.
    decision_events = [e for e in events if e.type == "lineage.generation.watchdog_decision"]
    assert len(decision_events) == 1
    decision_details = decision_events[0].data.get("details") or {}
    assert decision_details.get("directive") == "retry"
    assert decision_details.get("directive_is_terminal") is False
    assert decision_details.get("step_action") == "failed"
    assert decision_details.get("retry_budget_remaining") == 1
    assert decision_details.get("cancellation_mode") == WATCHDOG_CANCELLATION_MODE
    assert decision_details.get("watchdog_decision_event_id") == decision_events[0].id
    idempotency_key = decision_details.get("watchdog_directive_idempotency_key")
    assert isinstance(idempotency_key, str)
    assert idempotency_key.startswith("generation.watchdog:")

    # Dedicated control-plane event lands on the lineage aggregate.
    directive_events = [e for e in events if e.type == "control.directive.emitted"]
    assert len(directive_events) == 1
    directive_event = directive_events[0]
    assert directive_event.aggregate_type == "lineage"
    assert directive_event.aggregate_id == lineage_id
    assert directive_event.data["target_type"] == "lineage"
    assert directive_event.data["target_id"] == lineage_id
    assert directive_event.data["emitted_by"] == "generation.watchdog"
    assert directive_event.data["directive"] == "retry"
    assert directive_event.data["phase"] == "executing"
    assert directive_event.data["idempotency_key"] == idempotency_key
    # Watchdog correlation fields propagate so a projector filtering
    # by execution / generation does not have to join back to the
    # lineage state event.
    assert directive_event.data["execution_id"] == execution_id
    assert directive_event.data["generation_number"] == 1
    # ``extra`` carries the source watchdog metadata for debugging.
    extra = directive_event.data.get("extra") or {}
    assert extra.get("watchdog_action") == "timeout"
    assert extra.get("timeout_kind") == "no_material_progress_timeout"
    assert extra.get("cancellation_mode") == WATCHDOG_CANCELLATION_MODE
    assert extra.get("step_action") == "failed"
    assert extra.get("retry_budget_remaining") == 1
    assert extra.get("watchdog_decision_event_id") == decision_events[0].id


@pytest.mark.asyncio
async def test_timeout_preserved_when_watchdog_decision_batch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directive decisions are atomic and must not mask the watchdog timeout."""
    clock = _FakeMonotonicClock()
    monkeypatch.setattr(watchdog_module, "time", SimpleNamespace(monotonic=clock))
    event_store = await _store()
    lineage_id = "lin-578-control-fail"
    execution_id = "exec-578-control-fail"
    watchdog = _watchdog(
        event_store,
        lineage_id=lineage_id,
        execution_id=execution_id,
        generation_safety_timeout_seconds=0.1,
        watchdog_poll_seconds=0.005,
    )

    async def append_batch_failure(events: list[BaseEvent]) -> None:
        assert [event.type for event in events] == [
            "lineage.generation.watchdog_decision",
            "control.directive.emitted",
        ]
        raise PersistenceError(
            "watchdog decision batch failed",
            operation="append_batch",
            details={"event_types": [event.type for event in events]},
        )

    monkeypatch.setattr(event_store, "append_batch", append_batch_failure)

    async def long_work() -> str:
        try:
            while True:
                await asyncio.sleep(0.01)
                clock.advance(0.05)
        except asyncio.CancelledError:
            raise

    with pytest.raises(GenerationWatchdogTimeout) as exc_info:
        await watchdog.watch(long_work())

    assert exc_info.value.timeout_kind == "safety_timeout"
    events = await event_store.replay("lineage", lineage_id)
    assert [e for e in events if e.type == "lineage.generation.watchdog_decision"] == []
    assert [e for e in events if e.type == "control.directive.emitted"] == []


@pytest.mark.asyncio
async def test_directive_emission_alphabet_matches_watchdog_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the cross-module invariant for #578.

    ``WATCHDOG_TIMEOUT_KINDS`` (in ``evolution.directive_mapping``) is
    the public classification alphabet for watchdog timeouts.
    ``GenerationProgressWatchdog._raise_timeout`` is the only site
    that constructs ``GenerationWatchdogTimeout`` instances. If the
    watchdog grows a new threshold name without adding it to the
    alphabet, timeout metadata and audit payloads drift from the runtime
    contract.

    This test snapshots the watchdog's actual raise sites by
    inspecting the source bytecode constants — a brittle but
    intentional canary so the alphabet drift is caught at test time
    rather than in production replay.
    """
    from ouroboros.evolution.directive_mapping import WATCHDOG_TIMEOUT_KINDS
    from ouroboros.evolution.watchdog import GenerationProgressWatchdog

    # ``_raise_if_threshold_exceeded`` is the only site that names
    # each timeout kind as a string literal — the kind strings get
    # passed positionally into ``_raise_timeout``. Walk the
    # function's bytecode constants to enumerate the alphabet the
    # watchdog actually raises today.
    decision_site = GenerationProgressWatchdog._raise_if_threshold_exceeded
    raised_kinds = {
        const
        for const in decision_site.__code__.co_consts
        if isinstance(const, str) and const.endswith("timeout")
    }
    # Every kind the watchdog raises must have an entry in the
    # public alphabet, and every alphabet entry must be a real
    # raise site — the two sets must be equal.
    assert raised_kinds == WATCHDOG_TIMEOUT_KINDS, (
        f"watchdog raises {raised_kinds} but alphabet is {WATCHDOG_TIMEOUT_KINDS}"
    )


@pytest.mark.asyncio
async def test_session_activity_resets_idle_timeout() -> None:
    """Session aggregate tool/message events prove generation liveness."""
    event_store = await _store()
    session_id = "session-active"
    execution_id = "exec-session-active"
    watchdog = _watchdog(
        event_store,
        execution_id=execution_id,
        generation_idle_timeout_seconds=0.07,
        generation_no_progress_timeout_seconds=0,
    )

    async def session_work() -> str:
        await event_store.append(_session_started(session_id, execution_id))
        for _ in range(4):
            await asyncio.sleep(0.04)
            await event_store.append(_session_tool_called(session_id))
        return "done"

    assert await watchdog.watch(session_work()) == "done"


@pytest.mark.asyncio
async def test_ac_heartbeat_aggregate_resets_idle_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC heartbeats are emitted under AC aggregate IDs, not the execution ID."""
    clock = _FakeMonotonicClock()
    monkeypatch.setattr(watchdog_module, "time", SimpleNamespace(monotonic=clock))
    event_store = await _store()
    session_id = "session-heartbeat"
    execution_id = "evolve:lin-heartbeat:generation:1"
    ac_id = build_ac_runtime_scope(0, execution_context_id=execution_id).aggregate_id
    watchdog = _watchdog(
        event_store,
        execution_id=execution_id,
        generation_idle_timeout_seconds=0.07,
        generation_no_progress_timeout_seconds=0,
    )

    await watchdog.initialize_baseline()
    await event_store.append(_session_started(session_id, execution_id))
    await watchdog.poll()

    clock.advance(0.06)
    watchdog._raise_if_threshold_exceeded()

    await event_store.append(_ac_heartbeat(session_id, ac_id, 1))
    await watchdog.poll()

    assert watchdog._last_event_type == "execution.ac.heartbeat"
    assert watchdog._last_event_aggregate == f"execution/{ac_id}"

    clock.advance(0.06)
    watchdog._raise_if_threshold_exceeded()


@pytest.mark.asyncio
async def test_parent_execution_child_events_reset_idle_timeout() -> None:
    """Child execution scopes linked by parent_execution_id prove generation liveness."""
    event_store = await _store()
    session_id = "session-child-exec"
    execution_id = "evolve:lin-child:generation:1"
    watchdog = _watchdog(
        event_store,
        execution_id=execution_id,
        generation_idle_timeout_seconds=0.07,
        generation_no_progress_timeout_seconds=0,
    )

    async def child_work() -> str:
        await event_store.append(_session_started(session_id, execution_id))
        for count in range(1, 5):
            await asyncio.sleep(0.04)
            await event_store.append(
                _subagent_started(f"evolve_lin_child_generation_1_child_{count}", execution_id)
            )
        return "done"

    assert await watchdog.watch(child_work()) == "done"


@pytest.mark.asyncio
async def test_session_scoped_decomposition_events_reset_material_progress_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decomposition level progress is stored as execution events keyed by session ID."""
    clock = _FakeMonotonicClock()
    monkeypatch.setattr(watchdog_module, "time", SimpleNamespace(monotonic=clock))
    event_store = await _store()
    session_id = "session-levels"
    execution_id = "exec-levels"
    watchdog = _watchdog(
        event_store,
        execution_id=execution_id,
        generation_idle_timeout_seconds=1,
        generation_no_progress_timeout_seconds=0.07,
    )

    async def decomposition_work() -> str:
        await event_store.append(_session_started(session_id, execution_id))
        await asyncio.sleep(0.04)
        clock.advance(0.04)
        await event_store.append(
            _decomposition_level_event(
                session_id,
                "execution.decomposition.level_started",
                0,
            )
        )
        await asyncio.sleep(0.04)
        clock.advance(0.04)
        await event_store.append(
            _decomposition_level_event(
                session_id,
                "execution.decomposition.level_completed",
                0,
            )
        )
        await asyncio.sleep(0.04)
        clock.advance(0.04)
        return "done"

    assert await watchdog.watch(decomposition_work()) == "done"


@pytest.mark.asyncio
async def test_idle_generation_times_out_without_activity() -> None:
    """Silent generations are still bounded by idle timeout."""
    event_store = await _store()
    watchdog = _watchdog(
        event_store,
        generation_idle_timeout_seconds=0.05,
        generation_no_progress_timeout_seconds=0,
    )

    async def silent_work() -> str:
        await asyncio.sleep(0.2)
        return "late"

    with pytest.raises(GenerationWatchdogTimeout) as exc_info:
        await watchdog.watch(silent_work())

    assert exc_info.value.timeout_kind == "idle_timeout"


@pytest.mark.asyncio
async def test_retried_generation_does_not_count_stale_events_as_activity() -> None:
    """Baseline cursors skip events from prior attempts with the same execution ID."""
    event_store = await _store()
    lineage_id = "lin-retry"
    execution_id = "evolve:lin-retry:generation:1"
    session_id = "session-retry-old"
    ac_id = build_ac_runtime_scope(0, execution_context_id=execution_id).aggregate_id
    await event_store.append(
        BaseEvent(
            type="lineage.generation.started",
            aggregate_type="lineage",
            aggregate_id=lineage_id,
            data={"generation_number": 1},
        )
    )
    await event_store.append(_workflow_progress(execution_id, completed_count=1))
    await event_store.append(_session_started(session_id, execution_id))
    await event_store.append(_ac_heartbeat(session_id, ac_id, 1))

    watchdog = _watchdog(
        event_store,
        lineage_id=lineage_id,
        execution_id=execution_id,
        generation_idle_timeout_seconds=0.05,
        generation_no_progress_timeout_seconds=0,
    )

    async def silent_retry() -> str:
        await asyncio.sleep(0.2)
        return "late"

    with pytest.raises(GenerationWatchdogTimeout) as exc_info:
        await watchdog.watch(silent_retry())

    assert exc_info.value.timeout_kind == "idle_timeout"
    assert exc_info.value.details["activity_event_count"] == 0
    assert exc_info.value.details["material_event_count"] == 0
    assert exc_info.value.details["last_event_type"] is None


@pytest.mark.asyncio
async def test_late_discovered_session_starts_from_attempt_baseline() -> None:
    """A newly discovered session must not backfill rows from before the attempt."""
    event_store = await _store()
    execution_id = "evolve:lin-late-session:generation:1"
    session_id = "session-late-discovery"
    await event_store.append(_session_tool_called(session_id))

    watchdog = _watchdog(
        event_store,
        execution_id=execution_id,
        generation_idle_timeout_seconds=1,
        generation_no_progress_timeout_seconds=0,
    )
    await watchdog.initialize_baseline()
    await event_store.append(
        _workflow_progress(
            execution_id,
            completed_count=0,
            session_id=session_id,
        )
    )

    await watchdog.poll()

    assert watchdog._activity_event_count == 1
    assert watchdog._last_event_type == "workflow.progress.updated"
    assert session_id in watchdog._session_cursors
    assert watchdog._session_cursors[session_id] >= watchdog._attempt_start_cursor


@pytest.mark.asyncio
async def test_watchdog_decision_survives_for_resume() -> None:
    """Watchdog cancellation persists its decision on the EventStore.

    A resumer that creates a fresh watchdog on the same lineage_id reads the
    trailing ``lineage.generation.watchdog_decision`` and
    ``control.directive.emitted`` events via replay.  The evolution loop maps
    watchdog timeouts to ``StepAction.FAILED``, whose default recovery
    directive is retry.
    """
    event_store = await _store()
    lineage_id = "lin-resume-contract"
    generation_number = 1
    execution_id = "exec-resume-contract"

    # --- First attempt: watchdog times out due to no material progress ---
    first_watchdog = _watchdog(
        event_store,
        lineage_id=lineage_id,
        generation_number=generation_number,
        execution_id=execution_id,
        generation_no_progress_timeout_seconds=0.05,
        generation_idle_timeout_seconds=0,
    )

    async def busy_no_progress() -> str:
        await event_store.append(_workflow_progress(execution_id, completed_count=0))
        try:
            while True:
                await asyncio.sleep(0.02)
                await event_store.append(_workflow_progress(execution_id, completed_count=0))
        except asyncio.CancelledError:
            raise

    with pytest.raises(GenerationWatchdogTimeout) as exc_info:
        await first_watchdog.watch(busy_no_progress())

    assert exc_info.value.timeout_kind == "no_material_progress_timeout"

    # --- Drop the first watchdog; create a fresh instance on the same store ---
    del first_watchdog
    _fresh_watchdog = _watchdog(
        event_store,
        lineage_id=lineage_id,
        generation_number=generation_number,
        execution_id=execution_id,
    )
    assert not _fresh_watchdog._baseline_initialized

    # --- Replay from the resumer's perspective ---
    events = await event_store.replay("lineage", lineage_id)
    event_types = [e.type for e in events]

    assert "lineage.generation.watchdog_decision" in event_types, (
        "watchdog_decision must survive cancellation for a resumer to act on it"
    )
    assert "control.directive.emitted" in event_types, (
        "directive event must be present in the lineage replay for a resumer"
    )

    decision_event = next(e for e in events if e.type == "lineage.generation.watchdog_decision")
    directive_event = next(e for e in events if e.type == "control.directive.emitted")

    assert directive_event.data["directive"] == "retry", (
        "watchdog timeouts are StepAction.FAILED and default to retry"
    )
    assert directive_event.data["emitted_by"] == "generation.watchdog"
    assert directive_event.data["phase"] == "executing"
    assert (
        directive_event.data["idempotency_key"]
        == decision_event.data["details"]["watchdog_directive_idempotency_key"]
    )
    assert directive_event.data["extra"]["step_action"] == "failed"
    assert directive_event.data["extra"]["timeout_kind"] == "no_material_progress_timeout"
    assert directive_event.data["extra"]["is_terminal"] is False
    assert decision_event.data["generation_number"] == generation_number
    assert directive_event.data["generation_number"] == generation_number


@pytest.mark.asyncio
async def test_parent_cancellation_cancels_watched_generation() -> None:
    """Cancelling the watchdog wrapper cancels the child generation task."""
    event_store = await _store()
    watchdog = _watchdog(event_store, generation_idle_timeout_seconds=10)
    child_cancelled = asyncio.Event()

    async def long_work() -> str:
        try:
            await asyncio.sleep(10)
            return "late"
        except asyncio.CancelledError:
            child_cancelled.set()
            raise

    child_started = asyncio.Event()

    async def tracked_long_work() -> str:
        child_started.set()
        return await long_work()

    parent = asyncio.create_task(watchdog.watch(tracked_long_work()))
    await asyncio.wait_for(child_started.wait(), timeout=1)
    parent.cancel()

    with pytest.raises(asyncio.CancelledError):
        await parent
    await asyncio.wait_for(child_cancelled.wait(), timeout=1)


@pytest.mark.asyncio
async def test_no_material_progress_timeout_emits_cancellation_mode() -> None:
    """watchdog_decision event carries cancellation_mode after a no-progress timeout."""
    event_store = await _store()
    lineage_id = "lin-cancel-mode"
    execution_id = "exec-cancel-mode"
    watchdog = _watchdog(
        event_store,
        lineage_id=lineage_id,
        execution_id=execution_id,
        generation_no_progress_timeout_seconds=0.07,
    )

    async def busy_work() -> str:
        try:
            while True:
                await asyncio.sleep(0.02)
                await event_store.append(_workflow_progress(execution_id, completed_count=0))
        except asyncio.CancelledError:
            raise

    with pytest.raises(GenerationWatchdogTimeout) as exc_info:
        await watchdog.watch(busy_work())

    assert exc_info.value.timeout_kind == "no_material_progress_timeout"

    events = await event_store.replay("lineage", lineage_id)
    decision_events = [e for e in events if e.type == "lineage.generation.watchdog_decision"]
    assert decision_events, "expected at least one watchdog_decision event"
    details = decision_events[-1].data.get("details", {})
    assert details.get("cancellation_mode") == WATCHDOG_CANCELLATION_MODE
    assert details["cancellation_mode"] == "cooperative_direct_one_stage"
