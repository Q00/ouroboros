"""Closed run-failure cause derivation from durable executor evidence."""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.failure_taxonomy import RUN_FAILURE_CAUSES, FailureReasonCode
from ouroboros.orchestrator.run_failure_cause import (
    derive_run_failure_cause,
    failure_reason_code_for_run_cause,
)
from ouroboros.orchestrator.verify_gate_outcome import _VERIFY_GATE_CAUSES

SESSION = "orch_test"
OTHER_SESSION = "orch_other"
EXECUTION = "exec_test"


def _execution_event(event_type: str, session_id: str = SESSION, **data: Any) -> BaseEvent:
    return BaseEvent(
        type=event_type,
        aggregate_type="execution",
        aggregate_id=EXECUTION,
        data={"session_id": session_id, "execution_id": EXECUTION, **data},
    )


def _session_failed(session_id: str = SESSION, **data: Any) -> BaseEvent:
    return BaseEvent(
        type="orchestrator.session.failed",
        aggregate_type="session",
        aggregate_id=session_id,
        data={"error": "Partial failure", "error_type": None, **data},
    )


def _derive(events: list[BaseEvent], *, terminal_status: str = "failed") -> str:
    return derive_run_failure_cause(events, session_id=SESSION, terminal_status=terminal_status)


def test_vocabulary_covers_every_verify_gate_cause() -> None:
    assert {f"verify_{cause}" for cause in _VERIFY_GATE_CAUSES} <= RUN_FAILURE_CAUSES


def test_empty_evidence_is_unknown() -> None:
    assert _derive([]) == "unknown"


def test_cancelled_terminal_status_wins() -> None:
    events = [_execution_event("execution.verify.failed", verify_cause="exit_nonzero")]
    assert _derive(events, terminal_status="cancelled") == "cancelled"


def test_settlement_rejection_outranks_earlier_gate_failures() -> None:
    """The local replay of orch_31443ae9e15f: a retried gate failure, then a
    settlement-wide rejection that actually failed the run."""
    events = [
        _execution_event("execution.verify.failed", ac_index=0, verify_cause="exit_nonzero"),
        _execution_event(
            "execution.verify.failed",
            ac_index=0,
            verify_cause="workspace_mutated",
            final_workspace_revalidation=True,
        ),
        _execution_event(
            "execution.verify.failed",
            ac_index=2,
            verify_cause="workspace_mutated",
            final_workspace_revalidation=True,
        ),
        _session_failed(),
    ]
    assert _derive(events) == "verify_workspace_mutated"


def test_exhausted_ac_is_attributed_to_its_last_verify_cause() -> None:
    """The local replay of orch_1eb248ce6b3b: three attempts, the last one
    rejected by the gate, then the retry budget ran out."""
    events = [
        _execution_event(
            "execution.ac.attempt_judged", ac_index=0, root_ac_index=0, outcome="failed"
        ),
        _execution_event(
            "execution.ac.attempt_judged", ac_index=0, root_ac_index=0, outcome="failed"
        ),
        _execution_event("execution.verify.failed", ac_index=0, verify_cause="workspace_mutated"),
        _execution_event(
            "execution.ac.recovery_exhausted",
            root_ac_index=0,
            last_failure_class="EVIDENCE_MISSING",
            retry_termination_reason="budget_exhausted",
        ),
        _execution_event(
            "execution.ac.attempt_judged", ac_index=1, root_ac_index=1, outcome="blocked"
        ),
        _session_failed(),
    ]
    assert _derive(events) == "verify_workspace_mutated"


def test_exhausted_ac_without_verify_cause_uses_audited_failure_class() -> None:
    events = [
        _execution_event(
            "execution.ac.recovery_exhausted",
            root_ac_index=0,
            last_failure_class="FABRICATION_SUSPECTED",
            retry_termination_reason="repeated_failure_early_stop",
        ),
    ]
    assert _derive(events) == "worker_fabrication_suspected"


def test_exhausted_ac_with_unknown_class_is_worker_failed() -> None:
    events = [
        _execution_event(
            "execution.ac.recovery_exhausted",
            root_ac_index=0,
            last_failure_class="unknown",
            retry_termination_reason="budget_exhausted",
        ),
    ]
    assert _derive(events) == "worker_failed"


def test_verify_attributed_exhaustion_outranks_unattributed_sibling() -> None:
    events = [
        _execution_event(
            "execution.ac.recovery_exhausted", root_ac_index=0, last_failure_class="unknown"
        ),
        _execution_event(
            "execution.ac.recovery_exhausted", root_ac_index=1, last_failure_class="unknown"
        ),
        _execution_event("execution.verify.failed", ac_index=2, verify_cause="exit_nonzero"),
        _execution_event(
            "execution.ac.recovery_exhausted", root_ac_index=2, last_failure_class="unknown"
        ),
    ]
    assert _derive(events) == "verify_exit_nonzero"


def test_orchestrator_exception_is_runtime_error() -> None:
    events = [_session_failed(error_type="OrchestratorError")]
    assert _derive(events) == "runtime_error"


def test_all_blocked_attempts_is_dependency_blocked() -> None:
    events = [
        _execution_event("execution.ac.attempt_judged", ac_index=0, outcome="blocked"),
        _execution_event("execution.ac.attempt_judged", ac_index=1, outcome="blocked"),
        _session_failed(),
    ]
    assert _derive(events) == "dependency_blocked"


def test_other_sessions_sharing_the_execution_are_ignored() -> None:
    events = [
        _execution_event(
            "execution.verify.failed",
            session_id=OTHER_SESSION,
            ac_index=0,
            verify_cause="exit_nonzero",
            final_workspace_revalidation=True,
        ),
        _session_failed(session_id=OTHER_SESSION, error_type="OrchestratorError"),
    ]
    assert _derive(events) == "unknown"


def test_unaudited_verify_cause_is_not_forwarded() -> None:
    events = [
        _execution_event(
            "execution.verify.failed",
            ac_index=0,
            verify_cause="rm -rf / went wrong",
            final_workspace_revalidation=True,
        )
    ]
    assert _derive(events) == "unknown"


def test_every_derivable_cause_is_in_the_closed_set() -> None:
    for cause in RUN_FAILURE_CAUSES:
        assert isinstance(failure_reason_code_for_run_cause(cause), FailureReasonCode)


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        ("verify_workspace_mutated", FailureReasonCode.VALIDATION),
        ("verify_exit_nonzero", FailureReasonCode.VALIDATION),
        ("verify_timeout", FailureReasonCode.TIMEOUT),
        ("verify_environment_unverifiable", FailureReasonCode.CONFIG),
        ("worker_failed", FailureReasonCode.VALIDATION),
        ("dependency_blocked", FailureReasonCode.VALIDATION),
        ("runtime_error", FailureReasonCode.TOOL),
        ("cancelled", FailureReasonCode.CANCELLED),
        ("unknown", FailureReasonCode.UNKNOWN),
        ("not a cause", FailureReasonCode.UNKNOWN),
    ],
)
def test_reason_code_fold(cause: str, expected: FailureReasonCode) -> None:
    assert failure_reason_code_for_run_cause(cause) is expected
