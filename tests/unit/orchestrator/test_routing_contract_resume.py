"""Durable model-routing and proof-cohort execution contracts."""

from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.config.models import (
    EconomicsConfig,
    ModelConfig,
    OuroborosConfig,
    TierConfig,
    get_default_config,
)
from ouroboros.core.project_identity import ProjectIdentity, project_id_for_root
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
from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime
from ouroboros.orchestrator.execution_semantics import CURRENT_EXECUTION_SEMANTICS_VERSION
from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
from ouroboros.orchestrator.goose_runtime import GooseCliRuntime
from ouroboros.orchestrator.grok_cli_runtime import GrokCliRuntime
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
from ouroboros.orchestrator.runtime_factory import create_agent_runtime
from ouroboros.orchestrator.session import (
    SESSION_RUNTIME_IDENTITY_PROGRESS_KEY,
    SESSION_START_IDENTITY_PROGRESS_KEY,
    SessionRepository,
)
from ouroboros.orchestrator.zcode_cli_runtime import ZcodeCLIRuntime


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
    if not (repo_root / ".git").exists():
        _init_git_repo(repo_root)
    (repo_root / "packages" / "app").mkdir(parents=True, exist_ok=True)
    _git(
        "worktree",
        "add",
        "-q",
        "-b",
        f"ooo/{durable_id}",
        str(worktree_path),
        "HEAD",
        cwd=repo_root,
    )
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


def _assert_runtime_identity_observed(contract: dict[str, object]) -> dict[str, object]:
    routing = contract["model_routing"]
    assert isinstance(routing, dict)
    runtime_execution = routing["runtime_execution"]
    assert isinstance(runtime_execution, dict)
    assert runtime_execution["version"] == 1
    assert runtime_execution["observed"] is True
    identity = runtime_execution["identity"]
    assert isinstance(identity, dict)
    return identity


@pytest.fixture(autouse=True)
def _clear_model_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING", raising=False)
    monkeypatch.delenv("OUROBOROS_EXECUTION_MODEL", raising=False)
    monkeypatch.setattr("ouroboros.config.get_execution_model", lambda: None)
    monkeypatch.setattr("ouroboros.config.load_config", get_default_config)


def test_execution_contract_requires_explicit_project_identity() -> None:
    """#1798: the self-resolving sentinel default is gone.

    Omitting project_identity used to silently trigger a Git-subprocess
    resolution through a private sentinel that no call site could see. The
    parameter is required now, so an implicit call fails at the call site
    instead of hiding a resolution cost.
    """
    with pytest.raises(TypeError, match="project_identity"):
        _runner()._build_execution_contract(seed=_seed())


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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )

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
    persisted = copy.deepcopy(
        original._build_execution_contract(
            project_identity=original._project_identity(), seed=_seed()
        )
    )
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
    persisted = copy.deepcopy(
        original._build_execution_contract(
            project_identity=original._project_identity(),
        )
    )
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
    persisted = copy.deepcopy(
        original._build_execution_contract(
            project_identity=original._project_identity(), seed=_seed()
        )
    )
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
    persisted = copy.deepcopy(
        original._build_execution_contract(
            project_identity=original._project_identity(), seed=_seed()
        )
    )
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
    persisted = copy.deepcopy(
        original._build_execution_contract(
            project_identity=original._project_identity(), seed=_seed()
        )
    )
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
    persisted = copy.deepcopy(
        _runner()._build_execution_contract(
            project_identity=_runner()._project_identity(), seed=_seed()
        )
    )
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
    contract = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=_seed()
    )

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
    contract = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=seed
    )

    inputs = contract["execution_inputs"]
    semantics = contract["execution_semantics"]
    assert inputs["schema_version"] == 2
    assert semantics["version"] == 7
    assert semantics["verify_shell_identity"] is None or isinstance(
        semantics["verify_shell_identity"], dict
    )
    assert semantics["adaptive_concurrency_policy"] == {
        "algorithm": "aimd/v2",
        "admission_scope": "provider_call",
        "initial_limit": semantics["effective_parallel_workers"],
        "max_limit": semantics["max_parallel_workers"],
        "successes_before_increase": 3,
        "decrease_ratio": "1/2",
        "max_retry_after_seconds": 86400,
    }
    assert semantics["usage_limit_pause_seconds"] == 18000
    assert semantics["runtime_effect_capabilities"]["version"] == 2
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
        runner._build_execution_contract(project_identity=None, seed=_seed())


@pytest.mark.parametrize(
    "field",
    [
        "context_pack_fragment",
        "execution_profile_json",
        "inherited_runtime_handle_json",
    ],
)
def test_current_execution_inputs_require_complete_effect_population(field: str) -> None:
    persisted = copy.deepcopy(
        _runner()._build_execution_contract(
            project_identity=_runner()._project_identity(), seed=_seed()
        )
    )
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
    contract = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=research_seed
    )

    strategy = runner._execution_strategy_snapshot(contract, require_bound=True)

    assert "Edit" not in strategy.get_tools()
    assert "research" in strategy.get_system_prompt_fragment().lower()


def test_complete_tool_catalog_drift_is_rejected_before_resume_dispatch() -> None:
    from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

    runner = _runner()
    research_seed = _seed().model_copy(update={"task_type": "research"})
    contract = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=research_seed
    )
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
    persisted = copy.deepcopy(
        original._build_execution_contract(
            project_identity=original._project_identity(),
        )
    )
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
    baseline = runner._build_execution_contract(
        project_identity=runner._project_identity(),
    )
    baseline_routing_fingerprint = baseline["frugality_proof"]["routing_fingerprint"]

    runner._run_verify_commands = False
    runner._verify_command_timeout_seconds = 7
    runner._ac_retry_attempts = 9
    runner._enable_decomposition = False
    runner._decomposition_mode = "off"
    runner._max_decomposition_depth = 7
    runner._fat_harness_mode = True
    changed = runner._build_execution_contract(
        project_identity=runner._project_identity(),
    )

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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )

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


