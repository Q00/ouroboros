"""Deterministic Routing B route contract and Admission Kernel tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
from dataclasses import replace
import json
import pickle
import typing
import weakref

import pytest

import ouroboros.orchestrator.route_policy as route_policy
from ouroboros.orchestrator.route_policy import (
    MAX_ROUTE_CANDIDATES,
    MAX_ROUTE_CAPABILITIES,
    MAX_ROUTE_COST_UNITS,
    MAX_ROUTE_ORDINAL,
    RouteAdmission,
    RouteCandidate,
    RouteDecisionDisposition,
    RouteRegistry,
    RouteRejection,
    RouteRejectionCode,
    RouteRequirements,
    admit_route,
    validate_admission,
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
    malformed_string_decision = admit_route(
        registry,
        RouteRequirements(),
        advisor_order=("second", "x" * 161),
    )

    assert oversized_decision.selected is not None
    assert malformed_decision.selected is not None
    assert malformed_string_decision.selected is not None
    assert oversized_decision.selected.route_id == "first"
    assert malformed_decision.selected.route_id == "first"
    assert malformed_string_decision.selected.route_id == "first"


def test_advisor_callbacks_are_bounded_and_fail_closed() -> None:
    registry = _registry(
        _route("first", cost=5, ordinal=0),
        _route("second", cost=5, ordinal=1),
    )

    class RaisesDuringIteration(Sequence[str]):
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return "second"

        def __iter__(self):
            raise RuntimeError("broken advisor")

    class InfiniteAdvisor(Sequence[str]):
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return "second"

        def __iter__(self):
            while True:
                yield "second"

    for advisor in (RaisesDuringIteration(), InfiniteAdvisor()):
        decision = admit_route(
            registry,
            RouteRequirements(),
            advisor_order=advisor,  # type: ignore[arg-type]
        )
        assert decision.selected is not None
        assert decision.selected.route_id == "first"

    class LyingSequence(Sequence[str]):
        def __len__(self):
            raise RuntimeError("length is unavailable")

        def __getitem__(self, index):
            if index == 0:
                return "second"
            if index == 1:
                return "first"
            raise IndexError(index)

        def __iter__(self):
            yield "second"
            yield "first"

    decision = admit_route(
        registry,
        RouteRequirements(),
        advisor_order=LyingSequence(),  # type: ignore[arg-type]
    )
    assert decision.selected is not None
    assert decision.selected.route_id == "second"


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

    with pytest.raises(ValueError, match="exceeds its bound"):
        RouteRegistry.from_contract_data(oversized)


def test_contract_parser_bounds_lying_and_infinite_sequences_before_nested_parsing() -> None:
    route_data = _route("candidate", cost=1).to_contract_data()

    consumed_candidates = 0

    class InfiniteCandidates(Sequence[dict[str, object]]):
        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int) -> dict[str, object]:
            return route_data

        def __iter__(self):
            nonlocal consumed_candidates
            while True:
                consumed_candidates += 1
                yield route_data

    with pytest.raises(ValueError, match="exceeds its bound"):
        RouteRegistry.from_contract_data({"version": 1, "candidates": InfiniteCandidates()})
    assert consumed_candidates == MAX_ROUTE_CANDIDATES + 1

    consumed_capabilities = 0

    class InfiniteCapabilities(Sequence[str]):
        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int) -> str:
            return "read"

        def __iter__(self):
            nonlocal consumed_capabilities
            while True:
                consumed_capabilities += 1
                yield "read"

    route_data_with_capabilities = dict(route_data)
    route_data_with_capabilities["capabilities"] = InfiniteCapabilities()
    with pytest.raises(ValueError, match="exceeds its bound"):
        RouteCandidate.from_contract_data(route_data_with_capabilities)
    assert consumed_capabilities == MAX_ROUTE_CAPABILITIES + 1


def test_contract_parser_bounds_mapping_keys_before_nested_value_access() -> None:
    route_data = _route("candidate", cost=1).to_contract_data()
    route_keys = tuple(route_data)

    class InfiniteMapping(Mapping[str, object]):
        def __iter__(self):
            yield from route_keys
            index = 0
            while True:
                yield f"unexpected-{index}"
                index += 1

        def __len__(self) -> int:
            return 2

        def __getitem__(self, key: str) -> object:
            return route_data[key]

    with pytest.raises(ValueError, match="keys|shape"):
        RouteCandidate.from_contract_data(InfiniteMapping())

    class InfiniteRegistryMapping(Mapping[str, object]):
        def __iter__(self):
            yield "version"
            yield "candidates"
            index = 0
            while True:
                yield f"unexpected-{index}"
                index += 1

        def __len__(self) -> int:
            return 1

        def __getitem__(self, key: str) -> object:
            if key == "version":
                return 1
            if key == "candidates":
                return []
            return None

    with pytest.raises(ValueError, match="keys|shape"):
        RouteRegistry.from_contract_data(InfiniteRegistryMapping())


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
    with pytest.raises(ValueError, match="ordered"):
        RouteRejection("rejected", [RouteRejectionCode.DISABLED])  # type: ignore[arg-type]


def test_credential_shaped_authority_identity_is_rejected_before_serialization() -> None:
    credential_shapes = (
        "ghp_not-a-route-identity",
        "AIza" + "A" * 35,
        "AKIA" + "A" * 16,
        "ASIA" + "A" * 16,
        "github:ghp_namespaced-credential",
        "api_key:opaque-provider-credential",
        "access_token/opaque-provider-credential",
        "accessToken:opaqueProviderSecret",
        "clientSecret/opaqueProviderSecret",
        "api_key.opaqueProviderSecret",
        "access_token_opaque-provider-credential",
        "access-token-opaque-provider-credential",
        "credentials:opaquevalue",
        "credentials_opaquevalue",
        "passwd:opaquevalue",
        "glpat-opaque-provider-credential",
        "hf_opaque-provider-credential",
        "runtime:access_token-abc123",
        "runtime:api_key-abc123",
        "runtime:client_secret-abc123",
        "runtime:secretopaquevalue",
        "runtime:tokenopaquevalue",
        "runtime:credentialopaquevalue",
        *(
            f"runtime:auth{label}value"
            for label in (
                "bearer",
                "credential",
                "key",
                "opaque",
                "password",
                "secret",
                "token",
                "value",
            )
        ),
        "runtime:prod-ghp_abcdefghijklmnopqrstuvwxyz",
        "runtime:prod-sk_live_abcdefghijklmnopqrstuvwxyz",
        "runtime:prod-hvs.abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
        "SG." + "A" * 22 + "." + "B" * 43,
        "hvs." + "A" * 24,
        "runtime:" + "x" * 200,
        "runtime:dop_v1_" + "x" * 64,
        "runtime:opaqueprovider",
    )
    for identity in credential_shapes:
        with pytest.raises(ValueError, match="stable authority descriptor"):
            _route("credential", cost=1, authority_identity=identity)
        with pytest.raises(ValueError, match="stable authority descriptor"):
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
    with pytest.raises(TypeError, match="Admission Kernel"):
        RouteAdmission(
            disposition=RouteDecisionDisposition.ADMITTED,
            selected=object(),  # type: ignore[arg-type]
            eligible_route_ids=(),
            rejections=(),
            reason="test",
        )

    with pytest.raises(TypeError, match="Admission Kernel"):
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
        replace(  # type: ignore[type-var]
            decision,
            selected=other,
            eligible_route_ids=(other.route_id,),
        )

    with pytest.raises(AttributeError, match="immutable"):
        decision.selected = other  # type: ignore[misc]

    assert copy.copy(decision) is decision
    assert copy.deepcopy(decision) is decision
    assert decision.selected is original
    assert not hasattr(decision, "_kernel_token")
    assert not hasattr(RouteAdmission, "_from_kernel")
    with pytest.raises(TypeError, match="Admission Kernel"):
        RouteAdmission.__new__(RouteAdmission)  # type: ignore[call-overload]

    with pytest.raises(TypeError, match="pickled"):
        pickle.dumps(decision)


def test_object_protocol_cannot_forge_or_rewrite_admission_state() -> None:
    original = _route("original", cost=1)
    outside = _route("outside-registry", cost=2)
    decision = admit_route(_registry(original), RouteRequirements())

    forged: RouteAdmission = object.__new__(RouteAdmission)
    object.__setattr__(forged, "_sealed_state", object.__getattribute__(decision, "_sealed_state"))
    with pytest.raises(TypeError, match="unpublished"):
        _ = forged.admitted

    state = object.__getattribute__(decision, "_sealed_state")
    object.__setattr__(decision, "_sealed_state", state._replace(selected=outside))
    with pytest.raises(TypeError, match="unpublished"):
        _ = decision.selected

    for private_name in (
        "_AdmissionState",
        "_TRUSTED_ADMISSIONS",
        "_candidate_fingerprint",
        "_rejection_fingerprint",
    ):
        assert not hasattr(route_policy, private_name)


def test_effect_boundary_revalidates_admission_against_live_registry() -> None:
    original = _route("original", cost=1)
    registry = _registry(original)
    requirements = RouteRequirements()
    decision = admit_route(registry, requirements)

    assert validate_admission(registry, requirements, decision) is True

    forged: RouteAdmission = object.__new__(RouteAdmission)
    object.__setattr__(forged, "_sealed_state", object.__getattribute__(decision, "_sealed_state"))
    assert validate_admission(registry, requirements, forged) is False


def test_effect_boundary_rejects_same_id_route_semantic_drift() -> None:
    original = _route("same-id", cost=1, model="model-a")
    changed = _route("same-id", cost=99, model="model-b", authority_identity="session-b")
    requirements = RouteRequirements()
    stale_admission = admit_route(_registry(original), requirements)

    assert validate_admission(_registry(changed), requirements, stale_admission) is False


def test_effect_boundary_uses_plain_scalar_semantics_not_overloaded_equality() -> None:
    class AlwaysEqual(str):
        def strip(self) -> str:
            return self

        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            return str.__hash__(self)

    original = _route("same-id", cost=1, model=AlwaysEqual("old-model"))
    changed = _route("same-id", cost=1, model="new-model")
    requirements = RouteRequirements()
    stale_admission = admit_route(_registry(original), requirements)

    assert type(original.model) is str
    assert validate_admission(_registry(changed), requirements, stale_admission) is False


def test_route_numeric_fields_reject_overloaded_integer_subclasses() -> None:
    class ExplodingInt(int):
        def __eq__(self, other: object) -> bool:
            return True

        def __hash__(self) -> int:
            raise RuntimeError("hash")

    with pytest.raises(ValueError, match="integer"):
        _route("bad-cost", cost=ExplodingInt(1))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="integer"):
        _route("bad-ordinal", cost=1, ordinal=ExplodingInt(1))  # type: ignore[arg-type]


def test_effect_boundary_rejects_closure_registered_outside_route() -> None:
    original = _route("original", cost=1)
    outside = _route("outside-registry", cost=1)
    registry = _registry(original)
    requirements = RouteRequirements()

    cells = tuple(cell.cell_contents for cell in admit_route.__closure__ or ())
    state_type = next(item for item in cells if getattr(item, "__name__", "") == "AdmissionState")
    kernel_type = RouteAdmission
    trusted = next(item for item in cells if isinstance(item, dict))
    candidate_fingerprint = next(
        item for item in cells if getattr(item, "__name__", "") == "candidate_fingerprint"
    )
    state = state_type(
        RouteDecisionDisposition.ADMITTED,
        outside,
        candidate_fingerprint(outside),
        (outside.route_id,),
        (),
        (),
        "cheapest_eligible_route",
    )
    forged: RouteAdmission = object.__new__(kernel_type)
    object.__setattr__(forged, "_sealed_state", state)
    trusted[id(forged)] = (weakref.ref(forged), state)

    assert forged.admitted is True
    assert validate_admission(registry, requirements, forged) is False


def test_advisor_string_normalization_failure_falls_back_to_kernel_order() -> None:
    class ExplodingString(str):
        def strip(self) -> str:
            raise RuntimeError("malformed advisor token")

    class NonStringString(str):
        def strip(self) -> object:
            return object()

    registry = _registry(_route("cheap", cost=1), _route("expensive", cost=2))
    exploding = admit_route(
        registry,
        RouteRequirements(),
        advisor_order=(ExplodingString("expensive"),),
    )
    non_string = admit_route(
        registry,
        RouteRequirements(),
        advisor_order=(NonStringString("expensive"),),
    )

    for decision in (exploding, non_string):
        assert decision.admitted is True
        assert decision.selected is not None
        assert decision.selected.route_id == "cheap"


def test_advisor_hash_failure_falls_back_to_kernel_order() -> None:
    class HashExplodes(str):
        def strip(self) -> str:
            return self

        def __hash__(self) -> int:
            raise RuntimeError("malformed advisor token")

    registry = _registry(_route("cheap", cost=1), _route("expensive", cost=2))
    decision = admit_route(
        registry,
        RouteRequirements(),
        advisor_order=(HashExplodes("expensive"),),
    )

    assert decision.selected is not None
    assert decision.selected.route_id == "cheap"


def test_exported_admission_reflection_has_one_coherent_runtime_contract() -> None:
    assert admit_route.__name__ == "admit_route"
    assert admit_route.__qualname__ == "admit_route"
    assert typing.get_type_hints(admit_route)["return"] is RouteAdmission
    for method_name in ("__new__", "__copy__", "__deepcopy__", "_trusted_state"):
        typing.get_type_hints(getattr(RouteAdmission, method_name))


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
