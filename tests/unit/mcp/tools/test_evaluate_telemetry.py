"""Tests for the direct (non-job) ouroboros_evaluate telemetry boundary.

``EvaluateHandler.handle`` (the synchronous ``ouroboros_evaluate`` tool) never
creates a job, so job-backed ``JobTelemetryBoundary`` terminal events never
fire for it. ``record_direct_evaluation_outcome`` (telemetry_boundary.py) is
the only path by which a direct evaluate call reaches the ``workflow_outcome``
funnel that the verified-active-user rule and per-backend success rates read
from.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.evaluation.models import (
    CheckResult,
    CheckType,
    EvaluationResult,
    MechanicalResult,
    SemanticResult,
)
from ouroboros.mcp.telemetry_boundary import record_direct_evaluation_outcome
from ouroboros.mcp.tools.evaluation_handlers import EvaluateHandler

_CAPTURE_TARGET = "ouroboros.mcp.telemetry_boundary.usage_telemetry.capture"


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
        assert props["phase"] == "terminal"
        assert props["terminal_status"] == "completed"
        assert props["ok"] is True
        assert props["verified"] is True
        assert props["final_approved"] is True

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
        assert props["ok"] is True
        assert props["verified"] is False
        assert props["final_approved"] is False

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
        assert props["ok"] is False
        assert props["verified"] is False
        assert props["final_approved"] is None

    async def test_config_error_before_pipeline_runs_emits_failed(self) -> None:
        """RuntimeError raised while wiring the adapter still counts as an attempt."""
        with (
            patch(
                "ouroboros.mcp.tools.evaluation_handlers.create_llm_adapter",
                side_effect=RuntimeError("unsupported backend"),
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
        assert props["ok"] is False
        assert props["final_approved"] is None


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
        assert props["final_approved"] is True

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
        assert props["final_approved"] is False

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
        assert props["final_approved"] is None


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
        assert props["ok"] is True
        assert props["terminal_status"] == "completed"

    def test_not_verified_when_approved_but_failed(self) -> None:
        """A failed attempt is never verified, even if final_approved slipped in True."""
        with patch(_CAPTURE_TARGET) as capture:
            record_direct_evaluation_outcome(final_approved=True, failed=True)

        _, props = capture.call_args.args
        assert props["verified"] is False
        assert props["ok"] is False
        assert props["terminal_status"] == "failed"

    def test_not_verified_when_not_approved(self) -> None:
        with patch(_CAPTURE_TARGET) as capture:
            record_direct_evaluation_outcome(final_approved=False, failed=False)

        _, props = capture.call_args.args
        assert props["verified"] is False
        assert props["final_approved"] is False

    def test_none_approval_is_preserved_not_coerced(self) -> None:
        with patch(_CAPTURE_TARGET) as capture:
            record_direct_evaluation_outcome(final_approved=None, failed=True)

        _, props = capture.call_args.args
        assert props["final_approved"] is None

    def test_never_raises_when_capture_explodes(self) -> None:
        with patch(_CAPTURE_TARGET, side_effect=RuntimeError("posthog down")):
            record_direct_evaluation_outcome(final_approved=True, failed=False)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
