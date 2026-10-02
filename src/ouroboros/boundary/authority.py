"""Acceptance authority inside the runner: the frozen check package decides.

``OrchestratorRunner`` installs the authority on its parallel executor
(``install``) and calls it once on the executor's result, after the worker
has stopped and before the terminal acceptance plan and the session status
are persisted (``OrchestratorRunner.acceptance_authority``).

While the worker runs (``CheckPackageGate``, installed as the executor's
``check_package_gate``):

- the legacy per-criterion verifier (typed evidence plus the transcript
  verifier) no longer rejects an attempt of a criterion an admitted check
  covers; its verdict stays on the result as an annotation (advisory reason
  and failure class for telemetry), so it triggers no retry. For a criterion
  no admitted check covers (uncovered, or every check excluded at
  admission) the legacy verifier decides: its rejection fails
  the attempt and drives the retry with the legacy verdict's own failure
  class, as with the check package off;
- after each attempt of a root criterion the gate runs that criterion's
  checks on the workspace, through the default binding or the entry point
  the worker declared in its evidence; a fail marks the attempt failed with
  the counterexample (``CheckPackageProvenance.repair``), which the
  executor's retry loop carries into the next attempt. The executor's final
  settlement judges every other gate of such an attempt on the final
  workspace; only one it finds holding may be accepted again by the
  terminal decision (``failed_by_the_package_gate_alone``). A failure through a
  worker-declared binding names that binding. The gate runs the visible
  cases only (the base run of a declared binding too): held-out cases run
  only in the final verification, so no held-out input reaches a process the
  worker's code runs in while a later attempt could still use it, and a
  repair message never mentions held-out cases. A gate run is a repair
  signal, never a verified pass (``acceptance.criterion_verdicts`` needs a
  passing held-out case).

After the worker stops (``CheckPackageAuthority.__call__``):

1. ``verify_check_package`` records the final bindings, verifies the
   finished workspace, and records verification and selection;
2. ``reconcile_acceptance`` decides every criterion (pass accepts, fail and
   indeterminate reject, a criterion the worker never attempted is not
   accepted; an unverified or uncovered criterion is decided
   by the legacy verifier, and stays unverified and accepted only when the
   legacy verifier has no evidence either); the legacy verdict is kept per
   criterion; ``boundary.acceptance.reconciled`` records it;
3. the executor result is returned with each root result's ``success`` and
   ``outcome`` set to the decision and the counts recomputed, so the durable
   status, the panel, the exit code, and ``workflow_outcome`` carry it.

Failures close, never open. On an error the gate returns the attempt
unchanged (an attempt it cannot judge is not a repair signal). When the
authority itself cannot decide (anything raised, including reading the
legacy verdicts), every criterion an admitted check covers is
``indeterminate`` (``authority_error:<type>``), so it is not accepted, and
every other criterion is ``uncovered``, which the legacy verifier decides,
exactly as a resumed run without its package (``boundary/resume.py``). A
check package failure is never lifted because something else failed. With
no admitted package nothing is covered, so the same rule gives the legacy
verdicts. Only when even that decision cannot be built is every root result
failed (``_fail_attempted``).

The authority belongs to one run: the execution id, Seed digest and
criterion keys its state was prepared for. A gate or terminal call for any
other run is refused (``run_mismatch:<field>``): the gate lets the legacy
verifier decide the attempt, and the terminal call leaves every criterion
the package covers undecided without recording anything in this run's
journal or using its one decision.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.boundary.acceptance import (
    AcceptanceReconciliation,
    CriterionVerdict,
    ExistingOutcome,
    Governor,
    LegacyNoEvidenceReason,
    PackageCriterionStatus,
    criterion_verdicts,
    reconcile_acceptance,
)
from ouroboros.boundary.binding import CheckTier, declared_entry_points, entry_points_request
from ouroboros.boundary.binding_flow import (
    assign_tiers,
    bindings_payload,
    verify_with_bindings,
)
from ouroboros.boundary.events import ReconciliationPayload
from ouroboros.boundary.ledger import BoundaryLedger
from ouroboros.boundary.package import seed_criterion_keys, seed_digest
from ouroboros.boundary.per_check import criteria_without_admitted_check
from ouroboros.boundary.run_wiring import (
    BoundaryRunState,
    BoundaryVerdict,
    CheckPackageSettings,
    _counterexamples,
    forget_live_state,
    repair_text,
    verify_check_package,
)
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.parallel_executor_models import (
    PACKAGE_FAILURE_CLASS_PREFIX,
    CheckPackageOwner,
    CheckPackageProvenance,
    failed_by_the_package_gate_alone,
    legacy_owned,
)

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

_ACCEPTED_OUTCOMES = frozenset({"succeeded", "satisfied_externally"})
PACKAGE_REJECTION_ERROR = "check_package: the finished workspace fails the frozen check package"
PACKAGE_INDETERMINATE_ERROR = (
    "check_package: the frozen check package could not decide this criterion"
)
LEGACY_REJECTION_ERROR = "legacy verifier rejected this criterion"
LEGACY_DECIDED_PREFIX = "legacy-decided"
NO_BINDING = "no_binding"
NO_BINDING_AFTER_REQUEST = "no_binding_after_request"
NO_BINDING_BUDGET_EXHAUSTED = "no_binding_budget_exhausted"
BINDING_REJECTED_PREFIX = "binding_invalid:"
AUTHORITY_ERROR_PREFIX = "authority_error:"
RUN_MISMATCH_PREFIX = "run_mismatch:"


RunIdentity = tuple[str, str, tuple[str, ...]]
"""The run an authority belongs to: execution id, Seed digest, ordered criterion keys."""


def mismatched_run(run: RunIdentity, seed: Seed, execution_id: str | None) -> str | None:
    """``run_mismatch:<field>`` when a call is not for ``run``, else ``None``.

    The one identity rule of the live and the resumed authority.
    ``execution_id`` ``None`` means the caller did not say (the Seed is
    still checked). A Seed that cannot be digested is a mismatch.
    """
    expected_execution, expected_digest, expected_keys = run
    if execution_id is not None and execution_id != expected_execution:
        return f"{RUN_MISMATCH_PREFIX}execution_id"
    try:
        if seed_digest(seed) != expected_digest:
            return f"{RUN_MISMATCH_PREFIX}seed_digest"
        if seed_criterion_keys(seed) != expected_keys:
            return f"{RUN_MISMATCH_PREFIX}criterion_keys"
    except Exception:  # noqa: BLE001 - an unreadable Seed is not this run's Seed
        return f"{RUN_MISMATCH_PREFIX}seed_digest"
    return None


@dataclass(frozen=True, slots=True)
class AuthorityOutcome:
    """What the authority decided for one run."""

    legacy_run_accepted: bool
    verdict: BoundaryVerdict | None = None
    reconciliation: AcceptanceReconciliation | None = None
    error: str | None = None
    legacy: dict[int, ExistingOutcome] = field(default_factory=dict)

    @property
    def package_decided(self) -> bool:
        """Whether the package governed at least one criterion."""
        if self.reconciliation is None:
            return False
        return any(
            decision.governed_by.value == "check_package"
            for decision in self.reconciliation.decisions
        )


def _declared_from(result: Any) -> list[Any]:
    """The first ``entry_points`` found on a result or its sub-results."""
    entries = declared_entry_points(getattr(result, "typed_evidence", None))
    if entries:
        return entries
    for sub in getattr(result, "sub_results", ()) or ():
        entries = _declared_from(sub)
        if entries:
            return entries
    return []


def legacy_verdict_in_tree(result: Any) -> tuple[bool, str | None, str | None]:
    """``(rejected, failure_class, rejection_text)`` over a result and its sub-results.

    A decomposed root is assembled from its sub-ACs' successes; with the gate
    installed a sub-AC's legacy rejection is advisory and stays on that
    sub-result, not on the root. The legacy verdict of the root is therefore
    the whole tree's: rejected when the legacy verifier rejected the root or
    any sub-AC.

    Only the rejection the executor made (``legacy_rejection``) is a legacy
    rejection. A verifier verdict that did not pass without one is not: the
    executor keeps such a result successful when the transcript was
    unavailable (``TRANSCRIPT_MISSING_INFRASTRUCTURE``), when the environment
    was unverifiable, or when a passing ``verify_command`` replaced the
    evidence, and with the check package off it is accepted. That verdict
    carries no rejection; it only supplies the failure class of a real
    rejection.
    """
    text = getattr(result, "legacy_rejection", None) or None
    if text:
        verdict = getattr(result, "atomic_verifier_verdict", None)
        failure_class = (
            getattr(verdict, "failure_class", None)
            if verdict is not None and not bool(getattr(verdict, "passed", True))
            else None
        )
        return True, failure_class, text
    for sub in getattr(result, "sub_results", ()) or ():
        rejected, failure_class, sub_text = legacy_verdict_in_tree(sub)
        if rejected:
            return True, failure_class, sub_text
    return False, None, None


def _legacy_evidence(result: Any) -> bool:
    """Whether the legacy verifier accepted ``result`` on evidence.

    A passing verifier verdict, or a passing ``verify_command`` whose
    environment was verifiable; a decomposed root has evidence when every
    sub-result has. A success with no verdict, an unavailable transcript
    (``TRANSCRIPT_MISSING_INFRASTRUCTURE``) or an unverifiable environment
    is an acceptance without evidence.
    """
    gate = getattr(result, "verify_gate_outcome", None)
    if (
        gate is not None
        and bool(getattr(gate, "passed", False))
        and not bool(getattr(gate, "environment_unverifiable", False))
    ):
        return True
    verdict = getattr(result, "atomic_verifier_verdict", None)
    if verdict is not None and bool(getattr(verdict, "passed", False)):
        return True
    subs = tuple(getattr(result, "sub_results", ()) or ())
    return bool(subs) and all(_legacy_evidence(sub) for sub in subs)


def _legacy_no_evidence_reason(result: Any) -> LegacyNoEvidenceReason:
    """Why ``_legacy_evidence`` found no evidence for ``result``; call it only then.

    Read from the same typed fields: the root's own unverifiable environment
    or unavailable transcript first, then the first sub-result without
    evidence of a decomposed root, then the root's verdict. Decides nothing.
    """
    gate = getattr(result, "verify_gate_outcome", None)
    if gate is not None and bool(getattr(gate, "environment_unverifiable", False)):
        return LegacyNoEvidenceReason.ENVIRONMENT_UNVERIFIABLE
    verdict = getattr(result, "atomic_verifier_verdict", None)
    failure_class = getattr(verdict, "failure_class", None) if verdict is not None else None
    if failure_class == FailureClass.TRANSCRIPT_MISSING_INFRASTRUCTURE.value:
        return LegacyNoEvidenceReason.TRANSCRIPT_UNAVAILABLE
    if failure_class == FailureClass.SCRIPT_ABSENT_FROM_ARTIFACT.value:
        return LegacyNoEvidenceReason.SCRIPT_ABSENT_FROM_ARTIFACT
    for sub in tuple(getattr(result, "sub_results", ()) or ()):
        if not _legacy_evidence(sub):
            return _legacy_no_evidence_reason(sub)
    if verdict is None:
        return LegacyNoEvidenceReason.NO_VERIFIER_VERDICT
    return LegacyNoEvidenceReason.VERIFIER_VERDICT_NOT_PASSED


def _legacy_owned(reconciliation: AcceptanceReconciliation) -> AcceptanceReconciliation:
    """The reconciliation of a run with no admitted package: the legacy verifier owns it.

    Every criterion is uncovered, so the package governs nothing: an attempted
    criterion the reconciliation would attribute to the package (a legacy
    acceptance without evidence) is decided by the legacy verifier, exactly as
    with the check package off. Acceptance is unchanged.
    """
    decisions = tuple(
        replace(decision, governed_by=Governor.EXISTING_VERIFIER)
        if decision.governed_by is Governor.CHECK_PACKAGE
        else decision
        for decision in reconciliation.decisions
    )
    return replace(reconciliation, decisions=decisions)


def existing_outcomes_from_results(
    parallel_result: Any, *, gated: bool = False
) -> dict[int, ExistingOutcome]:
    """Per root criterion: whether the worker attempted it, and the legacy verdict.

    ``outcome`` carries the legacy verifier's verdict (``failed`` when it
    rejected the attempt, whatever the executor did with that rejection) and
    ``failure_class`` its class. With the gate installed (``gated``), an
    attempt counts as made when the executor result succeeded, failed only
    because the package gate failed it (``failed_by_the_package_gate_alone``),
    or was a legacy-decided rejection (``legacy_owned``); a runtime failure, a
    blocked or invalid criterion, a failed ``verify_command``, or a package
    gate failure whose other gates the final settlement did not find holding
    is not an attempt the package may accept. Without the gate, a failed
    result is a legacy rejection of an attempt.
    """
    outcomes: dict[int, ExistingOutcome] = {}
    for result in getattr(parallel_result, "results", ()) or ():
        index = getattr(result, "ac_index", None)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        base = getattr(getattr(result, "outcome", None), "value", None)
        if not isinstance(base, str):
            base = "succeeded" if getattr(result, "success", False) else "failed"
        # The executor keeps the rejection it made advisory (typed evidence or
        # transcript verifier) on the result, or on a sub-AC's result for a
        # decomposed root.
        legacy_rejected, failure_class, _text = legacy_verdict_in_tree(result)
        if gated:
            judged = (
                bool(getattr(result, "success", False))
                or failed_by_the_package_gate_alone(result)
                or legacy_owned(result)
            )
        else:
            judged = base in _ACCEPTED_OUTCOMES or base == "failed"
            legacy_rejected = legacy_rejected or base == "failed"
        if judged:
            outcome = (
                "failed"
                if legacy_rejected
                else (base if base in _ACCEPTED_OUTCOMES else "succeeded")
            )
            terminal = "failed" if outcome == "failed" else "completed"
        else:
            outcome, terminal = base, "not_attempted"
        no_evidence = outcome in _ACCEPTED_OUTCOMES and not _legacy_evidence(result)
        outcomes[index] = ExistingOutcome(
            root_ac_index=index,
            outcome=outcome,
            disposition="accepted" if outcome in _ACCEPTED_OUTCOMES else outcome,
            terminal_status=terminal,
            failure_class=failure_class,
            no_evidence=no_evidence,
            no_evidence_reason=_legacy_no_evidence_reason(result) if no_evidence else None,
        )
    return outcomes


def apply_reconciliation(parallel_result: Any, reconciliation: AcceptanceReconciliation) -> Any:
    """Return ``parallel_result`` with every root result set to its decision.

    A failed result becomes a success only when the package gate alone fails
    it on the settled final workspace (``failed_by_the_package_gate_alone``):
    never one another gate failed, and never on a cached pass. Every result
    it changes records the deciding authority (``CheckPackageProvenance``).
    """
    from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome

    decisions = {decision.root_ac_index: decision for decision in reconciliation.decisions}
    results = []
    success_delta = failure_delta = external_delta = 0
    changed = False
    for result in parallel_result.results:
        decision = decisions.get(result.ac_index)
        previous = result.outcome
        if decision is None:
            results.append(result)
            continue
        if (
            decision.accepted
            and previous is ACExecutionOutcome.FAILED
            and failed_by_the_package_gate_alone(result)
        ):
            provenance = CheckPackageProvenance(
                CheckPackageOwner.CHECK_PACKAGE,
                declared_binding_pass=decision.declared_binding_pass,
            )
            results.append(
                replace(
                    result,
                    success=True,
                    outcome=ACExecutionOutcome.SUCCEEDED,
                    error=None,
                    check_package=provenance,
                )
            )
            success_delta += 1
            failure_delta -= 1
            changed = True
        elif not decision.accepted and previous in (
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.SATISFIED_EXTERNALLY,
        ):
            if decision.legacy_decided:
                error = legacy_decided_error(result, decision.reason)
            elif decision.package_status is PackageCriterionStatus.FAIL:
                error = PACKAGE_REJECTION_ERROR
            else:
                error = f"{PACKAGE_INDETERMINATE_ERROR} ({decision.reason})"
            owner = (
                CheckPackageOwner.LEGACY_VERIFIER
                if decision.legacy_decided
                else CheckPackageOwner.CHECK_PACKAGE
            )
            results.append(
                replace(
                    result,
                    success=False,
                    outcome=ACExecutionOutcome.FAILED,
                    error=error,
                    check_package=CheckPackageProvenance(owner),
                )
            )
            failure_delta += 1
            if previous is ACExecutionOutcome.SUCCEEDED:
                success_delta -= 1
            else:
                external_delta -= 1
            changed = True
        else:
            results.append(result)
    if not changed:
        return parallel_result
    return replace(
        parallel_result,
        results=tuple(results),
        success_count=parallel_result.success_count + success_delta,
        failure_count=parallel_result.failure_count + failure_delta,
        externally_satisfied_count=parallel_result.externally_satisfied_count + external_delta,
    )


def legacy_decided_error(result: Any, reason: str) -> str:
    """The error of a criterion the legacy verifier decided and rejected."""
    text = legacy_verdict_in_tree(result)[2] or LEGACY_REJECTION_ERROR
    return f"{LEGACY_DECIDED_PREFIX} ({reason}): {text}"


class CheckPackageGate:
    """Per-attempt repair signal from the frozen package (see the module docstring)."""

    def __init__(self, authority: CheckPackageAuthority) -> None:
        self._authority = authority
        self.log: list[dict[str, Any]] = []
        # Attempts the legacy verifier failed on a criterion no admitted
        # check covers (it decides those criteria, retries included).
        self.legacy_failures = 0
        # One decision per attempt: settlement paths hand the same attempt to
        # the gate again; they get the stored decision, not a new verification.
        # An attempt is the whole result handed over (a cross-harness alternate
        # shares its criterion and retry number, not its session or verdict).
        self._decided: dict[tuple[int, int], list[tuple[Any, Any]]] = {}

    async def __call__(
        self,
        *,
        seed: Seed,
        ac_index: int,
        result: Any,
        execution_id: str | None = None,
        session_id: str | None = None,
    ) -> Any:
        mismatch = self._authority.run_mismatch(seed, execution_id)
        if mismatch is not None:
            # Another run's attempt: the package does not judge it, and the
            # legacy verdict it made advisory decides again.
            log.warning("boundary.gate.foreign_run", ac_index=ac_index, mismatch=mismatch)
            return self._legacy_decides(result) if getattr(result, "success", False) else result
        attempt = (ac_index, int(getattr(result, "retry_attempt", 0) or 0))
        for seen, stored in self._decided.get(attempt, ()):
            if seen == result:
                return stored
        try:
            decided = await self._decide(ac_index, result)
        except Exception as exc:  # noqa: BLE001 - the gate must never fail an attempt by itself
            log.warning("boundary.gate.failed", ac_index=ac_index, error_type=type(exc).__name__)
            return result
        self._decided.setdefault(attempt, []).append((result, decided))
        if decided is not result and legacy_owned(decided):
            # A legacy-decided rejection, counted once per attempt.
            self.legacy_failures += 1
        return decided

    async def _decide(self, ac_index: int, result: Any) -> Any:
        authority = self._authority
        state = authority.state
        package = state.package
        if not getattr(result, "success", False):
            return result
        if package is None or not state.admitted:
            # No admitted package: the legacy verifier decides every attempt.
            return self._legacy_decides(result)
        keys = state.criterion_keys
        if not 0 <= ac_index < len(keys):
            return result
        key = keys[ac_index]
        check_ids = authority.admitted_check_ids(key)
        if not check_ids or key in authority.legacy_decided_keys():
            # No admitted check covers it (uncovered, or every check
            # excluded at admission): the legacy verifier decides
            # it, so its rejection fails the attempt and drives the retry,
            # exactly as with the check package off.
            return self._legacy_decides(result)
        entries = authority.remember_declaration(key, _declared_from(result))
        assignments, results = await assign_tiers(
            package,
            base=state.base_snapshot,
            contract=state.contract,
            declared={key: entries} if entries else None,
            expected_base_digest=state.admission.base_tree_digest,
            admission=state.admission,
            # Visible cases only, for the base run of a declared binding too:
            # no held-out input reaches any process before the final verdict.
            run_options={"interpreter": state.interpreter, "include_held_out": False},
            base_run_cache=authority.base_runs,
        )
        subset = {check_id: assignments[check_id] for check_id in check_ids}
        bound = await verify_with_bindings(
            package,
            authority.candidate,
            subset,
            contract=state.contract,
            interpreter=state.interpreter,
            include_held_out=False,
        )
        verification = bound.effective
        verdicts = criterion_verdicts(
            package, verification, admission=state.admission, assignments=subset
        )
        item = verdicts[key]
        await BoundaryLedger(authority.event_store).record_bindings(
            state.boundary_id,
            package_id=package.package_id,
            payload=bindings_payload(
                subset,
                {k: v for k, v in results.items() if k in subset},
                phase="repair",
                root_ac_index=ac_index,
                retry_attempt=getattr(result, "retry_attempt", 0),
                status=item.status.value,
            ),
        )
        self.log.append(
            {"ac_index": ac_index, "status": item.status.value, "tier": item.tier.value}
        )
        retry_attempt = int(getattr(result, "retry_attempt", 0) or 0)
        if item.status.is_unverified and item.reason == NO_BINDING and not entries:
            # The criterion needs a late binding and the worker declared none.
            # Ask once, declaration only, within the retry budget.
            if key in authority.binding_requested:
                return result
            if not authority.repair_follows(retry_attempt):
                authority.binding_budget_exhausted.add(key)
                return result
            authority.binding_requested.add(key)
            self.log[-1]["binding_requested"] = True
            return self._repair(
                result, _declaration_request_message(key, authority.interfaces().get(ac_index))
            )
        if item.status is PackageCriterionStatus.INDETERMINATE and item.reason.startswith(
            BINDING_REJECTED_PREFIX
        ):
            # A rejected declaration is fixable within the retry budget; the
            # reason names no oracle value.
            return self._repair(result, _binding_rejected_message(item, package))
        if item.status is not PackageCriterionStatus.FAIL:
            return result
        partial = BoundaryVerdict(
            verdict="fail",
            reasons=(),
            boundary_id=state.boundary_id,
            package_id=package.package_id,
            counterexamples=_counterexamples(verification) if verification is not None else (),
            verdicts=verdicts,
            oracle_results={
                check.check_id: check.oracle_result
                for check in (verification.checks if verification is not None else ())
                if check.oracle_result
            },
        )
        message = repair_text(partial, key) or PACKAGE_REJECTION_ERROR
        return self._repair(result, message)

    @staticmethod
    def _legacy_decides(result: Any) -> Any:
        """The legacy verifier's rejection fails the attempt, as with the check package off.

        Only the provenance says the legacy verifier owns it: the failure class
        stays the legacy verdict's own, so retries and routing see exactly the
        legacy class (``BLOCKED`` stays ``BLOCKED``).
        """
        from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome

        if not legacy_verdict_in_tree(result)[0]:
            return result
        return replace(
            result,
            success=False,
            outcome=ACExecutionOutcome.FAILED,
            error=legacy_decided_error(result, "no admitted check"),
            check_package=CheckPackageProvenance(CheckPackageOwner.LEGACY_VERIFIER),
        )

    @staticmethod
    def _repair(result: Any, message: str) -> Any:
        from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome

        return replace(
            result,
            success=False,
            outcome=ACExecutionOutcome.FAILED,
            error=PACKAGE_REJECTION_ERROR,
            check_package=CheckPackageProvenance(CheckPackageOwner.CHECK_PACKAGE, repair=message),
        )


def _declaration_request_message(key: str, interface: Mapping[str, Any] | None) -> str:
    """Declaration-only repair: name the criterion and the grammar, nothing about the oracle."""
    return (
        f"The check package could not find the entry point of this criterion ({key}): the "
        "default name it looks for does not exist, and your evidence declared no "
        "entry_points. Keep your implementation unless it is incomplete, and emit the "
        "evidence JSON again with entry_points declared for this criterion."
        + entry_points_request(interface)
    )


def _binding_rejected_message(item: Any, package: Any) -> str:
    """Repair text for a declared entry point that was rejected (no oracle values)."""
    binding = item.binding or {}
    spec = next((o for o in package.oracles if o.check_id in item.check_ids), None)
    params = ", ".join(spec.call_params) if spec is not None else ""
    lines = [
        f"Your declared entry point for this criterion was rejected: {item.reason}.",
        f"Declared: {binding.get('call_kind')} {binding.get('symbol')}"
        + (
            f" with arg_map {json.dumps(binding.get('arg_map'), sort_keys=True)}"
            if binding.get("arg_map")
            else ""
        )
        + ".",
        "Fix the entry_points declaration: name a function your change introduced or "
        "changed (or one that exists at the base), outside .ouroboros_checks, whose "
        f"inputs ({params}) are mapped only by name or position.",
    ]
    return "\n".join(lines)


class CheckPackageAuthority:
    """The frozen check package as the acceptance authority of one run."""

    def __init__(
        self,
        state: BoundaryRunState,
        settings: CheckPackageSettings,
        *,
        event_store: EventStore,
        candidate_checkout: Path,
    ) -> None:
        if settings.check_timeout_seconds != state.contract.check_timeout_seconds:
            # The run contract recorded before the worker started decides every
            # check; settings resolved otherwise are a different run.
            raise ValueError("settings disagree with the run contract the run recorded")
        self._state = state
        # The run this authority belongs to; every call is checked against it.
        self._run = (state.execution_id, state.seed_digest, tuple(state.criterion_keys))
        self._settings = settings
        self._event_store = event_store
        self._candidate = candidate_checkout
        self.outcome: AuthorityOutcome | None = None
        self.gate = CheckPackageGate(self)
        self.installed = False
        # One base run per late binding across repair attempts and the end.
        self.base_runs: dict[str, Any] = {}
        # The worker's latest declared entry point per criterion. A later
        # attempt that declares nothing does not withdraw it: otherwise a
        # worker could turn a failing criterion into an unverified one by
        # omitting entry_points after a counterexample.
        self.declared: dict[str, list[Any]] = {}
        # The executor's same-runtime retry budget (set by ``install``).
        self.max_retry_attempts: int | None = None
        # Criteria that needed a late binding and had no declaration: asked
        # once for a declaration, or not asked because no retry was left.
        self.binding_requested: set[str] = set()
        self.binding_budget_exhausted: set[str] = set()
        # Set once the terminal verification starts (held-out cases may run).
        self._terminal_started = False

    def repair_follows(self, retry_attempt: int) -> bool:
        """Whether a repair attempt follows ``retry_attempt`` (unknown budget: yes)."""
        return self.max_retry_attempts is None or retry_attempt < self.max_retry_attempts

    def remember_declaration(self, key: str, entries: list[Any]) -> list[Any]:
        """Record ``entries`` for ``key`` when present; return the declaration in force."""
        if entries:
            self.declared[key] = list(entries)
        return self.declared.get(key, [])

    @property
    def state(self) -> BoundaryRunState:
        return self._state

    @property
    def settings(self) -> CheckPackageSettings:
        return self._settings

    @property
    def event_store(self) -> EventStore:
        return self._event_store

    @property
    def candidate(self) -> Path:
        return self._candidate.resolve()

    def interfaces(self) -> dict[int, dict[str, Any]]:
        """Root criterion index to the oracle's call kind and input names (no cases)."""
        package = self._state.package
        if package is None:
            return {}
        index = {key: number for number, key in enumerate(self._state.criterion_keys)}
        excluded = self._excluded()
        lost = self.legacy_decided_keys()
        return {
            index[spec.criterion_key]: spec.interface()
            for spec in package.oracles
            if spec.criterion_key in index
            and spec.check_id not in excluded
            and spec.criterion_key not in lost
        }

    def _exclusions(self) -> dict[str, str]:
        """The admission's excluded checks (check id to its recorded exclusion reason)."""
        admission = self._state.admission
        return dict((admission.excluded_checks if admission is not None else None) or {})

    def _excluded(self) -> frozenset[str]:
        return frozenset(self._exclusions())

    def admitted_check_ids(self, key: str) -> list[str]:
        """The admitted (not excluded) checks linked to criterion ``key``."""
        package = self._state.package
        if package is None:
            return []
        excluded = self._excluded()
        return [
            check.check_id
            for check in package.checks
            if check.check_id not in excluded
            and any(link.criterion_key == key for link in check.assertions)
        ]

    def legacy_decided_keys(self) -> frozenset[str]:
        """Criteria that lost their authority to per-check admission (``per_check.py``)."""
        package = self._state.package
        excluded = self._exclusions()
        if package is None or not excluded:
            return frozenset()
        return frozenset(criteria_without_admitted_check(package, excluded))

    def run_mismatch(self, seed: Seed, execution_id: str | None) -> str | None:
        """``run_mismatch:<field>`` when a call is not for this run, else ``None``."""
        return mismatched_run(self._run, seed, execution_id)

    def install(self, executor: Any) -> None:
        """Make the legacy verifier advisory and the package the repair signal.

        Only with an admitted package: without one the executor is left
        untouched, so the run is exactly the legacy run (legacy rejections
        fail attempts and drive retries), and the terminal decision
        reconciles every criterion as uncovered from the unmodified results.
        """
        if not self._state.admitted:
            return
        executor.check_package_gate = self.gate
        executor.check_package_interfaces = self.interfaces()
        budget = getattr(executor, "_ac_retry_attempts", None)
        if isinstance(budget, int) and not isinstance(budget, bool):
            self.max_retry_attempts = max(0, budget)
        self.installed = True

    async def __call__(self, *, seed: Seed, execution_id: str, parallel_result: Any) -> Any:
        if self.outcome is not None:
            # One verdict per run: a second call (for example a resumed
            # parallel pass on the same runner) keeps the first decision.
            return parallel_result
        mismatch = self.run_mismatch(seed, execution_id)
        if mismatch is not None:
            return self._refuse_foreign_run(seed, execution_id, parallel_result, mismatch)
        if self._terminal_started:
            # An earlier terminal verification was interrupted after its
            # held-out cases may have reached the candidate: they decide nothing again.
            return await self._undecided(self._run[2], parallel_result, "terminal_interrupted")
        self._terminal_started = True
        try:
            return await self._decide_run(execution_id, parallel_result)
        finally:
            # Once the terminal verification starts, decided or interrupted
            # (cancelled included), nothing in this process re-derives the held-out cases.
            forget_live_state(self._state)

    def _refuse_foreign_run(
        self, seed: Seed, execution_id: str, parallel_result: Any, mismatch: str
    ) -> Any:
        """Another run's result: every criterion the package covers stays undecided.

        Nothing is recorded in this run's journal and this run's one decision
        is not used. When the criteria are not this run's, coverage is
        unknown, so every criterion counts as covered.
        """
        log.warning(
            "boundary.authority.foreign_run",
            execution_id=execution_id,
            boundary_id=self._state.boundary_id,
            mismatch=mismatch,
        )
        try:
            keys = seed_criterion_keys(seed)
        except Exception:  # noqa: BLE001 - no criteria can be read: fail every root
            return _fail_attempted(parallel_result)
        covered = self.covered_keys() if keys == self._run[2] else frozenset(keys)
        decided, _reconciliation, _legacy = decide_without_package(
            keys, covered, mismatch, parallel_result, gated=self.installed
        )
        return decided

    def covered_keys(self) -> frozenset[str]:
        """Criteria at least one admitted (not excluded) check covers."""
        package = self._state.package
        if package is None:
            return frozenset()
        return frozenset(
            key
            for key in self._state.criterion_keys
            if self.admitted_check_ids(key) and key not in self.legacy_decided_keys()
        )

    async def _decide_run(self, execution_id: str, parallel_result: Any) -> Any:
        # ``run_mismatch`` proved the Seed's criterion keys are this run's: the
        # Seed is not read again, so nothing here can raise outside the fail-closed path.
        keys = self._run[2]
        try:
            legacy = existing_outcomes_from_results(parallel_result, gated=self.installed)
            declared: Mapping[str, list[Any]] = {}
            for result in getattr(parallel_result, "results", ()) or ():
                index = getattr(result, "ac_index", -1)
                if not 0 <= index < len(keys):
                    continue
                entries = self.remember_declaration(keys[index], _declared_from(result))
                if entries:
                    declared = {**declared, keys[index]: entries}
            verdict = await verify_check_package(
                self._state,
                event_store=self._event_store,
                candidate_checkout=self._candidate,
                declared_entry_points=declared,
                base_run_cache=self.base_runs,
            )
            verdict = _label_missing_bindings(
                verdict, self.binding_requested, self.binding_budget_exhausted
            )
            # Without an admitted package ``verdict.verdicts`` is empty: every
            # criterion is uncovered and the legacy verifier decides it.
            reconciliation = reconcile_acceptance(
                keys,
                verdict.verdicts,
                legacy,
                existing_run_accepted=bool(parallel_result.all_succeeded),
                legacy_decides_unverified=True,
            )
            if verdict.package_id is None:
                reconciliation = _legacy_owned(reconciliation)
            else:
                await BoundaryLedger(self._event_store).record_acceptance_reconciled(
                    self._state.boundary_id,
                    package_id=verdict.package_id,
                    reconciliation=reconciliation.to_payload(),
                )
            self.outcome = AuthorityOutcome(
                _legacy_accepted(legacy),
                verdict=verdict,
                reconciliation=reconciliation,
                legacy=legacy,
            )
            return apply_reconciliation(parallel_result, reconciliation)
        except Exception as exc:  # noqa: BLE001 - fail closed below, never open
            log.warning(
                "boundary.authority.failed",
                execution_id=execution_id,
                boundary_id=self._state.boundary_id,
                error_type=type(exc).__name__,
            )
            return await self._undecided(keys, parallel_result, type(exc).__name__)

    async def _undecided(self, keys: tuple[str, ...], parallel_result: Any, error: str) -> Any:
        """Decide without the package: covered criteria undecided, the rest legacy-decided."""
        try:
            try:
                covered = self.covered_keys()
            except Exception:  # noqa: BLE001 - unknown coverage: everything is covered
                covered = frozenset(keys)
            decided, reconciliation, legacy = decide_without_package(
                keys,
                covered,
                f"{AUTHORITY_ERROR_PREFIX}{error}",
                parallel_result,
                gated=self.installed,
            )
        except Exception:  # noqa: BLE001 - no decision could be built: fail every root
            log.warning("boundary.authority.undecided_failed", error_type=error)
            self.outcome = AuthorityOutcome(False, error=error)
            return _fail_attempted(parallel_result)
        self.outcome = AuthorityOutcome(
            _legacy_accepted(legacy), reconciliation=reconciliation, error=error, legacy=legacy
        )
        package = self._state.package
        try:
            if package is not None:
                # Marked undecided: the journal accepts it without a candidate
                # verification only because it claims no verified status.
                payload = ReconciliationPayload.model_validate(
                    {
                        **reconciliation.to_dict(),
                        "undecided_reason": f"{AUTHORITY_ERROR_PREFIX}{error}",
                    }
                )
                await BoundaryLedger(self._event_store).record_acceptance_reconciled(
                    self._state.boundary_id,
                    package_id=package.package_id,
                    reconciliation=payload,
                )
        except Exception:  # noqa: BLE001 - the fail-closed decision stands unrecorded
            log.warning("boundary.authority.undecided_not_recorded", error_type=error)
        return decided


