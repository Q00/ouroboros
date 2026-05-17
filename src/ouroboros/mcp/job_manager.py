"""Async job management for long-running MCP operations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import inspect
import logging
from typing import Any
from uuid import uuid4

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.agent_process import AgentProcessHandle
from ouroboros.orchestrator.events import create_execution_terminal_event
from ouroboros.orchestrator.heartbeat import is_holder_alive, is_owned_by_current_process
from ouroboros.orchestrator.runner import clear_cancellation, request_cancellation
from ouroboros.orchestrator.session import SessionRepository, SessionStatus
from ouroboros.persistence.checkpoint import CheckpointStore
from ouroboros.persistence.event_store import EventStore


class JobStatus(StrEnum):
    """Lifecycle states for async MCP jobs."""

    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class JobLinks:
    """Cross-reference IDs attached to a job."""

    session_id: str | None = None
    execution_id: str | None = None
    lineage_id: str | None = None


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    """Materialized view of a background job."""

    job_id: str
    job_type: str
    status: JobStatus
    message: str
    created_at: datetime
    updated_at: datetime
    cursor: int = 0
    links: JobLinks = field(default_factory=JobLinks)
    result_text: str | None = None
    result_meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }


def _safe_meta(value: Any) -> Any:
    """Convert arbitrary values into JSON-safe payloads."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _safe_meta(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_meta(v) for v in value]
    return str(value)


_JOB_TTL = timedelta(hours=1)
_RECOVERED_COMPLETION_EVENT_ID_PREFIX = "mcp-job-recovered-completed-"
logger = logging.getLogger(__name__)


def _latest_job_terminal_event(events: list[BaseEvent]) -> BaseEvent | None:
    """Return the latest persisted terminal job event from a job stream."""
    terminal_types = {
        "mcp.job.completed",
        "mcp.job.failed",
        "mcp.job.cancelled",
        "mcp.job.interrupted",
    }
    return next((event for event in reversed(events) if event.type in terminal_types), None)


def _latest_job_status_event(events: list[BaseEvent]) -> BaseEvent | None:
    """Return the latest job event carrying a status field."""
    return next((event for event in reversed(events) if "status" in event.data), None)


def _snapshot_with_status_event(
    snapshot: JobSnapshot,
    event: BaseEvent,
    cursor: int,
) -> JobSnapshot:
    """Project a non-terminal status event onto an existing reconstructed snapshot."""
    data = event.data
    return replace(
        snapshot,
        status=JobStatus(data.get("status", snapshot.status.value)),
        message=data.get("message", snapshot.message),
        updated_at=event.timestamp,
        cursor=cursor,
    )


