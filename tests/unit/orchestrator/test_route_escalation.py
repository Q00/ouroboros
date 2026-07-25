"""Bounded escalation and raw observation contract tests."""

from __future__ import annotations

from collections.abc import Mapping
import json

import pytest

from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.route_escalation import (
    EscalationAction,
    EscalationReason,
    RouteObservation,
    VerifierOutcome,
    advance_route,
)
from ouroboros.orchestrator.route_policy import (
    RouteCandidate,
    RouteRegistry,
    RouteRequirements,
)


def _route(route_id: str, *, cost: int, capabilities: tuple[str, ...] = ()) -> RouteCandidate:
    return RouteCandidate(
        route_id=route_id,
        model=f"model-{route_id}",
        harness="harness-a",
        effort="medium",
        cost_units=cost,
        persona="default",
        tool_policy="default",
        authority_identity="authority-default",
        capabilities=capabilities,
        ordinal=cost,
    )


def _registry() -> RouteRegistry:
    return RouteRegistry(
        candidates=(
            _route("cheap", cost=1, capabilities=("tools",)),
            _route("standard", cost=10, capabilities=("tools",)),
            _route("frontier", cost=20, capabilities=("tools", "vision")),
        )
    )


def test_observation_snapshots_route_and_capability_match_without_authority_identity() -> None:
    candidate = _route("cheap", cost=1, capabilities=("tools",))
    observation = RouteObservation.from_candidate(
        candidate,
        RouteRequirements(required_capabilities=("tools",)),
        episode_id="episode-1",
        attempt_index=0,
        verifier_outcome=VerifierOutcome.FAILED,
        failure_class=FailureClass.FABRICATION_SUSPECTED,
        escalation_reason=EscalationReason.CLASSIFIED_FAILURE,
    )

    payload = observation.to_contract_data()

    assert payload["route_id"] == "cheap"
    assert payload["cost_units"] == 1
    assert payload["capability_match"] is True
    assert "authority_identity" not in payload
    assert json.loads(json.dumps(payload)) == payload


def test_observation_round_trip_is_deterministic_and_strict() -> None:
    candidate = _route("cheap", cost=1)
    observation = RouteObservation.from_candidate(
        candidate,
        RouteRequirements(),
        episode_id="episode-1",
        attempt_index=0,
        verifier_outcome=VerifierOutcome.ACCEPTED,
    )

    assert RouteObservation.from_contract_data(observation.to_contract_data()) == observation
    malformed = observation.to_contract_data()
    malformed["unexpected"] = True
    with pytest.raises(ValueError, match="unsupported shape"):
        RouteObservation.from_contract_data(malformed)

    class InfiniteMapping(Mapping[str, object]):
        def __iter__(self):
            yield from malformed
            index = 0
            while True:
                yield f"unexpected-{index}"
                index += 1

        def __len__(self) -> int:
            return 1

        def __getitem__(self, key: str) -> object:
            return malformed[key]

    with pytest.raises(ValueError, match="unsupported shape"):
        RouteObservation.from_contract_data(InfiniteMapping())
    malformed = observation.to_contract_data()
    malformed["version"] = True
    with pytest.raises(ValueError, match="unsupported shape"):
        RouteObservation.from_contract_data(malformed)


def test_observation_rejects_mismatched_capability_claim() -> None:
    with pytest.raises(ValueError, match="capability_match"):
        RouteObservation(
            episode_id="episode-1",
            attempt_index=0,
            route_id="cheap",
            model="model-cheap",
            harness="harness-a",
            effort="medium",
            cost_units=1,
            capabilities=(),
            required_capabilities=("tools",),
            capability_match=True,
            verifier_outcome=VerifierOutcome.FAILED,
            failure_class=FailureClass.STALL,
        )


@pytest.mark.parametrize(
    "secret",
    ["ghp_example", "runtime:hvs.abcdefghijklmnop", "header123.payload12.signature123"],
)
def test_observation_rejects_credential_shaped_route_metadata(secret: str) -> None:
    with pytest.raises(ValueError):
        RouteObservation(
            episode_id="episode-1",
            attempt_index=0,
            route_id="cheap",
            model=secret,
            harness="harness-a",
            effort="medium",
            cost_units=1,
            capabilities=(),
            required_capabilities=(),
            capability_match=True,
            verifier_outcome=VerifierOutcome.FAILED,
            failure_class=FailureClass.STALL,
        )


def test_observation_rejects_overloaded_integer_subclasses() -> None:
    class ExplodingInt(int):
        def __lt__(self, other: object) -> bool:
            raise RuntimeError("comparison")

    candidate = _route("cheap", cost=1)
    with pytest.raises(ValueError, match="integer"):
        RouteObservation.from_candidate(
            candidate,
            RouteRequirements(),
            episode_id="episode-1",
            attempt_index=ExplodingInt(0),  # type: ignore[arg-type]
            verifier_outcome=VerifierOutcome.ACCEPTED,
        )
    with pytest.raises(ValueError, match="integer"):
        RouteObservation(
            episode_id="episode-1",
            attempt_index=0,
            route_id="cheap",
            model="model-cheap",
            harness="harness-a",
            effort="medium",
            cost_units=ExplodingInt(1),  # type: ignore[arg-type]
            capabilities=(),
            required_capabilities=(),
            capability_match=True,
            verifier_outcome=VerifierOutcome.ACCEPTED,
        )