def test_resume_fails_closed_for_pre_adaptive_v3_semantics() -> None:
    """A static-semaphore session cannot silently resume under AIMD effects."""
    persisted = copy.deepcopy(
        _runner()._build_execution_contract(project_identity=None, seed=_seed())
    )
    semantics = persisted["execution_semantics"]
    semantics["version"] = 3
    del semantics["adaptive_concurrency_policy"]
    persisted["frugality_proof"]["execution_semantics_fingerprint"] = (
        OrchestratorRunner._execution_semantics_fingerprint(semantics)
    )

    with pytest.raises(OrchestratorError, match="pre-adaptive") as exc_info:
        _runner()._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )

    assert exc_info.value.details["resume_blocked"] == ("adaptive_concurrency_policy_unavailable")
    assert exc_info.value.details["retryable"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("algorithm", "aimd/v0"),
        ("admission_scope", "ac_root"),
        ("initial_limit", 99),
        ("max_limit", 99),
        ("successes_before_increase", 4),
        ("decrease_ratio", "2/3"),
        ("max_retry_after_seconds", 10**1000),
    ],
)
def test_resume_rejects_adaptive_policy_drift(field: str, value: object) -> None:
    persisted = copy.deepcopy(
        _runner()._build_execution_contract(project_identity=None, seed=_seed())
    )
    persisted["execution_semantics"]["adaptive_concurrency_policy"][field] = value
    persisted["frugality_proof"]["execution_semantics_fingerprint"] = (
        OrchestratorRunner._execution_semantics_fingerprint(persisted["execution_semantics"])
    )

    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        _runner()._restore_execution_contract(
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
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
    persisted = _runner()._build_execution_contract(
        project_identity=_runner()._project_identity(), seed=_seed()
    )
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
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
        "verify_shell_identity",
        "ac_retry_attempts",
        "cross_harness_redispatch",
        "enable_decomposition",
        "decomposition_mode",
        "max_decomposition_depth",
        "max_parallel_workers",
        "effective_parallel_workers",
        "adaptive_concurrency_policy",
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
    persisted = copy.deepcopy(
        original._build_execution_contract(
            project_identity=original._project_identity(),
        )
    )
    del persisted["execution_semantics"][field]
    persisted["frugality_proof"]["execution_semantics_fingerprint"] = (
        OrchestratorRunner._execution_semantics_fingerprint(persisted["execution_semantics"])
    )

    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        _runner()._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_v4_execution_semantics_migrate_to_unavailable_verify_shell() -> None:
    original = _runner()
    persisted = copy.deepcopy(
        original._build_execution_contract(project_identity=original._project_identity())
    )
    semantics = persisted["execution_semantics"]
    semantics["version"] = 4
    del semantics["verify_shell_identity"]
    persisted["frugality_proof"]["execution_semantics_fingerprint"] = (
        OrchestratorRunner._execution_semantics_fingerprint(semantics)
    )

    resumed = _runner()
    assert (
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted}) is False
    )
    migrated = resumed._execution_contract["execution_semantics"]
    assert migrated["version"] == CURRENT_EXECUTION_SEMANTICS_VERSION
    assert migrated["verify_shell_identity"] is None


def test_v5_lexical_shell_path_migrates_to_unavailable_identity() -> None:
    original = _runner()
    persisted = copy.deepcopy(
        original._build_execution_contract(project_identity=original._project_identity())
    )
    semantics = persisted["execution_semantics"]
    semantics["version"] = 5
    del semantics["verify_shell_identity"]
    semantics["verify_shell_path"] = "/legacy/bash"
    persisted["frugality_proof"]["execution_semantics_fingerprint"] = (
        OrchestratorRunner._execution_semantics_fingerprint(semantics)
    )

    resumed = _runner()
    assert (
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted}) is False
    )
    migrated = resumed._execution_contract["execution_semantics"]
    assert migrated["version"] == CURRENT_EXECUTION_SEMANTICS_VERSION
    assert migrated["verify_shell_identity"] is None


def test_resume_rejects_changed_verify_shell_authority() -> None:
    original = _runner()
    original._verify_shell_identity = {
        "path": "/trusted/bash",
        "realpath": "/trusted/bash",
        "sha256": "0" * 64,
    }
    persisted = original._build_execution_contract(project_identity=original._project_identity())

    resumed = _runner()
    resumed._verify_shell_identity = {
        "path": "/different/bash",
        "realpath": "/different/bash",
        "sha256": "1" * 64,
    }
    with pytest.raises(OrchestratorError, match="changed execution semantics"):
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_current_execution_semantics_migrates_retired_preflight_authority_once() -> None:
    persisted = copy.deepcopy(
        _runner()._build_execution_contract(
            project_identity=_runner()._project_identity(),
        )
    )
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
    persisted = copy.deepcopy(
        _runner()._build_execution_contract(
            project_identity=_runner()._project_identity(),
        )
    )
    persisted["execution_semantics"]["decomposition_mode"] = "preflight"

    with pytest.raises(OrchestratorError, match="invalid legacy preflight contract"):
        _runner()._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


@pytest.mark.parametrize(
    "field",
    [
        "targeted_resume",
        "empty_tool_restriction_support",
        "reasoning_effort_support",
        "enforceable_reasoning_efforts",
        "model_override_support",
        "session_signals",
    ],
)
def test_current_runtime_effect_capabilities_require_complete_exact_population(
    field: str,
) -> None:
    persisted = copy.deepcopy(
        _runner()._build_execution_contract(
            project_identity=_runner()._project_identity(),
        )
    )
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
    persisted = copy.deepcopy(
        _runner()._build_execution_contract(
            project_identity=_runner()._project_identity(),
        )
    )
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
    persisted = copy.deepcopy(
        original._build_execution_contract(
            project_identity=original._project_identity(),
        )
    )
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

    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )
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

        contract = runner._build_execution_contract(
            project_identity=runner._project_identity(),
        )

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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )
    route_compat = persisted["model_routing"]["route_compat"]
    assert route_compat["projection"]["effort"] == persisted_effort

    resumed = _runner()
    resumed._reasoning_effort = current_effort

    with pytest.raises(OrchestratorError, match="different reasoning effort"):
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_resume_restores_persisted_kill_switch() -> None:
    original = _runner()
    original._model_router = None
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )

    resumed = _runner()
    assert resumed._model_router is not None

    resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert resumed._model_router is None


