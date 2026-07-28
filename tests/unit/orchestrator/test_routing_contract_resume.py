"""Durable model-routing and proof-cohort execution contracts."""

from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.config.models import EconomicsConfig, ModelConfig, TierConfig
from ouroboros.core.project_identity import project_id_for_root
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.core.worktree import TaskWorkspace
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.execution_handlers import (
    ExecuteSeedHandler,
    _resolve_model_tier_request,
)
from ouroboros.orchestrator.adapter import (
    ParamSupport,
    RuntimeCapabilities,
    RuntimeHandle,
)
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.goose_runtime import GooseCliRuntime
from ouroboros.orchestrator.model_routing import (
    ModelRouter,
    deserialize_model_router,
    serialize_model_router,
)
from ouroboros.orchestrator.runner import (
    EXECUTION_CONTRACT_PROGRESS_KEY,
    EXECUTION_CONTRACT_V9_TOP_LEVEL_KEYS,
    FRUGALITY_PROOF_PROTOCOL_VERSION,
    OrchestratorError,
    OrchestratorRunner,
    build_system_prompt,
)
from ouroboros.orchestrator.session import (
    SESSION_RUNTIME_IDENTITY_PROGRESS_KEY,
    SESSION_START_IDENTITY_PROGRESS_KEY,
    SessionRepository,
)


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(root: Path) -> None:
    _git("init", "-q", str(root))
    _git(
        "-c",
        "user.name=Routing Contract Test",
        "-c",
        "user.email=routing-contract@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "initial",
        cwd=root,
    )


def _adapter(
    cwd: str = "/tmp/project",
    *,
    constructor_model: str | None = "constructor-sonnet",
) -> MagicMock:
    adapter = MagicMock()
    adapter.runtime_backend = "claude"
    adapter.llm_backend = "anthropic"
    adapter.working_directory = cwd
    adapter.permission_mode = "acceptEdits"
    adapter._model = constructor_model
    return adapter


def _runner(
    *,
    cwd: str | None = None,
    constructor_model: str | None = "constructor-sonnet",
    **kwargs,
) -> OrchestratorRunner:
    task_workspace = kwargs.get("task_workspace")
    resolved_cwd = (
        cwd
        or (task_workspace.effective_cwd if isinstance(task_workspace, TaskWorkspace) else None)
        or "/tmp/project"
    )
    return OrchestratorRunner(
        _adapter(resolved_cwd, constructor_model=constructor_model),
        AsyncMock(),
        MagicMock(),
        **kwargs,
    )


def _frontier_custom_router(*, base_tier: str = "frontier") -> ModelRouter:
    return ModelRouter(
        tier_models={
            "frugal": "custom-haiku",
            "standard": "custom-sonnet",
            "frontier": "custom-opus",
        },
        runtime_backend="claude",
        child_tier="frugal",
        base_tier=base_tier,
        escalation_retry_threshold=7,
    )


def _frontier_custom_economics() -> EconomicsConfig:
    return EconomicsConfig(
        default_tier="frugal",
        escalation_threshold=7,
        tiers={
            "frugal": TierConfig(
                cost_factor=1,
                models=[ModelConfig(provider="anthropic", model="custom-haiku")],
            ),
            "standard": TierConfig(
                cost_factor=10,
                models=[ModelConfig(provider="anthropic", model="custom-sonnet")],
            ),
            "frontier": TierConfig(
                cost_factor=30,
                models=[ModelConfig(provider="anthropic", model="custom-opus")],
            ),
        },
    )


def _use_frontier_custom_routing(runner: OrchestratorRunner) -> None:
    runner._route_economics = _frontier_custom_economics()
    runner._model_router = _frontier_custom_router()
    runner._requested_model_tier = "frontier"


def _seed(*, goal: str = "Prove durable routing", criterion: str = "Routing survives") -> Seed:
    return Seed(
        goal=goal,
        acceptance_criteria=(criterion,),
        ontology_schema=OntologySchema(name="Routing", description="Durable routing contract"),
        metadata=SeedMetadata(seed_id="seed-routing-contract"),
    )


def _workspace(*, durable_id: str, worktree_path: Path, repo_root: Path) -> TaskWorkspace:
    (repo_root / "packages" / "app").mkdir(parents=True, exist_ok=True)
    (worktree_path / "packages" / "app").mkdir(parents=True, exist_ok=True)
    return TaskWorkspace(
        durable_id=durable_id,
        repo_root=str(repo_root),
        repo_name=repo_root.name,
        original_cwd=str(repo_root / "packages" / "app"),
        effective_cwd=str(worktree_path / "packages" / "app"),
        worktree_path=str(worktree_path),
        branch=f"ooo/{durable_id}",
        lock_path=str(worktree_path.parent / ".locks" / f"{durable_id}.json"),
    )


def _assert_process_local_runtime_contract(contract: dict[str, object]) -> None:
    """Legacy adapters never contribute a durable runtime execution identity."""
    routing = contract["model_routing"]
    assert isinstance(routing, dict)
    assert routing["runtime_execution"] == {"version": 1, "observed": False}
    authority = contract["foundation_a_authority"]
    assert isinstance(authority, dict)
    assert authority["version"] == 1
    assert authority["scope"] == "process_local"
    assert isinstance(authority["correlation_id"], str)


@pytest.fixture(autouse=True)
def _clear_model_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING", raising=False)
    monkeypatch.delenv("OUROBOROS_EXECUTION_MODEL", raising=False)


def test_router_contract_round_trips_custom_frontier_policy() -> None:
    router = _frontier_custom_router()

    recognized, restored = deserialize_model_router(serialize_model_router(router))

    assert recognized is True
    assert restored == router


def test_router_contract_preserves_exact_model_whitespace() -> None:
    router = _frontier_custom_router()
    router = ModelRouter(
        tier_models={**router.tier_models, "standard": " exact-model "},
        runtime_backend=router.runtime_backend,
        child_tier=router.child_tier,
        base_tier=router.base_tier,
        escalation_retry_threshold=router.escalation_retry_threshold,
    )

    recognized, restored = deserialize_model_router(serialize_model_router(router))

    assert recognized is True
    assert restored == router


def test_router_contract_distinguishes_disabled_from_malformed() -> None:
    recognized, disabled = deserialize_model_router(serialize_model_router(None))
    assert recognized is True
    assert disabled is None

    recognized, malformed = deserialize_model_router(
        {"version": 1, "enabled": True, "router": {"tier_models": {}}}
    )
    assert recognized is False
    assert malformed is None

    for invalid_version in (True, 1.0):
        recognized, malformed = deserialize_model_router(
            {"version": invalid_version, "enabled": False}
        )
        assert recognized is False
        assert malformed is None


@pytest.mark.parametrize(
    "router_payload",
    [
        {
            "tier_models": {"evil": "model-x"},
            "runtime_backend": "claude",
            "child_tier": "evil",
            "base_tier": "evil",
            "escalation_retry_threshold": 1,
        },
        {
            "tier_models": {"frugal": "model-x"},
            "runtime_backend": "unknown-backend",
            "child_tier": "frugal",
            "base_tier": "standard",
            "escalation_retry_threshold": 1,
        },
    ],
)
def test_router_contract_rejects_semantically_invalid_ladder(router_payload: dict) -> None:
    recognized, router = deserialize_model_router(
        {"version": 1, "enabled": True, "router": router_payload}
    )

    assert recognized is False
    assert router is None


def test_resume_restores_persisted_custom_frontier_router() -> None:
    original = _runner()
    _use_frontier_custom_routing(original)
    persisted = original._build_execution_contract()

    resumed = _runner()
    resumed._route_economics = _frontier_custom_economics()
    resumed._model_router = _frontier_custom_router()
    resumed._requested_model_tier = "frontier"

    changed = resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert changed is False
    assert resumed._model_router == _frontier_custom_router()


@pytest.mark.parametrize(
    "schema_mutation",
    [
        "version",
        "foundation_a_authority",
        "execution_preferences",
        "execution_semantics",
        "execution_inputs",
        "model_routing",
        "frugality_proof",
        "guidance",
        "resume",
        "unknown_top_level_key",
    ],
)
def test_v9_resume_rejects_every_inexact_top_level_shape_before_effects(
    schema_mutation: str,
) -> None:
    original = _runner()
    persisted = copy.deepcopy(original._build_execution_contract(seed=_seed()))
    assert frozenset(persisted) == EXECUTION_CONTRACT_V9_TOP_LEVEL_KEYS
    if schema_mutation == "unknown_top_level_key":
        persisted[schema_mutation] = "not part of v9"
        expected_missing: list[str] = []
        expected_unknown = [schema_mutation]
    else:
        del persisted[schema_mutation]
        expected_missing = [schema_mutation]
        expected_unknown = []

    resumed = _runner()
    provider = MagicMock(side_effect=AssertionError("provider must not run"))
    guidance_restore = MagicMock(side_effect=AssertionError("restoration must not start"))
    resumed._adapter.execute_task = provider
    resumed._restore_guidance_contract = guidance_restore

    error_match = (
        "invalid execution contract" if schema_mutation == "version" else "top-level schema"
    )
    with pytest.raises(OrchestratorError, match=error_match) as exc_info:
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )

    if schema_mutation == "version":
        assert exc_info.value.details == {"contract_version": None}
    else:
        assert exc_info.value.details == {
            "contract_version": persisted.get("version"),
            "invalid": "top_level_schema",
            "missing": expected_missing,
            "unknown": expected_unknown,
        }
    provider.assert_not_called()
    guidance_restore.assert_not_called()


