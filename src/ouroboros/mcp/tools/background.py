"""Shared background-job pipeline for the ``Start*`` MCP tool handlers.

The fire-and-forget ``ouroboros_start_*`` tools all wrap their real work in
the same four-step pipeline:

1. ``allocate_job_id()`` — reserve a durable job id up front.
2. A ``should_cancel()`` pre-work guard so a job cancelled while still queued
   returns a terminal *cancelled* result instead of starting work.
3. ``run_with_agent_process(process_id="{scope}:{job_id}",
   cancel_key="mcp_job:{job_id}")`` — bind the runner to the AgentProcess
   acceptance boundary AND to the durable cancel marker
   :meth:`JobManager.cancel_job` writes under ``mcp_job:{job_id}``.
4. ``start_job(..., job_id=job_id)`` — enqueue and return the snapshot.

Extracting it here removes five copies and, more importantly, fixes a
divergence: ``StartEvaluateHandler`` and ``StartAutoHandler`` historically
wrapped their runner with ``lambda _handle: _runner()`` and passed **no**
``process_id`` / ``cancel_key``.  Because ``run_with_agent_process`` only
constructs a :class:`CheckpointStore` (the component that loads a persisted
cancel marker) when ``cancel_key or process_id`` is set, those two surfaces
wrote a durable ``mcp_job:{job_id}`` cancel marker that their agent process
could never observe — the restart-visible cancellation contract documented
on :meth:`JobManager._persist_durable_cancel` was silently broken for them.
Routing every ``Start*`` handler through this helper gives evaluate/auto the
same guard and cancel_key as evolve/execute/ralph, restoring that contract.

The helper deliberately does **not** build the receipt envelope: each
handler's receipt text/meta differs (lineage_id vs session_id vs auto's
dispatch_mode tuple), and some handlers compose work *around* enqueue
(auto's start-lease lifecycle, execute_seed's post-receipt idempotency
write).  The helper returns the :class:`JobSnapshot` so callers keep full
control of the receipt and of any compose-around bookkeeping.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import inspect
import logging
from typing import TYPE_CHECKING

from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.agent_process import run_with_agent_process

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ouroboros.mcp.job_manager import JobLinks, JobManager, JobSnapshot
    from ouroboros.orchestrator.agent_process import AgentProcessHandle
    from ouroboros.persistence.event_store import EventStore

# Type of the inner work callable: receives the AgentProcess handle (so the
# work can re-check cancellation mid-flight if it wants) and returns the
# tool result.
WorkFn = Callable[["AgentProcessHandle"], Awaitable[MCPToolResult]]


def make_cancelled_result(text: str) -> MCPToolResult:
    """Build the terminal *cancelled-before-work* tool result.

    Centralises the ``is_error=True`` / ``meta={"status": "cancelled"}``
    shape the pre-work guard returns so every ``Start*`` handler emits an
    identical cancellation envelope.  ``run_with_agent_process`` reads
    ``meta["status"] == "cancelled"`` to terminalise the AgentProcess as
    *cancelled* rather than *failed*.
    """
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text=text),),
        is_error=True,
        meta={"status": "cancelled"},
    )


async def start_background_tool_job(
    *,
    job_manager: JobManager,
    event_store: EventStore,
    job_type: str,
    intent: str,
    process_scope: str,
    initial_message: str,
    links: JobLinks,
    work_fn: WorkFn,
    cancelled_text: str,
    on_started: Callable[[JobSnapshot], Awaitable[None]] | None = None,
    on_enqueue_failure: Callable[[BaseException], Awaitable[None]] | None = None,
) -> JobSnapshot:
    """Run the shared allocate -> guard -> agent-process -> start_job pipeline.

    Args:
        job_manager: The per-server :class:`JobManager`.
        event_store: The shared :class:`EventStore` (passed to the runner).
        job_type: ``JobManager`` job_type tag (e.g. ``"evolve_step"``).
        intent: AgentProcess intent label (e.g. ``"evolve_step"``).
        process_scope: Prefix for the deterministic AgentProcess
            ``process_id`` (e.g. ``"evolve_step:{lineage_id}"``); the helper
            appends ``":{job_id}"``.
        initial_message: Human-readable queued message for the job snapshot.
        links: :class:`JobLinks` for the job (session/execution/lineage).
        work_fn: The inner work, invoked with the AgentProcess handle.  The
            helper wraps it with the standard ``should_cancel()`` pre-work
            guard so callers must not re-implement that check.
        cancelled_text: Text for the cancelled-before-work result.
        on_started: Optional async hook invoked with the snapshot *after* a
            successful ``start_job`` and *before* this function returns — used
            by auto to update its start lease while still owning the snapshot.
        on_enqueue_failure: Optional async hook invoked if ``start_job``
            raises, *before* the exception is re-raised — used by auto to
            release its start lease.  The helper always closes the pending
            runner coroutine on failure regardless of this hook.

    Returns:
        The :class:`JobSnapshot` from a successful enqueue.

    Raises:
        Re-raises any exception from ``start_job`` after running
        ``on_enqueue_failure`` and closing the runner coroutine.
    """
    job_id = await job_manager.allocate_job_id()

    async def _guarded_runner(handle: AgentProcessHandle) -> MCPToolResult:
        # Uniform pre-work cancel guard: a job cancelled while still queued
        # must return a terminal cancelled result without starting work.
        if handle.should_cancel():
            return make_cancelled_result(cancelled_text)
        return await work_fn(handle)

    runner = run_with_agent_process(
        event_store=event_store,
        intent=intent,
        work_fn=_guarded_runner,
        process_id=f"{process_scope}:{job_id}",
        cancel_key=f"mcp_job:{job_id}",
    )

    try:
        snapshot = await job_manager.start_job(
            job_type=job_type,
            initial_message=initial_message,
            runner=runner,
            links=links,
            job_id=job_id,
        )
    except BaseException as exc:
        # Mirror the pre-extraction auto handler's failure path: close the
        # un-started runner coroutine so it is not left un-awaited, then let
        # the caller release any compose-around bookkeeping before we re-raise.
        if inspect.iscoroutine(runner):
            runner.close()
        if on_enqueue_failure is not None:
            try:
                await on_enqueue_failure(exc)
            except Exception:
                # Cleanup is best-effort: a failing hook must never mask the
                # original enqueue exception being re-raised below.
                logger.warning(
                    "mcp.background_job.enqueue_failure_hook_failed",
                    extra={"job_type": job_type},
                    exc_info=True,
                )
        raise

    if on_started is not None:
        await on_started(snapshot)

    return snapshot
