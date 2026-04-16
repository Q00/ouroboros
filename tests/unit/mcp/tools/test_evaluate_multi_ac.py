"""Unit tests for EvaluateHandler multi-AC checklist path (#366).

These tests exercise the new `acceptance_criteria` parameter that turns
a single evaluate call into a per-AC checklist evaluation.  Single-AC
behaviour is covered by existing tests in test_definitions.py and is
deliberately left untouched here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.evaluation.models import (
    CheckResult,
    CheckType,
    EvaluationResult,
    MechanicalResult,
    SemanticResult,
)
from ouroboros.mcp.tools.evaluation_handlers import EvaluateHandler
from ouroboros.mcp.types import ToolInputType


def _semantic_result(*, ac_compliance: bool, score: float, reasoning: str) -> SemanticResult:
    """Build a SemanticResult tolerant to whether #367 fields exist yet."""
    import dataclasses

    kwargs = {
        "score": score,
        "ac_compliance": ac_compliance,
        "goal_alignment": 0.9 if ac_compliance else 0.4,
        "drift_score": 0.1 if ac_compliance else 0.5,
        "uncertainty": 0.1,
        "reasoning": reasoning,
    }
    field_names = {f.name for f in dataclasses.fields(SemanticResult)}
    if "evidence" in field_names:
        kwargs["evidence"] = ()
    if "questions_used" in field_names:
        kwargs["questions_used"] = ()
    return SemanticResult(**kwargs)


def _passing_eval(execution_id: str) -> EvaluationResult:
    return EvaluationResult(
        execution_id=execution_id,
        stage1_result=MechanicalResult(
            passed=True,
            checks=(CheckResult(check_type=CheckType.LINT, passed=True, message="ok"),),
        ),
        stage2_result=_semantic_result(
            ac_compliance=True,
            score=0.9,
            reasoning="AC met",
        ),
        final_approved=True,
    )


def _failing_eval(execution_id: str, *, reason: str) -> EvaluationResult:
    return EvaluationResult(
        execution_id=execution_id,
        stage2_result=_semantic_result(
            ac_compliance=False,
            score=0.3,
            reasoning=reason,
        ),
        final_approved=False,
    )


class TestDefinitionAcceptsMultiAC:
    """The tool schema must advertise the new acceptance_criteria parameter."""

    def test_acceptance_criteria_parameter_present(self) -> None:
        handler = EvaluateHandler()
        names = {p.name for p in handler.definition.parameters}

        assert "acceptance_criteria" in names
        assert "acceptance_criterion" in names  # backward-compat

        param = next(p for p in handler.definition.parameters if p.name == "acceptance_criteria")
        assert param.type == ToolInputType.ARRAY
        assert param.required is False


