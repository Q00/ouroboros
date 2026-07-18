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
    """Fix 1 (round 2, BLOCKING): reproduction + fix proof for the live-dispatch
    gap. Every OTHER test in this module hand-constructs ``ACExecutionResult``
    objects or mocks ``_execute_single_ac`` outright, which is exactly how the
    round-2 bug hid: those tests can never fail even when a genuinely live
    decomposed child can never populate its own ``verify_gate_outcome``. This
    test dispatches every child through the REAL ``_execute_single_ac`` ->
    ``_execute_atomic_ac`` code path (a stub *runtime adapter* is the only
    double -- nothing inside the orchestrator itself is mocked) to prove a
    live child can now reach a real, non-INDETERMINATE verdict.
    """

    @pytest.mark.asyncio
    async def test_live_decomposed_children_reach_trustworthy_verdict(self, tmp_path) -> None:
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

        # Every sibling was genuinely dispatched through _execute_atomic_ac
        # (no mocking of _execute_single_ac) and populated its OWN
        # verify_gate_outcome from the real filesystem oracle -- this is the
        # exact field that was always None for a live child before the fix.
        assert len(result.sub_results) == 2
        for sub_result in result.sub_results:
            assert sub_result.verify_gate_outcome is not None
            assert sub_result.verify_gate_outcome.passed is True

        assert result.decomposition_attestation is not None
        assert result.decomposition_attestation.verdict is DecompositionTrustVerdict.TRUSTWORTHY
        assert result.decomposition_attestation.trustworthy is True

    @pytest.mark.asyncio
    async def test_live_decomposed_child_missing_artifact_is_untrustworthy_not_indeterminate(
        self, tmp_path
    ) -> None:
        """The negative case: when the real filesystem oracle genuinely fails
        for a live child (the artifact is missing), the round must resolve to
        UNTRUSTWORTHY -- backed by a real evaluated gate -- not silently stay
        INDETERMINATE as it did for every live round before this fix."""
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

        assert result.sub_results[0].verify_gate_outcome is not None
        assert result.sub_results[0].verify_gate_outcome.passed is False
        assert result.decomposition_attestation is not None
        assert result.decomposition_attestation.verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
        assert result.decomposition_attestation.trustworthy is False


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
