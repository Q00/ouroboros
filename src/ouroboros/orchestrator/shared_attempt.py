"""One worker attempt for a Seed whose criteria have no known source.

A Seed with ``sourced_scope`` (every ``ooo auto`` Seed) gives its worker only
content with a known source: the request verbatim and the criteria that bind
by ledger provenance. Its own criteria are written by the Seed generator, and
they stay the Seed's criteria for verification. When they are not the binding
criteria, position for position, no criterion can be shown to the worker on
its own, and one leaf per criterion would run the same request N times. Such
a Seed runs one worker attempt (root 0, whose task is the sourced objective),
and every other criterion is judged from that attempt against its own text:
the typed evidence, the legacy verifier, its own verify command and the check
package gate, exactly once per criterion (:func:`settle_from_shared_attempt`).

The decision reads structure only: whether the Seed carries ``sourced_scope``,
how many criteria it has, and whether the binding criteria equal the Seed's
criteria as lists. Nothing classifies text.
"""

from __future__ import annotations

from dataclasses import replace
import os
from typing import TYPE_CHECKING, Any

from ouroboros.core.seed import AcceptanceCriterionSpec, SourcedScope, ac_text, ac_texts
from ouroboros.orchestrator.evidence.ac_classification import (
    _effective_evidence_schema_for_ac,
    _scoped_evidence_record_for_ac,
)
from ouroboros.orchestrator.evidence.acceptance_gate import split_leaf_rejection
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome, ACExecutionResult
from ouroboros.orchestrator.verifier import VerifierStatus, VerifierVerdict

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.orchestrator.dependency_analyzer import DependencyGraph
    from ouroboros.orchestrator.parallel_executor import ParallelACExecutor

# The root whose worker attempt executes a shared-attempt Seed.
SHARED_ATTEMPT_ROOT = 0
# The typed-evidence field that names the entry point of one criterion's check;
# the shared attempt declared it for root 0 only.
_ENTRY_POINTS_FIELD = "entry_points"


def sourced_criteria_aligned(scope: SourcedScope | None, criteria: tuple[str, ...]) -> bool:
    """True when the binding criteria are the Seed's criteria, position for position."""
    return (
        scope is not None
        and bool(scope.acceptance_criteria)
        and scope.acceptance_criteria == criteria
    )


def shares_one_attempt(scope: SourcedScope | None, criteria: tuple[str, ...]) -> bool:
    """True when one worker attempt executes every criterion of the Seed."""
    return scope is not None and len(criteria) > 1 and not sourced_criteria_aligned(scope, criteria)


def renders_sourced_objective(
    scope: SourcedScope | None, criteria: tuple[str, ...], root_index: int | None
) -> bool:
    """True when the dispatch under root ``root_index`` shows the worker the sourced objective.

    That is every dispatch of a Seed with ``sourced_scope`` unless its binding
    criteria are the Seed's own, position for position, and ``root_index``
    names one of them. The worker then owns the sourced objective, not the
    Seed's criterion text, so no criterion-text exemption applies to it.
    """
    aligned = (
        sourced_criteria_aligned(scope, criteria)
        and root_index is not None
        and 0 <= root_index < len(criteria)
    )
    return isinstance(scope, SourcedScope) and not aligned


def shared_attempt_graph(seed: Seed) -> DependencyGraph | None:
    """One stage holding every criterion, for a shared-attempt Seed; else ``None``.

    Root 0 and the criteria judged from its attempt share the stage, so the
    level checkpoint never records the attempt without its judgments.
    """
    if not shares_one_attempt(seed.sourced_scope, ac_texts(seed.acceptance_criteria)):
        return None
    from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph

    nodes = tuple(
        ACNode(index=index, content=ac_text(criterion))
        for index, criterion in enumerate(seed.acceptance_criteria)
    )
    return DependencyGraph(nodes=nodes, execution_levels=(tuple(node.index for node in nodes),))


