"""Tests wiring the gate-anchored decomposition attestation (harness module)
into the parallel executor: computed right after a decomposition round's
siblings finish dispatch, cached per node id, emitted as a durable event, and
consulted to poison a root AC's NEXT retry's child-tier discount.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.harness.decomposition_attestation import (
    DecompositionTrustAxis,
    DecompositionTrustVerdict,
)
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


def _make_executor() -> ParallelACExecutor:
    executor = ParallelACExecutor(
        adapter=MagicMock(),
        event_store=AsyncMock(),
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


def _make_live_artifact_executor(tmp_path, writes_per_dispatch: list[tuple[str, ...]]):
    """Build an executor whose stub RUNTIME adapter performs REAL file writes.

    Only the runtime transport is a double; children dispatch through the real
    ``_execute_single_ac`` -> ``_execute_atomic_ac`` path and every artifact
    existence check runs against the real filesystem under ``tmp_path``. Each
    sequential dispatch pops the next tuple of relative paths and creates
    those files -- modeling what that child's agent session actually produced.
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
