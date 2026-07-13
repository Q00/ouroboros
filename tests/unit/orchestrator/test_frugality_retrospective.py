"""Neutral frugality retrospective summary evidence."""

from __future__ import annotations

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.frugality_proof import (
    EVENT_AC_OUTCOME_FINALIZED,
    EVENT_TOKEN_ATTRIBUTION,
)
from ouroboros.orchestrator.frugality_retrospective import (
    summarize_frugality_retrospective,
)


def _token(
    ac_id: str,
    *,
    root: int,
    attempt: int,
    spend: object,
    execution_id: str = "exec-1",
) -> BaseEvent:
    return BaseEvent(
        type=EVENT_TOKEN_ATTRIBUTION,
        aggregate_type="ac",
        aggregate_id=ac_id,
        data={
            "execution_id": execution_id,
            "ac_id": ac_id,
            "root_ac_index": root,
            "retry_attempt": attempt,
            "token_spend": spend,
        },
    )


def _outcome(
    *,
    root: int,
    attempt: int,
    success: object,
    execution_id: str = "exec-1",
) -> BaseEvent:
    return BaseEvent(
        type=EVENT_AC_OUTCOME_FINALIZED,
        aggregate_type="execution",
        aggregate_id=execution_id,
        data={
            "execution_id": execution_id,
            "root_ac_index": root,
            "retry_attempt": attempt,
            "success": success,
            "is_decomposed": True,
        },
    )


def test_summary_reports_retry_and_unaccepted_spend_with_coverage() -> None:
    summary = summarize_frugality_retrospective(
        [
            _token("ac-0", root=0, attempt=0, spend=100),
            _outcome(root=0, attempt=0, success=False),
            _token("ac-0", root=0, attempt=1, spend=40),
            _outcome(root=0, attempt=1, success=True),
            _token("ac-1", root=1, attempt=0, spend=25),
        ],
        execution_id="exec-1",
    )

    assert summary.retry_associated_spend == pytest.approx(40.0)
    assert summary.unaccepted_spend == pytest.approx(100.0)
    assert summary.measured_attempts == 2
    assert summary.unknown_attempts == 1
    assert summary.invalid_attempts == 0


def test_summary_rejects_malformed_or_duplicate_attempt_evidence() -> None:
    summary = summarize_frugality_retrospective(
        [
            _token("ac-bad", root=0, attempt=0, spend=-1),
            _token("ac-dupe", root=0, attempt=0, spend=10),
            _token("ac-dupe", root=0, attempt=0, spend=20),
            _outcome(root=0, attempt=0, success=True),
            _token("ac-outcome", root=1, attempt=0, spend=30),
            _outcome(root=1, attempt=0, success="yes"),
        ],
        execution_id="exec-1",
    )

    assert summary.retry_associated_spend == 0.0
    assert summary.unaccepted_spend == 0.0
    assert summary.measured_attempts == 0
    assert summary.unknown_attempts == 0
    assert summary.invalid_attempts == 4


def test_summary_includes_optional_proof_provenance_only_when_present() -> None:
    proof = BaseEvent(
        type="execution.frugality_proof.evaluated",
        aggregate_type="execution",
        aggregate_id="exec-1",
        data={"status": "insufficient_data"},
    )

    summary = summarize_frugality_retrospective([proof], execution_id="exec-1")

    assert summary.frugality_proof_event_id == proof.id
    assert summary.frugality_proof_status == "insufficient_data"
    assert summary.to_event_data()["frugality_proof"] == {
        "event_id": proof.id,
        "status": "insufficient_data",
    }
