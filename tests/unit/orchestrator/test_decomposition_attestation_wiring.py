"""Tests wiring the gate-anchored decomposition attestation (harness module)
into the parallel executor: computed right after a decomposition round's
siblings finish dispatch, cached per node id, emitted as a durable event, and
consulted to poison a root AC's NEXT retry's child-tier discount.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.harness.decomposition_attestation import (
    DecompositionTrustAxis,
    DecompositionTrustVerdict,
    SiblingVerifyOutcome,
)
from ouroboros.orchestrator.ac_execution_capsule import ACSuccessContract
from ouroboros.orchestrator.decomposition_policy import (
    DecompositionChild,
    DecompositionDecisionRecord,
    DecompositionDisposition,
    DecompositionSource,
    SemanticAttestationStatus,
    StructuralCheckStatus,
)
from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionResult,
    ParallelACExecutor,
    _VerifyGateOutcome,
)


def _make_executor(event_store: object | None = None) -> ParallelACExecutor:
    executor = ParallelACExecutor(
        adapter=MagicMock(),
        event_store=event_store if event_store is not None else AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
    )
    executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
    executor._emit_workflow_progress = AsyncMock()
    executor._emit_level_started = AsyncMock()
    executor._emit_level_completed = AsyncMock()
    executor._emit_subtask_event = AsyncMock()
    return executor


def _trustworthy_split_decision(
    node_id: str, *, child_count: int = 2
) -> DecompositionDecisionRecord:
    children = tuple(
        DecompositionChild(
            description=f"child {i}",
            coverage_claims=(f"distinct scope {i}",),
            verification_hint=f"check {i}",
        )
        for i in range(child_count)
    )
    return DecompositionDecisionRecord(
        node_id=node_id,
        source=DecompositionSource.PREFLIGHT,
        disposition=DecompositionDisposition.SPLIT,
        children=children,
        structural_status=StructuralCheckStatus.PASSED,
        semantic_status=SemanticAttestationStatus.ESTABLISHED,
        trustworthy=True,
    )


def _child_result(
    ac_index: int,
    *,
    success: bool = True,
    verify_gate_outcome: _VerifyGateOutcome | None = None,
) -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content=f"child {ac_index}",
        success=success,
        verify_gate_outcome=verify_gate_outcome,
    )


class TestAttestDecompositionRound:
    """Direct tests of the new ``_attest_decomposition_round`` helper."""

    @pytest.mark.asyncio
    async def test_no_parent_contract_is_indeterminate(self) -> None:
        executor = _make_executor()
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        final_sub_results = [
            _child_result(
                0, verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
            ),
            _child_result(
                1, verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
            ),
        ]

        attestation, parent_outcome = await executor._attest_decomposition_round(
            node_identity=node_identity,
            final_sub_results=final_sub_results,
            ac_spec=None,
        )

        assert attestation.verdict is DecompositionTrustVerdict.INDETERMINATE
        assert attestation.trustworthy is False
        assert parent_outcome is None

    @pytest.mark.asyncio
    async def test_parent_gate_reverified_and_passes(self) -> None:
        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        final_sub_results = [
            _child_result(
                0, verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
            ),
            _child_result(
                1, verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
            ),
        ]
        ac_spec = AcceptanceCriterionSpec(description="parent AC", verify_command="pytest -q")

        attestation, parent_outcome = await executor._attest_decomposition_round(
            node_identity=node_identity,
            final_sub_results=final_sub_results,
            ac_spec=ac_spec,
        )

        assert attestation.verdict is DecompositionTrustVerdict.TRUSTWORTHY
        executor._run_ac_verify_gate.assert_awaited_once()
        assert parent_outcome is not None
        assert parent_outcome.passed is True

    @pytest.mark.asyncio
    async def test_verify_commands_disabled_is_indeterminate_and_skips_execution(self) -> None:
        """Fix 1 (BLOCKING): an operator who disabled verify-command execution
        must not have the parent's shell command run on their behalf by the
        attestation path either — this must mirror every other verify-gate
        call site's ``self._run_verify_commands`` guard."""
        executor = _make_executor()
        executor._run_verify_commands = False
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        final_sub_results = [
            _child_result(0, verify_gate_outcome=None),
            _child_result(1, verify_gate_outcome=None),
        ]
        ac_spec = AcceptanceCriterionSpec(description="parent AC", verify_command="rm -rf /tmp/x")

        attestation, parent_outcome = await executor._attest_decomposition_round(
            node_identity=node_identity,
            final_sub_results=final_sub_results,
            ac_spec=ac_spec,
        )

        executor._run_ac_verify_gate.assert_not_awaited()
        assert parent_outcome is None
        assert attestation.verdict is DecompositionTrustVerdict.INDETERMINATE
        assert attestation.trustworthy is False

    @pytest.mark.asyncio
    async def test_clobbering_scenario_parent_gate_fails(self) -> None:
        """Every child individually succeeded, but the parent's re-run gate
        fails — a sibling clobbered another's work or left a gap."""
        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(
                passed=False, reason="expected_artifacts missing: out.json", output_tail=""
            )
        )
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        final_sub_results = [
            _child_result(
                0, verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
            ),
            _child_result(
                1, verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
            ),
        ]
        ac_spec = AcceptanceCriterionSpec(description="parent AC", expected_artifacts=("out.json",))

        attestation, parent_outcome = await executor._attest_decomposition_round(
            node_identity=node_identity,
            final_sub_results=final_sub_results,
            ac_spec=ac_spec,
        )

        assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert attestation.trustworthy is False
        assert parent_outcome is not None
        assert parent_outcome.passed is False

    @pytest.mark.asyncio
    async def test_sibling_failure_is_untrustworthy(self) -> None:
        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        final_sub_results = [
            _child_result(
                0,
                success=False,
                verify_gate_outcome=_VerifyGateOutcome(passed=False, reason="boom", output_tail=""),
            ),
            _child_result(
                1, verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
            ),
        ]
        ac_spec = AcceptanceCriterionSpec(description="parent AC", verify_command="pytest -q")

        attestation, _parent_outcome = await executor._attest_decomposition_round(
            node_identity=node_identity,
            final_sub_results=final_sub_results,
            ac_spec=ac_spec,
        )

        assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert attestation.failed_sibling_id == "0"


