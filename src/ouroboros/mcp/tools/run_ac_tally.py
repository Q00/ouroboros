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

import structlog

from ouroboros.orchestrator.evidence.common import validate_attempt_judgment_payload
from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

AC_PASSED_KEY = "ac_passed"
AC_TOTAL_KEY = "ac_total"
_JUDGED_EVENT_TYPE = "execution.ac.attempt_judged"
_ACCEPTED_OUTCOMES = frozenset({"succeeded", "satisfied_externally"})
_JUDGED_EVENT_PAGE_SIZE = 5000


def tally_judged_acs(events: Any, *, session_id: str, execution_id: str) -> dict[str, int]:
    """Fold judged attempts into ``{"ac_passed": n, "ac_total": m}``.

    A root AC counts as passed when any of its attempts was accepted
    (``succeeded`` or ``satisfied_externally``); every judged root AC counts
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
        accepted = judgment.outcome in _ACCEPTED_OUTCOMES
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


__all__ = ["AC_PASSED_KEY", "AC_TOTAL_KEY", "derive_run_ac_tally", "tally_judged_acs"]
