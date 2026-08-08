"""Atomic acceptance boundary for starting an in-process MCP job."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any


def _create_task(awaitable: Any) -> asyncio.Task[Any]:
    """Narrow task-factory seam for deterministic partial-registration tests."""
    return asyncio.create_task(awaitable)


async def _created_receipt_state(manager: Any, job_id: str) -> bool | None:
    """Resolve a settled created write; ``None`` means unobservable ambiguity."""
    for _attempt in range(3):
        try:
            return await manager._job_exists(job_id)
        except BaseException:
            pass
    return None


async def _terminalize_registration_failure(manager: Any, job_id: str) -> bool:
    """Make a zero-runner created receipt truthful before allowing retry."""
    from ouroboros.mcp import job_manager as jobs

    try:
        await manager._append_event(
            "mcp.job.interrupted",
            job_id,
            jobs._stranded_job_interrupted_data(),
            event_id=f"{jobs._STRANDED_INTERRUPTED_EVENT_ID_PREFIX}{job_id}",
        )
        return True
    except BaseException:
        try:
            return bool((await manager.get_snapshot(job_id)).is_terminal)
        except BaseException:
            return False


async def start_managed_job(
    manager: Any,
    *,
    job_type: str,
    initial_message: str,
    runner: Any,
    links: Any,
    job_id: str | None,
) -> Any:
    """Persist acceptance, register ownership, and surface a job snapshot."""
    from ouroboros.mcp import job_manager as jobs

    await manager._ensure_initialized()
    if job_id is None:
        job_id = await manager.allocate_job_id()
    elif job_id not in manager._reserved_job_ids:
        if job_id in manager._known_job_ids or await manager._job_exists(job_id):
            raise ValueError(f"Job already exists: {job_id}")
        manager._known_job_ids.add(job_id)
    manager._reserved_job_ids.discard(job_id)
    job_links = links or jobs.JobLinks()

    creation_error: BaseException | None = None
    try:
        await manager._append_event(
            "mcp.job.created",
            job_id,
            {
                "job_type": job_type,
                "status": jobs.JobStatus.QUEUED.value,
                "message": initial_message,
                "links": {
                    "session_id": job_links.session_id,
                    "execution_id": job_links.execution_id,
                    "lineage_id": job_links.lineage_id,
                    "preserve_runner_result": job_links.preserve_runner_result,
                },
                **jobs.current_persisted_process_owner(),
            },
        )
    except BaseException as exc:
        created_state = await _created_receipt_state(manager, job_id)
        if created_state is False:
            raise
        if created_state is None:
            # Publication cannot be proved, so running would create an
            # unobservable side-effect owner. Keep the one-shot handoff
            # fail-closed, but do not start work until durable state is known.
            manager._started_job_ids.add(job_id)
            if inspect.iscoroutine(runner):
                runner.close()
            elif isinstance(runner, asyncio.Future):
                runner.cancel()
            raise
        creation_error = exc

    # The created receipt is provisionally owned until registration either
    # succeeds or is durably terminalized as a definitive zero-runner failure.
    manager._started_job_ids.add(job_id)
    try:
        if isinstance(runner, asyncio.Task):
            runner_task = runner
        elif inspect.iscoroutine(runner):
            runner_task = _create_task(runner)
        else:

            async def _await_runner(_awaitable: Any = runner) -> Any:
                return await _awaitable

            runner_task = _create_task(_await_runner())
    except BaseException as registration_error:
        if inspect.iscoroutine(runner):
            runner.close()
        elif isinstance(runner, asyncio.Future):
            runner.cancel()
        if await _terminalize_registration_failure(manager, job_id):
            manager._started_job_ids.discard(job_id)
        raise registration_error

    manager._runner_tasks[job_id] = runner_task
    job_coro = manager._run_job(job_id, job_type, runner_task)
    try:
        task = _create_task(job_coro)
    except BaseException as registration_error:
        job_coro.close()
        runner_task.cancel()
        try:
            await runner_task
        except BaseException:
            pass
        manager._runner_tasks.pop(job_id, None)
        if await _terminalize_registration_failure(manager, job_id):
            manager._started_job_ids.discard(job_id)
        raise registration_error

    manager._tasks[job_id] = task
    monitor_coro = manager._monitor_job(job_id)
    try:
        manager._monitors[job_id] = _create_task(monitor_coro)
    except BaseException:
        # Monitoring carries no terminal authority once the job task is live.
        monitor_coro.close()

    if creation_error is not None:
        raise creation_error
    return await manager.get_snapshot(job_id)
