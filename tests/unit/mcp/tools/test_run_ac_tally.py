"""``ac_passed``/``ac_total`` are derived from judged attempts, per root AC."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.run_ac_tally import derive_run_ac_tally, tally_judged_acs

SESSION = "orch_tally"
EXECUTION = "exec_tally"


def _judged(root: int, outcome: str, *, session: str = SESSION, **extra: Any) -> BaseEvent:
    retry_attempt = extra.pop("retry_attempt", 0)
    attempt_number = extra.pop("attempt_number", retry_attempt + 1)
    return BaseEvent(
        type="execution.ac.attempt_judged",
        aggregate_type="execution",
        aggregate_id=EXECUTION,
        data={
            "execution_id": EXECUTION,
            "session_id": session,
            "root_ac_index": root,
            "ac_index": root,
            "retry_attempt": retry_attempt,
            "attempt_number": attempt_number,
            "is_decomposed": False,
            "outcome": outcome,
            "success": outcome in {"succeeded", "satisfied_externally"},
            "ac_text": "/Users/private/project must never leak",
            **extra,
        },
    )


def test_counts_each_root_ac_once_and_any_accepted_attempt_as_passed() -> None:
    events = [
        _judged(0, "failed", retry_attempt=0),
        _judged(0, "succeeded", retry_attempt=1),
        _judged(1, "failed"),
        _judged(2, "blocked"),
        _judged(3, "satisfied_externally"),
    ]

    assert tally_judged_acs(events, session_id=SESSION, execution_id=EXECUTION) == {
        "ac_passed": 2,
        "ac_total": 4,
    }


def test_decomposed_children_fold_into_their_root() -> None:
    events = [
        _judged(0, "succeeded", is_decomposed=True, is_decomposed_child=True),
        _judged(0, "failed", is_decomposed=True, is_decomposed_child=True),
    ]

    assert tally_judged_acs(events, session_id=SESSION, execution_id=EXECUTION) == {
        "ac_passed": 1,
        "ac_total": 1,
    }


def test_other_sessions_and_malformed_rows_are_ignored() -> None:
    events = [
        _judged(0, "succeeded", session="orch_other"),
        BaseEvent(
            type="execution.ac.attempt_judged",
            aggregate_type="execution",
            aggregate_id=EXECUTION,
            data={"session_id": SESSION, "root_ac_index": True, "outcome": "succeeded"},
        ),
        BaseEvent(
            type="execution.ac.attempt_judged",
            aggregate_type="execution",
            aggregate_id=EXECUTION,
            data={"session_id": SESSION, "root_ac_index": "1", "outcome": "succeeded"},
        ),
        _judged(1, "failed"),
    ]

    assert tally_judged_acs(events, session_id=SESSION, execution_id=EXECUTION) == {
        "ac_passed": 0,
        "ac_total": 1,
    }


def test_nothing_judged_yields_no_tally() -> None:
    assert tally_judged_acs([], session_id=SESSION, execution_id=EXECUTION) == {}


def test_malformed_or_spoofed_judgments_are_ignored() -> None:
    malformed = _judged(
        -7,
        "succeeded",
        execution_id="exec-foreign",
        ac_index=999,
        is_decomposed="yes",
        success=False,
    )

    assert tally_judged_acs(
        [malformed, _judged(1, "failed")],
        session_id=SESSION,
        execution_id=EXECUTION,
    ) == {"ac_passed": 0, "ac_total": 1}


@pytest.mark.asyncio
async def test_derive_reads_only_judged_attempts_of_the_execution() -> None:
    store = AsyncMock()
    store.query_events = AsyncMock(return_value=[_judged(0, "succeeded"), _judged(1, "failed")])

    tally = await derive_run_ac_tally(store, session_id=SESSION, execution_id=EXECUTION)

    assert tally == {"ac_passed": 1, "ac_total": 2}
    store.query_events.assert_awaited_once_with(
        aggregate_id=EXECUTION,
        event_type="execution.ac.attempt_judged",
        limit=5000,
        offset=0,
    )


@pytest.mark.asyncio
async def test_derive_does_not_truncate_judgments_above_5000() -> None:
    events = [_judged(root, "succeeded") for root in range(5001)]
    store = AsyncMock()

    async def query_events(**kwargs: Any) -> list[BaseEvent]:
        limit = kwargs.get("limit")
        offset = kwargs.get("offset", 0)
        return events[offset:] if limit is None else events[offset : offset + limit]

    store.query_events = AsyncMock(side_effect=query_events)

    assert await derive_run_ac_tally(store, session_id=SESSION, execution_id=EXECUTION) == {
        "ac_passed": 5001,
        "ac_total": 5001,
    }


@pytest.mark.asyncio
async def test_unreadable_store_yields_no_tally_without_raising() -> None:
    store = AsyncMock()
    store.query_events = AsyncMock(side_effect=PersistenceError("locked"))

    assert await derive_run_ac_tally(store, session_id=SESSION, execution_id=EXECUTION) == {}