def test_dormant_model_routing_still_rejects_reasoning_effort_drift() -> None:
    original = _runner()
    original._model_router = None
    original._reasoning_effort = "low"
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )
    routing = persisted["model_routing"]
    assert routing["route_compat"] == {"version": 1, "enabled": False}
    assert routing["reasoning_effort"] == "low"

    resumed = _runner()
    resumed._reasoning_effort = "high"

    with pytest.raises(OrchestratorError, match="different reasoning effort"):
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_resume_rejects_changed_base_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The effort used to form each dispatch is part of resume identity."""
    monkeypatch.setattr("ouroboros.config.get_agent_reasoning_effort", lambda: "low")
    original = _runner()
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )

    assert persisted["model_routing"]["base_reasoning_effort"] == "low"

    monkeypatch.setattr("ouroboros.config.get_agent_reasoning_effort", lambda: "high")
    resumed = _runner()
    with pytest.raises(OrchestratorError, match="different reasoning effort"):
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_legacy_contract_without_base_effort_migrates_dormant_effort() -> None:
    """A missing base effort can be recovered from the persisted routing effort."""
    original = _runner()
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )
    legacy_routing = dict(persisted["model_routing"])
    legacy_routing.pop("base_reasoning_effort")
    legacy_contract = {
        **persisted,
        "model_routing": legacy_routing,
        "frugality_proof": {
            **persisted["frugality_proof"],
            "routing_fingerprint": OrchestratorRunner._routing_fingerprint(legacy_routing),
        },
    }

    resumed = _runner()
    changed = resumed._restore_execution_contract(
        {EXECUTION_CONTRACT_PROGRESS_KEY: legacy_contract}
    )

    assert changed is True
    assert resumed._execution_contract is not None
    assert resumed._execution_contract["model_routing"]["base_reasoning_effort"] is None


def test_legacy_contract_without_base_effort_migrates_enabled_effort() -> None:
    """An old contract's routing effort is the historical base effort."""
    original = _runner()
    original._reasoning_effort = "high"
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )
    legacy_routing = dict(persisted["model_routing"])
    legacy_routing.pop("base_reasoning_effort")
    legacy_contract = {
        **persisted,
        "model_routing": legacy_routing,
        "frugality_proof": {
            **persisted["frugality_proof"],
            "routing_fingerprint": OrchestratorRunner._routing_fingerprint(legacy_routing),
        },
    }

    resumed = _runner()
    resumed._reasoning_effort = "high"
    changed = resumed._restore_execution_contract(
        {EXECUTION_CONTRACT_PROGRESS_KEY: legacy_contract}
    )

    assert changed is True
    assert resumed._execution_contract is not None
    assert resumed._execution_contract["model_routing"]["base_reasoning_effort"] == "high"


