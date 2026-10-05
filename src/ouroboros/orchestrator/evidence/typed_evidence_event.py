"""Fields of the ``execution.ac.typed_evidence.observed`` event that describe the evidence.

Built from the verifier verdict, the typed evidence record and its validation;
``ParallelACExecutor._emit_atomic_typed_evidence_event`` adds the run identity.
The evidence path that decided the verdict (``verifier_decided_by``) and what
the evidence turn produced (``evidence_turn``: cited numbers, replay outcomes,
relevance decisions) are recorded here; the worker's prose never is.
"""

from __future__ import annotations

from typing import Any

from ouroboros.orchestrator.evidence.ac_classification import (
    _out_of_scope_evidence_fields_for_ac,
    _out_of_scope_evidence_values_for_ac,
)
from ouroboros.orchestrator.evidence.cited_evidence import CitedEvidence
from ouroboros.orchestrator.evidence_schema import EvidenceRecord, ValidationResult
from ouroboros.orchestrator.evidence_turn_dispatch import cited_evidence_summary
from ouroboros.orchestrator.profile_loader import ExecutionProfile
from ouroboros.orchestrator.verifier import EVIDENCE_PATH_STRINGS, VerifierVerdict


def typed_evidence_event_details(
    profile: ExecutionProfile,
    ac_content: str,
    *,
    typed_evidence: EvidenceRecord | None,
    typed_validation: ValidationResult | None,
    verifier_verdict: VerifierVerdict | None,
    has_success_contract: bool,
    has_expected_artifacts: bool,
    verify_gate_active: bool,
) -> dict[str, Any]:
    """The verdict, record and validation fields of the typed-evidence event."""
    data: dict[str, Any] = {}
    if verifier_verdict is not None:
        data["verifier_reasons"] = list(verifier_verdict.reasons)
        data["verifier_failure_class"] = verifier_verdict.failure_class
        data["verifier_status"] = verifier_verdict.status.value
        data["retry_admission"] = verifier_verdict.retry_admission.value
        data["verifier_evidence_used"] = list(verifier_verdict.evidence_used)
        data["verifier_not_replayed"] = list(verifier_verdict.not_replayed)
        data["verifier_withheld"] = list(verifier_verdict.withheld)
        data["verifier_decided_by"] = verifier_verdict.decided_by or EVIDENCE_PATH_STRINGS
    if typed_evidence is not None:
        scope = {
            "has_success_contract": has_success_contract,
            "has_expected_artifacts": has_expected_artifacts,
            "verify_gate_active": verify_gate_active,
        }
        data["typed_evidence_fields"] = sorted(typed_evidence.data)
        data["ignored_out_of_scope_evidence_fields"] = list(
            _out_of_scope_evidence_fields_for_ac(profile, ac_content, typed_evidence, **scope)
        )
        data["ignored_out_of_scope_evidence"] = _out_of_scope_evidence_values_for_ac(
            profile, ac_content, typed_evidence, **scope
        )
        cited = typed_evidence.cited
        data["evidence_turn"] = (
            cited_evidence_summary(cited) if isinstance(cited, CitedEvidence) else None
        )
    if typed_validation is not None:
        data["missing_fields"] = list(typed_validation.missing_fields)
        data["rejected_by"] = list(typed_validation.rejected_by)
        data["blocker"] = (
            typed_validation.blocker.summary() if typed_validation.blocker is not None else None
        )
    return data


def verifier_rejection_log_fields(
    typed_evidence: EvidenceRecord | None,
    typed_validation: ValidationResult | None,
    verifier_verdict: VerifierVerdict | None,
) -> dict[str, Any]:
    """Structured log fields for a verifier rejection of an atomic AC."""
    verdict = verifier_verdict
    return {
        "typed_evidence_present": typed_evidence is not None,
        "typed_evidence_valid": typed_validation.ok if typed_validation is not None else False,
        "verifier_ran": verdict is not None,
        "verifier_passed": verdict.passed if verdict is not None else False,
        "verifier_reasons": list(verdict.reasons) if verdict is not None else [],
        "verifier_failure_class": verdict.failure_class if verdict is not None else None,
        "verifier_status": verdict.status.value if verdict is not None else None,
        "retry_admission": verdict.retry_admission.value if verdict is not None else None,
        "verifier_evidence_used": list(verdict.evidence_used) if verdict is not None else [],
        "verifier_decided_by": (
            (verdict.decided_by or EVIDENCE_PATH_STRINGS) if verdict is not None else None
        ),
    }


__all__ = ["typed_evidence_event_details", "verifier_rejection_log_fields"]
