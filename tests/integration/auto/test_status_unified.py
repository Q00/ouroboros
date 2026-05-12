"""Unified auto + ralph status surface (Q00/ouroboros#782).

Pinned cases — see the issue for the full acceptance grid:

1. Happy path: ralph reaches ``qa passed`` → ``session_status`` reflects the
   terminal ralph state and the auto phase is ``COMPLETE``.
2. Cancel path: ``JobManager.cancel_job`` → auto state transitions to
   ``BLOCKED("ralph cancelled by user")`` within one simulated event-bus tick.
3. Gap window: ``ralph_lineage_id`` set but ``ralph_job_id is None`` → status
   reports ``pending: "starting ralph"`` and the phase string ``ralph_handoff``.
4. Plugin delegation: ``ralph_dispatch_mode = "plugin"`` → no job query is
   attempted; the ``ralph`` block carries the operator guidance only.

The integration suite uses real ``EventStore`` / ``JobManager`` in-memory so
the listener is exercised against the same code path that ships to users.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from ouroboros.auto.listeners import (
    RALPH_CANCEL_BLOCKER_REASON,
    apply_event,
    apply_events,
    mirror_ralph_job_events,
)
from ouroboros.auto.state import (
    AutoPhase,
    AutoPipelineState,
    AutoStore,
)
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.tools.query_handlers import SessionStatusHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers — keep state setup compact so each test focuses on one acceptance
# bullet. ``_state_at_run`` lands the state machine inside RALPH_HANDOFF the
# same way the production pipeline does.
# ---------------------------------------------------------------------------


def _state_at_run(tmp_path) -> AutoPipelineState:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.arm_deadline()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_session_id = "interview_stub"
    state.interview_completed = True
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.last_grade = "A"
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    return state


def _state_at_ralph_handoff(tmp_path, *, with_job_id: bool = True) -> AutoPipelineState:
    state = _state_at_run(tmp_path)
    state.ralph_lineage_id = "ralph-seed-test_001-deadbeef"
    state.transition(AutoPhase.RALPH_HANDOFF, "handing off")
    if with_job_id:
        state.ralph_job_id = "job_ralph_001"
        state.ralph_dispatch_mode = "job"
    return state


def _make_job_event(
    event_type: str,
    *,
    job_id: str,
    lineage_id: str,
    status: str | None = None,
    stop_reason: str | None = None,
    error: str | None = None,
    message: str | None = None,
    result_meta: dict[str, Any] | None = None,
) -> BaseEvent:
    payload: dict[str, Any] = {
        "links": {"lineage_id": lineage_id},
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if status is not None:
        payload["status"] = status
    if stop_reason is not None:
        payload["stop_reason"] = stop_reason
    if error is not None:
        payload["error"] = error
    if message is not None:
        payload["message"] = message
    if result_meta is not None:
        payload["result_meta"] = result_meta
    return BaseEvent(
        type=event_type,
        aggregate_type="job",
        aggregate_id=job_id,
        data=payload,
    )


# ---------------------------------------------------------------------------
# 1. Happy path — qa passed terminal status reaches the auto state.
# ---------------------------------------------------------------------------


def test_happy_path_qa_passed_completes_auto(tmp_path) -> None:
    """Ralph completes ⇒ session_status shows COMPLETE + terminal ralph."""
    state = _state_at_ralph_handoff(tmp_path)
    state.transition(AutoPhase.COMPLETE, "ralph loop completed (qa passed)")
    AutoStore(tmp_path).save(state)

    # Listener mirrors the terminal job event onto the persisted state.
    completed_event = _make_job_event(
        "mcp.job.completed",
        job_id=state.ralph_job_id,
        lineage_id=state.ralph_lineage_id,
        status=JobStatus.COMPLETED.value,
        result_meta={
            "status": "completed",
            "stop_reason": "qa passed",
            "iterations": 4,
        },
    )
    assert apply_event(state, completed_event) is True
    AutoStore(tmp_path).save(state)

    handler = SessionStatusHandler(auto_store=AutoStore(tmp_path))
    result = asyncio.run(handler.handle({"session_id": state.auto_session_id}))
    assert result.is_ok
    meta = result.value.meta
    assert meta["phase"] == "complete"
    assert meta["is_terminal"] is True
    ralph = meta["ralph"]
    assert ralph["dispatch_mode"] == "job"
    assert ralph["job_id"] == "job_ralph_001"
    assert ralph["status"] == JobStatus.COMPLETED.value
    assert ralph["stop_reason"] == "qa passed"
    assert ralph["current_generation"] == 4
    assert ralph["lineage_id"] == state.ralph_lineage_id


def test_listener_prefers_terminal_generations_over_iterations(tmp_path) -> None:
    """Terminal metadata must mirror lineage generation, not loop iteration count."""
    state = _state_at_ralph_handoff(tmp_path)

    completed_event = _make_job_event(
        "mcp.job.completed",
        job_id=state.ralph_job_id,
        lineage_id=state.ralph_lineage_id,
        status=JobStatus.COMPLETED.value,
        result_meta={
            "status": "completed",
            "stop_reason": "qa passed",
            "iterations": 2,
            "generations": [9, 10],
        },
    )

    assert apply_event(state, completed_event) is True
    assert state.ralph_current_generation == 10


# ---------------------------------------------------------------------------
# 2. Cancel propagation — cancel within one simulated event tick.
# ---------------------------------------------------------------------------


def test_cancel_propagation_marks_auto_blocked(tmp_path) -> None:
    """A single ``mcp.job.cancelled`` tick blocks the auto session."""
    state = _state_at_ralph_handoff(tmp_path)
    AutoStore(tmp_path).save(state)

    cancel_event = _make_job_event(
        "mcp.job.cancelled",
        job_id=state.ralph_job_id,
        lineage_id=state.ralph_lineage_id,
        status="cancelled",
    )
    applied = apply_events(state, [cancel_event])
    assert applied == 1
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_error == RALPH_CANCEL_BLOCKER_REASON
    assert state.ralph_job_status == "cancelled"


def test_cancel_terminal_after_complete_is_a_noop(tmp_path) -> None:
    """A late cancel must not clobber an already-COMPLETE auto session."""
    state = _state_at_ralph_handoff(tmp_path)
    state.transition(AutoPhase.COMPLETE, "ralph loop completed (qa passed)")

    cancel_event = _make_job_event(
        "mcp.job.cancelled",
        job_id=state.ralph_job_id,
        lineage_id=state.ralph_lineage_id,
        status="cancelled",
    )
    apply_event(state, cancel_event)
    assert state.phase is AutoPhase.COMPLETE
    assert state.last_error is None


@pytest.mark.asyncio
async def test_cancel_via_job_manager_propagates(tmp_path) -> None:
    """End-to-end cancel: ``JobManager.cancel_job`` → auto BLOCKED via listener.

    Wires a real ``JobManager`` against an in-memory event store, replays the
    persisted job events through ``apply_events`` and asserts the listener
    surfaces the cancellation onto the auto state.
    """
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    manager = JobManager(event_store=event_store)

    state = _state_at_ralph_handoff(tmp_path, with_job_id=False)

    async def _runner() -> Any:  # pragma: no cover - cancelled before completion
        await asyncio.sleep(60)

    snapshot = await manager.start_job(
        job_type="ralph",
        initial_message="ralph starting",
        runner=_runner(),
        links=JobLinks(lineage_id=state.ralph_lineage_id),
    )
    state.ralph_job_id = snapshot.job_id
    state.ralph_dispatch_mode = "job"
    AutoStore(tmp_path).save(state)

    # Allow the job to reach RUNNING and emit at least one mcp.job.* event.
    await asyncio.sleep(0.05)
    cancelled_snapshot = await manager.cancel_job(snapshot.job_id)
    assert cancelled_snapshot.status in {
        JobStatus.CANCELLED,
        JobStatus.CANCEL_REQUESTED,
    }

    # ``cancel_job`` cancels the runner Task; the terminal ``mcp.job.cancelled``
    # event is appended by ``_run_job`` once the cancellation propagates. Poll
    # the persisted history for up to ~1s so the integration test does not
    # depend on event-loop scheduling order.
    deadline = asyncio.get_running_loop().time() + 1.0
    persisted_cancelled = False
    while asyncio.get_running_loop().time() < deadline:
        events = await event_store.replay("job", snapshot.job_id)
        if any(ev.type == "mcp.job.cancelled" for ev in events):
            persisted_cancelled = True
            break
        await asyncio.sleep(0.02)
    assert persisted_cancelled, (
        f"expected mcp.job.cancelled in persisted events, got {[ev.type for ev in events]}"
    )

    apply_events(state, events)
    assert state.phase is AutoPhase.BLOCKED
    assert state.last_error == RALPH_CANCEL_BLOCKER_REASON

    await event_store.close()


@pytest.mark.asyncio
async def test_status_mirror_persists_real_job_manager_payloads(tmp_path) -> None:
    """Mirror real ``mcp.job.*`` events while the Ralph job is still running."""
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    manager = JobManager(event_store=event_store)

    auto_store = AutoStore(tmp_path)
    state = _state_at_ralph_handoff(tmp_path, with_job_id=False)
    finished = asyncio.Event()

    async def _runner() -> MCPToolResult:
        await finished.wait()
        return MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="ralph done"),),
            meta={
                "status": "completed",
                "stop_reason": "qa passed",
                "iterations": 4,
            },
        )

    snapshot = await manager.start_job(
        job_type="ralph",
        initial_message="ralph starting",
        runner=_runner(),
        links=JobLinks(lineage_id=state.ralph_lineage_id),
    )
    state.ralph_job_id = snapshot.job_id
    state.ralph_dispatch_mode = "job"
    auto_store.save(state)

    mirror_task = asyncio.create_task(
        mirror_ralph_job_events(
            state,
            auto_store,
            event_store,
            snapshot.job_id,
            poll_interval=0.01,
        )
    )
    await manager.update_status(
        snapshot.job_id,
        JobStatus.RUNNING,
        "Generation 3 | review",
        links=JobLinks(lineage_id=state.ralph_lineage_id),
    )

    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        saved = auto_store.load(state.auto_session_id)
        if saved.ralph_current_generation == 3 and saved.ralph_job_status == "running":
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("status mirror did not persist running generation progress")

    finished.set()
    await asyncio.wait_for(mirror_task, timeout=1.0)
    saved = auto_store.load(state.auto_session_id)
    assert saved.ralph_job_status == "completed"
    assert saved.ralph_stop_reason == "qa passed"
    assert saved.ralph_current_generation == 4

    await event_store.close()


# ---------------------------------------------------------------------------
# 3. Gap window — lineage_id set, no job_id yet.
# ---------------------------------------------------------------------------


def test_gap_window_reports_pending_starting_ralph(tmp_path) -> None:
    """``ouroboros_session_status`` surfaces the gap window explicitly."""
    state = _state_at_ralph_handoff(tmp_path, with_job_id=False)
    AutoStore(tmp_path).save(state)

    handler = SessionStatusHandler(auto_store=AutoStore(tmp_path))
    result = asyncio.run(handler.handle({"session_id": state.auto_session_id}))
    assert result.is_ok
    meta = result.value.meta
    assert meta["phase"] == "ralph_handoff"
    assert meta["pending"] == "starting ralph"
    # No ``ralph`` block — there is nothing to mirror yet.
    assert "ralph" not in meta
    text = result.value.content[0].text
    assert "Pending: starting ralph" in text


def test_plugin_pending_reports_interrupted_dispatch_not_starting_gap(tmp_path) -> None:
    """Unconfirmed plugin dispatch is distinct from the normal startup gap."""
    state = _state_at_ralph_handoff(tmp_path, with_job_id=False)
    state.ralph_dispatch_mode = "plugin_pending"
    AutoStore(tmp_path).save(state)

    handler = SessionStatusHandler(auto_store=AutoStore(tmp_path))
    result = asyncio.run(handler.handle({"session_id": state.auto_session_id}))
    assert result.is_ok
    meta = result.value.meta
    assert meta["phase"] == "ralph_handoff"
    assert "pending" not in meta
    assert meta["ralph"] == {
        "dispatch_mode": "plugin_pending",
        "lineage_id": state.ralph_lineage_id,
        "status": "interrupted_plugin_dispatch",
        "guidance": "plugin dispatch unconfirmed; resume will retry or block",
    }
    text = result.value.content[0].text
    assert "Pending: starting ralph" not in text
    assert "dispatch_mode: plugin_pending" in text
    assert "status: interrupted_plugin_dispatch" in text


def test_cli_plugin_pending_reports_interrupted_dispatch_not_starting_gap(tmp_path) -> None:
    """CLI mirrors the MCP distinction for plugin_pending checkpoints."""
    from ouroboros.cli.commands.status import _format_auto_status

    state = _state_at_ralph_handoff(tmp_path, with_job_id=False)
    state.ralph_dispatch_mode = "plugin_pending"

    rendered = _format_auto_status(state)

    assert "Ralph (plugin pending):" in rendered
    assert "dispatch_mode: plugin_pending" in rendered
    assert "status: interrupted plugin dispatch" in rendered
    assert "guidance: plugin dispatch unconfirmed; resume will retry or block" in rendered
    assert "Ralph (pending):" not in rendered
    assert "pending: starting ralph" not in rendered


@pytest.mark.parametrize("phase", (AutoPhase.BLOCKED, AutoPhase.FAILED))
def test_terminal_lineage_without_job_id_is_not_gap_window(tmp_path, phase: AutoPhase) -> None:
    """Terminal auto state must not be masked as a pending Ralph handoff."""
    state = _state_at_ralph_handoff(tmp_path, with_job_id=False)
    if phase is AutoPhase.BLOCKED:
        state.mark_blocked("ralph handoff failed", tool_name="ralph_starter")
    else:
        state.mark_failed("ralph handoff failed", tool_name="ralph_starter")
    AutoStore(tmp_path).save(state)

    handler = SessionStatusHandler(auto_store=AutoStore(tmp_path))
    result = asyncio.run(handler.handle({"session_id": state.auto_session_id}))
    assert result.is_ok
    meta = result.value.meta
    assert meta["phase"] == phase.value
    assert meta["is_terminal"] is True
    assert "pending" not in meta
    assert "ralph" not in meta
    assert "Pending: starting ralph" not in result.value.content[0].text


@pytest.mark.parametrize("phase", (AutoPhase.BLOCKED, AutoPhase.FAILED))
def test_cli_terminal_lineage_without_job_id_is_not_gap_window(tmp_path, phase: AutoPhase) -> None:
    """CLI pending gap warning is only valid during RALPH_HANDOFF."""
    from ouroboros.cli.commands.status import _format_auto_status

    state = _state_at_ralph_handoff(tmp_path, with_job_id=False)
    if phase is AutoPhase.BLOCKED:
        state.mark_blocked("ralph handoff failed", tool_name="ralph_starter")
    else:
        state.mark_failed("ralph handoff failed", tool_name="ralph_starter")

    rendered = _format_auto_status(state)
    assert "Phase: " + phase.value in rendered
    assert "Ralph (pending):" not in rendered
    assert "pending: starting ralph" not in rendered


# ---------------------------------------------------------------------------
# 4. Plugin delegation — no job query is attempted.
# ---------------------------------------------------------------------------


def test_plugin_dispatch_mode_no_job_query(tmp_path) -> None:
    """Plugin delegation surfaces guidance and skips the job lookup entirely."""
    state = _state_at_ralph_handoff(tmp_path, with_job_id=False)
    state.ralph_dispatch_mode = "plugin"
    state.transition(AutoPhase.COMPLETE, "ralph loop delegated")
    AutoStore(tmp_path).save(state)

    handler = SessionStatusHandler(auto_store=AutoStore(tmp_path))
    result = asyncio.run(handler.handle({"session_id": state.auto_session_id}))
    assert result.is_ok
    meta = result.value.meta
    assert meta["phase"] == "complete"
    ralph = meta["ralph"]
    assert ralph == {
        "dispatch_mode": "plugin",
        "guidance": "ralph delegated to OpenCode Task widget; follow that lifecycle",
    }

    # The listener must refuse to apply events while the dispatch mode is plugin
    # so we never accidentally mirror an unrelated event onto a delegated session.
    plugin_event = _make_job_event(
        "mcp.job.completed",
        job_id="job_other",
        lineage_id=state.ralph_lineage_id,
        status="completed",
    )
    assert apply_event(state, plugin_event) is False


# ---------------------------------------------------------------------------
# 5. CLI snapshot — pin layout (covers the ``ooo status auto`` rendering).
# ---------------------------------------------------------------------------


def test_cli_status_auto_snapshot(tmp_path) -> None:
    """Pin ``ooo status auto`` text layout for the unified surface."""
    from ouroboros.cli.commands.status import _format_auto_status

    state = AutoPipelineState(
        goal="Build a CLI",
        cwd=str(tmp_path),
        auto_session_id="auto_pinned00001",
    )
    state.arm_deadline()
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.interview_completed = True
    state.transition(AutoPhase.SEED_GENERATION, "seed")
    state.last_grade = "A"
    state.transition(AutoPhase.REVIEW, "review")
    state.transition(AutoPhase.RUN, "run")
    state.ralph_lineage_id = "ralph-seed-test_001-pinned00"
    state.transition(AutoPhase.RALPH_HANDOFF, "handing off")
    state.ralph_job_id = "job_pinned"
    state.ralph_dispatch_mode = "job"
    state.ralph_job_status = "running"
    state.ralph_current_generation = 3
    state.transition(AutoPhase.COMPLETE, "ralph loop completed (qa passed)")
    state.ralph_stop_reason = "qa passed"

    rendered = _format_auto_status(state)
    expected = (
        "Auto status\n"
        "===========\n"
        "Auto session: auto_pinned00001\n"
        "Phase: complete\n"
        "Terminal: True\n"
        "Last progress: ralph loop completed (qa passed)\n"
        "Ralph (job):\n"
        "  job_id: job_pinned\n"
        "  lineage_id: ralph-seed-test_001-pinned00\n"
        "  status: running\n"
        "  current_generation: 3\n"
        "  stop_reason: qa passed\n"
    )
    assert rendered == expected