def test_observation_rejects_accepted_failure_metadata_and_invalid_blocked_class() -> None:
    candidate = _route("cheap", cost=1)
    with pytest.raises(ValueError, match="accepted observations"):
        RouteObservation.from_candidate(
            candidate,
            RouteRequirements(),
            episode_id="episode-1",
            attempt_index=0,
            verifier_outcome=VerifierOutcome.ACCEPTED,
            failure_class=FailureClass.STALL,
        )
    with pytest.raises(ValueError, match="blocked observations"):
        RouteObservation.from_candidate(
            candidate,
            RouteRequirements(),
            episode_id="episode-1",
            attempt_index=0,
            verifier_outcome=VerifierOutcome.BLOCKED,
            failure_class=FailureClass.STALL,
        )


def test_fabrication_escalates_to_next_eligible_route_in_cost_order() -> None:
    decision = advance_route(
        _registry(),
        RouteRequirements(required_capabilities=("tools",)),
        current_route_id="cheap",
        attempted_route_ids=("cheap",),
        failure_class=FailureClass.FABRICATION_SUSPECTED,
    )

    assert decision.action is EscalationAction.ESCALATE_ROUTE
    assert decision.selected is not None
    assert decision.selected.route_id == "standard"
    assert decision.remaining_route_ids == ("frontier",)
    assert decision.reason is EscalationReason.CLASSIFIED_FAILURE


def test_escalation_respects_hard_capability_constraints() -> None:
    registry = RouteRegistry(
        candidates=(
            _route("cheap", cost=1, capabilities=("tools",)),
            _route("standard", cost=10, capabilities=("tools",)),
            _route("frontier", cost=20, capabilities=("vision",)),
        )
    )
    decision = advance_route(
        registry,
        RouteRequirements(required_capabilities=("tools",)),
        current_route_id="cheap",
        attempted_route_ids=("cheap",),
        failure_class=FailureClass.FABRICATION_SUSPECTED,
    )

    assert decision.selected is not None
    assert decision.selected.route_id == "standard"
    assert decision.remaining_route_ids == ()


def test_exhausted_escalation_becomes_explicit_blocked() -> None:
    decision = advance_route(
        _registry(),
        RouteRequirements(),
        current_route_id="frontier",
        attempted_route_ids=("cheap", "standard", "frontier"),
        failure_class=FailureClass.FABRICATION_SUSPECTED,
    )

    assert decision.action is EscalationAction.BLOCKED
    assert decision.selected is None
    assert decision.reason is EscalationReason.ROUTES_EXHAUSTED
    assert decision.blocked is True


def test_attempt_history_cannot_skip_an_eligible_route() -> None:
    with pytest.raises(ValueError, match="skipped"):
        advance_route(
            _registry(),
            RouteRequirements(),
            current_route_id="frontier",
            attempted_route_ids=("frontier",),
            failure_class=FailureClass.FABRICATION_SUSPECTED,
        )


def test_retryable_evidence_failure_retries_current_route_without_escalating() -> None:
    decision = advance_route(
        _registry(),
        RouteRequirements(),
        current_route_id="cheap",
        attempted_route_ids=("cheap",),
        failure_class=FailureClass.EVIDENCE_MISSING,
    )

    assert decision.action is EscalationAction.RETRY_SAME_ROUTE
    assert decision.selected is not None
    assert decision.selected.route_id == "cheap"
    assert decision.reason is EscalationReason.RETRYABLE_FAILURE


def test_scope_drift_requests_redispatch_without_authorizing_a_route() -> None:
    decision = advance_route(
        _registry(),
        RouteRequirements(),
        current_route_id="cheap",
        attempted_route_ids=("cheap",),
        failure_class=FailureClass.SCOPE_CREEP,
    )

    assert decision.action is EscalationAction.REDISPATCH
    assert decision.selected is None
    assert decision.reason is EscalationReason.REDISPATCH_REQUIRED


def test_hard_block_hands_off_even_when_routes_remain() -> None:
    decision = advance_route(
        _registry(),
        RouteRequirements(),
        current_route_id="cheap",
        attempted_route_ids=("cheap",),
        failure_class=FailureClass.BLOCKED,
    )

    assert decision.action is EscalationAction.BLOCKED
    assert decision.selected is None
    assert decision.remaining_route_ids == ("standard", "frontier")
    assert decision.reason is EscalationReason.HUMAN_HANDOFF_REQUIRED


@pytest.mark.parametrize(
    ("current_route_id", "attempted"),
    [
        ("standard", ("cheap",)),
        ("cheap", ("cheap", "cheap")),
        ("unknown", ("unknown",)),
    ],
)
def test_invalid_attempt_history_fails_closed(
    current_route_id: str, attempted: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        advance_route(
            _registry(),
            RouteRequirements(),
            current_route_id=current_route_id,
            attempted_route_ids=attempted,
            failure_class=FailureClass.STALL,
        )


def test_unknown_failure_class_is_rejected_before_policy_selection() -> None:
    with pytest.raises(ValueError, match="failure_class"):
        advance_route(
            _registry(),
            RouteRequirements(),
            current_route_id="cheap",
            attempted_route_ids=("cheap",),
            failure_class="UNKNOWN",  # type: ignore[arg-type]
        )


def test_unordered_attempt_history_is_rejected() -> None:
    with pytest.raises(ValueError, match="ordered sequence"):
        advance_route(
            _registry(),
            RouteRequirements(),
            current_route_id="cheap",
            attempted_route_ids={"cheap"},  # type: ignore[arg-type]
            failure_class=FailureClass.STALL,
        )
