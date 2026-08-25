"""Tests for the direct (non-job) ouroboros_evaluate telemetry boundary.

``EvaluateHandler.handle`` (the synchronous ``ouroboros_evaluate`` tool) never
creates a job, so job-backed ``JobTelemetryBoundary`` terminal events never
fire for it. ``record_direct_evaluation_outcome`` (telemetry_boundary.py) is
the only path by which a direct evaluate call reaches the ``workflow_outcome``
funnel that the verified-active-user rule and per-backend success rates read
from.
"""

from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.errors import ConfigError
from ouroboros.core.types import Result
from ouroboros.evaluation.models import (
    CheckResult,
    CheckType,
    EvaluationResult,
    MechanicalResult,
    SemanticResult,
)
from ouroboros.mcp.job_manager import JobManager
from ouroboros.mcp.telemetry_boundary import record_direct_evaluation_outcome
from ouroboros.mcp.tools.evaluation_handlers import EvaluateHandler, StartEvaluateHandler
from ouroboros.persistence.event_store import EventStore

_CAPTURE_TARGET = "ouroboros.mcp.telemetry_boundary.usage_telemetry.capture"
# The composition tests exercise BOTH producers of workflow_outcome: the
# direct boundary (telemetry_boundary.usage_telemetry.capture, an alias for
# this same function) and JobTelemetryBoundary.observe -> capture_job_outcome,
# which calls the module-local `capture` inside ouroboros/telemetry.py
# directly (not through the usage_telemetry alias). Patching the function at
# its true definition site catches both call shapes uniformly.
_LOW_LEVEL_CAPTURE_TARGET = "ouroboros.telemetry.capture"


async def _wait_terminal(manager: JobManager, job_id: str):
    for _ in range(200):
        snapshot = await manager.get_snapshot(job_id)
        if snapshot.is_terminal:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def _semantic_result(*, ac_compliance: bool, score: float) -> SemanticResult:
    kwargs = {
        "score": score,
        "ac_compliance": ac_compliance,
        "goal_alignment": 0.9 if ac_compliance else 0.4,
        "drift_score": 0.1 if ac_compliance else 0.5,
        "uncertainty": 0.1,
        "reasoning": "boundary test",
    }
    field_names = {f.name for f in dataclasses.fields(SemanticResult)}
    if "evidence" in field_names:
        kwargs["evidence"] = ()
    if "questions_used" in field_names:
        kwargs["questions_used"] = ()
    return SemanticResult(**kwargs)


def _eval_result(execution_id: str, *, final_approved: bool) -> EvaluationResult:
    return EvaluationResult(
        execution_id=execution_id,
        stage1_result=MechanicalResult(
            passed=True,
            checks=(CheckResult(check_type=CheckType.LINT, passed=True, message="ok"),),
        ),
        stage2_result=_semantic_result(
            ac_compliance=final_approved, score=0.9 if final_approved else 0.3
        ),
        final_approved=final_approved,
    )


def _install_pipeline_mock(result: Result) -> AsyncMock:
    mock_pipeline = AsyncMock()
    mock_pipeline.evaluate = AsyncMock(return_value=result)
    return mock_pipeline


