"""``ouroboros_evaluate`` reports unverified results as not approved (#2449).

The pipeline's approval gate requires executed Stage 1 evidence. The handler
must surface that decision as ``acceptance_state`` and must not render an
unverified result as a rejection or an approval.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from ouroboros.core.types import Result
from ouroboros.evaluation.models import (
    CheckResult,
    CheckType,
    EvaluationResult,
    MechanicalResult,
    SemanticResult,
)
from ouroboros.mcp.tools.evaluation_handlers import EvaluateHandler

_FAVORABLE_REVIEW = SemanticResult(
    score=0.95,
    ac_compliance=True,
    goal_alignment=0.9,
    drift_score=0.1,
    uncertainty=0.1,
    reasoning="Looks complete.",
)


async def _handle(result: EvaluationResult, session_id: str) -> tuple[dict, str]:
    pipeline = AsyncMock()
    pipeline.evaluate = AsyncMock(return_value=Result.ok(result))
    with (
        patch("ouroboros.evaluation.EvaluationPipeline", return_value=pipeline),
        patch(
            "ouroboros.persistence.event_store.EventStore",
            return_value=AsyncMock(initialize=AsyncMock()),
        ),
        patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture"),
    ):
        outcome = await EvaluateHandler().handle(
            {
                "session_id": session_id,
                "artifact": "def f(): pass",
                "acceptance_criterion": "Only AC",
            }
        )
    assert outcome.is_ok
    return dict(outcome.value.meta), outcome.value.content[0].text


async def test_unverified_result_is_reported_as_not_approved_unverified() -> None:
    skipped = MechanicalResult(
        passed=True,
        checks=(
            CheckResult(
                check_type=CheckType.TEST,
                passed=True,
                message="skipped",
                details={"skipped": True},
            ),
        ),
    )
    meta, text = await _handle(
        EvaluationResult(
            execution_id="u1",
            stage1_result=skipped,
            stage2_result=_FAVORABLE_REVIEW,
            final_approved=False,
        ),
        "u1",
    )

    assert meta["final_approved"] is False
    assert meta["acceptance_state"] == "unverified"
    assert meta["executed_evidence"] is False
    assert meta["failure_reason"].startswith("Not approved: unverified.")
    assert "Final Approval: NOT APPROVED (unverified" in text
    assert "Final Approval: REJECTED" not in text
    assert "Stage 1 ran no configured check" in text


async def test_executed_approval_is_reported_as_approved() -> None:
    executed = MechanicalResult(
        passed=True,
        checks=(CheckResult(check_type=CheckType.TEST, passed=True, message="ok", executed=True),),
    )
    meta, text = await _handle(
        EvaluationResult(
            execution_id="a1",
            stage1_result=executed,
            stage2_result=_FAVORABLE_REVIEW,
            final_approved=True,
        ),
        "a1",
    )

    assert meta["acceptance_state"] == "approved"
    assert meta["executed_evidence"] is True
    assert meta["failure_reason"] is None
    assert "Final Approval: APPROVED" in text


async def test_multi_ac_without_executed_evidence_is_unverified() -> None:
    pipeline = AsyncMock()
    pipeline.evaluate = AsyncMock(
        return_value=Result.ok(
            EvaluationResult(
                execution_id="m1",
                stage1_result=None,
                stage2_result=_FAVORABLE_REVIEW,
                final_approved=False,
            )
        )
    )
    with (
        patch("ouroboros.evaluation.EvaluationPipeline", return_value=pipeline),
        patch(
            "ouroboros.persistence.event_store.EventStore",
            return_value=AsyncMock(initialize=AsyncMock()),
        ),
        patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture"),
    ):
        outcome = await EvaluateHandler().handle(
            {
                "session_id": "m1",
                "artifact": "def f(): pass",
                "acceptance_criteria": ["First AC", "Second AC"],
            }
        )

    assert outcome.is_ok
    meta = outcome.value.meta
    assert meta["final_approved"] is False
    assert meta["acceptance_state"] == "unverified"
    assert meta["executed_evidence"] is False
    assert all(
        item["failure_reason"].startswith("Not approved: unverified.") for item in meta["checklist"]
    )
