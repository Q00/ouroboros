"""Deterministic Routing B route contract and Admission Kernel tests."""

from __future__ import annotations

import copy
from dataclasses import replace
import json

import pytest

from ouroboros.orchestrator.route_policy import (
    MAX_ROUTE_CANDIDATES,
    MAX_ROUTE_COST_UNITS,
    MAX_ROUTE_ORDINAL,
    RouteAdmission,
    RouteCandidate,
    RouteDecisionDisposition,
    RouteRegistry,
    RouteRejectionCode,
    RouteRequirements,
    admit_route,
)


def _route(
    route_id: str,
    *,
    cost: int,
    model: str = "model-a",
    harness: str = "harness-a",
    effort: str | None = "medium",
    persona: str = "default-persona",
    tool_policy: str = "default-tools",
    authority_identity: str = "authority-default",
    capabilities: tuple[str, ...] = (),
    enabled: bool = True,
    ordinal: int = 0,
) -> RouteCandidate:
    return RouteCandidate(
        route_id=route_id,
        model=model,
        harness=harness,
        effort=effort,
        cost_units=cost,
        persona=persona,
        tool_policy=tool_policy,
        authority_identity=authority_identity,
        capabilities=capabilities,
        enabled=enabled,
        ordinal=ordinal,
    )


def _registry(*routes: RouteCandidate) -> RouteRegistry:
    return RouteRegistry(candidates=routes)


def test_cheapest_eligible_route_wins_even_when_advisor_prefers_expensive_route() -> None:
    registry = _registry(_route("cheap", cost=1, ordinal=1), _route("expensive", cost=10))

    decision = admit_route(registry, RouteRequirements(), advisor_order=("expensive", "cheap"))

    assert decision.admitted is True
    assert decision.selected is not None
    assert decision.selected.route_id == "cheap"
    assert decision.eligible_route_ids == ("cheap", "expensive")


def test_advisor_order_breaks_only_equal_cost_ties() -> None:
    registry = _registry(
        _route("first", cost=5, ordinal=0),
        _route("second", cost=5, ordinal=1),
    )

    decision = admit_route(registry, RouteRequirements(), advisor_order=("second", "first"))

    assert decision.selected is not None
    assert decision.selected.route_id == "second"
    assert decision.eligible_route_ids == ("second", "first")


def test_unknown_and_repeated_advisor_ids_cannot_authorize_or_reorder_routes() -> None:
    registry = _registry(
        _route("cheap", cost=1, ordinal=0),
        _route("expensive", cost=10, ordinal=1),
    )

    decision = admit_route(
        registry,
        RouteRequirements(),
        advisor_order=("unknown", "expensive", "expensive", "unknown"),
    )

    assert decision.selected is not None
    assert decision.selected.route_id == "cheap"


def test_oversized_or_malformed_advisor_order_falls_back_to_kernel_order() -> None:
    registry = _registry(
        _route("first", cost=5, ordinal=0),
        _route("second", cost=5, ordinal=1),
    )

    oversized = ("second",) * 129
    oversized_decision = admit_route(registry, RouteRequirements(), advisor_order=oversized)
    malformed_decision = admit_route(
        registry,
        RouteRequirements(),
        advisor_order=(object(), "second"),  # type: ignore[arg-type]
    )

    assert oversized_decision.selected is not None
    assert malformed_decision.selected is not None
    assert oversized_decision.selected.route_id == "first"
    assert malformed_decision.selected.route_id == "first"


def test_required_capabilities_and_harness_allowlist_are_hard_constraints() -> None:
    registry = _registry(
        _route("cheap", cost=1, harness="unsafe", capabilities=("read",)),
        _route("eligible", cost=2, harness="safe", capabilities=("read", "write")),
    )

    decision = admit_route(
        registry,
        RouteRequirements(required_capabilities=("write",), allowed_harnesses=("safe",)),
    )

    assert decision.selected is not None
    assert decision.selected.route_id == "eligible"
    assert decision.rejections[0].reasons == (
        RouteRejectionCode.HARNESS_NOT_ALLOWED,
        RouteRejectionCode.MISSING_CAPABILITIES,
    )


