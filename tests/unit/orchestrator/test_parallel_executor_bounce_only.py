"""Bounce-only decomposition recovery regressions for issue #1400."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.decomposition_policy import (
    BounceCause,
    DecompositionChild,
    DecompositionDecisionRecord,
    DecompositionDisposition,
    DecompositionProposal,
    DecompositionSource,
    DecompositionTraceSummary,
    SemanticAttestationStatus,
    StructuralCheckStatus,
)
from ouroboros.orchestrator.dependency_analyzer import ACNode, ExecutionStage, StagedExecutionPlan
from ouroboros.orchestrator.execution_authority import canonical_workspace_authority
from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelACExecutor,
)
from ouroboros.orchestrator.verifier import RetryAdmission, VerifierVerdict
from ouroboros.persistence.checkpoint import CheckpointData
from tests.unit.orchestrator.parallel_executor_test_support import ProcessLocalTestExecutor


def _failed_result(*, failure_class: str = "SCOPE_CREEP") -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=0,
        ac_content="Parent work",
        success=False,
        error="Work started but distinct obligations remain. password=hunter2",
        messages=(AgentMessage(type="tool", content="called", tool_name="Read"),),
        atomic_verifier_verdict=VerifierVerdict(
            passed=False,
            reasons=("Observed work covers only one part of the parent contract.",),
            failure_class=failure_class,
            evidence_used=("attempt:read",),
            retry_admission=(
                RetryAdmission.ESCALATE_MODEL
                if failure_class == "FABRICATION_SUSPECTED"
                else RetryAdmission.REDISPATCH
            ),
        ),
    )


def _trusted_split(node_id: str) -> DecompositionDecisionRecord:
    return DecompositionDecisionRecord(
        node_id=node_id,
        source=DecompositionSource.BOUNCE,
        disposition=DecompositionDisposition.SPLIT,
        cause=BounceCause.TOO_BIG,
        reasons=("independent_attestation",),
        children=(
            DecompositionChild("Implement remaining output", ("output",), "check output"),
            DecompositionChild("Verify integration", ("integration",), "run tests"),
        ),
        structural_status=StructuralCheckStatus.PASSED,
        semantic_status=SemanticAttestationStatus.ESTABLISHED,
        trustworthy=True,
    )


_BOUNCE_EVENT_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _bounce_event(
    node: ExecutionNodeIdentity,
    *,
    execution_id: str,
    session_id: str,
    evidence_refs: tuple[str, ...] = (),
) -> BaseEvent:
    return BaseEvent(
        type="execution.decomposition.bounce_classified",
        timestamp=_BOUNCE_EVENT_TIME,
        aggregate_type="execution",
        aggregate_id=execution_id,
        data={
            **node.to_event_metadata(),
            "execution_id": execution_id,
            "session_id": session_id,
            "cause": "TOO_BIG",
            "rationale": "Attempt evidence shows distinct parent scope remains.",
            "failure_class": "SCOPE_CREEP",
            "retry_admission": "REDISPATCH",
            "evidence_refs": list(evidence_refs),
            "trace_summary": "attempted_tool_count=1\nremaining_artifact_count=1",
        },
    )


def _finalized_event(
    node: ExecutionNodeIdentity,
    decision: DecompositionDecisionRecord,
    *,
    execution_id: str,
    session_id: str,
) -> BaseEvent:
    return BaseEvent(
        type="execution.decomposition.decision_finalized",
        timestamp=_BOUNCE_EVENT_TIME + timedelta(seconds=1),
        aggregate_type="execution",
        aggregate_id=execution_id,
        data={
            **node.to_event_metadata(),
            **decision.to_dict(),
            "execution_id": execution_id,
            "session_id": session_id,
            "mode": "bounce_only",
            "child_count": len(decision.children),
        },
    )


def _replay_store(*events: BaseEvent) -> AsyncMock:
    store = AsyncMock()

    async def query_events(
        _execution_id: str,
        event_type: str | None = None,
        **_kwargs: Any,
    ) -> list[BaseEvent]:
        return [event for event in events if event.type == event_type]

    store.query_execution_related_events.side_effect = query_events
    return store


def _executor(*, max_depth: int = 2) -> ProcessLocalTestExecutor:
    return ProcessLocalTestExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=AsyncMock(),
        console=MagicMock(),
        max_decomposition_depth=max_depth,
        cross_harness_redispatch=False,
    )


def test_default_executor_decomposition_mode_is_bounce_only() -> None:
    """The live low-level default cannot run a pre-execution decomposition."""
    executor = _executor()

    assert executor._decomposition_mode == "bounce_only"


def test_executor_rejects_direct_preflight_override_before_provider_effect() -> None:
    runtime = MagicMock()

    with pytest.raises(ValueError, match="Unsupported decomposition_mode"):
        ParallelACExecutor(
            adapter=runtime,
            event_store=AsyncMock(),
            console=MagicMock(),
            decomposition_mode="preflight",  # type: ignore[arg-type]
        )

    runtime.execute_task.assert_not_called()


@pytest.mark.asyncio
async def test_successful_first_attempt_never_calls_decomposer() -> None:
    executor = _executor()
    executor._execute_atomic_ac = AsyncMock(
        return_value=ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)
    )
    executor._try_decompose_ac = AsyncMock()
    executor._request_bounce_classification = AsyncMock()

    result = await executor._execute_single_ac(
        ac_index=0,
        ac_content="Parent work",
        session_id="session-success",
        tools=[],
        tool_catalog=None,
        system_prompt="system",
        seed_goal="goal",
        execution_id="exec-success",
    )

    assert result.success is True
    executor._try_decompose_ac.assert_not_awaited()
    executor._request_bounce_classification.assert_not_awaited()


@pytest.mark.asyncio
async def test_usage_limit_pause_never_calls_bounce_classifier_or_decomposer() -> None:
    executor = _executor()
    executor._execute_atomic_ac = AsyncMock(
        return_value=ACExecutionResult(
            ac_index=0,
            ac_content="Parent work",
            success=False,
            messages=(
                AgentMessage(
                    type="result",
                    content="Usage limit reached. Please try again in 5 hours.",
                    data={"subtype": "error", "error_type": "CodexCliError"},
                ),
            ),
        )
    )
    executor._try_decompose_ac = AsyncMock()
    executor._request_bounce_classification = AsyncMock()

    result = await executor._execute_single_ac(
        ac_index=0,
        ac_content="Parent work",
        session_id="session-quota",
        tools=[],
        tool_catalog=None,
        system_prompt="system",
        seed_goal="goal",
        execution_id="exec-quota",
    )

    assert result.success is False
    executor._try_decompose_ac.assert_not_awaited()
    executor._request_bounce_classification.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_failure_keeps_existing_recovery_without_decomposition() -> None:
    executor = _executor()
    executor._execute_atomic_ac = AsyncMock(
        return_value=_failed_result(failure_class="FABRICATION_SUSPECTED")
    )
    executor._try_decompose_ac = AsyncMock()
    executor._request_bounce_classification = AsyncMock()

    result = await executor._execute_single_ac(
        ac_index=0,
        ac_content="Parent work",
        session_id="session-model",
        tools=[],
        tool_catalog=None,
        system_prompt="system",
        seed_goal="goal",
        execution_id="exec-model",
    )

    assert result.success is False
    executor._try_decompose_ac.assert_not_awaited()
    executor._request_bounce_classification.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [ACExecutionOutcome.BLOCKED, ACExecutionOutcome.INVALID],
)
async def test_authority_failure_never_enters_provider_backed_recovery(
    outcome: ACExecutionOutcome,
) -> None:
    executor = _executor()
    authority_failure = ACExecutionResult(
        ac_index=0,
        ac_content="Parent work",
        success=False,
        error="route admission blocked",
        outcome=outcome,
    )
    executor._execute_atomic_ac = AsyncMock(return_value=authority_failure)
    executor._request_bounce_classification = AsyncMock()
    executor._maybe_redispatch_alt_harness = AsyncMock()

    result = await executor._execute_single_ac(
        ac_index=0,
        ac_content="Parent work",
        session_id="session-authority-failure",
        tools=[],
        tool_catalog=None,
        system_prompt="system",
        seed_goal="goal",
        execution_id="exec-authority-failure",
    )

    assert result.outcome is outcome
    executor._request_bounce_classification.assert_not_awaited()
    executor._maybe_redispatch_alt_harness.assert_not_awaited()


@pytest.mark.asyncio
async def test_abandoned_stall_runs_bounce_recovery_without_losing_semantic_key() -> None:
    executor = _executor()
    executor._execute_atomic_ac = AsyncMock(
        side_effect=lambda **kwargs: ACExecutionResult(
            ac_index=kwargs["ac_index"],
            ac_content=kwargs["ac_content"],
            success=False,
            error="__STALL_DETECTED__",
            retry_attempt=kwargs["retry_attempt"],
            depth=kwargs["depth"],
        )
    )
    executor._request_bounce_classification = AsyncMock(return_value=(BounceCause.UNKNOWN, False))

    result = await executor._execute_single_ac(
        ac_index=0,
        ac_content="Parent work",
        session_id="session-stall",
        tools=[],
        tool_catalog=None,
        system_prompt="system",
        seed_goal="goal",
        execution_id="exec-stall",
    )

    assert result.success is False
    assert result.error.startswith("Stalled (no activity for ")
    executor._request_bounce_classification.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_runtime_budget_exhausted", [False, True])
async def test_evidence_backed_too_big_bounce_dispatches_trusted_children(
    same_runtime_budget_exhausted: bool,
) -> None:
    executor = _executor()
    node = ExecutionNodeIdentity.root(execution_context_id="exec-too-big", ac_index=0)
    executor._request_bounce_classification = AsyncMock(return_value=(BounceCause.TOO_BIG, True))
    executor._try_decompose_ac = AsyncMock(return_value=_trusted_split(node.node_id))
    executor._maybe_redispatch_alt_harness = AsyncMock()

    async def execute_atomic(**kwargs: Any) -> ACExecutionResult:
        if kwargs["depth"] == 0:
            return _failed_result()
        return ACExecutionResult(
            ac_index=kwargs["ac_index"],
            ac_content=kwargs["ac_content"],
            success=True,
            depth=kwargs["depth"],
        )

    executor._execute_atomic_ac = AsyncMock(side_effect=execute_atomic)
    executor._emit_subtask_event = AsyncMock()

    result = await executor._execute_single_ac(
        ac_index=0,
        ac_content="Parent work",
        session_id="session-too-big",
        tools=[],
        tool_catalog=None,
        system_prompt="system",
        seed_goal="goal",
        execution_id="exec-too-big",
        node_identity=node,
        same_runtime_budget_exhausted=same_runtime_budget_exhausted,
    )

    assert result.success is True
    assert result.is_decomposed is True
    assert result.decomposition_trustworthy is True
    assert [child.ac_content for child in result.sub_results] == [
        "Implement remaining output",
        "Verify integration",
    ]
    executor._try_decompose_ac.assert_awaited_once()
    executor._maybe_redispatch_alt_harness.assert_not_awaited()
    assert executor._try_decompose_ac.await_args.kwargs["source"] is DecompositionSource.BOUNCE
    assert executor._try_decompose_ac.await_args.kwargs["cause"] is BounceCause.TOO_BIG
    bounce_events = [
        call.args[0]
        for call in executor._event_store.append.await_args_list
        if call.args[0].type == "execution.decomposition.bounce_classified"
    ]
    assert len(bounce_events) == 1
    assert bounce_events[0].data["cause"] == BounceCause.TOO_BIG.value
    assert bounce_events[0].data["failure_class"] == "SCOPE_CREEP"
    assert bounce_events[0].data["retry_admission"] == RetryAdmission.REDISPATCH.value
    assert "hunter2" not in bounce_events[0].data["trace_summary"]


@pytest.mark.asyncio
async def test_bounce_persistence_failure_prevents_decomposition_provider_effect() -> None:
    executor = _executor()
    executor._execute_atomic_ac = AsyncMock(return_value=_failed_result())
    executor._request_bounce_classification = AsyncMock(return_value=(BounceCause.TOO_BIG, True))
    executor._try_decompose_ac = AsyncMock()
    executor._event_store.append.side_effect = RuntimeError("event store unavailable")

    with pytest.raises(RuntimeError, match="event store unavailable"):
        await executor._execute_single_ac(
            ac_index=0,
            ac_content="Parent work",
            session_id="session-bounce-persist",
            tools=[],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="goal",
            execution_id="exec-bounce-persist",
        )

    executor._try_decompose_ac.assert_not_awaited()


@pytest.mark.asyncio
async def test_decision_persistence_failure_prevents_child_dispatch() -> None:
    executor = _executor()
    node = ExecutionNodeIdentity.root(execution_context_id="exec-decision-persist", ac_index=0)
    executor._execute_atomic_ac = AsyncMock(return_value=_failed_result())
    executor._request_bounce_classification = AsyncMock(return_value=(BounceCause.TOO_BIG, True))
    executor._try_decompose_ac = AsyncMock(return_value=_trusted_split(node.node_id))
    executor._event_store.append.side_effect = [None, RuntimeError("decision append failed")]
    executor._execute_decomposition_children = AsyncMock()

    with pytest.raises(RuntimeError, match="decision append failed"):
        await executor._execute_single_ac(
            ac_index=0,
            ac_content="Parent work",
            session_id="session-decision-persist",
            tools=[],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="goal",
            execution_id="exec-decision-persist",
            node_identity=node,
        )

    executor._execute_decomposition_children.assert_not_awaited()
    assert node.node_id not in executor._decomposition_decisions


@pytest.mark.asyncio
async def test_too_big_at_depth_cap_records_escalated_compromise() -> None:
    executor = _executor(max_depth=0)
    executor._execute_atomic_ac = AsyncMock(return_value=_failed_result())
    executor._request_bounce_classification = AsyncMock(return_value=(BounceCause.TOO_BIG, True))
    executor._try_decompose_ac = AsyncMock()

    result = await executor._execute_single_ac(
        ac_index=0,
        ac_content="Parent work",
        session_id="session-depth",
        tools=[],
        tool_catalog=None,
        system_prompt="system",
        seed_goal="goal",
        execution_id="exec-depth",
    )

    assert result.success is False
    assert result.decomposition_depth_warning is True
    assert result.decomposition_decision is not None
    assert result.decomposition_decision.disposition is DecompositionDisposition.ESCALATED
    assert result.decomposition_decision.cause is BounceCause.TOO_BIG
    assert result.decomposition_decision.compromise_reason == "depth_cap_forced_atomic"
    executor._try_decompose_ac.assert_not_awaited()
    finalized_events = [
        call.args[0]
        for call in executor._event_store.append.await_args_list
        if call.args[0].type == "execution.decomposition.decision_finalized"
    ]
    assert len(finalized_events) == 1
    assert finalized_events[0].data["compromise_reason"] == "depth_cap_forced_atomic"
    assert finalized_events[0].data["trustworthy"] is False


@pytest.mark.asyncio
async def test_restored_trusted_bounce_decision_dispatches_without_reclassification() -> None:
    executor = _executor()
    node = ExecutionNodeIdentity.root(execution_context_id="exec-restored", ac_index=0)
    executor._publish_event_owned_decomposition_decision(_trusted_split(node.node_id))
    execute_atomic_ac = AsyncMock(
        side_effect=lambda **kwargs: ACExecutionResult(
            ac_index=kwargs["ac_index"],
            ac_content=kwargs["ac_content"],
            success=True,
            depth=kwargs["depth"],
        )
    )
    executor._execute_atomic_ac = execute_atomic_ac
    executor._try_decompose_ac = AsyncMock()
    executor._request_bounce_classification = AsyncMock()
    executor._emit_subtask_event = AsyncMock()

    result = await executor._execute_single_ac(
        ac_index=0,
        ac_content="Parent work",
        session_id="session-restored",
        tools=[],
        tool_catalog=None,
        system_prompt="system",
        seed_goal="goal",
        execution_id="exec-restored",
        node_identity=node,
    )

    assert result.is_decomposed is True
    assert execute_atomic_ac.await_count == 2
    executor._try_decompose_ac.assert_not_awaited()
    executor._request_bounce_classification.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_only_trusted_split_fails_before_parent_or_child_provider_entry() -> None:
    executor = _executor()
    node = ExecutionNodeIdentity.root(execution_context_id="exec-cache-only", ac_index=0)
    executor._decomposition_decisions[node.node_id] = _trusted_split(node.node_id)
    execute_atomic = AsyncMock()
    execute_children = AsyncMock()
    executor._execute_atomic_ac = execute_atomic
    executor._execute_decomposition_children = execute_children

    with pytest.raises(RuntimeError, match="finalized-event authority"):
        await executor._execute_single_ac(
            ac_index=0,
            ac_content="Parent work",
            session_id="session-cache-only",
            tools=[],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="goal",
            execution_id="exec-cache-only",
            node_identity=node,
        )

    execute_atomic.assert_not_awaited()
    execute_children.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_too_big_replay_resumes_decomposer_before_atomic_parent() -> None:
    node = ExecutionNodeIdentity.root(execution_context_id="exec-pending", ac_index=0)
    bounce_event = _bounce_event(
        node,
        execution_id="exec-pending",
        session_id="session-pending",
    )
    executor = ProcessLocalTestExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=_replay_store(bounce_event),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        cross_harness_redispatch=False,
    )
    seed = Seed(
        goal="Resume pending bounce",
        constraints=(),
        acceptance_criteria=("Parent work",),
        ontology_schema=OntologySchema(name="Replay", description="Test schema"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )
    await executor._restore_bounce_classifications(
        seed=seed,
        execution_id="exec-pending",
        session_id="session-pending",
    )
    decision = _trusted_split(node.node_id)
    try_decompose = AsyncMock(return_value=decision)
    execute_atomic = AsyncMock(side_effect=AssertionError("parent reran"))
    classify = AsyncMock(side_effect=AssertionError("classifier reran"))
    execute_children = AsyncMock(
        return_value=ACExecutionResult(
            ac_index=0,
            ac_content="Parent work",
            success=True,
            is_decomposed=True,
            decomposition_decision=decision,
        )
    )
    executor._try_decompose_ac = try_decompose
    executor._execute_atomic_ac = execute_atomic
    executor._request_bounce_classification = classify
    executor._execute_decomposition_children = execute_children

    result = await executor._execute_single_ac(
        ac_index=0,
        ac_content="Parent work",
        session_id="session-pending",
        tools=[],
        tool_catalog=None,
        system_prompt="system",
        seed_goal=seed.goal,
        execution_id="exec-pending",
        node_identity=node,
    )

    assert result.success and result.is_decomposed
    try_decompose.assert_awaited_once()
    execute_atomic.assert_not_awaited()
    classify.assert_not_awaited()
    assert executor._pending_bounce_decompositions == {}
    assert executor._event_owned_decomposition_decisions[node.node_id] == decision


@pytest.mark.asyncio
async def test_checkpoint_persists_and_restores_decision_by_stable_node_id() -> None:
    node = ExecutionNodeIdentity.root(execution_context_id="exec-checkpoint", ac_index=0)
    decision = _trusted_split(node.node_id)
    seed = Seed(
        goal="Checkpoint decomposition decisions",
        constraints=(),
        acceptance_criteria=("Parent work",),
        ontology_schema=OntologySchema(name="Checkpoint", description="Test schema"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )
    plan = StagedExecutionPlan(
        nodes=(ACNode(index=0, content="Parent work"),),
        stages=(ExecutionStage(index=0, ac_indices=(0,)),),
    )
    store = MagicMock()
    store.load.return_value = SimpleNamespace(is_ok=False)
    store.save.return_value = SimpleNamespace(is_ok=True)
    bounce_event = _bounce_event(
        node,
        execution_id="exec-checkpoint",
        session_id="session-checkpoint",
    )
    finalized_event = _finalized_event(
        node,
        decision,
        execution_id="exec-checkpoint",
        session_id="session-checkpoint",
    )
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=_replay_store(bounce_event, finalized_event),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        checkpoint_store=store,
        cross_harness_redispatch=False,
    )
    executor._run_batch_with_verify_and_retry = AsyncMock(
        return_value=[ACExecutionResult(ac_index=0, ac_content="Parent work", success=True)]
    )

    await executor.execute_parallel(
        seed,
        session_id="session-checkpoint",
        execution_id="exec-checkpoint",
        tools=[],
        system_prompt="system",
        execution_plan=plan,
    )

    checkpoint = store.save.call_args.args[0]
    assert checkpoint.state["decomposition_decisions"] == {node.node_id: decision.to_dict()}

    restore_store = MagicMock()
    restore_store.load.return_value = SimpleNamespace(is_ok=True, value=checkpoint)
    restored_executor = ParallelACExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=_replay_store(bounce_event, finalized_event),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        checkpoint_store=restore_store,
        cross_harness_redispatch=False,
    )
    restored_executor._run_batch_with_verify_and_retry = AsyncMock()

    await restored_executor.execute_parallel(
        seed,
        session_id="session-checkpoint",
        execution_id="exec-checkpoint",
        tools=[],
        system_prompt="system",
        execution_plan=plan,
    )

    assert restored_executor._decomposition_decisions[node.node_id] == decision
    restored_executor._run_batch_with_verify_and_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalized_event_restores_decision_before_batch_execution() -> None:
    node = ExecutionNodeIdentity.root(execution_context_id="exec-event-replay", ac_index=0)
    decision = _trusted_split(node.node_id)
    seed = Seed(
        goal="Replay finalized decision",
        constraints=(),
        acceptance_criteria=("Parent work",),
        ontology_schema=OntologySchema(name="Replay", description="Test schema"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )
    plan = StagedExecutionPlan(
        nodes=(ACNode(index=0, content="Parent work"),),
        stages=(ExecutionStage(index=0, ac_indices=(0,)),),
    )
    bounce_event = _bounce_event(
        node,
        execution_id="exec-event-replay",
        session_id="session-event-replay",
    )
    finalized_event = _finalized_event(
        node,
        decision,
        execution_id="exec-event-replay",
        session_id="session-event-replay",
    )

    replay_store = _replay_store(bounce_event, finalized_event)
    replay = ParallelACExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=replay_store,
        console=MagicMock(),
        decomposition_mode="bounce_only",
        cross_harness_redispatch=False,
    )
    try_decompose = AsyncMock()
    execute_atomic = AsyncMock(side_effect=AssertionError("atomic parent was redispatched"))
    execute_children = AsyncMock(
        return_value=ACExecutionResult(
            ac_index=0,
            ac_content="Parent work",
            success=True,
            is_decomposed=True,
            decomposition_decision=decision,
        )
    )
    replay._try_decompose_ac = try_decompose
    replay._execute_atomic_ac = execute_atomic
    replay._execute_decomposition_children = execute_children

    async def assert_decision_restored(**_kwargs: Any) -> list[ACExecutionResult]:
        assert replay._decomposition_decisions == {node.node_id: decision}
        result = await replay._execute_single_ac(
            ac_index=0,
            ac_content="Parent work",
            session_id="session-event-replay",
            tools=[],
            tool_catalog=None,
            system_prompt="system",
            seed_goal=seed.goal,
            execution_id="exec-event-replay",
            node_identity=node,
        )
        return [result]

    replay._run_batch_with_verify_and_retry = assert_decision_restored  # type: ignore[method-assign]
    await replay.execute_parallel(
        seed,
        session_id="session-event-replay",
        execution_id="exec-event-replay",
        tools=[],
        system_prompt="system",
        execution_plan=plan,
    )

    replay_store.query_execution_related_events.assert_any_await(
        "exec-event-replay",
        event_type="execution.decomposition.decision_finalized",
        limit=1563,
    )
    try_decompose.assert_not_awaited()
    execute_atomic.assert_not_awaited()
    execute_children.assert_awaited_once()


@pytest.mark.asyncio
async def test_historical_preflight_event_replays_without_new_preflight_effect() -> None:
    node = ExecutionNodeIdentity.root(execution_context_id="exec-preflight-replay", ac_index=0)
    decision = replace(
        _trusted_split(node.node_id),
        source=DecompositionSource.PREFLIGHT,
        cause=None,
    )
    event = BaseEvent(
        type="execution.decomposition.decision_finalized",
        aggregate_type="execution",
        aggregate_id="exec-preflight-replay",
        data={
            **node.to_event_metadata(),
            **decision.to_dict(),
            "execution_id": "exec-preflight-replay",
            "session_id": "session-preflight-replay",
            "mode": "preflight",
            "child_count": len(decision.children),
        },
    )
    store = AsyncMock()
    store.query_execution_related_events.return_value = [event]
    replay = ProcessLocalTestExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=store,
        console=MagicMock(),
        decomposition_mode="bounce_only",
        cross_harness_redispatch=False,
    )
    seed = Seed(
        goal="Replay historical preflight",
        constraints=(),
        acceptance_criteria=("Parent work",),
        ontology_schema=OntologySchema(name="Replay", description="Test schema"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )
    await replay._restore_finalized_decomposition_decisions(
        seed=seed,
        execution_id="exec-preflight-replay",
        session_id="session-preflight-replay",
    )
    try_decompose = AsyncMock()
    execute_atomic = AsyncMock(side_effect=AssertionError("historical parent was redispatched"))
    execute_children = AsyncMock(
        return_value=ACExecutionResult(
            ac_index=0,
            ac_content="Parent work",
            success=True,
            is_decomposed=True,
            decomposition_decision=decision,
        )
    )
    replay._try_decompose_ac = try_decompose
    replay._execute_atomic_ac = execute_atomic
    replay._execute_decomposition_children = execute_children

    result = await replay._execute_single_ac(
        ac_index=0,
        ac_content="Parent work",
        session_id="session-preflight-replay",
        tools=[],
        tool_catalog=None,
        system_prompt="system",
        seed_goal=seed.goal,
        execution_id="exec-preflight-replay",
        node_identity=node,
    )

    assert result.is_decomposed
    try_decompose.assert_not_awaited()
    execute_atomic.assert_not_awaited()
    execute_children.assert_awaited_once()


@pytest.mark.asyncio
async def test_bounce_finalized_event_without_prior_too_big_fails_replay() -> None:
    node = ExecutionNodeIdentity.root(execution_context_id="exec-orphan-final", ac_index=0)
    decision = _trusted_split(node.node_id)
    replay = _executor()
    replay._event_store = _replay_store(
        _finalized_event(
            node,
            decision,
            execution_id="exec-orphan-final",
            session_id="session-orphan-final",
        )
    )
    replay._event_emitter = replay._event_emitter.__class__(
        replay._event_store,
        safe_emit_event=replay._safe_emit_event,
    )
    seed = Seed(
        goal="Reject orphan final",
        constraints=(),
        acceptance_criteria=("Parent work",),
        ontology_schema=OntologySchema(name="Replay", description="Test schema"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )

    with pytest.raises(RuntimeError, match="prior TOO_BIG"):
        await replay._restore_finalized_decomposition_decisions(
            seed=seed,
            execution_id="exec-orphan-final",
            session_id="session-orphan-final",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_kind", ["unknown", "truncated", "container"])
async def test_checkpoint_malformed_decomposition_decisions_fail_before_execution(
    malformed_kind: str,
) -> None:
    node = ExecutionNodeIdentity.root(execution_context_id="exec-corrupt", ac_index=0)
    decision_data = _trusted_split(node.node_id).to_dict()
    if malformed_kind == "unknown":
        decision_data["unknown"] = True
        raw_decisions: object = {node.node_id: decision_data}
    elif malformed_kind == "truncated":
        del decision_data["source"]
        raw_decisions = {node.node_id: decision_data}
    else:
        raw_decisions = [decision_data]
    seed = Seed(
        goal="Reject malformed checkpoint",
        constraints=(),
        acceptance_criteria=("Parent work",),
        ontology_schema=OntologySchema(name="Checkpoint", description="Test schema"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )
    plan = StagedExecutionPlan(
        nodes=(ACNode(index=0, content="Parent work"),),
        stages=(ExecutionStage(index=0, ac_indices=(0,)),),
    )
    checkpoint = CheckpointData.create(
        getattr(seed, "id", "session-corrupt"),
        "parallel_execution",
        {
            "session_id": "session-corrupt",
            "execution_id": "exec-corrupt",
            "workspace_identity": canonical_workspace_authority("/tmp/project"),
            "completed_levels": 0,
            "decomposition_decisions": raw_decisions,
        },
    )
    checkpoint_store = MagicMock()
    checkpoint_store.load.return_value = SimpleNamespace(is_ok=True, value=checkpoint)
    event_store = AsyncMock()
    event_store.query_execution_related_events.return_value = []
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=event_store,
        console=MagicMock(),
        decomposition_mode="bounce_only",
        checkpoint_store=checkpoint_store,
        cross_harness_redispatch=False,
    )
    executor._run_batch_with_verify_and_retry = AsyncMock()

    with pytest.raises(RuntimeError, match="checkpoint decomposition"):
        await executor.execute_parallel(
            seed,
            session_id="session-corrupt",
            execution_id="exec-corrupt",
            tools=[],
            system_prompt="system",
            execution_plan=plan,
        )

    executor._run_batch_with_verify_and_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_cache_only_checkpoint_fails_before_execution() -> None:
    node = ExecutionNodeIdentity.root(execution_context_id="exec-cache-checkpoint", ac_index=0)
    decision = _trusted_split(node.node_id)
    seed = Seed(
        goal="Reject cache-only checkpoint",
        constraints=(),
        acceptance_criteria=("Parent work",),
        ontology_schema=OntologySchema(name="Checkpoint", description="Test schema"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )
    plan = StagedExecutionPlan(
        nodes=(ACNode(index=0, content="Parent work"),),
        stages=(ExecutionStage(index=0, ac_indices=(0,)),),
    )
    checkpoint = CheckpointData.create(
        getattr(seed, "id", "session-cache-checkpoint"),
        "parallel_execution",
        {
            "session_id": "session-cache-checkpoint",
            "execution_id": "exec-cache-checkpoint",
            "workspace_identity": canonical_workspace_authority("/tmp/project"),
            "completed_levels": 0,
            "decomposition_decisions": {node.node_id: decision.to_dict()},
        },
    )
    checkpoint_store = MagicMock()
    checkpoint_store.load.return_value = SimpleNamespace(is_ok=True, value=checkpoint)
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=_replay_store(),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        checkpoint_store=checkpoint_store,
        cross_harness_redispatch=False,
    )
    executor._run_batch_with_verify_and_retry = AsyncMock()

    with pytest.raises(RuntimeError, match="checkpoint.*finalized-event authority"):
        await executor.execute_parallel(
            seed,
            session_id="session-cache-checkpoint",
            execution_id="exec-cache-checkpoint",
            tools=[],
            system_prompt="system",
            execution_plan=plan,
        )

    executor._run_batch_with_verify_and_retry.assert_not_awaited()


def test_mismatched_decision_identity_fails_closed() -> None:
    executor = _executor()
    node = ExecutionNodeIdentity.root(execution_context_id="exec-identity", ac_index=0)

    coerced = executor._coerce_decomposition_decision(
        _trusted_split("different-node"),
        node_identity=node,
        source=DecompositionSource.BOUNCE,
        cause=BounceCause.TOO_BIG,
    )

    assert coerced.node_id == node.node_id
    assert coerced.disposition is DecompositionDisposition.UNKNOWN
    assert coerced.trustworthy is False
    assert coerced.reasons == ("decomposition_decision_identity_mismatch",)


@pytest.mark.asyncio
async def test_finalized_decision_event_is_idempotent_for_equal_record() -> None:
    node = ExecutionNodeIdentity.root(execution_context_id="exec-event", ac_index=0)
    bounce_event = _bounce_event(
        node,
        execution_id="exec-event",
        session_id="session-event",
    )
    store = _replay_store(bounce_event)
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=store,
        console=MagicMock(),
        decomposition_mode="bounce_only",
        cross_harness_redispatch=False,
    )
    decision = _trusted_split(node.node_id)
    await executor._restore_bounce_classifications(
        seed=Seed(
            goal="Finalize once",
            constraints=(),
            acceptance_criteria=("Parent work",),
            ontology_schema=OntologySchema(name="Finalize", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        ),
        execution_id="exec-event",
        session_id="session-event",
    )

    await executor._finalize_decomposition_decision(
        decision=decision,
        node_identity=node,
        execution_id="exec-event",
        session_id="session-event",
    )
    await executor._finalize_decomposition_decision(
        decision=decision,
        node_identity=node,
        execution_id="exec-event",
        session_id="session-event",
    )
    with pytest.raises(RuntimeError, match="finalized decomposition decision cannot change"):
        await executor._finalize_decomposition_decision(
            decision=DecompositionDecisionRecord(
                node_id=node.node_id,
                source=DecompositionSource.BOUNCE,
                disposition=DecompositionDisposition.ESCALATED,
                cause=BounceCause.TOO_BIG,
                reasons=("repair failed",),
                compromise_reason="generic_decomposition_repair_failed",
            ),
            node_identity=node,
            execution_id="exec-event",
            session_id="session-event",
        )

    events = [
        call.args[0]
        for call in store.append.await_args_list
        if call.args[0].type == "execution.decomposition.decision_finalized"
    ]
    assert len(events) == 1
    assert events[0].data["mode"] == "bounce_only"
    assert events[0].data["child_count"] == 2
    assert events[0].data["trustworthy"] is True


def test_trace_summary_is_bounded_and_does_not_copy_tool_inputs() -> None:
    executor = _executor()
    result = _failed_result()
    unsafe_message = AgentMessage(
        type="tool",
        content="password=hunter2 token=abc123",
        tool_name="Bash",
        data={"tool_input": {"command": "export API_KEY=secret-value"}},
    )
    result = ACExecutionResult(
        ac_index=result.ac_index,
        ac_content=result.ac_content,
        success=False,
        error=result.error + (" x" * 2000),
        messages=(*result.messages, unsafe_message),
        atomic_verifier_verdict=result.atomic_verifier_verdict,
    )

    trace = executor._build_decomposition_trace_summary(result=result, ac_spec=None)

    assert len(trace.summary) <= 1000
    assert "hunter2" not in trace.summary
    assert "abc123" not in trace.summary
    assert "secret-value" not in trace.summary
    assert "attempt_message_count=2" in trace.summary
    assert "attempted_tool_count=2" in trace.summary
    assert "verifier_reason_count=1" in trace.summary
    assert trace.evidence_refs == ()


class _SequenceRuntime:
    runtime_backend = "claude"
    working_directory = "/tmp/project"

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []
        self.resume_handles: list[RuntimeHandle | None] = []

    async def execute_task(
        self,
        prompt: str,
        *,
        resume_handle: RuntimeHandle | None = None,
        **_kwargs: Any,
    ):
        self.prompts.append(prompt)
        self.resume_handles.append(resume_handle)
        yield AgentMessage(type="result", content=self.responses.pop(0))


@pytest.mark.asyncio
async def test_classifier_rejects_oversized_output_instead_of_normalizing_authority() -> None:
    runtime = _SequenceRuntime(
        [
            json.dumps(
                {
                    "cause": "TOO_BIG",
                    "has_remaining_scope": True,
                    "explanation": "token=secret-value " + ("x" * 500),
                }
            )
        ]
    )
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        cross_harness_redispatch=False,
    )

    cause, remaining = await executor._request_bounce_classification(
        trace=DecompositionTraceSummary(summary="attempted_tools=Read")
    )

    assert cause is BounceCause.UNKNOWN
    assert remaining is False


@pytest.mark.asyncio
async def test_classifier_admits_only_closed_cause_and_scope_metadata() -> None:
    runtime = _SequenceRuntime([json.dumps({"cause": "TOO_BIG", "has_remaining_scope": True})])
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        cross_harness_redispatch=False,
    )

    result = await executor._request_bounce_classification(
        trace=DecompositionTraceSummary(summary="attempted_tool_count=1")
    )

    assert result == (BounceCause.TOO_BIG, True)


@pytest.mark.asyncio
async def test_classifier_uses_fresh_session_when_executor_inherits_runtime_handle() -> None:
    runtime = _SequenceRuntime([json.dumps({"cause": "TOO_BIG", "has_remaining_scope": True})])
    inherited = RuntimeHandle(
        backend="claude",
        native_session_id="parent-conversation",
        cwd="/tmp/project",
    )
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        inherited_runtime_handle=inherited,
        cross_harness_redispatch=False,
    )

    result = await executor._request_bounce_classification(
        trace=DecompositionTraceSummary(summary="attempted_tool_count=1")
    )

    assert result == (BounceCause.TOO_BIG, True)
    assert runtime.resume_handles == [None]


@pytest.mark.asyncio
async def test_classifier_requires_exact_bounded_schema() -> None:
    payload = {
        "cause": "TOO_BIG",
        "has_remaining_scope": True,
        "decompose_now": True,
    }
    runtime = _SequenceRuntime([json.dumps(payload)])
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        cross_harness_redispatch=False,
    )

    result = await executor._request_bounce_classification(
        trace=DecompositionTraceSummary(summary="attempted_tools=Read")
    )

    assert result == (BounceCause.UNKNOWN, False)


@pytest.mark.asyncio
async def test_classifier_cannot_self_author_attempt_evidence() -> None:
    executor = _executor()
    result = ACExecutionResult(
        ac_index=0,
        ac_content="Parent work",
        success=False,
        error="No attempt transcript was captured",
        atomic_verifier_verdict=VerifierVerdict(
            passed=False,
            reasons=("Scope may remain.",),
            failure_class="SCOPE_CREEP",
            evidence_used=(),
            retry_admission=RetryAdmission.REDISPATCH,
        ),
    )
    trace = executor._build_decomposition_trace_summary(result=result, ac_spec=None)
    executor._request_bounce_classification = AsyncMock(return_value=(BounceCause.TOO_BIG, True))

    classification = await executor._classify_bounce_result(result=result, trace=trace)

    assert classification.cause is BounceCause.UNKNOWN
    assert classification.allows_decomposition is False


def _generic_proposal(*, duplicate: bool = False) -> str:
    second = "Implement output" if duplicate else "Verify integration"
    return json.dumps(
        {
            "children": [
                {
                    "description": "Implement output",
                    "coverage_claims": ["output"],
                    "verification_hint": "check output",
                },
                {
                    "description": second,
                    "coverage_claims": ["integration"],
                    "verification_hint": "run tests",
                },
            ],
            "covers_parent": True,
            "rationale": "Distinct output and integration obligations.",
        }
    )


def _attestation(*, established: bool) -> str:
    return json.dumps(
        {
            "coverage_established": established,
            "non_overlap_established": established,
            "simpler_units_established": established,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["unknown_field", "non_boolean"])
async def test_semantic_attestation_rejects_non_strict_payloads(mutation: str) -> None:
    payload = json.loads(_attestation(established=True))
    if mutation == "unknown_field":
        payload["authorizes_child_dispatch"] = True
    else:
        payload["non_overlap_established"] = 1
    runtime = _SequenceRuntime([json.dumps(payload)])
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        cross_harness_redispatch=False,
    )
    proposal = DecompositionProposal.from_dict(json.loads(_generic_proposal()))
    assert proposal is not None

    established, reasons = await executor._attest_decomposition_proposal(
        parent_text="Produce output and verify integration",
        proposal=proposal,
        trace_summary="remaining_artifacts=report.json",
        system_prompt="system",
    )

    assert established is False
    assert reasons == ("semantic_attestation_unparseable",)


@pytest.mark.asyncio
async def test_generic_structured_proposal_requires_independent_attestation() -> None:
    runtime = _SequenceRuntime([_generic_proposal(), _attestation(established=True)])
    inherited_handle = RuntimeHandle(backend="claude", native_session_id="proposal-session")
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        cross_harness_redispatch=False,
        inherited_runtime_handle=inherited_handle,
    )

    result = await executor._try_decompose_ac(
        ac_content="Produce output and verify integration",
        ac_index=0,
        seed_goal="goal",
        tools=[],
        system_prompt="system",
        source=DecompositionSource.BOUNCE,
        cause=BounceCause.TOO_BIG,
        trace_summary="attempted_tools=Read; remaining_artifacts=report.json",
    )

    assert result.disposition is DecompositionDisposition.SPLIT
    assert result.trustworthy is True
    assert result.semantic_status is SemanticAttestationStatus.ESTABLISHED
    assert len(runtime.prompts) == 2
    assert runtime.resume_handles == [inherited_handle, None]


@pytest.mark.asyncio
async def test_live_decomposer_rejects_preflight_invocation_before_provider_effect() -> None:
    runtime = _SequenceRuntime([_generic_proposal()])
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        cross_harness_redispatch=False,
    )

    with pytest.raises(RuntimeError, match="TOO_BIG bounce"):
        await executor._try_decompose_ac(
            ac_content="Produce output and verify integration",
            ac_index=0,
            seed_goal="goal",
            tools=[],
            system_prompt="system",
            source=DecompositionSource.PREFLIGHT,
        )

    assert runtime.prompts == []


@pytest.mark.asyncio
async def test_failed_semantic_attestation_repairs_once_then_admits() -> None:
    runtime = _SequenceRuntime(
        [
            _generic_proposal(),
            _attestation(established=False),
            _generic_proposal(),
            _attestation(established=True),
        ]
    )
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        cross_harness_redispatch=False,
    )

    result = await executor._try_decompose_ac(
        ac_content="Produce output and verify integration",
        ac_index=0,
        seed_goal="goal",
        tools=[],
        system_prompt="system",
        source=DecompositionSource.BOUNCE,
        cause=BounceCause.TOO_BIG,
        trace_summary="remaining_artifacts=report.json",
    )

    assert result.disposition is DecompositionDisposition.SPLIT
    assert result.trustworthy is True
    assert result.repair_count == 1
    assert "non_overlap_not_established" in runtime.prompts[2]
    assert len(runtime.prompts) == 4


@pytest.mark.asyncio
async def test_generic_proposal_gets_exactly_one_repair_then_escalates() -> None:
    runtime = _SequenceRuntime(
        [_generic_proposal(duplicate=True), _generic_proposal(duplicate=True)]
    )
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        cross_harness_redispatch=False,
    )

    result = await executor._try_decompose_ac(
        ac_content="Produce output and verify integration",
        ac_index=0,
        seed_goal="goal",
        tools=[],
        system_prompt="system",
        source=DecompositionSource.BOUNCE,
        cause=BounceCause.TOO_BIG,
        trace_summary="remaining_artifacts=report.json",
    )

    assert result.disposition is DecompositionDisposition.ESCALATED
    assert result.repair_count == 1
    assert result.trustworthy is False
    assert result.compromise_reason == "generic_decomposition_repair_failed"
    assert len(runtime.prompts) == 2
