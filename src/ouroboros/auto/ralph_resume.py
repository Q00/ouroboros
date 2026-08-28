"""Legacy persisted Ralph checkpoint reconciliation for AutoPipeline."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.listeners import RALPH_CANCEL_BLOCKER_REASON
from ouroboros.auto.state import AutoPhase, AutoPipelineState

_BLOCKED_STOP_REASONS = frozenset(
    {
        "iteration_timeout",
        "wall_clock_exhausted",
        "oscillation_detected",
        "grade_regressing",
        "max_generations reached",
    }
)
_PEEK_SECONDS = 1.0


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _accepts_max_total_seconds(callable_obj: object) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    return "max_total_seconds" in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


async def reconcile_ralph_checkpoint(
    pipeline: Any,
    state: AutoPipelineState,
    ledger: SeedDraftLedger,
) -> Any:
    """Reconcile an already-dispatched job before the expired-deadline gates."""
    if state.ralph_dispatch_mode == "plugin":
        state.run_subagent = {}
        state.transition(AutoPhase.COMPLETE, "reconciled plugin Ralph checkpoint")
        pipeline._save(state)
        return pipeline._result(state, ledger)
    if pipeline.ralph_resumer is None or state.ralph_job_id is None:
        state.mark_blocked(
            "persisted Ralph checkpoint cannot be polled by this runtime",
            tool_name="ralph_starter",
        )
        pipeline._save(state)
        return pipeline._result(state, ledger, blocker=state.last_error)
    try:
        kwargs: dict[str, Any] = {"job_id": state.ralph_job_id}
        if _accepts_max_total_seconds(pipeline.ralph_resumer):
            kwargs["max_total_seconds"] = _PEEK_SECONDS
        meta = await asyncio.wait_for(pipeline.ralph_resumer(**kwargs), timeout=_PEEK_SECONDS + 0.5)
    except TimeoutError:
        meta = {"terminal_status": "running_async"}
    except Exception as exc:
        state.mark_failed(f"ralph resume poll failed: {exc}", tool_name="ralph_starter")
        pipeline._save(state)
        return pipeline._result(state, ledger, blocker=state.last_error)
    if not isinstance(meta, dict):
        state.mark_failed(
            f"ralph resumer returned {type(meta).__name__}, expected dict",
            tool_name="ralph_starter",
        )
        pipeline._save(state)
        return pipeline._result(state, ledger, blocker=state.last_error)
    terminal_status = _optional_str(meta.get("terminal_status"))
    stop_reason = _optional_str(meta.get("stop_reason"))
    state.ralph_job_status = terminal_status
    state.ralph_stop_reason = stop_reason
    generation = meta.get("current_generation")
    if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0:
        state.ralph_current_generation = generation
    if terminal_status == "completed":
        await pipeline._emit_product_emitted_terminal(
            state, review=None, stop_reason=stop_reason, resumed=True
        )
        state.transition(AutoPhase.COMPLETE, "reconciled completed Ralph job")
        pipeline._save(state)
        return pipeline._result(state, ledger)
    if terminal_status == "running_async":
        if state.is_deadline_expired():
            pipeline._mark_pipeline_timeout(state)
            return pipeline._result(state, ledger, blocker=state.last_error)
        state.mark_progress("background Ralph job is still tracked", tool_name="ralph_starter")
        pipeline._save(state)
        return pipeline._result(state, ledger, status_override="detached")
    if terminal_status == "cancelled" or (
        terminal_status == "failed" and stop_reason in _BLOCKED_STOP_REASONS
    ):
        blocker = RALPH_CANCEL_BLOCKER_REASON if terminal_status == "cancelled" else stop_reason
        state.mark_blocked(blocker or "Ralph job blocked", tool_name="ralph_starter")
        pipeline._save(state)
        return pipeline._result(state, ledger, blocker=state.last_error)
    state.mark_failed(
        f"ralph loop failed: {stop_reason or terminal_status or 'unknown status'}",
        tool_name="ralph_starter",
    )
    pipeline._save(state)
    return pipeline._result(state, ledger, blocker=state.last_error)