def test_legacy_contract_missing_base_effort_rejects_historical_high_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migrated historical effort still protects against current drift."""
    original = _runner()
    original._reasoning_effort = "high"
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )
    legacy_routing = dict(persisted["model_routing"])
    legacy_routing.pop("base_reasoning_effort")
    legacy_contract = {
        **persisted,
        "model_routing": legacy_routing,
        "frugality_proof": {
            **persisted["frugality_proof"],
            "routing_fingerprint": OrchestratorRunner._routing_fingerprint(legacy_routing),
        },
    }

    monkeypatch.setattr("ouroboros.config.get_agent_reasoning_effort", lambda: None)
    resumed = _runner()
    with pytest.raises(OrchestratorError, match="different reasoning effort"):
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: legacy_contract})
    assert resumed._execution_contract is None


def test_explicit_resume_tier_override_replaces_persisted_contract() -> None:
    original = _runner()
    _use_frontier_custom_routing(original)
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )

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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )

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
    persisted = copy.deepcopy(
        original._build_execution_contract(
            project_identity=original._project_identity(),
        )
    )
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
    persisted = copy.deepcopy(
        original._build_execution_contract(
            project_identity=original._project_identity(),
        )
    )
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(),
    )

    resumed = _runner()
    resumed._adapter.runtime_backend = "codex_cli"

    with pytest.raises(OrchestratorError, match="different runtime backend"):
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_explicit_tier_does_not_authorize_cross_backend_resume() -> None:
    original = _runner()
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )

    resumed = _runner(base_model_tier="standard")
    resumed._adapter.runtime_backend = "codex_cli"

    with pytest.raises(OrchestratorError, match="different runtime backend"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_explicit_tier_does_not_bypass_malformed_persisted_router() -> None:
    original = _runner()
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )

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
    original_contract = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )

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
    )._build_execution_contract(
        project_identity=OrchestratorRunner(
            resumed_runtime,
            AsyncMock(),
            MagicMock(),
        )._project_identity(),
        seed=_seed(),
    )

    _assert_runtime_identity_observed(original_contract)
    _assert_runtime_identity_observed(resumed_contract)
    assert (
        original_contract["foundation_a_authority"]["correlation_id"]
        != resumed_contract["foundation_a_authority"]["correlation_id"]
    )
    persisted_identity = original_contract["model_routing"]["runtime_execution"]["identity"]
    assert persisted_identity == {
        "cli_executable_path": str(Path("/bin/echo").absolute()),
        "cli_executable_content_sha256": original_runtime._cli_executable_content_identity(),
        "cli_executable_version": original_runtime._cli_executable_version_identity(),
        "builtin_mcp_handler_registry_fingerprint": (
            original_runtime._builtin_mcp_handler_registry_fingerprint
        ),
        "codex_config_fingerprint": original_runtime._codex_config_fingerprint,
        "codex_profile": "zep-proxy-a",
        "effective_model_observed": True,
        "fallback_model": "resolved-fallback-model",
        "fallback_profile": "zep-proxy-a",
        "kind": "codex_cli_v1",
        "llm_backend": "codex",
        "profile_resolution_fingerprint": (original_runtime._profile_resolution_fingerprint),
        "resume_handle_selector": {
            "backend": "codex_cli",
            "kind": "agent_runtime",
            "selectors": {},
        },
        "runtime_profile": "zep-runtime",
        "skill_dispatcher": "packaged",
        "skill_dispatcher_identity": "packaged",
        "skill_dispatch_registry_fingerprint": (
            original_runtime._skill_dispatch_registry_fingerprint
        ),
        "skills_dir": None,
        "startup_output_timeout_seconds": 60.0,
        "stdout_idle_timeout_seconds": 300.0,
    }
    resumed = OrchestratorRunner(resumed_runtime, AsyncMock(), MagicMock())
    with pytest.raises(OrchestratorError, match="different runtime execution profile"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: original_contract},
            seed=_seed(),
        )


def test_runner_rejects_untrusted_codex_runtime_subclass_identity() -> None:
    """Only exact built-in runtime classes may provide portable execution identity."""

    class SpoofedCodexRuntime(CodexCliRuntime):
        def execution_identity_contract(self) -> dict[str, object]:
            return {
                "kind": "codex_cli_v1",
                "effective_model_observed": True,
                "fallback_model": "spoofed",
            }

    runtime = SpoofedCodexRuntime(
        cli_path="/bin/echo",
        model="spoofed",
        cwd="/tmp/project",
    )
    runner = OrchestratorRunner(runtime, AsyncMock(), MagicMock())

    assert runner._runtime_execution_identity_contract() == {"version": 1, "observed": False}


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
    )._build_execution_contract(
        project_identity=OrchestratorRunner(
            original_runtime,
            AsyncMock(),
            MagicMock(),
        )._project_identity(),
        seed=_seed(),
    )

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
    )._build_execution_contract(
        project_identity=OrchestratorRunner(
            resumed_runtime,
            AsyncMock(),
            MagicMock(),
        )._project_identity(),
        seed=_seed(),
    )

    _assert_runtime_identity_observed(original_contract)
    _assert_runtime_identity_observed(resumed_contract)


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
    persisted = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=_seed()
    )

    _assert_runtime_identity_observed(persisted)


def test_automatic_codex_default_resume_requires_observed_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fingerprints alone do not prove the App/CLI-selected concrete model."""
    monkeypatch.setattr("ouroboros.config.get_execution_model", lambda: None)
    original_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    original_runtime._runtime_profile = None
    original_runtime._codex_profile = None
    original_runtime._resolved_fallback_model = None
    original_runtime._resolved_fallback_profile = None
    original = OrchestratorRunner(original_runtime, AsyncMock(), MagicMock())
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )

    assert original._model_router is None
    assert persisted["model_routing"]["constructor_model"] == {
        "observed": True,
        "model": None,
    }
    assert (
        persisted["model_routing"]["runtime_execution"]["identity"]["effective_model_observed"]
        is False
    )

    resumed_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    resumed_runtime._runtime_profile = None
    resumed_runtime._codex_profile = None
    resumed_runtime._resolved_fallback_model = None
    resumed_runtime._resolved_fallback_profile = None
    resumed = OrchestratorRunner(resumed_runtime, AsyncMock(), MagicMock())

    with pytest.raises(OrchestratorError, match="effective runtime model is unverifiable"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_automatic_codex_default_resume_rejects_a_different_executable_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The automatic default is bound to its resolved Codex executable."""
    monkeypatch.setattr("ouroboros.config.get_execution_model", lambda: None)
    first_cli = tmp_path / "codex-a"
    second_cli = tmp_path / "codex-b"
    for cli in (first_cli, second_cli):
        cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        cli.chmod(0o755)

    original_runtime = CodexCliRuntime(cli_path=first_cli, model=None, cwd="/tmp/project")
    original_runtime._runtime_profile = None
    original_runtime._codex_profile = None
    original_runtime._resolved_fallback_model = None
    original_runtime._resolved_fallback_profile = None
    persisted = OrchestratorRunner(
        original_runtime, AsyncMock(), MagicMock()
    )._build_execution_contract(
        project_identity=OrchestratorRunner(
            original_runtime, AsyncMock(), MagicMock()
        )._project_identity(),
        seed=_seed(),
    )

    persisted_identity = persisted["model_routing"]["runtime_execution"]["identity"]
    assert persisted_identity["cli_executable_path"] == str(first_cli.absolute())

    resumed_runtime = CodexCliRuntime(cli_path=second_cli, model=None, cwd="/tmp/project")
    resumed_runtime._runtime_profile = None
    resumed_runtime._codex_profile = None
    resumed_runtime._resolved_fallback_model = None
    resumed_runtime._resolved_fallback_profile = None
    resumed = OrchestratorRunner(resumed_runtime, AsyncMock(), MagicMock())

    with pytest.raises(OrchestratorError, match="different runtime execution profile"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_automatic_codex_default_resume_rejects_in_place_cli_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The same executable path must not conceal a changed Codex version."""
    monkeypatch.setattr("ouroboros.config.get_execution_model", lambda: None)
    cli = tmp_path / "codex"
    cli.write_text("#!/bin/sh\necho codex 1.0\n", encoding="utf-8")
    cli.chmod(0o755)

    original_runtime = CodexCliRuntime(cli_path=cli, model=None, cwd="/tmp/project")
    original_runtime._runtime_profile = None
    original_runtime._codex_profile = None
    original_runtime._resolved_fallback_model = None
    original_runtime._resolved_fallback_profile = None
    persisted = OrchestratorRunner(
        original_runtime, AsyncMock(), MagicMock()
    )._build_execution_contract(
        project_identity=OrchestratorRunner(
            original_runtime, AsyncMock(), MagicMock()
        )._project_identity(),
        seed=_seed(),
    )

    cli.write_text("#!/bin/sh\necho codex 2.0\n", encoding="utf-8")
    resumed_runtime = CodexCliRuntime(cli_path=cli, model=None, cwd="/tmp/project")
    resumed_runtime._runtime_profile = None
    resumed_runtime._codex_profile = None
    resumed_runtime._resolved_fallback_model = None
    resumed_runtime._resolved_fallback_profile = None

    with pytest.raises(OrchestratorError, match="different runtime execution profile"):
        OrchestratorRunner(resumed_runtime, AsyncMock(), MagicMock())._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_automatic_codex_default_resume_rejects_same_version_changed_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A wrapper with unchanged --version output must not hide changed bytes."""
    monkeypatch.setattr("ouroboros.config.get_execution_model", lambda: None)
    cli = tmp_path / "codex"
    cli.write_text("#!/bin/sh\n# one\necho codex 1.0\n", encoding="utf-8")
    cli.chmod(0o755)

    original_runtime = CodexCliRuntime(cli_path=cli, model=None, cwd="/tmp/project")
    original_runtime._runtime_profile = None
    original_runtime._codex_profile = None
    original_runtime._resolved_fallback_model = None
    original_runtime._resolved_fallback_profile = None
    persisted = OrchestratorRunner(
        original_runtime, AsyncMock(), MagicMock()
    )._build_execution_contract(
        project_identity=OrchestratorRunner(
            original_runtime, AsyncMock(), MagicMock()
        )._project_identity(),
        seed=_seed(),
    )

    cli.write_text("#!/bin/sh\n# two\necho codex 1.0\n", encoding="utf-8")
    resumed_runtime = CodexCliRuntime(cli_path=cli, model=None, cwd="/tmp/project")
    resumed_runtime._runtime_profile = None
    resumed_runtime._codex_profile = None
    resumed_runtime._resolved_fallback_model = None
    resumed_runtime._resolved_fallback_profile = None

    with pytest.raises(OrchestratorError, match="different runtime execution profile"):
        OrchestratorRunner(resumed_runtime, AsyncMock(), MagicMock())._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_non_codex_subclass_does_not_inherit_codex_profile_as_model_identity(
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
    persisted = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=_seed()
    )

    identity = _assert_runtime_identity_observed(persisted)
    assert identity["kind"] == "goose_v1"
    assert identity["fallback_model"] is None


def test_non_codex_runtime_identity_tracks_executable_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Trusted Codex-derived runtimes must bind resume identity to their executable."""
    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "off")
    first_cli = tmp_path / "gemini-a"
    second_cli = tmp_path / "gemini-b"
    first_cli.write_text("#!/bin/sh\necho gemini-a\n", encoding="utf-8")
    second_cli.write_text("#!/bin/sh\necho gemini-b\n", encoding="utf-8")
    first_cli.chmod(0o755)
    second_cli.chmod(0o755)
    true_runtime = GeminiCLIRuntime(cli_path=first_cli, model="gemini-pro", cwd="/tmp/project")
    false_runtime = GeminiCLIRuntime(cli_path=second_cli, model="gemini-pro", cwd="/tmp/project")

    true_identity = true_runtime.execution_identity_contract()
    false_identity = false_runtime.execution_identity_contract()

    assert true_identity["cli_executable_path"] == str(first_cli)
    assert false_identity["cli_executable_path"] == str(second_cli)
    assert true_identity != false_identity


