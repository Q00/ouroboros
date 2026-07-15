"""Durable, capability-aware mailbox for Ouroboros Synapse.

The mailbox validates an exact live runtime attempt, persists every transition,
and hands queued signals to the owning runtime at a capability-proven boundary.
No provider is credited with redirect or replacement support until it registers
tested capabilities and produces the required acknowledgement.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalCapabilities,
    SessionSignalCapabilityError,
    SessionSignalContractEffect,
    SessionSignalMode,
    SessionSignalState,
    resolve_session_signal_mode,
)
from ouroboros.core.session_signal_projection import (
    SessionSignalProjection,
    can_supersede_session_signal,
    project_session_signal,
)
from ouroboros.events.session_signal import (
    create_session_signal_accepted_event,
    create_session_signal_delivery_uncertain_event,
    create_session_signal_queued_event,
    create_session_signal_rejected_event,
    create_session_signal_requested_event,
)
from ouroboros.persistence.event_store import EventStore

_ACTIVE_RUNTIME_EVENTS = frozenset(
    {
        "execution.session.started",
        "execution.session.resumed",
        "execution.session.recovered",
    }
)
_TERMINAL_RUNTIME_EVENTS = frozenset(
    {
        "execution.session.completed",
        "execution.session.failed",
    }
)
_RUNTIME_LIFECYCLE_EVENTS = _ACTIVE_RUNTIME_EVENTS | _TERMINAL_RUNTIME_EVENTS
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})


def render_after_turn_signal_prompt(signal: SessionSignal) -> str:
    """Render bounded additive intent for a resumed worker turn."""
    return (
        "[Ouroboros Synapse: additive intent]\n"
        "Apply this clarification without weakening or replacing the approved "
        "goal, acceptance criteria, constraints, or non-goals. Re-check any "
        "affected work and report what changed.\n\n"
        f"Intent:\n{signal.message}\n\n"
        f"Reason:\n{signal.reason}"
    )


def render_inform_signal_prompt(signal: SessionSignal) -> str:
    """Render a no-tools bounded information/reply turn for the same AC session."""
    return (
        "[Ouroboros Synapse: information request]\n"
        "Treat this as read-only context. Do not use tools, modify artifacts, or "
        "change the approved goal, acceptance criteria, constraints, or non-goals. "
        "Reply briefly with the information the main conductor should relay.\n\n"
        f"Message:\n{signal.message}\n\n"
        f"Reason:\n{signal.reason}"
    )


class SessionSignalTargetError(ValueError):
    """Fail-closed target resolution error with a stable rejection code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SessionSignalTarget:
    """Resolved logical runtime target without caller-controlled native IDs."""

    execution_id: str
    session_scope_id: str
    session_attempt_id: str
    runtime_backend: str
    capabilities: SessionSignalCapabilities = field(default_factory=SessionSignalCapabilities)
    contract_version: int = 1
    orchestrator_session_id: str | None = None
    ac_id: str | None = None
    ac_content: str | None = None
    display_label: str | None = None
    ac_index: int | None = None
    parent_ac_index: int | None = None
    sub_ac_index: int | None = None
    node_id: str | None = None
    display_path: str | None = None
    depth: int | None = None

    def to_discovery_data(self) -> dict[str, object]:
        """Return model-facing metadata without exposing provider-native IDs."""
        return {
            "execution_id": self.execution_id,
            "target_session_scope_id": self.session_scope_id,
            "target_session_attempt_id": self.session_attempt_id,
            "runtime_backend": self.runtime_backend,
            "contract_version": self.contract_version,
            "ac_id": self.ac_id,
            "ac_content": self.ac_content,
            "display_label": self.display_label,
            "ac_index": self.ac_index,
            "ac_number": self.ac_index + 1 if self.ac_index is not None else None,
            "parent_ac_index": self.parent_ac_index,
            "parent_ac_number": (
                self.parent_ac_index + 1 if self.parent_ac_index is not None else None
            ),
            "sub_ac_index": self.sub_ac_index,
            "sub_ac_number": (self.sub_ac_index + 1 if self.sub_ac_index is not None else None),
            "node_id": self.node_id,
            "display_path": self.display_path,
            "depth": self.depth,
            "capabilities": self.capabilities.to_event_data(),
        }


class SessionSignalTargetResolver(Protocol):
    """Resolve and validate one exact active runtime attempt."""

    async def resolve(self, signal: SessionSignal) -> SessionSignalTarget: ...


