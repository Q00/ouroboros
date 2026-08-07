"""Response guards for background-job long polling."""

import asyncio
import os
from typing import Any

import structlog

log = structlog.get_logger(__name__)

JOB_WAIT_GUARD_VERSION = "detached-branch-timeout-v2"


def _consume_detached_wait_task(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        log.warning("mcp.tool.job_wait.detached_wait_failed", exc_info=True)


async def await_job_wait_branch(
    awaitable: Any,
    *,
    timeout: float,
    job_id: str,
    branch: str,
) -> Any:
    """Await a positive-time wait without blocking on slow cancellation cleanup."""
    log.debug(
        "mcp.tool.job_wait.guard.enter",
        job_id=job_id,
        branch=branch,
        timeout_seconds=timeout,
        guard_version=JOB_WAIT_GUARD_VERSION,
        pid=os.getpid(),
    )
    task = asyncio.create_task(awaitable)
    log.debug(
        "mcp.tool.job_wait.guard.task_created",
        job_id=job_id,
        branch=branch,
        guard_version=JOB_WAIT_GUARD_VERSION,
        pid=os.getpid(),
    )
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_detached_wait_task)
        raise
    log.debug(
        "mcp.tool.job_wait.guard.wait_returned",
        job_id=job_id,
        branch=branch,
        done=task in done,
        guard_version=JOB_WAIT_GUARD_VERSION,
        pid=os.getpid(),
    )
    if task in done:
        return task.result()

    task.cancel()
    task.add_done_callback(_consume_detached_wait_task)
    log.debug(
        "mcp.tool.job_wait.wait.branch_timeout",
        job_id=job_id,
        branch=branch,
        timeout_seconds=timeout,
        guard_version=JOB_WAIT_GUARD_VERSION,
        pid=os.getpid(),
    )
    raise TimeoutError


async def await_job_wait_request(
    awaitable: Any,
    *,
    timeout_seconds: int,
    response_timeout: float,
    job_id: str,
    branch: str,
) -> Any:
    """Return zero-time snapshots without applying the long-poll guard.

    Zero disables waiting for a future change; it does not bound the
    authoritative snapshot read. Positive-time waits remain guarded so a
    cancellation-resistant branch cannot hold the request open indefinitely.
    """
    if timeout_seconds == 0:
        return await awaitable
    return await await_job_wait_branch(
        awaitable,
        timeout=response_timeout,
        job_id=job_id,
        branch=branch,
    )