def decide_without_package(
    keys: Sequence[str],
    covered: Collection[str],
    reason: str,
    parallel_result: Any,
    *,
    gated: bool,
) -> tuple[Any, AcceptanceReconciliation, dict[int, ExistingOutcome]]:
    """The decision when the package cannot decide: covered undecided, the rest legacy.

    Every criterion in ``covered`` is indeterminate with ``reason`` (not
    accepted); every other criterion is uncovered, so the legacy verifier
    decides it. Shared by the live authority after an error and by a resumed
    run without a usable package. Returns the decided executor result, the
    reconciliation and the legacy verdicts it used.
    """
    verdicts = {
        key: (
            CriterionVerdict(
                key,
                PackageCriterionStatus.INDETERMINATE,
                CheckTier.A,
                reason,
                declared_binding_pass=False,
            )
            if key in covered
            else CriterionVerdict(
                key,
                PackageCriterionStatus.UNCOVERED,
                CheckTier.U,
                "uncovered",
                declared_binding_pass=False,
            )
        )
        for key in keys
    }
    legacy = existing_outcomes_from_results(parallel_result, gated=gated)
    reconciliation = reconcile_acceptance(
        keys,
        verdicts,
        legacy,
        existing_run_accepted=bool(parallel_result.all_succeeded),
        legacy_decides_unverified=True,
    )
    return apply_reconciliation(parallel_result, reconciliation), reconciliation, legacy