def test_copilot_runtime_identity_tracks_native_agent_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copilot --agent selection must be identity-bearing and precede constructor model."""
    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "off")
    model_runtime = CopilotCliRuntime(
        cli_path="/bin/echo",
        model="claude-opus-4.6",
        cwd="/tmp/project",
    )
    agent_runtime = CopilotCliRuntime(
        cli_path="/bin/echo",
        model="claude-opus-4.6",
        cwd="/tmp/project",
        runtime_profile="worker",
    )

    model_identity = model_runtime.execution_identity_contract()
    agent_identity = agent_runtime.execution_identity_contract()

    assert model_identity["fallback_model"] == "claude-opus-4.6"
    assert model_identity["native_agent"] is None
    assert model_identity["effective_model_observed"] is True
    assert agent_identity["fallback_model"] is None
    assert agent_identity["native_agent"] == "ouroboros-worker"
    assert agent_identity["effective_model_observed"] is False
    assert model_identity != agent_identity


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
    persisted = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=_seed()
    )

    assert persisted["model_routing"]["constructor_model"] == {
        "observed": True,
        "model": None,
    }
    _assert_runtime_identity_observed(persisted)
    # ``default`` is the same automatic Codex sentinel as ``None``.  It has no
    # concrete constructor pin, so a durable resume must fail closed unless the
    # runtime later exposes the actual App/CLI-selected model.
    with pytest.raises(OrchestratorError, match="effective runtime model is unverifiable"):
        runner._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


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
    original_contract = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )

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
    )._build_execution_contract(
        project_identity=OrchestratorRunner(
            resumed_runtime,
            AsyncMock(),
            MagicMock(),
        )._project_identity(),
        seed=_seed(),
    )

    _assert_runtime_identity_observed(original_contract)
    _assert_runtime_identity_observed(resumed_contract)


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
    )._build_execution_contract(
        project_identity=OrchestratorRunner(
            original_runtime,
            AsyncMock(),
            MagicMock(),
        )._project_identity(),
        seed=_seed(),
    )

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
    )._build_execution_contract(
        project_identity=OrchestratorRunner(
            resumed_runtime,
            AsyncMock(),
            MagicMock(),
        )._project_identity(),
        seed=_seed(),
    )

    _assert_runtime_identity_observed(original_contract)
    _assert_runtime_identity_observed(resumed_contract)


def test_contract_build_records_codex_runtime_execution_identity() -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )

    contract = OrchestratorRunner(
        runtime,
        AsyncMock(),
        MagicMock(),
    )._build_execution_contract(
        project_identity=OrchestratorRunner(
            runtime,
            AsyncMock(),
            MagicMock(),
        )._project_identity(),
        seed=_seed(),
    )

    identity = _assert_runtime_identity_observed(contract)
    assert identity["kind"] == "codex_cli_v1"
    assert identity["cli_executable_path"] == str(Path("/bin/echo").absolute())


def test_resume_rejects_unobserved_runtime_identity_for_pinned_codex_v9() -> None:
    """Pre-change v9 contracts without historical execution identity fail closed."""
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model="gpt-5",
        cwd="/tmp/project",
    )
    original = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
    legacy_routing = {
        **persisted["model_routing"],
        "runtime_execution": {"version": 1, "observed": False},
    }
    legacy_contract = {
        **persisted,
        "model_routing": legacy_routing,
        "frugality_proof": {
            **persisted["frugality_proof"],
            "routing_fingerprint": OrchestratorRunner._routing_fingerprint(legacy_routing),
        },
    }

    with pytest.raises(
        OrchestratorError,
        match="Cannot resume with a different runtime execution profile",
    ):
        OrchestratorRunner(runtime, AsyncMock(), MagicMock())._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: legacy_contract},
            seed=_seed(),
        )


def test_codex_runtime_with_custom_skills_dir_is_not_portable_identity(tmp_path: Path) -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
        skills_dir=tmp_path,
    )

    contract = OrchestratorRunner(
        runtime,
        AsyncMock(),
        MagicMock(),
    )._runtime_execution_identity_contract()

    assert contract == {"version": 1, "observed": False}


def test_codex_runtime_with_custom_skill_dispatcher_is_not_portable_identity() -> None:
    async def _dispatcher(_intercept, _current_handle):
        return None

    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
        skill_dispatcher=_dispatcher,
    )

    contract = OrchestratorRunner(
        runtime,
        AsyncMock(),
        MagicMock(),
    )._runtime_execution_identity_contract()

    assert contract == {"version": 1, "observed": False}


def test_codex_runtime_with_spoofed_dispatcher_identity_is_not_portable() -> None:
    """Durable dispatcher trust must require the packaged dispatcher type."""

    class _SpoofedDispatcher:
        async def dispatch(self, _intercept, _current_handle):
            return None

        def stable_identity_contract(self) -> dict[str, object]:
            return {
                "kind": "ouroboros_codex_command_dispatcher_v1",
                "implementation_sha256": "spoofed",
            }

    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
        skill_dispatcher=_SpoofedDispatcher().dispatch,
    )

    contract = OrchestratorRunner(
        runtime,
        AsyncMock(),
        MagicMock(),
    )._runtime_execution_identity_contract()

    assert contract == {"version": 1, "observed": False}


def test_factory_codex_runtime_records_portable_dispatcher_identity() -> None:
    """Factory-created Codex runtimes must preserve durable execution identity."""
    with patch(
        "ouroboros.orchestrator.runtime_factory.get_codex_cli_path",
        return_value="/bin/echo",
    ):
        runtime = create_agent_runtime(backend="codex", cwd="/tmp/project")

    contract = OrchestratorRunner(
        runtime,
        AsyncMock(),
        MagicMock(),
    )._build_execution_contract(
        project_identity=OrchestratorRunner(
            runtime,
            AsyncMock(),
            MagicMock(),
        )._project_identity(),
        seed=_seed(),
    )

    identity = _assert_runtime_identity_observed(contract)
    assert identity["kind"] == "codex_cli_v1"
    assert identity["cli_executable_path"] == str(Path("/bin/echo").absolute())
    assert identity["skill_dispatcher"] == "custom"
    assert identity["skill_dispatcher_identity"]


def test_factory_codex_runtime_identity_tracks_cli_path_drift() -> None:
    """Factory dispatcher support must not hide executable/config drift."""
    with patch(
        "ouroboros.orchestrator.runtime_factory.get_codex_cli_path",
        return_value="/bin/echo",
    ):
        original_runtime = create_agent_runtime(backend="codex", cwd="/tmp/project")

    with patch(
        "ouroboros.orchestrator.runtime_factory.get_codex_cli_path",
        return_value="/bin/true",
    ):
        drifted_runtime = create_agent_runtime(backend="codex", cwd="/tmp/project")

    original_identity = original_runtime.execution_identity_contract()
    drifted_identity = drifted_runtime.execution_identity_contract()

    assert original_identity["cli_executable_path"] != drifted_identity["cli_executable_path"]
    assert (
        original_identity["skill_dispatcher_identity"]
        == drifted_identity["skill_dispatcher_identity"]
    )


@pytest.mark.parametrize(
    "runtime_cls",
    [CopilotCliRuntime, GeminiCLIRuntime, GooseCliRuntime, GrokCliRuntime],
)
def test_trusted_codex_family_runtime_rejects_executable_content_drift(
    runtime_cls: type[CodexCliRuntime],
    tmp_path: Path,
) -> None:
    """Every portable Codex-family runtime must guard command-time binary drift."""
    cli = tmp_path / "runtime-cli"
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli.chmod(0o755)
    runtime = runtime_cls(cli_path=cli, cwd=tmp_path, model=None)

    cli.write_text("#!/bin/sh\necho changed\nexit 0\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="CLI executable changed"):
        runtime._build_command(
            str(tmp_path / "last-message.txt"),
            prompt="hello",
        )


def test_zcode_runtime_is_not_trusted_as_portable_identity() -> None:
    runtime = ZcodeCLIRuntime(
        cli_path="/tmp/zcode.cjs",
        model=None,
        cwd="/tmp/project",
    )

    contract = OrchestratorRunner(
        runtime,
        AsyncMock(),
        MagicMock(),
    )._runtime_execution_identity_contract()

    assert contract == {"version": 1, "observed": False}


@pytest.mark.parametrize(
    ("original_provider_effort", "drifted_provider_effort"),
    [("xhigh", "low"), (None, None)],
)
def test_codex_profile_reasoning_effort_drift_is_rejected_before_command_build(
    original_provider_effort: str | None,
    drifted_provider_effort: str | None,
) -> None:
    """Every profile effort that can form ``-c model_reasoning_effort`` is frozen."""
    original_profile: dict[str, object] = {"reasoning_effort": "high"}
    drifted_profile: dict[str, object] = {"reasoning_effort": "low"}
    if original_provider_effort is not None:
        original_profile["providers"] = {"codex": {"reasoning_effort": original_provider_effort}}
        drifted_profile["providers"] = {"codex": {"reasoning_effort": drifted_provider_effort}}

    original_config = OuroborosConfig(
        llm_profiles={"implementation": original_profile},
        llm_role_profiles={"agent_runtime_implementation": "implementation"},
    )
    drifted_config = OuroborosConfig(
        llm_profiles={"implementation": drifted_profile},
        llm_role_profiles={"agent_runtime_implementation": "implementation"},
    )
    handle = RuntimeHandle(
        backend="codex_cli",
        kind="implementation",
        metadata={"llm_role": "agent_runtime_implementation"},
    )

    with patch("ouroboros.providers.profiles.load_config", return_value=original_config):
        runtime = CodexCliRuntime(cli_path="/bin/echo", model=None, cwd="/tmp/project")
        original_command = runtime._build_command("/tmp/output", runtime_handle=handle)

    expected_effort = "xhigh" if original_provider_effort else "high"
    assert f"model_reasoning_effort={expected_effort}" in original_command
    with patch("ouroboros.providers.profiles.load_config", return_value=drifted_config):
        with pytest.raises(RuntimeError, match="profile routing changed"):
            runtime._build_command("/tmp/output", runtime_handle=handle)


def test_provider_neutral_llm_profile_model_changes_runtime_identity() -> None:
    """A handle-selectable top-level llm_profile model is durable identity."""
    original_config = OuroborosConfig(
        llm_profiles={"implementation": {"model": "gpt-a"}},
        llm_role_profiles={"agent_runtime_implementation": "implementation"},
    )
    drifted_config = OuroborosConfig(
        llm_profiles={"implementation": {"model": "gpt-b"}},
        llm_role_profiles={"agent_runtime_implementation": "implementation"},
    )

    with patch("ouroboros.providers.profiles.load_config", return_value=original_config):
        original_runtime = CodexCliRuntime(cli_path="/bin/echo", model=None, cwd="/tmp/project")
    with patch("ouroboros.providers.profiles.load_config", return_value=drifted_config):
        drifted_runtime = CodexCliRuntime(cli_path="/bin/echo", model=None, cwd="/tmp/project")

    assert (
        original_runtime.execution_identity_contract()["profile_resolution_fingerprint"]
        != drifted_runtime.execution_identity_contract()["profile_resolution_fingerprint"]
    )


def test_unselected_native_codex_profile_changes_runtime_identity(
    tmp_path: Path,
) -> None:
    """Any Codex-native profile table is handle-selectable through codex_profile."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config_toml = codex_home / "config.toml"
    base_config = OuroborosConfig()

    config_toml.write_text(
        'model = "gpt-5"\n\n[profiles.unselected]\nmodel = "gpt-a"\n',
        encoding="utf-8",
    )
    with (
        patch("ouroboros.codex.home.resolve_codex_home", return_value=codex_home),
        patch("ouroboros.providers.profiles.load_config", return_value=base_config),
    ):
        original_runtime = CodexCliRuntime(cli_path="/bin/echo", model=None, cwd="/tmp/project")

    config_toml.write_text(
        'model = "gpt-5"\n\n[profiles.unselected]\nmodel = "gpt-b"\n',
        encoding="utf-8",
    )
    with (
        patch("ouroboros.codex.home.resolve_codex_home", return_value=codex_home),
        patch("ouroboros.providers.profiles.load_config", return_value=base_config),
    ):
        drifted_runtime = CodexCliRuntime(cli_path="/bin/echo", model=None, cwd="/tmp/project")

    assert (
        original_runtime.execution_identity_contract()["codex_config_fingerprint"]
        != drifted_runtime.execution_identity_contract()["codex_config_fingerprint"]
    )


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
def test_runtime_selector_validation_rejects_changed_resume_handle(
    runtime_handle: RuntimeHandle,
) -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    runner = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    runner._execution_contract = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=_seed()
    )

    with pytest.raises(OrchestratorError, match="different runtime handle selector"):
        runner._validate_resume_handle_execution_identity(runtime_handle)


