"""Process-local observations for job reads, never job acceptance authority."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ouroboros.mcp.job_snapshot import JobSnapshot
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

if TYPE_CHECKING:
    from ouroboros.mcp.job_manager import JobManager


@dataclass(frozen=True, slots=True)
class PendingJobReceipt:
    """Evidence of a pending receipt, not evidence that its worker is still alive."""

    job_id: str
    worker_pid: int
    timeout_seconds: float


class PendingJobReceipts:
    """Retain this server's pending receipts until durable state becomes visible.

    Observations do not survive a new manager/server process. They must not create
    job events: those would prevent the detached worker from claiming its job ID.
    """

    def __init__(self) -> None:
        self._receipts: dict[str, PendingJobReceipt] = {}

    def remember(self, *, job_id: str, worker_pid: int, timeout_seconds: float) -> None:
        self._receipts[job_id] = PendingJobReceipt(job_id, worker_pid, timeout_seconds)

    def get(self, job_id: str) -> PendingJobReceipt | None:
        return self._receipts.get(job_id)

    def discard(self, job_id: str) -> None:
        self._receipts.pop(job_id, None)


def cached_job_snapshot(
    snapshots: Mapping[str, JobSnapshot],
    tasks: Mapping[str, asyncio.Task[Any]],
    job_id: str,
) -> JobSnapshot | None:
    """Nonterminal cache is usable only while this manager owns a live task."""
    snapshot = snapshots.get(job_id)
    if snapshot is None or snapshot.is_terminal:
        return snapshot
    task = tasks.get(job_id)
    return snapshot if task is not None and not task.done() else None


def pending_job_result(manager: JobManager, job_id: str) -> MCPToolResult | None:
    """Render a known pending handle only after a normal snapshot read failed."""
    observations = getattr(manager, "pending_receipts", None)
    if not isinstance(observations, PendingJobReceipts):
        return None
    receipt = observations.get(job_id)
    if receipt is None:
        return None
    return MCPToolResult(
        content=(
            MCPContentItem(
                type=ContentType.TEXT,
                text=(
                    f"Job {job_id}: acceptance_pending. This server issued a pending "
                    "start receipt, but durable job acceptance is not yet confirmed. "
                    "Current worker state is unknown; no result is available. "
                    "Do not start a duplicate. Poll ouroboros_job_status with the same job_id."
                ),
            ),
        ),
        is_error=False,
        meta={
            "job_id": job_id,
            "status": "acceptance_pending",
            "lifecycle_status": "acceptance_pending",
            "status_category": "non_terminal",
            "is_terminal": False,
            "result_available": False,
            "acceptance_confirmed": False,
            "worker_state": "unknown",
            "startup_worker_pid": receipt.worker_pid,
            "startup_timeout_seconds": receipt.timeout_seconds,
            "retry_start_allowed": False,
            "cursor": 0,
            "changed": False,
            "status_check": {"tool": "ouroboros_job_status", "arguments": {"job_id": job_id}},
        },
    )