class TestExecuteDecompositionChildrenWiring:
    """End-to-end wiring through ``_execute_decomposition_children``."""

    @pytest.mark.asyncio
    async def test_first_round_withholds_child_discount_until_gate_attested(self) -> None:
        """Proposal trust alone cannot authorize cheaper child routing.

        The first round has no prior gate evidence by definition, so every
        child must run untrusted at the parent/base tier.  This round's
        successful attestation may authorize a later retry, never the dispatch
        that produced the evidence itself.
        """
        from datetime import UTC, datetime

        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        executor._event_emitter.emit_decomposition_attested = AsyncMock(return_value=True)
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-first", ac_index=0)
        decision = _trustworthy_split_decision(node_identity.node_id)
        captured_trust_flags: list[bool] = []

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            captured_trust_flags.append(bool(kwargs["decomposition_trustworthy"]))
            return _child_result(
                0,
                verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail=""),
            )

        executor._execute_single_ac = AsyncMock(side_effect=fake_execute_single_ac)

        result = await executor._execute_decomposition_children(
            decision=decision,
            ac_index=0,
            ac_content="parent AC",
            session_id="s1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            seed_goal="goal",
            depth=0,
            execution_id="exec-first",
            level_contexts=None,
            retry_attempt=0,
            execution_counters=None,
            node_identity=node_identity,
            start_time=datetime.now(UTC),
            semantic_ac_key="key",
            ac_spec=AcceptanceCriterionSpec(description="parent AC", verify_command="pytest -q"),
        )

        assert decision.trustworthy is True
        assert captured_trust_flags
        assert all(flag is False for flag in captured_trust_flags)
        assert result.dispatched_decomposition_trustworthy is False
        assert result.decomposition_attestation is not None
        assert result.decomposition_attestation.trustworthy is True

    @pytest.mark.asyncio
    async def test_fresh_execution_consumes_registered_trust_for_identical_split(
        self,
    ) -> None:
        """A naturally produced attestation authorizes a real later dispatch."""
        from datetime import UTC, datetime

        from tests.unit.orchestrator.test_parallel_executor import (
            _make_replaying_event_store,
        )

        event_store, events = _make_replaying_event_store()
        ac_spec = AcceptanceCriterionSpec(description="parent AC", verify_command="pytest -q")

        async def _run(*, execution_id: str, captured_trust_flags: list[bool]) -> ACExecutionResult:
            executor = _make_executor(event_store)
            executor._decomposition_attestation_scope = "seed-shared:v2:test"
            executor._run_ac_verify_gate = AsyncMock(
                return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
            )
            node_identity = ExecutionNodeIdentity.root(
                execution_context_id=execution_id,
                ac_index=0,
            )
            decision = _trustworthy_split_decision(node_identity.node_id)

            async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
                captured_trust_flags.append(bool(kwargs["decomposition_trustworthy"]))
                return _child_result(
                    0,
                    verify_gate_outcome=_VerifyGateOutcome(
                        passed=True,
                        reason=None,
                        output_tail="",
                    ),
                )

            executor._execute_single_ac = AsyncMock(side_effect=fake_execute_single_ac)
            return await executor._execute_decomposition_children(
                decision=decision,
                ac_index=0,
                ac_content="parent AC",
                session_id=f"session-{execution_id}",
                tools=[],
                tool_catalog=None,
                system_prompt="",
                seed_goal="goal",
                depth=0,
                execution_id=execution_id,
                level_contexts=None,
                retry_attempt=0,
                execution_counters=None,
                node_identity=node_identity,
                start_time=datetime.now(UTC),
                semantic_ac_key="stable-parent-key",
                ac_spec=ac_spec,
            )

        first_flags: list[bool] = []
        first_result = await _run(execution_id="exec-first", captured_trust_flags=first_flags)
        assert first_flags and all(flag is False for flag in first_flags)
        assert first_result.decomposition_attestation is not None
        assert first_result.decomposition_attestation.trustworthy is True
        assert any(event.type == "decomposition.attestation.registered" for event in events)

        second_flags: list[bool] = []
        second_result = await _run(
            execution_id="exec-second",
            captured_trust_flags=second_flags,
        )
        assert second_flags and all(flag is True for flag in second_flags)
        assert second_result.dispatched_decomposition_trustworthy is True

    def test_reusable_scope_binds_workspace_and_dispatch_authority(self, tmp_path) -> None:
        """Workspace-local verification cannot authorize another checkout.

        The registry scope must also change when the prompt/tool/runtime
        authority changes, even when the serialized Seed and split are
        identical.
        """

        def _scope(workspace, *, system_prompt: str) -> str:
            executor = _make_executor()
            executor._task_cwd = str(workspace)
            contract = executor._build_checkpoint_dispatch_contract(
                tools=["Read", "Edit"],
                tool_catalog=None,
                system_prompt=system_prompt,
                system_prompt_builder=None,
            )
            return executor._build_decomposition_attestation_scope(
                seed_id="seed-shared",
                seed_fingerprint="semantic-fingerprint",
                dispatch_contract=contract,
            )

        workspace_a = tmp_path / "checkout-a"
        workspace_b = tmp_path / "checkout-b"

        scope_a = _scope(workspace_a, system_prompt="same authority")
        assert scope_a == _scope(workspace_a, system_prompt="same authority")
        assert scope_a != _scope(workspace_b, system_prompt="same authority")
        assert scope_a != _scope(workspace_a, system_prompt="changed authority")

    @pytest.mark.asyncio
    async def test_registered_trust_dispatches_real_children_at_lower_tier(
        self,
        tmp_path,
    ) -> None:
        """The reusable verdict reaches the enforced runtime model argument."""
        from datetime import UTC, datetime
        from pathlib import Path

        from ouroboros.orchestrator.adapter import (
            AgentMessage,
            ParamSupport,
            RuntimeCapabilities,
        )
        from tests.unit.orchestrator.test_model_routing_wiring import _claude_router
        from tests.unit.orchestrator.test_parallel_executor import (
            _make_replaying_event_store,
        )

        class _ArtifactRuntime:
            runtime_backend = "claude"
            permission_mode = "acceptEdits"
            capabilities = RuntimeCapabilities(
                skill_dispatch=True,
                targeted_resume=True,
                structured_output=True,
                model_override_support=ParamSupport.NATIVE,
            )

            def __init__(self, cwd: Path) -> None:
                self.working_directory = str(cwd)
                self.models: list[str | None] = []

            async def execute_task(self, *, prompt: str, model: str | None = None, **_kwargs):
                self.models.append(model)
                artifact = "child-0.txt" if "child 0" in prompt else "child-1.txt"
                (Path(self.working_directory) / artifact).write_text("done")
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        children = tuple(
            DecompositionChild(
                description=f"child {index}",
                coverage_claims=(f"distinct scope {index}",),
                verification_hint=f"check {index}",
                expected_artifacts=(f"child-{index}.txt",),
            )
            for index in range(2)
        )
        ac_spec = AcceptanceCriterionSpec(
            description="parent AC",
            expected_artifacts=("child-0.txt", "child-1.txt"),
        )
        event_store, _events = _make_replaying_event_store()

        async def _run(execution_id: str, cwd: Path) -> tuple[ACExecutionResult, list[str | None]]:
            cwd.mkdir(exist_ok=True)
            runtime = _ArtifactRuntime(cwd)
            executor = ParallelACExecutor(
                adapter=runtime,
                event_store=event_store,
                console=MagicMock(),
                enable_decomposition=False,
                task_cwd=str(cwd),
                model_router=_claude_router(),
            )
            dispatch_contract = executor._build_checkpoint_dispatch_contract(
                tools=[],
                tool_catalog=None,
                system_prompt="",
                system_prompt_builder=None,
            )
            executor._decomposition_attestation_scope = (
                executor._build_decomposition_attestation_scope(
                    seed_id="seed-real-shared",
                    seed_fingerprint="semantic-fingerprint",
                    dispatch_contract=dispatch_contract,
                )
            )
            executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
            executor._emit_workflow_progress = AsyncMock()
            executor._emit_level_started = AsyncMock()
            executor._emit_level_completed = AsyncMock()
            executor._emit_subtask_event = AsyncMock()
            node_identity = ExecutionNodeIdentity.root(
                execution_context_id=execution_id,
                ac_index=0,
            )
            decision = DecompositionDecisionRecord(
                node_id=node_identity.node_id,
                source=DecompositionSource.PREFLIGHT,
                disposition=DecompositionDisposition.SPLIT,
                children=children,
                structural_status=StructuralCheckStatus.PASSED,
                semantic_status=SemanticAttestationStatus.ESTABLISHED,
                trustworthy=True,
            )
            result = await executor._execute_decomposition_children(
                decision=decision,
                ac_index=0,
                ac_content="parent AC",
                session_id=f"session-{execution_id}",
                tools=[],
                tool_catalog=None,
                system_prompt="",
                seed_goal="goal",
                depth=0,
                execution_id=execution_id,
                level_contexts=None,
                retry_attempt=0,
                execution_counters=None,
                node_identity=node_identity,
                start_time=datetime.now(UTC),
                semantic_ac_key="stable-parent-key",
                ac_spec=ac_spec,
            )
            return result, runtime.models

        first_workspace = tmp_path / "first"
        first_result, first_models = await _run("exec-real-first", first_workspace)
        assert first_result.decomposition_attestation is not None
        assert first_result.decomposition_attestation.trustworthy is True
        assert first_models == ["sonnet-x", "sonnet-x"]

        other_result, other_models = await _run("exec-real-other", tmp_path / "other")
        assert other_result.dispatched_decomposition_trustworthy is False
        assert other_models == ["sonnet-x", "sonnet-x"]

        for artifact in ("child-0.txt", "child-1.txt"):
            (first_workspace / artifact).unlink()
        reused_result, reused_models = await _run("exec-real-reused", first_workspace)
        assert reused_result.dispatched_decomposition_trustworthy is True
        assert reused_models == ["haiku-x", "haiku-x"]

    @pytest.mark.asyncio
    async def test_attestation_cached_and_event_emitted(self) -> None:
        from datetime import UTC, datetime

        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        executor._event_emitter.emit_decomposition_attested = AsyncMock()
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        decision = _trustworthy_split_decision(node_identity.node_id)

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            return _child_result(
                0,
                verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail=""),
            )

        executor._execute_single_ac = AsyncMock(side_effect=fake_execute_single_ac)

        result = await executor._execute_decomposition_children(
            decision=decision,
            ac_index=0,
            ac_content="parent AC",
            session_id="s1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            seed_goal="goal",
            depth=0,
            execution_id="exec-1",
            level_contexts=None,
            retry_attempt=0,
            execution_counters=None,
            node_identity=node_identity,
            start_time=datetime.now(UTC),
            semantic_ac_key="key",
            ac_spec=AcceptanceCriterionSpec(description="parent AC", verify_command="pytest -q"),
        )

        assert result.decomposition_attestation is not None
        assert result.decomposition_attestation.trustworthy is True
        assert executor._decomposition_attestations[node_identity.node_id].trustworthy is True
        executor._event_emitter.emit_decomposition_attested.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_parent_verify_gate_outcome_reused_not_rerun_by_final_acceptance(self) -> None:
        """Fix 2 (BLOCKING): the parent verify-gate outcome computed for
        attestation must be threaded onto the returned result's
        ``verify_gate_outcome`` cache slot so the LATER, real acceptance gate
        (``_apply_verify_gate``) reuses it instead of invoking
        ``_run_ac_verify_gate`` — and therefore the underlying shell command —
        a second time for the same dispatch."""
        from datetime import UTC, datetime

        from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        executor._event_emitter.emit_decomposition_attested = AsyncMock()
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        decision = _trustworthy_split_decision(node_identity.node_id)
        ac_spec = AcceptanceCriterionSpec(description="parent AC", verify_command="pytest -q")

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            return _child_result(
                0,
                verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail=""),
            )

        executor._execute_single_ac = AsyncMock(side_effect=fake_execute_single_ac)

        result = await executor._execute_decomposition_children(
            decision=decision,
            ac_index=0,
            ac_content="parent AC",
            session_id="s1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            seed_goal="goal",
            depth=0,
            execution_id="exec-1",
            level_contexts=None,
            retry_attempt=0,
            execution_counters=None,
            node_identity=node_identity,
            start_time=datetime.now(UTC),
            semantic_ac_key="key",
            ac_spec=ac_spec,
        )

        # The gate ran exactly once, and the outcome was threaded onto the
        # result's cache slot.
        executor._run_ac_verify_gate.assert_awaited_once()
        assert isinstance(result.verify_gate_outcome, _VerifyGateOutcome)
        assert result.verify_gate_outcome.passed is True

        # The real, later acceptance gate must consume that SAME cached
        # outcome rather than re-invoking ``_run_ac_verify_gate``.
        seed = Seed(
            goal="goal",
            constraints=(),
            acceptance_criteria=(ac_spec,),
            ontology_schema=OntologySchema(name="Attest", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )
        gated = await executor._apply_verify_gate(
            seed=seed,
            ac_index=0,
            result=result,
            session_id="s1",
            execution_id="exec-1",
        )

        executor._run_ac_verify_gate.assert_awaited_once()  # still only once
        assert gated.success is True

    @pytest.mark.asyncio
    async def test_prior_untrustworthy_attestation_poisons_next_retry_child_tier(self) -> None:
        """A previous round's UNTRUSTWORTHY verdict must override a fresh
        (unverified) decision.trustworthy=True on the SAME root AC's retry —
        the child-tier discount must not be re-admitted just because the LLM
        proposed structurally-valid JSON again."""
        from datetime import UTC, datetime

        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        executor._event_emitter.emit_decomposition_attested = AsyncMock()
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        decision = _trustworthy_split_decision(node_identity.node_id)

        # Seed a prior UNTRUSTWORTHY attestation for this exact node id, as if a
        # previous retry round already proved the split untrustworthy.
        from ouroboros.harness.decomposition_attestation import (
            DecompositionAttestation,
            DecompositionTrustAxis,
        )

        executor._decomposition_attestations[node_identity.node_id] = DecompositionAttestation(
            node_id=node_identity.node_id,
            verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
            failed_axis=DecompositionTrustAxis.PARENT_GATE,
            failed_sibling_id=None,
            reason="prior round clobbered the workspace",
        )

        captured_trust_flags: list[bool] = []

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            captured_trust_flags.append(bool(kwargs["decomposition_trustworthy"]))
            return _child_result(
                0,
                verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail=""),
            )

        executor._execute_single_ac = AsyncMock(side_effect=fake_execute_single_ac)

        await executor._execute_decomposition_children(
            decision=decision,
            ac_index=0,
            ac_content="parent AC",
            session_id="s1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            seed_goal="goal",
            depth=0,
            execution_id="exec-1",
            level_contexts=None,
            retry_attempt=1,
            execution_counters=None,
            node_identity=node_identity,
            start_time=datetime.now(UTC),
            semantic_ac_key="key",
            ac_spec=AcceptanceCriterionSpec(description="parent AC", verify_command="pytest -q"),
        )

        assert decision.trustworthy is True  # the fresh decision itself looked fine...
        assert captured_trust_flags  # ...but every child was dispatched with:
        assert all(flag is False for flag in captured_trust_flags)  # trust withheld


