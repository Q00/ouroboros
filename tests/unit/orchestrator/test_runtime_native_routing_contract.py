"""Existing-runtime tier routing preserves durable Codex model selections.

Real runner preparation and restore validate historical model maps, Route B
snapshots, advisory sentinels, and the explicit Codex routing opt-in. No provider
process or paid model call runs in these tests.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.config import get_default_config
from ouroboros.config.models import ModelConfig, OuroborosConfig
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.model_routing import MODEL_TIER_LADDER
from ouroboros.orchestrator.route_compat import deserialize_route_compat_contract
from ouroboros.orchestrator.runner import (
    EXECUTION_CONTRACT_PROGRESS_KEY,
    OrchestratorError,
    OrchestratorRunner,
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


def seed() -> Seed:
    return Seed(
        goal="Route native runtime tasks without changing a resumed run",
        acceptance_criteria=("Native model selection survives resume",),
        ontology_schema=OntologySchema(name="NativeRouting", description="Native model contract"),
        metadata=SeedMetadata(seed_id="native-routing-seed"),
    )


def runner(runtime: Any, config: OuroborosConfig) -> OrchestratorRunner:
    store = AsyncMock()
    store.replay.return_value = []
    store.query_execution_related_events.return_value = []
    with patch("ouroboros.config.load_config", return_value=config):
        return OrchestratorRunner(runtime, store, MagicMock(), task_cwd=runtime.working_directory)


async def prepared_contract(instance: OrchestratorRunner) -> dict[str, Any]:
    result = await instance.prepare_session(
        seed(), execution_id="native-exec", session_id="native-session"
    )
    try:
        assert result.is_ok, result.error if result.is_err else ""
        assert instance._execution_contract is not None
        persisted = deepcopy(instance._execution_contract)
        started = next(
            call.args[0]
            for call in instance._event_store.append.await_args_list
            if call.args[0].type == "orchestrator.session.started"
        )
        assert started.data["execution_contract"] == persisted
        return persisted
    finally:
        instance._retire_process_local_authority(
            session_id="native-session", execution_id="native-exec"
        )


def with_runtime_models(
    config: OuroborosConfig, provider: str, models: dict[str, str]
) -> OuroborosConfig:
    tiers = {
        tier: value.model_copy(
            update={
                "models": [entry for entry in value.models if entry.provider != provider]
                + [ModelConfig(provider=provider, model=models[tier])]
            }
        )
        for tier, value in config.economics.tiers.items()
    }
    return config.model_copy(
        update={"economics": config.economics.model_copy(update={"tiers": tiers})}
    )


@pytest.mark.asyncio
async def test_old_sentinel_contract_resumes_as_advisory(
    monkeypatch: pytest.MonkeyPatch, cli: str, tmp_path: Path
) -> None:
    config = get_default_config()
    sentinel_config = with_runtime_models(
        config, "openai", dict.fromkeys(MODEL_TIER_LADDER, "default")
    )
    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "1")
    old = runner(CodexCliRuntime(cli_path=cli, cwd=tmp_path), sentinel_config)
    persisted = await prepared_contract(old)
    assert set(persisted["model_routing"]["resolved_models"]["tiers"].values()) == {"default"}
    monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING")
    resumed = runner(CodexCliRuntime(cli_path=cli, cwd=tmp_path), config)

    changed = resumed._restore_execution_contract(
        {EXECUTION_CONTRACT_PROGRESS_KEY: persisted}, seed=seed()
    )

    assert changed is False
    assert set(resumed._model_router.tier_models.values()) == {"default"}
    assert (
        await resumed._route_call_effort(execution_id="native-exec", session_id="native-session")
        == {}
    )
    events = [
        call.args[0]
        for call in resumed._event_store.append.await_args_list
        if call.args[0].type == "execution.ac.model_routed"
    ]
    assert len(events) == 1 and events[0].data["model_mode"] == "advised"
    assert (
        resumed._execution_contract["model_routing"]["route_compat"]
        == persisted["model_routing"]["route_compat"]
    )


@pytest.mark.asyncio
async def test_codex_default_is_dormant_but_explicit_routing_has_distinct_tiers(
    monkeypatch: pytest.MonkeyPatch, cli: str, tmp_path: Path
) -> None:
    config = get_default_config()
    automatic = runner(CodexCliRuntime(cli_path=cli, cwd=tmp_path), config)
    dormant = await prepared_contract(automatic)
    assert automatic._model_router is None
    assert dormant["model_routing"]["enabled"] is False
    assert (
        await automatic._route_call_effort(execution_id="native-exec", session_id="native-session")
        == {}
    )

    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "1")
    explicit = runner(CodexCliRuntime(cli_path=cli, cwd=tmp_path), config)
    enabled = await prepared_contract(explicit)
    assert enabled["model_routing"]["enabled"] is True
    assert dict(explicit._model_router.tier_models) == {
        "frugal": "gpt-6-luna",
        "standard": "gpt-6.1-sol",
        "frontier": "gpt-6-astra",
    }
    assert await explicit._route_call_effort(
        execution_id="native-exec", session_id="native-session"
    ) == {"model": explicit._model_router.tier_models["standard"]}


_LEGACY_CODEX_MODELS = {
    "frugal": "gpt-5.1-codex-mini",
    "standard": "gpt-5-codex",
    "frontier": "gpt-5.2",
}


async def legacy_concrete_contract(
    monkeypatch: pytest.MonkeyPatch, cli: str, root: Path
) -> dict[str, Any]:
    """Prepare the actual old concrete ladder before the normalization bump.

    Disabling only the new historical-id normalization reproduces the shipped
    pre-upgrade resolution; all runner preparation, serialization, authority,
    seed and workspace contracts still execute normally.
    """
    legacy_config = with_runtime_models(get_default_config(), "openai", _LEGACY_CODEX_MODELS)
    with monkeypatch.context() as old_release:
        old_release.setenv("OUROBOROS_MODEL_TIER_ROUTING", "1")
        old_release.setattr(
            "ouroboros.orchestrator.model_routing.normalize_tier_model",
            lambda model, **_kwargs: model,
        )
        original = runner(CodexCliRuntime(cli_path=cli, cwd=root), legacy_config)
        return await prepared_contract(original)


@pytest.mark.asyncio
async def test_concrete_codex_models_remain_frozen_across_default_upgrade(
    monkeypatch: pytest.MonkeyPatch, cli: str, tmp_path: Path
) -> None:
    persisted = await legacy_concrete_contract(monkeypatch, cli, tmp_path)
    routing = persisted["model_routing"]
    assert routing["router"]["tier_models"] == _LEGACY_CODEX_MODELS
    assert routing["resolved_models"]["tiers"] == _LEGACY_CODEX_MODELS
    resumed = runner(CodexCliRuntime(cli_path=cli, cwd=tmp_path), get_default_config())

    assert (
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted}, seed=seed()
        )
        is False
    )

    assert dict(resumed._model_router.tier_models) == _LEGACY_CODEX_MODELS
    assert resumed._execution_contract["model_routing"] == routing
    recognized, projection = deserialize_route_compat_contract(routing["route_compat"])
    assert recognized and projection is not None
    assert {
        tier: projection.candidate_for_tier(tier).model for tier in MODEL_TIER_LADDER
    } == _LEGACY_CODEX_MODELS
    # This enters the live Route B effect-boundary validation against the
    # restored economics, rather than merely checking copied JSON fields.
    assert await resumed._route_call_effort(
        execution_id="native-exec", session_id="native-session"
    ) == {"model": "gpt-5-codex"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["swap_tiers", "never_configured"])
async def test_concrete_codex_replay_rejects_consistent_but_unauthorized_models(
    tamper: str, monkeypatch: pytest.MonkeyPatch, cli: str, tmp_path: Path
) -> None:
    persisted = await legacy_concrete_contract(monkeypatch, cli, tmp_path)
    models = dict(_LEGACY_CODEX_MODELS)
    if tamper == "swap_tiers":
        models["frugal"], models["frontier"] = models["frontier"], models["frugal"]
    else:
        models["standard"] = "proxy/never-configured-coder"
    routing = persisted["model_routing"]
    routing["router"]["tier_models"] = dict(models)
    routing["resolved_models"]["tiers"] = dict(models)
    projection = routing["route_compat"]["projection"]
    tier_for_route = {entry["route_id"]: entry["tier"] for entry in projection["tier_route_ids"]}
    for candidate in projection["registry"]["candidates"]:
        candidate["model"] = models[tier_for_route[candidate["route_id"]]]
    # Keep the serialized fields and their checksum mutually consistent. The
    # refusal must come from provider/tier authorization, not a stale checksum.
    persisted["frugality_proof"]["routing_fingerprint"] = OrchestratorRunner._routing_fingerprint(
        routing
    )
    resumed = runner(CodexCliRuntime(cli_path=cli, cwd=tmp_path), get_default_config())

    with pytest.raises(OrchestratorError, match="changed model-routing policy"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted}, seed=seed()
        )
    assert resumed._execution_contract is None
