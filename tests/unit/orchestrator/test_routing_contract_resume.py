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
    whatever the current config now resolves to. ``ac_retry_attempts`` (Fix
    7, round 3, BLOCKING) joins them for the same reason -- it directly
    changes the pre-ladder dispatch count, token spend, and whether the
    lateral-escalation ladder is even reached."""
    original = _runner()
    original._lateral_escalation_enabled = True
    original._parked_retry_backoff_seconds = 900.0
    original._ac_retry_attempts = 99
    # Round-9 finding #4: ladder-eligibility effort and verify-gate config
    # join the contract for the same reason.
    original._reasoning_effort = "high"
    original._run_verify_commands = False
    original._verify_command_timeout_seconds = 123
    # Round-13 finding #3: dispatch-shape and worker-prompt semantics join
    # the contract for the same reason.
    original._decomposition_mode = "bounce_only"
    original._cross_harness_redispatch_enabled = True
    original._context_pack_enabled = False
    # Round-14 finding #2: the remaining dispatch/acceptance/spend semantics
    # join the contract for the same reason.
    original._max_decomposition_depth = 9
    original._fat_harness_mode = True
    original._shadow_replay_enabled = True
    # Round-15 finding #5: shared-workspace concurrency joins the contract
    # for the same reason (interleaved sibling writes are semantics).
    original._effective_parallel_workers = 7
    persisted = original._build_execution_contract()
    assert persisted["retry_policy"] == {
        "lateral_escalation_enabled": True,
        "parked_retry_backoff_seconds": 900.0,
        "ac_retry_attempts": 99,
        "reasoning_effort": "high",
        "run_verify_commands": False,
        "verify_command_timeout_seconds": 123,
        "decomposition_mode": "bounce_only",
        "cross_harness_redispatch_enabled": True,
        "context_pack_enabled": False,
        "max_decomposition_depth": 9,
        "fat_harness_mode": True,
        "shadow_replay_enabled": True,
        "effective_parallel_workers": 7,
    }

    resumed = _runner()
    # Sanity: the fresh runner's own config-resolved defaults differ from the
    # persisted policy, so a passing assertion below proves restoration
    # actually happened rather than coincidentally matching a shared default.
    assert resumed._lateral_escalation_enabled is False
    assert resumed._parked_retry_backoff_seconds != 900.0
    assert resumed._ac_retry_attempts != 99
    # The new fields' current-config values are forced to differ explicitly
    # (their config-resolved defaults depend on the environment).
    resumed._reasoning_effort = None
    resumed._run_verify_commands = True
    resumed._verify_command_timeout_seconds = 600
    resumed._decomposition_mode = "off"
    resumed._enable_decomposition = False
    resumed._cross_harness_redispatch_enabled = False
    resumed._context_pack_enabled = True
    resumed._max_decomposition_depth = 1
    resumed._fat_harness_mode = False
    resumed._shadow_replay_enabled = False
    resumed._effective_parallel_workers = 1

    changed = resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert changed is False
    assert resumed._lateral_escalation_enabled is True
    assert resumed._parked_retry_backoff_seconds == 900.0
    assert resumed._ac_retry_attempts == 99
    assert resumed._reasoning_effort == "high"
    assert resumed._run_verify_commands is False
    assert resumed._verify_command_timeout_seconds == 123
    assert resumed._decomposition_mode == "bounce_only"
    # The paired gate follows the restored mode, mirroring __init__.
    assert resumed._enable_decomposition is True
    assert resumed._cross_harness_redispatch_enabled is True
    assert resumed._context_pack_enabled is False
    assert resumed._max_decomposition_depth == 9
    assert resumed._fat_harness_mode is True
    # Restored wholesale — NOT re-derived from this process's env request,
    # which (unset here) would have resolved False.
    assert resumed._shadow_replay_enabled is True
    # Round-15 finding #5: the resumed run keeps the shared-workspace
    # concurrency it STARTED with, not this process's re-resolved value.
    assert resumed._effective_parallel_workers == 7


def test_resume_migrates_legacy_contract_missing_retry_policy() -> None:
    """A contract persisted before this field existed has no ``retry_policy``
    key at all. Missing is treated as a one-time legacy migration (mirrors
    ``execution_preferences``): the CURRENT config's resolved policy is kept
    for this resume, and the contract is flagged for re-persisting so every
    later resume restores THIS exact policy instead of drifting again."""
    original = _runner()
    persisted = original._build_execution_contract()
    del persisted["retry_policy"]
    # Fix 6 (round 2, BLOCKING): a GENUINELY legacy contract (predating both
    # the retry_policy field AND its folding into the routing fingerprint)
    # would never have had retry_policy baked into its fingerprint either.
    # Recompute it that way so this simulated legacy contract is internally
    # consistent -- otherwise the fingerprint would still encode the
    # now-deleted retry_policy data and spuriously fail identity validation
    # before ever reaching the migration path this test exercises.
    persisted["frugality_proof"]["routing_fingerprint"] = OrchestratorRunner._routing_fingerprint(
        persisted["model_routing"]
    )

    resumed = _runner()
    resumed._lateral_escalation_enabled = True  # current config for THIS process
    resumed._parked_retry_backoff_seconds = 42.0
    resumed._ac_retry_attempts = 7
    resumed._reasoning_effort = "medium"
    resumed._run_verify_commands = True
    resumed._verify_command_timeout_seconds = 77
    resumed._decomposition_mode = "preflight"
    resumed._cross_harness_redispatch_enabled = False
    resumed._context_pack_enabled = True
    resumed._max_decomposition_depth = 3
    resumed._fat_harness_mode = True
    resumed._effective_parallel_workers = 3

    changed = resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})

    assert changed is True
    assert resumed._lateral_escalation_enabled is True
    assert resumed._parked_retry_backoff_seconds == 42.0
    assert resumed._ac_retry_attempts == 7
    assert resumed._execution_contract is not None
    assert resumed._execution_contract["retry_policy"] == {
        "lateral_escalation_enabled": True,
        "parked_retry_backoff_seconds": 42.0,
        "ac_retry_attempts": 7,
        "reasoning_effort": "medium",
        "run_verify_commands": True,
        "verify_command_timeout_seconds": 77,
        "decomposition_mode": "preflight",
        "cross_harness_redispatch_enabled": False,
        "context_pack_enabled": True,
        "max_decomposition_depth": 3,
        "fat_harness_mode": True,
        # A migrated legacy contract keeps the recomputed (env-and-strict
        # derived) value; without strict authorization this is always False.
        "shadow_replay_enabled": False,
        # A migrated legacy contract keeps the current process's resolved
        # fan-out (set explicitly above for determinism).
        "effective_parallel_workers": 3,
    }


def _full_retry_policy(**overrides: object) -> dict[str, object]:
    """A well-formed thirteen-field policy, with targeted per-test corruption."""
    policy: dict[str, object] = {
        "lateral_escalation_enabled": True,
        "parked_retry_backoff_seconds": 300.0,
        "ac_retry_attempts": 2,
        "reasoning_effort": "high",
        "run_verify_commands": True,
        "verify_command_timeout_seconds": 600,
        "decomposition_mode": "preflight",
        "cross_harness_redispatch_enabled": False,
        "context_pack_enabled": True,
        "max_decomposition_depth": 2,
        "fat_harness_mode": False,
        "shadow_replay_enabled": False,
        "effective_parallel_workers": 3,
    }
    policy.update(overrides)
    return policy


@pytest.mark.parametrize(
    "malformed_retry_policy",
    [
        {"lateral_escalation_enabled": True},  # missing every other field
        _full_retry_policy(lateral_escalation_enabled="yes"),
        _full_retry_policy(parked_retry_backoff_seconds="300"),
        _full_retry_policy(parked_retry_backoff_seconds=-5.0),
        _full_retry_policy(parked_retry_backoff_seconds=True),
        "not-a-mapping",
        # Fix 7 (round 2, BLOCKING): a deserialized contract carrying
        # float("inf")/nan must be rejected here too, not just at
        # EconomicsConfig's Pydantic construction layer -- a resumed run
        # replays THIS check, not the config field validator, so
        # ``float("inf")`` must fail closed at both boundaries or it reaches
        # ``asyncio.sleep(inf)`` and hangs that AC's slot forever.
        _full_retry_policy(parked_retry_backoff_seconds=float("inf")),
        _full_retry_policy(parked_retry_backoff_seconds=float("nan")),
        # Fix 9 (round 3, BLOCKING): 0 (and any value below EconomicsConfig's
        # own ``ge=1.0`` floor) must be rejected HERE, at resume validation --
        # not silently accepted and then clamped to 1.0 by the executor
        # constructor's defense-in-depth floor, which would let the
        # persisted/fingerprinted policy (0) diverge from the policy actually
        # executed (1.0).
        _full_retry_policy(parked_retry_backoff_seconds=0.0),
        _full_retry_policy(parked_retry_backoff_seconds=0.5),
        # Fix 7 (round 3, BLOCKING): a round-2-era contract (or any tampered
        # payload) missing/malformed on the NEW ac_retry_attempts field must
        # fail closed here too, exactly like the other two fields already do.
        {
            "lateral_escalation_enabled": True,
            "parked_retry_backoff_seconds": 300.0,
        },  # missing ac_retry_attempts entirely (round-2-era contract shape)
        _full_retry_policy(ac_retry_attempts="2"),
        _full_retry_policy(ac_retry_attempts=-1),
        _full_retry_policy(ac_retry_attempts=True),
        _full_retry_policy(ac_retry_attempts=2.5),
        # Round-9 finding #4: an earlier-round three-field contract shape (or
        # any tampered payload) missing/malformed on the NEW effort and
        # verify-gate fields must fail closed here too.
        {
            "lateral_escalation_enabled": True,
            "parked_retry_backoff_seconds": 300.0,
            "ac_retry_attempts": 2,
        },  # rounds-1-8-era contract shape, missing the round-9 fields
        _full_retry_policy(reasoning_effort="minimal"),  # Codex-only value
        _full_retry_policy(reasoning_effort=True),
        _full_retry_policy(run_verify_commands="yes"),
        _full_retry_policy(run_verify_commands=None),
        _full_retry_policy(verify_command_timeout_seconds=0),
        _full_retry_policy(verify_command_timeout_seconds="600"),
        _full_retry_policy(verify_command_timeout_seconds=True),
        _full_retry_policy(verify_command_timeout_seconds=600.5),
        # Round-13 finding #3: a rounds-9-12-era contract shape (or any
        # tampered payload) missing/malformed on the NEW dispatch-shape and
        # worker-prompt fields must fail closed here too.
        {
            "lateral_escalation_enabled": True,
            "parked_retry_backoff_seconds": 300.0,
            "ac_retry_attempts": 2,
            "reasoning_effort": "high",
            "run_verify_commands": True,
            "verify_command_timeout_seconds": 600,
        },  # rounds-9-12-era six-field contract shape
        _full_retry_policy(decomposition_mode="everything"),
        _full_retry_policy(decomposition_mode=True),
        _full_retry_policy(decomposition_mode=None),
        _full_retry_policy(cross_harness_redispatch_enabled="yes"),
        _full_retry_policy(cross_harness_redispatch_enabled=None),
        _full_retry_policy(cross_harness_redispatch_enabled=1),
        _full_retry_policy(context_pack_enabled="on"),
        _full_retry_policy(context_pack_enabled=None),
        _full_retry_policy(context_pack_enabled=0),
        # Round-14 finding #2: a round-13-era contract shape (or any tampered
        # payload) missing/malformed on the NEW depth/acceptance-gate/
        # shadow-replay fields must fail closed here too.
        {
            "lateral_escalation_enabled": True,
            "parked_retry_backoff_seconds": 300.0,
            "ac_retry_attempts": 2,
            "reasoning_effort": "high",
            "run_verify_commands": True,
            "verify_command_timeout_seconds": 600,
            "decomposition_mode": "preflight",
            "cross_harness_redispatch_enabled": False,
            "context_pack_enabled": True,
        },  # round-13-era nine-field contract shape
        _full_retry_policy(max_decomposition_depth="2"),
        _full_retry_policy(max_decomposition_depth=-1),
        _full_retry_policy(max_decomposition_depth=True),
        _full_retry_policy(max_decomposition_depth=2.5),
        _full_retry_policy(max_decomposition_depth=None),
        _full_retry_policy(fat_harness_mode="yes"),
        _full_retry_policy(fat_harness_mode=None),
        _full_retry_policy(fat_harness_mode=1),
        _full_retry_policy(shadow_replay_enabled="on"),
        _full_retry_policy(shadow_replay_enabled=None),
        _full_retry_policy(shadow_replay_enabled=0),
        # Round-15 finding #5: the resolved fan-out mirrors
        # ``plan_fan_out_concurrency``'s own >= 1 floor — rejected, not
        # clamped, below it.
        _full_retry_policy(effective_parallel_workers="3"),
        _full_retry_policy(effective_parallel_workers=0),
        _full_retry_policy(effective_parallel_workers=-1),
        _full_retry_policy(effective_parallel_workers=True),
        _full_retry_policy(effective_parallel_workers=2.5),
        _full_retry_policy(effective_parallel_workers=None),
    ],
)
def test_malformed_retry_policy_fails_closed(malformed_retry_policy: object) -> None:
    original = _runner()
    persisted = original._build_execution_contract()
    persisted["retry_policy"] = malformed_retry_policy

    resumed = _runner()
    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        resumed._restore_execution_contract({EXECUTION_CONTRACT_PROGRESS_KEY: persisted})


def test_retry_policy_is_folded_into_routing_fingerprint() -> None:
    """Fix 6 (BLOCKING, PR #1648 round 2 review): retry_policy
    (lateral_escalation_enabled/parked_retry_backoff_seconds) materially
    changes a run's retry/termination behavior and therefore its token
    spend. Two otherwise-identical runs differing ONLY in retry_policy must
    produce DIFFERENT routing fingerprints, or the frugality proof would
    treat them as the same "exact" cohort and corrupt its spend/outcome
    comparison."""
    disabled = _runner()
    disabled._lateral_escalation_enabled = False
    disabled._parked_retry_backoff_seconds = 300.0
    disabled_contract = disabled._build_execution_contract()

    enabled = _runner()
    enabled._lateral_escalation_enabled = True
    enabled._parked_retry_backoff_seconds = 300.0
    enabled_contract = enabled._build_execution_contract()

    # The two contracts' MODEL ROUTING is otherwise identical -- the ONLY
    # difference is retry_policy.
    assert disabled_contract["model_routing"] == enabled_contract["model_routing"]
    assert disabled_contract["retry_policy"] != enabled_contract["retry_policy"]

    assert (
        disabled_contract["frugality_proof"]["routing_fingerprint"]
        != enabled_contract["frugality_proof"]["routing_fingerprint"]
    )


@pytest.mark.parametrize(
    ("field", "first_value", "second_value"),
    [
        ("_reasoning_effort", None, "high"),
        ("_reasoning_effort", "low", "xhigh"),
        ("_run_verify_commands", True, False),
        ("_verify_command_timeout_seconds", 600, 30),
    ],
)
def test_effort_and_verify_gate_are_folded_into_routing_fingerprint(
    field: str, first_value: object, second_value: object
) -> None:
    """Round-9 finding #4 (BLOCKING): ``reasoning_effort`` governs ladder
    eligibility (terminal-state/frontier-effort checks) and the verify-gate
    pair governs whether attestation — and thus the cheap-tier discount —
    can be evaluated at all. Two runs differing ONLY on one of these axes
    behave materially differently and must never collapse into the same
    frugality-proof cohort."""
    first = _runner()
    setattr(first, field, first_value)
    first_contract = first._build_execution_contract()

    second = _runner()
    setattr(second, field, second_value)
    second_contract = second._build_execution_contract()

    assert first_contract["model_routing"] == second_contract["model_routing"]
    assert first_contract["retry_policy"] != second_contract["retry_policy"]
    assert (
        first_contract["frugality_proof"]["routing_fingerprint"]
        != second_contract["frugality_proof"]["routing_fingerprint"]
    )


@pytest.mark.parametrize(
    ("field", "first_value", "second_value"),
    [
        ("_decomposition_mode", "preflight", "off"),
        ("_decomposition_mode", "preflight", "bounce_only"),
        ("_decomposition_mode", "bounce_only", "off"),
        ("_cross_harness_redispatch_enabled", False, True),
        ("_context_pack_enabled", True, False),
    ],
)
def test_dispatch_and_prompt_semantics_are_folded_into_routing_fingerprint(
    field: str, first_value: object, second_value: object
) -> None:
    """Round-13 finding #3 (BLOCKING): ``decomposition_mode`` changes what
    a run dispatches, ``cross_harness_redispatch_enabled`` changes whether
    a terminally failing AC gets an alternate-runtime redispatch before
    FAILED, and ``context_pack_enabled`` changes how worker prompts are
    constructed. Two runs differing ONLY on one of these axes behave
    materially differently and must never share a fingerprint/cohort."""
    first = _runner()
    setattr(first, field, first_value)
    first_contract = first._build_execution_contract()

    second = _runner()
    setattr(second, field, second_value)
    second_contract = second._build_execution_contract()

    assert first_contract["model_routing"] == second_contract["model_routing"]
    assert first_contract["retry_policy"] != second_contract["retry_policy"]
    assert (
        first_contract["frugality_proof"]["routing_fingerprint"]
        != second_contract["frugality_proof"]["routing_fingerprint"]
    )


@pytest.mark.parametrize(
    ("field", "first_value", "second_value"),
    [
        ("_max_decomposition_depth", 1, 9),
        ("_max_decomposition_depth", 0, 2),
        ("_fat_harness_mode", False, True),
        ("_shadow_replay_enabled", False, True),
    ],
)
def test_depth_acceptance_and_shadow_replay_are_folded_into_routing_fingerprint(
    field: str, first_value: object, second_value: object
) -> None:
    """Round-14 finding #2 (BLOCKING): ``max_decomposition_depth`` changes
    how far ACs recursively decompose (dispatch shape and spend),
    ``fat_harness_mode`` changes the atomic acceptance gate, and
    ``shadow_replay_enabled`` arms the shadow-baseline experiment harness.
    Two runs differing ONLY on one of these axes behave materially
    differently and must never share a fingerprint/cohort."""
    first = _runner()
    setattr(first, field, first_value)
    first_contract = first._build_execution_contract()

    second = _runner()
    setattr(second, field, second_value)
    second_contract = second._build_execution_contract()

    assert first_contract["model_routing"] == second_contract["model_routing"]
    assert first_contract["retry_policy"] != second_contract["retry_policy"]
    assert (
        first_contract["frugality_proof"]["routing_fingerprint"]
        != second_contract["frugality_proof"]["routing_fingerprint"]
    )


def test_depth_and_fat_harness_probe_from_round_14_review_diverges() -> None:
    """The round-14 review's exact probe: depth=1/fat=False vs
    depth=9/fat=True previously produced IDENTICAL fingerprints, letting
    materially incomparable runs share one frugality-proof cohort."""
    lean = _runner()
    lean._max_decomposition_depth = 1
    lean._fat_harness_mode = False
    lean_contract = lean._build_execution_contract()

    fat = _runner()
    fat._max_decomposition_depth = 9
    fat._fat_harness_mode = True
    fat_contract = fat._build_execution_contract()

    assert lean_contract["model_routing"] == fat_contract["model_routing"]
    assert (
        lean_contract["frugality_proof"]["routing_fingerprint"]
        != fat_contract["frugality_proof"]["routing_fingerprint"]
    )


def test_rebuild_recovered_system_prompt_threads_restored_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-14 finding #3 (BLOCKING): the executor calls this builder back
    AFTER RC3 restoration; it must construct the prompt from the RESTORED
    settings (not the runner's current fields) and restore the checkpointed
    guidance identity through the same fail-closed machinery
    ``resume_session`` uses."""
    runner = _runner()
    # The runner's CURRENT config says context pack ON — the restored run
    # said OFF; the builder must honor the restored value it is passed.
    runner._context_pack_enabled = True
    seed = _seed()
    captured: dict[str, object] = {}

    def _fake_build(
        seed_arg: object,
        strategy: object = None,
        *,
        repo_root: object = None,
        guidance_fragment: str = "",
        context_pack_enabled: object = None,
    ) -> str:
        captured["context_pack_enabled"] = context_pack_enabled
        captured["guidance_fragment"] = guidance_fragment
        return "rebuilt-prompt"

    monkeypatch.setattr("ouroboros.orchestrator.runner.build_system_prompt", _fake_build)
    # The exact shape a real run persists on the checkpoint: the resolved
    # guidance identity (mode/provenance plus content-hash metadata).
    persisted_guidance = OrchestratorRunner._guidance_contract(runner._ensure_new_run_guidance())
    prompt = runner._rebuild_recovered_system_prompt(
        seed,
        fat_harness_mode=False,
        context_pack_enabled=False,
        guidance_contract=persisted_guidance,
    )

    assert prompt == "rebuilt-prompt"
    assert captured["context_pack_enabled"] is False
    # Disabled restored guidance produces the empty fragment.
    assert captured["guidance_fragment"] == ""


@pytest.mark.parametrize(
    "bad_guidance",
    [
        "not-a-mapping",
        {"mode": "evil", "provenance_scope": "ouroboros_declared_guidance_only", "items": []},
        {"mode": "declared", "provenance_scope": "somewhere-else", "items": []},
    ],
)
def test_rebuild_recovered_system_prompt_fails_closed_on_bad_guidance(
    bad_guidance: object,
) -> None:
    """A malformed checkpointed guidance identity must refuse the recovered
    launch (fail closed), never silently keep the current process's
    guidance for a run that started with different guidance."""
    runner = _runner()
    with pytest.raises(OrchestratorError, match="invalid execution contract"):
        runner._rebuild_recovered_system_prompt(
            _seed(),
            fat_harness_mode=False,
            context_pack_enabled=None,
            guidance_contract=bad_guidance,
        )


def test_rebuild_recovered_system_prompt_refuses_changed_guidance() -> None:
    """A well-formed checkpointed guidance identity whose metadata no longer
    matches the currently-resolvable guidance must refuse the recovered
    launch — the exact ``resume_session`` fail-closed semantics."""
    runner = _runner()
    stale = OrchestratorRunner._guidance_contract(runner._ensure_new_run_guidance())
    stale["rendered_fragment_hash"] = "sha256:" + "0" * 64
    with pytest.raises(OrchestratorError, match="guidance changed"):
        runner._rebuild_recovered_system_prompt(
            _seed(),
            fat_harness_mode=False,
            context_pack_enabled=None,
            guidance_contract=stale,
        )


def test_retry_policy_is_folded_into_proof_cohort_identity() -> None:
    """End-to-end through ``_proof_cohort_identity``: two runs differing
    ONLY in retry_policy must resolve to DIFFERENT cohort identities so a
    frugality-proof cohort scan never groups them together."""
    disabled = _runner()
    disabled._lateral_escalation_enabled = False

    enabled = _runner()
    enabled._lateral_escalation_enabled = True

    seed = _seed()
    disabled_identity = disabled._proof_cohort_identity(
        {
            "seed_id": seed.metadata.seed_id,
            EXECUTION_CONTRACT_PROGRESS_KEY: disabled._build_execution_contract(seed=seed),
        }
    )
    enabled_identity = enabled._proof_cohort_identity(
        {
            "seed_id": seed.metadata.seed_id,
            EXECUTION_CONTRACT_PROGRESS_KEY: enabled._build_execution_contract(seed=seed),
        }
    )

    assert disabled_identity is not None
    assert enabled_identity is not None
    assert disabled_identity != enabled_identity


def test_same_retry_policy_still_produces_the_same_cohort_identity() -> None:
    """Regression guard: retry_policy must be an identity axis, not a
    source of spurious cohort splitting -- two runs with the SAME
    retry_policy (and everything else identical) still cohort together."""
    seed = _seed()
    first = _runner()
    first._lateral_escalation_enabled = True
    first._parked_retry_backoff_seconds = 600.0

    second = _runner()
    second._lateral_escalation_enabled = True
    second._parked_retry_backoff_seconds = 600.0

    first_identity = first._proof_cohort_identity(
        {
            "seed_id": seed.metadata.seed_id,
            EXECUTION_CONTRACT_PROGRESS_KEY: first._build_execution_contract(seed=seed),
        }
    )
    second_identity = second._proof_cohort_identity(
        {
            "seed_id": seed.metadata.seed_id,
            EXECUTION_CONTRACT_PROGRESS_KEY: second._build_execution_contract(seed=seed),
        }
    )

    assert first_identity is not None
    assert first_identity == second_identity


def test_legacy_contract_missing_retry_policy_still_resolves_a_cohort_identity() -> None:
    """A genuinely legacy persisted contract (no retry_policy key at all,
    predating both round-1 Fix 5 and this fix) must still resolve to a
    stable, self-consistent cohort identity -- Fix 6 must not regress
    reading old history."""
    seed = _seed()
    runner = _runner()
    contract = runner._build_execution_contract(seed=seed)
    contract["frugality_proof"]["routing_fingerprint"] = OrchestratorRunner._routing_fingerprint(
        contract["model_routing"]
    )
    del contract["retry_policy"]

    identity = runner._proof_cohort_identity(
        {"seed_id": seed.metadata.seed_id, EXECUTION_CONTRACT_PROGRESS_KEY: contract}
    )

    assert identity is not None


def test_malformed_retry_policy_excludes_entry_from_cohort_identity() -> None:
    """A present-but-malformed retry_policy on a historical contract must
    fail cohort-identity extraction closed (excluded from candidacy), not
    be silently treated as absent."""
    seed = _seed()
    runner = _runner()
    contract = runner._build_execution_contract(seed=seed)
    contract["retry_policy"] = {"lateral_escalation_enabled": True}  # missing backoff field

    identity = runner._proof_cohort_identity(
        {"seed_id": seed.metadata.seed_id, EXECUTION_CONTRACT_PROGRESS_KEY: contract}
    )

    assert identity is None


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
            "routing_fingerprint": OrchestratorRunner._routing_fingerprint(
                malformed_routing, retry_policy=persisted["retry_policy"]
            ),
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
            "routing_fingerprint": OrchestratorRunner._routing_fingerprint(
                malformed_routing, retry_policy=persisted["retry_policy"]
            ),
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
            "routing_fingerprint": OrchestratorRunner._routing_fingerprint(
                inconsistent_routing, retry_policy=persisted["retry_policy"]
            ),
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