class TestLiveDecompositionAttestationEndToEnd:
    """Fix 1 (round 3, BLOCKING): a decomposition child is only ever handed
    the PARENT's contract verbatim (see the ``ac_spec=ac_spec`` forward in
    ``_execute_decomposition_children`` and the ``verify_gate_active``
    comment in ``_execute_atomic_ac``). Running that parent-wide contract
    once per successful child, IN ADDITION to the single authoritative
    re-run over the union of all children in ``_attest_decomposition_round``,
    was both a cost bug (N+1 non-idempotent command executions) and a
    correctness bug: a child passing the PARENT's whole contract in
    isolation is not evidence that child did its own job correctly. As of
    round 3, children never execute the borrowed contract at all; a child's
    OWN evidence comes only from the per-child artifact-slice oracle (see
    ``TestPerChildArtifactSliceOracle``). The rounds in THIS class carry no
    child artifact slices, so their sibling axis stays un-evaluable -- these
    tests dispatch every child through the REAL ``_execute_single_ac`` ->
    ``_execute_atomic_ac`` code path (a stub *runtime adapter* is the only
    double) to prove that, and that the PARENT's own gate is still evaluated
    exactly once, for real, regardless of the sibling axis.
    """

    @pytest.mark.asyncio
    async def test_live_decomposed_children_never_run_borrowed_parent_gate(self, tmp_path) -> None:
        from datetime import UTC, datetime

        from tests.unit.orchestrator.test_parallel_executor import (
            _FinalMessageRuntime,
            _make_replaying_event_store,
        )

        # The parent AC's contract is a pure filesystem check the workspace
        # already satisfies -- deliberately side-effect-free (no shell
        # command) so this test proves the wiring itself rather than relying
        # on the stub runtime performing real file writes.
        (tmp_path / "out.txt").write_text("done", encoding="utf-8")

        event_store, _appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "[TASK_COMPLETE]",
            native_session_id="live-decomp-child",
            cwd=str(tmp_path),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            task_cwd=str(tmp_path),
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        executor._emit_workflow_progress = AsyncMock()
        executor._emit_level_started = AsyncMock()
        executor._emit_level_completed = AsyncMock()
        executor._emit_subtask_event = AsyncMock()
        executor._event_emitter.emit_decomposition_attested = AsyncMock()

        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-live", ac_index=0)
        decision = _trustworthy_split_decision(node_identity.node_id, child_count=2)
        ac_spec = AcceptanceCriterionSpec(description="parent AC", expected_artifacts=("out.txt",))

        result = await executor._execute_decomposition_children(
            decision=decision,
            ac_index=0,
            ac_content="parent AC",
            session_id="s1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            seed_goal="goal",
            depth=0,
            execution_id="exec-live",
            level_contexts=None,
            retry_attempt=0,
            execution_counters=None,
            node_identity=node_identity,
            start_time=datetime.now(UTC),
            semantic_ac_key="key",
            ac_spec=ac_spec,
        )

        # No sibling ever runs the borrowed parent-wide gate: it is not
        # evidence of THAT child's own correctness, and running it per child
        # would duplicate the (possibly non-idempotent) verify_command.
        assert len(result.sub_results) == 2
        for sub_result in result.sub_results:
            assert sub_result.verify_gate_outcome is None

        # Fail-closed: with no evaluable per-sibling gate, the round is
        # INDETERMINATE (never optimistically TRUSTWORTHY) via the sibling axis.
        assert result.decomposition_attestation is not None
        assert result.decomposition_attestation.verdict is DecompositionTrustVerdict.INDETERMINATE
        assert result.decomposition_attestation.failed_axis is DecompositionTrustAxis.SIBLING_GATE
        assert result.decomposition_attestation.trustworthy is False

        # The PARENT's own gate is still evaluated for real, exactly once,
        # after all children finished -- and cached on the returned result so
        # the final acceptance gate does not re-run it a second time.
        assert result.verify_gate_outcome is not None
        assert result.verify_gate_outcome.passed is True

    @pytest.mark.asyncio
    async def test_live_missing_parent_artifact_is_untrustworthy_via_parent_axis(
        self, tmp_path
    ) -> None:
        """No sibling ever ran the borrowed parent-wide contract (their axis
        stays unevaluable), but the PARENT's own re-run gate genuinely ran
        and FAILED -- real, evaluated negative evidence. Under the
        evaluate-all-then-prioritize-failure ordering that failure wins the
        verdict as UNTRUSTWORTHY on the PARENT axis (with no sibling blamed,
        since none was actually checked) instead of being masked into
        INDETERMINATE by the siblings' absence of evidence."""
        from datetime import UTC, datetime

        from tests.unit.orchestrator.test_parallel_executor import (
            _FinalMessageRuntime,
            _make_replaying_event_store,
        )

        # Deliberately do NOT create out.txt: the contract is unmet.
        event_store, _appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "[TASK_COMPLETE]",
            native_session_id="live-decomp-child-missing",
            cwd=str(tmp_path),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            task_cwd=str(tmp_path),
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        executor._emit_workflow_progress = AsyncMock()
        executor._emit_level_started = AsyncMock()
        executor._emit_level_completed = AsyncMock()
        executor._emit_subtask_event = AsyncMock()
        executor._event_emitter.emit_decomposition_attested = AsyncMock()

        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-live-2", ac_index=0)
        decision = _trustworthy_split_decision(node_identity.node_id, child_count=2)
        ac_spec = AcceptanceCriterionSpec(description="parent AC", expected_artifacts=("out.txt",))

        result = await executor._execute_decomposition_children(
            decision=decision,
            ac_index=0,
            ac_content="parent AC",
            session_id="s1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            seed_goal="goal",
            depth=0,
            execution_id="exec-live-2",
            level_contexts=None,
            retry_attempt=0,
            execution_counters=None,
            node_identity=node_identity,
            start_time=datetime.now(UTC),
            semantic_ac_key="key",
            ac_spec=ac_spec,
        )

        assert result.sub_results[0].verify_gate_outcome is None
        assert result.decomposition_attestation is not None
        assert result.decomposition_attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert result.decomposition_attestation.failed_axis is DecompositionTrustAxis.PARENT_GATE
        assert result.decomposition_attestation.failed_sibling_id is None
        assert result.decomposition_attestation.trustworthy is False

        # The PARENT's own gate still genuinely ran once and genuinely failed.
        assert result.verify_gate_outcome is not None
        assert result.verify_gate_outcome.passed is False


def _artifact_split_decision(
    node_id: str,
    slices: tuple[tuple[str, ...], ...],
) -> DecompositionDecisionRecord:
    """A structurally trustworthy split whose children carry artifact slices."""
    children = tuple(
        DecompositionChild(
            description=f"child {i}",
            coverage_claims=(f"distinct scope {i}",),
            verification_hint=f"check {i}",
            expected_artifacts=slice_,
        )
        for i, slice_ in enumerate(slices)
    )
    return DecompositionDecisionRecord(
        node_id=node_id,
        source=DecompositionSource.PREFLIGHT,
        disposition=DecompositionDisposition.SPLIT,
        children=children,
        structural_status=StructuralCheckStatus.PASSED,
        semantic_status=SemanticAttestationStatus.ESTABLISHED,
        trustworthy=True,
    )


def _make_live_artifact_executor(
    tmp_path,
    writes_per_dispatch: list[tuple[str, ...]],
    dispatch_success: list[bool] | None = None,
):
    """Build an executor whose stub RUNTIME adapter performs REAL file writes.

    Only the runtime transport is a double; children dispatch through the real
    ``_execute_single_ac`` -> ``_execute_atomic_ac`` path and every artifact
    existence check runs against the real filesystem under ``tmp_path``. Each
    sequential dispatch pops the next tuple of relative paths and creates
    those files -- modeling what that child's agent session actually produced.
    ``dispatch_success`` optionally scripts, per sequential dispatch, whether
    the runtime reports success (``[TASK_COMPLETE]``/subtype ``success``) or a
    late failure AFTER the file writes already happened (no completion marker,
    subtype ``error``) -- modeling a child that wrote its artifacts as an
    early step and then failed.
    """
    from tests.unit.orchestrator.test_parallel_executor import (
        _FinalMessageRuntime,
        _make_replaying_event_store,
    )

    class _ArtifactWritingRuntime(_FinalMessageRuntime):
        async def execute_task(self, *args: object, **kwargs: object):
            if writes_per_dispatch:
                for relative_path in writes_per_dispatch.pop(0):
                    target = tmp_path / relative_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("produced", encoding="utf-8")
            if dispatch_success:
                self._success = dispatch_success.pop(0)
                self._final_message = (
                    "[TASK_COMPLETE]" if self._success else "hit an error after writing files"
                )
            async for message in super().execute_task(*args, **kwargs):
                yield message

    event_store, _appended = _make_replaying_event_store()
    runtime = _ArtifactWritingRuntime(
        "[TASK_COMPLETE]",
        native_session_id="live-artifact-child",
        cwd=str(tmp_path),
    )
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=event_store,
        console=MagicMock(),
        enable_decomposition=False,
        task_cwd=str(tmp_path),
    )
    executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
    executor._emit_workflow_progress = AsyncMock()
    executor._emit_level_started = AsyncMock()
    executor._emit_level_completed = AsyncMock()
    executor._emit_subtask_event = AsyncMock()
    executor._event_emitter.emit_decomposition_attested = AsyncMock()
    executor._test_appended_events = _appended  # type: ignore[attr-defined]
    return executor