class TestDirectEvaluateSingleACTelemetry:
    """Single-AC path: exactly one workflow_outcome per terminal handle() call."""

    async def test_success_full_approval_emits_verified(self) -> None:
        mock_pipeline = _install_pipeline_mock(Result.ok(_eval_result("s1", final_approved=True)))

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
            patch(_CAPTURE_TARGET) as capture,
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s1",
                    "artifact": "def f(): pass",
                    "acceptance_criterion": "Only AC",
                }
            )

        assert result.is_ok
        capture.assert_called_once()
        event, props = capture.call_args.args
        assert event == "workflow_outcome"
        assert props["command"] == "evaluate"
        assert props["terminal_status"] == "completed"
        assert props["verified"] is True

    async def test_success_not_approved_emits_unverified(self) -> None:
        mock_pipeline = _install_pipeline_mock(Result.ok(_eval_result("s2", final_approved=False)))

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
            patch(_CAPTURE_TARGET) as capture,
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s2",
                    "artifact": "def f(): pass",
                    "acceptance_criterion": "Only AC",
                }
            )

        assert result.is_ok
        capture.assert_called_once()
        _, props = capture.call_args.args
        assert props["verified"] is False

    async def test_pipeline_failure_emits_failed_terminal_status(self) -> None:
        mock_pipeline = _install_pipeline_mock(Result.err(ValueError("semantic stage exploded")))

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
            patch(_CAPTURE_TARGET) as capture,
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s3",
                    "artifact": "def f(): pass",
                    "acceptance_criterion": "Only AC",
                }
            )

        assert result.is_err
        capture.assert_called_once()
        _, props = capture.call_args.args
        assert props["terminal_status"] == "failed"
        assert props["verified"] is False

    async def test_config_error_before_pipeline_runs_emits_failed(self) -> None:
        """RuntimeError raised while wiring the adapter still counts as an attempt."""
        with (
            patch(
                "ouroboros.mcp.tools.evaluation_handlers.create_llm_adapter",
                side_effect=ConfigError("unsupported backend"),
            ),
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
            patch(_CAPTURE_TARGET) as capture,
        ):
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s4",
                    "artifact": "def f(): pass",
                    "acceptance_criterion": "Only AC",
                }
            )

        assert result.is_err
        capture.assert_called_once()
        _, props = capture.call_args.args
        assert props["terminal_status"] == "failed"
        assert props["failure_reason_code"] == "config"

    async def test_generic_factory_runtime_error_is_not_config(self) -> None:
        with (
            patch(
                "ouroboros.mcp.tools.evaluation_handlers.create_llm_adapter",
                side_effect=RuntimeError("provider construction failed"),
            ),
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
            patch(_CAPTURE_TARGET) as capture,
        ):
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s4c",
                    "artifact": "def f(): pass",
                    "acceptance_criterion": "Only AC",
                }
            )

        assert result.is_err
        _, props = capture.call_args.args
        assert props["failure_reason_code"] == "unknown"

    async def test_pipeline_runtime_error_falls_back_to_unknown(self) -> None:
        mock_pipeline = _install_pipeline_mock(  # type: ignore[arg-type]
            Result.ok(_eval_result("s4b", final_approved=True))
        )
        mock_pipeline.evaluate = AsyncMock(side_effect=RuntimeError("pipeline bug"))

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
            patch(_CAPTURE_TARGET) as capture,
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s4b",
                    "artifact": "def f(): pass",
                    "acceptance_criterion": "Only AC",
                }
            )

        assert result.is_err
        _, props = capture.call_args.args
        assert props["failure_reason_code"] == "unknown"


class TestDirectEvaluateMultiACTelemetry:
    """Multi-AC checklist path shares the same direct-call boundary."""

    async def test_all_passed_emits_verified(self) -> None:
        mock_pipeline = AsyncMock()
        mock_pipeline.evaluate = AsyncMock(
            side_effect=[
                Result.ok(_eval_result("s5", final_approved=True)),
                Result.ok(_eval_result("s5", final_approved=True)),
            ]
        )

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
            patch(_CAPTURE_TARGET) as capture,
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s5",
                    "artifact": "x",
                    "acceptance_criteria": ["AC one", "AC two"],
                }
            )

        assert result.is_ok
        assert result.value.meta["multi_ac"] is True
        capture.assert_called_once()
        _, props = capture.call_args.args
        assert props["terminal_status"] == "completed"
        assert props["verified"] is True

    async def test_mixed_outcomes_emit_unverified(self) -> None:
        mock_pipeline = AsyncMock()
        mock_pipeline.evaluate = AsyncMock(
            side_effect=[
                Result.ok(_eval_result("s6", final_approved=True)),
                Result.ok(_eval_result("s6", final_approved=False)),
            ]
        )

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
            patch(_CAPTURE_TARGET) as capture,
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s6",
                    "artifact": "x",
                    "acceptance_criteria": ["AC one", "AC two"],
                }
            )

        assert result.is_ok
        assert result.value.meta["final_approved"] is False
        capture.assert_called_once()
        _, props = capture.call_args.args
        assert props["verified"] is False

    async def test_pipeline_error_mid_checklist_emits_failed(self) -> None:
        mock_pipeline = AsyncMock()
        mock_pipeline.evaluate = AsyncMock(
            side_effect=[
                Result.ok(_eval_result("s7", final_approved=True)),
                Result.err(ValueError("semantic stage exploded")),
            ]
        )

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
            patch(_CAPTURE_TARGET) as capture,
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s7",
                    "artifact": "x",
                    "acceptance_criteria": ["AC one", "AC two"],
                }
            )

        assert result.is_err
        capture.assert_called_once()
        _, props = capture.call_args.args
        assert props["terminal_status"] == "failed"


class TestDirectEvaluateArgumentValidationDoesNotEmit:
    """Argument-validation rejections happen before any evaluation is attempted."""

    async def test_missing_session_id_does_not_emit(self) -> None:
        with patch(_CAPTURE_TARGET) as capture:
            handler = EvaluateHandler()
            result = await handler.handle({"artifact": "x"})

        assert result.is_err
        capture.assert_not_called()

    async def test_missing_artifact_does_not_emit(self) -> None:
        with patch(_CAPTURE_TARGET) as capture:
            handler = EvaluateHandler()
            result = await handler.handle({"session_id": "s"})

        assert result.is_err
        capture.assert_not_called()