def test_runtime_selector_validation_accepts_default_handle() -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    runner = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    runner._execution_contract = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=_seed()
    )

    runner._validate_resume_handle_execution_identity(
        RuntimeHandle(
            backend="codex_cli",
            native_session_id="thread-123",
        )
    )


def test_contract_build_binds_inherited_runtime_handle_selector() -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model="gpt-pinned",
        cwd="/tmp/project",
    )
    handle = RuntimeHandle(
        backend="codex_cli",
        kind="implementation",
        native_session_id="thread-123",
        metadata={"llm_role": "agent_runtime_implementation"},
    )

    contract = OrchestratorRunner(
        runtime,
        AsyncMock(),
        MagicMock(),
    )._build_execution_contract(
        project_identity=OrchestratorRunner(
            runtime,
            AsyncMock(),
            MagicMock(),
        )._project_identity(),
        seed=_seed(),
        runtime_handle=handle,
    )

    identity = _assert_runtime_identity_observed(contract)
    assert identity["resume_handle_selector"] == {
        "backend": "codex_cli",
        "kind": "implementation",
        "selectors": {"llm_role": "agent_runtime_implementation"},
    }


def test_restore_accepts_same_bound_runtime_handle_selector() -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model="gpt-pinned",
        cwd="/tmp/project",
    )
    handle = RuntimeHandle(
        backend="codex_cli",
        kind="implementation",
        native_session_id="thread-123",
        metadata={"llm_role": "agent_runtime_implementation"},
    )
    original = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    contract = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed(), runtime_handle=handle
    )

    resumed = OrchestratorRunner(runtime, AsyncMock(), MagicMock())

    resumed._restore_execution_contract(
        {EXECUTION_CONTRACT_PROGRESS_KEY: contract},
        seed=_seed(),
        runtime_handle=handle,
    )