def _decomposition_children_kwargs(node_identity, decision, ac_spec, execution_id: str):
    from datetime import UTC, datetime

    return {
        "decision": decision,
        "ac_index": 0,
        "ac_content": "parent AC",
        "session_id": "s1",
        "tools": [],
        "tool_catalog": None,
        "system_prompt": "",
        "seed_goal": "goal",
        "depth": 0,
        "execution_id": execution_id,
        "level_contexts": None,
        "retry_attempt": 0,
        "execution_counters": None,
        "node_identity": node_identity,
        "start_time": datetime.now(UTC),
        "semantic_ac_key": "key",
        "ac_spec": ac_spec,
    }


class TestPerChildArtifactSliceOracle:
    """The per-child-local contract that makes TRUSTWORTHY honestly reachable:
    each child is graded against ITS OWN assigned slice of the PARENT's
    seed-authored expected_artifacts, via real filesystem existence checks
    snapshotted before dispatch and re-checked after -- no LLM self-grading,
    and never the parent's full verify gate re-run per child."""

    PARENT_ARTIFACTS = ("out_a.txt", "out_b.txt")

    def _spec(self) -> AcceptanceCriterionSpec:
        return AcceptanceCriterionSpec(
            description="parent AC", expected_artifacts=self.PARENT_ARTIFACTS
        )

    @pytest.mark.asyncio
    async def test_children_creating_their_slices_earn_trustworthy(self, tmp_path) -> None:
        """The whole point of the feature: a real decomposition round where
        every child genuinely creates its assigned artifacts (and the parent's
        re-run gate passes) earns TRUSTWORTHY -- without any child ever
        running the parent's borrowed verify gate (Fix 1 stays intact)."""
        executor = _make_live_artifact_executor(
            tmp_path, writes_per_dispatch=[("out_a.txt",), ("out_b.txt",)]
        )
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-art-1", ac_index=0)
        decision = _artifact_split_decision(
            node_identity.node_id, slices=(("out_a.txt",), ("out_b.txt",))
        )

        result = await executor._execute_decomposition_children(
            **_decomposition_children_kwargs(node_identity, decision, self._spec(), "exec-art-1")
        )

        # Fix 1 intact: no child ever ran the borrowed parent-wide gate.
        for sub_result in result.sub_results:
            assert sub_result.verify_gate_outcome is None

        assert result.decomposition_attestation is not None
        assert result.decomposition_attestation.verdict is DecompositionTrustVerdict.TRUSTWORTHY
        assert result.decomposition_attestation.trustworthy is True
        # The parent's own artifact gate also genuinely re-ran and passed.
        assert result.verify_gate_outcome is not None
        assert result.verify_gate_outcome.passed is True

        capsule_events = [
            event
            for event in executor._test_appended_events  # type: ignore[attr-defined]
            if event.type == "execution.ac.capsule.compiled"
        ]

        def _contract_digest(artifacts: tuple[str, ...]) -> str:
            payload = ACSuccessContract(expected_artifacts=artifacts).to_contract_data()
            canonical = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            return "sha256:" + hashlib.sha256(canonical).hexdigest()

        assert [
            event.data["capsule_manifest"]["success_contract_digest"] for event in capsule_events
        ] == [
            _contract_digest(("out_a.txt",)),
            _contract_digest(("out_b.txt",)),
        ]

    @pytest.mark.asyncio
    async def test_preexisting_slice_denies_credit_fail_closed(self, tmp_path) -> None:
        """Safety invariant 1 (credit-borrowing): if ALL of a child's assigned
        artifacts already existed before it dispatched, that child's axis is
        un-evaluable -- it must never be graded as passed, so the round stays
        INDETERMINATE (not TRUSTWORTHY)."""
        (tmp_path / "out_a.txt").write_text("already here", encoding="utf-8")
        executor = _make_live_artifact_executor(tmp_path, writes_per_dispatch=[(), ("out_b.txt",)])
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-art-2", ac_index=0)
        decision = _artifact_split_decision(
            node_identity.node_id, slices=(("out_a.txt",), ("out_b.txt",))
        )

        result = await executor._execute_decomposition_children(
            **_decomposition_children_kwargs(node_identity, decision, self._spec(), "exec-art-2")
        )

        attestation = result.decomposition_attestation
        assert attestation is not None
        assert attestation.verdict is DecompositionTrustVerdict.INDETERMINATE
        assert attestation.trustworthy is False
        assert attestation.failed_axis is DecompositionTrustAxis.SIBLING_GATE
        assert attestation.failed_sibling_id == "0"

    @pytest.mark.asyncio
    async def test_partial_preexisting_slice_denies_credit_fail_closed(
        self, tmp_path, monkeypatch
    ) -> None:
        """Safety invariant 1, partial-contamination leg (live regression for
        the count-based snapshot bug): a child assigned a MULTI-artifact slice
        where only ONE path already existed before dispatch -- and the child
        genuinely creates the rest -- must NOT earn credit for its slice. A
        count-based snapshot ('deny only when ALL pre-existed') would grade
        this child passed=True and launder the round into TRUSTWORTHY, which
        is the false-positive the fail-closed mandate forbids. The realistic
        trigger is a retry attempt on the same un-reset workspace whose
        earlier children left artifacts behind. The child's sibling axis must
        come back un-evaluable (has_verify_command=False, passed=None), never
        passed=True."""
        import ouroboros.orchestrator.parallel_executor as pe

        (tmp_path / "out_a.txt").write_text("leftover from a prior attempt", encoding="utf-8")
        # Child 0 is assigned (out_a, out_b): out_a pre-exists, and the child
        # genuinely creates out_b. Child 1 genuinely creates out_c.
        executor = _make_live_artifact_executor(
            tmp_path, writes_per_dispatch=[("out_b.txt",), ("out_c.txt",)]
        )
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-art-7", ac_index=0)
        decision = _artifact_split_decision(
            node_identity.node_id,
            slices=(("out_a.txt", "out_b.txt"), ("out_c.txt",)),
        )
        spec = AcceptanceCriterionSpec(
            description="parent AC",
            expected_artifacts=("out_a.txt", "out_b.txt", "out_c.txt"),
        )

        # Pass-through spy on the real attestation judge so the test can
        # assert the exact sibling-axis shape handed to it (nothing mocked:
        # the real function still computes the real verdict).
        real_attest = pe.attest_decomposition
        captured_siblings: list[tuple[SiblingVerifyOutcome, ...]] = []

        def _capturing_attest(*, node_id, siblings, parent):
            captured_siblings.append(siblings)
            return real_attest(node_id=node_id, siblings=siblings, parent=parent)

        monkeypatch.setattr(pe, "attest_decomposition", _capturing_attest)

        result = await executor._execute_decomposition_children(
            **_decomposition_children_kwargs(node_identity, decision, spec, "exec-art-7")
        )

        # The contaminated child's axis is un-evaluable -- NOT passed=True.
        assert captured_siblings
        contaminated_axis = captured_siblings[-1][0]
        assert contaminated_axis.has_verify_command is False
        assert contaminated_axis.passed is None
        assert "out_a.txt" in contaminated_axis.reason
        # The sibling with a genuinely fresh slice still earns real evidence.
        clean_axis = captured_siblings[-1][1]
        assert clean_axis.has_verify_command is True
        assert clean_axis.passed is True

        attestation = result.decomposition_attestation
        assert attestation is not None
        assert attestation.verdict is DecompositionTrustVerdict.INDETERMINATE
        assert attestation.trustworthy is False
        assert attestation.failed_axis is DecompositionTrustAxis.SIBLING_GATE
        assert attestation.failed_sibling_id == "0"

    @pytest.mark.asyncio
    async def test_child_missing_its_slice_makes_round_untrustworthy(self, tmp_path) -> None:
        """A child that finishes without its assigned artifacts existing is
        REAL, evaluated negative evidence: the round is UNTRUSTWORTHY and the
        failure is attributed to that child."""
        executor = _make_live_artifact_executor(tmp_path, writes_per_dispatch=[(), ("out_b.txt",)])
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-art-3", ac_index=0)
        decision = _artifact_split_decision(
            node_identity.node_id, slices=(("out_a.txt",), ("out_b.txt",))
        )

        result = await executor._execute_decomposition_children(
            **_decomposition_children_kwargs(node_identity, decision, self._spec(), "exec-art-3")
        )

        attestation = result.decomposition_attestation
        assert attestation is not None
        assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert attestation.failed_axis is DecompositionTrustAxis.SIBLING_GATE
        assert attestation.failed_sibling_id == "0"
        assert "out_a.txt" in attestation.reason

    @pytest.mark.asyncio
    async def test_failed_child_with_artifacts_on_disk_is_untrustworthy(
        self, tmp_path, monkeypatch
    ) -> None:
        """Round-11 Finding #3 regression (BLOCKING): a child that writes its
        assigned artifacts as an EARLY step of its own dispatch and then fails
        later (dispatch reports success=False) must NOT earn artifact-axis
        credit just because the files exist on disk. Before the fix, the
        post-dispatch check consulted only file existence, graded this child
        passed=True, and -- with the parent's own artifact gate also passing
        over the files on disk -- laundered a round containing a GENUINELY
        FAILED child into a false TRUSTWORTHY verdict. The failed dispatch is
        real, evaluated negative evidence: the axis must be passed=False (not
        the unevaluable False/None shape) and the round UNTRUSTWORTHY."""
        import ouroboros.orchestrator.parallel_executor as pe

        # Child 0 writes out_a.txt but its dispatch FAILS afterwards; child 1
        # genuinely creates out_b.txt and succeeds. Every parent artifact is
        # on disk at the end, so the parent-gate re-run passes -- exactly the
        # combination that used to mint the false TRUSTWORTHY.
        executor = _make_live_artifact_executor(
            tmp_path,
            writes_per_dispatch=[("out_a.txt",), ("out_b.txt",)],
            dispatch_success=[False, True],
        )
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-art-8", ac_index=0)
        decision = _artifact_split_decision(
            node_identity.node_id, slices=(("out_a.txt",), ("out_b.txt",))
        )

        real_attest = pe.attest_decomposition
        captured_siblings: list[tuple[SiblingVerifyOutcome, ...]] = []

        def _capturing_attest(*, node_id, siblings, parent):
            captured_siblings.append(siblings)
            return real_attest(node_id=node_id, siblings=siblings, parent=parent)

        monkeypatch.setattr(pe, "attest_decomposition", _capturing_attest)

        result = await executor._execute_decomposition_children(
            **_decomposition_children_kwargs(node_identity, decision, self._spec(), "exec-art-8")
        )

        # Sanity: the failed child's own dispatch genuinely reported failure,
        # and its assigned artifact genuinely exists on disk.
        assert result.sub_results[0].success is False
        assert (tmp_path / "out_a.txt").exists()

        # The failed child's artifact axis is real NEGATIVE evidence, never a
        # files-on-disk pass and never merely unevaluable.
        assert captured_siblings
        failed_axis = captured_siblings[-1][0]
        assert failed_axis.has_verify_command is True
        assert failed_axis.passed is False
        assert "dispatch reported failure" in failed_axis.reason
        # The genuinely successful sibling keeps its earned credit.
        clean_axis = captured_siblings[-1][1]
        assert clean_axis.passed is True

        attestation = result.decomposition_attestation
        assert attestation is not None
        assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert attestation.trustworthy is False
        assert attestation.failed_axis is DecompositionTrustAxis.SIBLING_GATE
        assert attestation.failed_sibling_id == "0"

    @pytest.mark.asyncio
    async def test_slice_outside_parent_contract_is_never_credited(self, tmp_path) -> None:
        """Safety invariant 2 (runtime leg, defense in depth on top of the
        proposal-time subset validation): a child slice path that is NOT part
        of the parent's seed-authored set is silently un-creditable -- it can
        never become passing evidence, so the round cannot reach TRUSTWORTHY
        on a self-invented oracle."""
        executor = _make_live_artifact_executor(
            tmp_path,
            writes_per_dispatch=[("invented.txt",), ("out_a.txt", "out_b.txt")],
        )
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-art-4", ac_index=0)
        # Child 0 claims a path the parent never declared.
        decision = _artifact_split_decision(
            node_identity.node_id, slices=(("invented.txt",), ("out_a.txt", "out_b.txt"))
        )

        result = await executor._execute_decomposition_children(
            **_decomposition_children_kwargs(node_identity, decision, self._spec(), "exec-art-4")
        )

        attestation = result.decomposition_attestation
        assert attestation is not None
        # Child 0 has no evaluable (parent-authored) slice left after the
        # runtime subset filter -> its axis is indeterminate, never passed.
        assert attestation.verdict is DecompositionTrustVerdict.INDETERMINATE
        assert attestation.failed_axis is DecompositionTrustAxis.SIBLING_GATE
        assert attestation.failed_sibling_id == "0"

    @pytest.mark.asyncio
    async def test_no_parent_artifacts_keeps_prior_behavior(self, tmp_path) -> None:
        """Safety invariant 4 (regression): when the parent declares NO
        expected_artifacts, none of the new machinery activates -- even if a
        (malformed) decision carries child slices -- and the round resolves
        INDETERMINATE exactly as before this feature."""
        executor = _make_live_artifact_executor(
            tmp_path, writes_per_dispatch=[("whatever.txt",), ()]
        )
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-art-5", ac_index=0)
        decision = _artifact_split_decision(node_identity.node_id, slices=(("whatever.txt",), ()))
        spec_without_artifacts = AcceptanceCriterionSpec(description="parent AC")

        result = await executor._execute_decomposition_children(
            **_decomposition_children_kwargs(
                node_identity, decision, spec_without_artifacts, "exec-art-5"
            )
        )

        attestation = result.decomposition_attestation
        assert attestation is not None
        assert attestation.verdict is DecompositionTrustVerdict.INDETERMINATE
        assert attestation.trustworthy is False
        assert result.verify_gate_outcome is None
        for sub_result in result.sub_results:
            assert sub_result.verify_gate_outcome is None

    @pytest.mark.asyncio
    async def test_run_verify_commands_disabled_skips_artifact_checks(
        self, tmp_path, monkeypatch
    ) -> None:
        """Safety invariant 6: artifact existence checks follow the SAME
        ``self._run_verify_commands`` posture as every other success-contract
        evaluation site (the parent's own artifact-only gate included) -- an
        operator who disabled contract execution gets no filesystem oracle
        runs and a fail-closed INDETERMINATE round."""
        import ouroboros.orchestrator.parallel_executor as pe

        executor = _make_live_artifact_executor(
            tmp_path, writes_per_dispatch=[("out_a.txt",), ("out_b.txt",)]
        )
        executor._run_verify_commands = False

        def _must_not_run(artifacts, cwd):  # pragma: no cover - failure path
            msg = "artifact existence oracle must not run when verify commands are disabled"
            raise AssertionError(msg)

        monkeypatch.setattr(pe, "_missing_expected_artifacts", _must_not_run)

        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-art-6", ac_index=0)
        decision = _artifact_split_decision(
            node_identity.node_id, slices=(("out_a.txt",), ("out_b.txt",))
        )

        result = await executor._execute_decomposition_children(
            **_decomposition_children_kwargs(node_identity, decision, self._spec(), "exec-art-6")
        )

        attestation = result.decomposition_attestation
        assert attestation is not None
        assert attestation.verdict is DecompositionTrustVerdict.INDETERMINATE
        assert attestation.trustworthy is False
        assert result.verify_gate_outcome is None

    @pytest.mark.asyncio
    async def test_conflicting_artifact_and_gate_evidence_fails_closed(self) -> None:
        """Merge rule: when a sibling somehow carries BOTH artifact-slice
        evidence and a per-child verify-gate outcome, a failure on EITHER leg
        fails that sibling -- a conflicting signal never resolves to passed."""
        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-art-7", ac_index=0)
        final_sub_results = [
            _child_result(
                0,
                verify_gate_outcome=_VerifyGateOutcome(
                    passed=False, reason="gate says no", output_tail=""
                ),
            ),
            _child_result(1, verify_gate_outcome=None),
        ]
        ac_spec = AcceptanceCriterionSpec(description="parent AC", verify_command="true")

        attestation, _parent = await executor._attest_decomposition_round(
            node_identity=node_identity,
            final_sub_results=final_sub_results,
            ac_spec=ac_spec,
            child_artifact_attributions=(
                (True, True, "artifact leg passed"),  # conflicts with failing gate leg
                (True, True, "artifact leg passed"),
            ),
        )

        assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert attestation.failed_sibling_id == "0"


