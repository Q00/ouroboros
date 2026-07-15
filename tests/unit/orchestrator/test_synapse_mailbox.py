"""Mailbox and exact-target tests for the first Synapse vertical slice."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalCapabilities,
    SessionSignalContractEffect,
    SessionSignalMode,
    SessionSignalSource,
    SessionSignalState,
)
from ouroboros.core.session_signal_projection import project_session_signal
from ouroboros.events.base import BaseEvent
from ouroboros.events.session_signal import create_session_signal_delivery_started_event
from ouroboros.orchestrator.synapse import (
    EventStoreSessionSignalTargetResolver,
    SessionSignalHub,
    SessionSignalMailbox,
    SessionSignalTarget,
    SessionSignalTargetError,
)


@dataclass
class _Store:
    events: list[BaseEvent] = field(default_factory=list)
    append_batch_calls: int = 0
    latest_execution_job_status: str | None = None

    async def get_latest_execution_job_status(self, execution_id: str) -> str | None:
        assert execution_id
        return self.latest_execution_job_status

    async def query_execution_related_events(
        self,
        execution_id: str,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        matching = [
            event
            for event in reversed(self.events)
            if event.aggregate_type == "execution"
            and (
                event.aggregate_id == execution_id or event.data.get("execution_id") == execution_id
            )
            and (event_type is None or event.type == event_type)
        ]
        if limit is None:
            return matching[offset:]
        return matching[offset : offset + limit]

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        return [
            event
            for event in self.events
            if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
        ]

    async def query_session_signal_events(
        self,
        *,
        execution_id: str,
        session_scope_id: str,
        session_attempt_id: str,
    ) -> list[BaseEvent]:
        return [
            event
            for event in self.events
            if event.aggregate_type == "session_signal"
            and event.data.get("expected_execution_id") == execution_id
            and event.data.get("target_session_scope_id") == session_scope_id
            and event.data.get("target_session_attempt_id") == session_attempt_id
        ]

    async def append(self, event: BaseEvent) -> None:
        self.events.append(event)

    async def append_batch(self, events: list[BaseEvent]) -> None:
        self.append_batch_calls += 1
        self.events.extend(events)


def _signal(**overrides: object) -> SessionSignal:
    values: dict[str, object] = {
        "signal_id": "sig_1",
        "target_session_scope_id": "scope_1",
        "target_session_attempt_id": "scope_1_attempt_1",
        "expected_execution_id": "exec_1",
        "mode": SessionSignalMode.REDIRECT,
        "fallback_mode": SessionSignalMode.AFTER_TURN,
        "message": "Apply the clarified local intent.",
        "source": SessionSignalSource.USER,
        "reason": "User clarification.",
        "idempotency_key": "turn_7_scope_1",
    }
    values.update(overrides)
    return SessionSignal(**values)  # type: ignore[arg-type]


def _runtime_event(
    event_type: str = "execution.session.started",
    *,
    scope: str = "scope_1",
    attempt: str = "scope_1_attempt_1",
    execution_id: str = "exec_1",
    backend: str | None = "codex_mcp",
) -> BaseEvent:
    return BaseEvent(
        type=event_type,
        aggregate_type="execution",
        aggregate_id=scope,
        data={
            "session_scope_id": scope,
            "session_attempt_id": attempt,
            "execution_id": execution_id,
            "runtime_backend": backend,
        },
    )


@pytest.mark.asyncio
async def test_target_resolver_requires_exact_active_attempt() -> None:
    store = _Store(events=[_runtime_event()])
    resolver = EventStoreSessionSignalTargetResolver(
        event_store=store,  # type: ignore[arg-type]
        capabilities_by_backend={"codex_mcp": SessionSignalCapabilities(after_turn_delivery=True)},
    )

    target = await resolver.resolve(_signal())

    assert target.execution_id == "exec_1"
    assert target.session_scope_id == "scope_1"
    assert target.session_attempt_id == "scope_1_attempt_1"
    assert target.runtime_backend == "codex_mcp"
    assert target.capabilities.after_turn_delivery is True


@pytest.mark.asyncio
async def test_target_resolver_lists_persisted_active_attempts_with_semantic_metadata() -> None:
    checkout = _runtime_event(
        scope="checkout_scope",
        attempt="checkout_scope_attempt_1",
        backend="codex_cli",
    )
    checkout.data.update(
        {
            "ac_id": "checkout_ac",
            "acceptance_criterion": "Create the checkout confirmation copy",
            "ac_index": 0,
            "node_id": "node_checkout",
            "display_path": "1",
            "depth": 0,
        }
    )
    database = _runtime_event(
        scope="database_scope",
        attempt="database_scope_attempt_1",
        backend="codex_cli",
    )
    database.data.update(
        {
            "ac_id": "database_ac",
            "acceptance_criterion": "Write the database migration note",
            "ac_index": 1,
            "node_id": "node_database",
            "display_path": "2",
            "depth": 0,
        }
    )
    terminal = _runtime_event(
        scope="finished_scope",
        attempt="finished_scope_attempt_1",
        backend="codex_cli",
    )
    terminal_done = _runtime_event(
        "execution.session.completed",
        scope="finished_scope",
        attempt="finished_scope_attempt_1",
        backend="codex_cli",
    )
    store = _Store(events=[checkout, database, terminal, terminal_done])
    resolver = EventStoreSessionSignalTargetResolver(
        event_store=store,  # type: ignore[arg-type]
        capabilities_by_backend={
            "codex_cli": SessionSignalCapabilities(
                inform_delivery=True,
                after_turn_delivery=True,
            )
        },
    )

    targets = await resolver.list_targets(execution_id="exec_1")

    assert [target.ac_id for target in targets] == ["checkout_ac", "database_ac"]
    assert targets[0].ac_content == "Create the checkout confirmation copy"
    assert targets[0].display_label == "AC 1"
    assert targets[0].display_path == "1"
    assert targets[0].capabilities.inform_delivery is True
    assert targets[1].ac_content == "Write the database migration note"


@pytest.mark.asyncio
async def test_target_resolver_rejects_replaced_attempt() -> None:
    store = _Store(events=[_runtime_event(attempt="scope_1_attempt_2")])
    resolver = EventStoreSessionSignalTargetResolver(event_store=store)  # type: ignore[arg-type]

    with pytest.raises(SessionSignalTargetError) as exc_info:
        await resolver.resolve(_signal())

    assert exc_info.value.code == "stale_attempt"


@pytest.mark.asyncio
async def test_target_resolver_rejects_terminal_attempt() -> None:
    store = _Store(
        events=[
            _runtime_event(),
            _runtime_event("execution.session.completed"),
        ]
    )
    resolver = EventStoreSessionSignalTargetResolver(event_store=store)  # type: ignore[arg-type]

    with pytest.raises(SessionSignalTargetError) as exc_info:
        await resolver.resolve(_signal())

    assert exc_info.value.code == "target_terminal"


@pytest.mark.asyncio
async def test_target_resolver_hides_attempts_after_background_job_interruption() -> None:
    store = _Store(
        events=[_runtime_event()],
        latest_execution_job_status="interrupted",
    )
    resolver = EventStoreSessionSignalTargetResolver(event_store=store)  # type: ignore[arg-type]

    assert await resolver.list_targets(execution_id="exec_1") == ()
    with pytest.raises(SessionSignalTargetError) as exc_info:
        await resolver.resolve(_signal())

    assert exc_info.value.code == "target_terminal"


@pytest.mark.asyncio
async def test_mailbox_queues_explicit_redirect_fallback() -> None:
    store = _Store(events=[_runtime_event()])
    resolver = EventStoreSessionSignalTargetResolver(
        event_store=store,  # type: ignore[arg-type]
        capabilities_by_backend={"codex_mcp": SessionSignalCapabilities(after_turn_delivery=True)},
    )
    mailbox = SessionSignalMailbox(store, resolver)  # type: ignore[arg-type]

    projection = await mailbox.request(_signal())

    assert projection.state is SessionSignalState.QUEUED
    assert projection.effective_mode is SessionSignalMode.AFTER_TURN
    assert store.append_batch_calls == 1
    assert [event.type for event in store.events[-3:]] == [
        "control.session.signal.requested",
        "control.session.signal.accepted",
        "control.session.signal.queued",
    ]


@pytest.mark.asyncio
async def test_mailbox_rejects_unsupported_redirect() -> None:
    store = _Store(events=[_runtime_event()])
    resolver = EventStoreSessionSignalTargetResolver(event_store=store)  # type: ignore[arg-type]
    mailbox = SessionSignalMailbox(store, resolver)  # type: ignore[arg-type]

    projection = await mailbox.request(replace(_signal(), fallback_mode=None))

    assert projection.state is SessionSignalState.REJECTED
    assert projection.effective_mode is None
    assert store.events[-1].data["rejection_code"] == "capability_unsupported"


@pytest.mark.asyncio
async def test_mailbox_rejects_expired_before_resolution() -> None:
    store = _Store(events=[_runtime_event()])
    resolver = EventStoreSessionSignalTargetResolver(event_store=store)  # type: ignore[arg-type]
    mailbox = SessionSignalMailbox(store, resolver)  # type: ignore[arg-type]

    projection = await mailbox.request(_signal(expires_at=datetime.now(UTC) - timedelta(seconds=1)))

    assert projection.state is SessionSignalState.REJECTED
    assert store.events[-1].data["rejection_code"] == "expired"


@pytest.mark.asyncio
async def test_mailbox_rejects_spec_change_and_contract_generation_mismatch() -> None:
    store = _Store()
    hub = SessionSignalHub()
    hub.register(
        SessionSignalTarget(
            execution_id="exec_1",
            session_scope_id="scope_1",
            session_attempt_id="scope_1_attempt_1",
            runtime_backend="codex_cli",
            capabilities=SessionSignalCapabilities(after_turn_delivery=True),
            contract_version=2,
        )
    )
    mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)  # type: ignore[arg-type]

    spec_change = await mailbox.request(
        _signal(
            signal_id="sig_spec_change",
            idempotency_key="spec_change",
            contract_effect=SessionSignalContractEffect.SPECIFICATION_CHANGE,
            user_approval_event_id="approval_1",
        )
    )
    mismatch = await mailbox.request(
        _signal(
            signal_id="sig_contract_mismatch",
            idempotency_key="contract_mismatch",
            expected_contract_version=1,
        )
    )

    assert spec_change.state is SessionSignalState.REJECTED
    spec_events = await store.replay("session_signal", "sig_spec_change")
    assert spec_events[-1].data["rejection_code"] == (
        "specification_change_requires_shared_successor"
    )
    assert mismatch.state is SessionSignalState.REJECTED
    mismatch_events = await store.replay("session_signal", "sig_contract_mismatch")
    assert mismatch_events[-1].data["rejection_code"] == "contract_version_mismatch"


@pytest.mark.asyncio
async def test_repeated_request_is_durably_idempotent() -> None:
    store = _Store(events=[_runtime_event()])
    resolver = EventStoreSessionSignalTargetResolver(
        event_store=store,  # type: ignore[arg-type]
        capabilities_by_backend={"codex_mcp": SessionSignalCapabilities(checkpoint_redirect=True)},
    )
    mailbox = SessionSignalMailbox(store, resolver)  # type: ignore[arg-type]
    signal = _signal()

    first, second = await asyncio.gather(mailbox.request(signal), mailbox.request(signal))

    assert first.state is SessionSignalState.QUEUED
    assert second.event_ids == first.event_ids
    assert len([event for event in store.events if event.aggregate_type == "session_signal"]) == 3


@pytest.mark.asyncio
async def test_signal_id_cannot_be_reused_for_different_content() -> None:
    store = _Store(events=[_runtime_event()])
    resolver = EventStoreSessionSignalTargetResolver(
        event_store=store,  # type: ignore[arg-type]
        capabilities_by_backend={"codex_mcp": SessionSignalCapabilities(checkpoint_redirect=True)},
    )
    mailbox = SessionSignalMailbox(store, resolver)  # type: ignore[arg-type]
    await mailbox.request(_signal())

    with pytest.raises(ValueError, match="different immutable request"):
        await mailbox.request(_signal(message="A different but still bounded intent."))


@pytest.mark.asyncio
async def test_live_hub_resolves_queues_and_unregisters_exact_attempt() -> None:
    hub = SessionSignalHub()
    target = SessionSignalTarget(
        execution_id="exec_1",
        session_scope_id="scope_1",
        session_attempt_id="scope_1_attempt_1",
        runtime_backend="codex_mcp",
        capabilities=SessionSignalCapabilities(after_turn_delivery=True),
    )
    hub.register(target)
    signal = _signal(mode=SessionSignalMode.AFTER_TURN, fallback_mode=None)

    assert await hub.resolve(signal) == target
    await hub.enqueue(signal, effective_mode=SessionSignalMode.AFTER_TURN)
    queued = hub.pop_pending(target)

    assert queued is not None
    assert queued.signal == signal
    assert queued.effective_mode is SessionSignalMode.AFTER_TURN
    assert hub.pop_pending(target) is None
    assert hub.unregister(target) == ()

    with pytest.raises(SessionSignalTargetError) as exc_info:
        await hub.resolve(signal)
    assert exc_info.value.code == "target_not_active"


def test_live_hub_lists_discovery_metadata_for_one_execution() -> None:
    hub = SessionSignalHub()
    target = SessionSignalTarget(
        execution_id="exec_1",
        session_scope_id="scope_1",
        session_attempt_id="scope_1_attempt_1",
        runtime_backend="codex_cli",
        capabilities=SessionSignalCapabilities(after_turn_delivery=True),
        ac_id="scope_1",
        ac_content="Implement the login confirmation UX",
        display_label="AC 1",
        ac_index=0,
        depth=0,
    )
    hub.register(target)
    hub.register(
        SessionSignalTarget(
            execution_id="exec_other",
            session_scope_id="other_scope",
            session_attempt_id="other_scope_attempt_1",
            runtime_backend="codex_cli",
        )
    )

    assert hub.list_targets(execution_id="exec_1") == (target,)
    assert target.to_discovery_data()["ac_number"] == 1
    assert target.to_discovery_data()["ac_content"] == ("Implement the login confirmation UX")


@pytest.mark.asyncio
async def test_mailbox_hands_queued_signal_to_live_hub() -> None:
    store = _Store()
    hub = SessionSignalHub()
    target = SessionSignalTarget(
        execution_id="exec_1",
        session_scope_id="scope_1",
        session_attempt_id="scope_1_attempt_1",
        runtime_backend="codex_mcp",
        capabilities=SessionSignalCapabilities(after_turn_delivery=True),
    )
    hub.register(target)
    mailbox = SessionSignalMailbox(
        store,  # type: ignore[arg-type]
        hub,
        delivery_queue=hub,
    )

    projection = await mailbox.request(
        _signal(mode=SessionSignalMode.AFTER_TURN, fallback_mode=None)
    )

    assert projection.state is SessionSignalState.QUEUED
    assert hub.pop_pending(target) is not None


@pytest.mark.asyncio
async def test_user_priority_supersedes_lower_pending_and_blocks_later_worker() -> None:
    store = _Store()
    hub = SessionSignalHub()
    target = SessionSignalTarget(
        execution_id="exec_1",
        session_scope_id="scope_1",
        session_attempt_id="scope_1_attempt_1",
        runtime_backend="codex_cli",
        capabilities=SessionSignalCapabilities(after_turn_delivery=True),
    )
    hub.register(target)
    mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)  # type: ignore[arg-type]
    conductor = _signal(
        signal_id="sig_conductor",
        idempotency_key="conductor",
        mode=SessionSignalMode.AFTER_TURN,
        fallback_mode=None,
        source=SessionSignalSource.CONDUCTOR,
    )
    user = _signal(
        signal_id="sig_user",
        idempotency_key="user",
        mode=SessionSignalMode.AFTER_TURN,
        fallback_mode=None,
        source=SessionSignalSource.USER,
    )
    worker = _signal(
        signal_id="sig_worker",
        idempotency_key="worker",
        mode=SessionSignalMode.AFTER_TURN,
        fallback_mode=None,
        source=SessionSignalSource.WORKER,
    )

    assert (await mailbox.request(conductor)).state is SessionSignalState.QUEUED
    assert (await mailbox.request(user)).state is SessionSignalState.QUEUED
    conductor_projection = project_session_signal(
        await store.replay("session_signal", conductor.signal_id)
    )
    worker_projection = await mailbox.request(worker)

    assert conductor_projection.state is SessionSignalState.REJECTED
    conductor_events = await store.replay("session_signal", conductor.signal_id)
    assert conductor_events[-1].data["rejection_code"] == ("superseded_by_higher_priority_signal")
    assert worker_projection.state is SessionSignalState.REJECTED
    worker_events = await store.replay("session_signal", worker.signal_id)
    assert worker_events[-1].data["rejection_code"] == "higher_priority_signal_pending"
    pending = hub.pop_pending(target)
    assert pending is not None and pending.signal.signal_id == user.signal_id


@pytest.mark.asyncio
async def test_register_replaying_restores_queued_and_quarantines_claimed_delivery() -> None:
    store = _Store()
    target = SessionSignalTarget(
        execution_id="exec_1",
        session_scope_id="scope_1",
        session_attempt_id="scope_1_attempt_1",
        runtime_backend="codex_cli",
        capabilities=SessionSignalCapabilities(after_turn_delivery=True),
    )
    first_hub = SessionSignalHub()
    first_hub.register(target)
    mailbox = SessionSignalMailbox(store, first_hub, delivery_queue=first_hub)  # type: ignore[arg-type]
    queued_signal = _signal(
        signal_id="sig_replay_queued",
        idempotency_key="replay_queued",
        mode=SessionSignalMode.AFTER_TURN,
        fallback_mode=None,
    )
    claimed_signal = _signal(
        signal_id="sig_replay_claimed",
        idempotency_key="replay_claimed",
        mode=SessionSignalMode.AFTER_TURN,
        fallback_mode=None,
    )
    await mailbox.request(queued_signal)
    await mailbox.request(claimed_signal)
    claimed = first_hub.pop_pending(target)
    assert claimed is not None and claimed.signal.signal_id == queued_signal.signal_id
    await store.append(
        create_session_signal_delivery_started_event(
            claimed.signal,
            effective_mode=claimed.effective_mode,
            runtime_backend=target.runtime_backend,
        )
    )

    replay_hub = SessionSignalHub(event_store=store)  # type: ignore[arg-type]
    await replay_hub.register_replaying(target)

    replayed = replay_hub.pop_pending(target)
    assert replayed is not None and replayed.signal.signal_id == claimed_signal.signal_id
    claimed_projection = project_session_signal(
        await store.replay("session_signal", queued_signal.signal_id)
    )
    assert claimed_projection.state is SessionSignalState.DELIVERY_UNCERTAIN
    assert store.events[-1].data["automatic_retry_allowed"] is False


@pytest.mark.asyncio
async def test_live_worker_refreshes_signal_queued_by_separate_mcp_process() -> None:
    store = _Store(events=[_runtime_event(backend="codex_cli")])
    target = SessionSignalTarget(
        execution_id="exec_1",
        session_scope_id="scope_1",
        session_attempt_id="scope_1_attempt_1",
        runtime_backend="codex_cli",
        capabilities=SessionSignalCapabilities(
            inform_delivery=True,
            after_turn_delivery=True,
        ),
    )
    worker_hub = SessionSignalHub(event_store=store)  # type: ignore[arg-type]
    await worker_hub.register_replaying(target)
    remote_resolver = EventStoreSessionSignalTargetResolver(
        event_store=store,  # type: ignore[arg-type]
        capabilities_by_backend={
            "codex_cli": SessionSignalCapabilities(
                inform_delivery=True,
                after_turn_delivery=True,
            )
        },
    )
    remote_mailbox = SessionSignalMailbox(
        event_store=store,  # type: ignore[arg-type]
        target_resolver=remote_resolver,
    )
    signal = _signal(
        mode=SessionSignalMode.INFORM,
        fallback_mode=None,
    )

    projection = await remote_mailbox.request(signal)

    assert projection.state is SessionSignalState.QUEUED
    assert worker_hub.pop_pending(target) is None

    await worker_hub.refresh_pending(target)
    queued = worker_hub.pop_pending(target)

    assert queued is not None
    assert queued.signal.signal_id == signal.signal_id
    assert queued.effective_mode is SessionSignalMode.INFORM
