"""Cross-connection ownership races for durable Synapse admission."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ouroboros.core.errors import PersistenceError
from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalCapabilities,
    SessionSignalMode,
    SessionSignalSource,
    SessionSignalState,
)
from ouroboros.core.session_signal_projection import project_session_signal
from ouroboros.events.base import BaseEvent
from ouroboros.events.session_signal import (
    create_session_signal_accepted_event,
    create_session_signal_queued_event,
    create_session_signal_rejected_event,
)
from ouroboros.orchestrator.synapse import (
    EventStoreSessionSignalTargetResolver,
    SessionSignalHub,
    SessionSignalMailbox,
    SessionSignalTarget,
)
from ouroboros.persistence.event_store import EventStore, sqlite_database_url
import ouroboros.persistence.session_signal_store as signal_store_module
from ouroboros.persistence.session_signal_store import (
    admit_if_target_active,
    append_runtime_lifecycle,
    runtime_attempt_is_active,
)


def _target(*, attempt_id: str = "attempt_race") -> SessionSignalTarget:
    return SessionSignalTarget(
        execution_id="exec_race",
        session_scope_id="scope_race",
        session_attempt_id=attempt_id,
        runtime_backend="codex_mcp",
        capabilities=SessionSignalCapabilities(after_turn_delivery=True),
    )


def _lifecycle(event_type: str, *, target: SessionSignalTarget | None = None) -> BaseEvent:
    target = target or _target()
    return BaseEvent(
        type=event_type,
        aggregate_type="execution",
        aggregate_id=target.session_scope_id,
        data={
            "execution_id": target.execution_id,
            "session_scope_id": target.session_scope_id,
            "session_attempt_id": target.session_attempt_id,
            "runtime_backend": target.runtime_backend,
        },
    )


def _signal(index: int, *, target: SessionSignalTarget | None = None) -> SessionSignal:
    target = target or _target()
    return SessionSignal(
        signal_id=f"sig_race_{index}",
        target_session_scope_id=target.session_scope_id,
        target_session_attempt_id=target.session_attempt_id,
        expected_execution_id=target.execution_id,
        mode=SessionSignalMode.AFTER_TURN,
        message=f"Apply bounded clarification {index}.",
        source=SessionSignalSource.USER,
        reason="Exercise exact-target shutdown serialization.",
        idempotency_key=f"race_{index}",
    )


def _admission_events(index: int) -> tuple[BaseEvent, BaseEvent, BaseEvent]:
    target = _target()
    signal = _signal(index, target=target)
    accepted = create_session_signal_accepted_event(
        signal,
        effective_mode=SessionSignalMode.AFTER_TURN,
        capabilities=target.capabilities,
        runtime_backend=target.runtime_backend,
    )
    queued = create_session_signal_queued_event(
        signal,
        effective_mode=SessionSignalMode.AFTER_TURN,
        runtime_backend=target.runtime_backend,
    )
    rejected = create_session_signal_rejected_event(
        signal,
        rejection_code="target_ended_before_admission",
        detail="The exact runtime attempt ended before durable queue admission.",
        effective_mode=SessionSignalMode.AFTER_TURN,
        runtime_backend=target.runtime_backend,
    )
    return accepted, queued, rejected


class _ResolvedTarget:
    def __init__(
        self, *, entered: asyncio.Event | None = None, release: asyncio.Event | None = None
    ):
        self.entered = entered
        self.release = release

    async def resolve(self, signal: SessionSignal) -> SessionSignalTarget:
        assert signal.expected_execution_id == _target().execution_id
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return _target()


@pytest.mark.asyncio
async def test_close_drains_runtime_lifecycle_transaction_and_refuses_late_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "runtime-lifecycle-close.db"
    store = EventStore(sqlite_database_url(db_path))
    await store.initialize()
    await append_runtime_lifecycle(store, _lifecycle("execution.session.started"))
    entered, release = asyncio.Event(), asyncio.Event()
    original_begin = signal_store_module._begin

    async def gated_begin(engine):
        connection = await original_begin(engine)
        entered.set()
        await release.wait()
        return connection

    monkeypatch.setattr(signal_store_module, "_begin", gated_begin)
    terminal = _lifecycle("execution.session.completed")
    write_task = asyncio.create_task(append_runtime_lifecycle(store, terminal))
    await asyncio.wait_for(entered.wait(), timeout=5)
    close_task = asyncio.create_task(store.close())
    await asyncio.sleep(0)
    assert not close_task.done()

    release.set()
    assert await asyncio.wait_for(write_task, timeout=5)
    await asyncio.wait_for(close_task, timeout=10)

    with pytest.raises(PersistenceError, match="closing"):
        await append_runtime_lifecycle(
            store,
            _lifecycle("execution.session.failed", target=_target(attempt_id="late")),
        )

    reopened = EventStore(sqlite_database_url(db_path))
    await reopened.initialize()
    try:
        persisted = await reopened.replay("execution", terminal.aggregate_id)
        assert terminal.id in {event.id for event in persisted}
        assert not await runtime_attempt_is_active(
            reopened,
            identity=("exec_race", "scope_race", "attempt_race"),
        )
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_close_drains_signal_admission_transaction_and_refuses_late_admission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "signal-admission-close.db"
    store = EventStore(sqlite_database_url(db_path))
    await store.initialize()
    await append_runtime_lifecycle(store, _lifecycle("execution.session.started"))
    entered, release = asyncio.Event(), asyncio.Event()
    original_begin = signal_store_module._begin

    async def gated_begin(engine):
        connection = await original_begin(engine)
        entered.set()
        await release.wait()
        return connection

    monkeypatch.setattr(signal_store_module, "_begin", gated_begin)
    accepted, queued, rejected = _admission_events(120)
    admission_task = asyncio.create_task(
        admit_if_target_active(
            store,
            identity=("exec_race", "scope_race", "attempt_race"),
            accepted=accepted,
            queued=queued,
            rejected=rejected,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    close_task = asyncio.create_task(store.close())
    await asyncio.sleep(0)
    assert not close_task.done()

    release.set()
    assert await asyncio.wait_for(admission_task, timeout=5) is True
    await asyncio.wait_for(close_task, timeout=10)

    late_events = _admission_events(121)
    with pytest.raises(PersistenceError, match="closing"):
        await admit_if_target_active(
            store,
            identity=("exec_race", "scope_race", "attempt_race"),
            accepted=late_events[0],
            queued=late_events[1],
            rejected=late_events[2],
        )

    reopened = EventStore(sqlite_database_url(db_path))
    await reopened.initialize()
    try:
        assert [
            event.type for event in await reopened.replay("session_signal", "sig_race_120")
        ] == [
            "control.session.signal.accepted",
            "control.session.signal.queued",
        ]
        assert await reopened.replay("session_signal", "sig_race_121") == []
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_registered_local_hub_admits_without_lifecycle_guard(tmp_path: Path) -> None:
    store = EventStore(sqlite_database_url(tmp_path / "synapse-local-owner.db"))
    await store.initialize()
    hub = SessionSignalHub()
    target = _target()
    hub.register(target)
    try:
        mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)

        projection = await mailbox.request(_signal(100))

        assert projection.state is SessionSignalState.QUEUED
        pending = hub.pop_pending(target)
        assert pending is not None
        assert pending.signal.signal_id == "sig_race_100"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_absent_guard_still_rejects_nonlocal_resolver(tmp_path: Path) -> None:
    store = EventStore(sqlite_database_url(tmp_path / "synapse-unfenced-resolver.db"))
    await store.initialize()
    try:
        mailbox = SessionSignalMailbox(store, _ResolvedTarget())  # type: ignore[arg-type]

        projection = await mailbox.request(_signal(101))

        assert projection.state is SessionSignalState.REJECTED
        events = await store.replay("session_signal", "sig_race_101")
        assert events[-1].data["rejection_code"] == "target_ended_before_admission"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_absent_guard_rejects_hub_bound_to_a_different_store(tmp_path: Path) -> None:
    runtime_store = EventStore(sqlite_database_url(tmp_path / "synapse-runtime-owner.db"))
    mailbox_store = EventStore(sqlite_database_url(tmp_path / "synapse-mailbox-owner.db"))
    await runtime_store.initialize()
    await mailbox_store.initialize()
    hub = SessionSignalHub(event_store=runtime_store)
    hub.register(_target())
    try:
        mailbox = SessionSignalMailbox(mailbox_store, hub, delivery_queue=hub)

        projection = await mailbox.request(_signal(103))

        assert projection.state is SessionSignalState.REJECTED
        assert hub.pop_pending(_target()) is None
    finally:
        await mailbox_store.close()
        await runtime_store.close()


@pytest.mark.asyncio
async def test_terminal_guard_overrides_registered_local_hub(tmp_path: Path) -> None:
    store = EventStore(sqlite_database_url(tmp_path / "synapse-stale-local-owner.db"))
    await store.initialize()
    hub = SessionSignalHub()
    hub.register(_target())
    try:
        assert await append_runtime_lifecycle(store, _lifecycle("execution.session.started"))
        assert await append_runtime_lifecycle(store, _lifecycle("execution.session.completed"))
        mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)

        projection = await mailbox.request(_signal(102))

        assert projection.state is SessionSignalState.REJECTED
        assert hub.pop_pending(_target()) is None
        events = await store.replay("session_signal", "sig_race_102")
        assert events[-1].data["rejection_code"] == "target_ended_before_admission"
    finally:
        await store.close()


@pytest.mark.parametrize(
    "terminal_type",
    [
        "execution.session.completed",
        "execution.session.failed",
        "execution.session.cancelled",
    ],
)
@pytest.mark.parametrize(
    "late_active_type",
    [
        "execution.session.started",
        "execution.session.resumed",
        "execution.session.recovered",
    ],
)
@pytest.mark.parametrize("terminal_first", [True, False])
@pytest.mark.asyncio
async def test_terminal_guard_absorbs_late_active_lifecycle_across_connections(
    tmp_path: Path,
    terminal_type: str,
    late_active_type: str,
    terminal_first: bool,
) -> None:
    url = sqlite_database_url(tmp_path / "synapse-absorbing-terminal.db")
    starter, terminator, late_worker, mcp = (
        EventStore(url),
        EventStore(url),
        EventStore(url),
        EventStore(url),
    )
    for store in (starter, terminator, late_worker, mcp):
        await store.initialize()
    identity = ("exec_race", "scope_race", "attempt_race")
    try:
        assert await append_runtime_lifecycle(starter, _lifecycle("execution.session.started"))
        assert await runtime_attempt_is_active(mcp, identity=identity)

        if terminal_first:
            assert await append_runtime_lifecycle(terminator, _lifecycle(terminal_type))
            assert await append_runtime_lifecycle(
                late_worker,
                _lifecycle(late_active_type),
            )
        else:
            assert await append_runtime_lifecycle(
                late_worker,
                _lifecycle(late_active_type),
            )
            assert await append_runtime_lifecycle(terminator, _lifecycle(terminal_type))

        assert not await runtime_attempt_is_active(mcp, identity=identity)
        resolver = EventStoreSessionSignalTargetResolver(
            mcp,
            capabilities_by_backend={
                "codex_mcp": SessionSignalCapabilities(after_turn_delivery=True)
            },
        )
        assert await resolver.list_targets(execution_id="exec_race") == ()

        signal = _signal(110)
        projection = await SessionSignalMailbox(mcp, resolver).request(signal)
        assert projection.state is SessionSignalState.REJECTED
        events = await mcp.replay("session_signal", signal.signal_id)
        assert [event.type for event in events] == [
            "control.session.signal.requested",
            "control.session.signal.rejected",
        ]
        assert events[-1].data["rejection_code"] == "target_terminal"

        replacement = _target(attempt_id="attempt_race_2")
        replacement_identity = (
            replacement.execution_id,
            replacement.session_scope_id,
            replacement.session_attempt_id,
        )
        assert await append_runtime_lifecycle(
            late_worker,
            _lifecycle("execution.session.started", target=replacement),
        )
        assert await runtime_attempt_is_active(mcp, identity=replacement_identity)
        assert await resolver.list_targets(execution_id=replacement.execution_id) == (replacement,)
    finally:
        for store in (mcp, late_worker, terminator, starter):
            await store.close()


@pytest.mark.asyncio
async def test_terminal_drains_signal_queued_by_separate_store(tmp_path: Path) -> None:
    url = sqlite_database_url(tmp_path / "synapse-race.db")
    worker, mcp = EventStore(url), EventStore(url)
    await worker.initialize()
    await mcp.initialize()
    try:
        assert await append_runtime_lifecycle(worker, _lifecycle("execution.session.started"))
        mailbox = SessionSignalMailbox(mcp, _ResolvedTarget())  # type: ignore[arg-type]
        assert (await mailbox.request(_signal(1))).state is SessionSignalState.QUEUED

        assert await append_runtime_lifecycle(worker, _lifecycle("execution.session.completed"))

        projection = project_session_signal(await mcp.replay("session_signal", "sig_race_1"))
        assert projection.state is SessionSignalState.REJECTED
    finally:
        await mcp.close()
        await worker.close()


@pytest.mark.asyncio
async def test_resolved_before_terminal_is_rejected_at_admission(tmp_path: Path) -> None:
    url = sqlite_database_url(tmp_path / "synapse-resolve-race.db")
    worker, mcp = EventStore(url), EventStore(url)
    await worker.initialize()
    await mcp.initialize()
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        assert await append_runtime_lifecycle(worker, _lifecycle("execution.session.started"))
        mailbox = SessionSignalMailbox(
            mcp,
            _ResolvedTarget(entered=entered, release=release),  # type: ignore[arg-type]
        )
        request = asyncio.create_task(mailbox.request(_signal(2)))
        await entered.wait()
        assert await append_runtime_lifecycle(worker, _lifecycle("execution.session.failed"))
        release.set()

        projection = await request
        assert projection.state is SessionSignalState.REJECTED
        events = await mcp.replay("session_signal", "sig_race_2")
        assert [event.type for event in events] == [
            "control.session.signal.requested",
            "control.session.signal.rejected",
        ]
        assert events[-1].data["rejection_code"] == "target_ended_before_admission"
    finally:
        await mcp.close()
        await worker.close()


@pytest.mark.asyncio
async def test_concurrent_cross_connection_shutdown_leaves_no_queued_projection(
    tmp_path: Path,
) -> None:
    url = sqlite_database_url(tmp_path / "synapse-concurrent-race.db")
    worker, mcp = EventStore(url), EventStore(url)
    await worker.initialize()
    await mcp.initialize()
    try:
        assert await append_runtime_lifecycle(worker, _lifecycle("execution.session.started"))
        mailboxes = [SessionSignalMailbox(mcp, _ResolvedTarget()) for _ in range(12)]
        requests = [mailbox.request(_signal(index)) for index, mailbox in enumerate(mailboxes)]
        await asyncio.gather(
            *requests,
            append_runtime_lifecycle(worker, _lifecycle("execution.session.completed")),
        )

        projections = [
            project_session_signal(await mcp.replay("session_signal", f"sig_race_{index}"))
            for index in range(12)
        ]
        assert all(projection.is_terminal for projection in projections)
        assert SessionSignalState.QUEUED not in {projection.state for projection in projections}
    finally:
        await mcp.close()
        await worker.close()
