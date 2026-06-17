"""The effort-routing decision is emitted as a queryable event.

The deterministic frugality proof reads ``execution.ac.effort_routed`` events to
join per-AC (effort_level x effort_mode) with token attribution and the TraceGuard
verdict. Only ``enforced`` rows count toward the proof, so the event must carry the
honest mode — that is what these tests pin.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.orchestrator.adapter import (
    FULL_CAPABILITIES,
    AgentMessage,
    ParamSupport,
    RuntimeHandle,
)
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor


def _capturing_event_store() -> tuple[AsyncMock, list]:
    store = AsyncMock()
    events: list = []

    async def _append(event):
        events.append(event)

    store.append.side_effect = _append
    return store, events


class _EnforcedRuntime:
    """A runtime that declares NATIVE effort support and accepts the kwarg."""

    _runtime_handle_backend = "claude"

    def __init__(self) -> None:
        self.received_effort: str | None = "UNSET"

    @property
    def runtime_backend(self) -> str:
        return self._runtime_handle_backend

    @property
    def working_directory(self) -> str | None:
        return "/tmp/project"

    @property
    def permission_mode(self) -> str | None:
        return "acceptEdits"

    @property
    def capabilities(self):
        return replace(FULL_CAPABILITIES, reasoning_effort_support=ParamSupport.NATIVE)

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        reasoning_effort: str | None = None,
    ):
        self.received_effort = reasoning_effort
        yield AgentMessage(
            type="result",
            content="[TASK_COMPLETE]",
            data={"subtype": "success"},
            resume_handle=resume_handle,
        )


class _AdvisedRuntime:
    """A runtime with no capability declaration and no effort kwarg (the default)."""

    _runtime_handle_backend = "opencode"

    @property
    def runtime_backend(self) -> str:
        return self._runtime_handle_backend

    @property
    def working_directory(self) -> str | None:
        return "/tmp/project"

    @property
    def permission_mode(self) -> str | None:
        return "acceptEdits"

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ):
        yield AgentMessage(
            type="result",
            content="[TASK_COMPLETE]",
            data={"subtype": "success"},
            resume_handle=resume_handle,
        )


def _effort_events(events: list) -> list:
    return [e for e in events if getattr(e, "type", None) == "execution.ac.effort_routed"]


async def _run_one_ac(executor: ParallelACExecutor, *, is_sub_ac: bool):
    return await executor._execute_atomic_ac(
        ac_index=1,
        ac_content="Implement a thing",
        session_id="sess_effort",
        tools=["Read"],
        system_prompt="system",
        seed_goal="Ship it",
        depth=0,
        start_time=datetime.now(UTC),
        execution_id="exec_effort",
        is_sub_ac=is_sub_ac,
        parent_ac_index=0 if is_sub_ac else None,
        sub_ac_index=0 if is_sub_ac else None,
    )


@pytest.mark.asyncio
async def test_enforced_runtime_emits_enforced_event_and_passes_kwarg() -> None:
    store, events = _capturing_event_store()
    runtime = _EnforcedRuntime()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        reasoning_effort="high",
    )

    await _run_one_ac(executor, is_sub_ac=False)

    routed = _effort_events(events)
    assert len(routed) == 1
    assert routed[0].data["effort_mode"] == "enforced"
    assert routed[0].data["effort_level"] == "high"
    # NATIVE runtime actually received the level.
    assert runtime.received_effort == "high"


@pytest.mark.asyncio
async def test_decomposed_child_runs_one_notch_lower() -> None:
    store, events = _capturing_event_store()
    runtime = _EnforcedRuntime()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        reasoning_effort="high",
    )

    await _run_one_ac(executor, is_sub_ac=True)

    routed = _effort_events(events)
    assert routed[0].data["effort_level"] == "medium"  # high -> one notch lower
    assert routed[0].data["is_decomposed_child"] is True
    assert runtime.received_effort == "medium"


@pytest.mark.asyncio
async def test_advised_runtime_records_advised_and_does_not_pass_kwarg() -> None:
    store, events = _capturing_event_store()
    runtime = _AdvisedRuntime()  # no capabilities, no effort kwarg
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        reasoning_effort="high",
    )

    # Must not raise even though execute_task has no reasoning_effort parameter.
    await _run_one_ac(executor, is_sub_ac=False)

    routed = _effort_events(events)
    assert len(routed) == 1
    assert routed[0].data["effort_mode"] == "advised"
    assert routed[0].data["effort_level"] == "high"


@pytest.mark.asyncio
async def test_dormant_when_no_base_effort_emits_no_event() -> None:
    store, events = _capturing_event_store()
    executor = ParallelACExecutor(
        adapter=_EnforcedRuntime(),
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        # reasoning_effort defaults None -> dormant
    )

    await _run_one_ac(executor, is_sub_ac=False)

    assert _effort_events(events) == []