@dataclass(frozen=True, slots=True)
class QueuedSessionSignal:
    """One capability-resolved signal waiting at an active runtime boundary."""

    signal: SessionSignal
    effective_mode: SessionSignalMode


class SessionSignalQueue(Protocol):
    """Queue a validated signal into the owning live runtime."""

    async def enqueue(
        self,
        signal: SessionSignal,
        *,
        effective_mode: SessionSignalMode,
    ) -> tuple[QueuedSessionSignal, ...]: ...


@dataclass(slots=True)
class _LiveSessionSignalTarget:
    target: SessionSignalTarget
    pending: deque[QueuedSessionSignal] = field(default_factory=deque)


@dataclass(slots=True)
class SessionSignalHub:
    """In-process exact-attempt registry and queue for active AC runtimes."""

    event_store: EventStore | None = None

    _targets: dict[tuple[str, str, str], _LiveSessionSignalTarget] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    @staticmethod
    def _key(
        execution_id: str,
        session_scope_id: str,
        session_attempt_id: str,
    ) -> tuple[str, str, str]:
        return execution_id, session_scope_id, session_attempt_id

    def register(self, target: SessionSignalTarget) -> None:
        """Register one exact active attempt; duplicate ownership fails closed."""
        key = self._key(
            target.execution_id,
            target.session_scope_id,
            target.session_attempt_id,
        )
        if key in self._targets:
            raise RuntimeError("SessionSignal target attempt is already registered")
        self._targets[key] = _LiveSessionSignalTarget(target=target)

    async def register_replaying(self, target: SessionSignalTarget) -> None:
        """Register an active target and repair/replay durable pending delivery state."""
        self.register(target)
        if self.event_store is None:
            return
        try:
            await self._refresh_pending_from_store(target)
        except Exception:
            self.unregister(target)
            raise

    async def refresh_pending(self, target: SessionSignalTarget) -> None:
        """Import signals queued by another MCP process for one live target.

        Codex and other stdio hosts create a fresh MCP server for a resumed main
        conversation turn. That process can persist a signal, but it cannot mutate
        the worker process's in-memory queue. The owning worker calls this method at
        its next delivery boundary so the event store is the cross-process relay.
        """
        key = self._key(
            target.execution_id,
            target.session_scope_id,
            target.session_attempt_id,
        )
        if key not in self._targets:
            raise SessionSignalTargetError(
                "target_not_active",
                "The exact session attempt is not registered as active.",
            )
        if self.event_store is None:
            return
        await self._refresh_pending_from_store(target)

    async def _refresh_pending_from_store(self, target: SessionSignalTarget) -> None:
        assert self.event_store is not None
        key = self._key(
            target.execution_id,
            target.session_scope_id,
            target.session_attempt_id,
        )
        live = self._targets.get(key)
        if live is None:
            raise SessionSignalTargetError(
                "target_not_active",
                "The exact session attempt is not registered as active.",
            )

        events = await self.event_store.query_session_signal_events(
            execution_id=target.execution_id,
            session_scope_id=target.session_scope_id,
            session_attempt_id=target.session_attempt_id,
        )
        grouped: dict[str, list[object]] = {}
        for event in events:
            grouped.setdefault(event.aggregate_id, []).append(event)

        replayable_ids: set[str] = set()
        pending: list[tuple[object, QueuedSessionSignal]] = []
        for signal_events in grouped.values():
            typed_events = list(signal_events)
            projection = project_session_signal(typed_events)  # type: ignore[arg-type]
            requested = next(
                (
                    event
                    for event in typed_events
                    if event.type == "control.session.signal.requested"
                ),
                None,
            )
            if requested is None:
                continue
            signal = SessionSignal.from_event_data(requested.data)
            if projection.state is SessionSignalState.DELIVERING:
                await self.event_store.append(
                    create_session_signal_delivery_uncertain_event(
                        signal,
                        effective_mode=projection.effective_mode or SessionSignalMode.AFTER_TURN,
                        detail=(
                            "The previous process ended after claiming delivery but "
                            "before recording provider acknowledgement."
                        ),
                        runtime_backend=target.runtime_backend,
                        orchestrator_session_id=target.orchestrator_session_id,
                    )
                )
                continue
            if projection.state is not SessionSignalState.QUEUED:
                continue
            queued_event = next(
                event for event in typed_events if event.type == "control.session.signal.queued"
            )
            if signal.is_expired():
                await self.event_store.append(
                    create_session_signal_rejected_event(
                        signal,
                        rejection_code="expired_before_replay",
                        detail="The SessionSignal expired before replay into the live queue.",
                        effective_mode=projection.effective_mode,
                        runtime_backend=target.runtime_backend,
                        orchestrator_session_id=target.orchestrator_session_id,
                    )
                )
                continue
            if projection.effective_mode is None:
                raise ValueError("Queued SessionSignal replay requires effective_mode")
            replayable_ids.add(signal.signal_id)
            pending.append(
                (
                    queued_event.timestamp,
                    QueuedSessionSignal(signal, projection.effective_mode),
                )
            )

        # A different MCP process may have rejected or completed a signal after
        # this worker first loaded it. Reconcile before importing new rows.
        live.pending = deque(
            queued for queued in live.pending if queued.signal.signal_id in replayable_ids
        )

        for _timestamp, queued in sorted(pending, key=lambda item: item[0]):
            try:
                superseded = self._enqueue_live(queued)
            except SessionSignalTargetError as exc:
                await self.event_store.append(
                    create_session_signal_rejected_event(
                        queued.signal,
                        rejection_code=exc.code,
                        detail=str(exc),
                        effective_mode=queued.effective_mode,
                        runtime_backend=target.runtime_backend,
                        orchestrator_session_id=target.orchestrator_session_id,
                    )
                )
                continue
            for displaced in superseded:
                await self.event_store.append(
                    create_session_signal_rejected_event(
                        displaced.signal,
                        rejection_code="superseded_by_higher_priority_signal",
                        detail=(
                            "A higher-authority SessionSignal superseded this pending "
                            "message before delivery."
                        ),
                        effective_mode=displaced.effective_mode,
                        runtime_backend=target.runtime_backend,
                        orchestrator_session_id=target.orchestrator_session_id,
                    )
                )

    def unregister(self, target: SessionSignalTarget) -> tuple[QueuedSessionSignal, ...]:
        """Remove exact ownership and return signals that were never consumed."""
        key = self._key(
            target.execution_id,
            target.session_scope_id,
            target.session_attempt_id,
        )
        live = self._targets.pop(key, None)
        if live is None:
            return ()
        return tuple(live.pending)

    async def resolve(self, signal: SessionSignal) -> SessionSignalTarget:
        key = self._key(
            signal.expected_execution_id,
            signal.target_session_scope_id,
            signal.target_session_attempt_id,
        )
        live = self._targets.get(key)
        if live is None:
            same_scope = any(
                target_key[:2] == (signal.expected_execution_id, signal.target_session_scope_id)
                for target_key in self._targets
            )
            raise SessionSignalTargetError(
                "stale_attempt" if same_scope else "target_not_active",
                (
                    "The requested session scope is active under a different attempt."
                    if same_scope
                    else "The exact session attempt is not registered as active."
                ),
            )
        return live.target

    async def enqueue(
        self,
        signal: SessionSignal,
        *,
        effective_mode: SessionSignalMode,
    ) -> tuple[QueuedSessionSignal, ...]:
        key = self._key(
            signal.expected_execution_id,
            signal.target_session_scope_id,
            signal.target_session_attempt_id,
        )
        live = self._targets.get(key)
        if live is None:
            raise SessionSignalTargetError(
                "target_lost_before_delivery",
                "The exact session attempt ended before signal handoff.",
            )
        return self._enqueue_live(QueuedSessionSignal(signal=signal, effective_mode=effective_mode))

    def _enqueue_live(
        self,
        queued: QueuedSessionSignal,
    ) -> tuple[QueuedSessionSignal, ...]:
        key = self._key(
            queued.signal.expected_execution_id,
            queued.signal.target_session_scope_id,
            queued.signal.target_session_attempt_id,
        )
        live = self._targets.get(key)
        if live is None:
            raise SessionSignalTargetError(
                "target_lost_before_delivery",
                "The exact session attempt ended before signal handoff.",
            )
        if any(item.signal.signal_id == queued.signal.signal_id for item in live.pending):
            return ()
        higher = next(
            (
                item
                for item in live.pending
                if not can_supersede_session_signal(
                    item.signal.source,
                    queued.signal.source,
                )
            ),
            None,
        )
        if higher is not None:
            raise SessionSignalTargetError(
                "higher_priority_signal_pending",
                "A higher-authority SessionSignal is already pending for this target.",
            )
        superseded = tuple(
            item
            for item in live.pending
            if queued.signal.source.priority > item.signal.source.priority
        )
        if superseded:
            live.pending = deque(item for item in live.pending if item not in superseded)
        live.pending.append(queued)
        return superseded

    def pop_pending(self, target: SessionSignalTarget) -> QueuedSessionSignal | None:
        """Pop one pending signal without yielding, closing the unregister race."""
        key = self._key(
            target.execution_id,
            target.session_scope_id,
            target.session_attempt_id,
        )
        live = self._targets.get(key)
        if live is None or not live.pending:
            return None
        return live.pending.popleft()

    def list_targets(self, *, execution_id: str) -> tuple[SessionSignalTarget, ...]:
        """List exact active attempts for one execution for main-session discovery."""
        return tuple(
            sorted(
                (
                    live.target
                    for live in self._targets.values()
                    if live.target.execution_id == execution_id
                ),
                key=lambda target: (
                    target.depth if target.depth is not None else 0,
                    target.ac_index if target.ac_index is not None else 10**9,
                    target.parent_ac_index if target.parent_ac_index is not None else 10**9,
                    target.sub_ac_index if target.sub_ac_index is not None else 10**9,
                    target.session_scope_id,
                ),
            )
        )


