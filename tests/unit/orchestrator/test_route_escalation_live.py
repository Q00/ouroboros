"""Live bounded-escalation wiring at the parallel provider boundary."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.config.models import EconomicsConfig, ModelConfig, TierConfig
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
    derive_semantic_ac_key,
)
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import AgentMessage, ParamSupport, RuntimeCapabilities
from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.model_routing import build_model_router
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor, _VerifyGateOutcome
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


def _multi_seed() -> Seed:
    return Seed(
        goal="bounded routing",
        acceptance_criteria=("ship first", "ship second"),
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _executor(
    *,
    model_support: ParamSupport = ParamSupport.NATIVE,
    base_tier: str = "frugal",
) -> tuple[ParallelACExecutor, AsyncMock, list[BaseEvent]]:
    economics = _economics()
    router = build_model_router(
        economics,
        runtime_backend="claude",
        base_tier_override=base_tier,
    )
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
    success: bool = False,
    outcome: str = "failed",
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
            "success": success,
            "outcome": outcome,
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
            "provisional_result": (
                {
                    "schema_version": 1,
                    "final_message_tail": "restored success",
                    "context_tools": [],
                    "duration_seconds": 0.0,
                    "session_id": None,
                    "retry_attempt": observation.attempt_index,
                    "verify_gate_outcome": None,
                }
                if observation.verifier_outcome is VerifierOutcome.ATTEMPT_SUCCEEDED
                else None
            ),
            "human_handoff_required": False,
            "final_acceptance_declared": final_acceptance_declared,
        },
    )


def _judgment_for_route_event(event: BaseEvent) -> BaseEvent:
    observation = RouteObservation.from_contract_data(event.data["observation"])
    success = observation.verifier_outcome is VerifierOutcome.ATTEMPT_SUCCEEDED
    outcome = {
        VerifierOutcome.ATTEMPT_SUCCEEDED: "succeeded",
        VerifierOutcome.FAILED: "failed",
        VerifierOutcome.BLOCKED: "blocked",
    }[observation.verifier_outcome]
    return BaseEvent(
        type="execution.ac.attempt_judged",
        aggregate_type="execution",
        aggregate_id=event.aggregate_id,
        data={
            "execution_id": event.data["execution_id"],
            "session_id": event.data["session_id"],
            "root_ac_index": event.data["root_ac_index"],
            "ac_index": event.data["root_ac_index"],
            "route_contract_version": 1,
            "route_episode_id": observation.episode_id,
            "route_attempt_index": observation.attempt_index,
            "route_id": observation.route_id,
            "call_site": "parallel",
            "success": success,
            "outcome": outcome,
        },
    )


def _set_route_replay_events(
    store: AsyncMock,
    route_events: list[BaseEvent],
    *,
    judgments: list[BaseEvent] | None = None,
) -> None:
    judgment_events = (
        [_judgment_for_route_event(event) for event in route_events]
        if judgments is None
        else judgments
    )

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        if kwargs.get("event_type") == "execution.ac.route_observed":
            return route_events
        if kwargs.get("event_type") == "execution.ac.attempt_judged":
            return judgment_events
        return []

    store.query_execution_related_events.side_effect = query


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
        expected = kwargs.get("route_overrides", {}).get(0)
        route_id = expected.route_id if expected is not None else "compat:claude:frugal"
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
        expected = kwargs.get("route_overrides", {}).get(0)
        route_id = expected.route_id if expected is not None else "compat:claude:frugal"
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
async def test_parallel_usage_limit_pauses_before_route_observation_or_escalation() -> None:
    executor, _store, events = _executor()
    calls = 0

    async def fake_batch(**_kwargs: Any) -> list[ACExecutionResult]:
        nonlocal calls
        calls += 1
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="ship it",
                success=False,
                error="Usage limit reached. Please try again in 5 hours.",
                outcome=ACExecutionOutcome.FAILED,
                messages=(
                    AgentMessage(
                        type="result",
                        content="Usage limit reached. Please try again in 5 hours.",
                        data={"subtype": "error", "error_type": "CodexCliError"},
                    ),
                ),
                route_candidate=_candidate(executor, "compat:claude:frugal"),
            )
        ]

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

    assert calls == 1
    assert isinstance(results[0], ACExecutionResult)
    assert results[0].outcome is ACExecutionOutcome.FAILED
    assert not any(
        event.type in {"execution.ac.attempt_judged", "execution.ac.route_observed"}
        for event in events
    )
    pause = next(event for event in events if event.type == "execution.ac.route_paused")
    assert pause.data["route"]["route_id"] == "compat:claude:frugal"
    assert pause.data["prior_route_ids"] == []
    assert pause.data["attempt_index"] == 0


@pytest.mark.asyncio
async def test_route_pause_aborts_remaining_stages_and_is_not_checkpointed_complete() -> None:
    executor, _store, _events = _executor()
    checkpoint_store = MagicMock()
    checkpoint_store.load.return_value = Result.ok(None)
    executor._checkpoint_store = checkpoint_store
    seed = _multi_seed()
    calls: list[list[int]] = []

    async def paused_batch(**kwargs: Any) -> list[ACExecutionResult]:
        indices = kwargs["batch_executable"]
        calls.append(indices)
        return [
            ACExecutionResult(
                ac_index=indices[0],
                ac_content="ship first",
                success=False,
                messages=(
                    AgentMessage(
                        type="result",
                        content="Usage limit reached. Please try again in 5 hours.",
                        data={"subtype": "error", "error_type": "CodexCliError"},
                    ),
                ),
                outcome=ACExecutionOutcome.FAILED,
                route_candidate=_candidate(executor, "compat:claude:frugal"),
            )
        ]

    executor._run_batch_with_verify_and_retry = paused_batch  # type: ignore[method-assign]
    graph = DependencyGraph(
        nodes=(
            ACNode(index=0, content="ship first", depends_on=()),
            ACNode(index=1, content="ship second", depends_on=(0,)),
        ),
        execution_levels=((0,), (1,)),
    )

    result = await executor.execute_parallel(
        seed,
        execution_plan=graph.to_execution_plan(),
        session_id="session-1",
        execution_id="execution-1",
        tools=[],
        system_prompt="sys",
    )

    assert calls == [[0]]
    assert result.recoverable_route_pause is True
    assert result.stages == ()
    checkpoint_store.save.assert_not_called()


@pytest.mark.asyncio
async def test_decomposed_leaf_pause_aborts_remaining_stages() -> None:
    executor, _store, _events = _executor()
    seed = _multi_seed()
    calls: list[list[int]] = []
    pause_message = AgentMessage(
        type="result",
        content="Usage limit reached. Please try again in 5 hours.",
        data={"subtype": "error", "error_type": "CodexCliError"},
    )

    async def decomposed_pause(**kwargs: Any) -> list[ACExecutionResult]:
        indices = kwargs["batch_executable"]
        calls.append(indices)
        return [
            ACExecutionResult(
                ac_index=indices[0],
                ac_content="ship first",
                success=False,
                is_decomposed=True,
                sub_results=(
                    ACExecutionResult(
                        ac_index=100,
                        ac_content="paused leaf",
                        success=False,
                        messages=(pause_message,),
                        outcome=ACExecutionOutcome.FAILED,
                        depth=1,
                    ),
                ),
                outcome=ACExecutionOutcome.FAILED,
            )
        ]

    executor._run_batch_with_verify_and_retry = decomposed_pause  # type: ignore[method-assign]
    graph = DependencyGraph(
        nodes=(
            ACNode(index=0, content="ship first", depends_on=()),
            ACNode(index=1, content="ship second", depends_on=(0,)),
        ),
        execution_levels=((0,), (1,)),
    )

    result = await executor.execute_parallel(
        seed,
        execution_plan=graph.to_execution_plan(),
        session_id="session-1",
        execution_id="execution-1",
        tools=[],
        system_prompt="sys",
    )

    assert calls == [[0]]
    assert result.recoverable_route_pause is True
    assert result.stages == ()


@pytest.mark.asyncio
async def test_decomposed_root_uses_legacy_retry_without_route_observation() -> None:
    executor, _store, events = _executor()
    executor._ac_retry_attempts = 1
    calls = 0
    forced_legacy: list[bool] = []

    async def composite_batch(**kwargs: Any) -> list[ACExecutionResult]:
        nonlocal calls
        calls += 1
        forced_legacy.append(bool(kwargs.get("force_legacy_routing", False)))
        success = calls == 2
        child = ACExecutionResult(
            ac_index=100,
            ac_content="legacy child",
            success=success,
            final_message="child complete" if success else "child failed",
            error=None if success else "evidence missing",
            outcome=(
                ACExecutionOutcome.SUCCEEDED if success else ACExecutionOutcome.FAILED
            ),
            depth=1,
        )
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="ship it",
                success=success,
                final_message="composite complete" if success else "composite failed",
                error=None if success else "evidence missing",
                is_decomposed=True,
                sub_results=(child,),
                outcome=(
                    ACExecutionOutcome.SUCCEEDED if success else ACExecutionOutcome.FAILED
                ),
            )
        ]

    executor._execute_ac_batch = composite_batch  # type: ignore[method-assign]
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

    assert calls == 2
    assert forced_legacy == [False, True]
    assert isinstance(results[0], ACExecutionResult) and results[0].success
    assert not any(event.type == "execution.ac.route_observed" for event in events)


@pytest.mark.asyncio
async def test_parallel_pause_resume_preserves_successful_sibling_and_exact_route() -> None:
    executor, store, events = _executor()
    seed = _multi_seed()
    cheap = _candidate(executor, "compat:claude:frugal")

    async def first_round(**_kwargs: Any) -> list[ACExecutionResult]:
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="ship first",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                route_candidate=cheap,
            ),
            ACExecutionResult(
                ac_index=1,
                ac_content="ship second",
                success=False,
                messages=(
                    AgentMessage(
                        type="result",
                        content="Quota window exhausted. Retry after 2 hours.",
                        data={"subtype": "error", "error_type": "OpenCodeError"},
                    ),
                ),
                outcome=ACExecutionOutcome.FAILED,
                route_candidate=cheap,
            ),
        ]

    executor._execute_ac_batch = first_round  # type: ignore[method-assign]
    first = await executor._run_batch_with_bounded_route_escalation(
        seed=seed,
        batch_executable=[0, 1],
        session_id="session-1",
        execution_id="execution-1",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts={0: 0, 1: 0},
        execution_counters=None,
    )
    assert isinstance(first[0], ACExecutionResult) and first[0].success

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        return [event for event in events if event.type == kwargs.get("event_type")]

    store.query_execution_related_events.side_effect = query
    resumed_indices: list[list[int]] = []

    async def resumed_round(**kwargs: Any) -> list[ACExecutionResult]:
        indices = kwargs["batch_indices"]
        resumed_indices.append(indices)
        assert kwargs["route_overrides"][1] == cheap
        return [
            ACExecutionResult(
                ac_index=1,
                ac_content="ship second",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                route_candidate=cheap,
            )
        ]

    executor._execute_ac_batch = resumed_round  # type: ignore[method-assign]
    resumed = await executor._run_batch_with_bounded_route_escalation(
        seed=seed,
        batch_executable=[0, 1],
        session_id="session-1",
        execution_id="execution-1",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts={0: 0, 1: 0},
        execution_counters=None,
    )

    assert resumed_indices == [[1]]
    assert all(isinstance(result, ACExecutionResult) and result.success for result in resumed)


@pytest.mark.asyncio
async def test_parallel_pause_capability_loss_fails_closed_before_provider() -> None:
    native, _native_store, events = _executor()
    cheap = _candidate(native, "compat:claude:frugal")
    await native._persist_parallel_route_pause(
        seed=_seed(),
        result=ACExecutionResult(
            ac_index=0,
            ac_content="ship it",
            success=False,
            outcome=ACExecutionOutcome.FAILED,
            route_candidate=cheap,
        ),
        root_ac_index=0,
        session_id="session-1",
        execution_id="execution-1",
        prior_route_ids=(),
    )
    paused_event = next(event for event in events if event.type == "execution.ac.route_paused")

    degraded, store, _degraded_events = _executor(model_support=ParamSupport.IGNORED)

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        if kwargs.get("event_type") == "execution.ac.route_paused":
            return [paused_event]
        return []

    store.query_execution_related_events.side_effect = query
    provider = AsyncMock()
    degraded._execute_ac_batch = provider  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="live routing is unavailable"):
        await degraded._run_batch_with_verify_and_retry(
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
    provider.assert_not_awaited()


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
async def test_live_successor_cost_drift_blocks_before_second_provider_effect() -> None:
    executor, store, events = _executor()
    provider_calls = 0

    async def execute_task(**_kwargs: Any):
        nonlocal provider_calls
        provider_calls += 1
        yield AgentMessage(
            type="result",
            content="evidence missing",
            data={"subtype": "error"},
        )

    executor._adapter.execute_task = execute_task  # type: ignore[attr-defined,method-assign]

    async def append_and_drift(event: BaseEvent) -> None:
        events.append(event)
        if event.type == "execution.ac.route_observed":
            assert executor._route_economics is not None
            tiers = dict(executor._route_economics.tiers)
            tiers["standard"] = tiers["standard"].model_copy(update={"cost_factor": 99})
            executor._route_economics = executor._route_economics.model_copy(
                update={"tiers": tiers}
            )

    store.append.side_effect = append_and_drift
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

    assert provider_calls == 1
    assert isinstance(results[0], ACExecutionResult)
    assert results[0].outcome is ACExecutionOutcome.BLOCKED
    assert "successor snapshot drifted" in (results[0].error or "")


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
async def test_route_observation_without_judgment_seals_replay_before_provider() -> None:
    executor, store, _events = _executor()
    seed = _seed()
    observation, decision = _durable_first_failure(executor, seed)
    route_event = _route_event(seed, observation=observation, decision=decision)
    _set_route_replay_events(store, [route_event], judgments=[])

    with pytest.raises(RuntimeError, match="no matching route-aware attempt judgment"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observation_outcome", "judgment_success", "judgment_outcome"),
    [
        (VerifierOutcome.FAILED, True, "succeeded"),
        (VerifierOutcome.FAILED, False, "blocked"),
        (VerifierOutcome.ATTEMPT_SUCCEEDED, False, "failed"),
        (VerifierOutcome.BLOCKED, False, "failed"),
    ],
    ids=(
        "successful-judgment-failed-observation",
        "blocked-judgment-failed-observation",
        "failed-judgment-successful-observation",
        "failed-judgment-blocked-observation",
    ),
)
async def test_route_judgment_must_semantically_match_observation(
    observation_outcome: VerifierOutcome,
    judgment_success: bool,
    judgment_outcome: str,
) -> None:
    executor, store, _events = _executor()
    seed = _seed()
    candidate = _candidate(executor, "compat:claude:frugal")
    observation = RouteObservation.from_candidate(
        candidate,
        RouteRequirements(),
        episode_id=_episode_id(seed),
        attempt_index=0,
        verifier_outcome=observation_outcome,
        failure_class=(
            None
            if observation_outcome is VerifierOutcome.ATTEMPT_SUCCEEDED
            else (
                FailureClass.BLOCKED
                if observation_outcome is VerifierOutcome.BLOCKED
                else FailureClass.EVIDENCE_MISSING
            )
        ),
        escalation_reason=(
            None
            if observation_outcome is VerifierOutcome.ATTEMPT_SUCCEEDED
            else EscalationReason.CLASSIFIED_FAILURE
        ),
    )
    route_event = _route_event(seed, observation=observation, decision=None)
    judgment = _route_judgment(
        seed,
        success=judgment_success,
        outcome=judgment_outcome,
    )
    _set_route_replay_events(store, [route_event], judgments=[judgment])

    with pytest.raises(RuntimeError, match="contradicts"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
async def test_route_judgment_rejects_unknown_outcome() -> None:
    executor, store, _events = _executor()
    seed = _seed()
    observation, decision = _durable_first_failure(executor, seed)
    route_event = _route_event(seed, observation=observation, decision=decision)
    _set_route_replay_events(
        store,
        [route_event],
        judgments=[_route_judgment(seed, outcome="mystery")],
    )

    with pytest.raises(RuntimeError, match="invalid outcome"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
async def test_route_judgment_canonicalizes_success_without_explicit_outcome() -> None:
    executor, _store, events = _executor()
    seed = _seed()
    await executor._emit_ac_attempt_judged(
        result=ACExecutionResult(
            ac_index=0,
            ac_content="ship it",
            success=True,
            route_candidate=_candidate(executor, "compat:claude:frugal"),
        ),
        root_ac_index=0,
        session_id="session-1",
        execution_id="execution-1",
        required=True,
        route_episode_id=_episode_id(seed),
        route_attempt_index=0,
    )

    judgment = next(event for event in events if event.type == "execution.ac.attempt_judged")
    assert judgment.data["success"] is True
    assert judgment.data["outcome"] == "succeeded"


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

    _set_route_replay_events(
        store,
        [route_event],
        judgments=[legacy_judgment, _judgment_for_route_event(route_event)],
    )

    (
        histories,
        overrides,
        terminals,
        provisional_successes,
    ) = await executor._load_bounded_route_resume_state(
        seed=seed,
        execution_id="execution-1",
        session_id="session-1",
        root_ac_indices=(0,),
    )

    assert histories[0] == ("compat:claude:frugal",)
    assert overrides[0].route_id == "compat:claude:standard"
    assert terminals == {}
    assert provisional_successes == {}


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
    _set_route_replay_events(
        store,
        [_route_event(seed, observation=observation, decision=decision)],
    )
    calls: list[str] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        route_id = kwargs["route_overrides"][0].route_id
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
    _set_route_replay_events(
        store,
        [_route_event(seed, observation=observation, decision=malformed)],
    )

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
    _set_route_replay_events(
        store,
        [_route_event(seed, observation=observation, decision=changed)],
    )

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
    route_events = [
        _route_event(
            seed,
            observation=drifted_observation(observation),
            decision=decision,
        )
    ]
    _set_route_replay_events(store, route_events)

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
    route_events = [
        _route_event(seed, observation=first_observation, decision=first_decision),
        _route_event(
            seed,
            observation=drifted_observation(second_observation),
            decision=second_decision,
        ),
    ]
    _set_route_replay_events(store, route_events)

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
    route_events = [
        _route_event(seed, observation=first_observation, decision=first_decision),
        _route_event(seed, observation=second_observation, decision=second_decision),
    ]
    _set_route_replay_events(store, route_events)

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
    route_events = [
        _route_event(
            seed,
            observation=observation,
            decision=None,
            final_acceptance_declared=True,
        )
    ]
    _set_route_replay_events(store, route_events)

    with pytest.raises(RuntimeError, match="cannot declare Final Gate acceptance"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
async def test_provisional_success_resume_seals_provider_and_defers_to_final_gate() -> None:
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
    _set_route_replay_events(
        store,
        [_route_event(seed, observation=observation, decision=None)],
    )
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
    assert result.outcome is ACExecutionOutcome.SUCCEEDED
    assert result.retry_attempt == 0
    assert result.error is None
    assert result.final_message == "restored success"


@pytest.mark.asyncio
async def test_provisional_success_resume_restores_verify_evidence_and_level_context(
    tmp_path: Any,
) -> None:
    executor, store, events = _executor()
    executor._run_verify_commands = True
    executor._adapter.working_directory = str(tmp_path)  # type: ignore[attr-defined]
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("ok")
    spec = AcceptanceCriterionSpec(
        description="ship it",
        expected_artifacts=("artifact.txt",),
    )
    seed = Seed(
        goal="bounded routing",
        acceptance_criteria=(spec,),
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )
    candidate = _candidate(executor, "compat:claude:frugal")
    original = ACExecutionResult(
        ac_index=0,
        ac_content="ship it",
        success=True,
        messages=(
            AgentMessage(
                type="tool",
                content="wrote artifact",
                tool_name="Write",
                data={"tool_input": {"file_path": str(artifact)}},
            ),
        ),
        final_message="artifact produced with verified output",
        duration_seconds=1.25,
        retry_attempt=0,
        verify_gate_outcome=_VerifyGateOutcome(
            passed=True,
            reason=None,
            output_tail="verified",
            workspace_digest=executor._workspace_content_digest(str(tmp_path)),
        ),
        route_candidate=candidate,
    )
    await executor._emit_ac_attempt_judged(
        result=original,
        root_ac_index=0,
        session_id="session-1",
        execution_id="execution-1",
        required=True,
        route_episode_id=_episode_id(seed),
        route_attempt_index=0,
    )
    await executor._persist_route_observation(
        seed=seed,
        result=original,
        root_ac_index=0,
        session_id="session-1",
        execution_id="execution-1",
        attempted_route_ids=(candidate.route_id,),
        failure_class=None,
        decision=None,
    )

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        return [event for event in events if event.type == kwargs.get("event_type")]

    store.query_execution_related_events.side_effect = query
    provider = AsyncMock()
    executor._execute_ac_batch = provider  # type: ignore[method-assign]
    replayed = await executor._run_batch_with_bounded_route_escalation(
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
    restored = replayed[0]
    assert isinstance(restored, ACExecutionResult)
    assert restored.final_message == original.final_message
    assert restored.messages[0].tool_name == "Write"
    assert restored.messages[0].data["tool_input"]["file_path"] == str(artifact)
    assert restored.verify_gate_outcome == original.verify_gate_outcome
    settled = await executor._settle_verify_gate_results(
        seed=seed,
        results=[restored],
        session_id="session-1",
        execution_id="execution-1",
    )
    assert settled[0].success is True


@pytest.mark.asyncio
async def test_terminal_route_replay_reports_last_zero_based_attempt() -> None:
    executor, store, events = _executor()

    async def fail_every_route(**kwargs: Any) -> list[ACExecutionResult]:
        expected = kwargs.get("route_overrides", {}).get(0)
        route_id = expected.route_id if expected is not None else "compat:claude:frugal"
        return [_failed(executor, route_id)]

    executor._execute_ac_batch = fail_every_route  # type: ignore[method-assign]
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

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        event_type = kwargs.get("event_type")
        return [event for event in events if event.type == event_type]

    store.query_execution_related_events.side_effect = query
    provider = AsyncMock()
    executor._execute_ac_batch = provider  # type: ignore[method-assign]
    replayed = await executor._run_batch_with_bounded_route_escalation(
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

    provider.assert_not_awaited()
    assert isinstance(replayed[0], ACExecutionResult)
    assert replayed[0].outcome is ACExecutionOutcome.BLOCKED
    assert replayed[0].retry_attempt == 2


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