class TestRecordDirectEvaluationOutcome:
    """Direct unit coverage of the boundary function itself."""

    def test_verified_only_when_not_failed_and_approved(self) -> None:
        with patch(_CAPTURE_TARGET) as capture:
            record_direct_evaluation_outcome(final_approved=True, failed=False)

        _, props = capture.call_args.args
        assert props["verified"] is True
        assert props["terminal_status"] == "completed"

    def test_not_verified_when_approved_but_failed(self) -> None:
        """A failed attempt is never verified, even if final_approved slipped in True."""
        with patch(_CAPTURE_TARGET) as capture:
            record_direct_evaluation_outcome(final_approved=True, failed=True)

        _, props = capture.call_args.args
        assert props["verified"] is False
        assert props["terminal_status"] == "failed"

    def test_not_verified_when_not_approved(self) -> None:
        with patch(_CAPTURE_TARGET) as capture:
            record_direct_evaluation_outcome(final_approved=False, failed=False)

        _, props = capture.call_args.args
        assert props["verified"] is False

    def test_none_approval_is_preserved_not_coerced(self) -> None:
        with patch(_CAPTURE_TARGET) as capture:
            record_direct_evaluation_outcome(final_approved=None, failed=True)

        _, props = capture.call_args.args
        assert props["verified"] is False

    def test_never_raises_when_capture_explodes(self) -> None:
        with patch(_CAPTURE_TARGET, side_effect=RuntimeError("posthog down")):
            record_direct_evaluation_outcome(final_approved=True, failed=False)


class TestJobBackedEvaluationSuppressesDirectEmission:
    """One StartEvaluate job must produce exactly one workflow_outcome.

    Regression for the double-emission bug: EvaluateHandler.handle() used to
    call record_direct_evaluation_outcome() unconditionally, so a job-backed
    call through StartEvaluateHandler -> run_evaluation_job produced BOTH the
    undeduplicated direct event AND the job-derived one (with $insert_id) once
    JobManager's own terminal event fired. These tests exercise the real
    EvaluateHandler -> StartEvaluateHandler -> JobManager chain (only the
    EvaluationPipeline is mocked) so the suppression is proven end-to-end,
    not just by inspecting the emit_terminal_telemetry flag in isolation.
    """

    async def test_start_evaluate_job_emits_exactly_one_job_derived_outcome(self) -> None:
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        try:
            mock_pipeline = _install_pipeline_mock(
                Result.ok(_eval_result("job-s1", final_approved=True))
            )
            job_manager = JobManager(store)

            with (
                patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
                patch(
                    "ouroboros.persistence.event_store.EventStore",
                    return_value=AsyncMock(initialize=AsyncMock()),
                ),
            ):
                MockPipeline.return_value = mock_pipeline
                evaluate_handler = EvaluateHandler(event_store=store)
                start_handler = StartEvaluateHandler(
                    evaluate_handler=evaluate_handler,
                    event_store=store,
                    job_manager=job_manager,
                )

                with patch(_LOW_LEVEL_CAPTURE_TARGET) as capture:
                    started = await start_handler.handle(
                        {
                            "session_id": "job-s1",
                            "artifact": "def f(): pass",
                            "acceptance_criterion": "Only AC",
                        }
                    )
                    assert started.is_ok
                    job_id = started.value.meta["job_id"]
                    assert job_id is not None
                    snapshot = await _wait_terminal(job_manager, job_id)

                    assert snapshot.status.value == "completed"
                    capture.assert_called_once()
                    event, props = capture.call_args.args
                    assert event == "workflow_outcome"
                    assert props["command"] == "evaluate"
                    assert props["terminal_status"] == "completed"
                    assert props["verified"] is True
                    # The job-derived producer is the ONLY one that stamps $insert_id.
                    assert "$insert_id" in props
        finally:
            await store.close()

    async def test_direct_evaluate_call_has_no_insert_id(self) -> None:
        """The direct-path producer never dedupes — no $insert_id key at all."""
        mock_pipeline = _install_pipeline_mock(
            Result.ok(_eval_result("direct-s1", final_approved=True))
        )

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
            patch(_LOW_LEVEL_CAPTURE_TARGET) as capture,
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "direct-s1",
                    "artifact": "def f(): pass",
                    "acceptance_criterion": "Only AC",
                }
            )

        assert result.is_ok
        capture.assert_called_once()
        _, props = capture.call_args.args
        assert "$insert_id" not in props


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