def test_resume_rejects_router_policy_that_validates_its_own_projection() -> None:
    original = _runner()
    persisted = copy.deepcopy(original._build_execution_contract())
    routing = persisted["model_routing"]
    router = routing["router"]
    projection = routing["route_compat"]["projection"]
    router["escalation_retry_threshold"] = 999
    projection["escalation_retry_threshold"] = 999
    routing_fingerprint = OrchestratorRunner._routing_fingerprint(routing)
    persisted["frugality_proof"]["routing_fingerprint"] = routing_fingerprint

    with pytest.raises(OrchestratorError, match="changed model-routing policy"):
        _runner()._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_pre_admission_v2_contract_fails_closed_without_complete_effect_inputs() -> None:
    original = _runner()
    persisted = copy.deepcopy(original._build_execution_contract(seed=_seed()))
    persisted["version"] = 2
    routing = persisted["model_routing"]
    del routing["reasoning_effort"]
    del routing["route_compat"]
    del routing["requested_model_tier"]
    del persisted["execution_semantics"]
    del persisted["frugality_proof"]["execution_semantics_fingerprint"]
    del persisted["execution_inputs"]
    del persisted["frugality_proof"]["execution_inputs_fingerprint"]
    persisted["frugality_proof"]["routing_fingerprint"] = OrchestratorRunner._routing_fingerprint(
        routing
    )

    with pytest.raises(OrchestratorError, match="without durable effect inputs"):
        _runner()._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_pre_requested_tier_v3_contract_fails_closed_without_complete_effect_inputs() -> None:
    original = _runner(base_model_tier="frontier")
    persisted = copy.deepcopy(original._build_execution_contract(seed=_seed()))
    persisted["version"] = 3
    routing = persisted["model_routing"]
    del routing["requested_model_tier"]
    del persisted["execution_semantics"]
    del persisted["frugality_proof"]["execution_semantics_fingerprint"]
    del persisted["execution_inputs"]
    del persisted["frugality_proof"]["execution_inputs_fingerprint"]
    persisted["frugality_proof"]["routing_fingerprint"] = OrchestratorRunner._routing_fingerprint(
        routing
    )

    with pytest.raises(OrchestratorError, match="without durable effect inputs"):
        _runner()._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_pre_execution_semantics_v4_contract_fails_closed_without_complete_effect_inputs() -> None:
    original = _runner()
    persisted = copy.deepcopy(original._build_execution_contract(seed=_seed()))
    persisted["version"] = 4
    del persisted["execution_semantics"]
    del persisted["frugality_proof"]["execution_semantics_fingerprint"]
    del persisted["execution_inputs"]
    del persisted["frugality_proof"]["execution_inputs_fingerprint"]

    with pytest.raises(OrchestratorError, match="without durable effect inputs"):
        _runner()._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


@pytest.mark.parametrize("legacy_version", [5, 6, 7, 8])
def test_recent_contracts_without_complete_effect_inputs_fail_closed(
    legacy_version: int,
) -> None:
    persisted = copy.deepcopy(_runner()._build_execution_contract(seed=_seed()))
    persisted["version"] = legacy_version

    with pytest.raises(OrchestratorError, match="without durable effect inputs") as exc_info:
        _runner()._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )

    assert exc_info.value.details["contract_version"] == legacy_version
    assert exc_info.value.details["resume_blocked"] == "execution_inputs_unavailable"


def test_fat_harness_contract_freezes_resolved_profile_strategy_and_catalog() -> None:
    runner = _runner(fat_harness_mode=True)
    contract = runner._build_execution_contract(seed=_seed())

    strategy = runner._execution_strategy_snapshot(contract, require_bound=True)
    inputs = contract["execution_inputs"]

    assert "consolidated evidence contract" in strategy.get_system_prompt_fragment()
    assert strategy.get_tools() == ["Read", "Edit", "Write", "Bash", "Glob", "Grep"]
    assert inputs["allowed_tools"] == strategy.get_tools()
    assert '"name":"Read"' in inputs["tool_catalog_json"]
    assert len(inputs["tool_catalog_fingerprint"]) == 64


def test_v9_inputs_freeze_context_profile_parent_lineage_pause_and_runtime_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_CONTEXT_PACK", "1")
    monkeypatch.delenv("OUROBOROS_USAGE_LIMIT_PAUSE_HOURS", raising=False)
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        '[project]\nname = "frozen-app"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    inherited = RuntimeHandle(
        backend="claude",
        native_session_id="original-parent",
        metadata={"fork_session": True},
    )
    runner = _runner(cwd=str(tmp_path), inherited_runtime_handle=inherited)
    seed = _seed()
    contract = runner._build_execution_contract(seed=seed)

    inputs = contract["execution_inputs"]
    semantics = contract["execution_semantics"]
    assert inputs["schema_version"] == 2
    assert semantics["version"] == 3
    assert semantics["usage_limit_pause_seconds"] == 18000
    assert semantics["runtime_effect_capabilities"]["version"] == 1
    assert "frozen-app 1.0.0" in inputs["context_pack_fragment"]
    persisted_profile = runner._execution_profile_snapshot(contract, require_bound=True)
    persisted_handle = runner._execution_inherited_runtime_handle_snapshot(
        contract,
        require_bound=True,
    )
    assert persisted_profile is not None
    assert persisted_profile.profile == "code"
    assert persisted_handle is not None
    assert persisted_handle.native_session_id == "original-parent"

    manifest.write_text(
        '[project]\nname = "frozen-app"\nversion = "9.9.9"\n',
        encoding="utf-8",
    )
    frozen_prompt = build_system_prompt(
        seed,
        strategy=runner._execution_strategy_snapshot(contract, require_bound=True),
        repo_root=tmp_path,
        context_pack_enabled=True,
        resolved_context_pack_fragment=runner._execution_context_pack_fragment_snapshot(
            contract,
            require_bound=True,
        ),
    )
    current_prompt = build_system_prompt(
        seed,
        repo_root=tmp_path,
        context_pack_enabled=True,
    )

    assert "frozen-app 1.0.0" in frozen_prompt
    assert "frozen-app 9.9.9" not in frozen_prompt
    assert "frozen-app 9.9.9" in current_prompt


def test_execution_inputs_reject_noncanonical_parent_handle_before_publication() -> None:
    runner = _runner(
        inherited_runtime_handle=RuntimeHandle(
            backend="claude",
            metadata={"non_finite": float("nan")},
        )
    )

    with pytest.raises(OrchestratorError, match="non-canonical execution effect inputs"):
        runner._build_execution_contract(seed=_seed())


@pytest.mark.parametrize(
    "field",
    [
        "context_pack_fragment",
        "execution_profile_json",
        "inherited_runtime_handle_json",
    ],
)
def test_current_execution_inputs_require_complete_effect_population(field: str) -> None:
    persisted = copy.deepcopy(_runner()._build_execution_contract(seed=_seed()))
    del persisted["execution_inputs"][field]
    persisted["frugality_proof"]["execution_inputs_fingerprint"] = (
        OrchestratorRunner._execution_inputs_fingerprint(persisted["execution_inputs"])
    )

    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        _runner()._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_persisted_research_strategy_does_not_gain_edit_on_resume() -> None:
    runner = _runner()
    research_seed = _seed().model_copy(update={"task_type": "research"})
    contract = runner._build_execution_contract(seed=research_seed)

    strategy = runner._execution_strategy_snapshot(contract, require_bound=True)

    assert "Edit" not in strategy.get_tools()
    assert "research" in strategy.get_system_prompt_fragment().lower()


def test_complete_tool_catalog_drift_is_rejected_before_resume_dispatch() -> None:
    from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

    runner = _runner()
    research_seed = _seed().model_copy(update={"task_type": "research"})
    contract = runner._build_execution_contract(seed=research_seed)
    drifted_catalog = assemble_session_tool_catalog(
        ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
    )

    with pytest.raises(OrchestratorError, match="changed prompt/tool authority"):
        runner._bind_execution_tool_authority(
            contract,
            merged_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            tool_catalog=drifted_catalog,
        )


def test_malformed_v3_contract_does_not_bypass_effect_input_version_gate() -> None:
    original = _runner()
    persisted = copy.deepcopy(original._build_execution_contract())
    persisted["version"] = 3
    routing = persisted["model_routing"]
    del routing["requested_model_tier"]
    del routing["reasoning_effort"]
    del persisted["execution_semantics"]
    del persisted["frugality_proof"]["execution_semantics_fingerprint"]
    persisted["frugality_proof"]["routing_fingerprint"] = OrchestratorRunner._routing_fingerprint(
        routing
    )

    with pytest.raises(OrchestratorError, match="without durable effect inputs"):
        _runner()._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_execution_semantics_change_contract_without_changing_route_identity() -> None:
    runner = _runner()
    baseline = runner._build_execution_contract()
    baseline_routing_fingerprint = baseline["frugality_proof"]["routing_fingerprint"]

    runner._run_verify_commands = False
    runner._verify_command_timeout_seconds = 7
    runner._ac_retry_attempts = 9
    runner._enable_decomposition = False
    runner._decomposition_mode = "off"
    runner._max_decomposition_depth = 7
    runner._fat_harness_mode = True
    changed = runner._build_execution_contract()

    assert changed["model_routing"] == baseline["model_routing"]
    assert changed["frugality_proof"]["routing_fingerprint"] == baseline_routing_fingerprint
    assert changed["execution_semantics"] != baseline["execution_semantics"]
    assert (
        changed["frugality_proof"]["execution_semantics_fingerprint"]
        != (baseline["frugality_proof"]["execution_semantics_fingerprint"])
    )


