"""An empty transcript is a collection fault, not a worker rejection.

RFC ``verify-gate-windows-portability`` Part B3: a leaf that did real work and
whose transcript never reached the verifier must not be classified the same way
as a leaf whose claims are unsupported — a gaming leaf leaves plausible-looking
messages, not none.
"""

from __future__ import annotations

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.failure_taxonomy import FailureClass, RecoveryAction, policy_for
from ouroboros.orchestrator.profile_loader import load_profile
from ouroboros.orchestrator.verifier import RetryAdmission


def _verify(messages: tuple[AgentMessage, ...]) -> object:
    return _verify_atomic_evidence_against_runtime_messages(
        messages=messages,
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["src/app.py"],
                "commands_run": ["pytest -q"],
                "tests_passed": ["tests/test_app.py"],
            }
        ),
        ac_content="Implement AC 1",
        execution_profile=load_profile("code"),
        task_cwd=None,
        adapter_working_directory=None,
    )


def test_empty_transcript_is_classified_as_infrastructure() -> None:
    verdict = _verify((AgentMessage(type="result", content="done"),))

    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.TRANSCRIPT_MISSING_INFRASTRUCTURE.value
    assert "transcript_missing_infrastructure" in verdict.reasons[0]
    # The worker is not accused of anything it cannot answer for.
    assert "unsupported evidence claims" not in verdict.reasons[0]


def test_unsupported_claims_stay_a_worker_rejection() -> None:
    verdict = _verify(
        (
            AgentMessage(type="assistant", content="I looked around a bit."),
            AgentMessage(type="result", content="done"),
        )
    )

    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


def test_infrastructure_class_continues_without_resplitting_the_ac() -> None:
    verdict = _verify((AgentMessage(type="result", content="done"),))

    # Re-splitting or escalating would throw away work that was never judged.
    assert verdict.retry_admission is RetryAdmission.ACCEPT
    assert policy_for(FailureClass.TRANSCRIPT_MISSING_INFRASTRUCTURE).action is (
        RecoveryAction.CONTINUE
    )