class TestDecompositionAttestationReplayOnResume:
    """Fix 2 (round 2, BLOCKING): a fresh executor (empty in-memory cache,
    as happens after a resume/restart) must reconstruct a PRIOR round's
    UNTRUSTWORTHY verdict from the durable ``execution.ac.decomposition_attested``
    event log rather than silently forgetting it and re-authorizing the
    cheap child-tier discount."""

    @pytest.mark.asyncio
    async def test_fresh_executor_replays_prior_untrustworthy_verdict_from_event_store(
        self,
    ) -> None:
        from datetime import UTC, datetime

        from ouroboros.events.base import BaseEvent
        from ouroboros.harness.decomposition_attestation import (
            DecompositionAttestation,
            DecompositionTrustAxis,
        )
        from tests.unit.orchestrator.test_parallel_executor import (
            _make_replaying_event_store,
        )

        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-resume", ac_index=0)

        # Simulate a PRIOR process having already recorded an UNTRUSTWORTHY
        # verdict for this exact root AC and persisted it as a durable event
        # -- exactly what ``ExecutionEventEmitter.emit_decomposition_attested``
        # writes.
        prior_attestation = DecompositionAttestation(
            node_id=node_identity.node_id,
            verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
            failed_axis=DecompositionTrustAxis.PARENT_GATE,
            failed_sibling_id=None,
            reason="prior process: parent gate failed after decomposition",
        )
        event_store, appended_events = _make_replaying_event_store()
        appended_events.append(
            BaseEvent(
                type="execution.ac.decomposition_attested",
                aggregate_type="execution",
                aggregate_id="exec-resume",
                data={
                    **node_identity.to_event_metadata(),
                    **prior_attestation.to_event_data(),
                    "execution_id": "exec-resume",
                    "session_id": "s1",
                },
            )
        )

        # A brand-new executor -- as a resumed process would construct --
        # with NOTHING in its in-memory ``_decomposition_attestations`` cache.
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        assert executor._decomposition_attestations == {}
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        executor._emit_workflow_progress = AsyncMock()
        executor._emit_level_started = AsyncMock()
        executor._emit_level_completed = AsyncMock()
        executor._emit_subtask_event = AsyncMock()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        executor._event_emitter.emit_decomposition_attested = AsyncMock()

        decision = _trustworthy_split_decision(node_identity.node_id)

        captured_trust_flags: list[bool] = []

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            captured_trust_flags.append(bool(kwargs["decomposition_trustworthy"]))
            return _child_result(
                0,
                verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail=""),
            )

        executor._execute_single_ac = AsyncMock(side_effect=fake_execute_single_ac)

        await executor._execute_decomposition_children(
            decision=decision,
            ac_index=0,
            ac_content="parent AC",
            session_id="s1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            seed_goal="goal",
            depth=0,
            execution_id="exec-resume",
            level_contexts=None,
            retry_attempt=1,
            execution_counters=None,
            node_identity=node_identity,
            start_time=datetime.now(UTC),
            semantic_ac_key="key",
            ac_spec=AcceptanceCriterionSpec(description="parent AC", verify_command="pytest -q"),
        )

        # The fresh decision itself looks fine (trustworthy=True in isolation)...
        assert decision.trustworthy is True
        # ...but the REPLAYED prior verdict must still withhold trust for
        # every child, proving it was reconstructed from the durable event
        # log rather than the (empty) in-memory cache. (The cache now holds
        # THIS round's own freshly-computed verdict, since the round that
        # just ran overwrites it -- exactly like the pre-existing in-memory
        # behavior; what this test proves is the trust-withholding DECISION
        # made *before* that round ran, which is captured in
        # ``captured_trust_flags``.)
        assert captured_trust_flags
        assert all(flag is False for flag in captured_trust_flags)

    @pytest.mark.asyncio
    async def test_fresh_executor_with_no_prior_event_defaults_to_none(self) -> None:
        """No matching durable event for this node id -> no prior attestation
        (identical to today's in-memory ``None`` default), not an error and
        not an optimistic trustworthy default."""
        from tests.unit.orchestrator.test_parallel_executor import (
            _make_replaying_event_store,
        )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._load_decomposition_attestation(
            "node-never-seen", execution_id="exec-none", session_id="s1"
        )

        assert result is None
        assert "node-never-seen" not in executor._decomposition_attestations