def test_restore_rejects_different_bound_runtime_handle_selector() -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model="gpt-pinned",
        cwd="/tmp/project",
    )
    original_handle = RuntimeHandle(
        backend="codex_cli",
        kind="implementation",
        native_session_id="thread-123",
        metadata={"llm_role": "agent_runtime_implementation"},
    )
    changed_handle = replace(
        original_handle,
        metadata={"llm_role": "agent_runtime_review"},
    )
    original = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    contract = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed(), runtime_handle=original_handle
    )

    resumed = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    with pytest.raises(OrchestratorError, match="different runtime execution profile"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: contract},
            seed=_seed(),
            runtime_handle=changed_handle,
        )


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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )

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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )

    assert persisted["model_routing"]["constructor_model"] == {
        "observed": True,
        "model": None,
    }
    _assert_process_local_runtime_contract(persisted)
    with pytest.raises(OrchestratorError, match="effective runtime model is unverifiable"):
        original._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_kill_switched_contract_still_rejects_cross_backend_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "off")
    original = _runner(constructor_model="shared-model-pin")
    assert original._model_router is None
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )

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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )

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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
    resumed = _runner()
    resumed._adapter.llm_backend = "openai"

    with pytest.raises(OrchestratorError, match="different LLM backend"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_current_contract_always_binds_bypass_permission_mode() -> None:
    original = _runner()
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )

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
    # this schema default unset lets the handler distinguish omitted (preserve
    # automatic Codex selection / restore a checkpoint) from explicitly supplied
    # ``medium`` (pin standard routing).
    assert parameter.default is None
    assert "Default: medium" not in parameter.description
    assert "Omit to preserve automatic runtime selection" in parameter.description
    assert "pass medium explicitly" in parameter.description

    assert _resolve_model_tier_request({}) == (None, None, None)
    assert _resolve_model_tier_request({"model_tier": None}) == (
        None,
        None,
        None,
    )
    assert _resolve_model_tier_request({"model_tier": "medium"}) == (
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

    first_proof = first._build_execution_contract(
        project_identity=first._project_identity(), seed=_seed()
    )["frugality_proof"]
    second_proof = second._build_execution_contract(
        project_identity=second._project_identity(), seed=_seed()
    )["frugality_proof"]
    first_resume = first._build_execution_contract(
        project_identity=first._project_identity(), seed=_seed()
    )["resume"]
    second_resume = second._build_execution_contract(
        project_identity=second._project_identity(), seed=_seed()
    )["resume"]

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
    _git(
        f"--git-dir={common_git}",
        "worktree",
        "add",
        "-q",
        "-b",
        "ooo/run-1",
        str(generated),
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

    (common_git / "HEAD").write_text("not-a-ref-or-object-id\n", encoding="utf-8")
    for invalid in (
        _runner(cwd=str(common_git)),
        _runner(task_workspace=task_workspace),
    ):
        with pytest.raises(OrchestratorError) as exc_info:
            invalid._project_identity()
        assert exc_info.value.details["resume_blocked"] == "project_identity_unavailable"

    with pytest.raises(OrchestratorError) as exc_info:
        _runner(cwd=str(common_git))._restore_execution_contract(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
                SESSION_START_IDENTITY_PROGRESS_KEY: direct.to_event_data(),
            },
            seed=_seed(),
        )
    assert exc_info.value.details["resume_blocked"] == "project_identity_unavailable"


def test_managed_relative_scope_mismatch_is_rejected_on_resume(tmp_path: Path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    source_cwd = source / "packages" / "expected"
    expected_cwd = worktree / "packages" / "expected"
    wrong_cwd = worktree / "packages" / "wrong"
    _init_git_repo(source)
    _git(
        "worktree",
        "add",
        "-q",
        "-b",
        "ooo/scope-resume",
        str(worktree),
        "HEAD",
        cwd=source,
    )
    for directory in (source_cwd, expected_cwd, wrong_cwd):
        directory.mkdir(parents=True)
    workspace = TaskWorkspace(
        durable_id="scope-resume",
        repo_root=str(source),
        repo_name="source",
        original_cwd=str(source_cwd),
        effective_cwd=str(expected_cwd),
        worktree_path=str(worktree),
        branch="ooo/scope-resume",
        lock_path=str(tmp_path / ".locks" / "scope-resume.json"),
    )
    original = _runner(task_workspace=workspace)
    identity = original._project_identity()
    assert identity is not None
    persisted = original._build_execution_contract(
        seed=_seed(),
        project_identity=identity,
    )
    inconsistent = replace(workspace, effective_cwd=str(wrong_cwd))

    with pytest.raises(OrchestratorError) as exc_info:
        _runner(task_workspace=inconsistent)._restore_execution_contract(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
                SESSION_START_IDENTITY_PROGRESS_KEY: identity.to_event_data(),
            },
            seed=_seed(),
        )

    assert exc_info.value.details["resume_blocked"] == "runtime_cwd_mismatch"


def test_stale_managed_checkout_is_rejected_on_resume(tmp_path: Path) -> None:
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    workspace = _workspace(
        durable_id="stale-resume",
        worktree_path=worktree,
        repo_root=source,
    )
    original = _runner(task_workspace=workspace)
    identity = original._project_identity()
    assert identity is not None
    persisted = original._build_execution_contract(
        seed=_seed(),
        project_identity=identity,
    )

    worktree.rename(tmp_path / "detached-worktree")
    (worktree / "packages" / "app").mkdir(parents=True)

    with pytest.raises(OrchestratorError) as exc_info:
        _runner(task_workspace=workspace)._restore_execution_contract(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
                SESSION_START_IDENTITY_PROGRESS_KEY: identity.to_event_data(),
            },
            seed=_seed(),
        )

    assert exc_info.value.details["resume_blocked"] == "project_identity_mismatch"


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


