"""Bounce-only decomposition recovery regressions for issue #1400."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.decomposition_policy import (
    BounceCause,
    DecompositionChild,
    DecompositionDecisionRecord,
    DecompositionDisposition,
    DecompositionSource,
    DecompositionTraceSummary,
    SemanticAttestationStatus,
    StructuralCheckStatus,
)
from ouroboros.orchestrator.dependency_analyzer import ACNode, ExecutionStage, StagedExecutionPlan
from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity
from ouroboros.orchestrator.parallel_executor import ACExecutionResult, ParallelACExecutor
from ouroboros.orchestrator.verifier import RetryAdmission, VerifierVerdict


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


def _executor(*, max_depth: int = 3) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=AsyncMock(),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        max_decomposition_depth=max_depth,
        cross_harness_redispatch=False,
    )


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
@pytest.mark.parametrize("same_runtime_budget_exhausted", [False, True])
async def test_evidence_backed_too_big_bounce_dispatches_trusted_children(
    same_runtime_budget_exhausted: bool,
) -> None:
    executor = _executor()
    node = ExecutionNodeIdentity.root(execution_context_id="exec-too-big", ac_index=0)
    executor._request_bounce_classification = AsyncMock(
        return_value=(BounceCause.TOO_BIG, "Distinct parent scope remains.", ("remaining:1",), True)
    )
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
async def test_too_big_at_depth_cap_records_escalated_compromise() -> None:
    executor = _executor(max_depth=0)
    executor._execute_atomic_ac = AsyncMock(return_value=_failed_result())
    executor._request_bounce_classification = AsyncMock(
        return_value=(BounceCause.TOO_BIG, "Distinct parent scope remains.", (), True)
    )
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
    executor._decomposition_decisions[node.node_id] = _trusted_split(node.node_id)
    executor._execute_atomic_ac = AsyncMock(
        side_effect=lambda **kwargs: ACExecutionResult(
            ac_index=kwargs["ac_index"],
            ac_content=kwargs["ac_content"],
            success=True,
            depth=kwargs["depth"],
        )
    )
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
    assert executor._execute_atomic_ac.await_count == 2
    executor._try_decompose_ac.assert_not_awaited()
    executor._request_bounce_classification.assert_not_awaited()


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
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=AsyncMock(),
        console=MagicMock(),
        decomposition_mode="bounce_only",
        checkpoint_store=store,
        cross_harness_redispatch=False,
    )
    executor._decomposition_decisions[node.node_id] = decision
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
        event_store=AsyncMock(),
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
    store = AsyncMock()
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory="/tmp/project", runtime_backend="claude"),
        event_store=store,
        console=MagicMock(),
        decomposition_mode="bounce_only",
        cross_harness_redispatch=False,
    )
    node = ExecutionNodeIdentity.root(execution_context_id="exec-event", ac_index=0)
    decision = _trusted_split(node.node_id)

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
    assert len(events) == 2
    assert events[0].data["mode"] == "bounce_only"
    assert events[0].data["child_count"] == 2
    assert events[0].data["trustworthy"] is True
    assert events[1].data["compromise_reason"] == "generic_decomposition_repair_failed"


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
    assert "Read, Bash" in trace.summary


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
async def test_classifier_output_is_bounded_and_redacted_before_use() -> None:
    runtime = _SequenceRuntime(
        [
            json.dumps(
                {
                    "cause": "TOO_BIG",
                    "reason": "token=secret-value " + ("x" * 500),
                    "evidence_refs": [f"ref-{index}: password=hunter2" for index in range(20)],
                    "has_remaining_scope": True,
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

    cause, reason, refs, remaining = await executor._request_bounce_classification(
        trace=DecompositionTraceSummary(summary="attempted_tools=Read")
    )

    assert cause is BounceCause.TOO_BIG
    assert remaining is True
    assert len(reason) <= 240
    assert "secret-value" not in reason
    assert len(refs) == 8
    assert all("hunter2" not in ref for ref in refs)


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
    executor._request_bounce_classification = AsyncMock(
        return_value=(BounceCause.TOO_BIG, "Scope remains.", ("invented:ref",), True)
    )

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
            "reasons": [] if established else ["siblings overlap"],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [DecompositionSource.BOUNCE, DecompositionSource.PREFLIGHT],
)
async def test_generic_structured_proposal_requires_independent_attestation(
    source: DecompositionSource,
) -> None:
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
        source=source,
        cause=BounceCause.TOO_BIG if source is DecompositionSource.BOUNCE else None,
        trace_summary="attempted_tools=Read; remaining_artifacts=report.json",
    )

    assert result.disposition is DecompositionDisposition.SPLIT
    assert result.trustworthy is True
    assert result.semantic_status is SemanticAttestationStatus.ESTABLISHED
    assert len(runtime.prompts) == 2
    assert runtime.resume_handles == [inherited_handle, None]


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
    assert "siblings overlap" in runtime.prompts[2]
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
