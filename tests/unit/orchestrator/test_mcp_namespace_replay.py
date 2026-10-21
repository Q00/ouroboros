"""Replay actual MCP contracts prepared before shared family namespaces applied."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.config import get_default_config
from ouroboros.config.models import EconomicsConfig, ModelConfig, OuroborosConfig, TierConfig
from ouroboros.orchestrator import model_routing
from ouroboros.orchestrator.claude_worker_runtime import build_claude_worker_runtime
from ouroboros.orchestrator.codex_mcp_runtime import build_codex_mcp_worker_runtime
from ouroboros.orchestrator.model_routing import MODEL_TIER_LADDER
from ouroboros.orchestrator.route_compat import deserialize_route_compat_contract
from ouroboros.orchestrator.runner import (
    EXECUTION_CONTRACT_PROGRESS_KEY,
    OrchestratorError,
    OrchestratorRunner,
)
from tests.unit.orchestrator.test_runtime_native_routing_contract import (
    prepared_contract,
    runner,
    seed,
)


@pytest.fixture(autouse=True)
def isolate_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING", raising=False)
    monkeypatch.delenv("OUROBOROS_EXECUTION_MODEL", raising=False)
    monkeypatch.setattr("ouroboros.config.get_execution_model", lambda _backend=None: None)
    monkeypatch.setattr("ouroboros.config.get_agent_reasoning_effort", lambda: None)
    monkeypatch.setattr("ouroboros.config.load_config", get_default_config)


@pytest.fixture
def cli(tmp_path: Path) -> str:
    path = tmp_path / "native-cli"
    path.write_text("#!/bin/sh\necho native-cli 1.0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def runtime(backend: str, cli: str, cwd: Path) -> Any:
    factory = (
        build_codex_mcp_worker_runtime if backend == "codex_mcp" else build_claude_worker_runtime
    )
    return factory(cli_path=cli, cwd=cwd, model="constructor-model")


def config(backend: str, *, pinned: bool, shape: str) -> OuroborosConfig:
    family, vendor = ("codex", "openai") if backend == "codex_mcp" else ("claude", "anthropic")
    tiers = {}
    for tier in MODEL_TIER_LADDER:
        models = [ModelConfig(provider=family, model=f"family/{tier}")]
        if shape != "sparse" or tier != "frontier":
            models.append(ModelConfig(provider=vendor, model=f"vendor/{tier}"))
        if shape == "exact" and tier == "standard":
            models.append(ModelConfig(provider=backend, model=f"exact/{tier}"))
        tiers[tier] = TierConfig(cost_factor=1, models=models)
    default = get_default_config()
    return default.model_copy(
        update={
            "economics": EconomicsConfig(tiers=tiers),
            "models": default.models.model_copy(update={"pin": pinned}),
        }
    )


async def legacy_contract(
    backend: str,
    configured: OuroborosConfig,
    cli: str,
    cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sentinel: bool = False,
    historical: bool = False,
) -> dict[str, Any]:
    providers = model_routing.tier_model_providers
    vendor = "openai" if backend == "codex_mcp" else "anthropic"

    def old_providers(name: str) -> tuple[str, ...]:
        return (backend, vendor) if name == backend else providers(name)

    # Reproduce only the old resolver. Runner preparation, policy snapshots,
    # Route B serialization, and authority handling remain real.
    with monkeypatch.context() as old_release:
        old_release.setenv("OUROBOROS_MODEL_TIER_ROUTING", "1")
        if sentinel:
            old_release.setattr(
                model_routing, "normalize_tier_model", lambda _model, **_kw: "default"
            )
        elif historical:
            old_release.setattr(model_routing, "normalize_tier_model", lambda model, **_kw: model)
        with patch.object(model_routing, "tier_model_providers", side_effect=old_providers):
            return await prepared_contract(runner(runtime(backend, cli, cwd), configured))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["codex_mcp", "claude_mcp"])
@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("shape", ["full", "sparse", "exact"])
async def test_mcp_pre_family_contract_replays_without_resolving_new_family(
    backend: str,
    pinned: bool,
    shape: str,
    cli: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = config(backend, pinned=pinned, shape=shape)
    persisted = await legacy_contract(backend, configured, cli, tmp_path, monkeypatch)
    frozen = persisted["model_routing"]["resolved_models"]["tiers"]
    assert "family/standard" not in frozen.values()
    assert set(frozen) == set(MODEL_TIER_LADDER) - ({"frontier"} if shape == "sparse" else set())
    resumed = runner(runtime(backend, cli, tmp_path), configured)

    assert (
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted}, seed=seed()
        )
        is False
    )

    assert dict(resumed._model_router.tier_models) == frozen
    assert resumed._execution_contract["model_routing"] == persisted["model_routing"]
    recognized, projection = deserialize_route_compat_contract(
        persisted["model_routing"]["route_compat"]
    )
    assert recognized and projection is not None
    assert {tier: projection.candidate_for_tier(tier).model for tier in frozen} == frozen
    # Exercise restored Route B authority at the live call boundary as well.
    routed = await resumed._route_call_effort(
        execution_id="native-exec", session_id="native-session"
    )
    assert routed == ({"model": frozen["standard"]} if backend == "claude_mcp" else {})


def tamper_models(persisted: dict[str, Any], models: dict[str, str]) -> None:
    routing = persisted["model_routing"]
    routing["router"]["tier_models"] = dict(models)
    routing["resolved_models"]["tiers"] = dict(models)
    projection = routing["route_compat"]["projection"]
    tiers = {entry["route_id"]: entry["tier"] for entry in projection["tier_route_ids"]}
    for candidate in projection["registry"]["candidates"]:
        candidate["model"] = models[tiers[candidate["route_id"]]]
    persisted["frugality_proof"]["routing_fingerprint"] = OrchestratorRunner._routing_fingerprint(
        routing
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["codex_mcp", "claude_mcp"])
@pytest.mark.parametrize(
    "tamper", ["mixed_family", "swapped_tiers", "unconfigured", "lower_priority"]
)
async def test_mcp_pre_family_compatibility_rejects_consistent_whole_map_tampering(
    backend: str, tamper: str, cli: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = config(
        backend, pinned=True, shape="exact" if tamper == "lower_priority" else "full"
    )
    persisted = deepcopy(await legacy_contract(backend, configured, cli, tmp_path, monkeypatch))
    models = dict(persisted["model_routing"]["resolved_models"]["tiers"])
    if tamper == "mixed_family":
        models["standard"] = "family/standard"
    elif tamper == "swapped_tiers":
        models["frugal"], models["frontier"] = models["frontier"], models["frugal"]
    elif tamper == "lower_priority":
        models["standard"] = "vendor/standard"
    else:
        models["standard"] = "unconfigured/model"
    tamper_models(persisted, models)
    resumed = runner(runtime(backend, cli, tmp_path), configured)

    with pytest.raises(OrchestratorError, match="changed model-routing policy"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted}, seed=seed()
        )
    assert resumed._execution_contract is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [False, True])
async def test_mcp_legacy_sentinel_replays_only_its_original_sparse_shape(
    tamper: bool, cli: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = config("codex_mcp", pinned=False, shape="sparse")
    persisted = await legacy_contract(
        "codex_mcp", configured, cli, tmp_path, monkeypatch, sentinel=True
    )
    frozen = persisted["model_routing"]["resolved_models"]["tiers"]
    assert frozen == {"frugal": "default", "standard": "default"}
    if tamper:
        tamper_models(persisted, {"frugal": "default", "standard": "family/standard"})
    resumed = runner(runtime("codex_mcp", cli, tmp_path), configured)

    if tamper:
        with pytest.raises(OrchestratorError, match="changed model-routing policy"):
            resumed._restore_execution_contract(
                {EXECUTION_CONTRACT_PROGRESS_KEY: persisted}, seed=seed()
            )
        assert resumed._execution_contract is None
        return
    assert (
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted}, seed=seed()
        )
        is False
    )
    assert dict(resumed._model_router.tier_models) == frozen
    assert (
        await resumed._route_call_effort(execution_id="native-exec", session_id="native-session")
        == {}
    )


_LEGACY_CODEX_MODELS = {
    "frugal": "gpt-5.1-codex-mini",
    "standard": "gpt-5-codex",
    "frontier": "gpt-5.2",
}


def historical_codex_config(*, pinned: bool, shape: str) -> OuroborosConfig:
    configured = config("codex_mcp", pinned=pinned, shape=shape)
    tiers = {
        name: tier.model_copy(
            update={
                "models": [
                    entry.model_copy(update={"model": _LEGACY_CODEX_MODELS[name]})
                    if entry.provider == "openai"
                    else entry
                    for entry in tier.models
                ]
            }
        )
        for name, tier in configured.economics.tiers.items()
    }
    return configured.model_copy(update={"economics": EconomicsConfig(tiers=tiers)})


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("shape", ["full", "sparse"])
async def test_mcp_historical_pre_family_contract_replays_after_both_upgrades(
    pinned: bool, shape: str, cli: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = historical_codex_config(pinned=pinned, shape=shape)
    persisted = await legacy_contract(
        "codex_mcp", configured, cli, tmp_path, monkeypatch, historical=True
    )
    frozen = persisted["model_routing"]["resolved_models"]["tiers"]
    expected = {tier: model for tier, model in _LEGACY_CODEX_MODELS.items() if tier in frozen}
    assert frozen == expected
    resumed = runner(runtime("codex_mcp", cli, tmp_path), configured)

    assert (
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted}, seed=seed()
        )
        is False
    )
    assert dict(resumed._model_router.tier_models) == expected
    assert resumed._execution_contract["model_routing"] == persisted["model_routing"]
    assert (
        await resumed._route_call_effort(execution_id="native-exec", session_id="native-session")
        == {}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper", ["mixed_family", "swapped_tiers", "unconfigured", "wrong_provider_history"]
)
async def test_mcp_historical_legacy_candidate_rejects_consistent_map_tampering(
    tamper: str, cli: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = historical_codex_config(pinned=True, shape="full")
    persisted = await legacy_contract(
        "codex_mcp", configured, cli, tmp_path, monkeypatch, historical=True
    )
    models = dict(persisted["model_routing"]["resolved_models"]["tiers"])
    if tamper == "mixed_family":
        models["standard"] = "family/standard"
    elif tamper == "swapped_tiers":
        models["frugal"], models["frontier"] = models["frontier"], models["frugal"]
    elif tamper == "wrong_provider_history":
        models["standard"] = "claude-sonnet-4-20250514"
    else:
        models["standard"] = "unconfigured/model"
    tamper_models(persisted, models)
    resumed = runner(runtime("codex_mcp", cli, tmp_path), configured)

    with pytest.raises(OrchestratorError, match="changed model-routing policy"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted}, seed=seed()
        )
    assert resumed._execution_contract is None