def test_resume_rejects_weaker_current_execution_semantics_before_dispatch() -> None:
    original = _runner(fat_harness_mode=True, max_decomposition_depth=4)
    original._run_verify_commands = True
    original._verify_command_timeout_seconds = 600
    original._ac_retry_attempts = 2
    persisted = original._build_execution_contract(seed=_seed())

    resumed = _runner(enable_decomposition=False, fat_harness_mode=False)
    resumed._run_verify_commands = False
    resumed._verify_command_timeout_seconds = 1
    resumed._ac_retry_attempts = 0
    resumed._max_decomposition_depth = 0

    with pytest.raises(OrchestratorError, match="changed execution semantics"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_resume_rejects_context_pack_and_effective_concurrency_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt mode and resolved fan-out are durable pre-provider semantics."""
    monkeypatch.setenv("OUROBOROS_CONTEXT_PACK", "1")
    monkeypatch.setenv("OUROBOROS_MAX_CONCURRENCY", "1")
    original = _runner(max_parallel_workers=3)
    persisted = original._build_execution_contract(seed=_seed())
    assert persisted["execution_semantics"]["context_pack_enabled"] is True
    assert persisted["execution_semantics"]["effective_parallel_workers"] == 1

    monkeypatch.setenv("OUROBOROS_CONTEXT_PACK", "0")
    monkeypatch.setenv("OUROBOROS_MAX_CONCURRENCY", "3")
    resumed = _runner(max_parallel_workers=3)

    with pytest.raises(OrchestratorError, match="changed execution semantics"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_resume_rejects_backend_rate_and_pacing_owner_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rate gate consumes the same durable resolved backend snapshot."""
    monkeypatch.setenv("OUROBOROS_CLAUDE_RPM", "10")
    original = _runner(max_parallel_workers=3)
    original._adapter.self_governs_rate_limit = False
    persisted = original._build_execution_contract(seed=_seed())
    assert persisted["execution_semantics"]["backend_requests_per_minute"] == 10
    assert persisted["execution_semantics"]["backend_self_governs_rate_limit"] is False

    monkeypatch.setenv("OUROBOROS_CLAUDE_RPM", "20")
    resumed = _runner(max_parallel_workers=3)
    resumed._adapter.self_governs_rate_limit = True

    with pytest.raises(OrchestratorError, match="changed execution semantics"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_resume_rejects_usage_limit_pause_policy_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery timing is immutable execution authority, not live resume config."""

    monkeypatch.setenv("OUROBOROS_USAGE_LIMIT_PAUSE_HOURS", "1")
    persisted = _runner()._build_execution_contract(seed=_seed())
    assert persisted["execution_semantics"]["usage_limit_pause_seconds"] == 3600

    monkeypatch.setenv("OUROBOROS_USAGE_LIMIT_PAUSE_HOURS", "9")
    with pytest.raises(OrchestratorError, match="changed execution semantics"):
        _runner()._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


@pytest.mark.parametrize(
    "resumed_capabilities",
    [
        RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            reasoning_effort_support=ParamSupport.IGNORED,
            enforceable_reasoning_efforts=frozenset({"low", "medium", "high", "xhigh"}),
            model_override_support=ParamSupport.NATIVE,
        ),
        RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            reasoning_effort_support=ParamSupport.NATIVE,
            enforceable_reasoning_efforts=frozenset({"low", "medium", "xhigh"}),
            model_override_support=ParamSupport.NATIVE,
        ),
    ],
)
def test_resume_rejects_runtime_effort_capability_or_vocabulary_drift(
    resumed_capabilities: RuntimeCapabilities,
) -> None:
    """Provider kwargs cannot be recomputed from a changed runtime declaration."""
    original = _runner()
    original._reasoning_effort = "high"
    original._adapter.capabilities = RuntimeCapabilities(
        skill_dispatch=True,
        targeted_resume=True,
        structured_output=True,
        reasoning_effort_support=ParamSupport.NATIVE,
        enforceable_reasoning_efforts=frozenset({"low", "medium", "high", "xhigh"}),
        model_override_support=ParamSupport.NATIVE,
    )
    persisted = original._build_execution_contract(seed=_seed())
    assert (
        persisted["execution_semantics"]["runtime_effect_capabilities"]["reasoning_effort_support"]
        == "native"
    )

    resumed = _runner()
    resumed._reasoning_effort = "high"
    resumed._adapter.capabilities = resumed_capabilities

    with pytest.raises(OrchestratorError, match="changed execution semantics"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


@pytest.mark.parametrize(
    "field",
    [
        "run_verify_commands",
        "verify_command_timeout_seconds",
        "ac_retry_attempts",
        "cross_harness_redispatch",
        "enable_decomposition",
        "decomposition_mode",
        "max_decomposition_depth",
        "max_parallel_workers",
        "effective_parallel_workers",
        "fat_harness_mode",
        "shadow_replay_enabled",
        "checkpoint_store_enabled",
        "session_signal_hub_enabled",
        "context_pack_enabled",
        "backend_limits_backend",
        "backend_max_concurrency",
        "backend_requests_per_minute",
        "backend_tokens_per_minute",
        "backend_self_governs_rate_limit",
        "usage_limit_pause_seconds",
        "runtime_effect_capabilities",
    ],
)
def test_current_execution_semantics_requires_complete_exact_population(field: str) -> None:
    original = _runner()
    persisted = copy.deepcopy(original._build_execution_contract())
    del persisted["execution_semantics"][field]
    persisted["frugality_proof"]["execution_semantics_fingerprint"] = (
        OrchestratorRunner._execution_semantics_fingerprint(persisted["execution_semantics"])
    )

    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        _runner()._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_current_execution_semantics_migrates_retired_preflight_authority_once() -> None:
    persisted = copy.deepcopy(_runner()._build_execution_contract())
    persisted["execution_semantics"]["decomposition_mode"] = "preflight"
    persisted["frugality_proof"]["execution_semantics_fingerprint"] = (
        OrchestratorRunner._execution_semantics_fingerprint(persisted["execution_semantics"])
    )

    resumed = _runner()
    changed = resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert changed is True
    assert resumed._execution_contract is not None
    migrated = resumed._execution_contract
    assert migrated["execution_semantics"]["decomposition_mode"] == "bounce_only"
    assert migrated["frugality_proof"]["execution_semantics_fingerprint"] == (
        OrchestratorRunner._execution_semantics_fingerprint(migrated["execution_semantics"])
    )
    assert resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: migrated}) is False


def test_legacy_preflight_migration_rejects_unsealed_semantics() -> None:
    persisted = copy.deepcopy(_runner()._build_execution_contract())
    persisted["execution_semantics"]["decomposition_mode"] = "preflight"

    with pytest.raises(OrchestratorError, match="invalid legacy preflight contract"):
        _runner()._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


@pytest.mark.parametrize(
    "field",
    [
        "targeted_resume",
        "reasoning_effort_support",
        "enforceable_reasoning_efforts",
        "model_override_support",
        "session_signals",
    ],
)
def test_current_runtime_effect_capabilities_require_complete_exact_population(
    field: str,
) -> None:
    persisted = copy.deepcopy(_runner()._build_execution_contract())
    del persisted["execution_semantics"]["runtime_effect_capabilities"][field]
    persisted["frugality_proof"]["execution_semantics_fingerprint"] = (
        OrchestratorRunner._execution_semantics_fingerprint(persisted["execution_semantics"])
    )

    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        _runner()._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


@pytest.mark.parametrize("pause_seconds", [True, 0, 31_536_001])
def test_current_execution_semantics_rejects_unbounded_pause_policy(
    pause_seconds: object,
) -> None:
    persisted = copy.deepcopy(_runner()._build_execution_contract())
    persisted["execution_semantics"]["usage_limit_pause_seconds"] = pause_seconds
    persisted["frugality_proof"]["execution_semantics_fingerprint"] = (
        OrchestratorRunner._execution_semantics_fingerprint(persisted["execution_semantics"])
    )

    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        _runner()._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


@pytest.mark.parametrize("missing", ["reasoning_effort", "route_compat", "requested_model_tier"])
def test_current_contract_missing_admission_field_is_not_treated_as_legacy(
    missing: str,
) -> None:
    original = _runner()
    persisted = copy.deepcopy(original._build_execution_contract())
    routing = persisted["model_routing"]
    del routing[missing]
    persisted["frugality_proof"]["routing_fingerprint"] = OrchestratorRunner._routing_fingerprint(
        routing
    )

    with pytest.raises(OrchestratorError):
        _runner()._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_unbounded_economics_contract_is_json_safe_and_round_trips() -> None:
    huge = 10**5000
    economics = _frontier_custom_economics()
    tiers = dict(economics.tiers)
    tiers["standard"] = tiers["standard"].model_copy(update={"cost_factor": huge})
    economics = economics.model_copy(update={"tiers": tiers, "escalation_threshold": huge})
    router = _frontier_custom_router()
    router = ModelRouter(
        tier_models=router.tier_models,
        runtime_backend=router.runtime_backend,
        child_tier=router.child_tier,
        base_tier=router.base_tier,
        escalation_retry_threshold=huge,
    )
    original = _runner()
    original._route_economics = economics
    original._model_router = router
    original._requested_model_tier = "frontier"

    persisted = original._build_execution_contract()
    encoded = json.dumps(persisted, sort_keys=True)
    assert isinstance(encoded, str)
    routing = persisted["model_routing"]
    assert routing["router"]["escalation_retry_threshold"] == "1" + "0" * 5000
    projection = routing["route_compat"]["projection"]
    assert projection["escalation_retry_threshold"] == "1" + "0" * 5000
    assert projection["registry"]["candidates"][1]["cost_units"] == "1" + "0" * 5000

    resumed = _runner()
    resumed._route_economics = economics
    resumed._model_router = router
    resumed._requested_model_tier = "frontier"
    assert (
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: json.loads(encoded)})
        is False
    )


def test_contract_serialization_ignores_constrained_python_digit_limit() -> None:
    if not hasattr(sys, "set_int_max_str_digits"):
        pytest.skip("Python integer digit limits are unavailable")
    previous_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        huge = 10**1000
        economics = _frontier_custom_economics().model_copy(update={"escalation_threshold": huge})
        tiers = dict(economics.tiers)
        tiers["standard"] = tiers["standard"].model_copy(update={"cost_factor": huge})
        economics = economics.model_copy(update={"tiers": tiers})
        router = replace(_frontier_custom_router(), escalation_retry_threshold=huge)
        runner = _runner()
        runner._route_economics = economics
        runner._model_router = router
        runner._requested_model_tier = "frontier"

        contract = runner._build_execution_contract()

        assert json.dumps(contract, sort_keys=True)
        assert contract["model_routing"]["router"]["escalation_retry_threshold"] == (
            "1" + "0" * 1000
        )
    finally:
        sys.set_int_max_str_digits(previous_limit)


@pytest.mark.parametrize("requested_tier", ["frugal", "frontier"])
def test_resume_without_argument_preserves_persisted_non_default_tier(
    requested_tier: str,
) -> None:
    original = _runner(base_model_tier=requested_tier)
    persisted = original._build_execution_contract()
    assert persisted["model_routing"]["requested_model_tier"] == requested_tier

    resumed = _runner()
    changed = resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert changed is False
    assert resumed._requested_model_tier == requested_tier
    assert resumed._model_router is not None
    assert resumed._model_router.base_tier == requested_tier


@pytest.mark.parametrize(
    ("persisted_effort", "current_effort"),
    [("low", "high"), ("high", None), (None, "low")],
)
def test_resume_rejects_base_reasoning_effort_transition(
    persisted_effort: str | None,
    current_effort: str | None,
) -> None:
    original = _runner()
    original._reasoning_effort = persisted_effort
    persisted = original._build_execution_contract()
    route_compat = persisted["model_routing"]["route_compat"]
    assert route_compat["projection"]["effort"] == persisted_effort

    resumed = _runner()
    resumed._reasoning_effort = current_effort

    with pytest.raises(OrchestratorError, match="changed route compatibility catalog"):
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_resume_restores_persisted_kill_switch() -> None:
    original = _runner()
    original._model_router = None
    persisted = original._build_execution_contract()

    resumed = _runner()
    assert resumed._model_router is not None

    resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert resumed._model_router is None


def test_dormant_model_routing_still_rejects_reasoning_effort_drift() -> None:
    original = _runner()
    original._model_router = None
    original._reasoning_effort = "low"
    persisted = original._build_execution_contract()
    routing = persisted["model_routing"]
    assert routing["route_compat"] == {"version": 1, "enabled": False}
    assert routing["reasoning_effort"] == "low"

    resumed = _runner()
    resumed._reasoning_effort = "high"

    with pytest.raises(OrchestratorError, match="changed reasoning-effort contract"):
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_explicit_resume_tier_override_replaces_persisted_contract() -> None:
    original = _runner()
    _use_frontier_custom_routing(original)
    persisted = original._build_execution_contract()

    resumed = _runner(base_model_tier="standard")
    resumed._route_economics = _frontier_custom_economics()
    resumed._model_router = _frontier_custom_router(base_tier="standard")
    changed = resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert changed is True
    assert resumed._model_router is not None
    assert resumed._model_router.base_tier == "standard"
    assert resumed._model_router.tier_models == _frontier_custom_router().tier_models


def test_explicit_override_preserves_legacy_workspace_across_two_resumes(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    workspace = repo_root / "packages" / "app"
    workspace.mkdir(parents=True)
    _init_git_repo(repo_root)
    original = _runner(cwd=str(workspace))
    _use_frontier_custom_routing(original)
    persisted = original._build_execution_contract(seed=_seed())
    persisted["frugality_proof"]["project_root"] = str(workspace.resolve())
    persisted["frugality_proof"]["workspace_path"] = "."
    legacy_start = {
        "execution_id": "legacy-routing-override",
        "seed_id": "seed-routing-contract",
    }

    first_resume = _runner(cwd=str(workspace), base_model_tier="standard")
    first_resume._route_economics = _frontier_custom_economics()
    first_resume._model_router = _frontier_custom_router(base_tier="standard")
    changed = first_resume._restore_execution_contract(
        {
            EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
            SESSION_START_IDENTITY_PROGRESS_KEY: legacy_start,
        },
        seed=_seed(),
    )

    assert changed is True
    replacement = first_resume._execution_contract
    assert replacement is not None
    assert replacement["frugality_proof"]["project_root"] == str(workspace.resolve())
    assert replacement["frugality_proof"]["workspace_path"] == "."

    second_resume = _runner(cwd=str(workspace))
    second_resume._route_economics = _frontier_custom_economics()
    second_resume._model_router = _frontier_custom_router(base_tier="standard")
    second_resume._requested_model_tier = "standard"

    assert (
        second_resume._restore_execution_contract(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: replacement,
                SESSION_START_IDENTITY_PROGRESS_KEY: legacy_start,
            },
            seed=_seed(),
        )
        is False
    )


def test_explicit_resume_tier_override_replaces_changed_catalog() -> None:
    original = _runner()
    _use_frontier_custom_routing(original)
    persisted = original._build_execution_contract()

    resumed = _runner(base_model_tier="standard")
    changed_economics = _frontier_custom_economics()
    tiers = dict(changed_economics.tiers)
    tiers["standard"] = TierConfig(
        cost_factor=999,
        models=[ModelConfig(provider="anthropic", model="custom-sonnet")],
    )
    resumed._route_economics = EconomicsConfig(
        default_tier=changed_economics.default_tier,
        escalation_threshold=changed_economics.escalation_threshold,
        tiers=tiers,
    )
    resumed._model_router = _frontier_custom_router(base_tier="standard")

    changed = resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert changed is True
    replacement = resumed._execution_contract
    assert replacement is not None
    projection = replacement["model_routing"]["route_compat"]["projection"]
    assert projection["registry"]["candidates"][1]["cost_units"] == "999"


def test_enabled_router_rejects_dormant_route_compat_on_resume() -> None:
    original = _runner()
    persisted = copy.deepcopy(original._build_execution_contract())
    routing = persisted["model_routing"]
    routing["route_compat"] = {"version": 1, "enabled": False}
    persisted["frugality_proof"]["routing_fingerprint"] = OrchestratorRunner._routing_fingerprint(
        routing
    )

    with pytest.raises(OrchestratorError, match="enabled route compatibility"):
        _runner()._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_dormant_current_contract_requires_explicit_route_compat() -> None:
    original = _runner()
    original._model_router = None
    persisted = copy.deepcopy(original._build_execution_contract())
    routing = persisted["model_routing"]
    del routing["route_compat"]
    persisted["frugality_proof"]["routing_fingerprint"] = OrchestratorRunner._routing_fingerprint(
        routing
    )

    with pytest.raises(OrchestratorError, match="explicit route compatibility"):
        _runner()._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_present_malformed_resume_contract_fails_closed() -> None:
    resumed = _runner()
    assert resumed._model_router is not None

    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        resumed._restore_execution_contract(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: {
                    "version": 1,
                    "model_routing": {"version": 1, "enabled": True},
                }
            }
        )


def test_empty_observed_runtime_identity_is_rejected() -> None:
    original = _runner()
    persisted = original._build_execution_contract(seed=_seed())
    malformed_routing = {
        **persisted["model_routing"],
        "runtime_execution": {
            "version": 1,
            "observed": True,
            "identity": {},
        },
    }
    malformed_contract = {
        **persisted,
        "model_routing": malformed_routing,
        "frugality_proof": {
            **persisted["frugality_proof"],
            "routing_fingerprint": OrchestratorRunner._routing_fingerprint(malformed_routing),
        },
    }

    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        _runner()._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: malformed_contract},
            seed=_seed(),
        )


def test_cross_backend_resume_is_rejected_before_dispatch() -> None:
    original = _runner()
    original._model_router = _frontier_custom_router()
    persisted = original._build_execution_contract()

    resumed = _runner()
    resumed._adapter.runtime_backend = "codex_cli"

    with pytest.raises(OrchestratorError, match="different runtime backend"):
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_explicit_tier_does_not_authorize_cross_backend_resume() -> None:
    original = _runner()
    persisted = original._build_execution_contract(seed=_seed())

    resumed = _runner(base_model_tier="standard")
    resumed._adapter.runtime_backend = "codex_cli"

    with pytest.raises(OrchestratorError, match="different runtime backend"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_explicit_tier_does_not_bypass_malformed_persisted_router() -> None:
    original = _runner()
    persisted = original._build_execution_contract(seed=_seed())
    persisted_routing = persisted["model_routing"]
    assert isinstance(persisted_routing.get("router"), dict)
    malformed_routing = {
        **persisted_routing,
        "router": {
            **persisted_routing["router"],
            "base_tier": "tampered-tier",
        },
    }
    malformed_contract = {
        **persisted,
        "model_routing": malformed_routing,
        "frugality_proof": {
            **persisted["frugality_proof"],
            "routing_fingerprint": OrchestratorRunner._routing_fingerprint(malformed_routing),
        },
    }

    resumed = _runner(base_model_tier="standard")
    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: malformed_contract},
            seed=_seed(),
        )


def test_explicit_tier_does_not_bypass_nested_backend_mismatch() -> None:
    original = _runner()
    persisted = original._build_execution_contract(seed=_seed())
    persisted_routing = persisted["model_routing"]
    assert isinstance(persisted_routing.get("router"), dict)
    inconsistent_routing = {
        **persisted_routing,
        "router": {
            **persisted_routing["router"],
            "runtime_backend": "codex_cli",
        },
    }
    inconsistent_contract = {
        **persisted,
        "model_routing": inconsistent_routing,
        "frugality_proof": {
            **persisted["frugality_proof"],
            "routing_fingerprint": OrchestratorRunner._routing_fingerprint(inconsistent_routing),
        },
    }

    resumed = _runner(base_model_tier="standard")
    with pytest.raises(OrchestratorError, match="inconsistent runtime backend"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: inconsistent_contract},
            seed=_seed(),
        )


def test_constructor_model_pin_is_persisted_and_mismatch_is_rejected() -> None:
    original = _runner(constructor_model="claude-sonnet-original")
    persisted = original._build_execution_contract(seed=_seed())

    assert persisted["model_routing"]["runtime_backend"] == "claude"
    assert persisted["model_routing"]["constructor_model"] == {
        "observed": True,
        "model": "claude-sonnet-original",
    }

    resumed = _runner(constructor_model="claude-sonnet-changed")
    with pytest.raises(OrchestratorError, match="different constructor model"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_codex_dynamic_profiles_do_not_create_a_portable_resume_identity() -> None:
    original_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    original_runtime._runtime_profile = "zep-runtime"
    original_runtime._codex_profile = "zep-proxy-a"
    original_runtime._resolved_fallback_model = "resolved-fallback-model"
    original_runtime._resolved_fallback_profile = None
    original = OrchestratorRunner(original_runtime, AsyncMock(), MagicMock())
    original_contract = original._build_execution_contract(seed=_seed())

    resumed_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    resumed_runtime._runtime_profile = "zep-runtime"
    resumed_runtime._codex_profile = "zep-proxy-b"
    resumed_runtime._resolved_fallback_model = "resolved-fallback-model"
    resumed_runtime._resolved_fallback_profile = None
    resumed_contract = OrchestratorRunner(
        resumed_runtime,
        AsyncMock(),
        MagicMock(),
    )._build_execution_contract(seed=_seed())

    _assert_process_local_runtime_contract(original_contract)
    _assert_process_local_runtime_contract(resumed_contract)
    assert (
        original_contract["foundation_a_authority"]["correlation_id"]
        != resumed_contract["foundation_a_authority"]["correlation_id"]
    )


def test_codex_resolved_fallback_state_stays_out_of_durable_runtime_identity() -> None:
    original_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    original_runtime._resolved_fallback_model = "gpt-original"
    original_runtime._resolved_fallback_profile = None
    original_contract = OrchestratorRunner(
        original_runtime,
        AsyncMock(),
        MagicMock(),
    )._build_execution_contract(seed=_seed())

    resumed_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    resumed_runtime._resolved_fallback_model = "gpt-changed"
    resumed_runtime._resolved_fallback_profile = None
    resumed_contract = OrchestratorRunner(
        resumed_runtime,
        AsyncMock(),
        MagicMock(),
    )._build_execution_contract(seed=_seed())

    _assert_process_local_runtime_contract(original_contract)
    _assert_process_local_runtime_contract(resumed_contract)


def test_codex_profile_name_alone_stays_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "off")
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    runtime._codex_profile = "same-name-mutable-profile"
    runtime._resolved_fallback_model = None
    runtime._resolved_fallback_profile = "same-name-mutable-profile"
    runner = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    persisted = runner._build_execution_contract(seed=_seed())

    _assert_process_local_runtime_contract(persisted)


def test_non_codex_runtime_does_not_inherit_codex_profile_as_model_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "off")
    runtime = GooseCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    runtime._codex_profile = "irrelevant-codex-profile"
    runtime._resolved_fallback_model = "irrelevant-codex-model"
    runner = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    persisted = runner._build_execution_contract(seed=_seed())

    _assert_process_local_runtime_contract(persisted)


def test_runtime_model_sentinel_is_not_persisted_as_a_constructor_pin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "off")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model="default",
        cwd="/tmp/project",
    )
    runner = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    persisted = runner._build_execution_contract(seed=_seed())

    assert persisted["model_routing"]["constructor_model"] == {
        "observed": True,
        "model": None,
    }
    _assert_process_local_runtime_contract(persisted)


def test_codex_profile_file_changes_do_not_create_a_portable_runtime_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    (codex_home / "config.toml").write_text(
        'model_provider = "proxy-a"\n',
        encoding="utf-8",
    )
    profile_path = codex_home / "stable-name.config.toml"
    profile_path.write_text('model_provider = "proxy-a"\n', encoding="utf-8")

    original_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    original_runtime._codex_profile = "stable-name"
    original = OrchestratorRunner(original_runtime, AsyncMock(), MagicMock())
    original_contract = original._build_execution_contract(seed=_seed())

    profile_path.write_text('model_provider = "proxy-b"\n', encoding="utf-8")
    resumed_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    resumed_runtime._codex_profile = "stable-name"
    resumed_contract = OrchestratorRunner(
        resumed_runtime,
        AsyncMock(),
        MagicMock(),
    )._build_execution_contract(seed=_seed())

    _assert_process_local_runtime_contract(original_contract)
    _assert_process_local_runtime_contract(resumed_contract)


def test_codex_home_changes_do_not_create_a_portable_runtime_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_home = tmp_path / "codex-home-a"
    second_home = tmp_path / "codex-home-b"
    first_home.mkdir()
    second_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(first_home))
    original_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    original_contract = OrchestratorRunner(
        original_runtime,
        AsyncMock(),
        MagicMock(),
    )._build_execution_contract(seed=_seed())

    monkeypatch.setenv("CODEX_HOME", str(second_home))
    resumed_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    resumed_contract = OrchestratorRunner(
        resumed_runtime,
        AsyncMock(),
        MagicMock(),
    )._build_execution_contract(seed=_seed())

    _assert_process_local_runtime_contract(original_contract)
    _assert_process_local_runtime_contract(resumed_contract)


def test_contract_build_never_asks_codex_for_dynamic_execution_identity() -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    provider = MagicMock(side_effect=AssertionError("dynamic provider must not run"))
    runtime.execution_identity_contract = provider  # type: ignore[method-assign]

    contract = OrchestratorRunner(
        runtime,
        AsyncMock(),
        MagicMock(),
    )._build_execution_contract(seed=_seed())

    _assert_process_local_runtime_contract(contract)
    provider.assert_not_called()


@pytest.mark.parametrize(
    "runtime_handle",
    [
        RuntimeHandle(
            backend="codex_cli",
            native_session_id="thread-123",
            metadata={"codex_profile": "injected-profile"},
        ),
        RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            native_session_id="thread-123",
        ),
        RuntimeHandle(
            backend="claude",
            native_session_id="claude-thread",
        ),
    ],
)
def test_process_local_runtime_never_recomputes_a_resume_handle_selector(
    runtime_handle: RuntimeHandle,
) -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    runner = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    runner._execution_contract = runner._build_execution_contract(seed=_seed())
    provider = MagicMock(side_effect=AssertionError("selector provider must not run"))
    runtime.resume_handle_execution_identity_contract = provider  # type: ignore[method-assign]

    runner._validate_resume_handle_execution_identity(runtime_handle)

    provider.assert_not_called()


def test_process_local_runtime_default_handle_requires_no_persisted_selector() -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    runner = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    runner._execution_contract = runner._build_execution_contract(seed=_seed())
    provider = MagicMock(side_effect=AssertionError("selector provider must not run"))
    runtime.resume_handle_execution_identity_contract = provider  # type: ignore[method-assign]

    runner._validate_resume_handle_execution_identity(
        RuntimeHandle(
            backend="codex_cli",
            native_session_id="thread-123",
        )
    )
    provider.assert_not_called()


@pytest.mark.parametrize("backend", ["codex_cli", "goose", "pi", "hermes_cli", "opencode"])
def test_bound_runtime_identity_rejects_same_backend_thread_swap(backend: str) -> None:
    progress = {
        SESSION_RUNTIME_IDENTITY_PROGRESS_KEY: {
            "status": "bound",
            "backend": backend,
            "id_kind": "native_session_id",
            "id": "thread-original",
        }
    }

    with pytest.raises(OrchestratorError, match="different backend session"):
        OrchestratorRunner._validate_bound_runtime_resume_identity(
            progress,
            RuntimeHandle(
                backend=backend,
                native_session_id="thread-injected",
            ),
        )


def test_non_codex_runtime_rejects_cross_backend_handle() -> None:
    runtime = GooseCliRuntime(
        cli_path="/bin/echo",
        model="pinned-goose-model",
        cwd="/tmp/project",
    )
    runner = OrchestratorRunner(runtime, AsyncMock(), MagicMock())

    with pytest.raises(OrchestratorError, match="handle from a different backend"):
        runner._validate_runtime_handle_backend(
            RuntimeHandle(
                backend="claude",
                native_session_id="claude-session",
            )
        )


def test_runtime_handle_permission_is_overwritten_to_bypass() -> None:
    runner = _runner()

    normalized = runner._force_runtime_handle_permission(
        RuntimeHandle(
            backend="claude",
            native_session_id="claude-session",
            approval_mode="acceptEdits",
        )
    )

    assert normalized is not None
    assert normalized.approval_mode == "bypassPermissions"


def test_codex_runner_forces_native_bypass_flag() -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        permission_mode="acceptEdits",
        model="gpt-pinned",
        cwd="/tmp/project",
    )
    OrchestratorRunner(runtime, AsyncMock(), MagicMock())

    assert runtime.permission_mode == "bypassPermissions"
    assert "--dangerously-bypass-approvals-and-sandbox" in runtime._build_command("/tmp/output")


def test_codex_command_consumes_frozen_fallback_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    runtime._resolved_fallback_model = "gpt-frozen"
    runtime._resolved_fallback_profile = None
    monkeypatch.setattr(
        runtime,
        "_resolve_runtime_codex_config_uncached",
        lambda _runtime_handle: ("gpt-drifted", None),
    )

    command = runtime._build_command("/tmp/output", prompt="test")

    assert command[command.index("--model") + 1] == "gpt-frozen"


def test_kill_switched_resume_cannot_drift_to_a_new_constructor_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "off")
    original = _runner(constructor_model="claude-sonnet-original")
    assert original._model_router is None
    persisted = original._build_execution_contract(seed=_seed())

    monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING")
    resumed = _runner(constructor_model="claude-sonnet-changed")

    with pytest.raises(OrchestratorError, match="different constructor model"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_unpinned_kill_switched_runtime_is_limited_to_process_local_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "off")
    original = _runner(constructor_model=None)
    persisted = original._build_execution_contract(seed=_seed())

    assert persisted["model_routing"]["constructor_model"] == {
        "observed": True,
        "model": None,
    }
    _assert_process_local_runtime_contract(persisted)
    assert (
        original._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )
        is False
    )


def test_kill_switched_contract_still_rejects_cross_backend_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "off")
    original = _runner(constructor_model="shared-model-pin")
    assert original._model_router is None
    persisted = original._build_execution_contract(seed=_seed())

    monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING")
    resumed = _runner(constructor_model="shared-model-pin")
    resumed._adapter.runtime_backend = "codex_cli"

    with pytest.raises(OrchestratorError, match="different runtime backend"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_explicit_constructor_model_change_requires_a_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _runner(constructor_model="claude-sonnet-original")
    persisted = original._build_execution_contract(seed=_seed())

    monkeypatch.setenv("OUROBOROS_EXECUTION_MODEL", "claude-sonnet-intentional")
    resumed = _runner(constructor_model="claude-sonnet-intentional")

    with pytest.raises(OrchestratorError, match="different constructor model"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_cross_workspace_resume_is_rejected_before_dispatch(tmp_path: Path) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    original = _runner(cwd=str(project_a))
    persisted = original._build_execution_contract(seed=_seed())
    resumed = _runner(cwd=str(project_b))

    with pytest.raises(OrchestratorError, match="different project workspace"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_same_repo_different_managed_worktree_cannot_resume(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    original = _runner(
        task_workspace=_workspace(
            durable_id="run-original",
            worktree_path=tmp_path / "worktrees" / "run-original",
            repo_root=repo_root,
        )
    )
    persisted = original._build_execution_contract(seed=_seed())
    resumed = _runner(
        task_workspace=_workspace(
            durable_id="run-different",
            worktree_path=tmp_path / "worktrees" / "run-different",
            repo_root=repo_root,
        )
    )

    assert resumed._proof_workspace_identity() == original._proof_workspace_identity()
    with pytest.raises(OrchestratorError, match="different execution workspace"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_current_contract_rejects_llm_backend_drift() -> None:
    original = _runner()
    persisted = original._build_execution_contract(seed=_seed())
    resumed = _runner()
    resumed._adapter.llm_backend = "openai"

    with pytest.raises(OrchestratorError, match="different LLM backend"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_current_contract_always_binds_bypass_permission_mode() -> None:
    original = _runner()
    persisted = original._build_execution_contract(seed=_seed())
    resumed = _runner()
    resumed._adapter.permission_mode = "acceptEdits"

    assert persisted["model_routing"]["permission_mode"] == {
        "observed": True,
        "mode": "bypassPermissions",
    }
    assert (
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )
        is False
    )


def test_modified_seed_is_rejected_on_resume() -> None:
    original = _runner()
    persisted = original._build_execution_contract(seed=_seed())

    with pytest.raises(OrchestratorError, match="modified Seed"):
        _runner()._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(criterion="A materially different acceptance criterion"),
        )


def test_seed_fingerprint_ignores_identity_but_tracks_semantics() -> None:
    first = _seed()
    same_semantics = first.model_copy(update={"metadata": SeedMetadata(seed_id="another-id")})
    changed = _seed(goal="A changed executable goal")

    assert OrchestratorRunner._seed_semantics_fingerprint(first) == (
        OrchestratorRunner._seed_semantics_fingerprint(same_semantics)
    )
    assert OrchestratorRunner._seed_semantics_fingerprint(first) != (
        OrchestratorRunner._seed_semantics_fingerprint(changed)
    )


def test_contractless_legacy_resume_fails_closed_without_effect_inputs() -> None:
    runner = _runner()

    with pytest.raises(OrchestratorError, match="without durable effect inputs") as exc_info:
        runner._restore_execution_contract({}, seed=_seed())

    assert exc_info.value.details["contract_version"] is None
    assert exc_info.value.details["resume_blocked"] == "execution_inputs_unavailable"


@pytest.mark.asyncio
async def test_legacy_resume_fails_closed_before_seed_identity_comparison() -> None:
    start = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="legacy-seed-mismatch",
        data={
            "execution_id": "legacy-exec",
            "seed_id": "seed-routing-contract",
            "seed_goal": "Prove durable routing",
            "runtime_backend": "claude",
        },
    )
    store = AsyncMock()
    store.replay.return_value = [start]
    store.query_session_related_events.return_value = []
    adapter = _adapter()
    adapter.execute_task = MagicMock()
    runner = OrchestratorRunner(adapter, store, MagicMock())
    changed_seed = _seed().model_copy(
        update={"metadata": SeedMetadata(seed_id="different-seed-id")}
    )

    result = await runner.resume_session("legacy-seed-mismatch", changed_seed)

    assert result.is_err
    assert result.error.details["resume_blocked"] == "process_local_resume_unavailable"
    adapter.execute_task.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_resume_fails_closed_before_seed_goal_comparison() -> None:
    start = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="legacy-goal-mismatch",
        data={
            "execution_id": "legacy-exec",
            "seed_id": "seed-routing-contract",
            "seed_goal": "Prove durable routing",
            "runtime_backend": "claude",
        },
    )
    store = AsyncMock()
    store.replay.return_value = [start]
    store.query_session_related_events.return_value = []
    adapter = _adapter()
    adapter.execute_task = MagicMock()
    runner = OrchestratorRunner(adapter, store, MagicMock())

    result = await runner.resume_session(
        "legacy-goal-mismatch",
        _seed(goal="A completely changed executable goal"),
    )

    assert result.is_err
    assert result.error.details["resume_blocked"] == "process_local_resume_unavailable"
    adapter.execute_task.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_resume_fails_closed_before_backend_comparison() -> None:
    start = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="legacy-backend-mismatch",
        data={
            "execution_id": "legacy-exec",
            "seed_id": "seed-routing-contract",
            "seed_goal": "Prove durable routing",
            "runtime_backend": "claude",
        },
    )
    store = AsyncMock()
    store.replay.return_value = [start]
    store.query_session_related_events.return_value = []
    adapter = _adapter()
    adapter.runtime_backend = "codex_cli"
    adapter.execute_task = MagicMock()
    runner = OrchestratorRunner(adapter, store, MagicMock())

    result = await runner.resume_session("legacy-backend-mismatch", _seed())

    assert result.is_err
    assert result.error.details["resume_blocked"] == "process_local_resume_unavailable"
    adapter.execute_task.assert_not_called()


def test_legacy_resume_rejects_persisted_workspace_mismatch(tmp_path: Path) -> None:
    persisted_workspace = _workspace(
        durable_id="legacy-original",
        worktree_path=tmp_path / "worktrees" / "legacy-original",
        repo_root=tmp_path / "repo-original",
    )
    current_workspace = _workspace(
        durable_id="legacy-current",
        worktree_path=tmp_path / "worktrees" / "legacy-current",
        repo_root=tmp_path / "repo-current",
    )
    runner = _runner(task_workspace=current_workspace)

    with pytest.raises(OrchestratorError, match="without durable effect inputs"):
        runner._restore_execution_contract(
            {
                SESSION_START_IDENTITY_PROGRESS_KEY: {
                    "seed_id": "seed-routing-contract",
                    "seed_goal": "Prove durable routing",
                    "runtime_backend": "claude",
                },
                "workspace": persisted_workspace.to_progress_dict(),
            },
            seed=_seed(),
        )


def test_legacy_resume_cannot_reconstruct_forced_permission_or_effect_inputs() -> None:
    runner = _runner()

    with pytest.raises(OrchestratorError, match="without durable effect inputs"):
        runner._restore_execution_contract(
            {
                SESSION_START_IDENTITY_PROGRESS_KEY: {
                    "seed_id": "seed-routing-contract",
                    "seed_goal": "Prove durable routing",
                    "runtime_backend": "claude",
                    "llm_backend": "anthropic",
                },
                "runtime": {
                    "backend": "claude",
                    "approval_mode": "acceptEdits",
                },
            },
            seed=_seed(),
        )


def test_mcp_model_tier_omission_remains_distinguishable_from_explicit_medium() -> None:
    parameter = next(
        item for item in ExecuteSeedHandler().definition.parameters if item.name == "model_tier"
    )

    # FastMCP materializes declared defaults before invoking the handler. Keeping
    # this schema default unset lets the handler distinguish omitted (restore the
    # checkpoint) from explicitly supplied ``medium`` (replace it).
    assert parameter.default is None

    assert _resolve_model_tier_request({}, is_resume=False) == (
        "medium",
        "standard",
        "medium",
    )
    assert _resolve_model_tier_request({}, is_resume=True) == ("medium", None, None)
    assert _resolve_model_tier_request({"model_tier": "medium"}, is_resume=True) == (
        "medium",
        "standard",
        "medium",
    )


def test_managed_worktrees_share_canonical_source_workspace_identity(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    first = _runner(
        task_workspace=_workspace(
            durable_id="run-1",
            worktree_path=tmp_path / "worktrees" / "run-1",
            repo_root=repo_root,
        )
    )
    second = _runner(
        task_workspace=_workspace(
            durable_id="run-2",
            worktree_path=tmp_path / "worktrees" / "run-2",
            repo_root=repo_root,
        )
    )

    first_proof = first._build_execution_contract(seed=_seed())["frugality_proof"]
    second_proof = second._build_execution_contract(seed=_seed())["frugality_proof"]
    first_resume = first._build_execution_contract(seed=_seed())["resume"]
    second_resume = second._build_execution_contract(seed=_seed())["resume"]

    assert first_proof == second_proof
    assert first_resume != second_resume
    assert first_proof["project_root"] == str(repo_root.resolve())
    assert first_proof["workspace_path"] == "packages/app"
    assert first_proof["protocol_version"] == FRUGALITY_PROOF_PROTOCOL_VERSION
    assert len(first_proof["seed_fingerprint"]) == 64


def test_linked_checkout_and_managed_task_share_project_identity(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    _init_git_repo(primary)
    _git("worktree", "add", "-q", "-b", "linked", str(linked), "HEAD", cwd=primary)
    linked_workspace = linked / "packages" / "app"
    linked_workspace.mkdir(parents=True)
    generated = tmp_path / "managed" / "run-1"
    (generated / "packages" / "app").mkdir(parents=True)

    direct = _runner(cwd=str(linked_workspace))._project_identity()
    managed = _runner(
        task_workspace=_workspace(
            durable_id="run-1",
            worktree_path=generated,
            repo_root=linked,
        )
    )._project_identity()

    assert direct == managed
    assert direct is not None
    assert direct.project_root == str(primary.resolve())
    assert direct.workspace_path == "packages/app"


def test_newline_git_topology_survives_direct_managed_and_resume_paths(tmp_path: Path) -> None:
    primary = tmp_path / "primary\nrepo"
    linked = tmp_path / "linked\nrepo"
    generated = tmp_path / "managed" / "run-1"
    _init_git_repo(primary)
    _git("worktree", "add", "-q", "-b", "linked-newline", str(linked), "HEAD", cwd=primary)
    primary_workspace = primary / "packages" / "app"
    linked_workspace = linked / "packages" / "app"
    primary_workspace.mkdir(parents=True)
    linked_workspace.mkdir(parents=True)
    (generated / "packages" / "app").mkdir(parents=True)
    task_workspace = _workspace(
        durable_id="run-newline",
        worktree_path=generated,
        repo_root=linked,
    )

    direct = _runner(cwd=str(primary_workspace))._project_identity()
    linked_direct = _runner(cwd=str(linked_workspace))._project_identity()
    managed = _runner(task_workspace=task_workspace)._project_identity()

    assert direct == linked_direct == managed
    assert direct is not None
    persisted = _runner(cwd=str(linked_workspace))._build_execution_contract(
        seed=_seed(),
        project_identity=direct,
    )
    resumed = _runner(cwd=str(linked_workspace))

    changed = resumed._restore_execution_contract(
        {
            EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
            SESSION_START_IDENTITY_PROGRESS_KEY: direct.to_event_data(),
        },
        seed=_seed(),
    )

    assert changed is False
    assert resumed._project_identity() == direct


def test_nested_bare_direct_linked_and_managed_identity_survives_resume(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    source = tmp_path / "source"
    common_git = outer / "vendor" / "source.git"
    linked = tmp_path / "linked"
    generated = tmp_path / "managed" / "run-1"
    _init_git_repo(outer)
    _init_git_repo(source)
    common_git.parent.mkdir()
    generated.mkdir(parents=True)
    _git("clone", "-q", "--bare", str(source), str(common_git))
    _git(
        f"--git-dir={common_git}",
        "worktree",
        "add",
        "-q",
        "-b",
        "linked",
        str(linked),
        "HEAD",
    )
    task_workspace = TaskWorkspace(
        durable_id="run-1",
        repo_root=str(common_git),
        repo_name=common_git.name,
        original_cwd=str(common_git),
        effective_cwd=str(generated),
        worktree_path=str(generated),
        branch="ooo/run-1",
        lock_path=str(tmp_path / ".locks" / "run-1.json"),
    )

    direct = _runner(cwd=str(common_git))._project_identity()
    linked_direct = _runner(cwd=str(linked))._project_identity()
    managed = _runner(task_workspace=task_workspace)._project_identity()

    assert direct == linked_direct == managed
    assert direct is not None
    persisted = _runner(cwd=str(linked))._build_execution_contract(
        seed=_seed(),
        project_identity=direct,
    )
    resumed = _runner(cwd=str(linked))

    changed = resumed._restore_execution_contract(
        {
            EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
            SESSION_START_IDENTITY_PROGRESS_KEY: direct.to_event_data(),
        },
        seed=_seed(),
    )

    assert changed is False
    assert resumed._project_identity() == direct


def test_external_dot_git_explicit_owner_survives_managed_resume(tmp_path: Path) -> None:
    common_git = tmp_path / "storage" / ".git"
    common_git.parent.mkdir()
    holder = tmp_path / "holder"
    holder.mkdir()
    _git("init", "-q", f"--separate-git-dir={common_git}", str(holder))
    _git(
        "-c",
        "user.name=Routing Contract Test",
        "-c",
        "user.email=routing-contract@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "initial",
        cwd=holder,
    )
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "linked", str(linked), "HEAD", cwd=holder)
    primary = tmp_path / "primary"
    primary_workspace = primary / "packages" / "app"
    primary_workspace.mkdir(parents=True)
    (primary / ".git").write_text(f"gitdir: {common_git}\n", encoding="utf-8")
    _git("config", "core.worktree", str(primary), cwd=holder)
    linked_workspace = linked / "packages" / "app"
    linked_workspace.mkdir(parents=True)
    generated = tmp_path / "managed" / "run-1"
    (generated / "packages" / "app").mkdir(parents=True)
    task_workspace = _workspace(
        durable_id="run-1",
        worktree_path=generated,
        repo_root=linked,
    )

    direct = _runner(cwd=str(primary_workspace))._project_identity()
    linked_direct = _runner(cwd=str(linked_workspace))._project_identity()
    original = _runner(task_workspace=task_workspace)
    managed = original._project_identity()

    assert direct == linked_direct == managed
    assert managed is not None
    assert managed.project_root == str(primary.resolve())
    persisted = original._build_execution_contract(
        seed=_seed(),
        project_identity=managed,
    )
    resumed = _runner(task_workspace=task_workspace)

    changed = resumed._restore_execution_contract(
        {
            EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
            SESSION_START_IDENTITY_PROGRESS_KEY: {
                "execution_id": "anchored-external-dot-git",
                "seed_id": "seed-routing-contract",
                **managed.to_event_data(),
            },
        },
        seed=_seed(),
    )

    assert changed is False
    assert resumed._project_identity() == managed


def test_standard_dot_git_explicit_owner_survives_managed_resume(tmp_path: Path) -> None:
    holder = tmp_path / "holder"
    _init_git_repo(holder)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "linked", str(linked), "HEAD", cwd=holder)
    primary = tmp_path / "primary"
    primary_workspace = primary / "packages" / "app"
    primary_workspace.mkdir(parents=True)
    (primary / ".git").write_text(f"gitdir: {holder / '.git'}\n", encoding="utf-8")
    _git("config", "core.worktree", str(primary), cwd=holder)
    linked_workspace = linked / "packages" / "app"
    linked_workspace.mkdir(parents=True)
    generated = tmp_path / "managed" / "run-1"
    (generated / "packages" / "app").mkdir(parents=True)
    task_workspace = _workspace(
        durable_id="run-1",
        worktree_path=generated,
        repo_root=linked,
    )

    direct = _runner(cwd=str(primary_workspace))._project_identity()
    linked_direct = _runner(cwd=str(linked_workspace))._project_identity()
    original = _runner(task_workspace=task_workspace)
    managed = original._project_identity()

    assert direct == linked_direct == managed
    assert managed is not None
    assert managed.project_root == str(primary.resolve())
    persisted = original._build_execution_contract(
        seed=_seed(),
        project_identity=managed,
    )
    resumed = _runner(task_workspace=task_workspace)

    changed = resumed._restore_execution_contract(
        {
            EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
            SESSION_START_IDENTITY_PROGRESS_KEY: {
                "execution_id": "anchored-standard-dot-git",
                "seed_id": "seed-routing-contract",
                **managed.to_event_data(),
            },
        },
        seed=_seed(),
    )

    assert changed is False
    assert resumed._project_identity() == managed


def test_unowned_gitfile_cannot_resume_as_its_target_repository(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    unowned = tmp_path / "unowned"
    _init_git_repo(owner)
    unowned.mkdir()
    (unowned / ".git").write_text(f"gitdir: {owner / '.git'}\n", encoding="utf-8")
    original = _runner(cwd=str(owner))
    identity = original._project_identity()
    assert identity is not None
    persisted = original._build_execution_contract(
        seed=_seed(),
        project_identity=identity,
    )

    with pytest.raises(OrchestratorError, match="conflicting project identity"):
        _runner(cwd=str(unowned))._restore_execution_contract(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
                SESSION_START_IDENTITY_PROGRESS_KEY: identity.to_event_data(),
            },
            seed=_seed(),
        )


def test_pre_anchor_managed_linked_override_survives_two_resumes(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    _init_git_repo(primary)
    _git("worktree", "add", "-q", "-b", "linked", str(linked), "HEAD", cwd=primary)
    (linked / "packages" / "app").mkdir(parents=True)
    generated = tmp_path / "managed" / "legacy-run"
    (generated / "packages" / "app").mkdir(parents=True)
    task_workspace = _workspace(
        durable_id="legacy-run",
        worktree_path=generated,
        repo_root=linked,
    )
    original = _runner(task_workspace=task_workspace)
    _use_frontier_custom_routing(original)
    persisted = original._build_execution_contract(seed=_seed())
    persisted["frugality_proof"]["project_root"] = str(linked.resolve())
    persisted["frugality_proof"]["workspace_path"] = "packages/app"
    legacy_start = {
        "execution_id": "legacy-managed-linked",
        "seed_id": "seed-routing-contract",
    }

    first_resume = _runner(
        task_workspace=task_workspace,
        base_model_tier="standard",
    )
    first_resume._route_economics = _frontier_custom_economics()
    first_resume._model_router = _frontier_custom_router(base_tier="standard")
    assert first_resume._restore_execution_contract(
        {
            EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
            SESSION_START_IDENTITY_PROGRESS_KEY: legacy_start,
        },
        seed=_seed(),
    )
    replacement = first_resume._execution_contract
    assert replacement is not None
    assert replacement["frugality_proof"]["project_root"] == str(linked.resolve())
    assert replacement["frugality_proof"]["workspace_path"] == "packages/app"

    second_resume = _runner(task_workspace=task_workspace)
    second_resume._route_economics = _frontier_custom_economics()
    second_resume._model_router = _frontier_custom_router(base_tier="standard")
    second_resume._requested_model_tier = "standard"

    assert (
        second_resume._restore_execution_contract(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: replacement,
                SESSION_START_IDENTITY_PROGRESS_KEY: legacy_start,
            },
            seed=_seed(),
        )
        is False
    )


def test_direct_nested_checkout_uses_same_project_identity_contract(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    workspace = repo_root / "packages" / "app"
    workspace.mkdir(parents=True)
    _init_git_repo(repo_root)
    runner = _runner(cwd=str(workspace))

    identity = runner._project_identity()
    proof = runner._build_execution_contract(
        seed=_seed(),
        project_identity=identity,
    )["frugality_proof"]

    assert identity is not None
    assert identity.project_id == project_id_for_root(repo_root)
    assert proof["project_root"] == identity.project_root
    assert proof["workspace_path"] == identity.workspace_path == "packages/app"


def test_pre_anchor_nested_contract_resumes_with_legacy_cwd_identity(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    workspace = repo_root / "packages" / "app"
    workspace.mkdir(parents=True)
    _init_git_repo(repo_root)
    original = _runner(cwd=str(workspace))
    persisted = original._build_execution_contract(seed=_seed())
    persisted["frugality_proof"]["project_root"] = str(workspace.resolve())
    persisted["frugality_proof"]["workspace_path"] = "."
    resumed = _runner(cwd=str(workspace))

    changed = resumed._restore_execution_contract(
        {
            EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
            SESSION_START_IDENTITY_PROGRESS_KEY: {
                "execution_id": "legacy-exec",
                "seed_id": "seed-routing-contract",
            },
        },
        seed=_seed(),
    )

    assert changed is False


def test_project_anchor_binds_nested_contract_and_current_workspace(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    workspace = repo_root / "packages" / "app"
    workspace.mkdir(parents=True)
    _init_git_repo(repo_root)
    original = _runner(cwd=str(workspace))
    identity = original._project_identity()
    assert identity is not None
    persisted = original._build_execution_contract(
        seed=_seed(),
        project_identity=identity,
    )
    resumed = _runner(cwd=str(workspace))

    changed = resumed._restore_execution_contract(
        {
            EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
            SESSION_START_IDENTITY_PROGRESS_KEY: {
                "execution_id": "anchored-exec",
                "seed_id": "seed-routing-contract",
                **identity.to_event_data(),
            },
        },
        seed=_seed(),
    )

    assert changed is False


@pytest.mark.parametrize(
    "start_identity",
    [
        {"project_id": "project_0123456789abcdef0123456789abcdef"},
        {
            "project_id": "project_invalid",
            "project_root": "/tmp/project",
            "workspace_path": ".",
        },
    ],
)
def test_project_anchor_rejects_partial_or_invalid_identity(
    start_identity: dict[str, str],
) -> None:
    runner = _runner()
    persisted = runner._build_execution_contract(seed=_seed())

    with pytest.raises(OrchestratorError, match="project identity anchor"):
        runner._restore_execution_contract(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
                SESSION_START_IDENTITY_PROGRESS_KEY: start_identity,
            },
            seed=_seed(),
        )


def test_project_anchor_rejects_nested_contract_conflict(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    runner = _runner(cwd=str(repo_root))
    identity = runner._project_identity()
    assert identity is not None
    persisted = runner._build_execution_contract(
        seed=_seed(),
        project_identity=identity,
    )
    persisted["frugality_proof"]["workspace_path"] = "packages/other"

    with pytest.raises(OrchestratorError, match="conflicting project identity"):
        runner._restore_execution_contract(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
                SESSION_START_IDENTITY_PROGRESS_KEY: identity.to_event_data(),
            },
            seed=_seed(),
        )


def test_project_anchor_rejects_current_project_change(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _init_git_repo(first_root)
    _init_git_repo(second_root)
    original = _runner(cwd=str(first_root))
    identity = original._project_identity()
    assert identity is not None
    persisted = original._build_execution_contract(
        seed=_seed(),
        project_identity=identity,
    )
    resumed = _runner(cwd=str(second_root))

    with pytest.raises(OrchestratorError, match="conflicting project identity"):
        resumed._restore_execution_contract(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
                SESSION_START_IDENTITY_PROGRESS_KEY: identity.to_event_data(),
            },
            seed=_seed(),
        )


def test_project_anchor_rejects_deleted_current_workspace(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    original = _runner(cwd=str(repo_root))
    identity = original._project_identity()
    assert identity is not None
    persisted = original._build_execution_contract(
        seed=_seed(),
        project_identity=identity,
    )
    repo_root.rename(tmp_path / "moved-repo")
    resumed = _runner(cwd=str(repo_root))

    with pytest.raises(OrchestratorError, match="Cannot resolve project identity"):
        resumed._restore_execution_contract(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
                SESSION_START_IDENTITY_PROGRESS_KEY: identity.to_event_data(),
            },
            seed=_seed(),
        )


@pytest.mark.asyncio
async def test_start_event_contract_is_resume_fallback_without_progress_row() -> None:
    runner = _runner()
    contract = runner._build_execution_contract(seed=_seed())
    start = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="sess-start-only",
        data={
            "execution_id": "exec-start-only",
            "seed_id": "seed-routing-contract",
            "execution_contract": contract,
        },
    )
    store = AsyncMock()
    store.replay.return_value = [start]
    store.query_session_related_events.return_value = []

    result = await SessionRepository(store).reconstruct_session("sess-start-only")

    assert result.is_ok
    assert result.value.progress[EXECUTION_CONTRACT_PROGRESS_KEY] == contract
    assert result.value.progress[SESSION_START_IDENTITY_PROGRESS_KEY] == {
        "execution_id": "exec-start-only",
        "seed_id": "seed-routing-contract",
    }


@pytest.mark.asyncio
async def test_legacy_start_identity_survives_progress_replay() -> None:
    start = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="legacy-start-identity",
        data={
            "execution_id": "legacy-exec",
            "seed_id": "seed-routing-contract",
            "seed_goal": "Prove durable routing",
            "runtime_backend": "claude",
            "llm_backend": "anthropic",
        },
    )
    overwrite_attempt = BaseEvent(
        type="orchestrator.progress.updated",
        aggregate_type="session",
        aggregate_id="legacy-start-identity",
        data={
            "progress": {
                SESSION_START_IDENTITY_PROGRESS_KEY: {
                    "seed_id": "tampered",
                    "runtime_backend": "codex_cli",
                }
            }
        },
    )
    store = AsyncMock()
    store.replay.return_value = [start, overwrite_attempt]
    store.query_session_related_events.return_value = []

    result = await SessionRepository(store).reconstruct_session("legacy-start-identity")

    assert result.is_ok
    assert result.value.progress[SESSION_START_IDENTITY_PROGRESS_KEY] == {
        "execution_id": "legacy-exec",
        "seed_id": "seed-routing-contract",
        "seed_goal": "Prove durable routing",
        "runtime_backend": "claude",
        "llm_backend": "anthropic",
    }


@pytest.mark.asyncio
async def test_project_start_anchor_survives_progress_replay() -> None:
    start = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="project-start-identity",
        data={
            "execution_id": "project-exec",
            "seed_id": "seed-routing-contract",
            "project_id": "project_0123456789abcdef0123456789abcdef",
            "project_root": "/tmp/project",
            "workspace_path": "packages/app",
        },
    )
    overwrite_attempt = BaseEvent(
        type="orchestrator.progress.updated",
        aggregate_type="session",
        aggregate_id="project-start-identity",
        data={
            "progress": {
                SESSION_START_IDENTITY_PROGRESS_KEY: {
                    "project_id": "project_tampered",
                    "project_root": "/tmp/other",
                    "workspace_path": ".",
                }
            }
        },
    )
    store = AsyncMock()
    store.replay.return_value = [start, overwrite_attempt]
    store.query_session_related_events.return_value = []

    result = await SessionRepository(store).reconstruct_session("project-start-identity")

    assert result.is_ok
    assert result.value.progress[SESSION_START_IDENTITY_PROGRESS_KEY] == start.data


@pytest.mark.asyncio
async def test_runtime_resume_identity_conflict_is_preserved_from_event_history() -> None:
    start = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="runtime-identity-conflict",
        data={
            "execution_id": "runtime-identity-exec",
            "seed_id": "seed-routing-contract",
        },
    )
    first = BaseEvent(
        type="orchestrator.progress.updated",
        aggregate_type="session",
        aggregate_id="runtime-identity-conflict",
        data={
            "progress": {
                "runtime": RuntimeHandle(
                    backend="codex_cli",
                    native_session_id="thread-original",
                ).to_persisted_dict()
            }
        },
    )
    conflicting = BaseEvent(
        type="orchestrator.progress.updated",
        aggregate_type="session",
        aggregate_id="runtime-identity-conflict",
        data={
            "progress": {
                SESSION_RUNTIME_IDENTITY_PROGRESS_KEY: {
                    "status": "bound",
                    "backend": "codex_cli",
                    "id_kind": "native_session_id",
                    "id": "forged-reserved-value",
                },
                "runtime": RuntimeHandle(
                    backend="codex_cli",
                    native_session_id="thread-injected",
                ).to_persisted_dict(),
            }
        },
    )
    store = AsyncMock()
    store.replay.return_value = [start, first, conflicting]
    store.query_session_related_events.return_value = []

    result = await SessionRepository(store).reconstruct_session("runtime-identity-conflict")

    assert result.is_ok
    assert result.value.progress[SESSION_RUNTIME_IDENTITY_PROGRESS_KEY] == {
        "status": "conflict",
        "first": {
            "status": "bound",
            "backend": "codex_cli",
            "id_kind": "native_session_id",
            "id": "thread-original",
        },
        "later": {
            "status": "bound",
            "backend": "codex_cli",
            "id_kind": "native_session_id",
            "id": "thread-injected",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_contract", [None, "corrupt-contract"])
async def test_malformed_start_contract_is_preserved_and_rejected(
    malformed_contract: object,
) -> None:
    start = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="sess-malformed-start",
        data={
            "execution_id": "exec-malformed-start",
            "seed_id": "seed-routing-contract",
            "execution_contract": malformed_contract,
        },
    )
    store = AsyncMock()
    store.replay.return_value = [start]
    store.query_session_related_events.return_value = []

    result = await SessionRepository(store).reconstruct_session("sess-malformed-start")

    assert result.is_ok
    assert EXECUTION_CONTRACT_PROGRESS_KEY in result.value.progress
    assert result.value.progress[EXECUTION_CONTRACT_PROGRESS_KEY] == malformed_contract
    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        _runner()._restore_execution_contract(result.value.progress, seed=_seed())


@pytest.mark.asyncio
async def test_progress_omission_preserves_start_event_execution_contract() -> None:
    runner = _runner()
    contract = runner._build_execution_contract(seed=_seed())
    start = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="sess-progress-omits-contract",
        data={
            "execution_id": "exec-progress-omits-contract",
            "seed_id": "seed-routing-contract",
            "execution_contract": contract,
        },
    )
    progress = BaseEvent(
        type="orchestrator.progress.updated",
        aggregate_type="session",
        aggregate_id="sess-progress-omits-contract",
        data={"progress": {"messages_processed": 3}},
    )
    store = AsyncMock()
    store.replay.return_value = [start, progress]
    store.query_session_related_events.return_value = []

    result = await SessionRepository(store).reconstruct_session("sess-progress-omits-contract")

    assert result.is_ok
    assert result.value.progress[EXECUTION_CONTRACT_PROGRESS_KEY] == contract


@pytest.mark.asyncio
async def test_corrupt_progress_contract_does_not_downgrade_to_legacy_resume() -> None:
    runner = _runner()
    contract = runner._build_execution_contract(seed=_seed())
    start = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="sess-corrupt-contract",
        data={
            "execution_id": "exec-corrupt-contract",
            "seed_id": "seed-routing-contract",
            "execution_contract": contract,
        },
    )
    corrupt_progress = BaseEvent(
        type="orchestrator.progress.updated",
        aggregate_type="session",
        aggregate_id="sess-corrupt-contract",
        data={"progress": {EXECUTION_CONTRACT_PROGRESS_KEY: None}},
    )
    store = AsyncMock()
    store.replay.return_value = [start, corrupt_progress]
    store.query_session_related_events.return_value = []

    reconstructed = await SessionRepository(store).reconstruct_session("sess-corrupt-contract")

    assert reconstructed.is_ok
    assert EXECUTION_CONTRACT_PROGRESS_KEY in reconstructed.value.progress
    assert reconstructed.value.progress[EXECUTION_CONTRACT_PROGRESS_KEY] is None
    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        runner._restore_execution_contract(reconstructed.value.progress, seed=_seed())


@pytest.mark.asyncio
async def test_prepare_session_persists_same_execution_contract_in_start_and_progress() -> None:
    store = AsyncMock()
    events = []

    async def _append(event) -> None:
        events.append(event)

    store.append.side_effect = _append
    runner = OrchestratorRunner(_adapter(), store, MagicMock())
    runner._model_router = _frontier_custom_router()
    seed = _seed(criterion="Routing survives resume")

    result = await runner.prepare_session(
        seed,
        execution_id="exec-routing-contract",
        session_id="sess-routing-contract",
    )

    assert result.is_ok
    start = next(event for event in events if event.type == "orchestrator.session.started")
    progress = next(event for event in events if event.type == "orchestrator.progress.updated")
    persisted = start.data[EXECUTION_CONTRACT_PROGRESS_KEY]
    assert progress.data["progress"][EXECUTION_CONTRACT_PROGRESS_KEY] == persisted
    assert persisted["model_routing"]["router"]["base_tier"] == "frontier"
    assert persisted["frugality_proof"]["project_root"] == str(Path("/tmp/project").resolve())
    assert len(persisted["frugality_proof"]["routing_fingerprint"]) == 64
    assert len(persisted["frugality_proof"]["seed_fingerprint"]) == 64
