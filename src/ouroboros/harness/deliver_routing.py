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


def _reason_code(reason: str) -> str:
    return reason.split(":", maxsplit=1)[0].strip()


__all__ = [
    "DeliverGateRoute",
    "route_deliver_gate_verdict",
]
