"""Live bounded-escalation wiring at the parallel provider boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping
from contextvars import ContextVar
from copy import deepcopy
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
from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalCapabilities,
    SessionSignalMode,
    SessionSignalSource,
    SessionSignalState,
)
from ouroboros.core.session_signal_projection import project_session_signal
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.events.session_signal import (
    create_session_signal_accepted_event,
    create_session_signal_queued_event,
    create_session_signal_requested_event,
)
from ouroboros.orchestrator import provider_admission
from ouroboros.orchestrator.ac_runtime_handle_manager import ACRuntimeHandleManager
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
    RuntimeCapabilities,
    RuntimeHandle,
)
from ouroboros.orchestrator.decomposition_limits import MAX_DECOMPOSITION_DEPTH
from ouroboros.orchestrator.decomposition_policy import (
    BounceCause,
    DecompositionChild,
    DecompositionDecisionRecord,
    DecompositionDisposition,
    DecompositionSource,
    SemanticAttestationStatus,
    StructuralCheckStatus,
)
from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.level_context import LevelContext, extract_level_context
from ouroboros.orchestrator.model_routing import build_model_router
from ouroboros.orchestrator.parallel_executor import (
    ParallelACExecutor,
    ParallelExecutionCancelled,
    _collect_result_conflict_files,
    _deserialize_conflict_files,
    _VerifyGateOutcome,
)
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
)
from ouroboros.orchestrator.route_escalation import (
    MAX_ROUTE_ATTEMPTS,
    EscalationReason,
    RouteEscalationDecision,
    RouteObservation,
    VerifierOutcome,
    advance_route,
)
from ouroboros.orchestrator.route_policy import RouteRequirements
from ouroboros.orchestrator.synapse import QueuedSessionSignal
from ouroboros.orchestrator.verifier import RetryAdmission, VerifierVerdict
from ouroboros.persistence.event_store import EventStore


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


def test_durable_conflict_projection_keeps_a_finite_per_path_bound() -> None:
    oversized = "p" * 32_769
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ship it",
        success=True,
        conflict_files=(oversized,),
    )

    with pytest.raises(RuntimeError, match="conflict projection"):
        _collect_result_conflict_files(result)
    with pytest.raises(RuntimeError, match="conflict projection"):
        _deserialize_conflict_files([oversized])


def _executor(
    *,
    model_support: ParamSupport = ParamSupport.NATIVE,
    base_tier: str = "frugal",
    enable_decomposition: bool = False,
    execute_task: Any | None = None,
    working_directory: str | None = None,
    max_decomposition_depth: int = 2,
    process_local_resume_nonce: str | None = None,
    event_store: AsyncMock | None = None,
    event_log: list[BaseEvent] | None = None,
    adapter_override: Any | None = None,
    ac_retry_attempts: int = 99,
    max_concurrent: int = 3,
) -> tuple[ParallelACExecutor, AsyncMock, list[BaseEvent]]:
    economics = _economics()
    router = build_model_router(
        economics,
        runtime_backend="claude",
        base_tier_override=base_tier,
    )
    assert router is not None
    store = event_store or AsyncMock()
    events: list[BaseEvent] = event_log if event_log is not None else []

    async def append(event: BaseEvent) -> None:
        events.append(event)

    store.append.side_effect = append
    store.query_execution_related_events.return_value = []
    adapter = adapter_override or _Adapter()
    if execute_task is not None:
        adapter.execute_task = execute_task  # type: ignore[attr-defined,method-assign]
    if working_directory is not None:
        adapter.working_directory = working_directory
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
        enable_decomposition=enable_decomposition,
        max_decomposition_depth=max_decomposition_depth,
        run_verify_commands=False,
        model_router=router,
        route_economics=economics,
        ac_retry_attempts=ac_retry_attempts,
        max_concurrent=max_concurrent,
        process_local_resume_nonce=process_local_resume_nonce,
    )
    return executor, store, events


def _live_executor(
    *,
    adapter: Any,
    event_store: EventStore,
    working_directory: str,
    process_local_resume_nonce: str,
    max_concurrent: int = 3,
    session_signal_hub: Any | None = None,
    executor_type: type[ParallelACExecutor] = ParallelACExecutor,
) -> ParallelACExecutor:
    """Build the production provider boundary against a real event store."""

    economics = _economics()
    router = build_model_router(
        economics,
        runtime_backend="claude",
        base_tier_override="frugal",
    )
    assert router is not None
    adapter.working_directory = working_directory
    return executor_type(
        adapter=adapter,
        event_store=event_store,
        console=MagicMock(),
        enable_decomposition=False,
        max_concurrent=max_concurrent,
        task_cwd=working_directory,
        run_verify_commands=False,
        model_router=router,
        route_economics=economics,
        ac_retry_attempts=99,
        cross_harness_redispatch=False,
        process_local_resume_nonce=process_local_resume_nonce,
        session_signal_hub=session_signal_hub,
    )


def test_larger_legacy_depth_disables_bounded_routes_with_native_configuration() -> None:
    """Depth compatibility cannot accidentally publish Routing D replay authority."""

    executor, _store, _events = _executor(
        max_decomposition_depth=MAX_DECOMPOSITION_DEPTH + 1,
    )

    assert executor._max_decomposition_depth == 5
    assert executor._durable_decomposition_replay_enabled is False
    assert executor._bounded_route_escalation_enabled is False


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
            "retry_attempt": attempt_index,
            "attempt_number": attempt_index + 1,
            "is_decomposed": False,
            "is_decomposed_child": False,
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


def _paused_route_handle(marker: str = "a") -> RuntimeHandle:
    return RuntimeHandle(
        backend="claude",
        native_session_id=f"paused-route-{marker}",
        cwd="/tmp/project",
        metadata={
            "session_scope_id": f"execution-1-paused-route-{marker}",
            "ac_dispatch_id": marker * 32,
            "ac_capsule_fingerprint": "sha256:" + marker * 64,
        },
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


def _split_decision(
    *,
    execution_id: str = "execution-1",
    root_ac_index: int = 0,
    node_identity: ExecutionNodeIdentity | None = None,
    child_descriptions: tuple[str, str] = ("first child", "second child"),
):
    identity = node_identity or ExecutionNodeIdentity.root(
        execution_context_id=execution_id, ac_index=root_ac_index
    )
    return DecompositionDecisionRecord(
        node_id=identity.node_id,
        source=DecompositionSource.PREFLIGHT,
        disposition=DecompositionDisposition.SPLIT,
        children=tuple(
            DecompositionChild(
                description=description,
                coverage_claims=(f"scope-{index}",),
                verification_hint=f"verify scope {index}",
            )
            for index, description in enumerate(child_descriptions)
        ),
        structural_status=StructuralCheckStatus.PASSED,
        semantic_status=SemanticAttestationStatus.ESTABLISHED,
        trustworthy=True,
    )


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
                    "schema_version": 2,
                    "context_summary": {
                        "ac_index": 0,
                        "ac_content": "ship it",
                        "success": True,
                        "tools_used": [],
                        "files_modified": [],
                        "key_output": "restored success",
                        "public_api": "",
                    },
                    "conflict_files": [],
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
            "retry_attempt": observation.attempt_index,
            "attempt_number": observation.attempt_index + 1,
            "is_decomposed": False,
            "is_decomposed_child": False,
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
async def test_live_bounded_route_too_big_transitions_to_verified_composite() -> None:
    """Routing D must not make the sole live bounce path unreachable."""

    executor, _store, events = _executor(enable_decomposition=True)
    node = ExecutionNodeIdentity.root(execution_context_id="execution-1", ac_index=0)
    decision = replace(
        _split_decision(node_identity=node),
        source=DecompositionSource.BOUNCE,
        cause=BounceCause.TOO_BIG,
    )
    route_calls = 0

    async def failed_atomic_batch(**_kwargs: Any) -> list[ACExecutionResult]:
        nonlocal route_calls
        route_calls += 1
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="ship it",
                success=False,
                error="The parent has multiple unfinished scopes.",
                messages=(AgentMessage(type="tool", content="attempted", tool_name="Read"),),
                outcome=ACExecutionOutcome.FAILED,
                atomic_verifier_verdict=VerifierVerdict(
                    passed=False,
                    reasons=("unfinished independent scopes",),
                    failure_class=FailureClass.SCOPE_CREEP.value,
                    retry_admission=RetryAdmission.REDISPATCH,
                ),
                route_candidate=_candidate(executor, "compat:claude:frugal"),
            )
        ]

    child_results = (
        ACExecutionResult(
            ac_index=0,
            ac_content=decision.children[0].description,
            success=True,
            final_message="first complete",
            depth=1,
        ),
        ACExecutionResult(
            ac_index=1,
            ac_content=decision.children[1].description,
            success=True,
            final_message="second complete",
            depth=1,
        ),
    )
    composite = ACExecutionResult(
        ac_index=0,
        ac_content="ship it",
        success=True,
        final_message="composite complete",
        is_decomposed=True,
        sub_results=child_results,
        decomposition_decision=decision,
    )
    executor._execute_ac_batch = failed_atomic_batch  # type: ignore[method-assign]
    executor._request_bounce_classification = AsyncMock(return_value=(BounceCause.TOO_BIG, True))
    executor._try_decompose_ac = AsyncMock(return_value=decision)  # type: ignore[method-assign]
    executor._execute_decomposition_children = AsyncMock(  # type: ignore[method-assign]
        return_value=composite
    )

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

    assert route_calls == 1
    assert isinstance(results[0], ACExecutionResult)
    assert results[0].success and results[0].is_decomposed
    executor._request_bounce_classification.assert_awaited_once()
    executor._try_decompose_ac.assert_awaited_once()  # type: ignore[attr-defined]
    executor._execute_decomposition_children.assert_awaited_once()  # type: ignore[attr-defined]
    event_types = [event.type for event in events]
    assert "execution.decomposition.bounce_classified" in event_types
    assert "execution.decomposition.decision_finalized" in event_types
    assert "execution.ac.composite_completed" in event_types
    assert "execution.ac.route_observed" not in event_types


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
@pytest.mark.parametrize("cancel_at", ("provider_result", "route_observed"))
async def test_parallel_cancellation_population_never_dispatches_route_successor(
    cancel_at: str,
) -> None:
    """Cancellation on either side of persistence closes the successor boundary."""

    from ouroboros.orchestrator.runner import clear_cancellation, request_cancellation

    session_id = f"session-cancel-{cancel_at}"
    executor, store, events = _executor()
    calls = 0

    async def append(event: BaseEvent) -> None:
        events.append(event)
        if cancel_at == "route_observed" and event.type == "execution.ac.route_observed":
            await request_cancellation(session_id, reason=cancel_at, cancelled_by="test")

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        nonlocal calls
        calls += 1
        expected = kwargs.get("route_overrides", {}).get(0)
        route_id = expected.route_id if expected is not None else "compat:claude:frugal"
        if cancel_at == "provider_result":
            await request_cancellation(session_id, reason=cancel_at, cancelled_by="test")
        return [_failed(executor, route_id)]

    store.append.side_effect = append
    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    try:
        with pytest.raises(ParallelExecutionCancelled):
            await executor._run_batch_with_bounded_route_escalation(
                seed=_seed(),
                batch_executable=[0],
                session_id=session_id,
                execution_id="execution-1",
                tools=[],
                tool_catalog=None,
                system_prompt="sys",
                level_contexts=[],
                ac_retry_attempts={0: 0},
                execution_counters={"messages_count": 1, "tool_calls_count": 0},
            )
    finally:
        await clear_cancellation(session_id)

    assert calls == 1
    judgments = [event for event in events if event.type == "execution.ac.attempt_judged"]
    observations = [event for event in events if event.type == "execution.ac.route_observed"]
    if cancel_at == "provider_result":
        assert judgments == []
        assert observations == []
    else:
        assert len(judgments) == 1
        assert len(observations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_error", "provider_metadata"),
    [
        ("permission denied: missing access to deployment", {}),
        ("missing required tool terraform", {}),
        ("configuration is not configured", {}),
        ("provider failed", {"errorType": "PermissionDenied"}),
        ("provider failed", {"kind": "PermissionDenied"}),
        ("provider failed", {"error_code": "MISSING_TOOL"}),
        ("provider failed", {"reason": "AUTHENTICATION_REQUIRED"}),
        ("provider failed", {"status": "environment_variable_not_configured"}),
        ("provider failed", {"status": 401}),
        ("provider failed", {"httpStatusCode": 403}),
    ],
)
async def test_parallel_hard_preconditions_stop_before_route_successor(
    provider_error: str,
    provider_metadata: dict[str, object],
) -> None:
    """Every established hard-block class spends exactly one provider call."""

    provider_calls = 0

    async def execute_task(**_kwargs: Any):
        nonlocal provider_calls
        provider_calls += 1
        yield AgentMessage(
            type="result",
            content=provider_error,
            data={"subtype": "error", **provider_metadata},
        )

    executor, _store, events = _executor(execute_task=execute_task)
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
    route_events = [event for event in events if event.type == "execution.ac.route_observed"]
    assert len(route_events) == 1
    assert route_events[0].data["observation"]["failure_class"] == FailureClass.BLOCKED.value
    assert route_events[0].data["decision"]["action"] == "blocked"
    assert route_events[0].data["human_handoff_required"] is True


def test_parallel_hard_precondition_outranks_retryable_verifier_label() -> None:
    """A verifier cannot spend past a provider-declared environment block."""

    executor, _store, _events = _executor()
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ship it",
        success=False,
        error="permission denied: missing access to deployment",
        outcome=ACExecutionOutcome.FAILED,
        atomic_verifier_verdict=VerifierVerdict(
            passed=False,
            reasons=("evidence unavailable",),
            failure_class=FailureClass.EVIDENCE_MISSING.value,
        ),
    )

    assert executor._failure_class_for_result(result) == FailureClass.BLOCKED.value


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
                runtime_handle=_paused_route_handle(),
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
async def test_parallel_quota_cancels_waiting_sibling_before_provider_effect(
    tmp_path: Any,
) -> None:
    """A shared quota closes every not-yet-entered provider boundary in the batch."""

    provider_effects: list[str] = []

    async def execute_task(**kwargs: Any):
        provider_effects.append(kwargs["model"])
        if len(provider_effects) != 1:
            raise AssertionError("waiting sibling crossed the shared quota gate")
        yield AgentMessage(
            type="result",
            content="Usage limit reached. Please try again later.",
            data={"subtype": "error", "error_type": "CodexCliError"},
            resume_handle=RuntimeHandle(
                backend="claude",
                native_session_id="batch-quota-owner",
                cwd=str(tmp_path),
            ),
        )

    executor, _store, events = _executor(
        max_concurrent=1,
        execute_task=execute_task,
        working_directory=str(tmp_path),
    )
    seed = _multi_seed()
    graph = DependencyGraph(
        nodes=(
            ACNode(index=0, content="ship first", depends_on=()),
            ACNode(index=1, content="ship second", depends_on=()),
        ),
        execution_levels=((0, 1),),
    )

    result = await executor.execute_parallel(
        seed,
        execution_plan=graph.to_execution_plan(),
        session_id="session-quota-gate",
        execution_id="execution-quota-gate",
        tools=[],
        system_prompt="sys",
    )

    assert len(provider_effects) == 1
    assert result.recoverable_route_pause is True
    assert [item.ac_index for item in result.results] == [0]
    assert result.stages == ()
    assert not any(
        event.type == "execution.ac.attempt_judged" and event.data.get("root_ac_index") == 1
        for event in events
    )


@pytest.mark.asyncio
async def test_parallel_quota_preempts_completed_sibling_composite_recovery() -> None:
    """Post-batch legacy recovery cannot dispatch after any sibling reports quota."""

    executor, _store, events = _executor(enable_decomposition=True)
    pause_message = AgentMessage(
        type="result",
        content="Usage limit reached. Please try again later.",
        data={"subtype": "error", "error_type": "CodexCliError"},
    )

    async def fake_batch(**_kwargs: Any) -> list[ACExecutionResult]:
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="ship first",
                success=False,
                is_decomposed=True,
                sub_results=(),
                outcome=ACExecutionOutcome.FAILED,
            ),
            ACExecutionResult(
                ac_index=1,
                ac_content="ship second",
                success=False,
                messages=(pause_message,),
                final_message=pause_message.content,
                outcome=ACExecutionOutcome.FAILED,
                route_candidate=_candidate(executor, "compat:claude:frugal"),
                runtime_handle=_paused_route_handle(),
            ),
        ]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    continue_composite = AsyncMock(
        side_effect=AssertionError("composite recovery crossed the quota boundary")
    )
    executor._continue_decomposed_legacy_recovery = continue_composite  # type: ignore[method-assign]

    results = await executor._run_batch_with_bounded_route_escalation(
        seed=_multi_seed(),
        batch_executable=[0, 1],
        session_id="session-composite-quota",
        execution_id="execution-composite-quota",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts={0: 0, 1: 0},
        execution_counters=None,
    )

    continue_composite.assert_not_awaited()
    assert isinstance(results[0], BaseException)
    assert isinstance(results[1], ACExecutionResult)
    assert [event.type for event in events].count("execution.ac.route_paused") == 1
    assert not any(event.type == "execution.ac.attempt_judged" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry_metadata",
    [
        {"retry_after_ms": 7_200_000},
        {"resume_after": "2099-01-01T02:00:00+00:00"},
        {"reset_at": "2099-01-01T02:00:00+00:00"},
    ],
)
async def test_supported_quota_metadata_never_authorizes_a_route_successor(
    retry_metadata: dict[str, object],
) -> None:
    """Every established retry encoding pauses before another provider effect."""

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
                error="Provider quota window.",
                outcome=ACExecutionOutcome.FAILED,
                messages=(
                    AgentMessage(
                        type="result",
                        content="Provider quota window.",
                        data={
                            "subtype": "error",
                            "reason": "quota window",
                            **retry_metadata,
                        },
                    ),
                ),
                route_candidate=_candidate(executor, "compat:claude:frugal"),
                runtime_handle=_paused_route_handle(),
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
    assert not any(
        event.type in {"execution.ac.attempt_judged", "execution.ac.route_observed"}
        for event in events
    )
    assert [event.type for event in events].count("execution.ac.route_paused") == 1


@pytest.mark.asyncio
async def test_hostile_quota_mapping_protocol_stops_after_one_route() -> None:
    """Mapping.get failures pause before judgment, observation, or successor effects."""

    class ExplodingMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise RuntimeError(f"provider mapping rejected {key}")

        def __iter__(self) -> Iterator[str]:
            yield "status_code"

        def __len__(self) -> int:
            return 1

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
                error="Provider request failed.",
                outcome=ACExecutionOutcome.FAILED,
                messages=(
                    AgentMessage(
                        type="result",
                        content="Provider request failed.",
                        data={"subtype": "error", "details": ExplodingMapping()},
                    ),
                ),
                route_candidate=_candidate(executor, "compat:claude:frugal"),
                runtime_handle=_paused_route_handle(),
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
    assert not any(
        event.type in {"execution.ac.attempt_judged", "execution.ac.route_observed"}
        for event in events
    )
    assert [event.type for event in events].count("execution.ac.route_paused") == 1


@pytest.mark.asyncio
async def test_quota_metadata_population_overflow_stops_after_one_route() -> None:
    """Overflow of the bounded metadata walk fails closed before escalation."""

    executor, _store, events = _executor()
    calls = 0
    data: dict[str, object] = {"subtype": "error", "reason": "provider failure"}
    cursor = data
    for _ in range(33):
        child: dict[str, object] = {}
        cursor["details"] = child
        cursor = child
    cursor["quota_exhausted"] = True

    async def fake_batch(**_kwargs: Any) -> list[ACExecutionResult]:
        nonlocal calls
        calls += 1
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="ship it",
                success=False,
                error="Provider request failed.",
                outcome=ACExecutionOutcome.FAILED,
                messages=(
                    AgentMessage(
                        type="result",
                        content="Provider request failed.",
                        data=data,
                    ),
                ),
                route_candidate=_candidate(executor, "compat:claude:frugal"),
                runtime_handle=_paused_route_handle(),
            )
        ]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
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
    assert not any(
        event.type in {"execution.ac.attempt_judged", "execution.ac.route_observed"}
        for event in events
    )
    assert [event.type for event in events].count("execution.ac.route_paused") == 1


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
@pytest.mark.parametrize("resume_succeeds", [True, False])
async def test_partial_composite_resume_reuses_only_exact_paused_child_boundary(
    tmp_path: Any,
    resume_succeeds: bool,
) -> None:
    provider_resume_handles: list[RuntimeHandle | None] = []

    async def execute_task(**kwargs: Any):
        provider_resume_handles.append(kwargs.get("resume_handle"))
        call_number = len(provider_resume_handles)
        if call_number == 1:
            yield AgentMessage(
                type="result",
                content="first child completed once",
                data={"subtype": "success"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="completed-child-session",
                    cwd=str(tmp_path),
                ),
            )
        elif call_number == 2:
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "CodexCliError"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="paused-child-session",
                    cwd=str(tmp_path),
                ),
            )
        elif call_number == 3:
            handle = kwargs.get("resume_handle")
            assert isinstance(handle, RuntimeHandle)
            assert handle.native_session_id == "paused-child-session"
            yield AgentMessage(
                type="result",
                content=(
                    "paused child resumed successfully"
                    if resume_succeeds
                    else "paused child resumed with a classified failure"
                ),
                data={"subtype": "success" if resume_succeeds else "error"},
                resume_handle=handle,
            )
        elif call_number == 4 and not resume_succeeds:
            assert kwargs.get("resume_handle") is None
            yield AgentMessage(
                type="result",
                content=('{"cause":"BAD_SPEC","has_remaining_scope":false}'),
            )
        else:  # pragma: no cover - the assertion below is the primary guard
            raise AssertionError("a completed child was redispatched")

    executor, store, events = _executor(
        enable_decomposition=True,
        execute_task=execute_task,
        working_directory=str(tmp_path),
        max_decomposition_depth=1,
    )
    seed = _seed()
    decision = _split_decision()
    root_identity = ExecutionNodeIdentity.root(
        execution_context_id="execution-1",
        ac_index=0,
    )
    executor._publish_event_owned_decomposition_decision(decision)

    first = await executor._run_batch_with_bounded_route_escalation(
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
    assert len(provider_resume_handles) == 2, repr(first)
    assert isinstance(first[0], ACExecutionResult)
    has_pause = any(
        "Usage limit" in message.content
        for child in first[0].sub_results
        for message in child.messages
    )
    assert has_pause
    partial = next(event for event in events if event.type == "execution.ac.composite_paused")
    assert partial.data["frames"][0]["paused_child_index"] == 1
    assert len(partial.data["frames"][0]["completed_children"]) == 1
    assert not any(
        event.type == "execution.session.failed"
        and event.data.get("node_id") == root_identity.child(1).node_id
        for event in events
    )

    async def append_batch(batch: list[BaseEvent]) -> None:
        events.extend(batch)

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        return [event for event in events if event.type == kwargs.get("event_type")]

    async def replay(aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        return [
            event
            for event in events
            if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
        ]

    store.append_batch.side_effect = append_batch
    store.query_execution_related_events.side_effect = query
    store.replay.side_effect = replay
    resumed_executor, _same_store, _same_events = _executor(
        enable_decomposition=True,
        execute_task=execute_task,
        working_directory=str(tmp_path),
        max_decomposition_depth=1,
        process_local_resume_nonce=executor._process_local_resume_nonce,
        event_store=store,
        event_log=events,
        adapter_override=executor._adapter,
    )
    resumed_executor._publish_event_owned_decomposition_decision(decision)
    resumed = await resumed_executor._run_batch_with_bounded_route_escalation(
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

    assert len(provider_resume_handles) == (3 if resume_succeeds else 4)
    restored = resumed[0]
    assert isinstance(restored, ACExecutionResult)
    assert restored.success is resume_succeeds
    assert restored.is_decomposed
    assert tuple(child.final_message for child in restored.sub_results) == (
        "first child completed once",
        (
            "paused child resumed successfully"
            if resume_succeeds
            else "paused child resumed with a classified failure"
        ),
    )
    assert any(event.type == "execution.ac.composite_completed" for event in events)


@pytest.mark.asyncio
async def test_nested_partial_composite_resume_reuses_exact_depth_two_leaf_boundary(
    tmp_path: Any,
) -> None:
    provider_resume_handles: list[RuntimeHandle | None] = []

    async def execute_task(**kwargs: Any):
        provider_resume_handles.append(kwargs.get("resume_handle"))
        call_number = len(provider_resume_handles)
        if call_number < 3:
            yield AgentMessage(
                type="result",
                content=f"completed leaf {call_number} once",
                data={"subtype": "success"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id=f"completed-leaf-{call_number}",
                    cwd=str(tmp_path),
                ),
            )
        elif call_number == 3:
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "CodexCliError"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="nested-paused-leaf",
                    cwd=str(tmp_path),
                ),
            )
        elif call_number == 4:
            handle = kwargs.get("resume_handle")
            assert isinstance(handle, RuntimeHandle)
            assert handle.native_session_id == "nested-paused-leaf"
            yield AgentMessage(
                type="result",
                content="nested paused leaf resumed",
                data={"subtype": "success"},
                resume_handle=handle,
            )
        else:  # pragma: no cover - the call count below is the primary guard
            raise AssertionError("a completed nested leaf was redispatched")

    executor, store, events = _executor(
        enable_decomposition=True,
        execute_task=execute_task,
        working_directory=str(tmp_path),
        max_decomposition_depth=2,
    )
    seed = _seed()
    root = ExecutionNodeIdentity.root(execution_context_id="execution-1", ac_index=0)
    root_decision = _split_decision(
        node_identity=root,
        child_descriptions=("outer completed", "nested composite"),
    )
    nested_decision = _split_decision(
        node_identity=root.child(1),
        child_descriptions=("nested completed", "nested paused"),
    )
    executor._publish_event_owned_decomposition_decision(root_decision)
    executor._publish_event_owned_decomposition_decision(
        DecompositionDecisionRecord(
            node_id=root.child(0).node_id,
            source=DecompositionSource.PREFLIGHT,
            disposition=DecompositionDisposition.ATOMIC,
        )
    )
    executor._publish_event_owned_decomposition_decision(nested_decision)

    first = await executor._run_batch_with_bounded_route_escalation(
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
    assert len(provider_resume_handles) == 3, repr(first)
    partial = next(event for event in events if event.type == "execution.ac.composite_paused")
    assert [frame["paused_child_index"] for frame in partial.data["frames"]] == [1, 1]
    assert partial.data["paused_leaf"]["node_id"] == root.child(1).child(1).node_id

    async def append_batch(batch: list[BaseEvent]) -> None:
        events.extend(batch)

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        return [event for event in reversed(events) if event.type == kwargs.get("event_type")]

    async def replay(aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        return [
            event
            for event in events
            if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
        ]

    store.append_batch.side_effect = append_batch
    store.query_execution_related_events.side_effect = query
    store.replay.side_effect = replay
    resumed_executor, _same_store, _same_events = _executor(
        enable_decomposition=True,
        execute_task=execute_task,
        working_directory=str(tmp_path),
        max_decomposition_depth=2,
        process_local_resume_nonce=executor._process_local_resume_nonce,
        event_store=store,
        event_log=events,
        adapter_override=executor._adapter,
    )
    resumed_executor._publish_event_owned_decomposition_decision(root_decision)
    resumed_executor._publish_event_owned_decomposition_decision(nested_decision)
    resumed = await resumed_executor._run_batch_with_bounded_route_escalation(
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

    assert len(provider_resume_handles) == 4
    restored = resumed[0]
    assert isinstance(restored, ACExecutionResult)
    assert restored.success and restored.is_decomposed
    nested = restored.sub_results[1]
    assert nested.success and nested.is_decomposed
    assert tuple(child.final_message for child in nested.sub_results) == (
        "completed leaf 2 once",
        "nested paused leaf resumed",
    )


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
        children = tuple(
            ACExecutionResult(
                ac_index=100 + index,
                ac_content=content,
                success=success,
                final_message="child complete" if success else "child failed",
                error=None if success else "evidence missing",
                outcome=(ACExecutionOutcome.SUCCEEDED if success else ACExecutionOutcome.FAILED),
                depth=1,
            )
            for index, content in enumerate(("first child", "second child"))
        )
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="ship it",
                success=success,
                final_message="composite complete" if success else "composite failed",
                error=None if success else "evidence missing",
                is_decomposed=True,
                sub_results=children,
                outcome=(ACExecutionOutcome.SUCCEEDED if success else ACExecutionOutcome.FAILED),
                decomposition_decision=_split_decision(),
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
                runtime_handle=_paused_route_handle(),
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
        resume_state = kwargs["route_resume_states"][1]
        assert resume_state.candidate == cheap
        assert resume_state.expected_route_candidate is None
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
async def test_live_first_route_pause_resumes_same_capsule_and_provider_handle(
    tmp_path: Any,
) -> None:
    """The initial route is replayed without inventing successor authority."""

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'first-route-pause.db'}")
    await store.initialize()
    adapter = _Adapter()
    calls: list[dict[str, Any]] = []

    async def execute_task(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "CodexCliError"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="first-route-provider",
                    cwd=str(tmp_path),
                    approval_mode="acceptEdits",
                ),
            )
            return
        yield AgentMessage(
            type="result",
            content="resumed first route",
            data={"subtype": "success"},
        )

    adapter.execute_task = execute_task  # type: ignore[attr-defined,method-assign]
    nonce = "1" * 32
    seed = _seed()
    run_kwargs = {
        "seed": seed,
        "batch_executable": [0],
        "session_id": "session-live-first-pause",
        "execution_id": "execution-live-first-pause",
        "tools": [],
        "tool_catalog": None,
        "system_prompt": "sys",
        "level_contexts": [],
        "ac_retry_attempts": {0: 0},
        "execution_counters": None,
    }
    try:
        first_executor = _live_executor(
            adapter=adapter,
            event_store=store,
            working_directory=str(tmp_path),
            process_local_resume_nonce=nonce,
        )
        first = await first_executor._run_batch_with_bounded_route_escalation(**run_kwargs)
        assert isinstance(first[0], ACExecutionResult) and not first[0].success

        pauses = await store.query_execution_related_events(
            "execution-live-first-pause",
            event_type="execution.ac.route_paused",
            limit=4,
        )
        assert len(pauses) == 1
        resume_state = pauses[0].data["resume_state"]
        assert resume_state["retry_attempt"] == 0
        assert resume_state["retry_prompt_extra"] == ""
        assert resume_state["route_id_override"] is None
        assert resume_state["expected_route_candidate"] is None

        resumed_executor = _live_executor(
            adapter=adapter,
            event_store=store,
            working_directory=str(tmp_path),
            process_local_resume_nonce=nonce,
        )
        resumed = await resumed_executor._run_batch_with_bounded_route_escalation(**run_kwargs)

        assert isinstance(resumed[0], ACExecutionResult) and resumed[0].success
        assert [call["model"] for call in calls] == ["haiku-x", "haiku-x"]
        restored_handle = calls[1]["resume_handle"]
        assert restored_handle.native_session_id == "first-route-provider"
        assert (
            restored_handle.metadata["ac_capsule_fingerprint"]
            == resume_state["capsule_fingerprint"]
        )
        assert calls[1]["prompt"] == calls[0]["prompt"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_live_terminal_parallel_handle_cannot_publish_recoverable_pause(
    tmp_path: Any,
) -> None:
    """A provider terminal event cannot be rebranded as resumable authority."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'terminal-route-pause.db'}")
    await store.initialize()
    adapter = _Adapter()
    provider_calls = 0

    async def execute_task(*_args: Any, **_kwargs: Any) -> AsyncIterator[AgentMessage]:
        nonlocal provider_calls
        provider_calls += 1
        yield AgentMessage(
            type="result",
            content="Usage limit reached. Please try again in 5 hours.",
            data={"subtype": "error", "error_type": "CodexCliError"},
            resume_handle=RuntimeHandle(
                backend="claude",
                native_session_id="terminal-provider-session",
                cwd=str(tmp_path),
                approval_mode="acceptEdits",
                metadata={"runtime_event_type": "session.failed"},
            ),
        )

    adapter.execute_task = execute_task  # type: ignore[attr-defined,method-assign]
    executor = _live_executor(
        adapter=adapter,
        event_store=store,
        working_directory=str(tmp_path),
        process_local_resume_nonce="9" * 32,
    )
    try:
        with pytest.raises(
            RuntimeError,
            match="parallel route pause has no exact resumable provider boundary",
        ):
            await executor._run_batch_with_bounded_route_escalation(
                seed=_seed(),
                batch_executable=[0],
                session_id="session-terminal-route-pause",
                execution_id="execution-terminal-route-pause",
                tools=[],
                tool_catalog=None,
                system_prompt="sys",
                level_contexts=[],
                ac_retry_attempts={0: 0},
                execution_counters=None,
            )

        assert provider_calls == 1
        pauses = await store.query_execution_related_events(
            "execution-terminal-route-pause",
            event_type="execution.ac.route_paused",
            limit=2,
        )
        assert pauses == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_live_repeated_quota_restores_latest_unconsumed_provider_boundary(
    tmp_path: Any,
) -> None:
    """Newest-first storage cannot roll a repeated pause back to an older dispatch."""

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'repeated-route-pause.db'}")
    await store.initialize()
    adapter = _Adapter()
    calls: list[dict[str, Any]] = []

    async def execute_task(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
        calls.append(dict(kwargs))
        if len(calls) <= 2:
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "CodexCliError"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="repeated-quota-provider",
                    cwd=str(tmp_path),
                    approval_mode="acceptEdits",
                ),
            )
            return
        yield AgentMessage(
            type="result",
            content="resumed after the second quota window",
            data={"subtype": "success"},
        )

    adapter.execute_task = execute_task  # type: ignore[attr-defined,method-assign]
    nonce = "5" * 32
    seed = _seed()
    run_kwargs = {
        "seed": seed,
        "batch_executable": [0],
        "session_id": "session-live-repeated-pause",
        "execution_id": "execution-live-repeated-pause",
        "tools": [],
        "tool_catalog": None,
        "system_prompt": "sys",
        "level_contexts": [],
        "ac_retry_attempts": {0: 0},
        "execution_counters": None,
    }
    try:
        for _pause_number in range(2):
            executor = _live_executor(
                adapter=adapter,
                event_store=store,
                working_directory=str(tmp_path),
                process_local_resume_nonce=nonce,
            )
            paused = await executor._run_batch_with_bounded_route_escalation(**run_kwargs)
            assert isinstance(paused[0], ACExecutionResult) and not paused[0].success

        pauses = await store.query_execution_related_events(
            "execution-live-repeated-pause",
            event_type="execution.ac.route_paused",
            limit=4,
        )
        assert len(pauses) == 2
        latest_resume_state = pauses[0].data["resume_state"]
        assert latest_resume_state["dispatch_id"] != pauses[1].data["resume_state"]["dispatch_id"]

        resumed_executor = _live_executor(
            adapter=adapter,
            event_store=store,
            working_directory=str(tmp_path),
            process_local_resume_nonce=nonce,
        )
        resumed = await resumed_executor._run_batch_with_bounded_route_escalation(**run_kwargs)

        assert isinstance(resumed[0], ACExecutionResult) and resumed[0].success
        assert len(calls) == 3
        assert calls[2]["resume_handle"].native_session_id == "repeated-quota-provider"
        runtime_events = await store.replay(
            "execution",
            latest_resume_state["runtime_scope_id"],
        )
        resumed_dispatch = [
            event for event in runtime_events if event.type == "execution.ac.attempt.dispatched"
        ][-1]
        assert (
            resumed_dispatch.data["previous_ac_dispatch_id"] == latest_resume_state["dispatch_id"]
        )
        assert (
            calls[2]["resume_handle"].metadata["ac_dispatch_id"]
            == resumed_dispatch.data["ac_dispatch_id"]
        )
        assert calls[0]["prompt"] == calls[1]["prompt"] == calls[2]["prompt"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_live_successor_route_pause_restores_retry_prompt_and_provider_handle(
    tmp_path: Any,
) -> None:
    """An escalated pause replays the exact successor capsule and provider turn."""

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'successor-route-pause.db'}")
    await store.initialize()
    adapter = _Adapter()
    calls: list[dict[str, Any]] = []

    async def execute_task(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            yield AgentMessage(
                type="result",
                content="evidence missing",
                data={"subtype": "error"},
            )
            return
        if len(calls) == 2:
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "CodexCliError"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="successor-route-provider",
                    cwd=str(tmp_path),
                    approval_mode="acceptEdits",
                ),
            )
            return
        yield AgentMessage(
            type="result",
            content="resumed successor route",
            data={"subtype": "success"},
        )

    adapter.execute_task = execute_task  # type: ignore[attr-defined,method-assign]
    nonce = "2" * 32
    seed = _seed()
    run_kwargs = {
        "seed": seed,
        "batch_executable": [0],
        "session_id": "session-live-successor-pause",
        "execution_id": "execution-live-successor-pause",
        "tools": [],
        "tool_catalog": None,
        "system_prompt": "sys",
        "level_contexts": [],
        "ac_retry_attempts": {0: 0},
        "execution_counters": None,
    }
    try:
        first_executor = _live_executor(
            adapter=adapter,
            event_store=store,
            working_directory=str(tmp_path),
            process_local_resume_nonce=nonce,
        )
        first = await first_executor._run_batch_with_bounded_route_escalation(**run_kwargs)
        assert isinstance(first[0], ACExecutionResult) and not first[0].success

        pauses = await store.query_execution_related_events(
            "execution-live-successor-pause",
            event_type="execution.ac.route_paused",
            limit=4,
        )
        assert len(pauses) == 1
        resume_state = pauses[0].data["resume_state"]
        assert resume_state["retry_attempt"] == 1
        assert resume_state["retry_prompt_extra"]
        assert resume_state["route_id_override"] == "compat:claude:standard"
        assert resume_state["expected_route_candidate"]["route_id"] == ("compat:claude:standard")

        resumed_executor = _live_executor(
            adapter=adapter,
            event_store=store,
            working_directory=str(tmp_path),
            process_local_resume_nonce=nonce,
        )
        resumed = await resumed_executor._run_batch_with_bounded_route_escalation(**run_kwargs)

        assert isinstance(resumed[0], ACExecutionResult) and resumed[0].success
        assert [call["model"] for call in calls] == ["haiku-x", "sonnet-x", "sonnet-x"]
        restored_handle = calls[2]["resume_handle"]
        assert restored_handle.native_session_id == "successor-route-provider"
        assert (
            restored_handle.metadata["ac_capsule_fingerprint"]
            == resume_state["capsule_fingerprint"]
        )
        assert calls[2]["prompt"] == calls[1]["prompt"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_live_entered_sibling_becomes_durable_uncertain_handoff(
    tmp_path: Any,
) -> None:
    """Only a sibling cancelled before authority entry may remain pending."""

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'entered-sibling-pause.db'}")
    await store.initialize()
    adapter = _Adapter()
    sibling_entered = asyncio.Event()
    never_release_sibling = asyncio.Event()
    calls: list[tuple[str, RuntimeHandle | None]] = []

    async def execute_task(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
        prompt = kwargs["prompt"]
        runtime_handle = kwargs.get("resume_handle")
        ac_name = "first" if "## Your Task (AC 1)\nship first" in prompt else "second"
        calls.append((ac_name, runtime_handle))
        if ac_name == "first":
            if runtime_handle is not None and runtime_handle.resume_session_id is not None:
                yield AgentMessage(
                    type="result",
                    content="quota owner resumed",
                    data={"subtype": "success"},
                )
                return
            await sibling_entered.wait()
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "CodexCliError"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="parallel-quota-owner",
                    cwd=str(tmp_path),
                    approval_mode="acceptEdits",
                ),
            )
            return
        sibling_entered.set()
        await never_release_sibling.wait()

    adapter.execute_task = execute_task  # type: ignore[attr-defined,method-assign]
    nonce = "3" * 32
    seed = _multi_seed()
    run_kwargs = {
        "seed": seed,
        "batch_executable": [0, 1],
        "session_id": "session-live-entered-sibling",
        "execution_id": "execution-live-entered-sibling",
        "tools": [],
        "tool_catalog": None,
        "system_prompt": "sys",
        "level_contexts": [],
        "ac_retry_attempts": {0: 0, 1: 0},
        "execution_counters": None,
    }
    try:
        first_executor = _live_executor(
            adapter=adapter,
            event_store=store,
            working_directory=str(tmp_path),
            process_local_resume_nonce=nonce,
            max_concurrent=2,
        )
        first = await first_executor._run_batch_with_bounded_route_escalation(**run_kwargs)
        assert isinstance(first[0], ACExecutionResult) and not first[0].success
        assert isinstance(first[1], ACExecutionResult)
        assert first[1].outcome is ACExecutionOutcome.BLOCKED

        handoffs = await store.query_execution_related_events(
            "execution-live-entered-sibling",
            event_type="execution.ac.uncertain_handoff_required",
            limit=3,
        )
        assert [event.data["root_ac_index"] for event in handoffs] == [1]

        resumed_executor = _live_executor(
            adapter=adapter,
            event_store=store,
            working_directory=str(tmp_path),
            process_local_resume_nonce=nonce,
            max_concurrent=2,
        )
        resumed = await resumed_executor._run_batch_with_bounded_route_escalation(**run_kwargs)

        assert isinstance(resumed[0], ACExecutionResult) and resumed[0].success
        assert isinstance(resumed[1], ACExecutionResult)
        assert resumed[1].outcome is ACExecutionOutcome.BLOCKED
        assert [name for name, _handle in calls].count("second") == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_completed_primary_turn_survives_sibling_quota_during_signal_refresh(
    tmp_path: Any,
) -> None:
    """Post-provider bookkeeping is not an uncertain external-effect boundary."""

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'completed-primary.db'}")
    await store.initialize()
    adapter = _Adapter()
    primary_post_call = asyncio.Event()
    quota_provider_completed = asyncio.Event()
    calls: list[str] = []

    class _RefreshBarrierHub:
        async def register_replaying(self, _target: Any) -> None:
            return None

        async def refresh_pending(self, target: Any) -> None:
            if target.ac_index != 0:
                return
            primary_post_call.set()
            await quota_provider_completed.wait()
            await asyncio.sleep(0.05)

        def pop_pending(self, _target: Any) -> None:
            return None

        def unregister(self, _target: Any) -> list[Any]:
            return []

    async def execute_task(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
        is_first = "## Your Task (AC 1)\nship first" in kwargs["prompt"]
        calls.append("first" if is_first else "second")
        if is_first:
            yield AgentMessage(
                type="result",
                content="first completed",
                data={"subtype": "success"},
            )
            return
        await primary_post_call.wait()
        yield AgentMessage(
            type="result",
            content="Usage limit reached. Please try again in 5 hours.",
            data={"subtype": "error", "error_type": "UsageLimitError"},
            resume_handle=RuntimeHandle(
                backend="claude",
                native_session_id="completed-primary-quota-owner",
                cwd=str(tmp_path),
                approval_mode="acceptEdits",
            ),
        )
        quota_provider_completed.set()

    adapter.execute_task = execute_task  # type: ignore[attr-defined,method-assign]
    signal_hub = _RefreshBarrierHub()
    executor = _live_executor(
        adapter=adapter,
        event_store=store,
        working_directory=str(tmp_path),
        process_local_resume_nonce="5" * 32,
        max_concurrent=2,
        session_signal_hub=signal_hub,
    )
    try:
        results = await executor._run_batch_with_bounded_route_escalation(
            seed=_multi_seed(),
            batch_executable=[0, 1],
            session_id="session-completed-primary",
            execution_id="execution-completed-primary",
            tools=[],
            tool_catalog=None,
            system_prompt="sys",
            level_contexts=[],
            ac_retry_attempts={0: 0, 1: 0},
            execution_counters=None,
        )
        assert isinstance(results[0], ACExecutionResult) and results[0].success, (results, calls)
        assert results[0].final_message == "first completed"
        assert isinstance(results[1], ACExecutionResult) and not results[1].success
        assert calls == ["first", "second"]
        handoffs = await store.query_execution_related_events(
            "execution-completed-primary",
            event_type="execution.ac.uncertain_handoff_required",
            limit=3,
        )
        assert handoffs == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_completed_signal_follow_up_survives_sibling_quota(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully observed follow-up remains completed during later local persistence."""

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'completed-follow-up.db'}")
    await store.initialize()
    adapter = _Adapter()
    follow_up_post_call = asyncio.Event()
    quota_provider_completed = asyncio.Event()
    calls: list[str] = []
    signal = SessionSignal(
        signal_id="sig_completed_follow_up",
        target_session_scope_id="scope-completed-follow-up",
        target_session_attempt_id="attempt-completed-follow-up",
        expected_execution_id="execution-completed-follow-up",
        mode=SessionSignalMode.AFTER_TURN,
        message="Apply the bounded follow-up.",
        source=SessionSignalSource.USER,
        reason="Exercise the completed follow-up boundary.",
        idempotency_key="completed-follow-up-1",
    )

    class _OneSignalHub:
        delivered = False

        async def register_replaying(self, _target: Any) -> None:
            return None

        async def refresh_pending(self, _target: Any) -> None:
            return None

        def pop_pending(self, target: Any) -> QueuedSessionSignal | None:
            if target.ac_index != 0 or self.delivered:
                return None
            self.delivered = True
            return QueuedSessionSignal(signal, SessionSignalMode.AFTER_TURN)

        def unregister(self, _target: Any) -> list[Any]:
            return []

    original_append_batch = store.append_batch

    async def append_batch(events: list[BaseEvent]) -> None:
        if any(event.type == "control.session.signal.completed" for event in events):
            follow_up_post_call.set()
            await quota_provider_completed.wait()
            await asyncio.sleep(0.05)
        await original_append_batch(events)

    monkeypatch.setattr(store, "append_batch", append_batch)

    async def execute_task(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
        prompt = str(kwargs["prompt"])
        resume_handle = kwargs.get("resume_handle")
        if "## Your Task (AC 2)\nship second" in prompt:
            calls.append("second")
            await follow_up_post_call.wait()
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "UsageLimitError"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="completed-follow-up-quota-owner",
                    cwd=str(tmp_path),
                    approval_mode="acceptEdits",
                ),
            )
            quota_provider_completed.set()
            return

        handle = RuntimeHandle(
            backend="claude",
            kind="agent_runtime",
            native_session_id="completed-follow-up-provider",
            cwd=str(tmp_path),
            metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
        )
        if "[Ouroboros Synapse: additive intent]" in prompt:
            calls.append("first-follow-up")
            content = "[TASK_COMPLETE] follow-up completed"
        else:
            calls.append("first-primary")
            content = "first primary completed"
        yield AgentMessage(
            type="result",
            content=content,
            data={"subtype": "success"},
            resume_handle=handle,
        )

    adapter.execute_task = execute_task  # type: ignore[attr-defined,method-assign]
    signal_hub = _OneSignalHub()
    executor = _live_executor(
        adapter=adapter,
        event_store=store,
        working_directory=str(tmp_path),
        process_local_resume_nonce="6" * 32,
        max_concurrent=2,
        session_signal_hub=signal_hub,
    )
    try:
        results = await executor._run_batch_with_bounded_route_escalation(
            seed=_multi_seed(),
            batch_executable=[0, 1],
            session_id="session-completed-follow-up",
            execution_id="execution-completed-follow-up",
            tools=[],
            tool_catalog=None,
            system_prompt="sys",
            level_contexts=[],
            ac_retry_attempts={0: 0, 1: 0},
            execution_counters=None,
        )
        assert isinstance(results[0], ACExecutionResult) and results[0].success, (results, calls)
        assert results[0].final_message == "[TASK_COMPLETE] follow-up completed"
        assert isinstance(results[1], ACExecutionResult) and not results[1].success
        assert {name: calls.count(name) for name in calls} == {
            "first-primary": 1,
            "first-follow-up": 1,
            "second": 1,
        }
        handoffs = await store.query_execution_related_events(
            "execution-completed-follow-up",
            event_type="execution.ac.uncertain_handoff_required",
            limit=3,
        )
        assert handoffs == []
    finally:
        await store.close()


@pytest.mark.parametrize("signal_mode", [SessionSignalMode.INFORM, SessionSignalMode.AFTER_TURN])
@pytest.mark.parametrize("pause_checkpoint", ["follow_up_dispatch", "delivery_claim"])
@pytest.mark.asyncio
async def test_signal_blocked_before_provider_entry_preserves_primary_across_pause_resume(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    signal_mode: SessionSignalMode,
    pause_checkpoint: str,
) -> None:
    """A sibling quota aborts an unentered follow-up without poisoning its primary."""

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / f'pre-entry-{signal_mode}.db'}")
    await store.initialize()
    adapter = _Adapter()
    follow_up_checkpoint_reached = asyncio.Event()
    quota_provider_completed = asyncio.Event()
    calls: list[str] = []
    signal = SessionSignal(
        signal_id=f"sig_pre_entry_{signal_mode}",
        target_session_scope_id=f"scope-pre-entry-{signal_mode}",
        target_session_attempt_id=f"attempt-pre-entry-{signal_mode}",
        expected_execution_id=f"execution-pre-entry-{signal_mode}",
        mode=signal_mode,
        message="Apply the bounded follow-up if its provider boundary is still open.",
        source=SessionSignalSource.USER,
        reason="Exercise the safe pre-entry quota boundary.",
        idempotency_key=f"pre-entry-{signal_mode}-1",
    )
    capabilities = SessionSignalCapabilities(
        inform_delivery=True,
        after_turn_delivery=True,
    )
    await store.append_batch(
        [
            create_session_signal_requested_event(signal),
            create_session_signal_accepted_event(
                signal,
                effective_mode=signal_mode,
                capabilities=capabilities,
                runtime_backend="claude",
            ),
            create_session_signal_queued_event(
                signal,
                effective_mode=signal_mode,
                runtime_backend="claude",
            ),
        ]
    )

    class _OneSignalHub:
        delivered = False

        async def register_replaying(self, _target: Any) -> None:
            return None

        async def refresh_pending(self, _target: Any) -> None:
            return None

        def pop_pending(self, target: Any) -> QueuedSessionSignal | None:
            if target.ac_index != 0 or self.delivered:
                return None
            self.delivered = True
            return QueuedSessionSignal(signal, signal_mode)

        def unregister(self, _target: Any) -> list[Any]:
            return []

    original_append = store.append

    async def append(event: BaseEvent) -> None:
        await original_append(event)
        is_follow_up_dispatch = (
            event.type == "execution.ac.attempt.dispatched"
            and event.data.get("dispatch_kind") == "session_signal_followup"
            and event.data.get("signal_id") == signal.signal_id
        )
        is_delivery_claim = (
            event.type == "control.session.signal.delivering"
            and event.aggregate_id == signal.signal_id
        )
        if (
            pause_checkpoint == "follow_up_dispatch"
            and is_follow_up_dispatch
            or pause_checkpoint == "delivery_claim"
            and is_delivery_claim
        ):
            follow_up_checkpoint_reached.set()
            await quota_provider_completed.wait()
            # Let the quota owner's observation publish the shared pause while
            # the follow-up is still outside provider_effect_scope.enter().
            await asyncio.sleep(0.05)

    monkeypatch.setattr(store, "append", append)

    async def execute_task(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
        prompt = str(kwargs["prompt"])
        resume_handle = kwargs.get("resume_handle")
        if "## Your Task (AC 2)\nship second" in prompt:
            calls.append("second")
            if resume_handle is not None and resume_handle.resume_session_id is not None:
                yield AgentMessage(
                    type="result",
                    content="quota owner resumed",
                    data={"subtype": "success"},
                )
                return
            await follow_up_checkpoint_reached.wait()
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "UsageLimitError"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id=f"pre-entry-quota-{signal_mode}",
                    cwd=str(tmp_path),
                    approval_mode="acceptEdits",
                ),
            )
            quota_provider_completed.set()
            return

        handle = RuntimeHandle(
            backend="claude",
            kind="agent_runtime",
            native_session_id=f"pre-entry-primary-{signal_mode}",
            cwd=str(tmp_path),
            metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
        )
        if "[Ouroboros Synapse:" in prompt:
            calls.append("first-follow-up")
            content = "[TASK_COMPLETE] follow-up completed"
        else:
            calls.append("first-primary")
            content = "first primary completed"
        yield AgentMessage(
            type="result",
            content=content,
            data={"subtype": "success"},
            resume_handle=handle,
        )

    adapter.execute_task = execute_task  # type: ignore[attr-defined,method-assign]
    signal_hub = _OneSignalHub()
    execution_id = f"execution-pre-entry-{signal_mode}"
    session_id = f"session-pre-entry-{signal_mode}"
    run_kwargs = {
        "seed": _multi_seed(),
        "batch_executable": [0, 1],
        "session_id": session_id,
        "execution_id": execution_id,
        "tools": [],
        "tool_catalog": None,
        "system_prompt": "sys",
        "level_contexts": [],
        "ac_retry_attempts": {0: 0, 1: 0},
        "execution_counters": None,
    }
    nonce = "7" * 32
    try:
        first_executor = _live_executor(
            adapter=adapter,
            event_store=store,
            working_directory=str(tmp_path),
            process_local_resume_nonce=nonce,
            max_concurrent=2,
            session_signal_hub=signal_hub,
        )
        first = await first_executor._run_batch_with_bounded_route_escalation(**run_kwargs)

        assert isinstance(first[0], ACExecutionResult) and first[0].success, (first, calls)
        assert first[0].final_message == "first primary completed"
        assert isinstance(first[1], ACExecutionResult) and not first[1].success
        assert calls.count("first-primary") == 1
        assert calls.count("first-follow-up") == 0

        signal_events = await store.replay("session_signal", signal.signal_id)
        projection = project_session_signal(signal_events)
        assert projection.state is SessionSignalState.REJECTED
        assert signal_events[-1].data["rejection_code"] == "batch_paused_before_delivery"
        if pause_checkpoint == "delivery_claim":
            assert any(event.type == "control.session.signal.delivering" for event in signal_events)
        assert not any(
            event.type == "control.session.signal.delivery_uncertain" for event in signal_events
        )

        dispatches = await store.query_execution_related_events(
            execution_id,
            event_type="execution.ac.attempt.dispatched",
            limit=10,
        )
        primary = next(
            event
            for event in dispatches
            if event.data.get("ac_index") == 0 and event.data.get("dispatch_kind") == "primary"
        )
        follow_up = next(
            event for event in dispatches if event.data.get("signal_id") == signal.signal_id
        )
        scope_events = await store.replay(primary.aggregate_type, primary.aggregate_id)
        runtime_identity = first_executor._ac_runtime_handle_manager._resolve_ac_runtime_identity(
            0,
            execution_context_id=execution_id,
            node_identity=ExecutionNodeIdentity.root(
                execution_context_id=execution_id,
                ac_index=0,
            ),
            retry_attempt=0,
        )
        _compiled, active_dispatches, seals = (
            ACRuntimeHandleManager._validate_capsule_dispatch_chain(
                scope_events,
                runtime_identity=runtime_identity,
                expected_capsule_fingerprint=primary.data["capsule_fingerprint"],
            )
        )
        assert [scope_events[index].id for index in active_dispatches] == [primary.id]
        assert seals == []
        follow_up_seal = next(
            event
            for event in scope_events
            if event.type == "execution.ac.dispatch.sealed"
            and event.data.get("ac_dispatch_id") == follow_up.data["ac_dispatch_id"]
        )
        assert follow_up_seal.data["reason"] == "provider admission cancelled before provider entry"

        resumed_executor = _live_executor(
            adapter=adapter,
            event_store=store,
            working_directory=str(tmp_path),
            process_local_resume_nonce=nonce,
            max_concurrent=2,
            session_signal_hub=signal_hub,
        )
        resumed = await resumed_executor._run_batch_with_bounded_route_escalation(**run_kwargs)

        assert all(
            isinstance(result, ACExecutionResult) and result.success for result in resumed
        ), (resumed, calls)
        assert resumed[0].final_message == "first primary completed"
        assert calls.count("first-primary") == 1
        assert calls.count("first-follow-up") == 0
        assert calls.count("second") == 2
        handoffs = await store.query_execution_related_events(
            execution_id,
            event_type="execution.ac.uncertain_handoff_required",
            limit=3,
        )
        assert handoffs == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_live_pre_entry_sibling_remains_pending_across_route_pause(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sibling held behind the semaphore has no provider effect to hand off."""

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'pre-entry-sibling-pause.db'}")
    await store.initialize()
    adapter = _Adapter()
    calls: list[str] = []
    first_provider_entered = asyncio.Event()
    sibling_waiting_for_admission = asyncio.Event()
    sibling_execution = ContextVar("sibling_execution", default=False)
    original_provider_admission_wait = provider_admission.wait

    async def observe_sibling_admission_wait() -> None:
        if sibling_execution.get():
            sibling_waiting_for_admission.set()
        await original_provider_admission_wait()

    monkeypatch.setattr(provider_admission, "wait", observe_sibling_admission_wait)

    class _SynchronizedExecutor(ParallelACExecutor):
        async def _execute_single_ac(self, *args: Any, **kwargs: Any) -> ACExecutionResult:
            is_sibling = kwargs["ac_index"] == 1
            if is_sibling:
                await first_provider_entered.wait()
            sibling_token = sibling_execution.set(is_sibling)
            try:
                return await super()._execute_single_ac(*args, **kwargs)
            finally:
                sibling_execution.reset(sibling_token)

    async def execute_task(*_args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
        ac_name = "first" if "## Your Task (AC 1)\nship first" in kwargs["prompt"] else "second"
        if ac_name == "first":
            first_provider_entered.set()
        runtime_handle = kwargs.get("resume_handle")
        calls.append(ac_name)
        if ac_name == "first" and (
            runtime_handle is None or runtime_handle.resume_session_id is None
        ):
            await sibling_waiting_for_admission.wait()
        if ac_name == "first" and (
            runtime_handle is None or runtime_handle.resume_session_id is None
        ):
            yield AgentMessage(
                type="result",
                content="Usage limit reached. Please try again in 5 hours.",
                data={"subtype": "error", "error_type": "CodexCliError"},
                resume_handle=RuntimeHandle(
                    backend="claude",
                    native_session_id="sequential-quota-owner",
                    cwd=str(tmp_path),
                    approval_mode="acceptEdits",
                ),
            )
            return
        yield AgentMessage(
            type="result",
            content=f"{ac_name} completed",
            data={"subtype": "success"},
        )

    adapter.execute_task = execute_task  # type: ignore[attr-defined,method-assign]
    nonce = "4" * 32
    seed = _multi_seed()
    run_kwargs = {
        "seed": seed,
        "batch_executable": [0, 1],
        "session_id": "session-live-pre-entry-sibling",
        "execution_id": "execution-live-pre-entry-sibling",
        "tools": [],
        "tool_catalog": None,
        "system_prompt": "sys",
        "level_contexts": [],
        "ac_retry_attempts": {0: 0, 1: 0},
        "execution_counters": None,
    }
    try:
        first_executor = _live_executor(
            adapter=adapter,
            event_store=store,
            working_directory=str(tmp_path),
            process_local_resume_nonce=nonce,
            max_concurrent=1,
            executor_type=_SynchronizedExecutor,
        )
        await first_executor._run_batch_with_bounded_route_escalation(**run_kwargs)

        handoffs = await store.query_execution_related_events(
            "execution-live-pre-entry-sibling",
            event_type="execution.ac.uncertain_handoff_required",
            limit=3,
        )
        assert handoffs == []
        assert first_provider_entered.is_set()
        assert calls == ["first"]

        first_provider_entered = asyncio.Event()
        sibling_waiting_for_admission = asyncio.Event()
        resumed_executor = _live_executor(
            adapter=adapter,
            event_store=store,
            working_directory=str(tmp_path),
            process_local_resume_nonce=nonce,
            max_concurrent=1,
            executor_type=_SynchronizedExecutor,
        )
        resumed = await resumed_executor._run_batch_with_bounded_route_escalation(**run_kwargs)

        assert all(
            isinstance(result, ACExecutionResult) and result.success for result in resumed
        ), (resumed, calls)
        assert calls == ["first", "first", "second"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_parallel_pause_resume_preserves_completed_composite_without_effect_replay(
    tmp_path: Any,
) -> None:
    executor, store, events = _executor()
    executor._adapter.working_directory = str(tmp_path)  # type: ignore[attr-defined]
    seed = _multi_seed()
    cheap = _candidate(executor, "compat:claude:frugal")
    decomposition_decision = _split_decision()
    touched = tmp_path / "composite.py"
    touched.write_text("def completed_effect():\n    return True\n")
    child_message = AgentMessage(
        type="tool",
        content="wrote composite",
        tool_name="Write",
        data={"tool_input": {"file_path": str(touched)}},
    )

    async def first_round(**_kwargs: Any) -> list[ACExecutionResult]:
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="ship first",
                success=True,
                final_message="composite complete",
                is_decomposed=True,
                sub_results=(
                    ACExecutionResult(
                        ac_index=100,
                        ac_content="first child",
                        success=True,
                        messages=(child_message,),
                        final_message="first child complete",
                        depth=1,
                    ),
                    ACExecutionResult(
                        ac_index=101,
                        ac_content="second child",
                        success=True,
                        final_message="second child complete",
                        depth=1,
                    ),
                ),
                outcome=ACExecutionOutcome.SUCCEEDED,
                decomposition_decision=decomposition_decision,
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
                runtime_handle=_paused_route_handle(),
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
    completion = next(event for event in events if event.type == "execution.ac.composite_completed")
    assert completion.data["root_ac_index"] == 0

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        return [event for event in events if event.type == kwargs.get("event_type")]

    store.query_execution_related_events.side_effect = query
    resumed_indices: list[list[int]] = []

    async def resumed_round(**kwargs: Any) -> list[ACExecutionResult]:
        indices = kwargs["batch_indices"]
        resumed_indices.append(indices)
        assert indices == [1]
        return [
            ACExecutionResult(
                ac_index=1,
                ac_content="ship second",
                success=True,
                final_message="atomic resumed",
                outcome=ACExecutionOutcome.SUCCEEDED,
                route_candidate=cheap,
            )
        ]

    executor._execute_ac_batch = resumed_round  # type: ignore[method-assign]
    executor._try_decompose_ac = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("completed composite decomposition replayed")
    )
    executor._publish_event_owned_decomposition_decision(decomposition_decision)
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
    restored = resumed[0]
    assert isinstance(restored, ACExecutionResult)
    assert restored.is_decomposed and restored.success
    assert restored.final_message == "composite complete"
    assert tuple(child.ac_content for child in restored.sub_results) == (
        "first child",
        "second child",
    )
    assert tuple(child.final_message for child in restored.sub_results) == (
        "first child complete",
        "second child complete",
    )
    assert restored.conflict_files == ()
    assert restored.sub_results[0].conflict_files == (str(touched),)
    executor._try_decompose_ac.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_provisional_success_replay_preserves_canonical_513_file_projection(
    tmp_path: Any,
) -> None:
    executor, store, events = _executor()
    executor._adapter.working_directory = str(tmp_path)  # type: ignore[attr-defined]
    seed = _seed()
    candidate = _candidate(executor, "compat:claude:frugal")
    ordered_paths = [tmp_path / f"z{index:03d}.py" for index in range(511)]
    ordered_paths.append(tmp_path / "a.py")
    for path in ordered_paths:
        path.write_text(
            "def canonical_first():\n    return True\n" if path.name == "a.py" else "value = 1\n"
        )
    file_messages = tuple(
        AgentMessage(
            type="tool",
            content=f"wrote {path.name}",
            tool_name="Write",
            data={"tool_input": {"file_path": str(path)}},
        )
        for path in ordered_paths
    )
    long_path = "/virtual/" + "p" * 2_049
    tool_messages = tuple(
        AgentMessage(
            type="tool",
            content=f"used tool {index}",
            tool_name=f"Tool-{index:03d}-" + "t" * 129,
        )
        for index in range(127)
    )
    messages = (
        *file_messages,
        AgentMessage(
            type="tool",
            content="edited a long virtual path",
            tool_name="Edit",
            data={"tool_input": {"file_path": long_path}},
        ),
        *tool_messages,
    )
    original = ACExecutionResult(
        ac_index=0,
        ac_content="ship it",
        success=True,
        messages=messages,
        final_message="all twenty-one files completed",
        outcome=ACExecutionOutcome.SUCCEEDED,
        route_candidate=candidate,
    )
    live_summary = extract_level_context(
        [(0, "ship it", True, messages, original.final_message)],
        0,
        workspace_root=str(tmp_path),
    ).completed_acs[0]
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
    (
        _histories,
        _overrides,
        _terminals,
        restored_results,
    ) = await executor._load_bounded_route_resume_state(
        seed=seed,
        execution_id="execution-1",
        session_id="session-1",
        root_ac_indices=(0,),
    )
    restored = restored_results[0]
    assert restored.context_summary == live_summary
    assert len(restored.context_summary.files_modified) == 513
    assert len(restored.context_summary.tools_used) == 129
    assert long_path in restored.context_summary.files_modified
    assert any(len(tool) > 128 for tool in restored.context_summary.tools_used)
    assert restored.context_summary.files_modified[0] == str(tmp_path / "a.py")
    assert "canonical_first" in restored.context_summary.public_api
    assert restored.conflict_files == tuple(
        sorted((long_path, *(str(path) for path in ordered_paths)))
    )
    assert (
        LevelContext(
            level_number=0,
            completed_acs=(restored.context_summary,),
        ).to_prompt_text()
        == LevelContext(
            level_number=0,
            completed_acs=(live_summary,),
        ).to_prompt_text()
    )
    other_writer = ACExecutionResult(
        ac_index=1,
        ac_content="also writes a",
        success=True,
        messages=(
            AgentMessage(
                type="tool",
                content="edited a",
                tool_name="Edit",
                data={"tool_input": {"file_path": str(tmp_path / "a.py")}},
            ),
        ),
    )
    conflicts = executor._coordinator.detect_file_conflicts([restored, other_writer])
    assert [(item.file_path, item.ac_indices) for item in conflicts] == [
        (str(tmp_path / "a.py"), (0, 1))
    ]


@pytest.mark.asyncio
async def test_provisional_success_replay_preserves_admitted_long_ac_identity() -> None:
    """A 65,537-character criterion cannot strand its durable judgment."""

    executor, store, events = _executor()
    description = "x" * 65_537
    seed = _seed().model_copy(
        update={"acceptance_criteria": (AcceptanceCriterionSpec(description=description),)}
    )
    candidate = _candidate(executor, "compat:claude:frugal")
    provider_session_id = "provider-" + "s" * 2_048
    result = ACExecutionResult(
        ac_index=0,
        ac_content=description,
        success=True,
        final_message="long criterion completed",
        session_id=provider_session_id,
        outcome=ACExecutionOutcome.SUCCEEDED,
        route_candidate=candidate,
    )
    await executor._emit_ac_attempt_judged(
        result=result,
        root_ac_index=0,
        session_id="session-1",
        execution_id="execution-1",
        required=True,
        route_episode_id=_episode_id(seed),
        route_attempt_index=0,
    )
    await executor._persist_route_observation(
        seed=seed,
        result=result,
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
    _histories, _overrides, _terminals, restored = await executor._load_bounded_route_resume_state(
        seed=seed,
        execution_id="execution-1",
        session_id="session-1",
        root_ac_indices=(0,),
    )

    assert restored[0].ac_content == description
    assert restored[0].session_id == provider_session_id
    assert restored[0].context_summary is not None
    assert restored[0].context_summary.ac_content == description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_envelope",
        "unknown_decision",
        "fingerprint_drift",
        "unknown_result",
        "unknown_child_result",
        "duplicate",
    ],
)
async def test_composite_completion_replay_rejects_non_strict_or_conflicting_state(
    mutation: str,
) -> None:
    executor, store, events = _executor()
    seed = _seed()
    completed = ACExecutionResult(
        ac_index=0,
        ac_content="ship it",
        success=True,
        final_message="composite complete",
        is_decomposed=True,
        sub_results=(
            ACExecutionResult(ac_index=100, ac_content="first child", success=True, depth=1),
            ACExecutionResult(ac_index=101, ac_content="second child", success=True, depth=1),
        ),
        outcome=ACExecutionOutcome.SUCCEEDED,
        decomposition_decision=_split_decision(),
    )
    await executor._persist_composite_completion(
        seed=seed,
        result=completed,
        root_ac_index=0,
        session_id="session-1",
        execution_id="execution-1",
    )
    completion = next(event for event in events if event.type == "execution.ac.composite_completed")
    replay_events = [completion]
    if mutation == "unknown_envelope":
        completion.data["acceptance_authority"] = "gate"
    elif mutation == "unknown_decision":
        completion.data["decomposition_decision"]["unbounded_children"] = []
    elif mutation == "fingerprint_drift":
        completion.data["decomposition_fingerprint"] = "0" * 64
    elif mutation == "unknown_result":
        completion.data["result"]["provider_transcript"] = []
    elif mutation == "unknown_child_result":
        completion.data["result"]["sub_results"][0]["accepted"] = True
    else:
        replay_events = [completion, completion]

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        if kwargs.get("event_type") == "execution.ac.composite_completed":
            return replay_events
        return []

    store.query_execution_related_events.side_effect = query
    with pytest.raises(RuntimeError, match="composite completion"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_envelope",
        "fingerprint_drift",
        "child_node_drift",
        "unknown_completed_child",
        "capsule_malformed",
        "conflicting_sequence",
    ],
)
async def test_partial_composite_replay_rejects_non_strict_or_conflicting_state(
    mutation: str,
) -> None:
    executor, store, events = _executor(enable_decomposition=True)
    seed = _seed()
    decision = _split_decision()
    root = ExecutionNodeIdentity.root(execution_context_id="execution-1", ac_index=0)
    paused_message = AgentMessage(
        type="result",
        content="Usage limit reached. Please try again in 5 hours.",
        data={"subtype": "error", "error_type": "CodexCliError"},
    )
    paused_handle = RuntimeHandle(
        backend="claude",
        native_session_id="paused-child-session",
        cwd="/tmp/project",
        metadata={
            "node_id": root.child(1).node_id,
            "session_scope_id": "execution-1-paused-child",
            "ac_dispatch_id": "a" * 32,
            "ac_capsule_fingerprint": "sha256:" + "b" * 64,
        },
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ship it",
        success=False,
        is_decomposed=True,
        sub_results=(
            ACExecutionResult(
                ac_index=0,
                ac_content="first child",
                success=True,
                final_message="completed once",
                depth=1,
            ),
            ACExecutionResult(
                ac_index=1,
                ac_content="second child",
                success=False,
                messages=(paused_message,),
                final_message=paused_message.content,
                runtime_handle=paused_handle,
                depth=1,
            ),
        ),
        decomposition_decision=decision,
    )
    await executor._persist_partial_composite_pause(
        seed=seed,
        result=result,
        root_ac_index=0,
        session_id="session-1",
        execution_id="execution-1",
    )
    partial = next(event for event in events if event.type == "execution.ac.composite_paused")
    replay_events = [partial]
    if mutation == "unknown_envelope":
        partial.data["acceptance_authority"] = "final_gate"
    elif mutation == "fingerprint_drift":
        partial.data["frames"][0]["decomposition_fingerprint"] = "0" * 64
    elif mutation == "child_node_drift":
        partial.data["frames"][0]["paused_child_node_id"] = root.child(0).node_id
    elif mutation == "unknown_completed_child":
        partial.data["frames"][0]["completed_children"][0]["provider_transcript"] = []
    elif mutation == "capsule_malformed":
        partial.data["paused_leaf"]["capsule_fingerprint"] = "b" * 64
    else:
        conflicting_data = deepcopy(partial.data)
        conflicting_data["frames"][0]["paused_child_index"] = 0
        conflicting_data["frames"][0]["completed_children"] = []
        conflicting_data["frames"][0]["paused_child_node_id"] = root.child(0).node_id
        conflicting_data["frames"][0]["paused_child_ac_index"] = 0
        conflicting_data["frames"][0]["paused_child_content"] = "first child"
        conflicting_data["paused_leaf"]["node_id"] = root.child(0).node_id
        conflicting_data["paused_leaf"]["ac_index"] = 0
        conflicting_data["paused_leaf"]["ac_content"] = "first child"
        conflicting = BaseEvent(
            type=partial.type,
            aggregate_type=partial.aggregate_type,
            aggregate_id=partial.aggregate_id,
            data=conflicting_data,
        )
        replay_events = [partial, conflicting]

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        if kwargs.get("event_type") == "execution.ac.composite_paused":
            return replay_events
        return []

    store.query_execution_related_events.side_effect = query
    with pytest.raises(RuntimeError, match="partial composite|composite completion result tree"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("projection", ["completion", "partial"])
async def test_composite_projection_without_finalized_event_cannot_mint_authority(
    projection: str,
) -> None:
    executor, store, events = _executor(enable_decomposition=True)
    seed = _seed()
    decision = _split_decision()
    if projection == "completion":
        result = ACExecutionResult(
            ac_index=0,
            ac_content="ship it",
            success=True,
            final_message="composite complete",
            is_decomposed=True,
            sub_results=(
                ACExecutionResult(ac_index=100, ac_content="first child", success=True, depth=1),
                ACExecutionResult(ac_index=101, ac_content="second child", success=True, depth=1),
            ),
            decomposition_decision=decision,
        )
        await executor._persist_composite_completion(
            seed=seed,
            result=result,
            root_ac_index=0,
            session_id="session-1",
            execution_id="execution-1",
        )
    else:
        root = ExecutionNodeIdentity.root(execution_context_id="execution-1", ac_index=0)
        paused = AgentMessage(
            type="result",
            content="Usage limit reached. Please try again in 5 hours.",
            data={"subtype": "error", "error_type": "CodexCliError"},
        )
        result = ACExecutionResult(
            ac_index=0,
            ac_content="ship it",
            success=False,
            is_decomposed=True,
            sub_results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content="first child",
                    success=False,
                    messages=(paused,),
                    final_message=paused.content,
                    runtime_handle=RuntimeHandle(
                        backend="claude",
                        native_session_id="cache-only-child",
                        cwd="/tmp/project",
                        metadata={
                            "node_id": root.child(0).node_id,
                            "session_scope_id": "execution-1-cache-only-child",
                            "ac_dispatch_id": "e" * 32,
                            "ac_capsule_fingerprint": "sha256:" + "e" * 64,
                        },
                    ),
                    depth=1,
                ),
            ),
            decomposition_decision=decision,
        )
        await executor._persist_partial_composite_pause(
            seed=seed,
            result=result,
            root_ac_index=0,
            session_id="session-1",
            execution_id="execution-1",
        )

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        return [event for event in events if event.type == kwargs.get("event_type")]

    store.query_execution_related_events.side_effect = query
    with pytest.raises(RuntimeError, match=f"{projection}.*finalized-event authority"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )

    assert executor._decomposition_decisions == {}
    assert executor._partial_composite_resumes == {}