def test_unregistered_child_cannot_resume_with_enclosing_bare_anchor(tmp_path: Path) -> None:
    owner = tmp_path / "owner.git"
    unowned = owner / "unregistered"
    workspace = unowned / "packages" / "app"
    _git("init", "-q", "--bare", str(owner))
    workspace.mkdir(parents=True)
    (unowned / ".git").write_text(f"gitdir: {owner}\n", encoding="utf-8")
    claimed = ProjectIdentity.from_root(
        owner,
        workspace_path="unregistered/packages/app",
    )
    persisted = _runner(cwd=str(workspace))._build_execution_contract(
        seed=_seed(),
        project_identity=claimed,
    )

    with pytest.raises(OrchestratorError, match="conflicting project identity"):
        _runner(cwd=str(workspace))._restore_execution_contract(
            {
                EXECUTION_CONTRACT_PROGRESS_KEY: persisted,
                SESSION_START_IDENTITY_PROGRESS_KEY: claimed.to_event_data(),
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
    task_workspace = _workspace(
        durable_id="legacy-run",
        worktree_path=generated,
        repo_root=linked,
    )
    original = _runner(task_workspace=task_workspace)
    _use_frontier_custom_routing(original)
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
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
    persisted = original._build_execution_contract(
        project_identity=original._project_identity(), seed=_seed()
    )
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
    persisted = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=_seed()
    )

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
    contract = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=_seed()
    )
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
    contract = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=_seed()
    )
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
    contract = runner._build_execution_contract(
        project_identity=runner._project_identity(), seed=_seed()
    )
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