@dataclass(slots=True)
class EventStoreSessionSignalTargetResolver:
    """Resolve active attempts from existing execution-session lifecycle events."""

    event_store: EventStore
    capabilities_by_backend: Mapping[str, SessionSignalCapabilities] = field(default_factory=dict)

    async def _execution_job_is_terminal(self, execution_id: str) -> bool:
        status_reader = getattr(self.event_store, "get_latest_execution_job_status", None)
        if not callable(status_reader):
            return False
        status = await status_reader(execution_id)
        return isinstance(status, str) and status in _TERMINAL_JOB_STATUSES

    @staticmethod
    def _runtime_backend(event: object) -> str:
        data = getattr(event, "data", {})
        runtime_backend = data.get("runtime_backend") if isinstance(data, dict) else None
        if not isinstance(runtime_backend, str) or not runtime_backend.strip():
            runtime = data.get("runtime") if isinstance(data, dict) else None
            runtime_backend = runtime.get("backend") if isinstance(runtime, dict) else None
        if not isinstance(runtime_backend, str) or not runtime_backend.strip():
            raise SessionSignalTargetError(
                "runtime_backend_unknown",
                "The active attempt does not declare a runtime backend.",
            )
        return runtime_backend.strip()

    def _target_from_event(self, event: object, *, execution_id: str) -> SessionSignalTarget:
        data = getattr(event, "data", {})
        if not isinstance(data, dict):
            raise SessionSignalTargetError(
                "target_metadata_invalid",
                "The active attempt lifecycle metadata is invalid.",
            )
        session_scope_id = data.get("session_scope_id")
        session_attempt_id = data.get("session_attempt_id")
        if not isinstance(session_scope_id, str) or not session_scope_id.strip():
            raise SessionSignalTargetError(
                "target_metadata_invalid",
                "The active attempt does not declare a session scope.",
            )
        if not isinstance(session_attempt_id, str) or not session_attempt_id.strip():
            raise SessionSignalTargetError(
                "target_metadata_invalid",
                "The active attempt does not declare a session attempt.",
            )
        runtime_backend = self._runtime_backend(event)

        def optional_int(name: str) -> int | None:
            value = data.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        ac_index = optional_int("ac_index")
        parent_ac_index = optional_int("parent_ac_index")
        sub_ac_index = optional_int("sub_ac_index")
        contract_version = optional_int("contract_version") or 1
        display_label = data.get("display_label")
        if not isinstance(display_label, str) or not display_label.strip():
            if ac_index is not None and parent_ac_index is None:
                display_label = f"AC {ac_index + 1}"
            elif parent_ac_index is not None and sub_ac_index is not None:
                display_label = f"AC {parent_ac_index + 1}.{sub_ac_index + 1}"
            else:
                display_label = None
        ac_content = data.get("acceptance_criterion") or data.get("ac_content")
        if not isinstance(ac_content, str) or not ac_content.strip():
            ac_content = None
        display_path = data.get("display_path")
        if not isinstance(display_path, str) or not display_path.strip():
            display_path = None
        orchestrator_session_id = data.get("orchestrator_session_id")
        if not isinstance(orchestrator_session_id, str) or not orchestrator_session_id.strip():
            orchestrator_session_id = None

        return SessionSignalTarget(
            execution_id=execution_id,
            session_scope_id=session_scope_id.strip(),
            session_attempt_id=session_attempt_id.strip(),
            runtime_backend=runtime_backend,
            capabilities=self.capabilities_by_backend.get(
                runtime_backend,
                SessionSignalCapabilities(),
            ),
            contract_version=contract_version,
            orchestrator_session_id=orchestrator_session_id,
            ac_id=data.get("ac_id") if isinstance(data.get("ac_id"), str) else None,
            ac_content=ac_content,
            display_label=display_label,
            ac_index=ac_index,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_id=data.get("node_id") if isinstance(data.get("node_id"), str) else None,
            display_path=display_path,
            depth=optional_int("depth"),
        )

    async def list_targets(self, *, execution_id: str) -> tuple[SessionSignalTarget, ...]:
        """Reconstruct the latest active attempt per scope from durable events."""
        if await self._execution_job_is_terminal(execution_id):
            return ()
        events = await self.event_store.query_execution_related_events(
            execution_id,
            limit=None,
        )
        latest_by_scope: dict[str, object] = {}
        for event in events:
            if event.type not in _RUNTIME_LIFECYCLE_EVENTS:
                continue
            scope = event.data.get("session_scope_id")
            if isinstance(scope, str) and scope and scope not in latest_by_scope:
                latest_by_scope[scope] = event

        targets = [
            self._target_from_event(event, execution_id=execution_id)
            for event in latest_by_scope.values()
            if event.type in _ACTIVE_RUNTIME_EVENTS
        ]
        return tuple(
            sorted(
                targets,
                key=lambda target: (
                    target.depth if target.depth is not None else 0,
                    target.ac_index if target.ac_index is not None else 10**9,
                    target.parent_ac_index if target.parent_ac_index is not None else 10**9,
                    target.sub_ac_index if target.sub_ac_index is not None else 10**9,
                    target.session_scope_id,
                ),
            )
        )

    async def resolve(self, signal: SessionSignal) -> SessionSignalTarget:
        if await self._execution_job_is_terminal(signal.expected_execution_id):
            raise SessionSignalTargetError(
                "target_terminal",
                "The execution job is terminal, so none of its AC attempts are active.",
            )
        events = await self.event_store.query_execution_related_events(
            signal.expected_execution_id,
            limit=None,
        )
        lifecycle = [event for event in events if event.type in _RUNTIME_LIFECYCLE_EVENTS]
        same_scope = [
            event
            for event in lifecycle
            if event.data.get("session_scope_id") == signal.target_session_scope_id
            and event.data.get("execution_id") == signal.expected_execution_id
        ]
        if not same_scope:
            raise SessionSignalTargetError(
                "target_not_found",
                "No runtime lifecycle event matches the requested execution and scope.",
            )

        latest = same_scope[0]  # query_execution_related_events returns newest first
        if latest.data.get("session_attempt_id") != signal.target_session_attempt_id:
            raise SessionSignalTargetError(
                "stale_attempt",
                "The requested session scope exists but the exact attempt was replaced.",
            )
        if latest.type in _TERMINAL_RUNTIME_EVENTS:
            raise SessionSignalTargetError(
                "target_terminal",
                "The exact runtime attempt is already terminal.",
            )
        if latest.type not in _ACTIVE_RUNTIME_EVENTS:
            raise SessionSignalTargetError(
                "target_not_active",
                "The exact runtime attempt is not active.",
            )
        return self._target_from_event(latest, execution_id=signal.expected_execution_id)


