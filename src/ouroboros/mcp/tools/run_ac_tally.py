"""Count how many of a run's acceptance criteria were judged done.

A run's ``workflow_outcome`` is all-or-nothing: one rejected AC (or a final
revalidation failure) makes the whole run ``failed``, so the fleet cannot
tell a run that delivered 3 of 4 criteria from one that delivered none.
This module derives two bounded integers — ``ac_passed`` / ``ac_total`` —
from the executor's durable ``execution.ac.attempt_judged`` events, per
root-level AC, so the outcome carries how much of the work was actually
accepted. Counts only; never AC text, paths, commands, or output.
"""

from __future__ import annotations

from typing import Any

import anyio
import structlog

from ouroboros.mcp.job_snapshot import JobSnapshot, fold_job_events
from ouroboros.orchestrator.evidence.common import validate_attempt_judgment_payload
from ouroboros.orchestrator.session import SessionStatus
from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

AC_PASSED_KEY = "ac_passed"
AC_TOTAL_KEY = "ac_total"
_JUDGED_EVENT_TYPE = "execution.ac.attempt_judged"
_JUDGED_EVENT_PAGE_SIZE = 5000
_CANCEL_TALLY_TIMEOUT_SECONDS = 1.0
_CANCEL_JOB_PAGE_SIZE = 500


def tally_judged_acs(events: Any, *, session_id: str, execution_id: str) -> dict[str, int]:
    """Fold judged attempts into ``{"ac_passed": n, "ac_total": m}``.

    A root AC counts as passed when any of its validated judgments succeeded;
    every judged root AC counts
    toward the total. Returns ``{}`` when the session judged nothing, so a
    run rejected before launch never claims ``0/0``.
    """
    passed_by_root: dict[int, bool] = {}
    for event in events:
        data = getattr(event, "data", None)
        if not isinstance(data, dict):
            continue
        try:
            judgment = validate_attempt_judgment_payload(
                data,
                event_type=getattr(event, "type", None),
                aggregate_id=getattr(event, "aggregate_id", None),
                expected_execution_id=execution_id,
                expected_session_id=session_id,
            )
        except ValueError:
            continue
        accepted = judgment.success
        passed_by_root[judgment.root_ac_index] = (
            passed_by_root.get(judgment.root_ac_index, False) or accepted
        )
    if not passed_by_root:
        return {}
    return {
        AC_PASSED_KEY: sum(1 for accepted in passed_by_root.values() if accepted),
        AC_TOTAL_KEY: len(passed_by_root),
    }


async def derive_run_ac_tally(
    event_store: EventStore,
    *,
    session_id: str,
    execution_id: str,
) -> dict[str, int]:
    """Read the run's judged attempts and tally them. Best-effort: never raises."""
    try:
        events = []
        offset = 0
        while True:
            page = await event_store.query_events(
                aggregate_id=execution_id,
                event_type=_JUDGED_EVENT_TYPE,
                limit=_JUDGED_EVENT_PAGE_SIZE,
                offset=offset,
            )
            events.extend(page)
            if len(page) < _JUDGED_EVENT_PAGE_SIZE:
                break
            offset += len(page)
    except Exception:
        log.warning(
            "mcp.tool.execute_seed.ac_tally_unavailable",
            session_id=session_id,
            execution_id=execution_id,
        )
        return {}
    return tally_judged_acs(events, session_id=session_id, execution_id=execution_id)


async def derive_linked_run_ac_tally(
    event_store: EventStore, snapshot: JobSnapshot
) -> dict[str, int]:
    if snapshot.job_type != "execute_seed":
        return {}
    if not snapshot.links.session_id or not snapshot.links.execution_id:
        return {}
    return await derive_run_ac_tally(
        event_store,
        session_id=snapshot.links.session_id,
        execution_id=snapshot.links.execution_id,
    )


async def derive_terminal_run_meta(
    event_store: EventStore,
    *,
    session_id: str,
    execution_id: str,
    session_status: SessionStatus | None,
) -> dict[str, Any]:
    from ouroboros.mcp.tools.run_failure_meta import derive_run_failure_meta

    meta = {}
    if session_status in {SessionStatus.FAILED, SessionStatus.CANCELLED}:
        meta.update(
            await derive_run_failure_meta(
                event_store,
                session_id=session_id,
                execution_id=execution_id,
                session_status=session_status,
            )
        )
    if session_status in {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED}:
        meta.update(
            await derive_run_ac_tally(
                event_store,
                session_id=session_id,
                execution_id=execution_id,
            )
        )
    return meta


async def derive_cancelled_job_ac_tally(event_store: EventStore, job_id: str) -> dict[str, int]:
    try:
        with anyio.fail_after(_CANCEL_TALLY_TIMEOUT_SECONDS):
            fold, cursor = None, 0
            while True:
                events, cursor = await event_store.get_events_after(
                    "job", job_id, last_row_id=cursor, limit=_CANCEL_JOB_PAGE_SIZE
                )
                if not events:
                    break
                fold = fold_job_events(fold, events, cursor=cursor)
                if len(events) < _CANCEL_JOB_PAGE_SIZE:
                    break
            if fold is None:
                return {}
            return await derive_linked_run_ac_tally(event_store, fold.to_snapshot(job_id))
    except Exception:
        log.warning("mcp.job.cancelled_ac_tally_unavailable", job_id=job_id)
        return {}


__all__ = [
    "AC_PASSED_KEY",
    "AC_TOTAL_KEY",
    "derive_run_ac_tally",
    "tally_judged_acs",
    "derive_linked_run_ac_tally",
    "derive_terminal_run_meta",
    "derive_cancelled_job_ac_tally",
]