async def settle_from_shared_attempt(
    executor: ParallelACExecutor,
    *,
    seed: Seed,
    ac_index: int,
    source: ACExecutionResult | None,
    session_id: str,
    execution_id: str,
) -> ACExecutionResult:
    """Judge criterion ``ac_index`` from the shared attempt ``source`` (root 0).

    The same steps an atomic leaf's own attempt goes through, with criterion
    ``ac_index``'s text and success contract: typed evidence, the legacy
    verifier, the acceptance rule, then ``_apply_verify_gate`` (its verify
    command, the check package gate, the no-evidence route). A criterion that
    passes is ``SETTLED_FROM_SHARED_ATTEMPT``; one that fails is ``FAILED``
    and gets no dispatch of its own. Without a successful attempt there is
    nothing to judge, and the criterion is ``BLOCKED`` (not attempted).
    """
    from ouroboros.orchestrator.parallel_executor import (
        _FOUNDATION_A_ENTRY_RUN_AC_VERIFY_GATE,
        _FOUNDATION_A_ENTRY_RUN_ATOMIC_VERIFIER_PASS,
        _invoke_execution_authority_entry,
    )

    spec = seed.acceptance_criteria[ac_index]
    ac_content = ac_text(spec)
    if source is None or not source.success:
        return ACExecutionResult(
            ac_index=ac_index,
            ac_content=ac_content,
            success=False,
            error=(
                f"Blocked: the shared worker attempt (AC {SHARED_ATTEMPT_ROOT + 1}) did not "
                "succeed, so this criterion was not judged"
            ),
            retry_attempt=source.retry_attempt if source is not None else 0,
            outcome=ACExecutionOutcome.BLOCKED,
        )

    has_success_contract = isinstance(spec, AcceptanceCriterionSpec) and spec.has_success_contract
    has_expected_artifacts = isinstance(spec, AcceptanceCriterionSpec) and bool(
        spec.expected_artifacts
    )
    verify_gate_active = executor._run_verify_commands
    contract: dict[str, Any] = {
        "has_success_contract": has_success_contract,
        "has_expected_artifacts": has_expected_artifacts,
        "verify_gate_active": verify_gate_active,
        # Root 0's worker was shown the sourced objective; every criterion
        # judged from its attempt is checked against the schema it was asked for.
        "sourced_objective": renders_sourced_objective(
            seed.sourced_scope, ac_texts(seed.acceptance_criteria), SHARED_ATTEMPT_ROOT
        ),
    }
    verify_gate_outcome = None
    if verify_gate_active and has_success_contract:
        cwd = executor._task_cwd or executor._adapter.working_directory or os.getcwd()
        verify_gate_outcome = await _invoke_execution_authority_entry(
            executor, _FOUNDATION_A_ENTRY_RUN_AC_VERIFY_GATE, spec=spec, cwd=cwd
        )

    messages = source.messages
    final_message = source.final_message
    typed_evidence, typed_validation, typed_error = executor._observe_atomic_typed_evidence(
        ac_content=ac_content, final_message=final_message, success=True, **contract
    )
    if typed_evidence is not None and _ENTRY_POINTS_FIELD in typed_evidence.data:
        typed_evidence = EvidenceRecord(
            data={k: v for k, v in typed_evidence.data.items() if k != _ENTRY_POINTS_FIELD},
            source=typed_evidence.source,
        )
    verifier_verdict = _invoke_execution_authority_entry(
        executor,
        _FOUNDATION_A_ENTRY_RUN_ATOMIC_VERIFIER_PASS,
        ac_content=ac_content,
        final_message=final_message,
        success=True,
        messages=messages,
        typed_evidence=typed_evidence,
        typed_validation=typed_validation,
        **contract,
    )
    verify_gate_replaces_all_evidence = bool(
        verify_gate_outcome is not None
        and executor._execution_profile is not None
        and not _effective_evidence_schema_for_ac(
            executor._execution_profile, ac_content, **contract
        ).required
    )
    if (
        executor._fat_harness_mode
        and verifier_verdict is None
        and not any(not message.is_final for message in messages)
    ):
        verifier_verdict = VerifierVerdict(
            passed=False,
            reasons=("transcript_missing_infrastructure: runtime support messages were empty",),
            failure_class="TRANSCRIPT_MISSING_INFRASTRUCTURE",
        )
    error = executor._fat_harness_acceptance_error(
        runtime_success=True,
        typed_evidence=typed_evidence,
        typed_validation=typed_validation,
        typed_error=typed_error,
        verifier_verdict=verifier_verdict,
        verify_gate_outcome=verify_gate_outcome,
        verify_gate_replaces_all_evidence=verify_gate_replaces_all_evidence,
    )
    if verifier_verdict is not None and verifier_verdict.status is VerifierStatus.UNAVAILABLE:
        error = None  # no evidence to judge: the work stays successful, as for a leaf
    error, recorded_rejection, legacy_rejection = split_leaf_rejection(
        error,
        mode=executor._transcript_verifier,
        verify_gate_outcome=verify_gate_outcome,
        gated=bool(getattr(executor, "check_package_gate", None)),
        session_id=session_id,
        execution_id=execution_id,
        ac_index=ac_index,
    )
    success = error is None
    if success and executor._execution_profile is not None and typed_evidence is not None:
        typed_evidence = _scoped_evidence_record_for_ac(
            executor._execution_profile, ac_content, typed_evidence, **contract
        )
    result = ACExecutionResult(
        ac_index=ac_index,
        ac_content=ac_content,
        success=success,
        messages=messages,
        final_message=(
            final_message
            if error is None
            else (f"{error}\n\nRuntime final message:\n{final_message}" if final_message else error)
        ),
        session_id=source.session_id,
        retry_attempt=source.retry_attempt,
        typed_evidence=typed_evidence,
        typed_evidence_validation=typed_validation,
        typed_evidence_error=typed_error,
        atomic_verifier_verdict=verifier_verdict,
        verify_gate_outcome=verify_gate_outcome,
        error=error,
        legacy_rejection=legacy_rejection,
        recorded_rejection=recorded_rejection,
    )
    result = await executor._apply_verify_gate(
        seed=seed,
        ac_index=ac_index,
        result=result,
        session_id=session_id,
        execution_id=execution_id,
    )
    if result.success and result.outcome is ACExecutionOutcome.SUCCEEDED:
        result = replace(result, outcome=ACExecutionOutcome.SETTLED_FROM_SHARED_ATTEMPT)
    return result


__all__ = [
    "SHARED_ATTEMPT_ROOT",
    "settle_from_shared_attempt",
    "shared_attempt_graph",
    "shares_one_attempt",
    "sourced_criteria_aligned",
]
