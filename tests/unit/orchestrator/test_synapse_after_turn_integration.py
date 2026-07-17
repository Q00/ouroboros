"""End-to-end in-process after-turn delivery for Ouroboros Synapse."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalCapabilities,
    SessionSignalMode,
    SessionSignalSource,
    SessionSignalState,
    derive_session_signal_id,
)
from ouroboros.core.session_signal_projection import project_session_signal
from ouroboros.orchestrator.adapter import FULL_CAPABILITIES, AgentMessage, RuntimeHandle
from ouroboros.orchestrator.parallel_executor import (
    ParallelACExecutor,
    _bounded_session_signal_runtime_reply,
)
from ouroboros.orchestrator.synapse import (
    EventStoreSessionSignalTargetResolver,
    SessionSignalHub,
    SessionSignalMailbox,
)
from ouroboros.persistence.event_store import EventStore


class _TwoTurnRuntime:
    runtime_backend = "codex_mcp"
    permission_mode = "bypassPermissions"

    def __init__(self, cwd: Path) -> None:
        self.working_directory = str(cwd)
        self.capabilities = replace(
            FULL_CAPABILITIES,
            session_signals=SessionSignalCapabilities(after_turn_delivery=True),
        )
        self.first_turn_started = asyncio.Event()
        self.release_first_turn = asyncio.Event()
        self.prompts: list[str] = []

    async def execute_task(self, **kwargs: Any):
        prompt = str(kwargs["prompt"])
        resume_handle = kwargs.get("resume_handle")
        self.prompts.append(prompt)
        call_number = len(self.prompts)
        handle = RuntimeHandle(
            backend="codex_mcp",
            kind="agent_runtime",
            native_session_id="thread_synapse_1",
            cwd=self.working_directory,
            metadata=(dict(resume_handle.metadata) if resume_handle is not None else {}),
        )

        if call_number == 1:
            self.first_turn_started.set()
            yield AgentMessage(
                type="assistant",
                content="Initial implementation is ready.",
                resume_handle=handle,
            )
            await self.release_first_turn.wait()
            yield AgentMessage(
                type="result",
                content="[TASK_COMPLETE] initial",
                data={"subtype": "success"},
                resume_handle=handle,
            )
            return

        assert resume_handle is not None
        assert "[Ouroboros Synapse: additive intent]" in prompt
        assert "Make the confirmation copy explicit." in prompt
        yield AgentMessage(
            type="result",
            content="[TASK_COMPLETE] redirected",
            data={"subtype": "success"},
            resume_handle=handle,
        )


class _ErrorEnvelopeResumeRuntime(_TwoTurnRuntime):
    async def execute_task(self, **kwargs: Any):
        if not self.prompts:
            async for message in super().execute_task(**kwargs):
                yield message
            return

        prompt = str(kwargs["prompt"])
        resume_handle = kwargs.get("resume_handle")
        self.prompts.append(prompt)
        assert resume_handle is not None
        yield AgentMessage(
            type="result",
            content="Resume bootstrap failed before provider acknowledgement.",
            data={"subtype": "error", "recoverable": True},
            resume_handle=resume_handle,
        )


class _InformRuntime(_TwoTurnRuntime):
    def __init__(self, cwd: Path) -> None:
        super().__init__(cwd)
        self.capabilities = replace(
            FULL_CAPABILITIES,
            session_signals=SessionSignalCapabilities(
                inform_delivery=True,
                background_reply=True,
                after_turn_delivery=True,
            ),
        )
        self.signal_tools: list[str] | None = None

    async def execute_task(self, **kwargs: Any):
        if not self.prompts:
            async for message in super().execute_task(**kwargs):
                yield message
            return

        prompt = str(kwargs["prompt"])
        resume_handle = kwargs.get("resume_handle")
        self.prompts.append(prompt)
        self.signal_tools = list(kwargs.get("tools", []))
        assert resume_handle is not None
        assert "[Ouroboros Synapse: information request]" in prompt
        yield AgentMessage(
            type="assistant",
            content="AC 1 is waiting on the confirmation-copy assertion.",
            resume_handle=resume_handle,
        )
        yield AgentMessage(
            type="result",
            content="[TASK_COMPLETE] information reply",
            data={"subtype": "success"},
            resume_handle=resume_handle,
        )


def test_bounded_reply_reassembles_streamed_assistant_chunks() -> None:
    messages = [
        AgentMessage(type="assistant", content="SYNAPSE_"),
        AgentMessage(type="assistant", content="REPLY"),
        AgentMessage(
            type="result",
            content="[TASK_COMPLETE]",
            data={"subtype": "success"},
        ),
    ]

    assert _bounded_session_signal_runtime_reply(messages) == "SYNAPSE_REPLY"


def test_bounded_reply_prefers_explicit_completion_over_prior_chunks() -> None:
    messages = [
        AgentMessage(type="assistant", content="partial"),
        AgentMessage(
            type="assistant",
            content="Complete bounded reply",
            data={"subtype": "completion"},
        ),
    ]

    assert _bounded_session_signal_runtime_reply(messages) == "Complete bounded reply"


@pytest.mark.asyncio
async def test_cross_process_after_turn_signal_is_applied_and_completed(tmp_path: Path) -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    hub = SessionSignalHub(event_store=store)
    runtime = _TwoTurnRuntime(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        session_signal_hub=hub,
    )
    target_resolver = EventStoreSessionSignalTargetResolver(
        event_store=store,
        capabilities_by_backend={
            runtime.runtime_backend: runtime.capabilities.session_signals,
        },
    )
    mailbox = SessionSignalMailbox(event_store=store, target_resolver=target_resolver)
    execution_id = "exec_synapse"
    scope_id = "exec_synapse_ac_1"
    attempt_id = "exec_synapse_ac_1_attempt_1"
    idempotency_key = "user_turn_9_ac_1"
    signal = SessionSignal(
        signal_id=derive_session_signal_id(
            expected_execution_id=execution_id,
            target_session_scope_id=scope_id,
            target_session_attempt_id=attempt_id,
            idempotency_key=idempotency_key,
        ),
        target_session_scope_id=scope_id,
        target_session_attempt_id=attempt_id,
        expected_execution_id=execution_id,
        mode=SessionSignalMode.AFTER_TURN,
        message="Make the confirmation copy explicit.",
        source=SessionSignalSource.USER,
        reason="The user clarified the desired UX.",
        idempotency_key=idempotency_key,
    )

    execution_task = asyncio.create_task(
        executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement the confirmation interaction",
            session_id="orch_synapse",
            execution_id=execution_id,
            tools=[],
            system_prompt="test",
            seed_goal="Deliver a friendly confirmation UX",
            depth=0,
            start_time=datetime.now(UTC),
        )
    )
    try:
        await asyncio.wait_for(runtime.first_turn_started.wait(), timeout=2)
        targets = ()
        for _attempt in range(20):
            targets = await target_resolver.list_targets(execution_id=execution_id)
            if targets:
                break
            await asyncio.sleep(0.01)
        assert len(targets) == 1
        assert targets[0].ac_content == "Implement the confirmation interaction"
        assert targets[0].display_label == "AC 1"
        assert targets[0].session_scope_id == scope_id
        assert targets[0].session_attempt_id == attempt_id
        queued = await mailbox.request(signal)
        assert queued.state is SessionSignalState.QUEUED
        runtime.release_first_turn.set()

        result = await asyncio.wait_for(execution_task, timeout=5)
        signal_events = await store.replay("session_signal", signal.signal_id)
        projection = project_session_signal(signal_events)

        assert result.success is True
        assert len(runtime.prompts) == 2
        assert projection.state is SessionSignalState.COMPLETED
        assert projection.effective_mode is SessionSignalMode.AFTER_TURN
        assert [event.type for event in signal_events] == [
            "control.session.signal.requested",
            "control.session.signal.accepted",
            "control.session.signal.queued",
            "control.session.signal.delivering",
            "control.session.signal.applied",
            "control.session.signal.completed",
        ]
    finally:
        if not execution_task.done():
            execution_task.cancel()
        await store.close()


@pytest.mark.asyncio
async def test_error_only_resume_is_delivery_uncertain_not_applied(tmp_path: Path) -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    hub = SessionSignalHub()
    runtime = _ErrorEnvelopeResumeRuntime(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        session_signal_hub=hub,
    )
    mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)
    signal = SessionSignal(
        signal_id="sig_error_envelope",
        target_session_scope_id="exec_error_envelope_ac_1",
        target_session_attempt_id="exec_error_envelope_ac_1_attempt_1",
        expected_execution_id="exec_error_envelope",
        mode=SessionSignalMode.AFTER_TURN,
        message="Apply only if the provider accepts the resumed turn.",
        source=SessionSignalSource.USER,
        reason="Manual resume guarantee.",
        idempotency_key="error_envelope_1",
    )
    execution_task = asyncio.create_task(
        executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Verify resume acknowledgement",
            session_id="orch_error_envelope",
            execution_id="exec_error_envelope",
            tools=[],
            system_prompt="test",
            seed_goal="Never overclaim delivery",
            depth=0,
            start_time=datetime.now(UTC),
        )
    )
    try:
        await asyncio.wait_for(runtime.first_turn_started.wait(), timeout=2)
        queued = await mailbox.request(signal)
        assert queued.state is SessionSignalState.QUEUED
        runtime.release_first_turn.set()

        result = await asyncio.wait_for(execution_task, timeout=5)
        events = await store.replay("session_signal", signal.signal_id)
        projection = project_session_signal(events)

        assert result.success is False
        assert projection.state is SessionSignalState.DELIVERY_UNCERTAIN
        assert [event.type for event in events] == [
            "control.session.signal.requested",
            "control.session.signal.accepted",
            "control.session.signal.queued",
            "control.session.signal.delivering",
            "control.session.signal.delivery_uncertain",
        ]
    finally:
        if not execution_task.done():
            execution_task.cancel()
        await store.close()


@pytest.mark.asyncio
async def test_inform_uses_no_tools_returns_bounded_reply_and_preserves_primary_result(
    tmp_path: Path,
) -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    hub = SessionSignalHub()
    runtime = _InformRuntime(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        session_signal_hub=hub,
    )
    mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)
    signal = SessionSignal(
        signal_id="sig_inform_reply",
        target_session_scope_id="exec_inform_ac_1",
        target_session_attempt_id="exec_inform_ac_1_attempt_1",
        expected_execution_id="exec_inform",
        mode=SessionSignalMode.INFORM,
        message="Tell the main conductor what remains, without changing files.",
        source=SessionSignalSource.USER,
        reason="The user asked for AC-specific assurance.",
        idempotency_key="inform_reply_1",
    )
    execution_task = asyncio.create_task(
        executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement the confirmation interaction",
            session_id="orch_inform",
            execution_id="exec_inform",
            tools=["Read", "Edit", "Bash"],
            system_prompt="test",
            seed_goal="Deliver a friendly confirmation UX",
            depth=0,
            start_time=datetime.now(UTC),
        )
    )
    try:
        await asyncio.wait_for(runtime.first_turn_started.wait(), timeout=2)
        assert (await mailbox.request(signal)).state is SessionSignalState.QUEUED
        runtime.release_first_turn.set()

        result = await asyncio.wait_for(execution_task, timeout=5)
        projection = project_session_signal(await store.replay("session_signal", signal.signal_id))

        assert result.success is True
        assert result.final_message == "[TASK_COMPLETE] initial"
        assert runtime.signal_tools == []
        assert projection.state is SessionSignalState.COMPLETED
        assert projection.effective_mode is SessionSignalMode.INFORM
        assert projection.reply == "AC 1 is waiting on the confirmation-copy assertion."
    finally:
        if not execution_task.done():
            execution_task.cancel()
        await store.close()


@pytest.mark.asyncio
async def test_signal_expiry_is_rechecked_at_runtime_consumption(tmp_path: Path) -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    hub = SessionSignalHub()
    runtime = _TwoTurnRuntime(tmp_path)
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        session_signal_hub=hub,
    )
    mailbox = SessionSignalMailbox(store, hub, delivery_queue=hub)
    execution_task = asyncio.create_task(
        executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Verify expiry handling",
            session_id="orch_expire",
            execution_id="exec_expire",
            tools=[],
            system_prompt="test",
            seed_goal="Never apply expired intent",
            depth=0,
            start_time=datetime.now(UTC),
        )
    )
    try:
        await asyncio.wait_for(runtime.first_turn_started.wait(), timeout=2)
        expires_at = datetime.now(UTC) + timedelta(seconds=1)
        signal = SessionSignal(
            signal_id="sig_expire_at_boundary",
            target_session_scope_id="exec_expire_ac_1",
            target_session_attempt_id="exec_expire_ac_1_attempt_1",
            expected_execution_id="exec_expire",
            mode=SessionSignalMode.AFTER_TURN,
            message="Apply only if this reaches the next safe boundary in time.",
            source=SessionSignalSource.USER,
            reason="Expiry boundary test.",
            idempotency_key="expire_boundary_1",
            expires_at=expires_at,
        )
        assert (await mailbox.request(signal)).state is SessionSignalState.QUEUED
        await asyncio.sleep(max(0.0, (expires_at - datetime.now(UTC)).total_seconds()) + 0.05)
        runtime.release_first_turn.set()

        result = await asyncio.wait_for(execution_task, timeout=5)
        events = await store.replay("session_signal", signal.signal_id)
        projection = project_session_signal(events)

        assert result.success is True
        assert len(runtime.prompts) == 1
        assert projection.state is SessionSignalState.REJECTED
        assert events[-1].data["rejection_code"] == "expired_before_delivery"
    finally:
        if not execution_task.done():
            execution_task.cancel()
        await store.close()
