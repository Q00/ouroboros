"""Job snapshot types and the resumable event fold behind them.

``JobManager.get_snapshot`` materializes a job by left-folding its event
stream. Polling loops (the per-job monitor, ``wait_for_change``) used to
replay that stream from row 0 on every tick, so their cost grew linearly
with a job's event count. ``JobEventFold`` captures the fold state together
with the event-store rowid cursor it has consumed, letting those loops fold
only the events appended since the previous tick. The stream is append-only
(TTL cleanup prunes in-memory registries, never persisted rows), which is
what makes the resumable fold sound.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from ouroboros.events.base import BaseEvent


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
    preserve_runner_result: bool = False


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
    result_payload: dict[str, Any] | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }


@dataclass(slots=True)
class JobEventFold:
    """Mutable fold of one job aggregate, resumable at ``cursor``."""

    created_data: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    cursor: int
    status: JobStatus
    message: str
    links: JobLinks
    result_text: str | None = None
    result_meta: dict[str, Any] = field(default_factory=dict)
    result_payload: dict[str, Any] | None = None
    error: str | None = None

    def to_snapshot(self, job_id: str) -> JobSnapshot:
        return JobSnapshot(
            job_id=job_id,
            job_type=self.created_data.get("job_type", "unknown"),
            status=self.status,
            message=self.message,
            created_at=self.created_at,
            updated_at=self.updated_at,
            cursor=self.cursor,
            links=self.links,
            result_text=self.result_text,
            result_meta=self.result_meta,
            result_payload=self.result_payload,
            error=self.error,
        )


def fold_job_events(
    prior: JobEventFold | None,
    events: Sequence[BaseEvent],
    *,
    cursor: int,
) -> JobEventFold:
    """Fold ``events`` into ``prior`` (mutated in place) or start a new fold.

    With ``prior=None`` the first event must be the job's creation event and
    ``events`` must be non-empty; this path reproduces a full replay. With a
    prior fold, ``events`` are the rows appended after ``prior.cursor``.
    """
    if prior is None:
        created = events[0]
        created_links = created.data.get("links", {})
        prior = JobEventFold(
            created_data=created.data,
            created_at=created.timestamp,
            updated_at=created.timestamp,
            cursor=cursor,
            status=JobStatus(created.data.get("status", JobStatus.QUEUED.value)),
            message=created.data.get("message", ""),
            links=JobLinks(
                session_id=created_links.get("session_id"),
                execution_id=created_links.get("execution_id"),
                lineage_id=created_links.get("lineage_id"),
                preserve_runner_result=created_links.get("preserve_runner_result") is True,
            ),
        )
        events = events[1:]

    for event in events:
        data = event.data
        link_data = data.get("links") or {}
        prior.links = JobLinks(
            session_id=link_data.get("session_id") or prior.links.session_id,
            execution_id=link_data.get("execution_id") or prior.links.execution_id,
            lineage_id=link_data.get("lineage_id") or prior.links.lineage_id,
            preserve_runner_result=(
                link_data.get("preserve_runner_result")
                if isinstance(link_data.get("preserve_runner_result"), bool)
                else prior.links.preserve_runner_result
            ),
        )
        if "status" in data:
            prior.status = JobStatus(data["status"])
        if "message" in data:
            prior.message = data["message"]
        if "result_text" in data:
            prior.result_text = data["result_text"]
        if "result_meta" in data and isinstance(data["result_meta"], dict):
            prior.result_meta = data["result_meta"]
        if "result_payload" in data and isinstance(data["result_payload"], dict):
            prior.result_payload = data["result_payload"]
        if "error" in data:
            prior.error = data["error"]
        prior.updated_at = event.timestamp

    prior.cursor = cursor
    return prior