def _legacy_accepted(legacy: Mapping[int, ExistingOutcome]) -> bool:
    return bool(legacy) and all(item.passed for item in legacy.values())


def _label_missing_bindings(
    verdict: BoundaryVerdict, requested: set[str], exhausted: set[str]
) -> BoundaryVerdict:
    """Say why a criterion that needed a late binding still has none.

    ``no_binding_after_request``: the worker was asked once and declared
    nothing valid; ``no_binding_budget_exhausted``: no retry was left to ask.
    The criterion stays unverified either way.
    """
    relabeled = {}
    for key, item in verdict.verdicts.items():
        if item.reason != NO_BINDING or not item.status.is_unverified:
            continue
        if key in requested:
            relabeled[key] = replace(item, reason=NO_BINDING_AFTER_REQUEST)
        elif key in exhausted:
            relabeled[key] = replace(item, reason=NO_BINDING_BUDGET_EXHAUSTED)
    if not relabeled:
        return verdict
    return replace(verdict, verdicts={**verdict.verdicts, **relabeled})


def _fail_attempted(parallel_result: Any) -> Any:
    """Every root result failed: no verifier could decide the run (fail closed)."""
    from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome

    results = tuple(
        result
        if result.outcome is ACExecutionOutcome.FAILED
        else replace(
            result,
            success=False,
            outcome=ACExecutionOutcome.FAILED,
            error=PACKAGE_INDETERMINATE_ERROR,
        )
        for result in parallel_result.results
    )
    return replace(
        parallel_result,
        results=results,
        success_count=0,
        failure_count=len(results),
        externally_satisfied_count=0,
    )


__all__ = [
    "PACKAGE_FAILURE_CLASS_PREFIX",
    "PACKAGE_INDETERMINATE_ERROR",
    "PACKAGE_REJECTION_ERROR",
    "AuthorityOutcome",
    "CheckPackageAuthority",
    "CheckPackageGate",
    "apply_reconciliation",
    "decide_without_package",
    "existing_outcomes_from_results",
    "legacy_verdict_in_tree",
]