class TestAttestationReplaySelectsHighestRetryAttempt:
    """Adversarial-review Bug #4 (TOCTOU race on the deferred backfill):
    round N's failed attestation write gets a deferred background backfill;
    round N+1 (same node id — stable across retries) computes a different
    verdict and its foreground write lands FIRST; the backfill's pre-write
    supersession check passed before N+1 attested, but its actual write ran
    through ``_safe_emit_event``'s multi-second retry window and landed
    SECOND. With raw last-write-wins BY LOG ORDER, replay then resurrected
    round N's stale verdict — potentially re-authorizing a discount the
    newer, correct verdict had foreclosed. The loader must select by the
    HIGHEST persisted ``retry_attempt`` among matching events, tie-breaking
    (and legacy events without the field) on log order."""

    @staticmethod
    def _executor_with_store():
        from tests.unit.orchestrator.test_parallel_executor import (
            _make_replaying_event_store,
        )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        return executor, appended_events

    @staticmethod
    def _attested_event(node_identity, attestation, *, retry_attempt=None):
        from ouroboros.events.base import BaseEvent

        data = {
            **node_identity.to_event_metadata(),
            **attestation.to_event_data(),
            "execution_id": "exec-race",
            "session_id": "s1",
        }
        if retry_attempt is not None:
            data["retry_attempt"] = retry_attempt
        return BaseEvent(
            type="execution.ac.decomposition_attested",
            aggregate_type="execution",
            aggregate_id="exec-race",
            data=data,
        )

    @pytest.mark.asyncio
    async def test_out_of_order_stale_backfill_does_not_resurrect_older_verdict(
        self,
    ) -> None:
        """The exact race outcome, constructed directly: the OLDER (lower
        retry_attempt) event lands in the log AFTER the NEWER (higher
        retry_attempt) one. Replay must still return the newer attestation —
        never re-authorize the trust the newer round foreclosed."""
        from ouroboros.harness.decomposition_attestation import DecompositionAttestation

        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-race", ac_index=0)
        newer = DecompositionAttestation(
            node_id=node_identity.node_id,
            verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
            failed_axis=DecompositionTrustAxis.PARENT_GATE,
            failed_sibling_id=None,
            reason="round N+1: parent gate failed after decomposition",
        )
        stale = DecompositionAttestation(
            node_id=node_identity.node_id,
            verdict=DecompositionTrustVerdict.TRUSTWORTHY,
            failed_axis=None,
            failed_sibling_id=None,
            reason="round N: all axes passed (stale, superseded by round N+1)",
        )
        executor, appended_events = self._executor_with_store()
        # Round N+1's foreground write landed FIRST...
        appended_events.append(self._attested_event(node_identity, newer, retry_attempt=2))
        # ...then round N's deferred backfill finally landed, OUT of order.
        appended_events.append(self._attested_event(node_identity, stale, retry_attempt=1))

        loaded = await executor._load_decomposition_attestation(
            node_identity.node_id, execution_id="exec-race", session_id="s1"
        )

        assert loaded is not None
        assert loaded.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert loaded.reason == "round N+1: parent gate failed after decomposition"

    @pytest.mark.asyncio
    async def test_in_order_log_still_returns_latest_round(self) -> None:
        """Control: the ordinary no-race log (attempts in log order) keeps
        returning the latest round exactly as before."""
        from ouroboros.harness.decomposition_attestation import DecompositionAttestation

        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-race", ac_index=0)
        older = DecompositionAttestation(
            node_id=node_identity.node_id,
            verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
            failed_axis=DecompositionTrustAxis.PARENT_GATE,
            failed_sibling_id=None,
            reason="round N failed",
        )
        newer = DecompositionAttestation(
            node_id=node_identity.node_id,
            verdict=DecompositionTrustVerdict.TRUSTWORTHY,
            failed_axis=None,
            failed_sibling_id=None,
            reason="round N+1 passed",
        )
        executor, appended_events = self._executor_with_store()
        appended_events.append(self._attested_event(node_identity, older, retry_attempt=1))
        appended_events.append(self._attested_event(node_identity, newer, retry_attempt=2))

        loaded = await executor._load_decomposition_attestation(
            node_identity.node_id, execution_id="exec-race", session_id="s1"
        )

        assert loaded is not None
        assert loaded.verdict is DecompositionTrustVerdict.TRUSTWORTHY
        assert loaded.reason == "round N+1 passed"

    @pytest.mark.asyncio
    async def test_legacy_events_without_retry_attempt_keep_log_order(self) -> None:
        """Durable records written before ``retry_attempt`` was persisted
        rank equally, so among themselves plain log order still wins — the
        exact pre-fix posture, never worse."""
        from ouroboros.harness.decomposition_attestation import DecompositionAttestation

        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-race", ac_index=0)
        first = DecompositionAttestation(
            node_id=node_identity.node_id,
            verdict=DecompositionTrustVerdict.TRUSTWORTHY,
            failed_axis=None,
            failed_sibling_id=None,
            reason="legacy first",
        )
        second = DecompositionAttestation(
            node_id=node_identity.node_id,
            verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
            failed_axis=DecompositionTrustAxis.PARENT_GATE,
            failed_sibling_id=None,
            reason="legacy second",
        )
        executor, appended_events = self._executor_with_store()
        appended_events.append(self._attested_event(node_identity, first))
        appended_events.append(self._attested_event(node_identity, second))

        loaded = await executor._load_decomposition_attestation(
            node_identity.node_id, execution_id="exec-race", session_id="s1"
        )

        assert loaded is not None
        assert loaded.reason == "legacy second"

    @pytest.mark.asyncio
    async def test_versioned_event_outranks_legacy_event_landing_after_it(self) -> None:
        """An event that DOES carry ``retry_attempt`` outranks a legacy
        (field-less) record regardless of where each sits in the log."""
        from ouroboros.harness.decomposition_attestation import DecompositionAttestation

        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-race", ac_index=0)
        versioned = DecompositionAttestation(
            node_id=node_identity.node_id,
            verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
            failed_axis=DecompositionTrustAxis.PARENT_GATE,
            failed_sibling_id=None,
            reason="versioned record",
        )
        legacy = DecompositionAttestation(
            node_id=node_identity.node_id,
            verdict=DecompositionTrustVerdict.TRUSTWORTHY,
            failed_axis=None,
            failed_sibling_id=None,
            reason="legacy record",
        )
        executor, appended_events = self._executor_with_store()
        appended_events.append(self._attested_event(node_identity, versioned, retry_attempt=0))
        appended_events.append(self._attested_event(node_identity, legacy))

        loaded = await executor._load_decomposition_attestation(
            node_identity.node_id, execution_id="exec-race", session_id="s1"
        )

        assert loaded is not None
        assert loaded.reason == "versioned record"


