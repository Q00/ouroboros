"""Tests for async MCP job management."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import os
from unittest.mock import AsyncMock, patch

from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.events.lineage import lineage_generation_watchdog_decision
from ouroboros.mcp import job_manager as job_manager_module
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobSnapshot, JobStatus
from ouroboros.mcp.tools.job_handlers import (
    JobResultHandler,
    JobStatusHandler,
    JobWaitHandler,
    _render_compact_job_snapshot,
    _render_job_snapshot,
    _render_job_snapshot_inner,
)
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.agent_process import AgentProcessHandle
from ouroboros.orchestrator.heartbeat import acquire as acquire_session_lock
from ouroboros.orchestrator.heartbeat import lock_path
from ouroboros.orchestrator.heartbeat import release as release_session_lock
from ouroboros.orchestrator.runner import clear_cancellation, is_cancellation_requested
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.checkpoint import CheckpointStore
from ouroboros.persistence.event_store import EventStore, PersistenceError


def _build_store(tmp_path) -> EventStore:
    db_path = tmp_path / "jobs.db"
    return EventStore(f"sqlite+aiosqlite:///{db_path}")


async def _cancel_manager_tasks(manager: JobManager) -> None:
    tasks = [
        *manager._tasks.values(),
        *manager._runner_tasks.values(),
        *manager._monitors.values(),
    ]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _ok_runner() -> MCPToolResult:
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
        is_error=False,
    )


async def _wait_for_job_status(
    manager: JobManager,
    job_id: str,
    status: JobStatus,
    *,
    timeout: float = 1.0,
) -> JobSnapshot:
    deadline = asyncio.get_running_loop().time() + timeout
    last_snapshot: JobSnapshot | None = None
    while asyncio.get_running_loop().time() < deadline:
        last_snapshot = await manager.get_snapshot(job_id)
        if last_snapshot.status is status:
            return last_snapshot
        await asyncio.sleep(0.01)
    if last_snapshot is None:
        last_snapshot = await manager.get_snapshot(job_id)
    raise AssertionError(f"job {job_id} did not become {status}; last={last_snapshot.status}")


class TestJobManager:
    """Test background job lifecycle behavior."""

    async def test_status_message_reports_generation_watchdog_decision(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:
            await store.append(
                lineage_generation_watchdog_decision(
                    "lin_watchdog",
                    3,
                    "timeout",
                    "Generation had no material progress for 14400.0s",
                )
            )
            now = datetime.now(UTC)
            snapshot = JobSnapshot(
                job_id="job_watchdog",
                job_type="evolve_step",
                status=JobStatus.RUNNING,
                message="Running evolve_step",
                created_at=now,
                updated_at=now,
                links=JobLinks(lineage_id="lin_watchdog"),
            )

            message = await manager._derive_status_message(snapshot)

            expected = (
                "Generation 3 watchdog timeout | Generation had no material progress for 14400.0s"
            )
            assert message == expected
        finally:
            await store.close()

    async def test_render_job_snapshot_reports_generation_watchdog_decision(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        await store.initialize()

        try:
            await store.append(
                lineage_generation_watchdog_decision(
                    "lin_watchdog_render",
                    4,
                    "timeout",
                    "Generation idle for 7200.0s",
                )
            )
            snapshot = JobSnapshot(
                job_id="job_watchdog_render",
                job_type="evolve_step",
                status=JobStatus.RUNNING,
                message="Running evolve_step",
                created_at=datetime(2026, 4, 22, tzinfo=UTC),
                updated_at=datetime(2026, 4, 22, tzinfo=UTC),
                links=JobLinks(lineage_id="lin_watchdog_render"),
            )

            text, _ = await _render_job_snapshot_inner(snapshot, store)

            assert "**Current Step**: Gen 4 watchdog timeout" in text
            assert "**Reason**: Generation idle for 7200.0s" in text
        finally:
            await store.close()

    async def test_monitor_completes_job_when_execution_terminal_is_complete(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            stop = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await stop.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late done"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_complete", execution_id="exec_complete"),
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_complete",
                    data={
                        "completed_count": 2,
                        "total_count": 2,
                        "current_phase": "Deliver",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_complete",
                    data={"session_id": "orch_complete", "status": "completed"},
                )
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.COMPLETED, timeout=2.0
            )

            assert snapshot.result_text == "Execution complete: 2/2 ACs completed"
            assert snapshot.message == "Execution complete; formal evaluation not run"
            assert snapshot.result_meta["completed_from_execution_terminal"] is True
            assert snapshot.result_meta["evaluated"] is False
            assert snapshot.result_meta["verification_status"] == "executed_unverified"
            assert snapshot.result_meta["formal_evaluation_required"] is True
            assert snapshot.result_meta["next_step"] == "ooo evaluate orch_complete"
            events, _ = await store.get_events_after("job", started.job_id, last_row_id=0)
            terminal_events = [
                event
                for event in events
                if event.type
                in {
                    "mcp.job.completed",
                    "mcp.job.failed",
                    "mcp.job.cancelled",
                    "mcp.job.interrupted",
                }
            ]
            assert [event.type for event in terminal_events] == ["mcp.job.completed"]
        finally:
            stop.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_runner_exception_is_not_masked_by_execution_completion(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_runner_failed_after_terminal",
                    data={"session_id": "orch_runner_failed", "status": "completed"},
                )
            )

            async def _runner() -> MCPToolResult:
                raise RuntimeError("post-processing failed")

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(
                    session_id="orch_runner_failed",
                    execution_id="exec_runner_failed_after_terminal",
                ),
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.FAILED, timeout=2.0
            )

            assert "post-processing failed" in (snapshot.error or "")
            assert snapshot.result_meta.get("completed_from_execution_terminal") is not True
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_runner_failure_terminal_append_persistent_failure_persists_fallback_failed(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:
            original_append_event = manager._append_event
            failed_attempts = 0

            async def _fail_terminal_append(
                event_type: str, job_id: str, data: dict, **kwargs
            ) -> None:
                nonlocal failed_attempts
                # Persistently fail the ORIGINAL failed-event append (initial try
                # plus the fallback's single retry) so the FAILED fallback with
                # diagnostic meta is exercised. The fallback's own append carries
                # terminal_append_failed meta and is allowed through.
                if event_type == "mcp.job.failed" and not data.get("result_meta", {}).get(
                    "terminal_append_failed"
                ):
                    failed_attempts += 1
                    raise PersistenceError("synthetic terminal append failure", operation="insert")
                await original_append_event(event_type, job_id, data, **kwargs)

            manager._append_event = _fail_terminal_append

            async def _runner() -> MCPToolResult:
                raise RuntimeError("runner boom")

            started = await manager.start_job(
                job_type="test",
                initial_message="queued",
                runner=_runner(),
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.FAILED, timeout=2.0
            )

            assert failed_attempts == 2  # initial append + one fallback retry
            assert snapshot.result_meta["terminal_append_failed"] is True
            assert snapshot.result_meta["original_event_type"] == "mcp.job.failed"
            assert snapshot.result_meta["original_status"] == JobStatus.FAILED.value
            assert "synthetic terminal append failure" in (snapshot.error or "")
            assert snapshot.status is not JobStatus.RUNNING
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_runner_success_terminal_append_persistent_failure_persists_fallback_failed(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:
            original_append_event = manager._append_event
            failed_attempts = 0

            async def _fail_completed_append(
                event_type: str, job_id: str, data: dict, **kwargs
            ) -> None:
                nonlocal failed_attempts
                if event_type == "mcp.job.completed":
                    failed_attempts += 1
                    raise PersistenceError("synthetic completed append failure", operation="insert")
                await original_append_event(event_type, job_id, data, **kwargs)

            manager._append_event = _fail_completed_append

            async def _runner() -> MCPToolResult:
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="runner ok"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="test",
                initial_message="queued",
                runner=_runner(),
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.FAILED, timeout=2.0
            )

            assert failed_attempts == 2  # initial append + one fallback retry
            assert snapshot.result_meta["terminal_append_failed"] is True
            assert snapshot.result_meta["original_event_type"] == "mcp.job.completed"
            assert snapshot.result_meta["original_status"] == JobStatus.COMPLETED.value
            assert "synthetic completed append failure" in (snapshot.error or "")
            assert snapshot.status is not JobStatus.RUNNING
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_run_job_never_strands_running_when_all_terminal_appends_fail(
        self, tmp_path
    ) -> None:
        """Residual #1566 zombie: when EVERY terminal append (the primary
        completed AND the FAILED fallback) fails, the best-effort helpers
        swallow the error and ``_run_job`` returns normally with no terminal
        event and no exception — stranding the job RUNNING forever. The
        finally-block durability backstop must still persist a terminal state.

        Fails on main (job never leaves RUNNING); passes with the backstop.
        """
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        _terminal_types = {
            "mcp.job.completed",
            "mcp.job.failed",
            "mcp.job.cancelled",
            "mcp.job.interrupted",
        }

        try:
            original_append_event = manager._append_event
            failed_terminal_appends = 0

            async def _fail_every_terminal_append_except_backstop(
                event_type: str, job_id: str, data: dict, **kwargs
            ) -> None:
                nonlocal failed_terminal_appends
                result_meta = data.get("result_meta")
                # The finally-block backstop is the ONLY terminal writer allowed
                # through — it is distinguished by the ``ensure_reason`` sentinel
                # that _ensure_terminal_event_best_effort stamps. Everything the
                # try body attempts (completed + fallback FAILED) fails, exactly
                # like a store under sustained "database is locked" pressure.
                is_backstop = isinstance(result_meta, dict) and "ensure_reason" in result_meta
                if event_type in _terminal_types and not is_backstop:
                    failed_terminal_appends += 1
                    raise PersistenceError(
                        "synthetic sustained terminal append failure", operation="insert"
                    )
                await original_append_event(event_type, job_id, data, **kwargs)

            manager._append_event = _fail_every_terminal_append_except_backstop

            async def _runner() -> MCPToolResult:
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="runner ok"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="test",
                initial_message="queued",
                runner=_runner(),
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.FAILED, timeout=2.0
            )

            # The try body burned its primary + fallback terminal appends...
            assert failed_terminal_appends >= 2
            # ...and yet the job is terminal, persisted by the finally backstop.
            assert snapshot.status is JobStatus.FAILED
            assert snapshot.result_meta.get("ensure_reason")
            assert snapshot.result_meta.get("terminal_append_failed") is True
            assert snapshot.message == "Job terminalized by last-resort guard"

            # Durable across a fresh manager (no live tasks): the terminal row
            # is genuinely persisted, not an in-memory recovery artifact.
            recovered = JobManager(store)
            recovered_snapshot = await recovered.get_snapshot(started.job_id)
            assert recovered_snapshot.status is JobStatus.FAILED
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_hostile_repeated_cancellation_cannot_strand_job_without_terminal_event(
        self, tmp_path
    ) -> None:
        """PR #1576's own CI run refuted the inline-backstop theory: every
        inline net in ``_run_job`` (the except guards, the ensure helper, an
        inline finally backstop) awaits while catching only ``Exception``, so a
        fresh CancelledError delivered at EVERY await point silently defeats
        them all — the finally pops the tasks and nothing is persisted. The
        detached release-point backstop must survive that hostile interleaving.

        Fails before the detached backstop (job stranded RUNNING); passes with it.
        """
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        release = asyncio.Event()

        async def _runner() -> MCPToolResult:
            await release.wait()
            return MCPToolResult()

        try:
            started = await manager.start_job(
                job_type="test",
                initial_message="queued",
                runner=_runner(),
            )
            await _wait_for_job_status(manager, started.job_id, JobStatus.RUNNING, timeout=2.0)

            task = manager._tasks[started.job_id]
            # Hostile canceller: deliver a fresh CancelledError at every await
            # point until the job task dies. Each cancel() re-arms delivery at
            # the task's next suspension, so no inline except/finally net can
            # complete an awaited persistence step.
            while not task.done():
                task.cancel()
                await asyncio.sleep(0)
            assert started.job_id not in manager._tasks

            deadline = asyncio.get_running_loop().time() + 2.0
            snapshot = await manager.get_snapshot(started.job_id)
            while not snapshot.is_terminal and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
                snapshot = await manager.get_snapshot(started.job_id)

            assert snapshot.is_terminal, f"job stranded in {snapshot.status}"
            # Written by the detached backstop with cancel semantics — the
            # hostile interleaving is a cancellation, not a failure.
            assert snapshot.status is JobStatus.CANCELLED
        finally:
            release.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_stranded_started_job_is_reconciled_by_get_snapshot(self, tmp_path) -> None:
        """Exact CI signature of the #1566/#1576 zombie: the job task fully
        released (tasks popped, stream = created+running, nothing raised) —
        modeled by silently dropping every terminal append while the job task
        lives, a superset of any silent-loss mechanism. The in-process
        stranded-job net in get_snapshot must terminalize it on the next poll.

        Fails without the net (RUNNING forever: the linked-execution reconciler
        has no execution.terminal evidence and the dead-owner reconciler sees a
        live owner); passes with it.
        """
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        _terminal_types = {
            "mcp.job.completed",
            "mcp.job.failed",
            "mcp.job.cancelled",
            "mcp.job.interrupted",
        }
        dropping = True
        original_append_event = manager._append_event

        async def _drop_terminal_appends(
            event_type: str, job_id: str, data: dict, **kwargs
        ) -> None:
            if dropping and event_type in _terminal_types:
                return  # silently lost: no event persisted, no exception raised
            await original_append_event(event_type, job_id, data, **kwargs)

        manager._append_event = _drop_terminal_appends

        try:
            started = await manager.start_job(
                job_type="test",
                initial_message="queued",
                runner=_ok_runner(),
            )
            job_task = manager._tasks.get(started.job_id)
            if job_task is not None:
                await asyncio.wait({job_task}, timeout=2.0)
            # Let the detached release-point backstop finish too (its appends
            # are also dropped) so the loss is total, like the CI capture.
            deadline = asyncio.get_running_loop().time() + 2.0
            while (
                getattr(manager, "_backstops", {}) and asyncio.get_running_loop().time() < deadline
            ):
                await asyncio.sleep(0)

            # The CI signature: tasks released, no terminal event persisted.
            assert started.job_id not in manager._tasks
            assert started.job_id not in manager._runner_tasks
            events, _ = await store.get_events_after("job", started.job_id, last_row_id=0)
            assert events
            assert all(e.type in {"mcp.job.created", "mcp.job.updated"} for e in events)

            dropping = False
            deadline = asyncio.get_running_loop().time() + 2.0
            snapshot = await manager.get_snapshot(started.job_id)
            while not snapshot.is_terminal and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
                snapshot = await manager.get_snapshot(started.job_id)

            assert snapshot.status is JobStatus.INTERRUPTED, f"job stranded in {snapshot.status}"
            assert snapshot.result_meta.get("interrupted_from_stranded_job_task") is True

            # Durable and idempotent: one terminal event, visible to a fresh manager.
            recovered = JobManager(store)
            assert (await recovered.get_snapshot(started.job_id)).status is JobStatus.INTERRUPTED
            events, _ = await store.get_events_after("job", started.job_id, last_row_id=0)
            assert sum(1 for e in events if e.type == "mcp.job.interrupted") == 1
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_runner_success_transient_append_failure_recovers_original_completed(
        self, tmp_path
    ) -> None:
        """One transient append failure must NOT downgrade a completed job to FAILED."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:
            original_append_event = manager._append_event
            failed_attempts = 0

            async def _fail_first_completed_append(
                event_type: str, job_id: str, data: dict, **kwargs
            ) -> None:
                nonlocal failed_attempts
                if event_type == "mcp.job.completed" and failed_attempts == 0:
                    failed_attempts += 1
                    raise PersistenceError("synthetic completed append failure", operation="insert")
                await original_append_event(event_type, job_id, data, **kwargs)

            manager._append_event = _fail_first_completed_append

            async def _runner() -> MCPToolResult:
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="runner ok"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="test",
                initial_message="queued",
                runner=_runner(),
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.COMPLETED, timeout=2.0
            )

            assert failed_attempts == 1
            assert snapshot.status is JobStatus.COMPLETED
            assert "terminal_append_failed" not in snapshot.result_meta
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_crash_inside_terminalization_still_persists_terminal_state(
        self, tmp_path
    ) -> None:
        """If _run_job's own terminal-writing logic crashes (not the append),
        the job must still end terminal instead of stranding in RUNNING."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:

            async def _boom_derivation(snapshot: JobSnapshot) -> str | None:
                raise PersistenceError("synthetic derivation crash", operation="select")

            manager._derive_completed_execution_result = _boom_derivation

            started = await manager.start_job(
                job_type="test",
                initial_message="queued",
                runner=_ok_runner(),
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.FAILED, timeout=2.0
            )

            assert snapshot.status is JobStatus.FAILED
            assert snapshot.result_meta["terminal_append_failed"] is True
            assert "synthetic derivation crash" in (snapshot.error or "")
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_terminal_fallback_skips_when_job_already_terminal(self, tmp_path) -> None:
        """The FAILED fallback must not overwrite a terminal event a concurrent writer persisted."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:
            started = await manager.start_job(
                job_type="test",
                initial_message="queued",
                runner=_ok_runner(),
            )
            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.COMPLETED, timeout=2.0
            )
            assert snapshot.status is JobStatus.COMPLETED

            async def _always_fail_retry() -> None:
                raise PersistenceError("retry still failing", operation="insert")

            await manager._append_terminal_fallback_event(
                started.job_id,
                original_event_type="mcp.job.cancelled",
                original_data={"status": JobStatus.CANCELLED.value, "message": "x"},
                append_error=PersistenceError("initial append failed", operation="insert"),
                retry_append=_always_fail_retry,
            )

            snapshot = await manager.get_snapshot(started.job_id)
            assert snapshot.status is JobStatus.COMPLETED
            assert "terminal_append_failed" not in snapshot.result_meta
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_error_result_is_not_masked_by_execution_completion(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_tool_error_after_terminal",
                    data={"session_id": "orch_tool_error", "status": "completed"},
                )
            )

            async def _runner() -> MCPToolResult:
                return MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text="handler failed after execution terminal",
                        ),
                    ),
                    is_error=True,
                )

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(
                    session_id="orch_tool_error",
                    execution_id="exec_tool_error_after_terminal",
                ),
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.FAILED, timeout=2.0
            )

            assert snapshot.result_text == "handler failed after execution terminal"
            assert snapshot.result_meta.get("completed_from_execution_terminal") is not True
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_execution_terminal_completion_preserves_success_result_meta(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:
            cancelled = False

            async def _runner() -> MCPToolResult:
                nonlocal cancelled
                await store.append(
                    BaseEvent(
                        type="execution.terminal",
                        aggregate_type="execution",
                        aggregate_id="exec_chain_meta",
                        data={"session_id": "orch_chain_meta", "status": "completed"},
                    )
                )
                try:
                    await asyncio.sleep(1.2)
                    return MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text="run complete"),),
                        is_error=False,
                        meta={
                            "success": True,
                            "verification_status": "evaluation_enqueued",
                            "chained_evaluate_job_id": "job_eval_123",
                            "evaluation_status": "enqueued",
                            "next_step": "ouroboros_job_wait job_eval_123",
                        },
                    )
                except asyncio.CancelledError:
                    cancelled = True
                    raise

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(
                    session_id="orch_chain_meta",
                    execution_id="exec_chain_meta",
                    preserve_runner_result=True,
                ),
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.COMPLETED, timeout=2.0
            )

            assert snapshot.result_meta["completed_from_execution_terminal"] is True
            assert snapshot.result_meta["verification_status"] == "evaluation_enqueued"
            assert snapshot.result_meta["chained_evaluate_job_id"] == "job_eval_123"
            assert snapshot.result_meta["evaluation_status"] == "enqueued"
            assert snapshot.result_meta["next_step"] == "ouroboros_job_wait job_eval_123"
            assert snapshot.message == "Execution complete; formal evaluation enqueued"
            assert cancelled is False
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_monitor_completion_does_not_mask_cancel_cleanup_error_result(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT,
                                text="cancel cleanup returned failure",
                            ),
                        ),
                        is_error=True,
                    )

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(
                    session_id="orch_monitor_error_result",
                    execution_id="exec_monitor_error_result",
                ),
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_monitor_error_result",
                    data={"session_id": "orch_monitor_error_result", "status": "completed"},
                )
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.FAILED, timeout=3.0
            )

            assert snapshot.result_text == "cancel cleanup returned failure"
            assert snapshot.result_meta.get("completed_from_execution_terminal") is not True
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_monitor_fails_job_when_deliver_progress_stays_zero_after_ac_success(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            stop = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await stop.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late done"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_stalled", execution_id="exec_stalled"),
            )

            original_append_event = manager._append_event

            async def _assert_failure_persists_before_teardown(
                event_type: str, job_id: str, data: dict, **kwargs
            ) -> None:
                if event_type == "mcp.job.failed":
                    runner_task = manager._runner_tasks[job_id]
                    job_task = manager._tasks[job_id]
                    assert not runner_task.cancelled()
                    assert not job_task.cancelled()
                await original_append_event(event_type, job_id, data, **kwargs)

            manager._append_event = _assert_failure_persists_before_teardown

            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_stalled",
                    data={
                        "completed_count": 0,
                        "total_count": 23,
                        "current_phase": "Deliver",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.session.completed",
                    aggregate_type="execution",
                    aggregate_id="exec_stalled_ac_1",
                    data={
                        "execution_id": "exec_stalled",
                        "session_id": "child_1",
                        "session_scope_id": "exec_stalled_ac_1",
                        "success": True,
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_stalled",
                    data={"session_id": "orch_stalled", "status": "failed"},
                )
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.FAILED, timeout=2.0
            )

            assert "workflow progress accounting stalled" in (snapshot.error or "")
            assert snapshot.result_meta["failed_from_progress_accounting_stall"] is True
        finally:
            stop.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_progress_accounting_append_failure_retries_terminalization(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            stop = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await stop.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late done"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_append_fail", execution_id="exec_append_fail"),
            )

            original_append_event = manager._append_event
            failed_attempts = 0

            async def _fail_first_terminal_failure_append(
                event_type: str, job_id: str, data: dict, **kwargs
            ) -> None:
                nonlocal failed_attempts
                if event_type == "mcp.job.failed" and failed_attempts == 0:
                    failed_attempts += 1
                    raise PersistenceError("synthetic append failure", operation="insert")
                await original_append_event(event_type, job_id, data, **kwargs)

            manager._append_event = _fail_first_terminal_failure_append

            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_append_fail",
                    data={
                        "session_id": "orch_append_fail",
                        "completed_count": 0,
                        "total_count": 1,
                        "current_phase": "Deliver",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.session.completed",
                    aggregate_type="execution",
                    aggregate_id="exec_append_fail_ac_1",
                    data={
                        "execution_id": "exec_append_fail",
                        "session_id": "child_1",
                        "session_scope_id": "exec_append_fail_ac_1",
                        "success": True,
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_append_fail",
                    data={"session_id": "orch_append_fail", "status": "failed"},
                )
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.FAILED, timeout=3.0
            )

            assert failed_attempts == 1
            assert snapshot.result_meta["failed_from_progress_accounting_stall"] is True
        finally:
            stop.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_progress_accounting_failure_keeps_runner_tracked_during_cancel_cleanup(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            cleanup_started = asyncio.Event()
            cleanup_release = asyncio.Event()

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleanup_started.set()
                    await cleanup_release.wait()
                    raise
                raise AssertionError("runner should only exit through cancellation")

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_cleanup", execution_id="exec_cleanup"),
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_cleanup",
                    data={
                        "session_id": "orch_cleanup",
                        "completed_count": 0,
                        "total_count": 1,
                        "current_phase": "Deliver",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.session.completed",
                    aggregate_type="execution",
                    aggregate_id="exec_cleanup_ac_1",
                    data={
                        "execution_id": "exec_cleanup",
                        "session_id": "child_1",
                        "session_scope_id": "exec_cleanup_ac_1",
                        "success": True,
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_cleanup",
                    data={"session_id": "orch_cleanup", "status": "failed"},
                )
            )

            await asyncio.wait_for(cleanup_started.wait(), timeout=2.0)

            assert started.job_id in manager._runner_tasks

            cleanup_release.set()
            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.FAILED, timeout=2.0
            )

            assert snapshot.result_meta["failed_from_progress_accounting_stall"] is True
        finally:
            cleanup_release.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_progress_accounting_failure_detaches_runner_after_cancel_grace_timeout(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            cleanup_started = asyncio.Event()
            cleanup_release = asyncio.Event()

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleanup_started.set()
                    await cleanup_release.wait()
                    raise
                raise AssertionError("runner should only exit through cancellation")

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(
                    session_id="orch_cleanup_timeout", execution_id="exec_cleanup_timeout"
                ),
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_cleanup_timeout",
                    data={
                        "session_id": "orch_cleanup_timeout",
                        "completed_count": 0,
                        "total_count": 1,
                        "current_phase": "Deliver",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.session.completed",
                    aggregate_type="execution",
                    aggregate_id="exec_cleanup_timeout_ac_1",
                    data={
                        "execution_id": "exec_cleanup_timeout",
                        "session_id": "child_1",
                        "session_scope_id": "exec_cleanup_timeout_ac_1",
                        "success": True,
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_cleanup_timeout",
                    data={"session_id": "orch_cleanup_timeout", "status": "failed"},
                )
            )

            with patch.object(
                job_manager_module,
                "_COMPLETED_EXECUTION_CANCEL_GRACE_SECONDS",
                0.05,
            ):
                await asyncio.wait_for(cleanup_started.wait(), timeout=2.0)
                await _wait_for_job_status(manager, started.job_id, JobStatus.FAILED, timeout=2.0)
                deadline = asyncio.get_running_loop().time() + 1.0
                while started.job_id in manager._runner_tasks:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise AssertionError("runner task was not detached after grace timeout")
                    await asyncio.sleep(0.01)

            assert started.job_id not in manager._runner_tasks
        finally:
            cleanup_release.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_snapshot_recovers_progress_accounting_failed_terminal_after_restart(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        writer = JobManager(store)

        try:
            await writer._append_event(
                "mcp.job.created",
                "job_recover_failed",
                {
                    "job_type": "execute_seed",
                    "status": JobStatus.RUNNING.value,
                    "message": "Running execute_seed",
                    "links": {
                        "session_id": "orch_recover_failed",
                        "execution_id": "exec_recover_failed",
                        "lineage_id": None,
                    },
                },
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_recover_failed",
                    data={
                        "session_id": "orch_recover_failed",
                        "completed_count": 0,
                        "total_count": 1,
                        "current_phase": "Deliver",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.session.completed",
                    aggregate_type="execution",
                    aggregate_id="exec_recover_failed_ac_1",
                    data={
                        "execution_id": "exec_recover_failed",
                        "session_id": "child_1",
                        "session_scope_id": "exec_recover_failed_ac_1",
                        "success": True,
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_recover_failed",
                    data={"session_id": "orch_recover_failed", "status": "failed"},
                )
            )

            restarted = JobManager(store)

            snapshot = await restarted.get_snapshot("job_recover_failed")

            assert snapshot.status is JobStatus.FAILED
            assert snapshot.result_meta["failed_from_progress_accounting_stall"] is True
            assert "workflow progress accounting stalled" in (snapshot.error or "")
            events, _ = await store.get_events_after("job", "job_recover_failed", last_row_id=0)
            assert [event.type for event in events] == ["mcp.job.created", "mcp.job.failed"]
        finally:
            await store.close()

    async def test_progress_accounting_blocker_waits_for_active_ac_sessions_after_terminal(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:
            snapshot = JobSnapshot(
                job_id="job_parallel",
                job_type="execute_seed",
                status=JobStatus.RUNNING,
                message="Running execute_seed",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                links=JobLinks(
                    session_id="orch_parallel_active", execution_id="exec_parallel_active"
                ),
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_parallel_active",
                    data={
                        "session_id": "orch_parallel_active",
                        "completed_count": 0,
                        "total_count": 2,
                        "current_phase": "Deliver",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.session.completed",
                    aggregate_type="execution",
                    aggregate_id="exec_parallel_active_ac_1",
                    data={
                        "execution_id": "exec_parallel_active",
                        "session_id": "child_1",
                        "session_scope_id": "exec_parallel_active_ac_1",
                        "success": True,
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="exec_parallel_active_ac_2",
                    data={
                        "execution_id": "exec_parallel_active",
                        "session_id": "child_2",
                        "session_scope_id": "exec_parallel_active_ac_2",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_parallel_active",
                    data={"session_id": "orch_parallel_active", "status": "failed"},
                )
            )

            assert await manager._derive_progress_accounting_blocker(snapshot) is None

            await store.append(
                BaseEvent(
                    type="execution.session.failed",
                    aggregate_type="execution",
                    aggregate_id="exec_parallel_active_ac_2",
                    data={
                        "execution_id": "exec_parallel_active",
                        "session_id": "child_2",
                        "session_scope_id": "exec_parallel_active_ac_2",
                        "success": False,
                    },
                )
            )

            blocker = await manager._derive_progress_accounting_blocker(snapshot)

            assert blocker is not None
            assert "all known AC runtime sessions were terminal" in blocker
        finally:
            await store.close()

    async def test_progress_accounting_blocker_requires_failed_execution_terminal(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:
            snapshot = JobSnapshot(
                job_id="job_parallel",
                job_type="execute_seed",
                status=JobStatus.RUNNING,
                message="Running execute_seed",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                links=JobLinks(session_id="orch_parallel", execution_id="exec_parallel"),
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_parallel",
                    data={
                        "session_id": "orch_parallel",
                        "completed_count": 0,
                        "total_count": 2,
                        "current_phase": "Deliver",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.session.completed",
                    aggregate_type="execution",
                    aggregate_id="exec_parallel_ac_1",
                    data={
                        "execution_id": "exec_parallel",
                        "session_id": "child_1",
                        "session_scope_id": "exec_parallel_ac_1",
                        "success": True,
                    },
                )
            )

            assert await manager._derive_progress_accounting_blocker(snapshot) is None

            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_parallel",
                    data={"session_id": "orch_parallel", "status": "failed"},
                )
            )

            blocker = await manager._derive_progress_accounting_blocker(snapshot)

            assert blocker is not None
            assert "execution reached terminal failed state" in blocker
        finally:
            await store.close()

    async def test_cancel_requested_wins_over_complete_execution_terminal(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            stop = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await stop.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late done"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_cancel", execution_id="exec_cancel"),
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_cancel",
                    data={"session_id": "orch_cancel", "status": "completed"},
                )
            )

            snapshot = await manager.cancel_job(started.job_id)
            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}

            await asyncio.sleep(1.2)
            snapshot = await manager.get_snapshot(started.job_id)

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert snapshot.result_meta.get("completed_from_execution_terminal") is not True
        finally:
            stop.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_execution_completion_waits_for_runner_cancellation_before_job_terminal_event(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            cancel_seen = asyncio.Event()
            release = asyncio.Event()

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancel_seen.set()
                    await release.wait()
                    raise
                return MCPToolResult()

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_wait", execution_id="exec_wait"),
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_wait",
                    data={"completed_count": 1, "total_count": 1},
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_wait",
                    data={"session_id": "orch_wait", "status": "completed"},
                )
            )

            await asyncio.wait_for(cancel_seen.wait(), timeout=2)
            snapshot = await manager.get_snapshot(started.job_id)
            assert snapshot.is_terminal is False

            release.set()
            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.COMPLETED, timeout=2.0
            )

            assert snapshot.result_text == "Execution complete: 1/1 ACs completed"
            recovered = JobManager(store)
            assert (await recovered.get_snapshot(started.job_id)).status is JobStatus.COMPLETED
            events, _ = await store.get_events_after("job", started.job_id, last_row_id=0)
            assert [
                event.type
                for event in events
                if event.type.startswith("mcp.job.") and event.type != "mcp.job.updated"
            ].count("mcp.job.completed") == 1
        finally:
            release.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_completed_execution_force_completes_noncooperative_live_runner(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        stop = asyncio.Event()
        cancel_seen = asyncio.Event()

        async def _runner() -> MCPToolResult:
            while not stop.is_set():
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancel_seen.set()
                    continue
            return MCPToolResult(content=(MCPContentItem(type=ContentType.TEXT, text="late"),))

        runner_task = asyncio.create_task(_runner())
        try:
            with patch.object(
                job_manager_module,
                "_COMPLETED_EXECUTION_CANCEL_GRACE_SECONDS",
                0.05,
            ):
                started = await manager.start_job(
                    job_type="execute_seed",
                    initial_message="queued",
                    runner=runner_task,
                    links=JobLinks(session_id="orch_stubborn", execution_id="exec_stubborn"),
                )
                await store.append(
                    BaseEvent(
                        type="workflow.progress.updated",
                        aggregate_type="execution",
                        aggregate_id="exec_stubborn",
                        data={"completed_count": 1, "total_count": 1},
                    )
                )
                await store.append(
                    BaseEvent(
                        type="execution.terminal",
                        aggregate_type="execution",
                        aggregate_id="exec_stubborn",
                        data={"session_id": "orch_stubborn", "status": "completed"},
                    )
                )

                await asyncio.wait_for(cancel_seen.wait(), timeout=2)
                snapshot = await _wait_for_job_status(
                    manager, started.job_id, JobStatus.COMPLETED, timeout=2.0
                )

                assert snapshot.result_text == "Execution complete: 1/1 ACs completed"
                assert snapshot.message == "Execution complete; formal evaluation not run"
                assert snapshot.result_meta["completed_from_execution_terminal"] is True
                assert snapshot.result_meta["evaluated"] is False
                assert snapshot.result_meta["verification_status"] == "executed_unverified"
                assert snapshot.result_meta["next_step"] == "ooo evaluate orch_stubborn"
                assert manager.has_live_job_task(started.job_id) is False
                events, _ = await store.get_events_after("job", started.job_id, last_row_id=0)
                assert [
                    event.type
                    for event in events
                    if event.type.startswith("mcp.job.") and event.type != "mcp.job.updated"
                ].count("mcp.job.completed") == 1
        finally:
            stop.set()
            runner_task.cancel()
            await asyncio.gather(runner_task, return_exceptions=True)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_completed_execution_recovers_after_restart_without_live_runner(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            await store.initialize()
            await store.append(
                BaseEvent(
                    type="mcp.job.created",
                    aggregate_type="job",
                    aggregate_id="job_recover_complete",
                    data={
                        "job_type": "execute_seed",
                        "status": JobStatus.QUEUED.value,
                        "message": "queued",
                        "links": {
                            "session_id": "orch_recover",
                            "execution_id": "exec_recover",
                            "lineage_id": None,
                        },
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="mcp.job.updated",
                    aggregate_type="job",
                    aggregate_id="job_recover_complete",
                    data={
                        "status": JobStatus.RUNNING.value,
                        "message": "Running execute_seed",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_recover",
                    data={"session_id": "orch_recover", "status": "completed"},
                )
            )

            snapshot = await manager.get_snapshot("job_recover_complete")

            assert snapshot.status is JobStatus.COMPLETED
            assert snapshot.message == "Execution complete; formal evaluation not run"
            assert snapshot.result_meta["completed_from_execution_terminal"] is True
            assert snapshot.result_meta["evaluated"] is False
            assert snapshot.result_meta["verification_status"] == "executed_unverified"
            assert snapshot.result_meta["formal_evaluation_required"] is True
            assert snapshot.result_meta["next_step"] == "ooo evaluate orch_recover"
            events, _ = await store.get_events_after("job", "job_recover_complete", last_row_id=0)
            terminal_events = [
                event.type
                for event in events
                if event.type
                in {
                    "mcp.job.completed",
                    "mcp.job.failed",
                    "mcp.job.cancelled",
                    "mcp.job.interrupted",
                }
            ]
            assert terminal_events == ["mcp.job.completed"]
        finally:
            await store.close()

    async def test_execution_terminal_completion_preserves_runner_cancel_result(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            cancel_seen = asyncio.Event()

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancel_seen.set()
                    return MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text="cancelled"),),
                        is_error=False,
                        meta={"action": "cancelled"},
                    )
                return MCPToolResult()

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(
                    session_id="orch_cancel_return",
                    execution_id="exec_cancel_return",
                ),
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_cancel_return",
                    data={"session_id": "orch_cancel_return", "status": "completed"},
                )
            )

            await asyncio.wait_for(cancel_seen.wait(), timeout=2)
            snapshot = await _wait_for_job_status(
                manager,
                started.job_id,
                JobStatus.CANCELLED,
                timeout=2.0,
            )

            assert snapshot.result_text == "cancelled"
            assert snapshot.result_meta.get("completed_from_execution_terminal") is not True
            events, _ = await store.get_events_after("job", started.job_id, last_row_id=0)
            terminal_events = [
                event.type
                for event in events
                if event.type
                in {
                    "mcp.job.completed",
                    "mcp.job.failed",
                    "mcp.job.cancelled",
                    "mcp.job.interrupted",
                }
            ]
            assert terminal_events == ["mcp.job.cancelled"]
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_execution_terminal_completion_preserves_runner_cancel_exception(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            cancel_seen = asyncio.Event()

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError as exc:
                    cancel_seen.set()
                    raise RuntimeError("cleanup failed after terminal execution") from exc
                return MCPToolResult()

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(
                    session_id="orch_cancel_exception",
                    execution_id="exec_cancel_exception",
                ),
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_cancel_exception",
                    data={"session_id": "orch_cancel_exception", "status": "completed"},
                )
            )

            await asyncio.wait_for(cancel_seen.wait(), timeout=2)
            snapshot = await _wait_for_job_status(
                manager,
                started.job_id,
                JobStatus.FAILED,
                timeout=2.0,
            )

            assert snapshot.result_meta.get("completed_from_execution_terminal") is not True
            assert snapshot.error == "cleanup failed after terminal execution"
            events, _ = await store.get_events_after("job", started.job_id, last_row_id=0)
            terminal_events = [
                event.type
                for event in events
                if event.type
                in {
                    "mcp.job.completed",
                    "mcp.job.failed",
                    "mcp.job.cancelled",
                    "mcp.job.interrupted",
                }
            ]
            assert terminal_events == ["mcp.job.failed"]
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_completed_execution_recovery_writes_single_terminal_event_with_concurrent_readers(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            await store.initialize()
            await store.append(
                BaseEvent(
                    type="mcp.job.created",
                    aggregate_type="job",
                    aggregate_id="job_recover_race",
                    data={
                        "job_type": "execute_seed",
                        "status": JobStatus.RUNNING.value,
                        "message": "Running execute_seed",
                        "links": {
                            "session_id": "orch_recover_race",
                            "execution_id": "exec_recover_race",
                            "lineage_id": None,
                        },
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_recover_race",
                    data={"session_id": "orch_recover_race", "status": "completed"},
                )
            )

            first, second = await asyncio.gather(
                manager.get_snapshot("job_recover_race"),
                manager.get_snapshot("job_recover_race"),
            )

            assert first.status is JobStatus.COMPLETED
            assert second.status is JobStatus.COMPLETED
            events, _ = await store.get_events_after("job", "job_recover_race", last_row_id=0)
            terminal_events = [
                event.type
                for event in events
                if event.type
                in {
                    "mcp.job.completed",
                    "mcp.job.failed",
                    "mcp.job.cancelled",
                    "mcp.job.interrupted",
                }
            ]
            assert terminal_events == ["mcp.job.completed"]
        finally:
            await store.close()

    async def test_completed_execution_recovery_is_idempotent_across_managers(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        first_manager = JobManager(store)
        second_manager = JobManager(store)

        try:
            await store.initialize()
            await store.append(
                BaseEvent(
                    type="mcp.job.created",
                    aggregate_type="job",
                    aggregate_id="job_recover_multi_manager",
                    data={
                        "job_type": "execute_seed",
                        "status": JobStatus.RUNNING.value,
                        "message": "Running execute_seed",
                        "links": {
                            "session_id": "orch_recover_multi_manager",
                            "execution_id": "exec_recover_multi_manager",
                            "lineage_id": None,
                        },
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_recover_multi_manager",
                    data={"session_id": "orch_recover_multi_manager", "status": "completed"},
                )
            )

            first, second = await asyncio.gather(
                first_manager.get_snapshot("job_recover_multi_manager"),
                second_manager.get_snapshot("job_recover_multi_manager"),
            )

            assert first.status is JobStatus.COMPLETED
            assert second.status is JobStatus.COMPLETED
            events, _ = await store.get_events_after(
                "job",
                "job_recover_multi_manager",
                last_row_id=0,
            )
            terminal_events = [
                event.type
                for event in events
                if event.type
                in {
                    "mcp.job.completed",
                    "mcp.job.failed",
                    "mcp.job.cancelled",
                    "mcp.job.interrupted",
                }
            ]
            assert terminal_events == ["mcp.job.completed"]
        finally:
            await store.close()

    async def test_completed_execution_recovery_is_non_mutating_for_read_only_store(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        database_url = store._database_url

        try:
            await store.initialize()
            await store.append(
                BaseEvent(
                    type="mcp.job.created",
                    aggregate_type="job",
                    aggregate_id="job_recover_read_only",
                    data={
                        "job_type": "execute_seed",
                        "status": JobStatus.RUNNING.value,
                        "message": "Running execute_seed",
                        "links": {
                            "session_id": "orch_recover_read_only",
                            "execution_id": "exec_recover_read_only",
                            "lineage_id": None,
                        },
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_recover_read_only",
                    data={"session_id": "orch_recover_read_only", "status": "completed"},
                )
            )
        finally:
            await store.close()

        read_only_store = EventStore(database_url, read_only=True)
        read_only_manager = JobManager(read_only_store)
        try:
            await read_only_store.initialize(create_schema=False)
            snapshot = await read_only_manager.get_snapshot("job_recover_read_only")

            assert snapshot.status is JobStatus.COMPLETED
            assert snapshot.message == "Execution complete; formal evaluation not run"
            assert snapshot.result_meta["completed_from_execution_terminal"] is True
            assert snapshot.result_meta["evaluated"] is False
            assert snapshot.result_meta["verification_status"] == "executed_unverified"
            assert snapshot.result_meta["next_step"] == "ooo evaluate orch_recover_read_only"
            events, _ = await read_only_store.get_events_after(
                "job",
                "job_recover_read_only",
                last_row_id=0,
            )
            terminal_events = [
                event.type
                for event in events
                if event.type
                in {
                    "mcp.job.completed",
                    "mcp.job.failed",
                    "mcp.job.cancelled",
                    "mcp.job.interrupted",
                }
            ]
            assert terminal_events == []
        finally:
            await read_only_store.close()

    async def test_progress_accounting_failure_recovery_is_non_mutating_for_read_only_store(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        database_url = store._database_url

        try:
            await store.initialize()
            await store.append(
                BaseEvent(
                    type="mcp.job.created",
                    aggregate_type="job",
                    aggregate_id="job_recover_failed_read_only",
                    data={
                        "job_type": "execute_seed",
                        "status": JobStatus.RUNNING.value,
                        "message": "Running execute_seed",
                        "links": {
                            "session_id": "orch_recover_failed_read_only",
                            "execution_id": "exec_recover_failed_read_only",
                            "lineage_id": None,
                        },
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_recover_failed_read_only",
                    data={
                        "session_id": "orch_recover_failed_read_only",
                        "completed_count": 0,
                        "total_count": 1,
                        "current_phase": "Deliver",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.session.completed",
                    aggregate_type="execution",
                    aggregate_id="exec_recover_failed_read_only_ac_1",
                    data={
                        "execution_id": "exec_recover_failed_read_only",
                        "session_id": "child_1",
                        "session_scope_id": "exec_recover_failed_read_only_ac_1",
                        "success": True,
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_recover_failed_read_only",
                    data={"session_id": "orch_recover_failed_read_only", "status": "failed"},
                )
            )
        finally:
            await store.close()

        read_only_store = EventStore(database_url, read_only=True)
        read_only_manager = JobManager(read_only_store)
        try:
            await read_only_store.initialize(create_schema=False)
            snapshot = await read_only_manager.get_snapshot("job_recover_failed_read_only")

            assert snapshot.status is JobStatus.FAILED
            assert snapshot.result_meta["failed_from_progress_accounting_stall"] is True
            assert "workflow progress accounting stalled" in (snapshot.error or "")
            events, _ = await read_only_store.get_events_after(
                "job",
                "job_recover_failed_read_only",
                last_row_id=0,
            )
            terminal_events = [
                event.type
                for event in events
                if event.type
                in {
                    "mcp.job.completed",
                    "mcp.job.failed",
                    "mcp.job.cancelled",
                    "mcp.job.interrupted",
                }
            ]
            assert terminal_events == []
        finally:
            await read_only_store.close()

    async def test_completed_execution_recovery_does_not_beat_concurrent_cancel_request(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            await store.initialize()
            await store.append(
                BaseEvent(
                    type="mcp.job.created",
                    aggregate_type="job",
                    aggregate_id="job_recover_cancel_race",
                    data={
                        "job_type": "execute_seed",
                        "status": JobStatus.RUNNING.value,
                        "message": "Running execute_seed",
                        "links": {
                            "session_id": "orch_recover_cancel_race",
                            "execution_id": "exec_recover_cancel_race",
                            "lineage_id": None,
                        },
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_recover_cancel_race",
                    data={"session_id": "orch_recover_cancel_race", "status": "completed"},
                )
            )
            recovery_lock = manager._recovery_locks.setdefault(
                "job_recover_cancel_race",
                asyncio.Lock(),
            )
            await recovery_lock.acquire()
            snapshot_task = asyncio.create_task(manager.get_snapshot("job_recover_cancel_race"))
            await asyncio.sleep(0)
            await store.append(
                BaseEvent(
                    type="mcp.job.updated",
                    aggregate_type="job",
                    aggregate_id="job_recover_cancel_race",
                    data={
                        "status": JobStatus.CANCEL_REQUESTED.value,
                        "message": "Cancellation requested",
                    },
                )
            )

            recovery_lock.release()
            snapshot = await snapshot_task

            assert snapshot.status is JobStatus.CANCEL_REQUESTED
            events, _ = await store.get_events_after(
                "job",
                "job_recover_cancel_race",
                last_row_id=0,
            )
            terminal_events = [
                event.type
                for event in events
                if event.type
                in {
                    "mcp.job.completed",
                    "mcp.job.failed",
                    "mcp.job.cancelled",
                    "mcp.job.interrupted",
                }
            ]
            assert terminal_events == []
        finally:
            await store.close()

    async def test_complete_workflow_progress_without_terminal_event_does_not_complete_job(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            stop = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await stop.wait()
                return MCPToolResult()

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_progress_only", execution_id="exec_progress_only"),
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_progress_only",
                    data={"completed_count": 1, "total_count": 1},
                )
            )

            await asyncio.sleep(1.2)
            snapshot = await manager.get_snapshot(started.job_id)

            assert snapshot.is_terminal is False
            assert snapshot.result_meta.get("completed_from_execution_terminal") is not True
        finally:
            stop.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_execution_terminal_ignores_incomplete_progress_for_result_text(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            stop = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await stop.wait()
                return MCPToolResult()

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_partial_progress", execution_id="exec_partial"),
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_partial",
                    data={"completed_count": 1, "total_count": 2},
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_partial",
                    data={"session_id": "orch_partial_progress", "status": "completed"},
                )
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.COMPLETED, timeout=2.0
            )

            assert snapshot.result_text == "Execution complete"
            assert snapshot.message == "Execution complete; formal evaluation not run"
            assert snapshot.result_meta["completed_from_execution_terminal"] is True
            assert snapshot.result_meta["evaluated"] is False
            assert snapshot.result_meta["verification_status"] == "executed_unverified"
            assert snapshot.result_meta["next_step"] == "ooo evaluate orch_partial_progress"
        finally:
            stop.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_start_job_completes_and_persists_result(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(0.05)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="done"),),
                    is_error=False,
                    meta={"kind": "test"},
                )

            started = await manager.start_job(
                job_type="test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            snapshot = await _wait_for_job_status(manager, started.job_id, JobStatus.COMPLETED)

            assert snapshot.status == JobStatus.COMPLETED
            assert snapshot.result_text == "done"
            assert snapshot.result_meta["kind"] == "test"
        finally:
            await store.close()

    async def test_job_result_handler_returns_persisted_success_payload(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(0.01)
                return MCPToolResult(
                    content=(
                        MCPContentItem(type=ContentType.TEXT, text="auto result summary"),
                        MCPContentItem(
                            type=ContentType.RESOURCE,
                            uri="file:///tmp/detached-auto-result.json",
                        ),
                    ),
                    is_error=False,
                    meta={
                        "auto_session_id": "auto_payload_success",
                        "result": {"artifact": "detached-auto-result.json", "ok": True},
                    },
                )

            started = await manager.start_job(
                job_type="auto",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="auto_payload_success"),
            )
            await _wait_for_job_status(manager, started.job_id, JobStatus.COMPLETED)

            result = await JobResultHandler(event_store=store).handle({"job_id": started.job_id})

            assert result.is_ok
            assert result.value.content == (
                MCPContentItem(type=ContentType.TEXT, text="auto result summary"),
                MCPContentItem(
                    type=ContentType.RESOURCE,
                    uri="file:///tmp/detached-auto-result.json",
                ),
            )
            assert result.value.meta["auto_session_id"] == "auto_payload_success"
            assert result.value.meta["result"] == {
                "artifact": "detached-auto-result.json",
                "ok": True,
            }
            assert result.value.meta["result_payload"]["content"][1]["uri"] == (
                "file:///tmp/detached-auto-result.json"
            )
        finally:
            await store.close()

    async def test_job_result_handler_returns_expired_terminal_result(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        job_id = "job_expired_terminal_result"
        expired_at = datetime.now(UTC) - timedelta(hours=2)
        await store.initialize()

        try:
            await store.append(
                BaseEvent(
                    id="evt_expired_terminal_created",
                    type="mcp.job.created",
                    timestamp=expired_at,
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "job_type": "execute_seed",
                        "status": JobStatus.QUEUED.value,
                        "message": "Queued execute_seed",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    id="evt_expired_terminal_completed",
                    type="mcp.job.completed",
                    timestamp=expired_at + timedelta(seconds=1),
                    aggregate_type="job",
                    aggregate_id=job_id,
                    data={
                        "status": JobStatus.COMPLETED.value,
                        "message": "Job complete",
                        "result_text": "overnight QA verdict",
                        "result_meta": {"qa_passed": True},
                    },
                )
            )

            result = await JobResultHandler(event_store=store).handle({"job_id": job_id})
        finally:
            await store.close()

        assert result.is_ok
        assert result.value.content[0].text == "overnight QA verdict"
        assert result.value.meta["result_available"] is True
        assert result.value.meta["qa_passed"] is True

    async def test_start_job_default_allocates_job_id(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                return MCPToolResult()

            started = await manager.start_job(
                job_type="test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            assert started.job_id.startswith("job_")
            assert len(started.job_id) == len("job_") + 12
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_start_job_accepts_preallocated_job_id_once(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                return MCPToolResult()

            job_id = await manager.allocate_job_id()
            started = await manager.start_job(
                job_id=job_id,
                job_type="test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            assert started.job_id == job_id
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_start_job_rejects_existing_job_id(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                return MCPToolResult()

            job_id = await manager.allocate_job_id()
            await manager.start_job(
                job_id=job_id,
                job_type="test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            try:
                runner = asyncio.get_running_loop().create_future()
                runner.set_result(MCPToolResult())
                await manager.start_job(
                    job_id=job_id,
                    job_type="test",
                    initial_message="queued again",
                    runner=runner,
                    links=JobLinks(),
                )
            except ValueError as exc:
                assert str(exc) == f"Job already exists: {job_id}"
            else:
                raise AssertionError("expected duplicate job_id to be rejected")
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_start_job_tracks_externally_created_task(self, tmp_path) -> None:
        """A pre-built Task is registered in ``_runner_tasks`` for cancellation routing."""
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(0.02)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ext"),),
                    is_error=False,
                )

            external_task = asyncio.create_task(_runner())
            started = await manager.start_job(
                job_type="external",
                initial_message="queued",
                runner=external_task,
                links=JobLinks(),
            )

            assert manager._runner_tasks.get(started.job_id) is external_task

            snapshot = await _wait_for_job_status(manager, started.job_id, JobStatus.COMPLETED)
            assert snapshot.status == JobStatus.COMPLETED
            assert started.job_id not in manager._runner_tasks
        finally:
            await store.close()

    async def test_start_job_wraps_bare_future_runner(self, tmp_path) -> None:
        """A bare Future is wrapped in a Task and still completes the job."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        future: asyncio.Future[MCPToolResult] = asyncio.get_running_loop().create_future()

        try:
            started = await manager.start_job(
                job_type="future",
                initial_message="queued",
                runner=future,
                links=JobLinks(),
            )
            runner_task = manager._runner_tasks.get(started.job_id)

            assert isinstance(runner_task, asyncio.Task)
            assert runner_task is not future

            future.set_result(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="future"),),
                    is_error=False,
                    meta={"kind": "future"},
                )
            )
            snapshot = await _wait_for_job_status(manager, started.job_id, JobStatus.COMPLETED)

            assert snapshot.status == JobStatus.COMPLETED
            assert snapshot.result_text == "future"
            assert snapshot.result_meta["kind"] == "future"
            assert started.job_id not in manager._runner_tasks
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_cancels_externally_created_task(self, tmp_path) -> None:
        """Cancellation reaches a pre-built Task registered as the runner."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        runner_cancelled = asyncio.Event()

        try:

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    runner_cancelled.set()
                    raise
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            external_task = asyncio.create_task(_runner())
            started = await manager.start_job(
                job_type="external-cancel",
                initial_message="queued",
                runner=external_task,
                links=JobLinks(),
            )

            await manager.cancel_job(started.job_id)
            await asyncio.wait_for(runner_cancelled.wait(), timeout=1)
            await asyncio.sleep(0)
            snapshot = await manager.get_snapshot(started.job_id)

            assert external_task.cancelled()
            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_wait_for_change_returns_new_cursor(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(0.05)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="waited"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="wait-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            snapshot, changed = await manager.wait_for_change(
                started.job_id,
                cursor=started.cursor,
                timeout_seconds=2,
            )

            assert changed is True
            assert snapshot.cursor >= started.cursor
        finally:
            await store.close()

    async def test_cancel_job_persists_job_scoped_agent_process_cancel(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        checkpoint_store = CheckpointStore(tmp_path / "checkpoints")
        manager = JobManager(store, checkpoint_store=checkpoint_store)

        try:
            never_done = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await never_done.wait()
                return MCPToolResult()

            started = await manager.start_job(
                job_type="durable_cancel",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            await manager.cancel_job(started.job_id)

            found, reason = AgentProcessHandle.load_persisted_cancel(
                f"mcp_job:{started.job_id}", store=checkpoint_store
            )
            assert found is True
            assert reason == "Background job cancelled"
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_persist_failure_does_not_block_cancellation(
        self, tmp_path, monkeypatch
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        def _raise_persist(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
            raise RuntimeError("checkpoint unavailable")

        monkeypatch.setattr(manager, "_persist_durable_cancel", _raise_persist)

        try:
            never_done = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await never_done.wait()
                return MCPToolResult()

            started = await manager.start_job(
                job_type="durable_cancel_best_effort",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            snapshot = await manager.cancel_job(started.job_id)

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_cancels_non_session_task(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(10)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="cancel-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            await manager.cancel_job(started.job_id)
            await asyncio.sleep(0.1)
            snapshot = await manager.get_snapshot(started.job_id)

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
        finally:
            await store.close()

    async def test_cancel_job_does_not_mark_linked_session_when_task_already_done(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="done"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="race-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_done_123", execution_id="exec_done_123"),
            )
            task = manager._tasks[started.job_id]
            await task

            snapshot = await manager.cancel_job(started.job_id)
            session_cancelled = await store.query_events(
                aggregate_id="orch_done_123",
                event_type="orchestrator.session.cancelled",
            )
            execution_cancelled = await store.query_events(
                aggregate_id="exec_done_123",
                event_type="execution.terminal",
            )

            assert snapshot.is_terminal
            assert not session_cancelled
            assert not any(event.data.get("status") == "cancelled" for event in execution_cancelled)
        finally:
            await store.close()

    async def test_cancel_job_stops_task_when_linked_session_already_terminal(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_terminal_123"
        execution_id = "exec_terminal_123"
        await clear_cancellation(session_id)
        runner_cancelled = asyncio.Event()

        try:

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    runner_cancelled.set()
                    raise
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="terminal-session-race",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )
            repo = SessionRepository(store)
            mark_result = await repo.mark_completed(session_id)
            assert mark_result.is_ok

            snapshot = await manager.cancel_job(started.job_id)
            await asyncio.wait_for(runner_cancelled.wait(), timeout=1)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            execution_cancelled = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert await is_cancellation_requested(session_id) is False
            assert not session_cancelled
            assert not any(event.data.get("status") == "cancelled" for event in execution_cancelled)
        finally:
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_requests_linked_session_cancellation_without_start_event(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_pending_123"
        execution_id = "exec_pending_123"
        await clear_cancellation(session_id)
        lock_path(session_id).unlink(missing_ok=True)

        try:

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(10)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="pending-session-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )
            runner_task = manager._runner_tasks[started.job_id]

            snapshot = await manager.cancel_job(started.job_id)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            terminal_events = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )
            await asyncio.sleep(0)

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert await is_cancellation_requested(session_id) is False
            assert not session_cancelled
            assert not terminal_events
            assert runner_task.done() is True
        finally:
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_clears_precreated_unstarted_session_cancellation(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_precreated_123"
        execution_id = "exec_precreated_123"
        await clear_cancellation(session_id)

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_precreated_123",
                session_id=session_id,
            )
            assert create_result.is_ok

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(10)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="precreated-session-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )
            runner_task = manager._runner_tasks[started.job_id]

            snapshot = await manager.cancel_job(started.job_id)
            await asyncio.sleep(0)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            execution_cancelled = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert await is_cancellation_requested(session_id) is False
            assert runner_task.done() is True
            assert session_cancelled
            assert execution_cancelled[-1].data["status"] == "cancelled"
        finally:
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_preserves_signal_when_runner_starts_during_cancel(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_start_race_123"
        execution_id = "exec_start_race_123"
        await clear_cancellation(session_id)

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_start_race_123",
                session_id=session_id,
            )
            assert create_result.is_ok

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    acquire_session_lock(session_id)
                    raise
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="start-race-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )

            snapshot = await manager.cancel_job(started.job_id)

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert await is_cancellation_requested(session_id) is True
        finally:
            lock_path(session_id).unlink(missing_ok=True)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_persists_cross_process_linked_cancellation(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_cross_process_123"
        execution_id = "exec_cross_process_123"
        await clear_cancellation(session_id)

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_cross_process_123",
                session_id=session_id,
            )
            assert create_result.is_ok
            lock_path(session_id).write_text("1")

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(10)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="cross-process-session-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )

            snapshot = await manager.cancel_job(started.job_id)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            execution_cancelled = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert session_cancelled
            assert session_cancelled[-1].data["cancelled_by"] == "mcp_job_manager"
            assert execution_cancelled
            assert execution_cancelled[-1].data["status"] == "cancelled"
        finally:
            release_session_lock(session_id)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_does_not_persist_cross_process_cancel_when_reconstruct_fails(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_reconstruct_fail_123"
        execution_id = "exec_reconstruct_fail_123"
        await clear_cancellation(session_id)
        runner_cancelled = asyncio.Event()

        try:
            await store.initialize()
            lock_path(session_id).write_text("1")

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    runner_cancelled.set()
                    raise
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="reconstruct-fail-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )

            with patch(
                "ouroboros.mcp.job_manager.SessionRepository.reconstruct_session",
                new=AsyncMock(return_value=Result.err(PersistenceError("replay failed"))),
            ):
                snapshot = await manager.cancel_job(started.job_id)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert runner_cancelled.is_set() is True
            assert not session_cancelled
        finally:
            lock_path(session_id).unlink(missing_ok=True)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_errors_before_persist_when_latest_reconstruct_fails(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_latest_reconstruct_fail_123"
        execution_id = "exec_latest_reconstruct_fail_123"
        await clear_cancellation(session_id)
        runner_cancelled = asyncio.Event()

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_latest_reconstruct_fail_123",
                session_id=session_id,
            )
            assert create_result.is_ok
            lock_path(session_id).write_text("1")

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    runner_cancelled.set()
                    raise
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="latest-reconstruct-fail-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )

            original_reconstruct = SessionRepository.reconstruct_session
            call_count = 0

            async def _reconstruct_once_then_fail(self, target_session_id):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return await original_reconstruct(self, target_session_id)
                return Result.err(PersistenceError("replay failed"))

            with patch(
                "ouroboros.mcp.job_manager.SessionRepository.reconstruct_session",
                new=_reconstruct_once_then_fail,
            ):
                try:
                    await manager.cancel_job(started.job_id)
                except ValueError as exc:
                    assert "Failed to inspect linked session before cancellation" in str(exc)
                else:
                    raise AssertionError("cancel_job should fail when latest inspect fails")

            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            execution_cancelled = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )

            assert runner_cancelled.is_set() is True
            assert not session_cancelled
            assert not execution_cancelled
        finally:
            lock_path(session_id).unlink(missing_ok=True)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_stops_task_when_linked_session_inspection_fails(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_inspection_fail_123"
        execution_id = "exec_inspection_fail_123"
        await clear_cancellation(session_id)

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_inspection_fail_123",
                session_id=session_id,
            )
            assert create_result.is_ok
            lock_path(session_id).write_text("1")

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(10)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="inspection-fail-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )
            runner_task = manager._runner_tasks[started.job_id]

            with patch.object(
                store,
                "query_events",
                new=AsyncMock(side_effect=PersistenceError("query failed")),
            ):
                snapshot = await manager.cancel_job(started.job_id)
            await asyncio.sleep(0)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            execution_cancelled = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert runner_task.done() is True
            assert await is_cancellation_requested(session_id) is True
            assert session_cancelled
            assert session_cancelled[-1].data["cancelled_by"] == "mcp_job_manager"
            assert execution_cancelled
            assert execution_cancelled[-1].data["status"] == "cancelled"
        finally:
            release_session_lock(session_id)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_requests_cancellation_for_started_linked_runner(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_started_123"
        execution_id = "exec_started_123"
        await clear_cancellation(session_id)
        runner_cancelled = asyncio.Event()

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_started_123",
                session_id=session_id,
            )
            assert create_result.is_ok
            acquire_session_lock(session_id)

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    runner_cancelled.set()
                    return MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text="cancelled"),),
                        is_error=False,
                    )
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="started-session-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )
            runner_task = manager._runner_tasks[started.job_id]

            snapshot = await manager.cancel_job(started.job_id)
            await asyncio.wait_for(runner_cancelled.wait(), timeout=1)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            terminal_events = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert await is_cancellation_requested(session_id) is True
            assert runner_cancelled.is_set() is True
            assert runner_task.done() is True
            assert not session_cancelled
            assert not terminal_events
        finally:
            release_session_lock(session_id)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_stops_task_when_persisting_linked_cancel_fails(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_mark_fail_123"
        execution_id = "exec_mark_fail_123"
        await clear_cancellation(session_id)
        runner_cancelled = asyncio.Event()

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_mark_fail_123",
                session_id=session_id,
            )
            assert create_result.is_ok
            lock_path(session_id).write_text("1")

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    runner_cancelled.set()
                    raise
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="mark-fail-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )

            with patch(
                "ouroboros.mcp.job_manager.SessionRepository.mark_cancelled",
                new=AsyncMock(return_value=Result.err(PersistenceError("write failed"))),
            ):
                try:
                    await manager.cancel_job(started.job_id)
                except ValueError as exc:
                    assert "Failed to mark linked session cancelled" in str(exc)
                else:
                    raise AssertionError("cancel_job should fail when session cancel does")

            assert runner_cancelled.is_set() is True
        finally:
            lock_path(session_id).unlink(missing_ok=True)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_render_job_snapshot_includes_sub_ac_progress(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        await store.initialize()

        try:
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_sub_ac_progress",
                    data={
                        "execution_id": "exec_job_sub_ac_progress",
                        "completed_count": 0,
                        "total_count": 2,
                        "current_phase": "Deliver",
                        "activity": "Monitoring",
                        "activity_detail": "Level 1/1",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.subtask.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_sub_ac_progress",
                    data={
                        "ac_index": 1,
                        "sub_task_index": 1,
                        "sub_task_id": "ac_1_sub_1",
                        "content": "Child one",
                        "status": "completed",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.subtask.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_sub_ac_progress",
                    data={
                        "ac_index": 1,
                        "sub_task_index": 2,
                        "sub_task_id": "ac_1_sub_2",
                        "content": "Child two",
                        "status": "executing",
                    },
                )
            )

            snapshot = JobSnapshot(
                job_id="job_sub_ac_progress",
                job_type="execute_seed",
                status=JobStatus.RUNNING,
                message="Deliver | 0/2 ACs",
                created_at=datetime(2026, 4, 22, tzinfo=UTC),
                updated_at=datetime(2026, 4, 22, tzinfo=UTC),
                cursor=2,
                links=JobLinks(execution_id="exec_job_sub_ac_progress"),
            )

            text, progress = await _render_job_snapshot_inner(snapshot, store)

            assert "**AC Progress**: 0/2" in text
            assert "**Sub-AC Progress**: 1/2 complete · 1 working" in text
            assert progress["sub_ac_completed"] == 1
            assert progress["sub_ac_total"] == 2
            assert "- `ac_1_sub_2`: executing -- Child two" in text
        finally:
            await store.close()

    async def test_render_job_snapshot_counts_sub_ac_beyond_recent_event_window(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        await store.initialize()

        try:
            for index in range(1, 301):
                await store.append(
                    BaseEvent(
                        type="execution.subtask.updated",
                        aggregate_type="execution",
                        aggregate_id="exec_job_many_sub_ac",
                        data={
                            "ac_index": 1,
                            "sub_task_index": index,
                            "sub_task_id": f"ac_1_sub_{index}",
                            "content": f"Child {index}",
                            "status": "completed",
                        },
                    )
                )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_many_sub_ac",
                    data={
                        "execution_id": "exec_job_many_sub_ac",
                        "completed_count": 0,
                        "total_count": 1,
                        "current_phase": "Deliver",
                        "activity": "Monitoring",
                    },
                )
            )

            snapshot = JobSnapshot(
                job_id="job_many_sub_ac",
                job_type="execute_seed",
                status=JobStatus.RUNNING,
                message="Deliver | 0/1 ACs",
                created_at=datetime(2026, 4, 22, tzinfo=UTC),
                updated_at=datetime(2026, 4, 22, tzinfo=UTC),
                cursor=301,
                links=JobLinks(execution_id="exec_job_many_sub_ac"),
            )

            text, progress = await _render_job_snapshot_inner(snapshot, store)

            assert "**Sub-AC Progress**: 300/300 complete" in text
            assert progress["sub_ac_completed"] == 300
            assert progress["sub_ac_total"] == 300
        finally:
            await store.close()

    async def test_render_job_snapshot_keeps_workflow_after_subtask_burst(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        await store.initialize()

        try:
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_old_workflow",
                    data={
                        "execution_id": "exec_job_old_workflow",
                        "completed_count": 1,
                        "total_count": 4,
                        "current_phase": "Implement",
                        "activity": "Monitoring",
                    },
                )
            )
            for index in range(1, 301):
                await store.append(
                    BaseEvent(
                        type="execution.subtask.updated",
                        aggregate_type="execution",
                        aggregate_id="exec_job_old_workflow",
                        data={
                            "ac_index": 1,
                            "sub_task_index": index,
                            "sub_task_id": f"ac_1_sub_{index}",
                            "content": f"Child {index}",
                            "status": "completed",
                        },
                    )
                )

            snapshot = JobSnapshot(
                job_id="job_old_workflow",
                job_type="execute_seed",
                status=JobStatus.RUNNING,
                message="Implement | 1/4 ACs",
                created_at=datetime(2026, 4, 22, tzinfo=UTC),
                updated_at=datetime(2026, 4, 22, tzinfo=UTC),
                cursor=301,
                links=JobLinks(execution_id="exec_job_old_workflow"),
            )

            text, progress = await _render_job_snapshot_inner(snapshot, store)

            assert "**Phase**: Implement" in text
            assert "**AC Progress**: 1/4" in text
            assert "**Sub-AC Progress**: 300/300 complete" in text
            assert progress["current_phase"] == "Implement"
            assert progress["ac_completed"] == 1
            assert progress["ac_total"] == 4
            assert progress["sub_ac_completed"] == 300
            assert progress["sub_ac_total"] == 300
        finally:
            await store.close()

    async def test_render_job_snapshot_does_not_cache_execution_progress(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        await store.initialize()

        try:
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_live_progress",
                    data={
                        "execution_id": "exec_job_live_progress",
                        "completed_count": 0,
                        "total_count": 2,
                        "current_phase": "Plan",
                        "activity": "Starting",
                    },
                )
            )
            snapshot = JobSnapshot(
                job_id="job_live_progress",
                job_type="execute_seed",
                status=JobStatus.RUNNING,
                message="Plan | 0/2 ACs",
                created_at=datetime(2026, 4, 22, tzinfo=UTC),
                updated_at=datetime(2026, 4, 22, tzinfo=UTC),
                cursor=77,
                links=JobLinks(execution_id="exec_job_live_progress"),
            )

            first_text, first_progress = await _render_job_snapshot(snapshot, store)
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_live_progress",
                    data={
                        "execution_id": "exec_job_live_progress",
                        "completed_count": 1,
                        "total_count": 2,
                        "current_phase": "Implement",
                        "activity": "Running",
                    },
                )
            )

            second_text, second_progress = await _render_job_snapshot(snapshot, store)

            assert "**Phase**: Plan" in first_text
            assert first_progress["ac_completed"] == 0
            assert "**Phase**: Implement" in second_text
            assert second_progress["ac_completed"] == 1
        finally:
            await store.close()

    def test_render_compact_job_snapshot_omits_full_sections(self) -> None:
        snapshot = JobSnapshot(
            job_id="job_compact",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Deliver | Sub-AC work | 0/2 ACs",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=77,
            links=JobLinks(execution_id="exec_compact"),
        )

        text = _render_compact_job_snapshot(
            snapshot,
            {
                "ac_completed": 0,
                "ac_total": 2,
                "current_phase": "Deliver",
                "sub_ac_completed": 1,
                "sub_ac_total": 3,
            },
            include_message=False,
        )

        assert text == "job_compact | running | Deliver | AC 0/2 | Sub-AC 1/3 | cursor 77"

    async def test_job_status_omitted_view_preserves_full_snapshot(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        snapshot = JobSnapshot(
            job_id="job_default_full",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=9,
            links=JobLinks(),
        )

        class StaticJobManager:
            async def get_snapshot(self, job_id: str) -> JobSnapshot:
                assert job_id == snapshot.job_id
                return snapshot

        handler = JobStatusHandler(event_store=store, job_manager=StaticJobManager())
        result = await handler.handle({"job_id": "job_default_full"})

        assert result.is_ok
        assert result.value.meta["view"] == "full"
        assert result.value.text_content.startswith("## Job: job_default_full")
        assert "**Status**: running" in result.value.text_content
        assert "job_default_full | running" not in result.value.text_content

    async def test_job_status_full_view_renders_raw_links_without_session_row(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        snapshot = JobSnapshot(
            job_id="job_auto_links",
            job_type="auto",
            status=JobStatus.RUNNING,
            message="Running auto",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=4,
            links=JobLinks(
                session_id="auto_session_links",
                execution_id="exec_links",
                lineage_id="lin_links",
            ),
        )

        class StaticJobManager:
            async def get_snapshot(self, job_id: str) -> JobSnapshot:
                assert job_id == snapshot.job_id
                return snapshot

        handler = JobStatusHandler(event_store=store, job_manager=StaticJobManager())
        await store.initialize()
        try:
            result = await handler.handle({"job_id": "job_auto_links"})
        finally:
            await store.close()

        assert result.is_ok
        assert "### Links" in result.value.text_content
        assert "**Session ID**: auto_session_links" in result.value.text_content
        assert "**Execution ID**: exec_links" in result.value.text_content
        assert "**Lineage ID**: lin_links" in result.value.text_content
        assert result.value.meta["session_id"] == "auto_session_links"
        assert result.value.meta["execution_id"] == "exec_links"
        assert result.value.meta["lineage_id"] == "lin_links"

    async def test_job_wait_omitted_view_preserves_full_unchanged_snapshot(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        snapshot = JobSnapshot(
            job_id="job_wait_full",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=12,
            links=JobLinks(),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 12
                assert timeout_seconds == 0
                return snapshot, False

        handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
        result = await handler.handle(
            {"job_id": "job_wait_full", "cursor": 12, "timeout_seconds": 0}
        )

        assert result.is_ok
        assert result.value.meta["view"] == "full"
        assert result.value.text_content.startswith("## Job: job_wait_full")
        assert "No new job-level events during this wait window." in result.value.text_content
        assert result.value.text_content != "unchanged cursor=12"

    async def test_job_wait_omitted_timeout_returns_immediate_snapshot(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        snapshot = JobSnapshot(
            job_id="job_wait_default_timeout",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=5,
            links=JobLinks(),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 5
                assert timeout_seconds == 0
                return snapshot, False

        handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
        result = await handler.handle({"job_id": "job_wait_default_timeout", "cursor": 5})

        assert result.is_ok
        assert result.value.meta["changed"] is False
        assert result.value.meta["cursor"] == 5

    async def test_job_wait_raw_mode_preserves_any_event_wakeup(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        await store.initialize()
        snapshot = JobSnapshot(
            job_id="job_wait_raw",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(execution_id="exec_wait_raw"),
        )

        class RawJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 0
                assert timeout_seconds == 0
                return snapshot, False

        try:
            await store.append(
                BaseEvent(
                    type="execution.output",
                    aggregate_type="execution",
                    aggregate_id="exec_wait_raw",
                    data={"message": "fine-grained backend event"},
                )
            )
            handler = JobWaitHandler(event_store=store, job_manager=RawJobManager())
            result = await handler.handle(
                {
                    "job_id": "job_wait_raw",
                    "cursor": 0,
                    "timeout_seconds": 0,
                    "view": "compact",
                }
            )

            assert result.is_ok
            assert result.value.meta["changed"] is False
            assert result.value.meta["wait_for"] == "raw"
            assert result.value.meta["cursor"] > 0
        finally:
            await store.close()

    async def test_job_wait_ac_change_ignores_raw_events_until_progress_changes(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        await store.initialize()
        snapshot = JobSnapshot(
            job_id="job_wait_ac_change",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(execution_id="exec_wait_ac_change"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 0
                assert timeout_seconds == 0
                return snapshot, False

        async def append_events() -> None:
            await asyncio.sleep(0.05)
            await store.append(
                BaseEvent(
                    type="execution.output",
                    aggregate_type="execution",
                    aggregate_id="exec_wait_ac_change",
                    data={"message": "raw backend event"},
                )
            )
            await asyncio.sleep(0.65)
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_wait_ac_change",
                    data={
                        "execution_id": "exec_wait_ac_change",
                        "completed_count": 1,
                        "total_count": 4,
                        "current_phase": "Implement",
                        "activity": "Running",
                    },
                )
            )

        task = asyncio.create_task(append_events())
        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            started_at = asyncio.get_running_loop().time()
            result = await handler.handle(
                {
                    "job_id": "job_wait_ac_change",
                    "cursor": 0,
                    "timeout_seconds": 2,
                    "view": "compact",
                    "wait_for": "ac_change",
                }
            )
            elapsed = asyncio.get_running_loop().time() - started_at

            assert result.is_ok
            assert elapsed >= 0.5
            assert result.value.meta["changed"] is True
            assert result.value.meta["wait_for"] == "ac_change"
            assert result.value.meta["ac_completed"] == 1
            assert result.value.text_content.startswith(
                "job_wait_ac_change | running | Implement | AC 1/4 | cursor "
            )
        finally:
            await task
            await store.close()

    async def test_job_wait_rejects_invalid_cursor_argument(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        handler = JobWaitHandler(event_store=store)

        result = await handler.handle({"job_id": "job_wait_bad_cursor", "cursor": "later"})

        assert result.is_err
        assert "cursor must be a non-negative integer" in str(result.error)

    async def test_job_wait_rejects_negative_timeout_argument(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        handler = JobWaitHandler(event_store=store)

        result = await handler.handle({"job_id": "job_wait_bad_timeout", "timeout_seconds": -1})

        assert result.is_err
        assert "timeout_seconds must be a non-negative integer" in str(result.error)

    async def test_job_result_invalid_handle_returns_mcp_error(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        await store.initialize()
        try:
            handler = JobResultHandler(event_store=store)
            result = await handler.handle({"job_id": "job_missing_detached_auto"})
        finally:
            await store.close()

        assert result.is_err
        assert "not found" in str(result.error).lower()

    async def test_detached_auto_status_retrieval_tracks_running_then_terminal_result(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        auto_session_id = "auto_detached_status"
        stop = asyncio.Event()

        async def _runner() -> MCPToolResult:
            await stop.wait()
            return MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text="detached auto completed artifact",
                    ),
                    MCPContentItem(
                        type=ContentType.RESOURCE,
                        uri="file:///tmp/auto-detached-status-result.json",
                    ),
                ),
                is_error=False,
                meta={
                    "auto_session_id": auto_session_id,
                    "status": "completed",
                    "result": {
                        "artifact": "auto-detached-status-result.json",
                        "ok": True,
                    },
                },
            )

        try:
            started = await manager.start_job(
                job_type="auto",
                initial_message="Queued detached auto",
                runner=_runner(),
                links=JobLinks(session_id=auto_session_id),
            )
            await _wait_for_job_status(manager, started.job_id, JobStatus.RUNNING)

            status_handler = JobStatusHandler(event_store=store, job_manager=manager)
            running_status = await status_handler.handle({"job_id": started.job_id, "view": "full"})

            assert running_status.is_ok
            running_text = running_status.value.content[0].text
            assert f"## Job: {started.job_id}" in running_text
            assert "**Type**: auto" in running_text
            assert "**Status**: running" in running_text
            assert "**Terminal**: false" in running_text
            assert "**Status Category**: non_terminal" in running_text
            assert f"**Session ID**: {auto_session_id}" in running_text
            assert running_status.value.meta["job_id"] == started.job_id
            assert running_status.value.meta["job_type"] == "auto"
            assert running_status.value.meta["status"] == "running"
            assert running_status.value.meta["lifecycle_status"] == "running"
            assert running_status.value.meta["status_category"] == "non_terminal"
            assert running_status.value.meta["is_terminal"] is False
            assert running_status.value.meta["result_available"] is False
            assert running_status.value.meta["error"] is None
            assert running_status.value.meta["status"] not in {
                "completed",
                "failed",
                "cancelled",
            }
            assert running_status.value.is_error is False
            assert running_status.value.meta["session_id"] == auto_session_id
            assert running_status.value.meta["links"] == {
                "session_id": auto_session_id,
                "execution_id": None,
                "lineage_id": None,
            }
            assert {
                "job_id",
                "job_type",
                "status",
                "lifecycle_status",
                "status_category",
                "is_terminal",
                "result_available",
                "error",
                "cursor",
                "links",
                "session_id",
                "execution_id",
                "lineage_id",
                "view",
            }.issubset(running_status.value.meta)

            wait_handler = JobWaitHandler(event_store=store, job_manager=manager)
            running_wait = await wait_handler.handle(
                {"job_id": started.job_id, "cursor": 0, "timeout_seconds": 0, "view": "summary"}
            )
            assert running_wait.is_ok
            assert started.job_id in running_wait.value.content[0].text
            assert "running" in running_wait.value.content[0].text
            assert running_wait.value.meta["job_id"] == started.job_id
            assert running_wait.value.meta["job_type"] == "auto"
            assert running_wait.value.meta["status"] == "running"
            assert running_wait.value.meta["lifecycle_status"] == "running"
            assert running_wait.value.meta["status_category"] == "non_terminal"
            assert running_wait.value.meta["is_terminal"] is False
            assert running_wait.value.meta["result_available"] is False
            assert running_wait.value.meta["error"] is None
            assert running_wait.value.meta["session_id"] == auto_session_id
            assert running_wait.value.meta["links"] == {
                "session_id": auto_session_id,
                "execution_id": None,
                "lineage_id": None,
            }

            result_handler = JobResultHandler(event_store=store, job_manager=manager)
            premature_result = await result_handler.handle({"job_id": started.job_id})
            assert premature_result.is_ok
            assert premature_result.value.is_error is False
            premature_text = premature_result.value.content[0].text
            assert f"Job result not ready: {started.job_id}" in premature_text
            assert "status=running" in premature_text
            assert "terminal=false" in premature_text
            assert "detached auto job is still tracked background work" in premature_text
            assert f"wait: ouroboros job wait {started.job_id}" in premature_text
            assert (
                f"retrieve after terminal status: ouroboros job result {started.job_id}"
                in premature_text
            )
            assert premature_result.value.meta["job_id"] == started.job_id
            assert premature_result.value.meta["status"] == "running"
            assert premature_result.value.meta["is_terminal"] is False
            assert premature_result.value.meta["result_available"] is False
            assert premature_result.value.meta["session_id"] == auto_session_id

            still_running = await manager.get_snapshot(started.job_id)
            assert still_running.status is JobStatus.RUNNING
            assert still_running.is_terminal is False

            stop.set()
            await _wait_for_job_status(manager, started.job_id, JobStatus.COMPLETED)

            terminal_status = await status_handler.handle(
                {"job_id": started.job_id, "view": "summary"}
            )
            assert terminal_status.is_ok
            assert started.job_id in terminal_status.value.content[0].text
            assert "completed" in terminal_status.value.content[0].text
            assert terminal_status.value.meta["status"] == "completed"
            assert terminal_status.value.meta["is_terminal"] is True
            assert terminal_status.value.meta["session_id"] == auto_session_id

            final_result = await result_handler.handle({"job_id": started.job_id})
            assert final_result.is_ok
            assert final_result.value.content == (
                MCPContentItem(
                    type=ContentType.TEXT,
                    text="detached auto completed artifact",
                ),
                MCPContentItem(
                    type=ContentType.RESOURCE,
                    uri="file:///tmp/auto-detached-status-result.json",
                ),
            )
            assert final_result.value.meta["status"] == "completed"
            assert final_result.value.meta["lifecycle_status"] == "completed"
            assert final_result.value.meta["is_terminal"] is True
            assert final_result.value.meta["result_available"] is True
            assert final_result.value.meta["session_id"] == auto_session_id
            assert final_result.value.meta["auto_session_id"] == auto_session_id
            assert final_result.value.meta["result"] == {
                "artifact": "auto-detached-status-result.json",
                "ok": True,
            }
            assert final_result.value.meta["result_payload"]["is_error"] is False
            assert final_result.value.meta["result_payload"]["content"][0]["text"] == (
                "detached auto completed artifact"
            )
            assert final_result.value.meta["result_payload"]["content"][1]["uri"] == (
                "file:///tmp/auto-detached-status-result.json"
            )
        finally:
            stop.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_job_wait_completed_detached_auto_returns_success_status(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        auto_session_id = "auto_wait_completed"

        async def _runner() -> MCPToolResult:
            return MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text="detached auto completed through wait",
                    ),
                ),
                is_error=False,
                meta={
                    "auto_session_id": auto_session_id,
                    "status": "completed",
                },
            )

        try:
            started = await manager.start_job(
                job_type="auto",
                initial_message="Queued detached auto",
                runner=_runner(),
                links=JobLinks(session_id=auto_session_id),
            )
            await _wait_for_job_status(manager, started.job_id, JobStatus.COMPLETED)

            handler = JobWaitHandler(event_store=store, job_manager=manager)
            result = await handler.handle(
                {
                    "job_id": started.job_id,
                    "cursor": 0,
                    "timeout_seconds": 0,
                    "view": "summary",
                }
            )

            assert result.is_ok
            assert result.value.is_error is False
            assert result.value.meta["job_id"] == started.job_id
            assert result.value.meta["status"] == "completed"
            assert result.value.meta["is_terminal"] is True
            assert result.value.meta["changed"] is True
            assert result.value.meta["session_id"] == auto_session_id
            assert started.job_id in result.value.text_content
            assert "completed" in result.value.text_content
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_job_wait_meta_includes_polling_links_for_clients(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        snapshot = JobSnapshot(
            job_id="job_wait_links",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=21,
            links=JobLinks(
                session_id="orch_wait_links",
                execution_id="exec_wait_links",
                lineage_id="lin_wait_links",
            ),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                return snapshot, True

        handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
        await store.initialize()
        try:
            result = await handler.handle(
                {"job_id": "job_wait_links", "cursor": 0, "timeout_seconds": 0}
            )
        finally:
            await store.close()

        assert result.is_ok
        assert result.value.meta["job_id"] == "job_wait_links"
        assert result.value.meta["status"] == "running"
        assert result.value.meta["cursor"] == 21
        assert result.value.meta["session_id"] == "orch_wait_links"
        assert result.value.meta["execution_id"] == "exec_wait_links"
        assert result.value.meta["lineage_id"] == "lin_wait_links"

    async def test_job_wait_linked_stream_surfaces_subagent_events(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        await store.initialize()
        await store.append(
            BaseEvent(
                type="subagent.dispatched",
                aggregate_type="subagent",
                aggregate_id="orch_stream_links",
                data={
                    "session_id": "orch_stream_links",
                    "tool_name": "ouroboros_qa",
                    "title": "QA Review",
                },
            )
        )
        snapshot = JobSnapshot(
            job_id="job_wait_linked_stream",
            job_type="auto",
            status=JobStatus.RUNNING,
            message="Running auto",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(session_id="orch_stream_links"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 0
                assert timeout_seconds == 0
                return snapshot, False

        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            result = await handler.handle(
                {
                    "job_id": "job_wait_linked_stream",
                    "cursor": 0,
                    "timeout_seconds": 0,
                    "stream": "linked",
                }
            )
        finally:
            await store.close()

        assert result.is_ok
        assert result.value.meta["changed"] is True
        assert result.value.meta["stream"] == "linked"
        assert result.value.meta["cursor"] > 0
        assert result.value.meta["stream_events"][0]["type"] == "subagent.dispatched"
        assert result.value.meta["stream_events"][0]["scope"] == "subagent:orch_stream_links"
        assert "### Stream Events" in result.value.text_content
        assert "`subagent.dispatched` [subagent:orch_stream_links] -- QA Review" in (
            result.value.text_content
        )

    async def test_job_wait_linked_stream_does_not_advance_past_omitted_events(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        await store.initialize()
        for index in range(12):
            await store.append(
                BaseEvent(
                    type="subagent.output",
                    aggregate_type="subagent",
                    aggregate_id=f"orch_stream_burst_{index}",
                    data={
                        "session_id": "orch_stream_burst",
                        "message": f"burst {index}",
                    },
                )
            )
        snapshot = JobSnapshot(
            job_id="job_wait_linked_burst",
            job_type="auto",
            status=JobStatus.RUNNING,
            message="Running auto",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(session_id="orch_stream_burst"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert timeout_seconds == 0
                return replace(snapshot, cursor=cursor), False

        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            # First poll over a stale cursor returns a bounded page (the per-stream
            # limit is 10), not all 12 events, so the MCP response stays bounded
            # regardless of how far behind the caller cursor is.
            result = await handler.handle(
                {
                    "job_id": "job_wait_linked_burst",
                    "cursor": 0,
                    "timeout_seconds": 0,
                    "stream": "linked",
                }
            )
            assert result.is_ok
            first_cursor = result.value.meta["cursor"]
            first_page = [item["detail"] for item in result.value.meta["stream_events"]]
            assert first_page == [f"burst {index}" for index in range(10)]
            assert result.value.meta["stream_has_more"] is True
            assert "more=1" in result.value.text_content or "More linked events pending" in (
                result.value.text_content
            )

            # The cursor advanced only to the page boundary, so the follow-up poll
            # drains the remainder without skipping the omitted events.
            second = await handler.handle(
                {
                    "job_id": "job_wait_linked_burst",
                    "cursor": first_cursor,
                    "timeout_seconds": 0,
                    "stream": "linked",
                }
            )
            assert second.is_ok
            second_cursor = second.value.meta["cursor"]
            second_page = [item["detail"] for item in second.value.meta["stream_events"]]
            assert second_page == [f"burst {index}" for index in range(10, 12)]
            assert second.value.meta["stream_has_more"] is False
            assert second_cursor > first_cursor

            # Every burst event is delivered exactly once across the two pages,
            # with none skipped past the advancing cursor.
            assert first_page + second_page == [f"burst {index}" for index in range(12)]

            unchanged = await handler.handle(
                {
                    "job_id": "job_wait_linked_burst",
                    "cursor": second_cursor,
                    "timeout_seconds": 0,
                    "stream": "linked",
                }
            )
        finally:
            await store.close()

        assert unchanged.is_ok
        assert unchanged.value.meta["stream_events"] == []
        assert unchanged.value.meta["stream_has_more"] is False
        assert unchanged.value.meta["cursor"] == second_cursor
        assert "No new job-level events during this wait window." in unchanged.value.text_content

    async def test_job_wait_linked_stream_drains_stale_cursor_in_bounded_pages(
        self, tmp_path
    ) -> None:
        """A stale cursor over a large backlog is drained in bounded pages.

        Regression guard for the unbounded-response risk: removing the old
        suffix truncation must not let a first/stale ``stream="linked"`` poll
        materialize the entire backlog. Each page stays within the per-stream
        limit, the cursor advances monotonically, and every event is delivered
        exactly once with none skipped past the advancing cursor.
        """
        store = _build_store(tmp_path)
        await store.initialize()
        total_events = 25
        for index in range(total_events):
            await store.append(
                BaseEvent(
                    type="subagent.output",
                    aggregate_type="subagent",
                    aggregate_id=f"orch_backlog_{index}",
                    data={
                        "session_id": "orch_backlog",
                        "message": f"event {index:02d}",
                    },
                )
            )
        snapshot = JobSnapshot(
            job_id="job_wait_linked_backlog",
            job_type="auto",
            status=JobStatus.RUNNING,
            message="Running auto",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(session_id="orch_backlog"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert timeout_seconds == 0
                return replace(snapshot, cursor=cursor), False

        delivered: list[str] = []
        cursor = 0
        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            for _ in range(total_events + 1):  # bounded loop; must converge well before
                result = await handler.handle(
                    {
                        "job_id": "job_wait_linked_backlog",
                        "cursor": cursor,
                        "timeout_seconds": 0,
                        "stream": "linked",
                    }
                )
                assert result.is_ok
                page = [item["detail"] for item in result.value.meta["stream_events"]]
                # Every page is bounded by the per-stream limit (single session stream).
                assert len(page) <= 10
                next_cursor = result.value.meta["cursor"]
                if not page:
                    assert result.value.meta["stream_has_more"] is False
                    break
                # Cursor advances strictly while events remain, guaranteeing progress.
                assert next_cursor > cursor
                cursor = next_cursor
                delivered.extend(page)
                if not result.value.meta["stream_has_more"]:
                    # A final empty poll confirms the backlog is fully drained.
                    continue
        finally:
            await store.close()

        # Single session stream → contiguous delivery with no duplicates or gaps.
        assert delivered == [f"event {index:02d}" for index in range(total_events)]

    async def test_job_wait_linked_stream_mixed_streams_no_duplicate_delivery(
        self, tmp_path
    ) -> None:
        """A saturated stream must not drag higher-rowid sibling events into a page.

        Regression for cross-stream duplicate delivery: with a full job page and
        one higher-rowid session event, the page boundary is clamped to the job
        stream's cursor, so the session event is held back rather than returned
        above the cursor (which would re-deliver it on the next poll). It must
        surface exactly once, on the following poll.
        """
        store = _build_store(tmp_path)
        await store.initialize()
        for index in range(10):
            await store.append(
                BaseEvent(
                    type="job.progress",
                    aggregate_type="job",
                    aggregate_id="job_mixed_streams",
                    data={"message": f"job {index}"},
                )
            )
        # Appended last, so this carries the highest rowid of the linked set.
        await store.append(
            BaseEvent(
                type="subagent.dispatched",
                aggregate_type="subagent",
                aggregate_id="orch_mixed",
                data={"session_id": "orch_mixed", "title": "late subagent"},
            )
        )
        snapshot = JobSnapshot(
            job_id="job_mixed_streams",
            job_type="auto",
            status=JobStatus.RUNNING,
            message="Running auto",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(session_id="orch_mixed"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert timeout_seconds == 0
                return replace(snapshot, cursor=cursor), False

        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            first = await handler.handle(
                {
                    "job_id": "job_mixed_streams",
                    "cursor": 0,
                    "timeout_seconds": 0,
                    "stream": "linked",
                }
            )
            assert first.is_ok
            first_types = [item["type"] for item in first.value.meta["stream_events"]]
            # The session event (higher rowid) is held back behind the boundary.
            assert first_types == ["job.progress"] * 10
            assert "subagent.dispatched" not in first_types
            assert first.value.meta["stream_has_more"] is True
            first_cursor = first.value.meta["cursor"]

            second = await handler.handle(
                {
                    "job_id": "job_mixed_streams",
                    "cursor": first_cursor,
                    "timeout_seconds": 0,
                    "stream": "linked",
                }
            )
            assert second.is_ok
            second_types = [item["type"] for item in second.value.meta["stream_events"]]
            # Delivered now, exactly once, with no job events repeated.
            assert second_types == ["subagent.dispatched"]
            assert second.value.meta["cursor"] > first_cursor
        finally:
            await store.close()

    async def test_job_wait_linked_stream_execution_scan_does_not_skip_held_events(
        self, tmp_path
    ) -> None:
        """The published cursor must not advance past held-back linked events.

        Regression: the handler separately scans execution events and folds their
        cursor into the response. With a saturated job page, a held session event,
        and a later execution progress event, that scan could publish a cursor
        above the held session event's rowid, skipping it forever. The linked
        cursor must stay pinned to the bounded page boundary so the held event
        surfaces on the next poll.
        """
        store = _build_store(tmp_path)
        await store.initialize()
        for index in range(10):
            await store.append(
                BaseEvent(
                    type="job.progress",
                    aggregate_type="job",
                    aggregate_id="job_skip",
                    data={"message": f"job {index}"},
                )
            )
        await store.append(
            BaseEvent(
                type="subagent.dispatched",
                aggregate_type="subagent",
                aggregate_id="orch_skip",
                data={"session_id": "orch_skip", "title": "held subagent"},
            )
        )
        # Highest rowid: a later execution progress event whose cursor must not
        # be allowed to leapfrog the held subagent event above.
        await store.append(
            BaseEvent(
                type="execution.node.updated",
                aggregate_type="execution",
                aggregate_id="exec_skip",
                data={"execution_id": "exec_skip", "node": "n1"},
            )
        )
        snapshot = JobSnapshot(
            job_id="job_skip",
            job_type="auto",
            status=JobStatus.RUNNING,
            message="Running auto",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(session_id="orch_skip", execution_id="exec_skip"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert timeout_seconds == 0
                return replace(snapshot, cursor=cursor), False

        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            first = await handler.handle(
                {
                    "job_id": "job_skip",
                    "cursor": 0,
                    "timeout_seconds": 0,
                    "stream": "linked",
                }
            )
            assert first.is_ok
            first_types = [item["type"] for item in first.value.meta["stream_events"]]
            assert first_types == ["job.progress"] * 10
            assert first.value.meta["stream_has_more"] is True
            first_cursor = first.value.meta["cursor"]

            second = await handler.handle(
                {
                    "job_id": "job_skip",
                    "cursor": first_cursor,
                    "timeout_seconds": 0,
                    "stream": "linked",
                }
            )
            assert second.is_ok
            second_types = [item["type"] for item in second.value.meta["stream_events"]]
            # The held subagent event is NOT skipped by the execution cursor; it
            # surfaces alongside the execution event on the next poll.
            assert "subagent.dispatched" in second_types
            assert "execution.node.updated" in second_types
            assert second.value.meta["cursor"] > first_cursor
        finally:
            await store.close()

    async def test_job_wait_default_stream_does_not_surface_subagent_events(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        await store.initialize()
        await store.append(
            BaseEvent(
                type="subagent.dispatched",
                aggregate_type="subagent",
                aggregate_id="orch_default_stream",
                data={"session_id": "orch_default_stream", "title": "Hidden unless opted in"},
            )
        )
        snapshot = JobSnapshot(
            job_id="job_wait_default_stream",
            job_type="auto",
            status=JobStatus.RUNNING,
            message="Running auto",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(session_id="orch_default_stream"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 0
                assert timeout_seconds == 0
                return snapshot, False

        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            result = await handler.handle(
                {"job_id": "job_wait_default_stream", "cursor": 0, "timeout_seconds": 0}
            )
        finally:
            await store.close()

        assert result.is_ok
        assert result.value.meta["changed"] is False
        assert result.value.meta["stream"] == "progress"
        assert result.value.meta["stream_events"] == []
        assert "Hidden unless opted in" not in result.value.text_content

    async def test_job_wait_summary_view_returns_compact_unchanged_line(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        snapshot = JobSnapshot(
            job_id="job_wait_summary",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=15,
            links=JobLinks(),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                return snapshot, False

        handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
        result = await handler.handle(
            {
                "job_id": "job_wait_summary",
                "cursor": 15,
                "timeout_seconds": 0,
                "view": "summary",
            }
        )

        assert result.is_ok
        assert result.value.meta["view"] == "summary"
        assert result.value.text_content == "unchanged cursor=15"

    async def test_job_wait_compact_view_surfaces_execution_progress_without_job_change(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        await store.initialize()
        await store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_wait_live_progress",
                data={
                    "execution_id": "exec_wait_live_progress",
                    "completed_count": 1,
                    "total_count": 3,
                    "current_phase": "Implement",
                    "activity": "Running",
                },
            )
        )
        snapshot = JobSnapshot(
            job_id="job_wait_live_progress",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Implement | 1/3 ACs",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(execution_id="exec_wait_live_progress"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 0
                assert timeout_seconds == 0
                return snapshot, False

        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            result = await handler.handle(
                {
                    "job_id": "job_wait_live_progress",
                    "cursor": 0,
                    "timeout_seconds": 0,
                    "view": "compact",
                }
            )

            assert result.is_ok
            assert result.value.meta["changed"] is True
            assert result.value.meta["view"] == "compact"
            assert result.value.meta["ac_completed"] == 1
            assert result.value.meta["cursor"] > 0
            assert result.value.text_content == (
                "job_wait_live_progress | running | Implement | AC 1/3 | "
                f"cursor {result.value.meta['cursor']}"
            )

            second_cursor = result.value.meta["cursor"]

            class UnchangedJobManager:
                async def wait_for_change(
                    self,
                    job_id: str,
                    *,
                    cursor: int,
                    timeout_seconds: int,
                ) -> tuple[JobSnapshot, bool]:
                    assert job_id == snapshot.job_id
                    assert cursor == second_cursor
                    assert timeout_seconds == 0
                    return snapshot, False

            unchanged_handler = JobWaitHandler(
                event_store=store,
                job_manager=UnchangedJobManager(),
            )
            unchanged = await unchanged_handler.handle(
                {
                    "job_id": "job_wait_live_progress",
                    "cursor": second_cursor,
                    "timeout_seconds": 0,
                    "view": "compact",
                }
            )

            assert unchanged.is_ok
            assert unchanged.value.meta["changed"] is False
            assert unchanged.value.text_content == f"unchanged cursor={second_cursor}"
        finally:
            await store.close()

    async def test_job_wait_full_view_labels_execution_progress_without_job_change(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        await store.initialize()
        await store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_wait_full_progress",
                data={
                    "execution_id": "exec_wait_full_progress",
                    "completed_count": 1,
                    "total_count": 3,
                    "current_phase": "Implement",
                    "activity": "Running",
                },
            )
        )
        snapshot = JobSnapshot(
            job_id="job_wait_full_progress",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Implement | 1/3 ACs",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(execution_id="exec_wait_full_progress"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 0
                assert timeout_seconds == 0
                return snapshot, False

        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            result = await handler.handle(
                {
                    "job_id": "job_wait_full_progress",
                    "cursor": 0,
                    "timeout_seconds": 0,
                }
            )

            assert result.is_ok
            assert result.value.meta["changed"] is True
            assert "**AC Progress**: 1/3" in result.value.text_content
            assert "Execution progress updated during this wait window." in (
                result.value.text_content
            )
            assert "No new job-level events during this wait window." not in (
                result.value.text_content
            )
        finally:
            await store.close()

    async def test_job_wait_compact_view_surfaces_subtask_progress_before_workflow(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        await store.initialize()
        await store.append(
            BaseEvent(
                type="execution.subtask.updated",
                aggregate_type="execution",
                aggregate_id="exec_wait_subtask_only",
                data={
                    "ac_index": 1,
                    "sub_task_index": 1,
                    "sub_task_id": "ac_1_sub_1",
                    "content": "Child one",
                    "status": "executing",
                },
            )
        )
        snapshot = JobSnapshot(
            job_id="job_wait_subtask_only",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(execution_id="exec_wait_subtask_only"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 0
                assert timeout_seconds == 0
                return snapshot, False

        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            result = await handler.handle(
                {
                    "job_id": "job_wait_subtask_only",
                    "cursor": 0,
                    "timeout_seconds": 0,
                    "view": "compact",
                }
            )

            assert result.is_ok
            assert result.value.meta["changed"] is True
            assert result.value.meta["view"] == "compact"
            assert result.value.meta["sub_ac_executing"] == 1
            assert result.value.meta["cursor"] > 0
            assert result.value.text_content == (
                "job_wait_subtask_only | running | Sub-AC work | Sub-AC 0/1 | "
                f"cursor {result.value.meta['cursor']}"
            )
        finally:
            await store.close()

    async def test_find_active_job_by_lineage_recovers_in_flight_job(self, tmp_path) -> None:
        """A non-terminal Ralph job is rediscoverable by lineage_id.

        Pins the auto-pipeline RALPH_HANDOFF resume contract: when
        ``ralph_lineage_id`` is persisted but ``ralph_job_id`` is not yet
        saved (gap window between ``start_job`` returning and the auto
        pipeline persisting the job_id), the resume path must re-attach
        to the in-flight job rather than dispatch a duplicate.
        """
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        gate: asyncio.Event = asyncio.Event()

        try:

            async def _slow_runner() -> MCPToolResult:
                await gate.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ralph done"),),
                    is_error=False,
                )

            assert (await manager.find_active_job_by_lineage("lin_recovery")) is None

            started = await manager.start_job(
                job_type="ralph",
                initial_message="queued",
                runner=_slow_runner(),
                links=JobLinks(lineage_id="lin_recovery"),
            )

            recovered = await manager.find_active_job_by_lineage("lin_recovery", job_type="ralph")
            assert recovered is not None
            assert recovered.job_id == started.job_id
            assert recovered.links.lineage_id == "lin_recovery"

            assert (
                await manager.find_active_job_by_lineage("lin_recovery", job_type="evolve")
            ) is None

            gate.set()
            await asyncio.sleep(0.05)

            assert (await manager.find_active_job_by_lineage("lin_recovery")) is None
            terminal_recovered = await manager.find_active_job_by_lineage(
                "lin_recovery", job_type="ralph", include_terminal=True
            )
            assert terminal_recovered is not None
            assert terminal_recovered.job_id == started.job_id
            assert terminal_recovered.status == JobStatus.COMPLETED
        finally:
            gate.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_detached_auto_status_output_includes_stable_status_category(
        self, tmp_path
    ) -> None:
        """Runnable API check for stable detached status category output."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        gate: asyncio.Event = asyncio.Event()

        try:

            async def _slow_runner() -> MCPToolResult:
                await gate.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="auto done"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="auto",
                initial_message="queued",
                runner=_slow_runner(),
                links=JobLinks(session_id="auto_status_category"),
            )
            await _wait_for_job_status(manager, started.job_id, JobStatus.RUNNING)

            status_handler = JobStatusHandler(event_store=store, job_manager=manager)
            status = await status_handler.handle({"job_id": started.job_id, "view": "full"})

            assert status.is_ok
            assert "**Status Category**: non_terminal" in status.value.text_content
            assert status.value.meta["status"] == "running"
            assert status.value.meta["lifecycle_status"] == "running"
            assert status.value.meta["status_category"] == "non_terminal"
            assert status.value.meta["is_terminal"] is False
        finally:
            gate.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_find_active_job_by_lineage_recovers_persisted_job_after_restart(
        self, tmp_path
    ) -> None:
        """A fresh JobManager can rediscover a persisted non-terminal lineage job."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        gate: asyncio.Event = asyncio.Event()

        try:

            async def _slow_runner() -> MCPToolResult:
                await gate.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ralph done"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="ralph",
                initial_message="queued",
                runner=_slow_runner(),
                links=JobLinks(lineage_id="lin_after_restart"),
            )

            restarted_manager = JobManager(store)
            recovered = await restarted_manager.find_active_job_by_lineage(
                "lin_after_restart", job_type="ralph"
            )

            assert recovered is not None
            assert recovered.job_id == started.job_id
            assert recovered.links.lineage_id == "lin_after_restart"
            assert recovered.status in {JobStatus.QUEUED, JobStatus.RUNNING}
            assert started.job_id in restarted_manager._known_job_ids
        finally:
            gate.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_find_active_job_by_session_recovers_in_flight_job(self, tmp_path) -> None:
        """A non-terminal auto job is rediscoverable by session_id."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        gate: asyncio.Event = asyncio.Event()

        try:

            async def _slow_runner() -> MCPToolResult:
                await gate.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="auto done"),),
                    is_error=False,
                )

            assert (await manager.find_active_job_by_session("auto_recovery")) is None

            started = await manager.start_job(
                job_type="auto",
                initial_message="queued",
                runner=_slow_runner(),
                links=JobLinks(session_id="auto_recovery"),
            )

            recovered = await manager.find_active_job_by_session("auto_recovery", job_type="auto")
            assert recovered is not None
            assert recovered.job_id == started.job_id
            assert recovered.links.session_id == "auto_recovery"

            assert (
                await manager.find_active_job_by_session("auto_recovery", job_type="ralph")
            ) is None

            gate.set()
            await asyncio.sleep(0.05)

            assert (await manager.find_active_job_by_session("auto_recovery")) is None
            terminal_recovered = await manager.find_active_job_by_session(
                "auto_recovery", job_type="auto", include_terminal=True
            )
            assert terminal_recovered is not None
            assert terminal_recovered.job_id == started.job_id
            assert terminal_recovered.status == JobStatus.COMPLETED
        finally:
            gate.set()
            await _cancel_manager_tasks(manager)
            await store.close()


class TestZombieJobReconciliation:
    """Reconcile non-terminal jobs whose owning process is provably gone.

    Closes the R-zombie gap: a job stuck in RUNNING/QUEUED whose owner crashed
    (with no recoverable linked-execution evidence) must not report RUNNING
    forever. Authoritative liveness uses the recorded owner PID + start time.
    """

    async def _seed_running_job(
        self,
        store: EventStore,
        job_id: str,
        *,
        owner_pid: int | None,
        owner_start_time: float | None,
        session_id: str | None = None,
    ) -> None:
        writer = JobManager(store)
        data: dict = {
            "job_type": "execute_seed",
            "status": JobStatus.RUNNING.value,
            "message": "Running execute_seed",
            "links": {"session_id": session_id, "execution_id": None, "lineage_id": None},
        }
        if owner_pid is not None:
            data["owner_pid"] = owner_pid
            data["owner_start_time"] = owner_start_time
        await writer._append_event("mcp.job.created", job_id, data)

    async def test_running_job_reconciled_to_interrupted_when_owner_dead(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        try:
            await self._seed_running_job(
                store, "job_zombie", owner_pid=4_242_424, owner_start_time=111.0
            )
            restarted = JobManager(store)

            with patch.object(job_manager_module, "is_process_identity_alive", return_value=False):
                snapshot = await restarted.get_snapshot("job_zombie")

            assert snapshot.status is JobStatus.INTERRUPTED
            assert snapshot.result_meta["interrupted_from_dead_owner"] is True
            assert snapshot.is_terminal is True
            events, _ = await store.get_events_after("job", "job_zombie", last_row_id=0)
            assert [event.type for event in events] == [
                "mcp.job.created",
                "mcp.job.interrupted",
            ]
        finally:
            await store.close()

    async def test_running_job_not_reconciled_when_owner_alive(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        try:
            await self._seed_running_job(
                store, "job_live", owner_pid=4_242_424, owner_start_time=111.0
            )
            restarted = JobManager(store)

            with patch.object(job_manager_module, "is_process_identity_alive", return_value=True):
                snapshot = await restarted.get_snapshot("job_live")

            assert snapshot.status is JobStatus.RUNNING
            events, _ = await store.get_events_after("job", "job_live", last_row_id=0)
            assert [event.type for event in events] == ["mcp.job.created"]
        finally:
            await store.close()

    async def test_running_job_not_reconciled_when_linked_session_still_alive(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        try:
            await self._seed_running_job(
                store,
                "job_linked_live",
                owner_pid=4_242_424,
                owner_start_time=111.0,
                session_id="orch_live",
            )
            restarted = JobManager(store)

            # Owner MCP process is gone, but the linked session runtime still
            # holds a live heartbeat lock — it remains the progress authority,
            # so the job must not be terminalized.
            with (
                patch.object(job_manager_module, "is_process_identity_alive", return_value=False),
                patch.object(job_manager_module, "is_holder_alive", return_value=True) as holder,
            ):
                snapshot = await restarted.get_snapshot("job_linked_live")

            holder.assert_any_call("orch_live")
            assert snapshot.status is JobStatus.RUNNING
            assert snapshot.is_terminal is False
            events, _ = await store.get_events_after("job", "job_linked_live", last_row_id=0)
            # Nothing persisted — the live linked session must be able to finish.
            assert [event.type for event in events] == ["mcp.job.created"]
        finally:
            await store.close()

    async def test_dead_owner_with_dead_linked_session_is_reconciled(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        try:
            await self._seed_running_job(
                store,
                "job_linked_dead",
                owner_pid=4_242_424,
                owner_start_time=111.0,
                session_id="orch_dead",
            )
            restarted = JobManager(store)

            # Owner gone AND no live linked holder → genuinely orphaned, so the
            # session-liveness guard must not suppress reconciliation.
            with (
                patch.object(job_manager_module, "is_process_identity_alive", return_value=False),
                patch.object(job_manager_module, "is_holder_alive", return_value=False),
            ):
                snapshot = await restarted.get_snapshot("job_linked_dead")

            assert snapshot.status is JobStatus.INTERRUPTED
            assert snapshot.result_meta["interrupted_from_dead_owner"] is True
            events, _ = await store.get_events_after("job", "job_linked_dead", last_row_id=0)
            assert [event.type for event in events] == [
                "mcp.job.created",
                "mcp.job.interrupted",
            ]
        finally:
            await store.close()

    async def test_legacy_job_without_owner_identity_is_not_reconciled(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        try:
            await self._seed_running_job(store, "job_legacy", owner_pid=None, owner_start_time=None)
            restarted = JobManager(store)

            with patch.object(
                job_manager_module, "is_process_identity_alive", return_value=False
            ) as alive:
                snapshot = await restarted.get_snapshot("job_legacy")

            # Owner identity unknown → conservative: never consult liveness, never reconcile.
            alive.assert_not_called()
            assert snapshot.status is JobStatus.RUNNING
            events, _ = await store.get_events_after("job", "job_legacy", last_row_id=0)
            assert [event.type for event in events] == ["mcp.job.created"]
        finally:
            await store.close()

    async def test_reconciliation_is_idempotent(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        try:
            await self._seed_running_job(
                store, "job_idem", owner_pid=4_242_424, owner_start_time=111.0
            )
            restarted = JobManager(store)

            with patch.object(job_manager_module, "is_process_identity_alive", return_value=False):
                first = await restarted.get_snapshot("job_idem")
                second = await restarted.get_snapshot("job_idem")

            assert first.status is JobStatus.INTERRUPTED
            assert second.status is JobStatus.INTERRUPTED
            events, _ = await store.get_events_after("job", "job_idem", last_row_id=0)
            # Exactly one synthetic terminal event — no duplicates on re-read.
            assert [event.type for event in events] == [
                "mcp.job.created",
                "mcp.job.interrupted",
            ]
        finally:
            await store.close()

    async def test_read_only_store_projects_interrupted_without_persisting(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        try:
            await self._seed_running_job(
                store, "job_ro", owner_pid=4_242_424, owner_start_time=111.0
            )
            restarted = JobManager(store)
            await restarted._ensure_initialized()
            store._read_only = True

            with patch.object(job_manager_module, "is_process_identity_alive", return_value=False):
                snapshot = await restarted.get_snapshot("job_ro")

            assert snapshot.status is JobStatus.INTERRUPTED
            assert snapshot.result_meta["interrupted_from_dead_owner"] is True
            store._read_only = False
            events, _ = await store.get_events_after("job", "job_ro", last_row_id=0)
            # Projection only — nothing persisted on a read-only store.
            assert [event.type for event in events] == ["mcp.job.created"]
        finally:
            await store.close()

    async def test_start_job_records_owning_process_identity(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        try:

            async def _runner() -> MCPToolResult:
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="done"),),
                )

            started = await manager.start_job(
                job_type="qa", initial_message="Running qa", runner=_runner()
            )
            await _wait_for_job_status(manager, started.job_id, JobStatus.COMPLETED)

            events, _ = await store.get_events_after("job", started.job_id, last_row_id=0)
            created = events[0]
            assert created.type == "mcp.job.created"
            assert created.data["owner_pid"] == os.getpid()
            assert "owner_start_time" in created.data
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()
