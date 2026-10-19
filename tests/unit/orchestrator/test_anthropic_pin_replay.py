"""A pin refresh changes new runs, never a validated frozen Anthropic run."""

from copy import deepcopy
from pathlib import Path

import pytest

from ouroboros.config import get_default_config
from ouroboros.config.models import ModelConfig
from ouroboros.orchestrator import model_routing
from ouroboros.orchestrator.claude_worker_runtime import build_claude_worker_runtime
from ouroboros.orchestrator.runner import EXECUTION_CONTRACT_PROGRESS_KEY, OrchestratorError
from tests.unit.orchestrator.test_mcp_namespace_replay import tamper_models
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
    path = tmp_path / "claude"
    path.write_text("#!/bin/sh\necho claude 1.0\n")
    path.chmod(0o755)
    return str(path)


HISTORY = [
    {"frugal": "claude-haiku-4-5", "standard": "claude-sonnet-4-6", "frontier": "claude-opus-5"},
    {
        "frugal": "claude-3-5-haiku",
        "standard": "claude-sonnet-4-20250514",
        "frontier": "claude-opus-4-6",
    },
    {"frugal": "claude-haiku-4-5", "standard": "claude-sonnet-4-6", "frontier": "claude-opus-4-8"},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", HISTORY)
@pytest.mark.parametrize("sparse", [False, True])
async def test_pre_refresh_contract_keeps_old_models_and_constructor(
    historical: dict[str, str],
    sparse: bool,
    cli: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = get_default_config()
    tiers = {
        name: tier.model_copy(
            update={"models": [ModelConfig(provider="anthropic", model=historical[name])]}
        )
        for name, tier in config.economics.tiers.items()
        if not sparse or name != "frontier"
    }
    config = config.model_copy(
        update={
            "economics": config.economics.model_copy(update={"tiers": tiers}),
            "models": config.models.model_copy(update={"pin": True}),
        }
    )
    # Real preparation from a release before migration was introduced, with
    # only historical default normalization replaced by its prior identity rule.
    old_runtime = build_claude_worker_runtime(
        cli_path=cli, cwd=tmp_path, model=historical["standard"]
    )
    with monkeypatch.context() as old_release:
        old_release.setattr(model_routing, "normalize_tier_model", lambda model, **_kw: model)
        persisted = await prepared_contract(runner(old_runtime, config))
    frozen = persisted["model_routing"]["resolved_models"]["tiers"]
    assert frozen == {name: historical[name] for name in tiers}
    current_runtime = build_claude_worker_runtime(cli_path=cli, cwd=tmp_path, model="sonnet")
    resumed = runner(current_runtime, config)

    assert (
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted}, seed=seed()
        )
        is False
    )
    assert dict(resumed._model_router.tier_models) == frozen
    assert current_runtime._model == historical["standard"]
    assert resumed._execution_contract["model_routing"] == persisted["model_routing"]
    assert await resumed._route_call_effort(
        execution_id="native-exec", session_id="native-session"
    ) == {"model": historical["standard"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["tier", "provider", "custom"])
async def test_refreshed_history_does_not_authorize_consistent_tampering(
    tamper: str, cli: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = get_default_config()
    old = HISTORY[0]
    config = config.model_copy(
        update={
            "economics": config.economics.model_copy(
                update={
                    "tiers": {
                        name: tier.model_copy(
                            update={"models": [ModelConfig(provider="anthropic", model=old[name])]}
                        )
                        for name, tier in config.economics.tiers.items()
                    }
                }
            ),
            "models": config.models.model_copy(update={"pin": True}),
        }
    )

    def factory():
        return build_claude_worker_runtime(cli_path=cli, cwd=tmp_path, model="sonnet")

    with monkeypatch.context() as old_release:
        old_release.setattr(model_routing, "normalize_tier_model", lambda model, **_kw: model)
        persisted = deepcopy(await prepared_contract(runner(factory(), config)))
    models = dict(persisted["model_routing"]["resolved_models"]["tiers"])
    if tamper == "tier":
        models["standard"] = old["frugal"]
    elif tamper == "provider":
        models["standard"] = "gpt-5-codex"
    else:
        models["standard"] = "claude-never-shipped"
    tamper_models(persisted, models)
    with pytest.raises(OrchestratorError, match="changed model-routing policy"):
        runner(factory(), config)._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted}, seed=seed()
        )
