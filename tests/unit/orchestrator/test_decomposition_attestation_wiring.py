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
            verify_command=f"python -m pytest tests/unit/test_child_{i}.py -q",
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

    @pytest.mark.asyncio
    async def test_live_children_receive_their_own_executable_verify_contracts(self) -> None:
        """BLOCKING #1: a production-shaped decomposition must give children
        attainable verify gates. The child does not inherit the whole parent
        AC contract; it receives only the executable contract declared on its
        own decomposition child."""
        from datetime import UTC, datetime

        executor = _make_executor()
        executor._run_ac_verify_gate = AsyncMock(
            return_value=_VerifyGateOutcome(passed=True, reason=None, output_tail="")
        )
        executor._event_emitter.emit_decomposition_attested = AsyncMock()
        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        decision = _trustworthy_split_decision(node_identity.node_id)
        captured_specs: list[AcceptanceCriterionSpec | None] = []

        async def fake_execute_single_ac(**kwargs: object) -> ACExecutionResult:
            captured_specs.append(kwargs["ac_spec"])  # type: ignore[arg-type]
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
            retry_attempt=0,
            execution_counters=None,
            node_identity=node_identity,
            start_time=datetime.now(UTC),
            semantic_ac_key="parent-key",
            ac_spec=AcceptanceCriterionSpec(description="parent AC", verify_command="pytest -q"),
        )

        assert captured_specs
        assert all(isinstance(spec, AcceptanceCriterionSpec) for spec in captured_specs)
        assert [spec.verify_command for spec in captured_specs if spec is not None] == [
            "python -m pytest tests/unit/test_child_0.py -q",
            "python -m pytest tests/unit/test_child_1.py -q",
        ]
        assert all(spec.description.startswith("child ") for spec in captured_specs if spec)

    @pytest.mark.asyncio
    async def test_prior_untrustworthy_attestation_replayed_after_restart(self) -> None:
        """BLOCKING #2: durable attestation events must poison a resumed
        executor's future child-tier routing, not only the original process's
        in-memory cache."""
        from datetime import UTC, datetime

        from ouroboros.events.base import BaseEvent
        from ouroboros.harness.decomposition_attestation import (
            DecompositionAttestation,
            DecompositionTrustAxis,
        )

        node_identity = ExecutionNodeIdentity.root(execution_context_id="exec-1", ac_index=0)
        prior = DecompositionAttestation(
            node_id=node_identity.node_id,
            verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
            failed_axis=DecompositionTrustAxis.PARENT_GATE,
            failed_sibling_id=None,
            reason="prior round clobbered the workspace",
        )
        store = AsyncMock()
        store.replay = AsyncMock(
            return_value=[
                BaseEvent(
                    type="execution.ac.decomposition_attested",
                    aggregate_type="execution",
                    aggregate_id="exec-1",
                    data={
                        **node_identity.to_event_metadata(),
                        **prior.to_event_data(),
                        "execution_id": "exec-1",
                        "session_id": "s1",
                    },
                )
            ]
        )
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
        )
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
            execution_id="exec-1",
            level_contexts=None,
            retry_attempt=1,
            execution_counters=None,
            node_identity=node_identity,
            start_time=datetime.now(UTC),
            semantic_ac_key="key",
            ac_spec=AcceptanceCriterionSpec(description="parent AC", verify_command="pytest -q"),
        )

        assert captured_trust_flags
        assert all(flag is False for flag in captured_trust_flags)
