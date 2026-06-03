"""Failure-taxonomy routing for #978 deliver-gate verdicts.

This module is the read-only #978 P3 routing primitive. It converts a
TraceGuard-derived :class:`ouroboros.harness.deliver_gate.DeliverGateVerdict`
into the existing recovery action vocabulary without mutating runtime state or
changing AC success semantics.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.harness.deliver_gate import DeliverGateVerdict
from ouroboros.orchestrator.failure_taxonomy import RecoveryAction
from ouroboros.orchestrator.verifier import RetryAdmission, VerifierStatus, VerifierVerdict


@dataclass(frozen=True, slots=True)
class DeliverGateRoute:
    """Routing decision for one deliver-gate verdict."""

    accepted: bool
    action: RecoveryAction | None
    reason: str
    source_reasons: tuple[str, ...] = ()


_RETRY_REASONS = frozenset(
    {
        "evidence_missing",
        "missing_evidence_handle",
        "runtime_tool_error",
        "tool_error",
        "verifier_unavailable",
    }
)
_REDISPATCH_REASONS = frozenset(
    {
        "unsupported_fact_id",
        "chunk_handle_without_fact",
        "evidence_handle_mismatch",
        "malformed_evidence_claim",
        "semantic_miss",
    }
)
_HITL_REASONS = frozenset(
    {
        "external_dependency_missing",
        "approval_required",
        "policy_blocked",
        "human_input_required",
    }
)


def route_deliver_gate_verdict(
    verdict: DeliverGateVerdict,
    *,
    rejection_count: int = 1,
    model_escalation_threshold: int = 2,
) -> DeliverGateRoute:
    """Map a deliver-gate verdict to the next recovery action.

    Args:
        verdict: TraceGuard-derived AC deliver-gate verdict.
        rejection_count: Number of consecutive rejected verdicts for the same
            AC attempt lineage. ``1`` means the first rejection.
        model_escalation_threshold: Rejection count at which repeated
            non-HITL/non-retry verification failures escalate to a stronger
            model rather than redispatching again.
    """
    if rejection_count < 1:
        msg = f"rejection_count must be >= 1, got {rejection_count}"
        raise ValueError(msg)
    if model_escalation_threshold < 1:
        msg = f"model_escalation_threshold must be >= 1, got {model_escalation_threshold}"
        raise ValueError(msg)

    if verdict.accepted:
        return DeliverGateRoute(
            accepted=True,
            action=None,
            reason="deliver_gate_accepted",
            source_reasons=(),
        )

    reasons = verdict.rejected_reasons
    reason_codes = tuple(_reason_code(reason) for reason in reasons)
    if not reason_codes:
        return DeliverGateRoute(
            accepted=False,
            action=RecoveryAction.REDISPATCH,
            reason="deliver_gate_rejected_without_reason",
            source_reasons=(),
        )

    if any(reason in _HITL_REASONS for reason in reason_codes):
        return DeliverGateRoute(
            accepted=False,
            action=RecoveryAction.ESCALATE_HUMAN,
            reason="deliver_gate_requires_human",
            source_reasons=reasons,
        )

    if any(reason in _RETRY_REASONS for reason in reason_codes):
        return DeliverGateRoute(
            accepted=False,
            action=RecoveryAction.RETRY,
            reason="deliver_gate_retryable_evidence_gap",
            source_reasons=reasons,
        )

    if rejection_count >= model_escalation_threshold:
        return DeliverGateRoute(
            accepted=False,
            action=RecoveryAction.ESCALATE_MODEL,
            reason="deliver_gate_repeated_rejection",
            source_reasons=reasons,
        )

    if any(reason in _REDISPATCH_REASONS for reason in reason_codes):
        return DeliverGateRoute(
            accepted=False,
            action=RecoveryAction.REDISPATCH,
            reason="deliver_gate_redispatch_required",
            source_reasons=reasons,
        )

    return DeliverGateRoute(
        accepted=False,
        action=RecoveryAction.REDISPATCH,
        reason="deliver_gate_unknown_rejection",
        source_reasons=reasons,
    )


def deliver_gate_verifier_verdict(
    verdict: DeliverGateVerdict,
    *,
    rejection_count: int = 1,
    model_escalation_threshold: int = 2,
) -> VerifierVerdict:
    """Promote a TraceGuard deliver verdict into the H1 verifier contract.

    This is the #1306 behavior-change bridge: callers can pass the returned
    ``VerifierVerdict`` through the existing H1 acceptance boundary instead of
    treating ``DeliverGateVerdict`` as read-only observation.
    """
    route = route_deliver_gate_verdict(
        verdict,
        rejection_count=rejection_count,
        model_escalation_threshold=model_escalation_threshold,
    )
    if route.accepted:
        return VerifierVerdict(
            passed=True,
            status=VerifierStatus.PASS,
            evidence_used=verdict.evidence_event_ids,
            retry_admission=RetryAdmission.ACCEPT,
        )

    failure_class = _failure_class_for_route(route)
    status = VerifierStatus.BLOCKED if failure_class == "BLOCKED" else VerifierStatus.FAIL
    return VerifierVerdict(
        passed=False,
        reasons=(
            f"{route.reason}: " + "; ".join(route.source_reasons)
            if route.source_reasons
            else route.reason,
        ),
        failure_class=failure_class,
        status=status,
        evidence_used=verdict.evidence_event_ids,
        retry_admission=_retry_admission_for_route(route),
    )


def _retry_admission_for_route(route: DeliverGateRoute) -> RetryAdmission:
    if route.action is RecoveryAction.RETRY:
        return RetryAdmission.RETRY
    if route.action is RecoveryAction.REDISPATCH:
        return RetryAdmission.REDISPATCH
    if route.action is RecoveryAction.ESCALATE_MODEL:
        return RetryAdmission.ESCALATE_MODEL
    if route.action is RecoveryAction.ESCALATE_HUMAN:
        return RetryAdmission.ESCALATE_HUMAN
    return RetryAdmission.BLOCK


def _failure_class_for_route(route: DeliverGateRoute) -> str:
    reason_codes = tuple(_reason_code(reason) for reason in route.source_reasons)
    if route.action is RecoveryAction.ESCALATE_HUMAN:
        return "BLOCKED"
    if any(reason in _RETRY_REASONS for reason in reason_codes):
        return "EVIDENCE_MISSING"
    if any(reason == "semantic_miss" for reason in reason_codes):
        return "SCOPE_CREEP"
    if any(
        reason in {"evidence_handle_mismatch", "chunk_handle_without_fact"}
        for reason in reason_codes
    ):
        return "EVIDENCE_FORM_MISMATCH"
    if any(
        reason in {"unsupported_fact_id", "malformed_evidence_claim"} for reason in reason_codes
    ):
        return "FABRICATION_SUSPECTED"
    if route.action is RecoveryAction.ESCALATE_MODEL:
        return "FABRICATION_SUSPECTED"
    if route.action is RecoveryAction.REDISPATCH:
        return "STALL"
    return "BLOCKED"


def _reason_code(reason: str) -> str:
    return reason.split(":", maxsplit=1)[0].strip()


__all__ = [
    "DeliverGateRoute",
    "deliver_gate_verifier_verdict",
    "route_deliver_gate_verdict",
]
