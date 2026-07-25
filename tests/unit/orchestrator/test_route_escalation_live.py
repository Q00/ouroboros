"""Live bounded-escalation wiring at the parallel provider boundary."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.config.models import EconomicsConfig, ModelConfig, TierConfig
from ouroboros.core.seed import (
    OntologySchema,
    Seed,
    SeedMetadata,
    derive_semantic_ac_key,
)
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import ParamSupport, RuntimeCapabilities
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.model_routing import build_model_router
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
)
from ouroboros.orchestrator.route_escalation import (
    EscalationReason,
    RouteEscalationDecision,
    RouteObservation,
    VerifierOutcome,
    advance_route,
)
from ouroboros.orchestrator.route_policy import RouteRequirements
from ouroboros.orchestrator.verifier import VerifierVerdict


def _economics() -> EconomicsConfig:
    return EconomicsConfig(  # type: ignore[arg-type]
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


class _Adapter:
    runtime_backend = "claude"
    working_directory = "/tmp/project"
    permission_mode = "acceptEdits"
    self_governs_rate_limit = True
    capabilities = RuntimeCapabilities(
        skill_dispatch=True,
        targeted_resume=True,
        structured_output=True,
        model_override_support=ParamSupport.NATIVE,
    )


def _seed() -> Seed:
    return Seed(
        goal="bounded routing",
        acceptance_criteria=("ship it",),
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _executor(
    *,
    model_support: ParamSupport = ParamSupport.NATIVE,
) -> tuple[ParallelACExecutor, AsyncMock, list[BaseEvent]]:
    economics = _economics()
    router = build_model_router(economics, runtime_backend="claude")
    assert router is not None
    store = AsyncMock()
    events: list[BaseEvent] = []

    async def append(event: BaseEvent) -> None:
        events.append(event)

    store.append.side_effect = append
    store.query_execution_related_events.return_value = []
    adapter = _Adapter()
    adapter.capabilities = RuntimeCapabilities(
        skill_dispatch=True,
        targeted_resume=True,
        structured_output=True,
        model_override_support=model_support,
    )
    executor = ParallelACExecutor(
        adapter=adapter,  # type: ignore[arg-type]
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=False,
        model_router=router,
        route_economics=economics,
        ac_retry_attempts=99,
    )
    return executor, store, events


def _route_judgment(
    seed: Seed,
    *,
    route_id: str = "compat:claude:frugal",
    attempt_index: int = 0,
) -> BaseEvent:
    return BaseEvent(
        type="execution.ac.attempt_judged",
        aggregate_type="execution",
        aggregate_id="execution-1",
        data={
            "execution_id": "execution-1",
            "session_id": "session-1",
            "root_ac_index": 0,
            "ac_index": 0,
            "route_contract_version": 1,
            "route_episode_id": _episode_id(seed),
            "route_attempt_index": attempt_index,
            "route_id": route_id,
            "call_site": "parallel",
            "success": False,
            "outcome": "failed",
        },
    )


def _candidate(executor: ParallelACExecutor, route_id: str):
    projection = executor._route_economics
    from ouroboros.orchestrator.route_compat import build_route_compat_projection

    built = build_route_compat_projection(
        projection,
        model_router=executor._model_router,
        runtime_backend="claude",
    )
    assert built is not None
    return next(
        candidate for candidate in built.registry.candidates if candidate.route_id == route_id
    )


def _failed(executor: ParallelACExecutor, route_id: str) -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=0,
        ac_content="ship it",
        success=False,
        error="evidence missing",
        outcome=ACExecutionOutcome.FAILED,
        atomic_verifier_verdict=VerifierVerdict(
            passed=False,
            reasons=("missing",),
            failure_class=FailureClass.EVIDENCE_MISSING.value,
        ),
        route_candidate=_candidate(executor, route_id),
    )


def _episode_id(
    seed: Seed,
    *,
    execution_id: str = "execution-1",
    session_id: str = "session-1",
    root_ac_index: int = 0,
) -> str:
    criterion = seed.acceptance_criteria[root_ac_index]
    semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
    digest = hashlib.sha256(
        f"{execution_id or session_id}\0{root_ac_index}\0{semantic_ac_key}".encode()
    ).hexdigest()
    return f"route:{digest}"


def _route_event(
    seed: Seed,
    *,
    observation: RouteObservation,
    decision: RouteEscalationDecision | dict[str, object] | None,
    execution_id: str = "execution-1",
    session_id: str = "session-1",
    root_ac_index: object = 0,
    final_acceptance_declared: object = False,
) -> BaseEvent:
    criterion = seed.acceptance_criteria[0]
    semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
    decision_data = (
        decision.to_contract_data() if isinstance(decision, RouteEscalationDecision) else decision
    )
    return BaseEvent(
        type="execution.ac.route_observed",
        aggregate_type="execution",
        aggregate_id=execution_id,
        data={
            "schema_version": 1,
            "execution_id": execution_id,
            "session_id": session_id,
            "root_ac_index": root_ac_index,
            "semantic_ac_key": semantic_ac_key,
            "call_site": "parallel",
            "observation": observation.to_contract_data(),
            "decision": decision_data,
            "human_handoff_required": False,
            "final_acceptance_declared": final_acceptance_declared,
        },
    )


def _durable_first_failure(
    executor: ParallelACExecutor,
    seed: Seed,
) -> tuple[RouteObservation, RouteEscalationDecision]:
    from ouroboros.orchestrator.route_compat import build_route_compat_projection

    cheap = _candidate(executor, "compat:claude:frugal")
    requirements = RouteRequirements()
    observation = RouteObservation.from_candidate(
        cheap,
        requirements,
        episode_id=_episode_id(seed),
        attempt_index=0,
        verifier_outcome=VerifierOutcome.FAILED,
        failure_class=FailureClass.EVIDENCE_MISSING,
        escalation_reason=EscalationReason.CLASSIFIED_FAILURE,
    )
    projection = build_route_compat_projection(
        executor._route_economics,
        model_router=executor._model_router,
        runtime_backend="claude",
    )
    assert projection is not None
    decision = advance_route(
        projection.registry,
        requirements,
        current_route_id=cheap.route_id,
        attempted_route_ids=(cheap.route_id,),
        failure_class=FailureClass.EVIDENCE_MISSING,
    )
    return observation, decision


@pytest.mark.asyncio
async def test_live_loop_walks_each_route_once_then_succeeds() -> None:
    executor, _store, events = _executor()
    calls: list[str] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        route_id = kwargs.get("route_overrides", {}).get(0, "compat:claude:frugal")
        calls.append(route_id)
        if route_id == "compat:claude:frontier":
            return [
                ACExecutionResult(
                    ac_index=0,
                    ac_content="ship it",
                    success=True,
                    route_candidate=_candidate(executor, route_id),
                )
            ]
        return [_failed(executor, route_id)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    results = await executor._run_batch_with_bounded_route_escalation(
        seed=_seed(),
        batch_executable=[0],
        session_id="session-1",
        execution_id="execution-1",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts={0: 0},
        execution_counters=None,
    )

    assert calls == [
        "compat:claude:frugal",
        "compat:claude:standard",
        "compat:claude:frontier",
    ]
    assert isinstance(results[0], ACExecutionResult) and results[0].success is True
    route_events = [event for event in events if event.type == "execution.ac.route_observed"]
    assert [event.data["observation"]["route_id"] for event in route_events] == calls
    assert all(event.data["call_site"] == "parallel" for event in route_events)
    assert all(event.data["final_acceptance_declared"] is False for event in route_events)
    judgments = [event for event in events if event.type == "execution.ac.attempt_judged"]
    assert [event.data["route_id"] for event in judgments] == calls
    assert [event.data["route_attempt_index"] for event in judgments] == [0, 1, 2]
    assert all(event.data["route_episode_id"] == _episode_id(_seed()) for event in judgments)


@pytest.mark.asyncio
async def test_route_exhaustion_is_durable_blocked_human_handoff() -> None:
    executor, _store, events = _executor()

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        route_id = kwargs.get("route_overrides", {}).get(0, "compat:claude:frugal")
        return [_failed(executor, route_id)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    results = await executor._run_batch_with_bounded_route_escalation(
        seed=_seed(),
        batch_executable=[0],
        session_id="session-1",
        execution_id="execution-1",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts={0: 0},
        execution_counters=None,
    )

    result = results[0]
    assert isinstance(result, ACExecutionResult)
    assert result.outcome is ACExecutionOutcome.BLOCKED
    assert "human handoff required" in (result.error or "")
    last_route = [event for event in events if event.type == "execution.ac.route_observed"][-1]
    assert last_route.data["decision"]["reason"] == EscalationReason.ROUTES_EXHAUSTED.value
    assert last_route.data["human_handoff_required"] is True
    recovery = [event for event in events if event.type == "execution.ac.recovery_exhausted"][-1]
    assert recovery.data["human_handoff_required"] is True


@pytest.mark.asyncio
async def test_observation_persistence_failure_prevents_next_provider_effect() -> None:
    executor, store, _events = _executor()
    calls = 0

    async def fake_batch(**_kwargs: Any) -> list[ACExecutionResult]:
        nonlocal calls
        calls += 1
        return [_failed(executor, "compat:claude:frugal")]

    async def fail_route_observation(event: BaseEvent) -> None:
        if event.type == "execution.ac.route_observed":
            raise RuntimeError("store unavailable")

    store.append.side_effect = fail_route_observation
    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="store unavailable"):
        await executor._run_batch_with_bounded_route_escalation(
            seed=_seed(),
            batch_executable=[0],
            session_id="session-1",
            execution_id="execution-1",
            tools=[],
            tool_catalog=None,
            system_prompt="sys",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_prior_route_state_with_current_non_native_support_fails_before_provider() -> None:
    executor, store, _events = _executor(model_support=ParamSupport.IGNORED)
    seed = _seed()
    observation, decision = _durable_first_failure(executor, seed)
    route_event = _route_event(seed, observation=observation, decision=decision)

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        return [route_event] if kwargs.get("event_type") == "execution.ac.route_observed" else []

    store.query_execution_related_events.side_effect = query
    provider_calls = 0

    async def fake_batch(**_kwargs: Any) -> list[ACExecutionResult]:
        nonlocal provider_calls
        provider_calls += 1
        return [_failed(executor, "compat:claude:frugal")]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    assert executor._bounded_route_escalation_enabled is False

    with pytest.raises(RuntimeError, match="live routing is unavailable"):
        await executor._run_batch_with_verify_and_retry(
            seed=seed,
            batch_executable=[0],
            session_id="session-1",
            execution_id="execution-1",
            tools=[],
            tool_catalog=None,
            system_prompt="sys",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

    assert provider_calls == 0


@pytest.mark.asyncio
async def test_unmatched_route_judgment_seals_replay_before_provider() -> None:
    executor, store, _events = _executor()
    seed = _seed()
    judgment = _route_judgment(seed)

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        return [judgment] if kwargs.get("event_type") == "execution.ac.attempt_judged" else []

    store.query_execution_related_events.side_effect = query
    provider_calls = 0

    async def fake_batch(**_kwargs: Any) -> list[ACExecutionResult]:
        nonlocal provider_calls
        provider_calls += 1
        return [_failed(executor, "compat:claude:frugal")]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="no matching durable route observation"):
        await executor._run_batch_with_bounded_route_escalation(
            seed=seed,
            batch_executable=[0],
            session_id="session-1",
            execution_id="execution-1",
            tools=[],
            tool_catalog=None,
            system_prompt="sys",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

    assert provider_calls == 0


@pytest.mark.asyncio
async def test_legacy_attempt_judgment_does_not_block_route_observation_replay() -> None:
    executor, store, _events = _executor()
    seed = _seed()
    observation, decision = _durable_first_failure(executor, seed)
    route_event = _route_event(seed, observation=observation, decision=decision)
    legacy_judgment = BaseEvent(
        type="execution.ac.attempt_judged",
        aggregate_type="execution",
        aggregate_id="execution-1",
        data={
            "execution_id": "execution-1",
            "session_id": "session-1",
            "root_ac_index": 0,
            "success": False,
            "outcome": "failed",
        },
    )

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        return (
            [route_event]
            if kwargs.get("event_type") == "execution.ac.route_observed"
            else [legacy_judgment]
        )

    store.query_execution_related_events.side_effect = query

    histories, overrides, terminals = await executor._load_bounded_route_resume_state(
        seed=seed,
        execution_id="execution-1",
        session_id="session-1",
        root_ac_indices=(0,),
    )

    assert histories[0] == ("compat:claude:frugal",)
    assert overrides[0] == "compat:claude:standard"
    assert terminals == {}


@pytest.mark.asyncio
async def test_resume_uses_durable_next_route_without_repeating_cheapest() -> None:
    executor, store, _events = _executor()
    seed = _seed()
    cheap = _candidate(executor, "compat:claude:frugal")
    requirements = RouteRequirements()
    observation = RouteObservation.from_candidate(
        cheap,
        requirements,
        episode_id=_episode_id(seed),
        attempt_index=0,
        verifier_outcome=VerifierOutcome.FAILED,
        failure_class=FailureClass.EVIDENCE_MISSING,
        escalation_reason=EscalationReason.CLASSIFIED_FAILURE,
    )
    projection = executor._route_economics
    from ouroboros.orchestrator.route_compat import build_route_compat_projection

    built = build_route_compat_projection(
        projection,
        model_router=executor._model_router,
        runtime_backend="claude",
    )
    assert built is not None
    decision = advance_route(
        built.registry,
        requirements,
        current_route_id=cheap.route_id,
        attempted_route_ids=(cheap.route_id,),
        failure_class=FailureClass.EVIDENCE_MISSING,
    )
    store.query_execution_related_events.return_value = [
        _route_event(seed, observation=observation, decision=decision)
    ]
    calls: list[str] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        route_id = kwargs["route_overrides"][0]
        calls.append(route_id)
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="ship it",
                success=True,
                route_candidate=_candidate(executor, route_id),
            )
        ]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    await executor._run_batch_with_bounded_route_escalation(
        seed=seed,
        batch_executable=[0],
        session_id="session-1",
        execution_id="execution-1",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts={0: 0},
        execution_counters=None,
    )
    assert calls == ["compat:claude:standard"]


@pytest.mark.asyncio
async def test_resume_rejects_malformed_persisted_decision_before_provider_effect() -> None:
    executor, store, _events = _executor()
    seed = _seed()
    observation, decision = _durable_first_failure(executor, seed)
    malformed = decision.to_contract_data()
    malformed["unexpected"] = True
    store.query_execution_related_events.return_value = [
        _route_event(seed, observation=observation, decision=malformed)
    ]

    with pytest.raises(RuntimeError, match="invalid decision"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
async def test_resume_recomputes_and_rejects_changed_selected_route() -> None:
    executor, store, _events = _executor()
    seed = _seed()
    observation, decision = _durable_first_failure(executor, seed)
    changed = decision.to_contract_data()
    assert isinstance(changed["selected_route"], dict)
    changed["selected_route"] = _candidate(executor, "compat:claude:frontier").to_contract_data()
    changed["remaining_route_ids"] = ["compat:claude:standard"]
    store.query_execution_related_events.return_value = [
        _route_event(seed, observation=observation, decision=changed)
    ]

    with pytest.raises(RuntimeError, match="decision drifted"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drifted_observation",
    [
        lambda observation: replace(observation, cost_units=observation.cost_units + 1),
        lambda observation: replace(observation, model="changed-model"),
    ],
    ids=("cost", "model"),
)
async def test_resume_rejects_route_snapshot_drift(drifted_observation: Any) -> None:
    executor, store, _events = _executor()
    seed = _seed()
    observation, decision = _durable_first_failure(executor, seed)
    store.query_execution_related_events.return_value = [
        _route_event(
            seed,
            observation=drifted_observation(observation),
            decision=decision,
        )
    ]

    with pytest.raises(RuntimeError, match="configuration drift"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drifted_observation",
    [
        lambda observation: replace(observation, cost_units=observation.cost_units + 1),
        lambda observation: replace(observation, model="changed-model"),
    ],
    ids=("successor-cost", "successor-model"),
)
async def test_resume_rejects_successor_snapshot_drift_after_first_attempt(
    drifted_observation: Any,
) -> None:
    executor, store, _events = _executor()
    seed = _seed()
    first_observation, first_decision = _durable_first_failure(executor, seed)
    standard = _candidate(executor, "compat:claude:standard")
    second_observation = RouteObservation.from_candidate(
        standard,
        RouteRequirements(),
        episode_id=_episode_id(seed),
        attempt_index=1,
        verifier_outcome=VerifierOutcome.FAILED,
        failure_class=FailureClass.EVIDENCE_MISSING,
        escalation_reason=EscalationReason.CLASSIFIED_FAILURE,
    )
    from ouroboros.orchestrator.route_compat import build_route_compat_projection

    built = build_route_compat_projection(
        executor._route_economics,
        model_router=executor._model_router,
        runtime_backend="claude",
    )
    assert built is not None
    second_decision = advance_route(
        built.registry,
        RouteRequirements(),
        current_route_id=standard.route_id,
        attempted_route_ids=("compat:claude:frugal", standard.route_id),
        failure_class=FailureClass.EVIDENCE_MISSING,
    )
    store.query_execution_related_events.return_value = [
        _route_event(seed, observation=first_observation, decision=first_decision),
        _route_event(
            seed,
            observation=drifted_observation(second_observation),
            decision=second_decision,
        ),
    ]

    with pytest.raises(RuntimeError, match="durable successor chain"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("second_attempt_index", [0, 2], ids=("duplicate", "gap"))
async def test_resume_rejects_duplicate_or_gapped_observation_indices(
    second_attempt_index: int,
) -> None:
    executor, store, _events = _executor()
    seed = _seed()
    first_observation, first_decision = _durable_first_failure(executor, seed)
    standard = _candidate(executor, "compat:claude:standard")
    second_observation = RouteObservation.from_candidate(
        standard,
        RouteRequirements(),
        episode_id=_episode_id(seed),
        attempt_index=second_attempt_index,
        verifier_outcome=VerifierOutcome.FAILED,
        failure_class=FailureClass.EVIDENCE_MISSING,
        escalation_reason=EscalationReason.CLASSIFIED_FAILURE,
    )
    projection = executor._route_economics
    from ouroboros.orchestrator.route_compat import build_route_compat_projection

    built = build_route_compat_projection(
        projection,
        model_router=executor._model_router,
        runtime_backend="claude",
    )
    assert built is not None
    second_decision = advance_route(
        built.registry,
        RouteRequirements(),
        current_route_id=standard.route_id,
        attempted_route_ids=("compat:claude:frugal", standard.route_id),
        failure_class=FailureClass.EVIDENCE_MISSING,
    )
    store.query_execution_related_events.return_value = [
        _route_event(seed, observation=first_observation, decision=first_decision),
        _route_event(seed, observation=second_observation, decision=second_decision),
    ]

    with pytest.raises(RuntimeError, match="gap or duplicate"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
async def test_route_observation_cannot_claim_final_gate_acceptance() -> None:
    executor, store, _events = _executor()
    seed = _seed()
    cheap = _candidate(executor, "compat:claude:frugal")
    observation = RouteObservation.from_candidate(
        cheap,
        RouteRequirements(),
        episode_id=_episode_id(seed),
        attempt_index=0,
        verifier_outcome=VerifierOutcome.ATTEMPT_SUCCEEDED,
    )
    store.query_execution_related_events.return_value = [
        _route_event(
            seed,
            observation=observation,
            decision=None,
            final_acceptance_declared=True,
        )
    ]

    with pytest.raises(RuntimeError, match="cannot declare Final Gate acceptance"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
async def test_provisional_success_resume_blocks_replay_without_declaring_acceptance() -> None:
    executor, store, _events = _executor()
    seed = _seed()
    cheap = _candidate(executor, "compat:claude:frugal")
    observation = RouteObservation.from_candidate(
        cheap,
        RouteRequirements(),
        episode_id=_episode_id(seed),
        attempt_index=0,
        verifier_outcome=VerifierOutcome.ATTEMPT_SUCCEEDED,
    )
    store.query_execution_related_events.return_value = [
        _route_event(seed, observation=observation, decision=None)
    ]
    provider = AsyncMock()
    executor._execute_ac_batch = provider  # type: ignore[method-assign]

    results = await executor._run_batch_with_bounded_route_escalation(
        seed=seed,
        batch_executable=[0],
        session_id="session-1",
        execution_id="execution-1",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts={0: 0},
        execution_counters=None,
    )

    provider.assert_not_awaited()
    result = results[0]
    assert isinstance(result, ACExecutionResult)
    assert result.outcome is ACExecutionOutcome.BLOCKED
    assert "Final Gate did not durably accept" in (result.error or "")


@pytest.mark.asyncio
async def test_resume_rejects_boolean_root_index_before_membership_lookup() -> None:
    executor, store, _events = _executor()
    seed = _seed()
    observation, decision = _durable_first_failure(executor, seed)
    store.query_execution_related_events.return_value = [
        _route_event(
            seed,
            observation=observation,
            decision=decision,
            root_ac_index=True,
        )
    ]

    with pytest.raises(RuntimeError, match="invalid root AC index"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )
