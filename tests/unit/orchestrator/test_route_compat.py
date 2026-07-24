"""Compatibility projection tests for the Routing B live-router seam."""

from __future__ import annotations

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
        "cost_units": 10,
        "persona": "default",
        "tool_policy": "default",
        "authority_identity": "runtime:claude",
        "capabilities": ["model-override"],
        "enabled": True,
        "ordinal": 1,
    }


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
    assert (
        build_route_compat_projection(
            _economics(), model_router=huge_threshold_router, runtime_backend="claude"
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
    assert admitted_execute_model_kwargs(admitted, model_decision=decision) == {"model": "sonnet-x"}

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

    payload = projection.to_contract_data()
    payload["runtime_backend"] = "codex_cli"
    changed_backend = deserialize_route_compat_projection(payload)
    assert changed_backend is not None
    assert not validate_route_compat_projection(
        changed_backend,
        _economics(),
        model_router=_router(),
        runtime_backend="claude",
    )

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
    )

    oversized_tiers = projection.to_contract_data()
    oversized_tiers["tier_route_ids"] = [
        {"tier": "frugal", "route_id": "compat:claude:frugal"}
    ] * 129
    assert deserialize_route_compat_projection(oversized_tiers) is None


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