@pytest.mark.asyncio
async def test_partial_composite_replay_folds_newest_first_advancing_history() -> None:
    executor, store, events = _executor(enable_decomposition=True)
    seed = _seed()
    decision = _split_decision()
    executor._publish_event_owned_decomposition_decision(decision)
    root = ExecutionNodeIdentity.root(execution_context_id="execution-1", ac_index=0)
    paused_message = AgentMessage(
        type="result",
        content="Usage limit reached. Please try again in 5 hours.",
        data={"subtype": "error", "error_type": "CodexCliError"},
    )

    def paused_child(index: int, marker: str) -> ACExecutionResult:
        return ACExecutionResult(
            ac_index=index,
            ac_content=("first child" if index == 0 else "second child"),
            success=False,
            messages=(paused_message,),
            final_message=paused_message.content,
            runtime_handle=RuntimeHandle(
                backend="claude",
                native_session_id=f"paused-child-{marker}",
                cwd="/tmp/project",
                metadata={
                    "node_id": root.child(index).node_id,
                    "session_scope_id": f"execution-1-paused-{marker}",
                    "ac_dispatch_id": marker * 32,
                    "ac_capsule_fingerprint": "sha256:" + marker * 64,
                },
            ),
            depth=1,
        )

    async def persist(sub_results: tuple[ACExecutionResult, ...]) -> None:
        await executor._persist_partial_composite_pause(
            seed=seed,
            result=ACExecutionResult(
                ac_index=0,
                ac_content="ship it",
                success=False,
                is_decomposed=True,
                sub_results=sub_results,
                decomposition_decision=decision,
            ),
            root_ac_index=0,
            session_id="session-1",
            execution_id="execution-1",
        )

    await persist((paused_child(0, "a"),))
    completed = ACExecutionResult(
        ac_index=0,
        ac_content="first child",
        success=True,
        final_message="completed once",
        depth=1,
    )
    await persist((completed, paused_child(1, "b")))
    await persist((completed, paused_child(1, "c")))
    partial_events = [event for event in events if event.type == "execution.ac.composite_paused"]

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        if kwargs.get("event_type") == "execution.ac.composite_paused":
            return list(reversed(partial_events))
        return []

    store.query_execution_related_events.side_effect = query
    await executor._load_bounded_route_resume_state(
        seed=seed,
        execution_id="execution-1",
        session_id="session-1",
        root_ac_indices=(0,),
    )

    state = executor._partial_composite_resumes[root.node_id]
    assert state.paused_child_index == 1
    assert len(state.completed_children) == 1
    assert state.paused_dispatch_id == "c" * 32
    assert state.paused_capsule_fingerprint == "sha256:" + "c" * 64