class TestDecompositionAttestationFailsClosedOnReplayFailure:
    """Fix 5 (round 3, BLOCKING): a genuine READ failure (every
    ``_replay_with_retry`` attempt raises) must NOT be silently treated the
    same as "no prior attestation was ever recorded" -- that is fail-OPEN and
    would let a restart re-authorize a cheap child tier this exact node id
    was already proven untrustworthy for. ``_load_decomposition_attestation``
    must fail closed with a synthetic UNTRUSTWORTHY verdict instead of
    falling through to the legitimate-miss ``None`` path.
    """

    @pytest.mark.asyncio
    async def test_replay_exception_fails_closed_to_untrustworthy(self) -> None:
        from unittest.mock import patch

        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._event_store.replay = AsyncMock(side_effect=RuntimeError("db unavailable"))

        with patch("ouroboros.orchestrator.parallel_executor.anyio.sleep", AsyncMock()):
            attestation = await executor._load_decomposition_attestation(
                "node-x", execution_id="exec-broken", session_id="s1"
            )

        assert attestation is not None
        assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert attestation.trustworthy is False
        # Fail-closed default is NOT cached -- a later successful read in the
        # same process must not stay poisoned by a transient failure.
        assert "node-x" not in executor._decomposition_attestations

    @pytest.mark.asyncio
    async def test_replay_exception_withholds_child_tier_discount_on_next_round(self) -> None:
        """End-to-end: the fail-closed attestation must actually withhold the
        child-tier discount for the NEXT decomposition round of this root AC,
        the same way a REAL prior UNTRUSTWORTHY verdict already does."""
        from datetime import UTC, datetime
        from unittest.mock import patch

        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-broken", ac_index=0)
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._event_store.replay = AsyncMock(side_effect=RuntimeError("db unavailable"))
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        executor._emit_workflow_progress = AsyncMock()
        executor._emit_level_started = AsyncMock()
        executor._emit_level_completed = AsyncMock()
        executor._emit_subtask_event = AsyncMock()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        executor._event_emitter.emit_decomposition_attested = AsyncMock()

        decision = _trustworthy_split_decision(node_identity.node_id)
        captured_trust_flags: list[bool] = []

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            captured_trust_flags.append(bool(kwargs["decomposition_trustworthy"]))
            return _child_result(
                0,
                verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail=""),
            )

        executor._execute_single_ac = AsyncMock(side_effect=fake_execute_single_ac)

        with patch("ouroboros.orchestrator.parallel_executor.anyio.sleep", AsyncMock()):
            await executor._execute_decomposition_children(
                decision=decision,
                ac_index=0,
                ac_content="parent AC",
                session_id="s1",
                tools=[],
                tool_catalog=None,
                system_prompt="",
                seed_goal="goal",
                depth=0,
                execution_id="exec-broken",
                level_contexts=None,
                retry_attempt=1,
                execution_counters=None,
                node_identity=node_identity,
                start_time=datetime.now(UTC),
                semantic_ac_key="key",
                ac_spec=AcceptanceCriterionSpec(
                    description="parent AC", verify_command="pytest -q"
                ),
            )

        assert decision.trustworthy is True
        assert captured_trust_flags
        assert all(flag is False for flag in captured_trust_flags)


class TestDecompositionAttestationFailsClosedOnMalformedEventData:
    """Round-4 Finding #4 (BLOCKING): replay SUCCEEDS and a matching
    ``execution.ac.decomposition_attested`` event for this node id IS found,
    but its payload fails to parse into a valid
    :class:`DecompositionAttestation`. Returning ``None`` for that case is
    distinguishable from the legitimate "never attested" bootstrap state so
    corruption remains explicit and loud. The loader must return the SAME
    synthetic fail-closed UNTRUSTWORTHY sentinel as the total-replay-failure
    case; ``None`` stays reserved for "no matching event exists at all".
    """

    @staticmethod
    def _store_with_malformed_attested_event(node_id: str, execution_id: str):
        from ouroboros.events.base import BaseEvent
        from tests.unit.orchestrator.test_parallel_executor import (
            _make_replaying_event_store,
        )

        event_store, appended_events = _make_replaying_event_store()
        appended_events.append(
            BaseEvent(
                type="execution.ac.decomposition_attested",
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    # node_id matches, so the loader FINDS this event -- but
                    # the verdict is not a valid DecompositionTrustVerdict
                    # value and required fields are missing/wrong-typed, so
                    # _decomposition_attestation_from_event_data returns None.
                    "node_id": node_id,
                    "verdict": "corrupted-not-a-verdict",
                    "failed_axis": 42,
                    "execution_id": execution_id,
                    "session_id": "s1",
                },
            )
        )
        return event_store

    @pytest.mark.asyncio
    async def test_malformed_found_event_fails_closed_to_untrustworthy(self) -> None:
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-corrupt", ac_index=0)
        event_store = self._store_with_malformed_attested_event(
            node_identity.node_id, "exec-corrupt"
        )
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        attestation = await executor._load_decomposition_attestation(
            node_identity.node_id, execution_id="exec-corrupt", session_id="s1"
        )

        assert attestation is not None
        assert attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert attestation.trustworthy is False
        # The fail-closed sentinel is NOT cached -- consistent with the
        # total-replay-failure convention.
        assert node_identity.node_id not in executor._decomposition_attestations

    @pytest.mark.asyncio
    async def test_malformed_found_event_withholds_child_tier_discount_on_next_round(
        self,
    ) -> None:
        """Caller-side effect: even though the FRESH proposal is trustworthy
        (``decision.trustworthy is True``), the corrupt prior record must
        force ``child_decomposition_trustworthy`` to ``False`` for this node
        id's next retry -- exactly like a REAL prior UNTRUSTWORTHY verdict."""
        from datetime import UTC, datetime

        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-corrupt2", ac_index=0)
        event_store = self._store_with_malformed_attested_event(
            node_identity.node_id, "exec-corrupt2"
        )
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        executor._emit_workflow_progress = AsyncMock()
        executor._emit_level_started = AsyncMock()
        executor._emit_level_completed = AsyncMock()
        executor._emit_subtask_event = AsyncMock()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        executor._event_emitter.emit_decomposition_attested = AsyncMock()

        decision = _trustworthy_split_decision(node_identity.node_id)
        captured_trust_flags: list[bool] = []

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            captured_trust_flags.append(bool(kwargs["decomposition_trustworthy"]))
            return _child_result(
                0,
                verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail=""),
            )

        executor._execute_single_ac = AsyncMock(side_effect=fake_execute_single_ac)

        await executor._execute_decomposition_children(
            decision=decision,
            ac_index=0,
            ac_content="parent AC",
            session_id="s1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            seed_goal="goal",
            depth=0,
            execution_id="exec-corrupt2",
            level_contexts=None,
            retry_attempt=1,
            execution_counters=None,
            node_identity=node_identity,
            start_time=datetime.now(UTC),
            semantic_ac_key="key",
            ac_spec=AcceptanceCriterionSpec(description="parent AC", verify_command="pytest -q"),
        )

        # The fresh proposal itself is trustworthy...
        assert decision.trustworthy is True
        # ...but the corrupt prior record must still withhold trust for
        # every child dispatched this round.
        assert captured_trust_flags
        assert all(flag is False for flag in captured_trust_flags)


class TestAttestationEventDataCrossValidation:
    """Round-6 Finding #5 (BLOCKING): ``_decomposition_attestation_from_event_data``
    ignored the independently-serialized ``trustworthy`` boolean and never
    checked that the reconstructed (verdict, failed_axis, failed_sibling_id)
    combination is one ``attest_decomposition`` can actually produce.
    ``DecompositionAttestation.trustworthy`` is COMPUTED from ``verdict``, so
    corrupting JUST the verdict string to "trustworthy" while the rest of the
    payload still described a failure reconstructed a spuriously-trustworthy
    attestation and silently authorized the cheap-tier discount."""

    @staticmethod
    def _reconstruct(data: dict[str, object]):
        from ouroboros.orchestrator.parallel_executor import (
            _decomposition_attestation_from_event_data,
        )

        return _decomposition_attestation_from_event_data(data)

    def test_corrupted_verdict_with_failure_payload_fails_closed(self) -> None:
        """The exact scenario the review found: verdict corrupted to
        "trustworthy" while trustworthy/failed_axis/reason still describe a
        parent-gate failure. Must return None, not a trustworthy object."""
        attestation = self._reconstruct(
            {
                "node_id": "n1",
                "verdict": "trustworthy",
                "trustworthy": False,
                "failed_axis": "parent_gate",
                "failed_sibling_id": None,
                "reason": "parent verify gate failed after decomposition",
            }
        )
        assert attestation is None

    def test_trustworthy_verdict_with_failed_axis_fails_closed(self) -> None:
        """Even when the serialized boolean AGREES with the corrupted verdict,
        a trustworthy verdict carrying a failure attribution is a shape
        ``attest_decomposition`` never produces."""
        assert (
            self._reconstruct(
                {
                    "node_id": "n1",
                    "verdict": "trustworthy",
                    "trustworthy": True,
                    "failed_axis": "parent_gate",
                    "failed_sibling_id": None,
                    "reason": "corrupted",
                }
            )
            is None
        )

    def test_untrustworthy_verdict_with_trustworthy_true_fails_closed(self) -> None:
        assert (
            self._reconstruct(
                {
                    "node_id": "n1",
                    "verdict": "untrustworthy",
                    "trustworthy": True,
                    "failed_axis": "parent_gate",
                    "failed_sibling_id": None,
                    "reason": "corrupted",
                }
            )
            is None
        )

    def test_non_trustworthy_verdict_without_failed_axis_fails_closed(self) -> None:
        for verdict in ("untrustworthy", "indeterminate"):
            assert (
                self._reconstruct(
                    {
                        "node_id": "n1",
                        "verdict": verdict,
                        "trustworthy": False,
                        "failed_axis": None,
                        "failed_sibling_id": None,
                        "reason": "corrupted",
                    }
                )
                is None
            )

    def test_sibling_attribution_on_parent_gate_fails_closed(self) -> None:
        """Only SIBLING_GATE ever names a specific sibling."""
        assert (
            self._reconstruct(
                {
                    "node_id": "n1",
                    "verdict": "untrustworthy",
                    "trustworthy": False,
                    "failed_axis": "parent_gate",
                    "failed_sibling_id": "child-2",
                    "reason": "corrupted",
                }
            )
            is None
        )

    def test_untrustworthy_sibling_gate_without_sibling_id_fails_closed(self) -> None:
        """An evaluated sibling-gate FAILURE always names the failing sibling."""
        assert (
            self._reconstruct(
                {
                    "node_id": "n1",
                    "verdict": "untrustworthy",
                    "trustworthy": False,
                    "failed_axis": "sibling_gate",
                    "failed_sibling_id": None,
                    "reason": "corrupted",
                }
            )
            is None
        )

    def test_missing_or_non_bool_trustworthy_fails_closed(self) -> None:
        """``to_event_data`` ALWAYS writes the boolean; its absence or a
        wrong type is corruption, not a legitimate older shape."""
        base = {
            "node_id": "n1",
            "verdict": "trustworthy",
            "failed_axis": None,
            "failed_sibling_id": None,
            "reason": "ok",
        }
        assert self._reconstruct(dict(base)) is None
        assert self._reconstruct({**base, "trustworthy": "true"}) is None

    def test_every_legitimate_attestation_shape_round_trips(self) -> None:
        """Negative control: every shape ``attest_decomposition`` actually
        produces must survive to_event_data -> reconstruction unchanged, so
        the new validation never rejects a legitimately-recorded verdict."""
        from ouroboros.harness.decomposition_attestation import (
            ParentVerifyOutcome,
            attest_decomposition,
        )

        passing_sibling = SiblingVerifyOutcome(
            sibling_id="c1", has_verify_command=True, passed=True
        )
        cases = [
            # UNTRUSTWORTHY / SIBLING_GATE (failing sibling attributed)
            attest_decomposition(
                node_id="n1",
                siblings=(
                    SiblingVerifyOutcome(
                        sibling_id="c1", has_verify_command=True, passed=False, reason="boom"
                    ),
                ),
                parent=ParentVerifyOutcome(has_verify_command=True, passed=True),
            ),
            # UNTRUSTWORTHY / PARENT_GATE
            attest_decomposition(
                node_id="n2",
                siblings=(passing_sibling,),
                parent=ParentVerifyOutcome(has_verify_command=True, passed=False, reason="gap"),
            ),
            # INDETERMINATE / SIBLING_GATE, no sibling recorded
            attest_decomposition(
                node_id="n3",
                siblings=(),
                parent=ParentVerifyOutcome(has_verify_command=True, passed=True),
            ),
            # INDETERMINATE / SIBLING_GATE, indeterminate sibling attributed
            attest_decomposition(
                node_id="n4",
                siblings=(
                    SiblingVerifyOutcome(sibling_id="c1", has_verify_command=False, passed=None),
                ),
                parent=ParentVerifyOutcome(has_verify_command=True, passed=True),
            ),
            # INDETERMINATE / PARENT_GATE
            attest_decomposition(
                node_id="n5",
                siblings=(passing_sibling,),
                parent=ParentVerifyOutcome(has_verify_command=False, passed=None),
            ),
            # TRUSTWORTHY
            attest_decomposition(
                node_id="n6",
                siblings=(passing_sibling,),
                parent=ParentVerifyOutcome(has_verify_command=True, passed=True),
            ),
        ]
        for original in cases:
            reconstructed = self._reconstruct(dict(original.to_event_data()))
            assert reconstructed == original, original


