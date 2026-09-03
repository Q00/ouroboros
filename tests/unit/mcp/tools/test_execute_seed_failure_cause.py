"""execute_seed attaches a closed failure cause to failed/cancelled run meta."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.execution_handlers import _derive_run_failure_meta
from ouroboros.orchestrator.session import SessionStatus

SESSION = "orch_failure_cause"
EXECUTION = "exec_failure_cause"


def _store_with(events_by_type: dict[str, list[BaseEvent]]) -> AsyncMock:
    store = AsyncMock()

    async def query_events(
        aggregate_id: str | None = None, event_type: str | None = None, **_: Any
    ) -> list[BaseEvent]:
        return list(events_by_type.get(event_type or "", []))

    store.query_events = AsyncMock(side_effect=query_events)
    return store


@pytest.mark.asyncio
async def test_failed_run_meta_names_settlement_cause_and_reason_code() -> None:
    store = _store_with(
        {
            "execution.verify.failed": [
                BaseEvent(
                    type="execution.verify.failed",
                    aggregate_type="execution",
                    aggregate_id=EXECUTION,
                    data={
                        "session_id": SESSION,
                        "ac_index": 0,
                        "verify_cause": "workspace_mutated",
                        "final_workspace_revalidation": True,
                        "verify_command": "pytest -q /Users/private/project",
                    },
                )
            ]
        }
    )

    meta = await _derive_run_failure_meta(
        store,
        session_id=SESSION,
        execution_id=EXECUTION,
        session_status=SessionStatus.FAILED,
    )

    assert meta == {
        "failure_cause": "verify_workspace_mutated",
        "failure_reason_code": "validation",
    }
    queried_types = {call.kwargs["event_type"] for call in store.query_events.await_args_list}
    assert queried_types == {
        "execution.verify.failed",
        "execution.ac.recovery_exhausted",
        "execution.ac.attempt_judged",
        "orchestrator.session.failed",
    }


@pytest.mark.asyncio
async def test_cancelled_run_meta_is_cancelled() -> None:
    meta = await _derive_run_failure_meta(
        _store_with({}),
        session_id=SESSION,
        execution_id=EXECUTION,
        session_status=SessionStatus.CANCELLED,
    )

    assert meta == {"failure_cause": "cancelled", "failure_reason_code": "cancelled"}


@pytest.mark.asyncio
async def test_unreadable_store_degrades_to_unknown_without_raising() -> None:
    store = AsyncMock()
    store.query_events = AsyncMock(side_effect=PersistenceError("locked"))

    meta = await _derive_run_failure_meta(
        store,
        session_id=SESSION,
        execution_id=EXECUTION,
        session_status=SessionStatus.FAILED,
    )

    assert meta == {"failure_cause": "unknown", "failure_reason_code": "unknown"}
