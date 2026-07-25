"""Compatibility projection tests for the Routing B live-router seam."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.config.models import EconomicsConfig, ModelConfig, TierConfig
from ouroboros.orchestrator.adapter import AgentMessage, ParamSupport, RuntimeHandle
from ouroboros.orchestrator.model_routing import (
    MODEL_MODE_ADVISED,
    MODEL_MODE_ENFORCED,
    ModelDecision,
    ModelRouter,
    build_model_router,
)
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome
from ouroboros.orchestrator.route_compat import (
    admit_compat_route,
    admitted_execute_model_kwargs,
    build_route_compat_projection,
    deserialize_route_compat_contract,
    deserialize_route_compat_projection,
    serialize_route_compat_contract,
    validate_route_compat_projection,
)
from ouroboros.orchestrator.route_policy import RouteDecisionDisposition


def _economics() -> EconomicsConfig:
    return EconomicsConfig(
        default_tier="frugal",
        escalation_threshold=2,
        tiers={
            "frugal": TierConfig(
                cost_factor=1,
                models=[ModelConfig(provider="anthropic", model="haiku-x")],
            ),
            "standard": TierConfig(
                cost_factor=10,
                models=[ModelConfig(provider="anthropic", model="sonnet-x")],
            ),
            "frontier": TierConfig(
                cost_factor=30,
                models=[ModelConfig(provider="anthropic", model="opus-x")],
            ),
        },
    )


def _provider_economics(provider: str) -> EconomicsConfig:
    return EconomicsConfig(
        default_tier="frugal",
        escalation_threshold=2,
        tiers={
            tier: TierConfig(
                cost_factor=index + 1,
                models=[ModelConfig(provider=provider, model=f"{tier}-model")],
            )
            for index, tier in enumerate(("frugal", "standard", "frontier"))
        },
    )


def _router(models: dict[str, str] | None = None) -> ModelRouter:
    tier_models = models or {
        "frugal": "haiku-x",
        "standard": "sonnet-x",
        "frontier": "opus-x",
    }
    return ModelRouter(
        tier_models=tier_models,
        runtime_backend="claude",
        child_tier="frugal",
        base_tier="standard",
        escalation_retry_threshold=2,
    )


def _projection(*, effort: str | None = "medium"):
    result = build_route_compat_projection(
        _economics(),
        model_router=_router(),
        runtime_backend="claude",
        effort=effort,
        capabilities=("model-override",),
    )
    assert result is not None
    return result


def test_projection_snapshots_configured_models_and_costs() -> None:
    projection = _projection()

    assert projection.runtime_backend == "claude"
    assert projection.route_id_for_tier("standard") == "compat:claude:standard"
    assert projection.candidate_for_tier("standard").to_contract_data() == {
        "route_id": "compat:claude:standard",
        "model": "sonnet-x",
        "harness": "claude",
        "effort": "medium",
        "cost_units": "10",
        "persona": "default",
        "tool_policy": "default",
        "authority_identity": "runtime:claude",
        "capabilities": ["model-override"],
        "enabled": True,
        "ordinal": 1,
    }


@pytest.mark.parametrize(
    ("runtime_backend", "provider"),
    (
        ("claude", "anthropic"),
        ("claude_code", "anthropic"),
        ("claude_mcp", "anthropic"),
        ("codex_cli", "openai"),
        ("codex_mcp", "openai"),
        ("gemini_cli", "google"),
    ),
)
def test_projection_supports_every_model_routing_backend(
    runtime_backend: str,
    provider: str,
) -> None:
    router = ModelRouter(
        tier_models={tier: f"{tier}-model" for tier in ("frugal", "standard", "frontier")},
        runtime_backend=runtime_backend,
        child_tier="frugal",
        base_tier="standard",
        escalation_retry_threshold=2,
    )

    projection = build_route_compat_projection(
        _provider_economics(provider),
        model_router=router,
        runtime_backend=runtime_backend,
    )

    assert projection is not None
    assert projection.authority_identity == f"runtime:{runtime_backend}"


def test_projection_rejects_router_catalog_or_backend_drift() -> None:
    assert (
        build_route_compat_projection(
            _economics(),
            model_router=_router({"frugal": "unconfigured-model"}),
            runtime_backend="claude",
        )
        is None
    )
    assert (
        build_route_compat_projection(
            _economics(), model_router=_router(), runtime_backend="codex_cli"
        )
        is None
    )


def test_projection_rejects_router_escalation_drift_from_economics() -> None:
    assert (
        build_route_compat_projection(
            _economics(),
            model_router=replace(_router(), escalation_retry_threshold=999),
            runtime_backend="claude",
        )
        is None
    )


def test_projection_bounds_hostile_capability_iterables() -> None:
    def infinite():
        while True:
            yield "model-override"

    assert (
        build_route_compat_projection(
            _economics(),
            model_router=_router(),
            runtime_backend="claude",
            capabilities=infinite(),
        )
        is None
    )

    projection = _projection()
    blocked = admit_compat_route(
        projection,
        model_decision=ModelDecision("standard", "sonnet-x", MODEL_MODE_ENFORCED),
        effort="medium",
        required_capabilities=infinite(),
    )
    assert blocked.disposition is RouteDecisionDisposition.BLOCKED

    sentinel_projection = build_route_compat_projection(
        _economics(),
        model_router=_router(),
        runtime_backend="claude",
        effort="medium",
        capabilities=("compat:invalid-capability",),
    )
    assert sentinel_projection is not None
    sentinel_collision = admit_compat_route(
        sentinel_projection,
        model_decision=ModelDecision("standard", "sonnet-x", MODEL_MODE_ENFORCED),
        effort="medium",
        required_capabilities=infinite(),
    )
    assert sentinel_collision.disposition is RouteDecisionDisposition.BLOCKED


def test_persisted_mapping_keys_are_checked_with_a_finite_budget() -> None:
    projection_payload = _projection().to_contract_data()

    class InfiniteMapping(Mapping[str, object]):
        def __iter__(self):
            yield from projection_payload
            index = 0
            while True:
                yield f"unexpected-{index}"
                index += 1

        def __len__(self) -> int:
            return 1

        def __getitem__(self, key: str) -> object:
            return projection_payload[key]

    assert deserialize_route_compat_projection(InfiniteMapping()) is None

    contract_payload = serialize_route_compat_contract(_projection())
    assert deserialize_route_compat_contract(InfiniteMapping()) == (False, None)

    class InfiniteContractMapping(InfiniteMapping):
        def __getitem__(self, key: str) -> object:
            return contract_payload[key]

        def __iter__(self):
            yield from contract_payload
            index = 0
            while True:
                yield f"unexpected-contract-{index}"
                index += 1

    assert deserialize_route_compat_contract(InfiniteContractMapping()) == (False, None)


def test_projection_rejects_unordered_capabilities_and_invalid_router_bounds() -> None:
    assert (
        build_route_compat_projection(
            _economics(),
            model_router=_router(),
            runtime_backend="claude",
            capabilities={"model-override"},
        )
        is None
    )
    invalid_router = ModelRouter(
        tier_models=_router().tier_models,
        runtime_backend="claude",
        child_tier="unknown",
        base_tier="standard",
        escalation_retry_threshold=2,
    )
    assert (
        build_route_compat_projection(
            _economics(), model_router=invalid_router, runtime_backend="claude"
        )
        is None
    )
    huge_threshold_router = ModelRouter(
        tier_models=_router().tier_models,
        runtime_backend="claude",
        child_tier="frugal",
        base_tier="standard",
        escalation_retry_threshold=10**9 + 1,
    )
    huge_threshold_projection = build_route_compat_projection(
        _economics().model_copy(update={"escalation_threshold": 10**9 + 1}),
        model_router=huge_threshold_router,
        runtime_backend="claude",
    )
    assert huge_threshold_projection is not None
    assert huge_threshold_projection.escalation_retry_threshold == 10**9 + 1

    class ExplodingInt(int):
        def __lt__(self, other: object) -> bool:
            raise RuntimeError("comparison")

    overloaded_threshold_router = ModelRouter(
        tier_models=_router().tier_models,
        runtime_backend="claude",
        child_tier="frugal",
        base_tier="standard",
        escalation_retry_threshold=ExplodingInt(2),  # type: ignore[arg-type]
    )
    assert (
        build_route_compat_projection(
            _economics(), model_router=overloaded_threshold_router, runtime_backend="claude"
        )
        is None
    )


def test_projection_does_not_turn_explicit_empty_authority_into_default() -> None:
    assert (
        build_route_compat_projection(
            _economics(),
            model_router=_router(),
            runtime_backend="claude",
            authority_identity="",
        )
        is None
    )


def test_projection_keeps_snapshot_after_router_mapping_mutation() -> None:
    router = _router()
    projection = build_route_compat_projection(
        _economics(), model_router=router, runtime_backend="claude"
    )
    assert projection is not None
    router.tier_models["standard"] = "attacker-model"  # type: ignore[index]

    assert projection.candidate_for_tier("standard").model == "sonnet-x"
    blocked = admit_compat_route(
        projection,
        model_decision=ModelDecision("standard", "attacker-model", MODEL_MODE_ENFORCED),
        effort=None,
    )
    assert blocked.disposition is RouteDecisionDisposition.BLOCKED


def test_projection_preserves_existing_model_and_cost_domains() -> None:
    model = "claude-sonnet@20250514"
    cost = 10**12
    economics = EconomicsConfig(
        default_tier="standard",
        escalation_threshold=10**12,
        tiers={
            "standard": TierConfig(
                cost_factor=cost,
                models=[ModelConfig(provider="anthropic", model=model)],
            )
        },
    )
    router = ModelRouter(
        tier_models={"standard": model},
        runtime_backend="claude",
        child_tier="frugal",
        base_tier="standard",
        escalation_retry_threshold=10**12,
    )

    projection = build_route_compat_projection(
        economics,
        model_router=router,
        runtime_backend="claude",
    )

    assert projection is not None
    assert projection.candidate_for_tier("standard").model == model
    assert projection.candidate_for_tier("standard").cost_units == cost
    assert projection.escalation_retry_threshold == 10**12


def test_projection_does_not_bound_materialized_source_model_list() -> None:
    target_model = "claude-sonnet@late-index"
    economics = EconomicsConfig(
        default_tier="standard",
        escalation_threshold=2,
        tiers={
            "standard": TierConfig(
                cost_factor=10,
                models=[
                    *(
                        ModelConfig(provider="openai", model=f"nonmatch-{index}")
                        for index in range(128)
                    ),
                    ModelConfig(provider="anthropic", model=target_model),
                ],
            )
        },
    )
    router = build_model_router(economics, runtime_backend="claude")
    assert router is not None
    assert router.tier_models["standard"] == target_model

    projection = build_route_compat_projection(
        economics,
        model_router=router,
        runtime_backend="claude",
    )

    assert projection is not None
    assert projection.candidate_for_tier("standard").model == target_model


def test_admission_pins_model_backend_effort_and_route_dimensions() -> None:
    projection = _projection()
    admitted = admit_compat_route(
        projection,
        model_decision=ModelDecision("standard", "sonnet-x", MODEL_MODE_ENFORCED),
        effort="medium",
    )

    assert admitted.admitted is True
    assert admitted.selected is not None
    assert admitted.selected.route_id == "compat:claude:standard"

    for decision in (
        ModelDecision("standard", "different-model", MODEL_MODE_ENFORCED),
        ModelDecision("frontier", "sonnet-x", MODEL_MODE_ENFORCED),
    ):
        rejected = admit_compat_route(projection, model_decision=decision, effort="medium")
        assert rejected.disposition is RouteDecisionDisposition.BLOCKED

    wrong_effort = admit_compat_route(
        projection,
        model_decision=ModelDecision("standard", "sonnet-x", MODEL_MODE_ENFORCED),
        effort="high",
    )
    assert wrong_effort.disposition is RouteDecisionDisposition.BLOCKED


def test_unresolved_or_missing_projection_is_a_kernel_block() -> None:
    blocked = admit_compat_route(
        None,
        model_decision=ModelDecision(None, None, "none"),
        effort=None,
    )
    assert blocked.disposition is RouteDecisionDisposition.BLOCKED
    assert blocked.admitted is False


def test_model_kwargs_require_admitted_enforced_route() -> None:
    projection = _projection(effort=None)
    decision = ModelDecision("standard", "sonnet-x", MODEL_MODE_ENFORCED)
    admitted = admit_compat_route(projection, model_decision=decision, effort=None)
    assert admitted_execute_model_kwargs(
        admitted,
        model_decision=decision,
        projection=projection,
        effort=None,
    ) == {"model": "sonnet-x"}

    advised = replace(decision, mode=MODEL_MODE_ADVISED)
    assert admitted_execute_model_kwargs(admitted, model_decision=advised) == {}
    assert (
        admitted_execute_model_kwargs(
            admit_compat_route(
                projection,
                model_decision=ModelDecision("standard", "tampered", MODEL_MODE_ENFORCED),
                effort=None,
            ),
            model_decision=decision,
        )
        == {}
    )


def test_model_kwargs_revalidate_admission_against_projection() -> None:
    projection = _projection(effort=None)
    decision = ModelDecision("standard", "sonnet-x", MODEL_MODE_ENFORCED)
    admission = admit_compat_route(projection, model_decision=decision, effort=None)

    assert admitted_execute_model_kwargs(
        admission,
        model_decision=decision,
        projection=projection,
        effort=None,
    ) == {"model": "sonnet-x"}

    changed = replace(
        projection,
        registry=replace(
            projection.registry,
            candidates=(
                projection.registry.candidates[0],
                replace(projection.registry.candidates[1], model="tampered-model"),
                *projection.registry.candidates[2:],
            ),
        ),
    )
    assert (
        admitted_execute_model_kwargs(
            admission,
            model_decision=decision,
            projection=changed,
            effort=None,
        )
        == {}
    )


def test_projection_contract_round_trip_and_tamper_rejection() -> None:
    projection = _projection()
    restored = deserialize_route_compat_projection(projection.to_contract_data())
    assert restored == projection
    recognized, restored_contract = deserialize_route_compat_contract(
        serialize_route_compat_contract(projection)
    )
    assert recognized is True
    assert restored_contract == projection
    assert deserialize_route_compat_contract(serialize_route_compat_contract(None)) == (
        True,
        None,
    )

    boolean_projection_version = projection.to_contract_data()
    boolean_projection_version["version"] = True
    assert deserialize_route_compat_projection(boolean_projection_version) is None

    boolean_contract_version = serialize_route_compat_contract(projection)
    boolean_contract_version["version"] = True
    assert deserialize_route_compat_contract(boolean_contract_version) == (False, None)

    payload = projection.to_contract_data()
    payload["runtime_backend"] = "codex_cli"
    changed_backend = deserialize_route_compat_projection(payload)
    assert changed_backend is None

    tampered = projection.to_contract_data()
    registry = tampered["registry"]
    assert isinstance(registry, dict)
    candidates = registry["candidates"]
    assert isinstance(candidates, list)
    candidates[0] = {**candidates[0], "cost_units": 999999}
    changed = deserialize_route_compat_projection(tampered)
    assert changed is not None
    # The parser preserves a syntactically valid payload; the caller must
    # compare it with a freshly built economics snapshot before dispatch.
    assert changed.registry.candidates[0].cost_units == 999999
    assert not validate_route_compat_projection(
        changed,
        _economics(),
        model_router=_router(),
        runtime_backend="claude",
        current_effort="medium",
    )


def test_current_projection_validation_does_not_trust_persisted_identity_metadata() -> None:
    projection = build_route_compat_projection(
        _economics(),
        model_router=_router(),
        runtime_backend="claude",
    )
    assert projection is not None
    changed_candidates = tuple(
        replace(
            candidate,
            effort="high",
            persona="worker",
            tool_policy="worker",
            authority_identity="runtime:claude:worker",
            capabilities=("tampered",),
        )
        for candidate in projection.registry.candidates
    )
    changed = replace(
        projection,
        registry=replace(projection.registry, candidates=changed_candidates),
        effort="high",
        persona="worker",
        tool_policy="worker",
        authority_identity="runtime:claude:worker",
    )

    assert validate_route_compat_projection(
        projection,
        _economics(),
        model_router=_router(),
        runtime_backend="claude",
    )
    assert not validate_route_compat_projection(
        changed,
        _economics(),
        model_router=_router(),
        runtime_backend="claude",
    )

    oversized_tiers = projection.to_contract_data()
    oversized_tiers["tier_route_ids"] = [
        {"tier": "frugal", "route_id": "compat:claude:frugal"}
    ] * 129
    assert deserialize_route_compat_projection(oversized_tiers) is None


def test_projection_parser_rejects_metadata_drift_before_dispatch() -> None:
    projection = _projection()
    payload = projection.to_contract_data()
    payload["persona"] = "different-persona"
    assert deserialize_route_compat_projection(payload) is None

    payload = projection.to_contract_data()
    payload["authority_identity"] = "runtime:claude:opaque"
    assert deserialize_route_compat_projection(payload) is None

    payload = projection.to_contract_data()
    tier_route_ids = payload["tier_route_ids"]
    assert isinstance(tier_route_ids, list)
    tier_route_ids[0] = {"tier": "frugal", "route_id": "compat:claude:unresolved"}
    assert deserialize_route_compat_projection(payload) is None

    payload = projection.to_contract_data()
    payload["unexpected"] = True
    assert deserialize_route_compat_projection(payload) is None

    contract = serialize_route_compat_contract(projection)
    contract["unexpected"] = True
    assert deserialize_route_compat_contract(contract) == (False, None)


class _CountingRuntime:
    runtime_backend = "claude"
    working_directory = "/tmp/project"
    permission_mode = "acceptEdits"

    def __init__(self) -> None:
        self.calls = 0

    @property
    def capabilities(self):
        from ouroboros.orchestrator.adapter import RuntimeCapabilities

        return RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            model_override_support=ParamSupport.NATIVE,
        )

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        model: str | None = None,
    ):
        self.calls += 1
        yield AgentMessage(type="result", content="[TASK_COMPLETE]", data={"subtype": "success"})


@pytest.mark.asyncio
async def test_parallel_executor_stops_before_provider_on_catalog_tamper() -> None:
    runtime = _CountingRuntime()
    router = _router()
    router.tier_models["standard"] = "tampered-model"  # type: ignore[index]
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        model_router=router,
        route_economics=_economics(),
    )

    result = await executor._execute_atomic_ac(
        ac_index=0,
        ac_content="Implement a thing",
        session_id="sess-route",
        tools=[],
        system_prompt="system",
        seed_goal="Ship it",
        depth=0,
        start_time=datetime.now(UTC),
        execution_id="exec-route",
    )

    assert result.outcome is ACExecutionOutcome.BLOCKED
    assert runtime.calls == 0