class TestMultiACRoutingBoundary:
    """Runtime behaviour of multi-AC routing in handle()."""

    def _install_pipeline_mock(self, eval_results: list[EvaluationResult]) -> MagicMock:
        mock_pipeline = AsyncMock()
        # `evaluate()` returns Result objects in order per call.
        results_iter = iter(eval_results)

        async def _evaluate(_context: object) -> object:
            return Result.ok(next(results_iter))

        mock_pipeline.evaluate = AsyncMock(side_effect=_evaluate)
        return mock_pipeline

    async def test_single_item_list_uses_single_ac_path(self) -> None:
        """A 1-element acceptance_criteria list falls back to single-AC path
        and the provided AC text is actually used (not the default).
        """
        mock_pipeline = self._install_pipeline_mock([_passing_eval("s1")])

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s1",
                    "artifact": "def f(): pass",
                    "acceptance_criteria": ["Only AC"],
                }
            )

        assert result.is_ok
        # Single-AC path — no multi_ac flag in meta.
        assert result.value.meta.get("multi_ac") is not True
        # The provided AC text must reach the pipeline, not the default.
        ctx_arg = mock_pipeline.evaluate.call_args[0][0]
        assert ctx_arg.current_ac == "Only AC"

    async def test_single_item_list_does_not_use_default_ac(self) -> None:
        """Regression: a 1-item acceptance_criteria must NOT fall back to the
        generic default 'Verify execution output meets requirements'.
        """
        mock_pipeline = self._install_pipeline_mock([_passing_eval("s1")])

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            await handler.handle(
                {
                    "session_id": "s1",
                    "artifact": "x = 1",
                    "acceptance_criteria": ["Payment webhook fires on success"],
                }
            )

        ctx_arg = mock_pipeline.evaluate.call_args[0][0]
        assert ctx_arg.current_ac == "Payment webhook fires on success"
        assert ctx_arg.current_ac != "Verify execution output meets requirements"

    async def test_singular_acceptance_criterion_forwarded(self) -> None:
        """The legacy ``acceptance_criterion`` (singular) param sets current_ac."""
        mock_pipeline = self._install_pipeline_mock([_passing_eval("s1")])

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s1",
                    "artifact": "def f(): pass",
                    "acceptance_criterion": "Legacy AC",
                }
            )

        assert result.is_ok
        ctx_arg = mock_pipeline.evaluate.call_args[0][0]
        assert ctx_arg.current_ac == "Legacy AC"

    async def test_single_item_list_ac_is_used_as_current_ac(self) -> None:
        """A 1-element acceptance_criteria list must be used as current_ac.

        Regression test: before the fix, a 1-item list was silently
        ignored and the evaluation ran against the fallback string
        'Verify execution output meets requirements'.
        """
        captured_contexts: list[object] = []
        mock_pipeline = AsyncMock()

        async def _capture_evaluate(context: object) -> object:
            captured_contexts.append(context)
            return Result.ok(_passing_eval("s1"))

        mock_pipeline.evaluate = AsyncMock(side_effect=_capture_evaluate)

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s1",
                    "artifact": "def f(): pass",
                    "acceptance_criteria": ["Log output is JSON-formatted"],
                }
            )

        assert result.is_ok
        assert len(captured_contexts) == 1
        ctx = captured_contexts[0]
        # The actual AC text must be used, not the generic fallback.
        assert ctx.current_ac == "Log output is JSON-formatted"

    async def test_two_passing_acs_produce_all_passed_checklist(self) -> None:
        """Two ACs both passing → meta.final_approved True, checklist populated."""
        mock_pipeline = self._install_pipeline_mock([_passing_eval("s1"), _passing_eval("s1")])

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s1",
                    "artifact": "def f(): pass",
                    "acceptance_criteria": ["AC one", "AC two"],
                }
            )

        assert result.is_ok
        meta = result.value.meta
        assert meta["multi_ac"] is True
        assert meta["ac_count"] == 2
        assert meta["passed_count"] == 2
        assert meta["pass_rate"] == 1.0
        assert meta["final_approved"] is True
        assert meta["run_feedback"] == []
        assert len(meta["checklist"]) == 2
        assert all(item["passed"] for item in meta["checklist"])
        assert "ALL PASSED" in result.value.text_content

    async def test_mixed_outcomes_produce_incomplete_checklist(self) -> None:
        """One passing + one failing AC → run_feedback lists the failure."""
        mock_pipeline = self._install_pipeline_mock(
            [
                _passing_eval("s2"),
                _failing_eval("s2", reason="Webhook signature not verified"),
            ]
        )

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s2",
                    "artifact": "def pay(): pass",
                    "acceptance_criteria": ["Charge processed", "Webhook validated"],
                }
            )

        assert result.is_ok
        meta = result.value.meta
        assert meta["multi_ac"] is True
        assert meta["ac_count"] == 2
        assert meta["passed_count"] == 1
        assert meta["final_approved"] is False
        assert len(meta["run_feedback"]) == 1
        assert "Webhook validated" in meta["run_feedback"][0]
        assert "INCOMPLETE" in result.value.text_content
        assert "[ ] 2. Webhook validated" in result.value.text_content

    async def test_empty_strings_in_list_are_filtered(self) -> None:
        """Whitespace/empty entries in acceptance_criteria are ignored.

        Two valid ACs after filtering must still take the multi-AC path.
        """
        mock_pipeline = self._install_pipeline_mock([_passing_eval("s3"), _passing_eval("s3")])

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s3",
                    "artifact": "x",
                    "acceptance_criteria": ["  ", "Valid AC one", "", "Valid AC two"],
                }
            )

        assert result.is_ok
        assert result.value.meta["multi_ac"] is True
        assert result.value.meta["ac_count"] == 2

    async def test_multi_ac_pipeline_error_propagates(self) -> None:
        """If any AC evaluation errors, the whole handle() returns err."""
        mock_pipeline = AsyncMock()
        calls = [
            Result.ok(_passing_eval("s4")),
            Result.err(ValueError("semantic stage exploded")),
        ]
        mock_pipeline.evaluate = AsyncMock(side_effect=calls)

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s4",
                    "artifact": "x",
                    "acceptance_criteria": ["AC1", "AC2"],
                }
            )

        assert result.is_err
        assert "Evaluation failed" in str(result.error)


@pytest.mark.parametrize(
    "value",
    [None, "not-a-list", 42],
)
class TestMultiACParameterTolerance:
    """Non-list / empty values for acceptance_criteria fall back to single-AC."""

    async def test_invalid_value_falls_back(self, value: object) -> None:
        mock_pipeline = AsyncMock()
        mock_pipeline.evaluate = AsyncMock(return_value=Result.ok(_passing_eval("s5")))

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=AsyncMock(initialize=AsyncMock()),
            ),
        ):
            MockPipeline.return_value = mock_pipeline
            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "s5",
                    "artifact": "x",
                    "acceptance_criteria": value,
                }
            )

        assert result.is_ok
        assert result.value.meta.get("multi_ac") is not True
