#!/usr/bin/env python3
"""Deterministic manual smoke tests for durable Ouroboros Synapse semantics.

Unlike ``manual-synapse-smoke.py``, this harness does not call an external LLM.
It exercises the production EventStore, mailbox, hub, replay projection, and AC
delivery boundary directly.  The restart cases close and reopen the SQLite
database so the result does not depend on in-memory state.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import MagicMock

from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalCapabilities,
    SessionSignalMode,
    SessionSignalSource,
    SessionSignalState,
)
from ouroboros.core.session_signal_projection import project_session_signal
from ouroboros.events.session_signal import create_session_signal_delivery_started_event
from ouroboros.orchestrator.adapter import FULL_CAPABILITIES, AgentMessage, RuntimeHandle
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.synapse import (
    SessionSignalHub,
    SessionSignalMailbox,
    SessionSignalTarget,
)
from ouroboros.persistence.event_store import EventStore


class _BoundaryRuntime:
    """One controllable primary turn; a signal follow-up is a test failure."""

    runtime_backend = "codex_cli"
    llm_backend = "deterministic"
    permission_mode = "bypassPermissions"

    def __init__(self, cwd: Path) -> None:
        self.working_directory = str(cwd)
        self.capabilities = replace(
            FULL_CAPABILITIES,
            session_signals=SessionSignalCapabilities(after_turn_delivery=True),
        )
        self.first_turn_started = asyncio.Event()
        self.release_first_turn = asyncio.Event()
        self.call_count = 0

    async def execute_task(self, **kwargs: Any):
        self.call_count += 1
        if self.call_count != 1:
            raise AssertionError("An expired SessionSignal reached the runtime")
        handle = RuntimeHandle(
            backend=self.runtime_backend,
            kind="agent_runtime",
            native_session_id="manual-expiry-thread",
            cwd=self.working_directory,
        )
        self.first_turn_started.set()
        yield AgentMessage(
            type="assistant",
            content="Primary turn reached the controlled boundary.",
            resume_handle=handle,
        )
        await self.release_first_turn.wait()
        yield AgentMessage(
            type="result",
            content="[TASK_COMPLETE] primary",
            data={"subtype": "success"},
            resume_handle=handle,
        )


def _target() -> SessionSignalTarget:
    return SessionSignalTarget(
        execution_id="manual_state_exec",
        session_scope_id="manual_state_exec_ac_1",
        session_attempt_id="manual_state_exec_ac_1_attempt_1",
        runtime_backend="codex_cli",
        capabilities=SessionSignalCapabilities(after_turn_delivery=True),
        orchestrator_session_id="manual_state_orchestrator",
        ac_id="manual_state_exec_ac_1",
        ac_content="Exercise durable Synapse state semantics",
        display_label="AC 1",
        ac_index=0,
    )


def _signal(
    signal_id: str,
    *,
    source: SessionSignalSource = SessionSignalSource.USER,
    expires_at: datetime | None = None,
) -> SessionSignal:
    target = _target()
    return SessionSignal(
        signal_id=signal_id,
        target_session_scope_id=target.session_scope_id,
        target_session_attempt_id=target.session_attempt_id,
        expected_execution_id=target.execution_id,
        mode=SessionSignalMode.AFTER_TURN,
        message=f"Apply the bounded manual state probe {signal_id}.",
        source=source,
        reason="Deterministic manual Synapse state verification.",
        idempotency_key=f"idem_{signal_id}",
        expires_at=expires_at,
    )


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _consumption_expiry(cwd: Path) -> dict[str, object]:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    hub = SessionSignalHub()
    runtime = _BoundaryRuntime(cwd)
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        session_signal_hub=hub,
    )
    mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)
    signal = _signal(
        "manual_expiry",
        expires_at=datetime.now(UTC) + timedelta(milliseconds=50),
    )
    execution_task = asyncio.create_task(
        executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Hold until the SessionSignal expires",
            session_id="manual_expiry_orchestrator",
            execution_id=_target().execution_id,
            tools=[],
            system_prompt="Deterministic manual state probe.",
            seed_goal="Never deliver expired intent",
            depth=0,
            start_time=datetime.now(UTC),
        )
    )
    try:
        await asyncio.wait_for(runtime.first_turn_started.wait(), timeout=2)
        queued = await mailbox.request(signal)
        if queued.state is not SessionSignalState.QUEUED:
            raise RuntimeError(f"Expiry probe did not queue: {queued.state.value}")
        await asyncio.sleep(0.1)
        runtime.release_first_turn.set()
        result = await asyncio.wait_for(execution_task, timeout=5)
        events = await store.replay("session_signal", signal.signal_id)
        projection = project_session_signal(events)
        rejection_code = events[-1].data.get("rejection_code")
        if not result.success:
            raise RuntimeError("Primary AC failed during consumption-expiry probe")
        if projection.state is not SessionSignalState.REJECTED:
            raise RuntimeError(f"Expired signal ended as {projection.state.value}")
        if rejection_code != "expired_before_delivery":
            raise RuntimeError(f"Unexpected expiry rejection: {rejection_code!r}")
        if runtime.call_count != 1:
            raise RuntimeError("Expired signal invoked a follow-up provider turn")
        return {
            "state": projection.state.value,
            "rejection_code": rejection_code,
            "provider_turns": runtime.call_count,
            "event_types": [event.type for event in events],
        }
    finally:
        if not execution_task.done():
            execution_task.cancel()
        await store.close()


async def _priority_supersession() -> dict[str, object]:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    hub = SessionSignalHub()
    target = _target()
    hub.register(target)
    mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)
    conductor = _signal("manual_conductor", source=SessionSignalSource.CONDUCTOR)
    user = _signal("manual_user", source=SessionSignalSource.USER)
    worker = _signal("manual_worker", source=SessionSignalSource.WORKER)
    try:
        if (await mailbox.request(conductor)).state is not SessionSignalState.QUEUED:
            raise RuntimeError("Conductor signal did not queue")
        if (await mailbox.request(user)).state is not SessionSignalState.QUEUED:
            raise RuntimeError("User signal did not queue")
        worker_projection = await mailbox.request(worker)
        conductor_events = await store.replay("session_signal", conductor.signal_id)
        user_events = await store.replay("session_signal", user.signal_id)
        worker_events = await store.replay("session_signal", worker.signal_id)
        conductor_projection = project_session_signal(conductor_events)
        pending = hub.pop_pending(target)
        if conductor_projection.state is not SessionSignalState.REJECTED:
            raise RuntimeError("User signal did not supersede the conductor signal")
        if conductor_events[-1].data.get("rejection_code") != (
            "superseded_by_higher_priority_signal"
        ):
            raise RuntimeError("Conductor supersession was not recorded durably")
        if worker_projection.state is not SessionSignalState.REJECTED:
            raise RuntimeError("Worker signal bypassed a pending user signal")
        if worker_events[-1].data.get("rejection_code") != "higher_priority_signal_pending":
            raise RuntimeError("Worker priority rejection was not recorded durably")
        if pending is None or pending.signal.signal_id != user.signal_id:
            raise RuntimeError("The pending queue does not contain exactly the user signal")
        return {
            "authority_order": ["user", "conductor", "worker"],
            "conductor_state": conductor_projection.state.value,
            "conductor_rejection": conductor_events[-1].data.get("rejection_code"),
            "user_state": project_session_signal(user_events).state.value,
            "worker_state": worker_projection.state.value,
            "worker_rejection": worker_events[-1].data.get("rejection_code"),
            "pending_signal": pending.signal.signal_id,
        }
    finally:
        await store.close()


async def _queue_then_close(database_url: str, signal: SessionSignal, *, claim: bool) -> None:
    store = EventStore(database_url)
    await store.initialize()
    hub = SessionSignalHub()
    target = _target()
    hub.register(target)
    mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)
    try:
        queued = await mailbox.request(signal)
        if queued.state is not SessionSignalState.QUEUED:
            raise RuntimeError(f"Restart probe did not queue: {queued.state.value}")
        if claim:
            claimed = hub.pop_pending(target)
            if claimed is None:
                raise RuntimeError("Claimed-restart probe had no pending signal")
            await store.append(
                create_session_signal_delivery_started_event(
                    claimed.signal,
                    effective_mode=claimed.effective_mode,
                    runtime_backend=target.runtime_backend,
                    orchestrator_session_id=target.orchestrator_session_id,
                )
            )
    finally:
        await store.close()


async def _restart_projection(
    database_url: str,
    signal: SessionSignal,
    *,
    expect_replay: bool,
) -> dict[str, object]:
    store = EventStore(database_url)
    await store.initialize()
    hub = SessionSignalHub(event_store=store)
    target = _target()
    try:
        await hub.register_replaying(target)
        pending = hub.pop_pending(target)
        events = await store.replay("session_signal", signal.signal_id)
        projection = project_session_signal(events)
        if expect_replay:
            if pending is None or pending.signal.signal_id != signal.signal_id:
                raise RuntimeError("Queued signal was not reconstructed after restart")
            if projection.state is not SessionSignalState.QUEUED:
                raise RuntimeError(f"Replayed signal state is {projection.state.value}")
        else:
            if pending is not None:
                raise RuntimeError("Claimed signal was automatically replayed after restart")
            if projection.state is not SessionSignalState.DELIVERY_UNCERTAIN:
                raise RuntimeError(f"Claimed signal recovered as {projection.state.value}")
            if events[-1].data.get("automatic_retry_allowed") is not False:
                raise RuntimeError("Uncertain delivery did not suppress automatic retry")
        return {
            "state": projection.state.value,
            "replayed": pending is not None,
            "automatic_retry_allowed": events[-1].data.get("automatic_retry_allowed"),
            "event_types": [event.type for event in events],
        }
    finally:
        await store.close()


async def _restart_recovery(cwd: Path) -> dict[str, object]:
    queued_url = _database_url(cwd / "queued-restart.db")
    queued_signal = _signal("manual_restart_queued")
    await _queue_then_close(queued_url, queued_signal, claim=False)
    queued_result = await _restart_projection(queued_url, queued_signal, expect_replay=True)

    claimed_url = _database_url(cwd / "claimed-restart.db")
    claimed_signal = _signal("manual_restart_claimed")
    await _queue_then_close(claimed_url, claimed_signal, claim=True)
    claimed_result = await _restart_projection(
        claimed_url,
        claimed_signal,
        expect_replay=False,
    )
    return {
        "queued_restart": queued_result,
        "claimed_restart": claimed_result,
    }


async def _main() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ouroboros-synapse-state-") as tmp:
        cwd = Path(tmp)
        return {
            "consumption_expiry": await _consumption_expiry(cwd),
            "priority_supersession": await _priority_supersession(),
            "restart_recovery": await _restart_recovery(cwd),
        }


def main() -> int:
    try:
        result = asyncio.run(_main())
    except Exception as exc:  # noqa: BLE001 - manual harness reports exact failure.
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps({"ok": True, **result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
