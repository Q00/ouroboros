"""The effort-routing decision is emitted as a queryable event.

The deterministic frugality proof reads ``execution.ac.effort_routed`` events to
join per-AC (effort_level x effort_mode) with token attribution and the TraceGuard
verdict. Only ``enforced`` rows count toward the proof, so the event must carry the
honest mode — that is what these tests pin.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import InvestmentSpec
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


class _CancelledRuntime(_EnforcedRuntime):
    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        reasoning_effort: str | None = None,
    ):
        raise asyncio.CancelledError
        yield AgentMessage(type="result", content="unreachable", data={})


class _FreshHandleRuntime(_EnforcedRuntime):
    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        reasoning_effort: str | None = None,
    ):
        # Simulate runtimes that return a new handle object on first entry and
        # do not copy orchestrator metadata themselves.
        yield AgentMessage(
            type="result",
            content="[TASK_COMPLETE]",
            data={"subtype": "success"},
            resume_handle=RuntimeHandle(
                backend="claude",
                native_session_id="fresh-provider-session",
                cwd=self.working_directory,
            ),
        )


def _effort_events(events: list) -> list:
    return [e for e in events if getattr(e, "type", None) == "execution.ac.effort_routed"]


def _investment_events(events: list) -> list:
    return [e for e in events if getattr(e, "type", None) == "execution.ac.investment_assessed"]


def _capsule_events(events: list) -> list:
    return [e for e in events if getattr(e, "type", None) == "execution.ac.capsule.compiled"]


async def _run_one_ac(
    executor: ParallelACExecutor,
    *,
    is_sub_ac: bool,
    retry_attempt: int = 0,
    investment_spec: InvestmentSpec | None = None,
    sibling_acs: list[tuple[int | None, str]] | None = None,
):
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
        retry_attempt=retry_attempt,
        investment_spec=investment_spec,
        sibling_acs=sibling_acs,
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
async def test_decomposed_child_inherits_parent_tier_unchanged() -> None:
    # V5: a decomposed child no longer runs one notch lower — it inherits the
    # parent tier unchanged. ``is_decomposed_child`` is still recorded as a proof
    # flag, but the level is not dropped.
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
    assert routed[0].data["effort_level"] == "high"  # inherited unchanged
    assert routed[0].data["is_decomposed_child"] is True
    assert runtime.received_effort == "high"


@pytest.mark.asyncio
async def test_second_retry_raises_effort_one_notch() -> None:
    # V5: a hard AC on its second retry earns MORE reasoning — one notch up.
    store, events = _capturing_event_store()
    runtime = _EnforcedRuntime()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        reasoning_effort="medium",
    )

    await _run_one_ac(executor, is_sub_ac=False, retry_attempt=2)

    routed = _effort_events(events)
    assert routed[0].data["effort_level"] == "high"  # medium raised one notch
    assert runtime.received_effort == "high"


@pytest.mark.asyncio
async def test_authorized_low_investment_lowers_effort_and_records_exact_inputs() -> None:
    store, events = _capturing_event_store()
    runtime = _EnforcedRuntime()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        reasoning_effort="high",
    )
    investment = InvestmentSpec(
        difficulty="low",
        stakes="low",
        provenance="measured",
        confidence="high",
    )

    await _run_one_ac(executor, is_sub_ac=False, investment_spec=investment)

    assessed = _investment_events(events)
    assert len(assessed) == 1
    assert assessed[0].data["difficulty"] == "low"
    assert assessed[0].data["stakes"] == "low"
    assert assessed[0].data["provenance"] == "measured"
    assert assessed[0].data["confidence"] == "high"
    assert assessed[0].data["can_cheapen"] is True
    assert assessed[0].data["used_signals"] == [
        "difficulty",
        "stakes",
        "provenance",
        "confidence",
    ]
    routed = _effort_events(events)[0].data
    assert routed["effort_level"] == "medium"
    assert routed["investment_assessment"] == {
        key: assessed[0].data[key]
        for key in (
            "difficulty",
            "stakes",
            "provenance",
            "confidence",
            "used_signals",
            "missing_signals",
            "can_cheapen",
            "minimum_effort",
            "rationale",
        )
    }
    assert runtime.received_effort == "medium"


@pytest.mark.asyncio
async def test_capsule_authority_fingerprint_changes_with_investment_spec() -> None:
    """Different investment authority must produce different durable capsules."""
    low_store, low_events = _capturing_event_store()
    high_store, high_events = _capturing_event_store()
    low_executor = ParallelACExecutor(
        adapter=_EnforcedRuntime(),
        event_store=low_store,
        console=MagicMock(),
        enable_decomposition=False,
        reasoning_effort="high",
    )
    high_executor = ParallelACExecutor(
        adapter=_EnforcedRuntime(),
        event_store=high_store,
        console=MagicMock(),
        enable_decomposition=False,
        reasoning_effort="high",
    )

    await _run_one_ac(
        low_executor,
        is_sub_ac=False,
        investment_spec=InvestmentSpec(
            difficulty="low",
            stakes="low",
            provenance="measured",
            confidence="high",
        ),
    )
    await _run_one_ac(
        high_executor,
        is_sub_ac=False,
        investment_spec=InvestmentSpec(
            difficulty="high",
            stakes="high",
            provenance="declared",
            confidence="high",
        ),
    )

    low_capsule = _capsule_events(low_events)
    high_capsule = _capsule_events(high_events)
    assert len(low_capsule) == len(high_capsule) == 1
    assert low_capsule[0].data["capsule_fingerprint"] != high_capsule[0].data[
        "capsule_fingerprint"
    ]


@pytest.mark.asyncio
async def test_capsule_authority_fingerprint_changes_with_sibling_prompt_scope() -> None:
    first_store, first_events = _capturing_event_store()
    second_store, second_events = _capturing_event_store()
    first_executor = ParallelACExecutor(
        adapter=_EnforcedRuntime(),
        event_store=first_store,
        console=MagicMock(),
        enable_decomposition=False,
    )
    second_executor = ParallelACExecutor(
        adapter=_EnforcedRuntime(),
        event_store=second_store,
        console=MagicMock(),
        enable_decomposition=False,
    )

    await _run_one_ac(
        first_executor,
        is_sub_ac=False,
        sibling_acs=[(0, "Implement the API"), (1, "Implement the CLI")],
    )
    await _run_one_ac(
        second_executor,
        is_sub_ac=False,
        sibling_acs=[(0, "Implement the API"), (1, "Write the deployment docs")],
    )

    first_capsule = _capsule_events(first_events)
    second_capsule = _capsule_events(second_events)
    assert len(first_capsule) == len(second_capsule) == 1
    assert first_capsule[0].data["capsule_fingerprint"] != second_capsule[0].data[
        "capsule_fingerprint"
    ]


@pytest.mark.asyncio
async def test_high_stakes_short_ac_raises_to_high_effort() -> None:
    store, events = _capturing_event_store()
    runtime = _EnforcedRuntime()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        reasoning_effort="low",
    )

    await _run_one_ac(
        executor,
        is_sub_ac=False,
        investment_spec=InvestmentSpec(
            difficulty="low",
            stakes="high",
            provenance="declared",
            confidence="high",
        ),
    )

    assert _effort_events(events)[0].data["effort_level"] == "high"
    assert runtime.received_effort == "high"


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
async def test_provider_cancellation_seals_dispatch_boundary() -> None:
    store, events = _capturing_event_store()
    executor = ParallelACExecutor(
        adapter=_CancelledRuntime(),
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
    )

    with pytest.raises(asyncio.CancelledError):
        await _run_one_ac(executor, is_sub_ac=False)

    sealed = [event for event in events if event.type == "execution.ac.dispatch.sealed"]
    assert len(sealed) == 1
    assert "cancelled" in sealed[0].data["reason"]


@pytest.mark.asyncio
async def test_fresh_runtime_handle_inherits_pre_dispatch_authority() -> None:
    store, events = _capturing_event_store()
    executor = ParallelACExecutor(
        adapter=_FreshHandleRuntime(),
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
    )

    await _run_one_ac(executor, is_sub_ac=False)

    started = next(event for event in events if event.type == "execution.session.started")
    runtime_payload = started.data["runtime"]
    assert runtime_payload["metadata"]["ac_capsule_fingerprint"].startswith("sha256:")
    assert len(runtime_payload["metadata"]["ac_dispatch_id"]) == 32
    assert (
        runtime_payload["metadata"]["process_local_resume_nonce"]
        == executor._process_local_resume_nonce
    )


@pytest.mark.asyncio
async def test_cancellation_seal_failure_is_fail_closed() -> None:
    """Cancellation must surface a seal failure instead of leaving replayable state."""
    store, _events = _capturing_event_store()
    executor = ParallelACExecutor(
        adapter=_CancelledRuntime(),
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
    )
    executor._event_emitter.emit_ac_dispatch_sealed = AsyncMock(
        side_effect=RuntimeError("ledger unavailable")
    )

    with pytest.raises(RuntimeError, match="cancellation seal failed"):
        await _run_one_ac(executor, is_sub_ac=False)


@pytest.mark.asyncio
async def test_effort_event_store_failure_does_not_abort_ac() -> None:
    """A degraded event store degrades the proof event to a warning, not an AC failure.

    The routing event is auxiliary proof telemetry — it is emitted through
    ``_safe_emit_event``, so a persistently failing ``event_store.append`` must NOT
    propagate out of ``_execute_atomic_ac`` and abort the AC before runtime dispatch.
    """
    store = AsyncMock()
    effort_append_attempts = 0

    async def _append(event):
        nonlocal effort_append_attempts
        if getattr(event, "type", None) == "execution.ac.effort_routed":
            effort_append_attempts += 1
            raise RuntimeError("event store unavailable")

    store.append.side_effect = _append
    runtime = _EnforcedRuntime()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        reasoning_effort="high",
    )

    # Must not raise even though the proof-event append fails; the AC still dispatches.
    await _run_one_ac(executor, is_sub_ac=False)

    # The runtime was reached and received the enforced level despite telemetry loss.
    assert runtime.received_effort == "high"
    # The proof append was attempted (and retried) rather than silently skipped.
    assert effort_append_attempts >= 1


@pytest.mark.asyncio
async def test_dormant_effort_still_emits_absent_investment_assessment() -> None:
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
    assessed = _investment_events(events)
    assert len(assessed) == 1
    assert assessed[0].data["provenance"] == "absent"
    assert assessed[0].data["can_cheapen"] is False
    assert assessed[0].data["missing_signals"] == ["difficulty", "stakes"]
