"""Durable model-routing and proof-cohort execution contracts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.config.models import OuroborosConfig
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.core.worktree import TaskWorkspace
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.execution_handlers import (
    ExecuteSeedHandler,
    _resolve_model_tier_request,
)
from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.goose_runtime import GooseCliRuntime
from ouroboros.orchestrator.model_routing import (
    ModelRouter,
    deserialize_model_router,
    serialize_model_router,
)
from ouroboros.orchestrator.runner import (
    EXECUTION_CONTRACT_PROGRESS_KEY,
    FRUGALITY_PROOF_PROTOCOL_VERSION,
    OrchestratorError,
    OrchestratorRunner,
)
from ouroboros.orchestrator.session import (
    SESSION_RUNTIME_IDENTITY_PROGRESS_KEY,
    SESSION_START_IDENTITY_PROGRESS_KEY,
    SessionRepository,
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
    cwd: str = "/tmp/project",
    constructor_model: str | None = "constructor-sonnet",
    **kwargs,
) -> OrchestratorRunner:
    return OrchestratorRunner(
        _adapter(cwd, constructor_model=constructor_model),
        AsyncMock(),
        MagicMock(),
        **kwargs,
    )


def _frontier_custom_router() -> ModelRouter:
    return ModelRouter(
        tier_models={
            "frugal": "custom-haiku",
            "standard": "custom-sonnet",
            "frontier": "custom-opus",
        },
        runtime_backend="claude",
        child_tier="frugal",
        base_tier="frontier",
        escalation_retry_threshold=7,
    )


def _seed(*, goal: str = "Prove durable routing", criterion: str = "Routing survives") -> Seed:
    return Seed(
        goal=goal,
        acceptance_criteria=(criterion,),
        ontology_schema=OntologySchema(name="Routing", description="Durable routing contract"),
        metadata=SeedMetadata(seed_id="seed-routing-contract"),
    )


def _workspace(*, durable_id: str, worktree_path: Path, repo_root: Path) -> TaskWorkspace:
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


@pytest.fixture(autouse=True)
def _clear_model_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING", raising=False)
    monkeypatch.delenv("OUROBOROS_EXECUTION_MODEL", raising=False)


def test_router_contract_round_trips_custom_frontier_policy() -> None:
    router = _frontier_custom_router()

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
    original._model_router = _frontier_custom_router()
    persisted = original._build_execution_contract()

    resumed = _runner()
    assert resumed._model_router is not None
    assert resumed._model_router.base_tier == "standard"

    changed = resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert changed is False
    assert resumed._model_router == _frontier_custom_router()


def test_resume_restores_persisted_kill_switch() -> None:
    original = _runner()
    original._model_router = None
    persisted = original._build_execution_contract()

    resumed = _runner()
    assert resumed._model_router is not None

    resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert resumed._model_router is None


def test_explicit_resume_tier_override_replaces_persisted_contract() -> None:
    original = _runner()
    original._model_router = _frontier_custom_router()
    persisted = original._build_execution_contract()

    resumed = _runner(base_model_tier="standard")
    changed = resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert changed is True
    assert resumed._model_router is not None
    assert resumed._model_router.base_tier == "standard"
    assert resumed._model_router.tier_models != _frontier_custom_router().tier_models


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


def test_resume_restores_persisted_retry_policy() -> None:
    """Fix 5 (BLOCKING, PR #1648 review): lateral_escalation_enabled and
    parked_retry_backoff_seconds affect termination/retry semantics, so a
    resumed run must keep the policy it STARTED with rather than picking up
    whatever the current config now resolves to."""
    original = _runner()
    original._lateral_escalation_enabled = True
    original._parked_retry_backoff_seconds = 900.0
    persisted = original._build_execution_contract()
    assert persisted["retry_policy"] == {
        "lateral_escalation_enabled": True,
        "parked_retry_backoff_seconds": 900.0,
    }

    resumed = _runner()
    # Sanity: the fresh runner's own config-resolved defaults differ from the
    # persisted policy, so a passing assertion below proves restoration
    # actually happened rather than coincidentally matching a shared default.
    assert resumed._lateral_escalation_enabled is False
    assert resumed._parked_retry_backoff_seconds != 900.0

    changed = resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert changed is False
    assert resumed._lateral_escalation_enabled is True
    assert resumed._parked_retry_backoff_seconds == 900.0


def test_resume_migrates_legacy_contract_missing_retry_policy() -> None:
    """A contract persisted before this field existed has no ``retry_policy``
    key at all. Missing is treated as a one-time legacy migration (mirrors
    ``execution_preferences``): the CURRENT config's resolved policy is kept
    for this resume, and the contract is flagged for re-persisting so every
    later resume restores THIS exact policy instead of drifting again."""
    original = _runner()
    persisted = original._build_execution_contract()
    del persisted["retry_policy"]

    resumed = _runner()
    resumed._lateral_escalation_enabled = True  # current config for THIS process
    resumed._parked_retry_backoff_seconds = 42.0

    changed = resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert changed is True
    assert resumed._lateral_escalation_enabled is True
    assert resumed._parked_retry_backoff_seconds == 42.0
    assert resumed._execution_contract is not None
    assert resumed._execution_contract["retry_policy"] == {
        "lateral_escalation_enabled": True,
        "parked_retry_backoff_seconds": 42.0,
    }


@pytest.mark.parametrize(
    "malformed_retry_policy",
    [
        {"lateral_escalation_enabled": True},  # missing parked_retry_backoff_seconds
        {"lateral_escalation_enabled": "yes", "parked_retry_backoff_seconds": 300.0},
        {"lateral_escalation_enabled": True, "parked_retry_backoff_seconds": "300"},
        {"lateral_escalation_enabled": True, "parked_retry_backoff_seconds": -5.0},
        {"lateral_escalation_enabled": True, "parked_retry_backoff_seconds": True},
        "not-a-mapping",
    ],
)
def test_malformed_retry_policy_fails_closed(malformed_retry_policy: object) -> None:
    original = _runner()
    persisted = original._build_execution_contract()
    persisted["retry_policy"] = malformed_retry_policy

    resumed = _runner()
    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


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


def test_codex_profile_drift_is_rejected_when_constructor_model_is_absent() -> None:
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
    persisted = original._build_execution_contract(seed=_seed())

    resumed_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    resumed_runtime._runtime_profile = "zep-runtime"
    resumed_runtime._codex_profile = "zep-proxy-b"
    resumed_runtime._resolved_fallback_model = "resolved-fallback-model"
    resumed_runtime._resolved_fallback_profile = None
    resumed = OrchestratorRunner(resumed_runtime, AsyncMock(), MagicMock())

    persisted_identity = persisted["model_routing"]["runtime_execution"]["identity"]
    assert persisted_identity == {
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
    }
    with pytest.raises(OrchestratorError, match="different runtime execution profile"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_codex_resolved_fallback_model_drift_is_rejected() -> None:
    original_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    original_runtime._resolved_fallback_model = "gpt-original"
    original_runtime._resolved_fallback_profile = None
    persisted = OrchestratorRunner(
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
    resumed = OrchestratorRunner(resumed_runtime, AsyncMock(), MagicMock())

    with pytest.raises(OrchestratorError, match="different runtime execution profile"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_codex_profile_name_alone_does_not_prove_an_effective_model(
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

    assert (
        persisted["model_routing"]["runtime_execution"]["identity"]["effective_model_observed"]
        is False
    )
    with pytest.raises(OrchestratorError, match="effective runtime model is unverifiable"):
        runner._restore_execution_contract(
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
    persisted = runner._build_execution_contract(seed=_seed())

    assert persisted["model_routing"]["runtime_execution"]["identity"] == {
        "kind": "goose_v1",
        "fallback_model": None,
        "effective_model_observed": False,
        "llm_backend": "goose",
    }
    with pytest.raises(OrchestratorError, match="effective runtime model is unverifiable"):
        runner._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


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
    with pytest.raises(OrchestratorError, match="effective runtime model is unverifiable"):
        runner._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_codex_profile_file_drift_is_rejected_even_with_native_routing(
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
    persisted = original._build_execution_contract(seed=_seed())

    profile_path.write_text('model_provider = "proxy-b"\n', encoding="utf-8")
    resumed_runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    resumed_runtime._codex_profile = "stable-name"
    resumed = OrchestratorRunner(resumed_runtime, AsyncMock(), MagicMock())

    with pytest.raises(OrchestratorError, match="different runtime execution profile"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_codex_home_drift_is_rejected_for_persisted_thread_store(
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
    persisted = OrchestratorRunner(
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
    resumed = OrchestratorRunner(resumed_runtime, AsyncMock(), MagicMock())

    with pytest.raises(OrchestratorError, match="different runtime execution profile"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_codex_implementation_role_mapping_drift_is_rejected() -> None:
    original_config = OuroborosConfig(
        llm_profiles={
            "standard": {"providers": {"codex": {"profile": "stable"}}},
            "frontier": {"providers": {"codex": {"profile": "changed"}}},
        },
        llm_role_profiles={
            "agent_runtime": "standard",
            "agent_runtime_implementation": "standard",
        },
    )
    drifted_config = original_config.model_copy(
        update={
            "llm_role_profiles": {
                "agent_runtime": "standard",
                "agent_runtime_implementation": "frontier",
            }
        }
    )

    with patch("ouroboros.providers.profiles.load_config", return_value=original_config):
        original_runtime = CodexCliRuntime(
            cli_path="/bin/echo",
            model=None,
            cwd="/tmp/project",
        )
    persisted = OrchestratorRunner(
        original_runtime,
        AsyncMock(),
        MagicMock(),
    )._build_execution_contract(seed=_seed())

    with patch("ouroboros.providers.profiles.load_config", return_value=drifted_config):
        resumed_runtime = CodexCliRuntime(
            cli_path="/bin/echo",
            model=None,
            cwd="/tmp/project",
        )
    resumed = OrchestratorRunner(resumed_runtime, AsyncMock(), MagicMock())

    assert original_runtime._resolved_fallback_profile == "stable"
    assert resumed_runtime._resolved_fallback_profile == "stable"
    with pytest.raises(OrchestratorError, match="different runtime execution profile"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
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
def test_codex_resume_handle_cannot_inject_command_selection(
    runtime_handle: RuntimeHandle,
) -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    runner = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    runner._execution_contract = runner._build_execution_contract(seed=_seed())

    with pytest.raises(OrchestratorError, match="different runtime handle selector"):
        runner._validate_resume_handle_execution_identity(runtime_handle)


def test_codex_default_resume_handle_matches_start_contract() -> None:
    runtime = CodexCliRuntime(
        cli_path="/bin/echo",
        model=None,
        cwd="/tmp/project",
    )
    runner = OrchestratorRunner(runtime, AsyncMock(), MagicMock())
    runner._execution_contract = runner._build_execution_contract(seed=_seed())

    runner._validate_resume_handle_execution_identity(
        RuntimeHandle(
            backend="codex_cli",
            native_session_id="thread-123",
        )
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
    persisted = original._build_execution_contract(seed=_seed())

    monkeypatch.delenv("OUROBOROS_MODEL_TIER_ROUTING")
    resumed = _runner(constructor_model="claude-sonnet-changed")

    with pytest.raises(OrchestratorError, match="different constructor model"):
        resumed._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
        )


def test_unpinned_kill_switched_runtime_cannot_resume_without_effective_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_MODEL_TIER_ROUTING", "off")
    original = _runner(constructor_model=None)
    persisted = original._build_execution_contract(seed=_seed())

    assert persisted["model_routing"]["constructor_model"] == {
        "observed": True,
        "model": None,
    }
    assert persisted["model_routing"]["runtime_execution"] == {
        "version": 1,
        "observed": False,
    }

    with pytest.raises(OrchestratorError, match="effective runtime model is unverifiable"):
        _runner(constructor_model=None)._restore_execution_contract(
            {EXECUTION_CONTRACT_PROGRESS_KEY: persisted},
            seed=_seed(),
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
    original = _runner(cwd=str(tmp_path / "project-a"))
    persisted = original._build_execution_contract(seed=_seed())
    resumed = _runner(cwd=str(tmp_path / "project-b"))

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


def test_first_legacy_resume_migrates_resolved_contract() -> None:
    runner = _runner()

    changed = runner._restore_execution_contract({}, seed=_seed())

    assert changed is True
    assert runner._execution_contract is not None
    assert "seed_fingerprint" in runner._execution_contract["frugality_proof"]


@pytest.mark.asyncio
async def test_legacy_resume_rejects_changed_seed_identity_before_dispatch() -> None:
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
    assert "different Seed identity" in result.error.message
    adapter.execute_task.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_resume_rejects_changed_seed_goal_before_dispatch() -> None:
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
    assert "modified Seed goal" in result.error.message
    adapter.execute_task.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_resume_rejects_cross_backend_before_dispatch() -> None:
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
    assert "different runtime backend" in result.error.message
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

    with pytest.raises(OrchestratorError, match="different project workspace"):
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


def test_legacy_resume_migrates_to_forced_bypass_permission() -> None:
    runner = _runner()

    changed = runner._restore_execution_contract(
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

    assert changed is True
    assert runner._execution_contract is not None
    assert runner._execution_contract["model_routing"]["permission_mode"] == {
        "observed": True,
        "mode": "bypassPermissions",
    }


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