def test_pins_are_hard_constraints() -> None:
    registry = _registry(
        _route("model-a-safe", cost=1, model="model-a", harness="safe"),
        _route("model-b-safe", cost=2, model="model-b", harness="safe"),
        _route("model-b-other", cost=3, model="model-b", harness="other"),
    )

    decision = admit_route(
        registry,
        RouteRequirements(pinned_model="model-b", pinned_harness="safe"),
    )

    assert decision.selected is not None
    assert decision.selected.route_id == "model-b-safe"
    assert RouteRejectionCode.MODEL_PIN_MISMATCH in decision.rejections[0].reasons
    assert RouteRejectionCode.HARNESS_PIN_MISMATCH in decision.rejections[1].reasons


def test_persona_tool_policy_and_authority_pins_are_hard_constraints() -> None:
    registry = _registry(
        _route(
            "wrong-context",
            cost=1,
            persona="researcher",
            tool_policy="read-only",
            authority_identity="session-a",
        ),
        _route(
            "eligible",
            cost=2,
            persona="builder",
            tool_policy="workspace-write",
            authority_identity="session-a",
        ),
    )

    decision = admit_route(
        registry,
        RouteRequirements(
            pinned_persona="builder",
            pinned_tool_policy="workspace-write",
            pinned_authority_identity="session-a",
        ),
    )

    assert decision.selected is not None
    assert decision.selected.route_id == "eligible"
    assert decision.rejections[0].reasons == (
        RouteRejectionCode.PERSONA_PIN_MISMATCH,
        RouteRejectionCode.TOOL_POLICY_PIN_MISMATCH,
    )


def test_disabled_and_effort_mismatch_routes_are_rejected() -> None:
    registry = _registry(
        _route("disabled", cost=1, enabled=False),
        _route("wrong-effort", cost=2, effort="low"),
        _route("eligible", cost=3),
    )

    decision = admit_route(registry, RouteRequirements(required_effort="medium"))

    assert decision.selected is not None
    assert decision.selected.route_id == "eligible"
    assert decision.rejections[0].reasons == (RouteRejectionCode.DISABLED,)
    assert decision.rejections[1].reasons == (RouteRejectionCode.EFFORT_MISMATCH,)


def test_no_eligible_route_is_blocked_without_a_selected_route() -> None:
    registry = _registry(_route("only", cost=1, enabled=False))

    decision = admit_route(registry, RouteRequirements())

    assert decision.disposition is RouteDecisionDisposition.BLOCKED
    assert decision.selected is None
    assert decision.eligible_route_ids == ()
    assert decision.reason == "no_eligible_route"


def test_registry_rejects_duplicate_ids_and_empty_candidates() -> None:
    duplicate = _route("same", cost=1)

    with pytest.raises(ValueError, match="unique"):
        RouteRegistry(candidates=(duplicate, duplicate))
    with pytest.raises(ValueError, match="at least one"):
        RouteRegistry(candidates=())
    with pytest.raises(ValueError, match="exceeds its bound"):
        RouteRegistry(
            candidates=tuple(
                _route(f"route-{index}", cost=index) for index in range(MAX_ROUTE_CANDIDATES + 1)
            )
        )


def test_contract_rejects_oversized_registry_before_nested_candidate_parsing() -> None:
    oversized = {"version": 1, "candidates": [{}] * (MAX_ROUTE_CANDIDATES + 1)}

    with pytest.raises(ValueError, match="bounded candidate"):
        RouteRegistry.from_contract_data(oversized)