class TestAttestationWriteFailureFailsClosed:
    """Round-5 Finding #4 (BLOCKING): a lost ``decomposition_attested`` write
    replays as silence. Bootstrap silence now withholds the child discount,
    but the lost correctness-bearing verdict must still be repaired durably.
    Once the write's retries are exhausted, the trust verdict the durable log
    cannot corroborate must not be acted on: the cached AND returned
    attestation are swapped for the fail-closed UNTRUSTWORTHY sentinel."""

    @staticmethod
    def _dispatch_kwargs(node_identity: ExecutionNodeIdentity) -> dict[str, object]:
        from datetime import UTC, datetime

        return {
            "ac_index": 0,
            "ac_content": "parent AC",
            "session_id": "s1",
            "tools": [],
            "tool_catalog": None,
            "system_prompt": "",
            "seed_goal": "goal",
            "depth": 0,
            "execution_id": "exec-1",
            "level_contexts": None,
            "retry_attempt": 0,
            "execution_counters": None,
            "node_identity": node_identity,
            "start_time": datetime.now(UTC),
            "semantic_ac_key": "key",
            "ac_spec": AcceptanceCriterionSpec(description="parent AC", verify_command="pytest -q"),
        }

    @pytest.mark.asyncio
    async def test_unpersisted_attestation_swapped_for_untrustworthy_sentinel(self) -> None:
        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        executor._event_emitter.emit_decomposition_attested = AsyncMock(return_value=False)
        executor._sleep = AsyncMock()
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        decision = _trustworthy_split_decision(node_identity.node_id)

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            return _child_result(
                0,
                verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail=""),
            )

        executor._execute_single_ac = AsyncMock(side_effect=fake_execute_single_ac)

        result = await executor._execute_decomposition_children(
            decision=decision, **self._dispatch_kwargs(node_identity)
        )

        # Extra bounded retry rounds on top of _safe_emit_event's internal ones.
        assert executor._event_emitter.emit_decomposition_attested.await_count == 3
        # The verdict the round actually COMPUTED was trustworthy — but with
        # no durable corroboration it must not be acted on: both the returned
        # result and the in-memory cache carry the fail-closed sentinel.
        assert result.decomposition_attestation is not None
        assert result.decomposition_attestation.trustworthy is False
        assert result.decomposition_attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        cached = executor._decomposition_attestations[node_identity.node_id]
        assert cached.trustworthy is False
        assert cached.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY

        # Round-6 Finding #4: the exhausted write keeps retrying in the
        # background; when it NEVER lands, the give-up is loud and the
        # cache keeps the fail-closed sentinel.
        import asyncio

        from ouroboros.orchestrator.parallel_executor import (
            _DEFERRED_DURABLE_WRITE_MAX_ATTEMPTS,
        )

        assert executor._deferred_durable_write_tasks
        await asyncio.gather(*executor._deferred_durable_write_tasks)
        assert executor._event_emitter.emit_decomposition_attested.await_count == (
            3 + _DEFERRED_DURABLE_WRITE_MAX_ATTEMPTS
        )
        still_cached = executor._decomposition_attestations[node_identity.node_id]
        assert still_cached.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY

    @pytest.mark.asyncio
    async def test_transient_write_failure_recovers_and_keeps_real_verdict(self) -> None:
        """Control: a write that lands on an extra retry round keeps the
        genuinely-computed verdict — the fail-closed swap only fires when
        every retry is exhausted."""
        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        executor._event_emitter.emit_decomposition_attested = AsyncMock(side_effect=[False, True])
        executor._sleep = AsyncMock()
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        decision = _trustworthy_split_decision(node_identity.node_id)

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            return _child_result(
                0,
                verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail=""),
            )

        executor._execute_single_ac = AsyncMock(side_effect=fake_execute_single_ac)

        result = await executor._execute_decomposition_children(
            decision=decision, **self._dispatch_kwargs(node_identity)
        )

        assert executor._event_emitter.emit_decomposition_attested.await_count == 2
        assert result.decomposition_attestation is not None
        assert result.decomposition_attestation.trustworthy is True
        assert executor._decomposition_attestations[node_identity.node_id].trustworthy is True
        # No background retry task for a write that landed in the foreground.
        assert not executor._deferred_durable_write_tasks

    @pytest.mark.asyncio
    async def test_background_retry_lands_original_attestation_and_restores_cache(
        self,
    ) -> None:
        """Round-6 Finding #4 (BLOCKING): after a crash, an EMPTY durable log
        for this node id replays as the legitimate 'never attested' miss and
        re-authorizes the proposal-trust discount — 'write failed' and
        'never happened' are indistinguishable silence. The exhausted write
        must keep retrying in the background (finding #3's convention), and
        once the ORIGINAL computed attestation finally lands, the durable
        log corroborates it and the in-memory cache is restored to the
        truthful verdict."""
        import asyncio

        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        executor._event_emitter.emit_decomposition_attested = AsyncMock(
            side_effect=[False, False, False, True]
        )
        executor._sleep = AsyncMock()
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        decision = _trustworthy_split_decision(node_identity.node_id)

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            return _child_result(
                0,
                verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail=""),
            )

        executor._execute_single_ac = AsyncMock(side_effect=fake_execute_single_ac)

        result = await executor._execute_decomposition_children(
            decision=decision, **self._dispatch_kwargs(node_identity)
        )

        # The round itself still fails closed (unchanged round-5 behavior).
        assert result.decomposition_attestation is not None
        assert result.decomposition_attestation.trustworthy is False
        assert executor._deferred_durable_write_tasks

        await asyncio.gather(*executor._deferred_durable_write_tasks)

        # The background retry re-emitted the ORIGINAL computed attestation
        # (trustworthy), not the sentinel...
        assert executor._event_emitter.emit_decomposition_attested.await_count == 4
        landed = executor._event_emitter.emit_decomposition_attested.await_args_list[-1]
        assert landed.kwargs["attestation"].trustworthy is True
        assert landed.kwargs["attestation"].verdict is DecompositionTrustVerdict.TRUSTWORTHY
        # ...and the cache was restored to the now-corroborated verdict.
        assert executor._decomposition_attestations[node_identity.node_id].trustworthy is True

    @pytest.mark.asyncio
    async def test_background_retry_stops_when_superseded_by_newer_round(self) -> None:
        """A newer round's attestation for the same node id must never be
        overwritten in replay by a late-landing backfill of the OLDER
        round's event (the loader is last-write-wins): once the sentinel is
        replaced, the background retry stops without emitting."""
        import asyncio

        from ouroboros.harness.decomposition_attestation import DecompositionAttestation

        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        executor._event_emitter.emit_decomposition_attested = AsyncMock(return_value=False)
        executor._sleep = AsyncMock()
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        decision = _trustworthy_split_decision(node_identity.node_id)

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            return _child_result(
                0,
                verify_gate_outcome=_VerifyGateOutcome(passed=True, reason=None, output_tail=""),
            )

        executor._execute_single_ac = AsyncMock(side_effect=fake_execute_single_ac)

        await executor._execute_decomposition_children(
            decision=decision, **self._dispatch_kwargs(node_identity)
        )
        assert executor._event_emitter.emit_decomposition_attested.await_count == 3

        # A newer round replaces the sentinel before the background retry
        # fires (its own write path owns durability for the new verdict).
        newer = DecompositionAttestation(
            node_id=node_identity.node_id,
            verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
            failed_axis=DecompositionTrustAxis.PARENT_GATE,
            failed_sibling_id=None,
            reason="newer round",
        )
        executor._decomposition_attestations[node_identity.node_id] = newer

        await asyncio.gather(*executor._deferred_durable_write_tasks)

        # The background loop stopped WITHOUT emitting the stale event and
        # WITHOUT touching the newer cached verdict.
        assert executor._event_emitter.emit_decomposition_attested.await_count == 3
        assert executor._decomposition_attestations[node_identity.node_id] is newer
