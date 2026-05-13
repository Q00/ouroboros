"""Tests for #978 P3 deliver-gate failure-taxonomy routing."""

from __future__ import annotations

import pytest

from ouroboros.harness.deliver_gate import DeliverGateVerdict
from ouroboros.harness.deliver_routing import route_deliver_gate_verdict
from ouroboros.orchestrator.failure_taxonomy import RecoveryAction


def _verdict(*, accepted: bool, reasons: tuple[str, ...] = ()) -> DeliverGateVerdict:
    return DeliverGateVerdict(
        ac_id="AC-1",
        accepted=accepted,
        unsupported_claim_rate=0.0 if accepted else 1.0,
        rejected_fact_ids=() if accepted else ("fact_1",),
        rejected_reasons=reasons,
    )


def test_accepted_verdict_has_no_recovery_action() -> None:
    route = route_deliver_gate_verdict(_verdict(accepted=True))

    assert route.accepted is True
    assert route.action is None
    assert route.reason == "deliver_gate_accepted"


def test_missing_evidence_routes_to_retry() -> None:
    route = route_deliver_gate_verdict(
        _verdict(accepted=False, reasons=("missing_evidence_handle: ev_1 was not found",))
    )

    assert route.action is RecoveryAction.RETRY
    assert route.reason == "deliver_gate_retryable_evidence_gap"


def test_unsupported_fact_routes_to_redispatch_before_escalation_threshold() -> None:
    route = route_deliver_gate_verdict(
        _verdict(accepted=False, reasons=("unsupported_fact_id: fact_1 is not present",)),
        rejection_count=1,
        model_escalation_threshold=2,
    )

    assert route.action is RecoveryAction.REDISPATCH
    assert route.reason == "deliver_gate_redispatch_required"


def test_repeated_traceguard_rejections_route_to_model_escalation() -> None:
    route = route_deliver_gate_verdict(
        _verdict(accepted=False, reasons=("unsupported_fact_id: fact_1 is not present",)),
        rejection_count=2,
        model_escalation_threshold=2,
    )

    assert route.action is RecoveryAction.ESCALATE_MODEL
    assert route.reason == "deliver_gate_repeated_rejection"


def test_external_dependency_routes_to_hitl() -> None:
    route = route_deliver_gate_verdict(
        _verdict(accepted=False, reasons=("external_dependency_missing: API key missing",))
    )

    assert route.action is RecoveryAction.ESCALATE_HUMAN
    assert route.reason == "deliver_gate_requires_human"


def test_rejects_invalid_routing_counters() -> None:
    with pytest.raises(ValueError, match="rejection_count"):
        route_deliver_gate_verdict(
            _verdict(accepted=False, reasons=("unsupported_fact_id",)), rejection_count=0
        )

    with pytest.raises(ValueError, match="model_escalation_threshold"):
        route_deliver_gate_verdict(
            _verdict(accepted=False, reasons=("unsupported_fact_id",)),
            model_escalation_threshold=0,
        )
