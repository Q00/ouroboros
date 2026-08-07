"""Cross-connection ownership races for durable Synapse admission."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalCapabilities,
    SessionSignalMode,
    SessionSignalSource,
    SessionSignalState,
)
from ouroboros.core.session_signal_projection import project_session_signal
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.synapse import SessionSignalMailbox, SessionSignalTarget
from ouroboros.persistence.event_store import EventStore, sqlite_database_url
from ouroboros.persistence.session_signal_store import append_runtime_lifecycle


def _target() -> SessionSignalTarget:
    return SessionSignalTarget(
        execution_id="exec_race",
        session_scope_id="scope_race",
        session_attempt_id="attempt_race",
        runtime_backend="codex_mcp",
        capabilities=SessionSignalCapabilities(after_turn_delivery=True),
    )


def _lifecycle(event_type: str) -> BaseEvent:
    target = _target()
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


def _signal(index: int) -> SessionSignal:
    target = _target()
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