def _execution_completed_job_event(job_id: str, result_text: str) -> BaseEvent:
    """Build the synthetic/persisted job-completion event for execution recovery."""
    return BaseEvent(
        id=f"{_RECOVERED_COMPLETION_EVENT_ID_PREFIX}{job_id}",
        type="mcp.job.completed",
        aggregate_type="job",
        aggregate_id=job_id,
        data={
            "status": JobStatus.COMPLETED.value,
            "message": "Job complete",
            "result_text": result_text,
            "result_meta": {"completed_from_execution_terminal": True},
            "is_error": False,
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


def _snapshot_with_terminal_event(
    snapshot: JobSnapshot,
    event: BaseEvent,
    cursor: int,
) -> JobSnapshot:
    """Project a terminal job event onto an existing reconstructed snapshot."""
    data = event.data
    status = JobStatus(data.get("status", snapshot.status.value))
    return replace(
        snapshot,
        status=status,
        message=data.get("message", snapshot.message),
        updated_at=event.timestamp,
        cursor=cursor,
        result_text=data.get("result_text", snapshot.result_text),
        result_meta=data.get("result_meta") if isinstance(data.get("result_meta"), dict) else {},
        error=data.get("error"),
    )


class JobManager:
    """Owns background MCP jobs and persists their state as events."""

    def __init__(
        self, event_store: EventStore | None = None, checkpoint_store: CheckpointStore | None = None
    ) -> None:
        self._event_store = event_store or EventStore()
        self._checkpoint_store = checkpoint_store
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._runner_tasks: dict[str, asyncio.Task[Any]] = {}
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._recovery_locks: dict[str, asyncio.Lock] = {}
        self._initialized = False
        self._known_job_ids: set[str] = set()
        self._reserved_job_ids: set[str] = set()

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    async def allocate_job_id(self) -> str:
        """Reserve a fresh job ID before constructing a job runner."""
        await self._ensure_initialized()
        while True:
            job_id = f"job_{uuid4().hex[:12]}"
            if job_id in self._known_job_ids:
                continue
            if await self._job_exists(job_id):
                continue
            self._known_job_ids.add(job_id)
            self._reserved_job_ids.add(job_id)
            return job_id

    async def start_job(
        self,
        *,
        job_type: str,
        initial_message: str,
        runner: asyncio.Future[Any] | Any,
        links: JobLinks | None = None,
        job_id: str | None = None,
    ) -> JobSnapshot:
        """Create and start a new background job."""
        await self._ensure_initialized()

        if job_id is None:
            job_id = await self.allocate_job_id()
        elif job_id not in self._reserved_job_ids:
            if job_id in self._known_job_ids or await self._job_exists(job_id):
                raise ValueError(f"Job already exists: {job_id}")
            self._known_job_ids.add(job_id)
        self._reserved_job_ids.discard(job_id)
        job_links = links or JobLinks()

        await self._append_event(
            "mcp.job.created",
            job_id,
            {
                "job_type": job_type,
                "status": JobStatus.QUEUED.value,
                "message": initial_message,
                "links": {
                    "session_id": job_links.session_id,
                    "execution_id": job_links.execution_id,
                    "lineage_id": job_links.lineage_id,
                },
            },
        )

        # Normalise ``runner`` to a Task so ``_run_job`` can rely on Task
        # semantics (cancellation, ``done()``). Coroutines are wrapped via
        # ``create_task``; pre-built Tasks are reused; bare awaitables (e.g.
        # ``Future``) are wrapped through an inner coroutine so cancellation
        # and GC remain consistent.
        if isinstance(runner, asyncio.Task):
            runner_task = runner
        elif inspect.iscoroutine(runner):
            runner_task = asyncio.create_task(runner)
        else:

            async def _await_runner(_awaitable: Any = runner) -> Any:
                return await _awaitable

            runner_task = asyncio.create_task(_await_runner())
        self._runner_tasks[job_id] = runner_task
        task = asyncio.create_task(self._run_job(job_id, job_type, runner_task))
        self._tasks[job_id] = task
        self._monitors[job_id] = asyncio.create_task(self._monitor_job(job_id))

        return await self.get_snapshot(job_id)

    async def _job_exists(self, job_id: str) -> bool:
        events, _ = await self._event_store.get_events_after("job", job_id, last_row_id=0)
        return bool(events)

    async def _run_job(
        self,
        job_id: str,
        job_type: str,
        runner: asyncio.Task[Any],
    ) -> None:
        """Run the actual background job and persist terminal state.

        ``runner`` is always a Task — :meth:`start_job` converts coroutines
        at the boundary, so the finally-block here does not need to manage
        coroutine ``close()`` cleanup.
        """
        await self.update_status(job_id, JobStatus.RUNNING, f"Running {job_type}")

        try:
            result = await runner
        except asyncio.CancelledError:
            snapshot = await self.get_snapshot(job_id)
            if snapshot.is_terminal:
                return
            completed_result = await self._derive_completed_execution_result(snapshot)
            if completed_result is not None and snapshot.status != JobStatus.CANCEL_REQUESTED:
                await self._append_execution_completed_event(job_id, completed_result)
                return
            if not runner.done():
                runner.cancel()
                try:
                    await runner
                except asyncio.CancelledError:
                    pass
            await self._append_event(
                "mcp.job.cancelled",
                job_id,
                {
                    "status": JobStatus.CANCELLED.value,
                    "message": "Job cancelled",
                },
            )
            raise
        except Exception as exc:
            snapshot = await self.get_snapshot(job_id)
            if snapshot.is_terminal:
                return
            await self._append_event(
                "mcp.job.failed",
                job_id,
                {
                    "status": JobStatus.FAILED.value,
                    "message": f"Job failed: {exc}",
                    "error": str(exc),
                },
            )
        else:
            snapshot = await self.get_snapshot(job_id)
            if snapshot.is_terminal:
                return
            completed_result = await self._derive_completed_execution_result(snapshot)
            if completed_result is not None and snapshot.status != JobStatus.CANCEL_REQUESTED:
                await self._append_execution_completed_event(job_id, completed_result)
                return
            terminal_type = "mcp.job.completed"
            terminal_status = JobStatus.COMPLETED
            result_meta = getattr(result, "meta", {})
            terminal_kind = None
            if isinstance(result_meta, dict):
                terminal_kind = result_meta.get("action") or result_meta.get("status")
            if snapshot.status == JobStatus.CANCEL_REQUESTED:
                terminal_type = "mcp.job.cancelled"
                terminal_status = JobStatus.CANCELLED
            elif terminal_kind == "interrupted":
                terminal_type = "mcp.job.interrupted"
                terminal_status = JobStatus.INTERRUPTED
            elif terminal_kind in {"cancel", "cancelled"}:
                terminal_type = "mcp.job.cancelled"
                terminal_status = JobStatus.CANCELLED
            elif getattr(result, "is_error", False):
                terminal_type = "mcp.job.failed"
                terminal_status = JobStatus.FAILED
            await self._append_event(
                terminal_type,
                job_id,
                {
                    "status": terminal_status.value,
                    "message": {
                        JobStatus.COMPLETED: "Job complete",
                        JobStatus.CANCELLED: "Job cancelled",
                        JobStatus.INTERRUPTED: "Job interrupted",
                        JobStatus.FAILED: "Job failed",
                    }.get(terminal_status, "Job complete"),
                    "result_text": getattr(result, "text_content", str(result))[:20_000],
                    "result_meta": _safe_meta(getattr(result, "meta", {})),
                    "is_error": bool(getattr(result, "is_error", False)),
                },
            )
        finally:
            self._tasks.pop(job_id, None)
            self._runner_tasks.pop(job_id, None)
            monitor = self._monitors.pop(job_id, None)
            if monitor is not None:
                monitor.cancel()
                try:
                    await monitor
                except asyncio.CancelledError:
                    pass

    async def _monitor_job(self, job_id: str) -> None:
        """Mirror linked execution/lineage progress into job updates."""
        last_message: str | None = None
        last_update = asyncio.get_running_loop().time()
        interval = 1.0
        _HEARTBEAT_INTERVAL = 60.0
        while True:
            await asyncio.sleep(interval)
            snapshot = await self.get_snapshot(job_id)
            if snapshot.is_terminal:
                return

            if snapshot.status != JobStatus.CANCEL_REQUESTED:
                completed_result = await self._derive_completed_execution_result(snapshot)
                if completed_result is not None:
                    runner = self._runner_tasks.get(job_id)
                    if runner is not None and not runner.done():
                        runner.cancel()
                    return

            message = await self._derive_status_message(snapshot)
            now = asyncio.get_running_loop().time()
            changed = message and message != last_message
            heartbeat_due = message and (now - last_update >= _HEARTBEAT_INTERVAL)
            if changed or heartbeat_due:
                await self.update_status(job_id, snapshot.status, message)
                last_message = message
                last_update = now
                interval = 1.0  # Reset on change
            else:
                interval = min(interval * 1.5, 5.0)  # Backoff up to 5s

    async def _append_execution_completed_event(
        self,
        job_id: str,
        result_text: str,
        *,
        check_current: bool = True,
        event_id: str | None = None,
    ) -> bool:
        """Persist durable job completion derived from terminal execution evidence."""
        if check_current:
            snapshot = await self.get_snapshot(job_id)
            if snapshot.is_terminal or snapshot.status == JobStatus.CANCEL_REQUESTED:
                return False
        await self._append_event(
            "mcp.job.completed",
            job_id,
            {
                "status": JobStatus.COMPLETED.value,
                "message": "Job complete",
                "result_text": result_text,
                "result_meta": {"completed_from_execution_terminal": True},
                "is_error": False,
            },
            event_id=event_id,
        )
        return True

    async def _derive_completed_execution_result(self, snapshot: JobSnapshot) -> str | None:
        """Return a terminal result when linked execution state proves completion.

        Background execution runners normally append the terminal job event when
        the MCP handler returns. If the execution stream has already emitted its
        source-of-truth ``execution.terminal`` completion but the handler remains
        open, the job can finish from that terminal execution evidence.
        ``workflow.progress.updated`` remains observational and is used only to
        enrich the result text.
        """
        if not snapshot.links.execution_id:
            return None
        terminal_events = await self._event_store.query_events(
            aggregate_id=snapshot.links.execution_id,
            event_type="execution.terminal",
            limit=1,
        )
        if not terminal_events:
            return None
        terminal_data = terminal_events[0].data
        if terminal_data.get("status") != "completed":
            return None

        workflow_events = await self._event_store.query_events(
            aggregate_id=snapshot.links.execution_id,
            event_type="workflow.progress.updated",
            limit=1,
        )
        if workflow_events:
            data = workflow_events[0].data
            completed = data.get("completed_count")
            total = data.get("total_count")
            if (
                isinstance(completed, int)
                and isinstance(total, int)
                and total > 0
                and completed >= total
            ):
                return f"Execution complete: {completed}/{total} ACs completed"
        return "Execution complete"

    async def _derive_status_message(self, snapshot: JobSnapshot) -> str | None:
        """Summarize linked execution or lineage progress."""
        if snapshot.links.execution_id:
            workflow_events = await self._event_store.query_events(
                aggregate_id=snapshot.links.execution_id,
                event_type="workflow.progress.updated",
                limit=1,
            )
            subtask_events = await self._event_store.query_events(
                aggregate_id=snapshot.links.execution_id,
                event_type="execution.subtask.updated",
                limit=1,
            )
            workflow_event = workflow_events[0] if workflow_events else None
            subtask_event = subtask_events[0] if subtask_events else None

            if workflow_event is not None:
                data = workflow_event.data
                completed = data.get("completed_count")
                total = data.get("total_count")
                current_phase = data.get("current_phase") or "Working"
                detail = data.get("activity_detail") or data.get("activity") or ""
                if subtask_event is not None:
                    sub_data = subtask_event.data
                    sub_name = sub_data.get("content") or sub_data.get("sub_task_id") or ""
                    sub_status = sub_data.get("status") or ""
                    if sub_name:
                        detail = f"{sub_name} ({sub_status})" if sub_status else sub_name
                progress = (
                    f"{completed}/{total} ACs"
                    if completed is not None and total is not None
                    else ""
                )
                return " | ".join(part for part in (current_phase, detail, progress) if part)

            if subtask_event is not None:
                sub_data = subtask_event.data
                sub_name = sub_data.get("content") or sub_data.get("sub_task_id") or ""
                sub_status = sub_data.get("status") or ""
                if sub_name:
                    return (
                        f"Working | {sub_name} ({sub_status})"
                        if sub_status
                        else f"Working | {sub_name}"
                    )

        if snapshot.links.session_id:
            repo = SessionRepository(self._event_store)
            session = await repo.reconstruct_session(snapshot.links.session_id)
            if session.is_ok:
                tracker = session.value
                return f"Session {tracker.status.value} | messages={tracker.messages_processed}"

        if snapshot.links.lineage_id:
            events = await self._event_store.query_events(
                aggregate_id=snapshot.links.lineage_id,
                limit=10,
            )
            latest = next(
                (e for e in events if e.type.startswith("lineage.")),
                None,
            )
            if latest is not None:
                data = latest.data
                gen = data.get("generation_number")
                phase = data.get("phase")
                reason = data.get("reason")
                if latest.type == "lineage.generation.phase_changed":
                    return f"Generation {gen} | {phase}"
                if latest.type == "lineage.generation.started":
                    return f"Generation {gen} | {phase}"
                if latest.type == "lineage.generation.completed":
                    return f"Generation {gen} completed"
                if latest.type == "lineage.generation.failed":
                    return f"Generation {gen} failed | {phase}"
                if latest.type == "lineage.generation.watchdog_decision":
                    action = data.get("action", "decision")
                    return f"Generation {gen} watchdog {action} | {reason or ''}".strip()
                if latest.type == "lineage.generation.interrupted":
                    gen = data.get("generation_number", "?")
                    last_phase = data.get("last_completed_phase", "unknown")
                    return f"Generation {gen} interrupted (last phase: {last_phase})"
                if latest.type in {
                    "lineage.converged",
                    "lineage.stagnated",
                    "lineage.exhausted",
                }:
                    return f"Lineage {latest.type.split('.', 1)[1]} | {reason or ''}".strip()

        return None

    async def update_status(
        self,
        job_id: str,
        status: JobStatus,
        message: str,
        *,
        links: JobLinks | None = None,
    ) -> None:
        """Persist a non-terminal status update."""
        await self._append_event(
            "mcp.job.updated",
            job_id,
            {
                "status": status.value,
                "message": message,
                "links": {
                    "session_id": links.session_id if links else None,
                    "execution_id": links.execution_id if links else None,
                    "lineage_id": links.lineage_id if links else None,
                },
            },
        )

    async def get_snapshot(self, job_id: str) -> JobSnapshot:
        """Reconstruct the latest state of a job from persisted events."""
        await self._ensure_initialized()
        events, cursor = await self._event_store.get_events_after("job", job_id, last_row_id=0)
        if not events:
            raise ValueError(f"Job not found: {job_id}")

        created = events[0]
        created_links = created.data.get("links", {})
        status = JobStatus(created.data.get("status", JobStatus.QUEUED.value))
        message = created.data.get("message", "")
        links = JobLinks(
            session_id=created_links.get("session_id"),
            execution_id=created_links.get("execution_id"),
            lineage_id=created_links.get("lineage_id"),
        )
        result_text: str | None = None
        result_meta: dict[str, Any] = {}
        error: str | None = None

        for event in events[1:]:
            data = event.data
            link_data = data.get("links") or {}
            links = JobLinks(
                session_id=link_data.get("session_id") or links.session_id,
                execution_id=link_data.get("execution_id") or links.execution_id,
                lineage_id=link_data.get("lineage_id") or links.lineage_id,
            )

            if "status" in data:
                status = JobStatus(data["status"])
            if "message" in data:
                message = data["message"]
            if "result_text" in data:
                result_text = data["result_text"]
            if "result_meta" in data and isinstance(data["result_meta"], dict):
                result_meta = data["result_meta"]
            if "error" in data:
                error = data["error"]

        snapshot = JobSnapshot(
            job_id=job_id,
            job_type=created.data.get("job_type", "unknown"),
            status=status,
            message=message,
            created_at=created.timestamp,
            updated_at=events[-1].timestamp,
            cursor=cursor,
            links=links,
            result_text=result_text,
            result_meta=result_meta,
            error=error,
        )
        return await self._recover_completed_execution_snapshot(snapshot)

    async def _recover_completed_execution_snapshot(self, snapshot: JobSnapshot) -> JobSnapshot:
        """Recover completed linked execution jobs when no live runner remains.

        Live jobs keep the existing JobManager invariant: terminal job state is
        emitted by the runner-owned path after the runner exits or cooperates
        with cancellation. After process restart, however, there is no live
        runner left to write that event; if the linked execution already has
        authoritative completed terminal evidence, materialize the job terminal
        event from that durable evidence.
        """
        if (
            snapshot.is_terminal
            or snapshot.status == JobStatus.CANCEL_REQUESTED
            or snapshot.job_id in self._tasks
            or snapshot.job_id in self._runner_tasks
        ):
            return snapshot
        completed_result = await self._derive_completed_execution_result(snapshot)
        if completed_result is None:
            return snapshot
        if getattr(self._event_store, "_read_only", False):
            return _snapshot_with_terminal_event(
                snapshot,
                _execution_completed_job_event(snapshot.job_id, completed_result),
                snapshot.cursor,
            )
        lock = self._recovery_locks.setdefault(snapshot.job_id, asyncio.Lock())
        async with lock:
            events, cursor = await self._event_store.get_events_after(
                "job", snapshot.job_id, last_row_id=0
            )
            existing_terminal = _latest_job_terminal_event(events)
            if existing_terminal is not None:
                return _snapshot_with_terminal_event(snapshot, existing_terminal, cursor)
            latest_status_event = _latest_job_status_event(events)
            if (
                latest_status_event is not None
                and latest_status_event.data.get("status") == JobStatus.CANCEL_REQUESTED.value
            ):
                return _snapshot_with_status_event(snapshot, latest_status_event, cursor)
            try:
                completed = await self._append_execution_completed_event(
                    snapshot.job_id,
                    completed_result,
                    check_current=False,
                    event_id=f"{_RECOVERED_COMPLETION_EVENT_ID_PREFIX}{snapshot.job_id}",
                )
            except PersistenceError:
                events, cursor = await self._event_store.get_events_after(
                    "job", snapshot.job_id, last_row_id=0
                )
                existing_terminal = _latest_job_terminal_event(events)
                if existing_terminal is not None:
                    return _snapshot_with_terminal_event(snapshot, existing_terminal, cursor)
                raise
            if not completed:
                return snapshot
            events, cursor = await self._event_store.get_events_after(
                "job", snapshot.job_id, last_row_id=0
            )
            latest = events[-1]
        return _snapshot_with_terminal_event(snapshot, latest, cursor)

    async def wait_for_change(
        self,
        job_id: str,
        *,
        cursor: int = 0,
        timeout_seconds: int = 10,
    ) -> tuple[JobSnapshot, bool]:
        """Wait until the job aggregate receives a new event."""
        await self._ensure_initialized()
        deadline = asyncio.get_running_loop().time() + timeout_seconds

        while True:
            events, new_cursor = await self._event_store.get_events_after("job", job_id, cursor)
            if events:
                snapshot = await self.get_snapshot(job_id)
                return replace(snapshot, cursor=new_cursor), True

            snapshot = await self.get_snapshot(job_id)
            if snapshot.is_terminal or asyncio.get_running_loop().time() >= deadline:
                return snapshot, False

            await asyncio.sleep(0.5)

    def has_live_job_task(self, job_id: str) -> bool:
        """Return true when this process still owns live tasks for ``job_id``."""
        return any(
            task is not None and not task.done()
            for task in (
                self._tasks.get(job_id),
                self._runner_tasks.get(job_id),
                self._monitors.get(job_id),
            )
        )

    async def cancel_job(self, job_id: str) -> JobSnapshot:
        """Request cancellation for a running job."""
        snapshot = await self.get_snapshot(job_id)
        if snapshot.is_terminal:
            return snapshot

        try:
            self._persist_durable_cancel(job_id, reason="Background job cancelled")
        except Exception:  # noqa: BLE001 - durable cancel must not block live cancellation
            logger.warning(
                "job_manager.cancel_job: failed to persist durable cancel marker",
                exc_info=True,
                extra={"job_id": job_id},
            )

        linked_session_repo: SessionRepository | None = None
        linked_session_reconstructed = False
        linked_session_started = False
        linked_session_owned_by_current_process = False
        linked_session_terminal = False
        if snapshot.links.session_id:
            linked_session_repo = SessionRepository(self._event_store)
            session_result = await linked_session_repo.reconstruct_session(
                snapshot.links.session_id
            )
            linked_session_reconstructed = session_result.is_ok
            linked_session_terminal = session_result.is_ok and session_result.value.status in {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
            }
            try:
                terminal_events = await self._event_store.query_events(
                    aggregate_id=snapshot.links.session_id,
                    limit=10,
                )
                linked_session_terminal = linked_session_terminal or any(
                    event.type
                    in {
                        "orchestrator.session.completed",
                        "orchestrator.session.failed",
                        "orchestrator.session.cancelled",
                    }
                    for event in terminal_events
                )
            except Exception:
                pass
            linked_session_started = is_holder_alive(snapshot.links.session_id)
            linked_session_owned_by_current_process = is_owned_by_current_process(
                snapshot.links.session_id
            )

        await self.update_status(job_id, JobStatus.CANCEL_REQUESTED, "Cancellation requested")

        should_persist_linked_cancel = False
        if snapshot.links.session_id:
            if not linked_session_terminal:
                await request_cancellation(snapshot.links.session_id)
                should_persist_linked_cancel = linked_session_reconstructed and (
                    not linked_session_started or not linked_session_owned_by_current_process
                )

        cancelled_tasks: list[asyncio.Task[Any]] = []
        task = self._tasks.get(job_id)
        if snapshot.links.session_id is None and task is not None and not task.done():
            task.cancel()
            cancelled_tasks.append(task)
        runner_task = self._runner_tasks.get(job_id)
        if runner_task is not None and not runner_task.done():
            runner_task.cancel()
            cancelled_tasks.append(runner_task)
        if cancelled_tasks:
            await asyncio.wait(cancelled_tasks, timeout=5)
        if snapshot.links.session_id and should_persist_linked_cancel:
            repo = linked_session_repo or SessionRepository(self._event_store)
            latest_session = await repo.reconstruct_session(snapshot.links.session_id)
            if latest_session.is_err:
                raise ValueError(
                    "Failed to inspect linked session before cancellation: "
                    f"{latest_session.error.message}"
                )
            latest_terminal = latest_session.is_ok and latest_session.value.status in {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
            }
            if not latest_terminal:
                cancel_result = await repo.mark_cancelled(
                    snapshot.links.session_id,
                    reason="Background job cancelled",
                    cancelled_by="mcp_job_manager",
                )
                if cancel_result.is_err:
                    raise ValueError(
                        f"Failed to mark linked session cancelled: {cancel_result.error.message}"
                    )
                if snapshot.links.execution_id:
                    await self._event_store.append(
                        create_execution_terminal_event(
                            execution_id=snapshot.links.execution_id,
                            session_id=snapshot.links.session_id,
                            status="cancelled",
                            error_message="Background job cancelled",
                        )
                    )
        if (
            snapshot.links.session_id
            and not linked_session_started
            and not is_holder_alive(snapshot.links.session_id)
        ):
            await clear_cancellation(snapshot.links.session_id)

        return await self.get_snapshot(job_id)

    async def find_active_job_by_lineage(
        self,
        lineage_id: str,
        *,
        job_type: str | None = None,
        include_terminal: bool = False,
    ) -> JobSnapshot | None:
        """Return the most recently updated job for ``lineage_id``.

        Used by the auto pipeline RALPH_HANDOFF resume path to rediscover an
        already-running Ralph job when the auto state recorded a lineage_id
        but crashed (or returned to caller) before the job_id was persisted.
        Without this lookup, resuming an in-flight handoff would dispatch a
        second Ralph loop for the same lineage. By default, returns ``None``
        when no active match exists; terminal jobs are deliberately ignored for
        legacy callers. Set ``include_terminal=True`` for crash-gap recovery
        paths that must consume the already-finished job result instead of
        starting duplicate lineage work.
        """
        await self._ensure_initialized()
        candidates: list[JobSnapshot] = []
        candidate_job_ids = set(self._known_job_ids)
        offset = 0
        while True:
            created_events = await self._event_store.query_events(
                event_type="mcp.job.created",
                limit=100,
                offset=offset,
            )
            if not created_events:
                break
            for event in created_events:
                links = event.data.get("links") or {}
                if links.get("lineage_id") != lineage_id:
                    continue
                if job_type is not None and event.data.get("job_type") != job_type:
                    continue
                candidate_job_ids.add(event.aggregate_id)
            if len(created_events) < 100:
                break
            offset += 100

        for job_id in candidate_job_ids:
            try:
                snapshot = await self.get_snapshot(job_id)
            except ValueError:
                continue
            if snapshot.is_terminal and not include_terminal:
                continue
            if snapshot.links.lineage_id != lineage_id:
                continue
            if job_type is not None and snapshot.job_type != job_type:
                continue
            self._known_job_ids.add(job_id)
            candidates.append(snapshot)
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.updated_at)

    async def find_active_job_by_session(
        self,
        session_id: str,
        *,
        job_type: str | None = None,
        include_terminal: bool = False,
    ) -> JobSnapshot | None:
        """Return the most recently updated job for ``session_id``.

        Mirrors :meth:`find_active_job_by_lineage` for tools whose durable
        checkpoint is keyed by session id. Returning an active match lets
        accept-boundary handlers reject duplicate starts before two workers
        race on the same session-scoped state file.
        """
        await self._ensure_initialized()
        candidates: list[JobSnapshot] = []
        candidate_job_ids = set(self._known_job_ids)
        offset = 0
        while True:
            created_events = await self._event_store.query_events(
                event_type="mcp.job.created",
                limit=100,
                offset=offset,
            )
            if not created_events:
                break
            for event in created_events:
                links = event.data.get("links") or {}
                if links.get("session_id") != session_id:
                    continue
                if job_type is not None and event.data.get("job_type") != job_type:
                    continue
                candidate_job_ids.add(event.aggregate_id)
            if len(created_events) < 100:
                break
            offset += 100

        for job_id in candidate_job_ids:
            try:
                snapshot = await self.get_snapshot(job_id)
            except ValueError:
                continue
            if snapshot.is_terminal and not include_terminal:
                continue
            if snapshot.links.session_id != session_id:
                continue
            if job_type is not None and snapshot.job_type != job_type:
                continue
            self._known_job_ids.add(job_id)
            candidates.append(snapshot)
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.updated_at)

    def _persist_durable_cancel(self, job_id: str, *, reason: str) -> None:
        """Persist the job-scoped AgentProcess cancel marker before volatile cancel.

        The running AgentProcess normally writes this marker when it observes a
        cooperative cancel, but ``cancel_job`` is the user-facing accept point.
        Persisting here closes the crash window between ``CANCEL_REQUESTED`` and
        runner observation so a restarted job using ``mcp_job:{job_id}`` will
        still see the cancellation.
        """
        store = self._checkpoint_store or CheckpointStore()
        store.initialize()
        AgentProcessHandle.persist_cancel_signal(
            f"mcp_job:{job_id}",
            store=store,
            reason=reason,
            source_process_id=job_id,
        )

    async def cleanup_expired_jobs(self, ttl: timedelta | None = None) -> int:
        """Remove terminal jobs older than *ttl* from the in-memory registry.

        Returns the number of cleaned-up job IDs.
        """
        ttl = ttl or _JOB_TTL
        now = datetime.now(UTC)
        expired: list[str] = []
        for job_id in list(self._known_job_ids):
            try:
                snapshot = await self.get_snapshot(job_id)
            except ValueError:
                expired.append(job_id)
                continue
            updated = snapshot.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if snapshot.is_terminal and updated < now - ttl:
                expired.append(job_id)
        for job_id in expired:
            self._known_job_ids.discard(job_id)
            self._tasks.pop(job_id, None)
            self._runner_tasks.pop(job_id, None)
            self._monitors.pop(job_id, None)
        return len(expired)

    async def _append_event(
        self,
        event_type: str,
        job_id: str,
        data: dict[str, Any],
        *,
        event_id: str | None = None,
    ) -> None:
        """Persist one job event."""
        await self._ensure_initialized()
        await self._event_store.append(
            BaseEvent(
                id=event_id or str(uuid4()),
                type=event_type,
                aggregate_type="job",
                aggregate_id=job_id,
                data={**data, "timestamp": datetime.now(UTC).isoformat()},
            )
        )
