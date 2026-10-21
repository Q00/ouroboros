"""Carry a finished terminal ``ouroboros run`` into formal evaluation and Ralph.

The MCP ``ooo run`` job enqueues a formal evaluation once the run is terminal,
and a rejected evaluation enqueues a bounded Ralph loop. The terminal command
gets the same successor chain here by reusing the MCP pieces instead of
re-implementing them: the run is projected into the ``MCPToolResult`` the MCP
job would have produced, ``enqueue_chained_evaluation`` starts the evaluation
through the composition root's successor handler (``build_run_successor_handler``),
and the CLI then follows the evaluation job, and the Ralph job it chains, to a
terminal receipt.

Nothing here changes the run's verdict or exit code. Failing to build the
handler or to enqueue is a warning, as on the MCP path. Ctrl-C while following
a job stops following it and never cancels the job: with the durable job store
the evaluation and Ralph jobs run in detached worker processes that outlive
this command.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from rich.markup import escape

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_info, print_warning

_WAIT_POLL_SECONDS = 5
_DECIDING_SESSION_STATUSES = frozenset({"completed", "failed", "cancelled", "paused"})


async def continue_run_into_evaluation(
    execution: Any,
    *,
    session_repo: Any,
    seed_content: str,
    worktree_path: str | None,
    working_dir: Path,
    runtime_override: str | None,
    auto_evaluate: bool | None,
    auto_evolve: bool | None,
) -> None:
    """Enqueue formal evaluation for a finished run and follow it, then Ralph.

    ``working_dir`` is the directory the run executed in, which QA judged too
    (inside a task worktree, the worktree's counterpart of the project
    directory); evaluation and Ralph work there. ``worktree_path`` is the task
    worktree root, shown in the run record. ``execution`` is the runner's
    ``OrchestratorResult``. ``auto_evaluate`` and
    ``auto_evolve`` are the per-invocation overrides (``None`` defers to
    ``execution.auto_evaluate`` / ``execution.auto_evolve``). Never raises
    ``Exception``; the run's verdict and exit code are the caller's.
    """
    from ouroboros.config.loader import get_auto_evaluate_enabled, get_auto_evolve_enabled
    from ouroboros.mcp.tools import run_evaluate_chain

    session_id = getattr(execution, "session_id", None) or None
    retry = f"ooo evaluate {session_id}" if session_id else "ooo evaluate <session_id>"
    _, evaluate_enabled, evolve_enabled = run_evaluate_chain.snapshot_run_successor_policy(
        {"auto_evaluate": auto_evaluate, "auto_evolve": auto_evolve},
        configured_auto_evaluate=get_auto_evaluate_enabled(),
        configured_auto_evolve=get_auto_evolve_enabled(),
    )
    if not evaluate_enabled:
        print_info(f"Formal evaluation is off for this run (auto_evaluate). Evaluate with: {retry}")
        return

    try:
        status = await _run_status(execution, session_repo)
        run_result = _run_result(execution, status=status, worktree_path=worktree_path)
        if not session_id or not run_evaluate_chain.is_evaluable_run_result(run_result):
            print_info(f"Formal evaluation skipped: a {status} run is not evaluable.")
            return
        handler = _build_successor_handler(runtime_override)
        queued = await run_evaluate_chain.enqueue_chained_evaluation(
            run_result,
            session_id=session_id,
            seed_content=seed_content,
            working_dir=working_dir,
            auto_evolve=evolve_enabled,
            start_evaluate_handler=handler,
        )
    except Exception as exc:  # noqa: BLE001 - evaluation must never flip the run verdict.
        print_warning(
            f"Formal evaluation could not start: {escape(str(exc))}. The run verdict is unchanged. "
            f"Next: {retry}"
        )
        return

    appended = (queued.content[-1].text or "").strip() if queued.content else ""
    job_id = queued.meta.get("chained_evaluate_job_id")
    if queued.meta.get("evaluation_status") != "enqueued" or not isinstance(job_id, str):
        print_warning(escape(appended))
        return
    console.print(appended, markup=False, highlight=False)
    await _follow_chain(
        job_id,
        job_manager=getattr(handler, "_job_manager", None),
        event_store=getattr(handler, "_event_store", None),
        retry=retry,
        ralph_may_follow=evolve_enabled,
    )


async def _run_status(execution: Any, session_repo: Any) -> str:
    """Classify the run the way the MCP run job and CLI run telemetry do.

    The reconstructed session decides (a paused or cancelled run is not
    evaluable); the runner's success flag is the fallback.
    """
    success = bool(getattr(execution, "success", False))
    status = "completed" if success else "failed"
    summary = getattr(execution, "summary", None)
    if not success and isinstance(summary, dict) and summary.get("cancelled") is True:
        status = "cancelled"
    session_id = getattr(execution, "session_id", None)
    if session_id and session_repo is not None:
        with contextlib.suppress(Exception):
            reconstructed = await session_repo.reconstruct_session(session_id)
            if reconstructed.is_ok:
                value = getattr(reconstructed.value.status, "value", None)
                if value in _DECIDING_SESSION_STATUSES:
                    status = value
    return status


def _run_result(execution: Any, *, status: str, worktree_path: str | None) -> Any:
    """Project the finished run into the result the MCP run job would carry."""
    from ouroboros.cli.commands.run import _get_verification_artifact
    from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

    session_id = getattr(execution, "session_id", None)
    execution_id = getattr(execution, "execution_id", None)
    summary = getattr(execution, "summary", None)
    body = _get_verification_artifact(
        summary if isinstance(summary, dict) else {},
        getattr(execution, "final_message", "") or "",
    )
    header = "Seed Execution COMPLETED" if status == "completed" else "Seed Execution FINISHED"
    lines = [header, f"Session ID: {session_id}", f"Execution ID: {execution_id}"]
    lines.append(f"Status: {status}")
    if worktree_path:
        lines.append(f"Task Worktree: {worktree_path}")
    text = "\n".join(lines) + ("\n\n" + body if body else "") + "\n"
    meta: dict[str, Any] = {
        "session_id": session_id,
        "execution_id": execution_id,
        "status": status,
        "success": status == "completed",
    }
    if worktree_path:
        meta["worktree_path"] = worktree_path
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text=text),),
        is_error=status != "completed",
        meta=meta,
    )


def _build_successor_handler(runtime_override: str | None) -> Any:
    """Resolve the evaluate stage runtime like ``ooo auto`` and build the stack.

    A ``plugin`` OpenCode mode makes ``StartEvaluateHandler`` return a host
    delegation envelope with no job id, which a terminal has no host to redeem,
    so it is demoted to ``subprocess``.
    """
    from ouroboros.auto.runtime_routing import (
        demote_plugin_opencode_mode,
        resolve_auto_stage_runtime_plan,
    )
    from ouroboros.mcp.tools.run_successors import build_run_successor_handler

    evaluate = resolve_auto_stage_runtime_plan(
        runtime_override=runtime_override,
        fallback_runtime_backend=runtime_override,
        fallback_opencode_mode=None,
    ).evaluate
    return build_run_successor_handler(
        agent_runtime_backend=evaluate.runtime_backend,
        opencode_mode=demote_plugin_opencode_mode(evaluate.opencode_mode),
    )


async def _follow_chain(
    evaluate_job_id: str,
    *,
    job_manager: Any,
    event_store: Any,
    retry: str,
    ralph_may_follow: bool,
) -> None:
    """Follow the evaluation job, then the Ralph job its receipt names."""
    followed = [evaluate_job_id]
    try:
        receipt_meta = await _follow_job(evaluate_job_id, "evaluate", job_manager, event_store)
        ralph_job_id = receipt_meta.get("chained_ralph_job_id")
        if isinstance(ralph_job_id, str) and ralph_job_id:
            followed.append(ralph_job_id)
            await _follow_job(ralph_job_id, "ralph", job_manager, event_store)
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Stop following only. The run already finished and its exit code is
        # the caller's; the jobs are not cancelled.
        _clear_interrupt_cancellation()
        _print_detached(followed, job_manager, retry, ralph_may_follow=ralph_may_follow)
    except Exception as exc:  # noqa: BLE001 - following is observability only.
        print_warning(f"Stopped following formal evaluation: {escape(str(exc))}")
        _print_detached(followed, job_manager, retry, ralph_may_follow=ralph_may_follow)


async def _follow_job(
    job_id: str, label: str, job_manager: Any, event_store: Any
) -> dict[str, Any]:
    """Poll one job to a terminal state, print its progress and receipt."""
    from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobWaitHandler

    print_info(
        f"Waiting for {label} job {job_id} (Ctrl-C stops waiting; the job is not cancelled)..."
    )
    wait_handler = JobWaitHandler(job_manager=job_manager, event_store=event_store)
    cursor = 0
    last_line = ""
    while True:
        waited = await wait_handler.handle(
            {
                "job_id": job_id,
                "cursor": cursor,
                "timeout_seconds": _WAIT_POLL_SECONDS,
                "view": "compact",
            }
        )
        if waited.is_err:
            print_warning(f"Could not follow {label} job {job_id}: {escape(str(waited.error))}")
            break
        meta = waited.value.meta or {}
        with contextlib.suppress(TypeError, ValueError):
            cursor = int(meta.get("cursor", cursor))
        line = (waited.value.text_content or "").strip()
        if meta.get("changed") and line and line != last_line:
            last_line = line
            console.print(rf"[dim]\[{label}][/] {escape(line)}")
        if meta.get("is_terminal"):
            break

    receipt = await JobResultHandler(job_manager=job_manager, event_store=event_store).handle(
        {"job_id": job_id}
    )
    if receipt.is_err:
        print_warning(
            f"Could not read the {label} job {job_id} result: {escape(str(receipt.error))}"
        )
        return {}
    if receipt.value.text_content:
        console.print(receipt.value.text_content, markup=False, highlight=False)
    return dict(receipt.value.meta or {})


def _clear_interrupt_cancellation() -> None:
    """Withdraw the SIGINT cancellation so the run's own exit path proceeds."""
    task = asyncio.current_task()
    if task is not None:
        while task.cancelling():
            task.uncancel()


def _print_detached(
    job_ids: list[str], job_manager: Any, retry: str, *, ralph_may_follow: bool
) -> None:
    """Say where the jobs this command stopped following went."""
    for job_id in job_ids:
        in_process = getattr(job_manager, "has_live_job_task", None)
        if callable(in_process) and in_process(job_id) is True:
            print_warning(
                f"Job {job_id} runs inside this process and stops when it exits. "
                f"Evaluate again with: {retry}"
            )
            continue
        print_info(
            f"Job {job_id} continues in the background. Follow it with: "
            f"ouroboros job wait {job_id} --timeout-seconds 60 --view compact; "
            f"result: ouroboros job result {job_id}"
        )
    if ralph_may_follow and len(job_ids) == 1:
        print_info(
            "If the evaluation is not approved it chains a Ralph job; "
            "the evaluation result names it as chained_ralph_job_id."
        )


__all__ = ["continue_run_into_evaluation"]
