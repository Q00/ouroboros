"""Pending receipt observations for job reads, never job acceptance authority."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
import math
from typing import TYPE_CHECKING, Any

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.job_snapshot import JobSnapshot
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

if TYPE_CHECKING:
    from ouroboros.mcp.job_manager import JobManager
    from ouroboros.persistence.event_store import EventStore

_RECEIPT_AGGREGATE = "job_receipt"
_PENDING_EVENT = "mcp.job.acceptance_pending_observed"
_PERSIST_TIMEOUT_SECONDS = 2.0


class JobNotFoundError(ValueError):
    """No durable job rows exist; other snapshot failures are not missing jobs."""


@dataclass(frozen=True, slots=True)
class PendingJobReceipt:
    """Evidence of a pending receipt, not evidence that its worker is still alive."""

    job_id: str
    worker_pid: int
    timeout_seconds: float
    persisted: bool = False


class PendingJobReceipts:
    """Remember timeout observations in the accepting job's existing EventStore.

    The separate aggregate is observational: writing a real job event would
    prevent the detached worker from claiming its ID. Memory remains a fallback
    when the bounded durable write fails; reads never certify worker liveness.
    """

    def __init__(self, event_store: EventStore) -> None:
        self._event_store = event_store
        self._receipts: dict[str, PendingJobReceipt] = {}

    def remember(
        self, *, job_id: str, worker_pid: int, timeout_seconds: float
    ) -> PendingJobReceipt:
        receipt = PendingJobReceipt(job_id, worker_pid, timeout_seconds)
        self._receipts[job_id] = receipt
        return receipt

    def get(self, job_id: str) -> PendingJobReceipt | None:
        return self._receipts.get(job_id)

    def discard(self, job_id: str) -> None:
        self._receipts.pop(job_id, None)

    async def persist(self, receipt: PendingJobReceipt) -> None:
        await self._event_store.append_durable(
            BaseEvent(
                type=_PENDING_EVENT,
                aggregate_type=_RECEIPT_AGGREGATE,
                aggregate_id=receipt.job_id,
                data={
                    "worker_pid": receipt.worker_pid,
                    "startup_timeout_seconds": receipt.timeout_seconds,
                },
            ),
            timeout=_PERSIST_TIMEOUT_SECONDS,
        )
        if self._receipts.get(receipt.job_id) is receipt:
            self._receipts[receipt.job_id] = replace(receipt, persisted=True)

    async def read(self, job_id: str) -> PendingJobReceipt | None:
        cached = self.get(job_id)
        if cached is not None and cached.persisted:
            return cached
        events, _ = await self._event_store.get_events_after(_RECEIPT_AGGREGATE, job_id)
        receipt = None
        for event in events:
            if event.type != _PENDING_EVENT:
                continue
            pid = event.data.get("worker_pid")
            timeout = event.data.get("startup_timeout_seconds")
            if (
                event.event_version != 1
                or type(pid) is not int
                or pid <= 0
                or isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise PersistenceError(
                    "Invalid or unsupported pending job receipt observation.",
                    operation="read_pending_job_receipt",
                    details={"job_id": job_id, "event_id": event.id},
                )
            receipt = PendingJobReceipt(job_id, pid, float(timeout), persisted=True)
        if receipt is not None and self._receipts.get(job_id) is cached:
            self._receipts[job_id] = receipt
        return receipt if receipt is not None else cached


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


async def pending_job_result(manager: JobManager, job_id: str) -> MCPToolResult | None:
    """Render a known pending handle only after a normal snapshot read failed."""
    observations = getattr(manager, "pending_receipts", None)
    if not isinstance(observations, PendingJobReceipts):
        return None
    receipt = await observations.read(job_id)
    if receipt is None:
        return None
    return MCPToolResult(
        content=(
            MCPContentItem(
                type=ContentType.TEXT,
                text=(
                    f"Job {job_id}: acceptance_pending. A pending start receipt was "
                    "recorded, but durable job acceptance is not yet confirmed. "
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
            "pending_receipt_persisted": receipt.persisted,
            "worker_state": "unknown",
            "startup_worker_pid": receipt.worker_pid,
            "startup_timeout_seconds": receipt.timeout_seconds,
            "retry_start_allowed": False,
            "cursor": 0,
            "changed": False,
            "status_check": {"tool": "ouroboros_job_status", "arguments": {"job_id": job_id}},
        },
    )
