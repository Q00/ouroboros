"""Unit tests for the CLI ``ooo run`` workflow_outcome telemetry parity."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import typer

from ouroboros.cli.commands.run import _record_cli_run_outcome, _run_orchestrator
from ouroboros.core.types import Result
from ouroboros.orchestrator.session import SessionStatus

VALID_SEED_DATA = {
    "goal": "Test task",
    "constraints": ["Python 3.14+"],
    "acceptance_criteria": ["All tests pass", "No lint errors"],
    "ontology_schema": {
        "name": "TestOntology",
        "description": "Test ontology",
        "fields": [{"name": "test_field", "field_type": "string", "description": "A test field"}],
    },
    "evaluation_principles": [],
    "exit_conditions": [],
    "metadata": {
        "seed_id": "test-seed-cli-outcome",
        "version": "1.0.0",
        "created_at": "2024-01-01T00:00:00Z",
        "ambiguity_score": 0.1,
        "interview_id": None,
    },
}

_FAILURE_META = {"failure_cause": "verify_gate_rejected", "failure_reason_code": "validation"}


def _exec_result(*, success: bool, summary: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        success=success,
        session_id="sess-test",
        messages_processed=1,
        duration_seconds=1.0,
        execution_id="exec-test",
        summary=summary if summary is not None else {},
        final_message="final",
    )


def _session_repo(status: SessionStatus | None) -> MagicMock:
    repo = MagicMock()
    if status is None:
        repo.reconstruct_session = AsyncMock(return_value=Result.err("no session"))
    else:
        repo.reconstruct_session = AsyncMock(return_value=Result.ok(SimpleNamespace(status=status)))
    return repo


@pytest.mark.asyncio
async def test_failed_run_records_failed_outcome_with_failure_cause() -> None:
    """A failed CLI run is counted and carries the closed failure cause, like an MCP job."""
    with (
        patch("ouroboros.telemetry.capture_job_outcome") as capture,
        patch(
            "ouroboros.mcp.tools.run_failure_meta.derive_run_failure_meta",
            new_callable=AsyncMock,
            return_value=_FAILURE_META,
        ) as derive,
    ):
        await _record_cli_run_outcome(
            Result.ok(_exec_result(success=False)),
            event_store=MagicMock(),
            session_repo=_session_repo(SessionStatus.FAILED),
            execution_id="exec-local",
            session_id="sess-local",
        )

    derive.assert_awaited_once_with(
        ANY,
        session_id="sess-test",
        execution_id="exec-test",
        session_status=SessionStatus.FAILED,
    )
    capture.assert_called_once_with(
        ANY,
        "run",
        terminal_status="failed",
        result_meta={"success": False, **_FAILURE_META},
    )
    assert capture.call_args.args[0].startswith("exec-test:")


@pytest.mark.asyncio
async def test_orchestrator_error_records_failed_outcome() -> None:
    """An orchestrator-level error is a terminal failure, not an invisible run."""
    with (
        patch("ouroboros.telemetry.capture_job_outcome") as capture,
        patch(
            "ouroboros.mcp.tools.run_failure_meta.derive_run_failure_meta",
            new_callable=AsyncMock,
            return_value={"failure_cause": "runtime_error", "failure_reason_code": "unknown"},
        ) as derive,
    ):
        await _record_cli_run_outcome(
            Result.err("boom"),
            event_store=MagicMock(),
            session_repo=_session_repo(None),
            execution_id="exec-local",
            session_id="sess-local",
        )

    derive.assert_awaited_once_with(
        ANY, session_id="sess-local", execution_id="exec-local", session_status=None
    )
    capture.assert_called_once()
    assert capture.call_args.args[0].startswith("exec-local:")
    assert capture.call_args.kwargs["terminal_status"] == "failed"
    assert capture.call_args.kwargs["result_meta"]["failure_cause"] == "runtime_error"


@pytest.mark.asyncio
async def test_successful_run_records_completed_without_failure_meta() -> None:
    with (
        patch("ouroboros.telemetry.capture_job_outcome") as capture,
        patch(
            "ouroboros.mcp.tools.run_failure_meta.derive_run_failure_meta",
            new_callable=AsyncMock,
        ) as derive,
    ):
        await _record_cli_run_outcome(
            Result.ok(_exec_result(success=True)),
            event_store=MagicMock(),
            session_repo=_session_repo(SessionStatus.COMPLETED),
            execution_id="exec-local",
            session_id="sess-local",
        )

    derive.assert_not_awaited()
    capture.assert_called_once_with(
        ANY, "run", terminal_status="completed", result_meta={"success": True}
    )


@pytest.mark.asyncio
async def test_cancelled_session_records_cancelled_not_failed() -> None:
    """The runner reports cancellation as success=False; the outcome must say cancelled."""
    with (
        patch("ouroboros.telemetry.capture_job_outcome") as capture,
        patch(
            "ouroboros.mcp.tools.run_failure_meta.derive_run_failure_meta",
            new_callable=AsyncMock,
            return_value={"failure_cause": "cancelled", "failure_reason_code": "cancelled"},
        ),
    ):
        await _record_cli_run_outcome(
            Result.ok(_exec_result(success=False, summary={"cancelled": True})),
            event_store=MagicMock(),
            session_repo=_session_repo(SessionStatus.CANCELLED),
            execution_id="exec-local",
            session_id="sess-local",
        )

    assert capture.call_args.kwargs["terminal_status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_summary_wins_when_session_cannot_be_reconstructed() -> None:
    with (
        patch("ouroboros.telemetry.capture_job_outcome") as capture,
        patch(
            "ouroboros.mcp.tools.run_failure_meta.derive_run_failure_meta",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        await _record_cli_run_outcome(
            Result.ok(_exec_result(success=False, summary={"cancelled": True})),
            event_store=MagicMock(),
            session_repo=_session_repo(None),
            execution_id="exec-local",
            session_id="sess-local",
        )

    assert capture.call_args.kwargs["terminal_status"] == "cancelled"


@pytest.mark.asyncio
async def test_paused_session_is_not_a_terminal_outcome() -> None:
    with patch("ouroboros.telemetry.capture_job_outcome") as capture:
        await _record_cli_run_outcome(
            Result.ok(_exec_result(success=False)),
            event_store=MagicMock(),
            session_repo=_session_repo(SessionStatus.PAUSED),
            execution_id="exec-local",
            session_id="sess-local",
        )

    capture.assert_not_called()


@pytest.mark.asyncio
async def test_each_invocation_gets_its_own_outcome_id() -> None:
    """``--resume`` reuses the execution id; a failed run and its successful resume are two outcomes."""
    with (
        patch("ouroboros.telemetry.capture_job_outcome") as capture,
        patch(
            "ouroboros.mcp.tools.run_failure_meta.derive_run_failure_meta",
            new_callable=AsyncMock,
            return_value=_FAILURE_META,
        ),
    ):
        for success, status in ((False, SessionStatus.FAILED), (True, SessionStatus.COMPLETED)):
            await _record_cli_run_outcome(
                Result.ok(_exec_result(success=success)),
                event_store=MagicMock(),
                session_repo=_session_repo(status),
                execution_id="exec-local",
                session_id="sess-local",
            )

    first, second = (call.args[0] for call in capture.call_args_list)
    assert first != second


@pytest.mark.asyncio
async def test_failure_meta_errors_still_count_the_failed_run() -> None:
    """A broken cause lookup degrades to ``unknown``; it must not drop the outcome."""
    with (
        patch("ouroboros.telemetry.capture_job_outcome") as capture,
        patch(
            "ouroboros.mcp.tools.run_failure_meta.derive_run_failure_meta",
            new_callable=AsyncMock,
            side_effect=RuntimeError("store down"),
        ),
    ):
        await _record_cli_run_outcome(
            Result.ok(_exec_result(success=False)),
            event_store=MagicMock(),
            session_repo=_session_repo(SessionStatus.FAILED),
            execution_id="exec-local",
            session_id="sess-local",
        )

    capture.assert_called_once_with(
        ANY, "run", terminal_status="failed", result_meta={"success": False}
    )


@pytest.mark.parametrize(
    ("exec_result", "expected_status"),
    [
        (Result.ok(_exec_result(success=False)), "failed"),
        (Result.err("orchestrator exploded"), "failed"),
    ],
    ids=["runner-failed", "orchestrator-error"],
)
@pytest.mark.asyncio
async def test_run_orchestrator_records_and_flushes_non_success_outcomes(
    tmp_path: Path,
    exec_result: Result[SimpleNamespace, str],
    expected_status: str,
) -> None:
    """Both non-success exits used to leave the funnel before the event was posted."""
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text("goal: ignored\n", encoding="utf-8")
    mock_runner = MagicMock()
    mock_runner.execute_seed = AsyncMock(return_value=exec_result)
    mock_runner.resume_session = AsyncMock()

    with (
        patch("ouroboros.cli.commands.run._load_seed_from_yaml", return_value=VALID_SEED_DATA),
        patch("ouroboros.orchestrator.create_agent_runtime"),
        patch("ouroboros.orchestrator.OrchestratorRunner", return_value=mock_runner),
        patch("ouroboros.persistence.event_store.EventStore") as mock_event_store_cls,
        patch("ouroboros.telemetry.capture_job_outcome") as capture,
        patch("ouroboros.telemetry.flush") as flush,
        pytest.raises(typer.Exit),
    ):
        mock_event_store_cls.return_value.initialize = AsyncMock()
        await _run_orchestrator(seed_file)

    capture.assert_called_once()
    assert capture.call_args.args[1] == "run"
    assert capture.call_args.kwargs["terminal_status"] == expected_status
    assert capture.call_args.kwargs["result_meta"]["success"] is False
    flush.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("success", "status", "terminal"),
    [(True, SessionStatus.COMPLETED, "completed"), (False, SessionStatus.FAILED, "failed")],
)
async def test_terminal_run_carries_ac_tally(
    success: bool, status: SessionStatus, terminal: str
) -> None:
    """The CLI outcome carries the same ac_passed/ac_total pair as an MCP job."""
    with (
        patch("ouroboros.telemetry.capture_job_outcome") as capture,
        patch(
            "ouroboros.mcp.tools.run_failure_meta.derive_run_failure_meta",
            new_callable=AsyncMock,
            return_value=_FAILURE_META,
        ),
        patch(
            "ouroboros.mcp.tools.run_ac_tally.derive_run_ac_tally",
            new_callable=AsyncMock,
            return_value={"ac_passed": 3, "ac_total": 4},
        ) as tally,
    ):
        await _record_cli_run_outcome(
            Result.ok(_exec_result(success=success)),
            event_store=MagicMock(),
            session_repo=_session_repo(status),
            execution_id="exec-local",
            session_id="sess-local",
        )

    tally.assert_awaited_once_with(ANY, session_id="sess-test", execution_id="exec-test")
    forwarded = capture.call_args.kwargs["result_meta"]
    assert capture.call_args.kwargs["terminal_status"] == terminal
    assert (forwarded["ac_passed"], forwarded["ac_total"]) == (3, 4)
