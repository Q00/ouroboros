"""Async job management for long-running MCP operations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.runner import request_cancellation
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.event_store import EventStore


class JobStatus(StrEnum):
    """Lifecycle states for async MCP jobs."""

    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
        return self.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


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


class JobManager:
    """Owns background MCP jobs and persists their state as events."""

    def __init__(self, event_store: EventStore | None = None) -> None:
        self._event_store = event_store or EventStore()
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._initialized = False
        self._known_job_ids: set[str] = set()

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    async def start_job(
        self,
        *,
        job_type: str,
        initial_message: str,
        runner: asyncio.Future[Any] | Any,
        links: JobLinks | None = None,
    ) -> JobSnapshot:
        """Create and start a new background job."""
        await self._ensure_initialized()

        job_id = f"job_{uuid4().hex[:12]}"
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

        self._known_job_ids.add(job_id)
        task = asyncio.create_task(self._run_job(job_id, job_type, runner))
        self._tasks[job_id] = task
        self._monitors[job_id] = asyncio.create_task(self._monitor_job(job_id))

        return await self.get_snapshot(job_id)

    async def _run_job(self, job_id: str, job_type: str, runner: Any) -> None:
        """Run the actual background job and persist terminal state."""
        await self.update_status(job_id, JobStatus.RUNNING, f"Running {job_type}")

        try:
            result = await runner
        except asyncio.CancelledError:
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
            terminal_type = "mcp.job.completed"
            terminal_status = JobStatus.COMPLETED
            if snapshot.status == JobStatus.CANCEL_REQUESTED:
                terminal_type = "mcp.job.cancelled"
                terminal_status = JobStatus.CANCELLED
            elif getattr(result, "is_interrupted", False):
                terminal_type = "mcp.job.failed"
                terminal_status = JobStatus.FAILED
            await self._append_event(
                terminal_type,
                job_id,
                {
                    "status": terminal_status.value,
                    "message": "Job complete"
                    if terminal_status == JobStatus.COMPLETED
                    else "Job cancelled"
                    if terminal_status == JobStatus.CANCELLED
                    else "Job interrupted",
                    "result_text": getattr(result, "text_content", str(result))[:20_000],
                    "result_meta": _safe_meta(getattr(result, "meta", {})),
                    "is_error": bool(getattr(result, "is_error", False)),
                },
            )
        finally:
            self._tasks.pop(job_id, None)
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

        return JobSnapshot(
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

    async def cancel_job(self, job_id: str) -> JobSnapshot:
        """Request cancellation for a running job."""
        snapshot = await self.get_snapshot(job_id)
        if snapshot.is_terminal:
            return snapshot

        await self.update_status(job_id, JobStatus.CANCEL_REQUESTED, "Cancellation requested")

        if snapshot.links.session_id:
            await request_cancellation(snapshot.links.session_id)
        else:
            task = self._tasks.get(job_id)
            if task is not None:
                task.cancel()

        return await self.get_snapshot(job_id)

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
            self._monitors.pop(job_id, None)
        return len(expired)

    async def _append_event(self, event_type: str, job_id: str, data: dict[str, Any]) -> None:
        """Persist one job event."""
        await self._ensure_initialized()
        await self._event_store.append(
            BaseEvent(
                type=event_type,
                aggregate_type="job",
                aggregate_id=job_id,
                data={**data, "timestamp": datetime.now(UTC).isoformat()},
            )
        )
