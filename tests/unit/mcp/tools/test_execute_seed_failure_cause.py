"""execute_seed attaches a closed failure cause to failed/cancelled run meta."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.errors import JobWorkError, MCPToolError
from ouroboros.mcp.failure_taxonomy import LAUNCH_FAILURE_CAUSES
from ouroboros.mcp.tools.background import job_work_error
from ouroboros.mcp.tools.run_failure_meta import derive_run_failure_meta, launch_error
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

    meta = await derive_run_failure_meta(
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
    meta = await derive_run_failure_meta(
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

    meta = await derive_run_failure_meta(
        store,
        session_id=SESSION,
        execution_id=EXECUTION,
        session_status=SessionStatus.FAILED,
    )

    assert meta == {"failure_cause": "unknown", "failure_reason_code": "unknown"}


# --- pre-launch rejections: launch_error -> job runner -> failed terminal meta ---


@pytest.mark.parametrize("cause", sorted(LAUNCH_FAILURE_CAUSES))
def test_launch_error_stamps_closed_cause_without_changing_the_message(cause: str) -> None:
    plain = MCPToolError("Task workspace error: dirty", tool_name="ouroboros_execute_seed")
    error = launch_error(cause, "Task workspace error: dirty")

    assert str(error) == str(plain)
    assert error.error_code is None
    assert error.details == plain.details
    assert error.failure_meta == {"failure_cause": cause}


def test_launch_error_rejects_causes_outside_the_closed_set() -> None:
    with pytest.raises(ValueError):
        launch_error("verify_exit_nonzero", "not a launch branch")
    with pytest.raises(ValueError):
        launch_error("dirty checkout at /Users/private", "prose is not a cause")


def test_launch_error_keeps_existing_details_and_retriability() -> None:
    error = launch_error(
        "launch_prepare_failed",
        "Execution failed: boom",
        is_retriable=True,
        details={"session_id": "orch_1"},
    )

    assert error.is_retriable is True
    assert error.details == {"session_id": "orch_1"}
    assert error.failure_meta == {"failure_cause": "launch_prepare_failed"}


@pytest.mark.parametrize(
    ("cause", "reason"),
    [
        ("launch_workspace_unavailable", "config"),
        ("launch_config_error", "config"),
        ("launch_prepare_failed", "tool"),
        ("launch_seed_invalid", "validation"),
        ("launch_resume_blocked", "validation"),
        ("launch_rejected", "validation"),
    ],
)
def test_job_work_error_lifts_launch_cause_and_reason_code(cause: str, reason: str) -> None:
    error = job_work_error(launch_error(cause, "rejected before launch"))

    assert isinstance(error, JobWorkError)
    assert isinstance(error, RuntimeError)
    assert str(error) == "rejected before launch"
    assert error.result_meta == {"failure_cause": cause, "failure_reason_code": reason}


def test_job_work_error_lifts_evaluate_reason_code_without_a_cause() -> None:
    error = job_work_error(
        MCPToolError(
            "Evaluation setup failed: no provider",
            tool_name="ouroboros_evaluate",
            failure_meta={"failure_reason_code": "config"},
        )
    )

    assert error.result_meta == {"failure_reason_code": "config"}


def test_job_work_error_drops_unaudited_values_and_prose() -> None:
    error = job_work_error(
        MCPToolError(
            "boom",
            tool_name="ouroboros_execute_seed",
            details={"path": "/Users/private/project"},
            failure_meta={
                "failure_cause": "dirty checkout at /Users/private/project",
                "failure_reason_code": "segfault",
                "note": "free text",
            },
        )
    )

    assert error.result_meta == {}
    assert str(error) == "boom details={'path': '/Users/private/project'}"


def test_job_work_error_wraps_plain_errors_with_empty_meta() -> None:
    error = job_work_error(RuntimeError("plain"))

    assert str(error) == "plain"
    assert error.result_meta == {}