def test_unordered_collections_are_rejected_at_the_contract_boundary() -> None:
    with pytest.raises(ValueError, match="ordered"):
        RouteCandidate(
            route_id="set-capabilities",
            model="model",
            harness="harness",
            effort="medium",
            cost_units=1,
            persona="persona",
            tool_policy="tools",
            authority_identity="authority",
            capabilities={"read"},  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="ordered"):
        RouteRequirements(required_capabilities={"read"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ordered"):
        RouteRegistry(candidates={_route("unordered", cost=1)})  # type: ignore[arg-type]


def test_credential_shaped_authority_identity_is_rejected_before_serialization() -> None:
    credential_shapes = (
        "ghp_not-a-route-identity",
        "AIza" + "A" * 35,
        "AKIA" + "A" * 16,
        "ASIA" + "A" * 16,
        "github:ghp_namespaced-credential",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
    )
    for identity in credential_shapes:
        with pytest.raises(ValueError, match="credential-shaped"):
            _route("credential", cost=1, authority_identity=identity)
        with pytest.raises(ValueError, match="credential-shaped"):
            RouteRequirements(pinned_authority_identity=identity)


def test_numeric_contract_fields_are_bounded_and_json_serializable() -> None:
    registry = _registry(_route("json-safe", cost=MAX_ROUTE_COST_UNITS, ordinal=MAX_ROUTE_ORDINAL))
    encoded = registry.to_contract_data()

    assert json.loads(json.dumps(encoded, sort_keys=True)) == encoded

    with pytest.raises(ValueError, match="cost_units exceeds"):
        _route("huge-cost", cost=10**5000)
    with pytest.raises(ValueError, match="ordinal exceeds"):
        _route("huge-ordinal", cost=1, ordinal=10**5000)


def test_admission_rejects_non_route_selected_values() -> None:
    with pytest.raises(ValueError, match="RouteCandidate"):
        RouteAdmission(
            disposition=RouteDecisionDisposition.ADMITTED,
            selected=object(),  # type: ignore[arg-type]
            eligible_route_ids=(),
            rejections=(),
            reason="test",
        )

    with pytest.raises(ValueError, match="Admission Kernel"):
        RouteAdmission(
            disposition=RouteDecisionDisposition.ADMITTED,
            selected=_route("fabricated", cost=1),
            eligible_route_ids=("fabricated",),
            rejections=(),
            reason="fabricated",
        )


def test_admission_cannot_be_dataclass_replaced_or_mutated() -> None:
    original = _route("original", cost=1)
    other = _route("outside-registry", cost=2)
    decision = admit_route(_registry(original), RouteRequirements())

    with pytest.raises(TypeError, match="dataclass"):
        replace(
            decision,
            selected=other,
            eligible_route_ids=(other.route_id,),
        )

    with pytest.raises(AttributeError, match="immutable"):
        decision.selected = other  # type: ignore[misc]


def test_streaming_capability_input_stops_at_the_bound() -> None:
    consumed = 0

    def capabilities():
        nonlocal consumed
        while True:
            consumed += 1
            yield f"cap-{consumed}"

    with pytest.raises(ValueError, match="exceeds its bound"):
        RouteRequirements(required_capabilities=capabilities())  # type: ignore[arg-type]

    assert consumed == 33


def test_contract_round_trip_is_stable_and_rejects_unknown_shapes() -> None:
    registry = _registry(
        _route("cheap", cost=1, capabilities=("read", "write"), ordinal=3),
        _route("other", cost=2, effort=None, enabled=False, ordinal=4),
    )

    encoded = registry.to_contract_data()
    decoded = RouteRegistry.from_contract_data(copy.deepcopy(encoded))

    assert decoded == registry
    assert decoded.to_contract_data() == encoded

    unknown_field = copy.deepcopy(encoded)
    unknown_field["unexpected"] = True
    with pytest.raises(ValueError, match="unsupported shape"):
        RouteRegistry.from_contract_data(unknown_field)

    bad_version = copy.deepcopy(encoded)
    bad_version["version"] = 99
    with pytest.raises(ValueError, match="version"):
        RouteRegistry.from_contract_data(bad_version)


def test_repeated_admission_is_deterministic() -> None:
    registry = _registry(
        _route("z", cost=4, ordinal=2),
        _route("a", cost=4, ordinal=2),
        _route("cheap", cost=1, ordinal=9),
    )
    requirements = RouteRequirements(required_capabilities=())

    first = admit_route(registry, requirements, advisor_order=("z", "a"))
    second = admit_route(registry, requirements, advisor_order=("z", "a"))

    assert first == second
    assert first.to_contract_data() == second.to_contract_data()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("route_id", "not valid"),
        ("model", ""),
        ("harness", 12),
        ("effort", "not valid"),
        ("cost_units", -1),
    ],
)
def test_route_candidate_rejects_malformed_contract_values(field: str, value: object) -> None:
    values: dict[str, object] = {
        "route_id": "valid",
        "model": "model",
        "harness": "harness",
        "effort": "medium",
        "cost_units": 1,
        "persona": "persona",
        "tool_policy": "tools",
        "authority_identity": "authority",
        "capabilities": (),
        "enabled": True,
        "ordinal": 0,
    }
    values[field] = value

    with pytest.raises(ValueError):
        RouteCandidate(**values)  # type: ignore[arg-type]