@pytest.mark.asyncio
async def test_valid_4097_composite_pauses_replay_without_a_total_cap(tmp_path: Any) -> None:
    producer, _mock_store, events = _executor(enable_decomposition=True)
    seed = _seed()
    decision = _split_decision()
    root = ExecutionNodeIdentity.root(execution_context_id="execution-1", ac_index=0)
    paused_message = AgentMessage(
        type="result",
        content="Usage limit reached. Please try again in 5 hours.",
        data={"subtype": "error", "error_type": "CodexCliError"},
    )
    paused_child = ACExecutionResult(
        ac_index=0,
        ac_content="first child",
        success=False,
        messages=(paused_message,),
        final_message=paused_message.content,
        runtime_handle=RuntimeHandle(
            backend="claude",
            native_session_id="paused-child-population",
            cwd="/tmp/project",
            metadata={
                "node_id": root.child(0).node_id,
                "session_scope_id": "execution-1-paused-population",
                "ac_dispatch_id": "d" * 32,
                "ac_capsule_fingerprint": "sha256:" + "d" * 64,
            },
        ),
        depth=1,
    )
    await producer._persist_partial_composite_pause(
        seed=seed,
        result=ACExecutionResult(
            ac_index=0,
            ac_content="ship it",
            success=False,
            is_decomposed=True,
            sub_results=(paused_child,),
            decomposition_decision=decision,
        ),
        root_ac_index=0,
        session_id="session-1",
        execution_id="execution-1",
    )
    template = next(event for event in events if event.type == "execution.ac.composite_paused")
    pause_events = [
        BaseEvent(
            type=template.type,
            aggregate_type=template.aggregate_type,
            aggregate_id=template.aggregate_id,
            data=deepcopy(template.data),
        )
        for _pause_number in range(4_097)
    ]

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'composite-pause-population.db'}")
    await store.initialize()
    await store.append_batch(pause_events)
    replay = _live_executor(
        adapter=_Adapter(),
        event_store=store,
        working_directory=str(tmp_path),
        process_local_resume_nonce="d" * 32,
    )
    replay._publish_event_owned_decomposition_decision(decision)
    try:
        await replay._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )

        state = replay._partial_composite_resumes[root.node_id]
        assert state.paused_child_index == 0
        assert state.paused_dispatch_id == "d" * 32
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_partial_composite_replay_rejects_newer_descendant_path_shortening() -> None:
    executor, store, events = _executor(enable_decomposition=True)
    seed = _seed()
    root = ExecutionNodeIdentity.root(execution_context_id="execution-1", ac_index=0)
    root_decision = _split_decision(
        node_identity=root,
        child_descriptions=("outer completed", "nested composite"),
    )
    nested_decision = _split_decision(
        node_identity=root.child(1),
        child_descriptions=("nested completed", "nested paused"),
    )
    paused_message = AgentMessage(
        type="result",
        content="Usage limit reached. Please try again in 5 hours.",
        data={"subtype": "error", "error_type": "CodexCliError"},
    )
    paused_leaf = ACExecutionResult(
        ac_index=101,
        ac_content="nested paused",
        success=False,
        messages=(paused_message,),
        final_message=paused_message.content,
        runtime_handle=RuntimeHandle(
            backend="claude",
            native_session_id="nested-paused",
            cwd="/tmp/project",
            metadata={
                "node_id": root.child(1).child(1).node_id,
                "session_scope_id": "execution-1-nested-paused",
                "ac_dispatch_id": "a" * 32,
                "ac_capsule_fingerprint": "sha256:" + "b" * 64,
            },
        ),
        depth=2,
    )
    nested = ACExecutionResult(
        ac_index=1,
        ac_content="nested composite",
        success=False,
        is_decomposed=True,
        sub_results=(
            ACExecutionResult(
                ac_index=100,
                ac_content="nested completed",
                success=True,
                final_message="nested completed once",
                depth=2,
            ),
            paused_leaf,
        ),
        decomposition_decision=nested_decision,
        depth=1,
    )
    await executor._persist_partial_composite_pause(
        seed=seed,
        result=ACExecutionResult(
            ac_index=0,
            ac_content="ship it",
            success=False,
            is_decomposed=True,
            sub_results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content="outer completed",
                    success=True,
                    final_message="outer completed once",
                    depth=1,
                ),
                nested,
            ),
            decomposition_decision=root_decision,
        ),
        root_ac_index=0,
        session_id="session-1",
        execution_id="execution-1",
    )
    original = next(event for event in events if event.type == "execution.ac.composite_paused")
    shortened_data = deepcopy(original.data)
    shortened_data["frames"] = shortened_data["frames"][:1]
    shortened_data["paused_leaf"].update(
        {
            "node_id": root.child(1).node_id,
            "ac_index": 1,
            "ac_content": "nested composite",
        }
    )
    shortened = BaseEvent(
        type=original.type,
        aggregate_type=original.aggregate_type,
        aggregate_id=original.aggregate_id,
        data=shortened_data,
    )

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        if kwargs.get("event_type") == "execution.ac.composite_paused":
            return [shortened, original]
        return []

    store.query_execution_related_events.side_effect = query
    with pytest.raises(RuntimeError, match="dropped an established descendant frame"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


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
            runtime_handle=_paused_route_handle(),
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
async def test_parallel_pause_rejects_effort_drift_from_durable_successor() -> None:
    executor, store, events = _executor()
    seed = _seed()
    observation, decision = _durable_first_failure(executor, seed)
    assert decision.selected is not None
    route_event = _route_event(seed, observation=observation, decision=decision)
    await executor._persist_parallel_route_pause(
        seed=seed,
        result=ACExecutionResult(
            ac_index=0,
            ac_content="ship it",
            success=False,
            retry_attempt=1,
            route_candidate=decision.selected,
            runtime_handle=_paused_route_handle(),
        ),
        root_ac_index=0,
        session_id="session-1",
        execution_id="execution-1",
        prior_route_ids=(observation.route_id,),
        retry_prompt_extra="durable retry context",
        route_id_override=decision.selected.route_id,
        expected_route_candidate=decision.selected,
    )
    pause_event = next(event for event in events if event.type == "execution.ac.route_paused")
    pause_event.data["route"] = replace(decision.selected, effort="high").to_contract_data()
    judgment = _judgment_for_route_event(route_event)

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        event_type = kwargs.get("event_type")
        if event_type == "execution.ac.route_observed":
            return [route_event]
        if event_type == "execution.ac.attempt_judged":
            return [judgment]
        if event_type == "execution.ac.route_paused":
            return [pause_event]
        return []

    store.query_execution_related_events.side_effect = query
    with pytest.raises(RuntimeError, match="durable successor capsule inputs"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
async def test_durable_route_override_creates_no_preflight_decomposition_state() -> None:
    executor, _store, _events = _executor(enable_decomposition=True)
    candidate = _candidate(executor, "compat:claude:frugal")

    # The minimal adapter intentionally has no provider stream. Reaching that
    # boundary proves preflight decomposition was bypassed without replacing a
    # protected execution method and invalidating the authority binding.
    result = await executor._execute_single_ac(
        ac_index=0,
        ac_content="ship it",
        session_id="session-1",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        seed_goal="bounded routing",
        execution_id="execution-1",
        route_id_override=candidate.route_id,
        expected_route_candidate=candidate,
    )

    assert result.success is False
    assert "execute_task" in (result.error or "")
    assert executor._decomposition_decisions == {}


@pytest.mark.asyncio
async def test_provisional_success_replay_rejects_starting_tier_floor_drift() -> None:
    source, _source_store, _source_events = _executor(base_tier="frugal")
    seed = _seed()
    candidate = _candidate(source, "compat:claude:frugal")
    observation = RouteObservation.from_candidate(
        candidate,
        RouteRequirements(),
        episode_id=_episode_id(seed),
        attempt_index=0,
        verifier_outcome=VerifierOutcome.ATTEMPT_SUCCEEDED,
    )
    route_event = _route_event(seed, observation=observation, decision=None)

    resumed, store, _events = _executor(base_tier="standard")
    _set_route_replay_events(store, [route_event])

    with pytest.raises(RuntimeError, match="starting-tier floor drift"):
        await resumed._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("event_kind", ["observation", "pause"])
async def test_parallel_route_replay_rejects_unknown_envelope_fields(
    event_kind: str,
) -> None:
    executor, store, events = _executor()
    seed = _seed()
    candidate = _candidate(executor, "compat:claude:frugal")
    if event_kind == "observation":
        observation, decision = _durable_first_failure(executor, seed)
        event = _route_event(seed, observation=observation, decision=decision)
        event.data["unknown_terminal_semantics"] = "accepted"
        _set_route_replay_events(store, [event])
    else:
        await executor._persist_parallel_route_pause(
            seed=seed,
            result=ACExecutionResult(
                ac_index=0,
                ac_content="ship it",
                success=False,
                route_candidate=candidate,
                runtime_handle=_paused_route_handle(),
            ),
            root_ac_index=0,
            session_id="session-1",
            execution_id="execution-1",
            prior_route_ids=(),
        )
        event = next(item for item in events if item.type == "execution.ac.route_paused")
        event.data["unknown_terminal_semantics"] = "accepted"

        async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
            return [event] if kwargs.get("event_type") == "execution.ac.route_paused" else []

        store.query_execution_related_events.side_effect = query

    with pytest.raises(RuntimeError, match="invalid event envelope"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
async def test_valid_65_parallel_pause_history_replays_without_a_total_cap(tmp_path: Any) -> None:
    producer, _mock_store, events = _executor()
    candidate = _candidate(producer, "compat:claude:frugal")
    for _pause_number in range(65):
        await producer._persist_parallel_route_pause(
            seed=_seed(),
            result=ACExecutionResult(
                ac_index=0,
                ac_content="ship it",
                success=False,
                route_candidate=candidate,
                runtime_handle=_paused_route_handle(),
            ),
            root_ac_index=0,
            session_id="session-1",
            execution_id="execution-1",
            prior_route_ids=(),
        )

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'parallel-pause-population.db'}")
    await store.initialize()
    await store.append_batch(events)
    replay = _live_executor(
        adapter=_Adapter(),
        event_store=store,
        working_directory=str(tmp_path),
        process_local_resume_nonce="b" * 32,
    )
    try:
        await replay._load_bounded_route_resume_state(
            seed=_seed(),
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )

        state = replay._parallel_route_resumes[0]
        assert state.candidate == candidate
        assert state.dispatch_id == "a" * 32
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pre_dispatch_route_state_detector_uses_overflow_sentinels() -> None:
    executor, store, _events = _executor()
    observed_limits: list[int | None] = []
    event = BaseEvent(
        type="execution.ac.route_observed",
        aggregate_type="execution",
        aggregate_id="execution-1",
        data={},
    )

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        limit = kwargs.get("limit")
        observed_limits.append(limit)
        assert isinstance(limit, int)
        return [event] * limit

    store.query_execution_related_events.side_effect = query
    with pytest.raises(RuntimeError, match="pre-dispatch scan exceeds"):
        await executor._has_persisted_bounded_route_state(
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
            root_ac_count=1,
        )
    assert observed_limits and all(limit is not None for limit in observed_limits)


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
@pytest.mark.parametrize("mutation", ["unknown_field", "oversized_episode", "oversized_route"])
async def test_route_judgment_replay_rejects_non_exact_or_oversized_envelopes(
    mutation: str,
) -> None:
    executor, store, _events = _executor()
    seed = _seed()
    observation, decision = _durable_first_failure(executor, seed)
    route_event = _route_event(seed, observation=observation, decision=decision)
    judgment = _route_judgment(seed)
    if mutation == "unknown_field":
        judgment.data["acceptance_authority"] = "final_gate"
    elif mutation == "oversized_episode":
        judgment.data["route_episode_id"] = "r" * 161
    else:
        judgment.data["route_id"] = "r" * 161
    _set_route_replay_events(store, [route_event], judgments=[judgment])

    with pytest.raises(RuntimeError, match="event envelope|correlation metadata"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


@pytest.mark.asyncio
async def test_route_judgment_replay_uses_finite_overflow_sentinel() -> None:
    executor, store, _events = _executor()
    seed = _seed()
    observed_limits: list[int] = []

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        if kwargs.get("event_type") != "execution.ac.attempt_judged":
            return []
        assert kwargs.get("payload_equals") == {
            "route_contract_version": 1,
            "session_id": "session-1",
        }
        limit = kwargs.get("limit")
        assert isinstance(limit, int) and limit > 1
        observed_limits.append(limit)
        return [_route_judgment(seed) for _ in range(limit)]

    store.query_execution_related_events.side_effect = query
    with pytest.raises(RuntimeError, match="population bound"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )
    assert observed_limits


@pytest.mark.asyncio
async def test_legacy_judgment_population_does_not_consume_route_replay_bound() -> None:
    """The SQL-bound route stream excludes valid unrelated legacy evidence."""

    executor, store, _events = _executor()
    seed = _seed()
    observation, decision = _durable_first_failure(executor, seed)
    route_event = _route_event(seed, observation=observation, decision=decision)
    route_judgment = _judgment_for_route_event(route_event)
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
        event_type = kwargs.get("event_type")
        if event_type == "execution.ac.route_observed":
            return [route_event]
        if event_type != "execution.ac.attempt_judged":
            return []
        # Reproduce the review population: the unfiltered event type is over
        # the former 4,096 sentinel, while the participating route stream is not.
        if kwargs.get("payload_equals") is None:
            return [legacy_judgment] * 4097
        assert kwargs["payload_equals"] == {
            "route_contract_version": 1,
            "session_id": "session-1",
        }
        assert kwargs.get("limit") == len(seed.acceptance_criteria) * MAX_ROUTE_ATTEMPTS + 1
        return [route_judgment]

    store.query_execution_related_events.side_effect = query

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
async def test_composite_completion_query_bound_tracks_admitted_root_population() -> None:
    executor, store, _events = _executor()
    seed = _seed().model_copy(
        update={
            "acceptance_criteria": tuple(
                AcceptanceCriterionSpec(description=f"ship criterion {index}")
                for index in range(257)
            )
        }
    )
    composite_limits: list[int] = []

    async def query(*_args: Any, **kwargs: Any) -> list[BaseEvent]:
        if kwargs.get("event_type") == "execution.ac.composite_completed":
            limit = kwargs.get("limit")
            assert isinstance(limit, int)
            composite_limits.append(limit)
        return []

    store.query_execution_related_events.side_effect = query
    histories, overrides, terminals, provisional = await executor._load_bounded_route_resume_state(
        seed=seed,
        execution_id="execution-1",
        session_id="session-1",
        root_ac_indices=(0,),
    )
    assert histories == {0: ()}
    assert overrides == {}
    assert terminals == {}
    assert provisional == {}
    assert composite_limits == [258]


@pytest.mark.asyncio
async def test_valid_4097_composite_completion_population_replays_all_roots(
    tmp_path: Any,
) -> None:
    """Every admitted root can own one terminal composite without a fixed cap."""

    root_count = 4_097
    producer, _mock_store, events = _executor()
    seed = _seed().model_copy(
        update={
            "acceptance_criteria": tuple(
                AcceptanceCriterionSpec(description=f"ship criterion {index}")
                for index in range(root_count)
            )
        }
    )
    for root_ac_index, criterion in enumerate(seed.acceptance_criteria):
        completed = ACExecutionResult(
            ac_index=root_ac_index,
            ac_content=str(criterion),
            success=True,
            final_message=f"composite {root_ac_index} complete",
            is_decomposed=True,
            sub_results=(
                ACExecutionResult(ac_index=100, ac_content="first child", success=True, depth=1),
                ACExecutionResult(ac_index=101, ac_content="second child", success=True, depth=1),
            ),
            outcome=ACExecutionOutcome.SUCCEEDED,
            decomposition_decision=_split_decision(root_ac_index=root_ac_index),
        )
        await producer._persist_composite_completion(
            seed=seed,
            result=completed,
            root_ac_index=root_ac_index,
            session_id="session-1",
            execution_id="execution-1",
        )

    completion_events = [
        event for event in events if event.type == "execution.ac.composite_completed"
    ]
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'composite-population.db'}")
    await store.initialize()
    await store.append_batch(completion_events)
    replay = _live_executor(
        adapter=_Adapter(),
        event_store=store,
        working_directory=str(tmp_path),
        process_local_resume_nonce="a" * 32,
    )
    for root_ac_index in range(root_count):
        replay._publish_event_owned_decomposition_decision(
            _split_decision(root_ac_index=root_ac_index)
        )
    try:
        (
            _histories,
            _overrides,
            _terminals,
            restored,
        ) = await replay._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=tuple(range(root_count)),
        )

        assert len(restored) == root_count
        assert restored[0].final_message == "composite 0 complete"
        assert restored[root_count - 1].final_message == f"composite {root_count - 1} complete"
    finally:
        await store.close()


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
    assert restored.messages == ()
    assert restored.context_summary is not None
    assert restored.context_summary.tools_used == ("Write",)
    assert restored.context_summary.files_modified == (str(artifact),)
    assert restored.conflict_files == (str(artifact),)
    assert restored.verify_gate_outcome == original.verify_gate_outcome
    settled = await executor._settle_verify_gate_results(
        seed=seed,
        results=[restored],
        session_id="session-1",
        execution_id="execution-1",
    )
    assert settled[0].success is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["unknown_result_field", "non_string_session", "unknown_verify_field", "non_string_reason"],
)
async def test_provisional_success_replay_rejects_non_strict_cached_evidence(
    mutation: str,
) -> None:
    executor, store, _events = _executor()
    seed = _seed()
    candidate = _candidate(executor, "compat:claude:frugal")
    observation = RouteObservation.from_candidate(
        candidate,
        RouteRequirements(),
        episode_id=_episode_id(seed),
        attempt_index=0,
        verifier_outcome=VerifierOutcome.ATTEMPT_SUCCEEDED,
    )
    event = _route_event(seed, observation=observation, decision=None)
    cached = event.data["provisional_result"]
    assert isinstance(cached, dict)
    if mutation == "unknown_result_field":
        cached["accepted"] = True
    elif mutation == "non_string_session":
        cached["session_id"] = 123
    else:
        verify = {
            "passed": True,
            "reason": None,
            "output_tail": "ok",
            "missing_artifacts": [],
            "workspace_mutated": False,
            "workspace_digest": None,
        }
        if mutation == "unknown_verify_field":
            verify["acceptance_authority"] = "gate"
        else:
            verify["reason"] = 123
        cached["verify_gate_outcome"] = verify
    _set_route_replay_events(store, [event])

    with pytest.raises(RuntimeError, match="provisional route success"):
        await executor._load_bounded_route_resume_state(
            seed=seed,
            execution_id="execution-1",
            session_id="session-1",
            root_ac_indices=(0,),
        )


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