@dataclass(slots=True)
class SessionSignalMailbox:
    """Persist requested/accepted/queued or rejected SessionSignal lifecycles."""

    event_store: EventStore
    target_resolver: SessionSignalTargetResolver
    delivery_queue: SessionSignalQueue | None = None
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    async def request(self, signal: SessionSignal) -> SessionSignalProjection:
        """Validate, resolve, and durably queue a signal exactly once per signal ID."""
        lock = self._locks.setdefault(signal.signal_id, asyncio.Lock())
        async with lock:
            return await self._request_locked(signal)

    async def _request_locked(self, signal: SessionSignal) -> SessionSignalProjection:
        existing_events = await self.event_store.replay("session_signal", signal.signal_id)
        if existing_events:
            existing = project_session_signal(existing_events)
            self._assert_same_signal(existing, signal)
            if existing.state is not SessionSignalState.REQUESTED:
                return existing
        else:
            requested = create_session_signal_requested_event(signal)
            await self.event_store.append(requested)
            existing_events = [requested]

        if signal.is_expired():
            rejected = create_session_signal_rejected_event(
                signal,
                rejection_code="expired",
                detail="The SessionSignal expired before target resolution.",
            )
            await self.event_store.append(rejected)
            return project_session_signal([*existing_events, rejected])

        if signal.contract_effect is SessionSignalContractEffect.SPECIFICATION_CHANGE:
            rejected = create_session_signal_rejected_event(
                signal,
                rejection_code="specification_change_requires_shared_successor",
                detail=(
                    "Specification-changing intent cannot be injected into one live AC; "
                    "create an approval-bound shared successor contract instead."
                ),
            )
            await self.event_store.append(rejected)
            return project_session_signal([*existing_events, rejected])

        try:
            target = await self.target_resolver.resolve(signal)
        except SessionSignalTargetError as exc:
            rejected = create_session_signal_rejected_event(
                signal,
                rejection_code=exc.code,
                detail=str(exc),
            )
            await self.event_store.append(rejected)
            return project_session_signal([*existing_events, rejected])

        try:
            effective_mode = resolve_session_signal_mode(signal, target.capabilities)
        except SessionSignalCapabilityError as exc:
            rejected = create_session_signal_rejected_event(
                signal,
                rejection_code="capability_unsupported",
                detail=str(exc),
                runtime_backend=target.runtime_backend,
            )
            await self.event_store.append(rejected)
            return project_session_signal([*existing_events, rejected])

        if (
            signal.expected_contract_version is not None
            and signal.expected_contract_version != target.contract_version
        ):
            rejected = create_session_signal_rejected_event(
                signal,
                rejection_code="contract_version_mismatch",
                detail="The active AC uses a different shared execution-contract version.",
                effective_mode=effective_mode,
                runtime_backend=target.runtime_backend,
                orchestrator_session_id=target.orchestrator_session_id,
            )
            await self.event_store.append(rejected)
            return project_session_signal([*existing_events, rejected])

        accepted = create_session_signal_accepted_event(
            signal,
            effective_mode=effective_mode,
            capabilities=target.capabilities,
            runtime_backend=target.runtime_backend,
            orchestrator_session_id=target.orchestrator_session_id,
        )
        queued = create_session_signal_queued_event(
            signal,
            effective_mode=effective_mode,
            runtime_backend=target.runtime_backend,
            orchestrator_session_id=target.orchestrator_session_id,
        )
        await self.event_store.append_batch([accepted, queued])
        queued_events = [*existing_events, accepted, queued]
        if self.delivery_queue is not None:
            try:
                superseded = await self.delivery_queue.enqueue(
                    signal,
                    effective_mode=effective_mode,
                )
                for displaced in superseded:
                    await self.event_store.append(
                        create_session_signal_rejected_event(
                            displaced.signal,
                            rejection_code="superseded_by_higher_priority_signal",
                            detail=(
                                "A higher-authority SessionSignal superseded this pending "
                                "message before delivery."
                            ),
                            effective_mode=displaced.effective_mode,
                            runtime_backend=target.runtime_backend,
                            orchestrator_session_id=target.orchestrator_session_id,
                        )
                    )
            except SessionSignalTargetError as exc:
                rejected = create_session_signal_rejected_event(
                    signal,
                    rejection_code=exc.code,
                    detail=str(exc),
                    effective_mode=effective_mode,
                    runtime_backend=target.runtime_backend,
                    orchestrator_session_id=target.orchestrator_session_id,
                )
                await self.event_store.append(rejected)
                queued_events.append(rejected)
        return project_session_signal(queued_events)

    @staticmethod
    def _assert_same_signal(
        existing: SessionSignalProjection,
        incoming: SessionSignal,
    ) -> None:
        if (
            existing.effective_idempotency_key != incoming.effective_idempotency_key
            or existing.requested_mode is not incoming.mode
            or existing.source is not incoming.source
            or existing.contract_effect is not incoming.contract_effect
            or existing.message_digest != incoming.message_digest
        ):
            raise ValueError(
                "SessionSignal signal_id already belongs to a different immutable request"
            )


__all__ = [
    "EventStoreSessionSignalTargetResolver",
    "QueuedSessionSignal",
    "SessionSignalMailbox",
    "SessionSignalHub",
    "SessionSignalQueue",
    "SessionSignalTarget",
    "SessionSignalTargetError",
    "SessionSignalTargetResolver",
    "render_after_turn_signal_prompt",
    "render_inform_signal_prompt",
]
