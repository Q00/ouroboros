"""Acceptance authority inside the runner: the frozen check package decides.

``OrchestratorRunner`` installs the authority on its parallel executor
(``install``) and calls it once on the executor's result, after the worker
has stopped and before the terminal acceptance plan and the session status
are persisted (``OrchestratorRunner.acceptance_authority``).

While the worker runs (``CheckPackageGate``, installed as the executor's
``check_package_gate``):

- the legacy per-criterion verifier (typed evidence plus the transcript
  verifier) no longer rejects an attempt; its verdict stays on the result as
  an annotation (advisory reason and failure class for telemetry), so it
  triggers no retry;
- after each attempt of a root criterion the gate runs that criterion's
  checks on the workspace, through the default binding or the entry point
  the worker declared in its evidence; a fail marks the attempt failed with
  the counterexample (``ACExecutionResult.check_package_repair``), which the
  executor's retry loop carries into the next attempt. A failure through a
  worker-declared binding names that binding. Held-out inputs are withheld.
  Nothing else drives a retry.

After the worker stops (``CheckPackageAuthority.__call__``):

1. ``verify_check_package`` records the final bindings, verifies the
   finished workspace, and records verification and selection;
2. ``reconcile_acceptance`` decides every criterion (pass and unverified
   accept, fail and indeterminate reject, a criterion the worker never
   attempted is not accepted); the legacy verdict is kept per criterion as
   advisory; ``boundary.acceptance.reconciled`` records it;
3. the executor result is returned with each root result's ``success`` and
   ``outcome`` set to the decision and the counts recomputed, so the durable
   status, the panel, the exit code, and ``workflow_outcome`` carry it.

Neither part ever raises into the run: on an error the gate returns the
attempt unchanged and the authority returns the executor result unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.boundary.acceptance import (
    AcceptanceReconciliation,
    ExistingOutcome,
    PackageCriterionStatus,
    criterion_verdicts,
    reconcile_acceptance,
)
from ouroboros.boundary.binding import declared_entry_points
from ouroboros.boundary.binding_flow import assign_tiers, bindings_payload, verify_with_bindings
from ouroboros.boundary.check_env import resolve_check_interpreter, scrubbed_check_environment
from ouroboros.boundary.ledger import BoundaryLedger
from ouroboros.boundary.package import seed_criterion_keys
from ouroboros.boundary.run_wiring import (
    BoundaryRunState,
    BoundaryVerdict,
    CheckPackageSettings,
    _base_manifest,
    _counterexamples,
    repair_message,
    verify_check_package,
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
PACKAGE_FAILURE_CLASS_PREFIX = "CHECK_PACKAGE_FAIL"


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


def existing_outcomes_from_results(
    parallel_result: Any, *, gated: bool = False
) -> dict[int, ExistingOutcome]:
    """Per root criterion: whether the worker attempted it, and the legacy verdict.

    ``outcome`` carries the legacy verifier's verdict (``failed`` when it
    rejected the attempt, whatever the executor did with that rejection) and
    ``failure_class`` its class. With the gate installed (``gated``), an
    attempt counts as made when the executor result succeeded or failed only
    because the package gate failed it; a runtime failure, a blocked or
    invalid criterion, or a failed ``verify_command`` is not an attempt the
    package may accept. Without the gate, a failed result is a legacy
    rejection of an attempt.
    """
    outcomes: dict[int, ExistingOutcome] = {}
    for result in getattr(parallel_result, "results", ()) or ():
        index = getattr(result, "ac_index", None)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        base = getattr(getattr(result, "outcome", None), "value", None)
        if not isinstance(base, str):
            base = "succeeded" if getattr(result, "success", False) else "failed"
        verdict = getattr(result, "atomic_verifier_verdict", None)
        legacy_rejected = verdict is not None and not bool(getattr(verdict, "passed", True))
        failure_class = getattr(verdict, "failure_class", None) if legacy_rejected else None
        if gated:
            judged = bool(getattr(result, "success", False)) or bool(
                getattr(result, "check_package_failure_class", None)
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
        outcomes[index] = ExistingOutcome(
            root_ac_index=index,
            outcome=outcome,
            disposition="accepted" if outcome in _ACCEPTED_OUTCOMES else outcome,
            terminal_status=terminal,
            failure_class=failure_class,
        )
    return outcomes


def apply_reconciliation(parallel_result: Any, reconciliation: AcceptanceReconciliation) -> Any:
    """Return ``parallel_result`` with every root result set to its decision."""
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
        if decision.accepted and previous is ACExecutionOutcome.FAILED:
            results.append(
                replace(result, success=True, outcome=ACExecutionOutcome.SUCCEEDED, error=None)
            )
            success_delta += 1
            failure_delta -= 1
            changed = True
        elif not decision.accepted and previous in (
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.SATISFIED_EXTERNALLY,
        ):
            error = (
                PACKAGE_REJECTION_ERROR
                if decision.package_status is PackageCriterionStatus.FAIL
                else f"{PACKAGE_INDETERMINATE_ERROR} ({decision.reason})"
            )
            results.append(
                replace(result, success=False, outcome=ACExecutionOutcome.FAILED, error=error)
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


class CheckPackageGate:
    """Per-attempt repair signal from the frozen package (see the module docstring)."""

    def __init__(self, authority: CheckPackageAuthority) -> None:
        self._authority = authority
        self.log: list[dict[str, Any]] = []
        # One decision per attempt: settlement paths hand the same attempt to
        # the gate again; they get the stored decision, not a new verification.
        self._decided: dict[tuple[int, int], dict[str, Any] | None] = {}

    async def __call__(self, *, seed: Seed, ac_index: int, result: Any) -> Any:
        attempt = (ac_index, int(getattr(result, "retry_attempt", 0) or 0))
        if attempt in self._decided:
            stored = self._decided[attempt]
            return result if stored is None or not result.success else replace(result, **stored)
        try:
            decided = await self._decide(ac_index, result)
            if decided is result:
                self._decided[attempt] = None
            elif getattr(decided, "check_package_repair", None):
                self._decided[attempt] = {
                    "success": False,
                    "outcome": decided.outcome,
                    "error": decided.error,
                    "check_package_repair": decided.check_package_repair,
                    "check_package_failure_class": decided.check_package_failure_class,
                }
            return decided
        except Exception as exc:  # noqa: BLE001 - the gate must never fail an attempt by itself
            log.warning("boundary.gate.failed", ac_index=ac_index, error_type=type(exc).__name__)
            return result

    async def _decide(self, ac_index: int, result: Any) -> Any:
        from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome

        authority = self._authority
        state = authority.state
        package = state.package
        if package is None or state.admission is None or not getattr(result, "success", False):
            return result
        keys = state.criterion_keys
        if not 0 <= ac_index < len(keys):
            return result
        key = keys[ac_index]
        check_ids = [
            check.check_id
            for check in package.checks
            if any(link.criterion_key == key for link in check.assertions)
        ]
        if not check_ids:
            return result
        entries = authority.remember_declaration(key, _declared_from(result))
        options = authority.run_options()
        assignments, results = await assign_tiers(
            package,
            artifact=authority.candidate,
            base=state.base_snapshot,
            declared={key: entries} if entries else None,
            base_manifest=_base_manifest(state),
            expected_base_digest=state.admission.base_tree_digest,
            admitted_tiers=state.admission.check_tiers,
            run_options={"env": options["env"], "interpreter": options["interpreter"]},
            base_run_cache=authority.base_runs,
        )
        subset = {check_id: assignments[check_id] for check_id in check_ids}
        bound = await verify_with_bindings(
            package,
            authority.candidate,
            subset,
            timeout_seconds=authority.settings.check_timeout_seconds,
            **options,
        )
        verification = bound.effective
        verdicts = criterion_verdicts(package, verification, assignments=subset)
        item = verdicts[key]
        await BoundaryLedger(authority.event_store).record_bindings(
            state.boundary_id,
            package_sha256=package.sha256,
            payload={
                **bindings_payload(
                    subset,
                    {k: v for k, v in results.items() if k in subset},
                    phase="repair",
                ),
                "root_ac_index": ac_index,
                "retry_attempt": getattr(result, "retry_attempt", 0),
                "status": item.status.value,
            },
        )
        self.log.append(
            {"ac_index": ac_index, "status": item.status.value, "tier": item.tier.value}
        )
        if item.status is not PackageCriterionStatus.FAIL:
            return result
        partial = BoundaryVerdict(
            verdict="fail",
            reasons=(),
            boundary_id=state.boundary_id,
            package_sha256=package.sha256,
            counterexamples=_counterexamples(verification) if verification is not None else (),
            verdicts=verdicts,
            oracle_results={
                check.check_id: check.oracle_result
                for check in (verification.checks if verification is not None else ())
                if check.oracle_result
            },
        )
        message = repair_message(partial, key) or PACKAGE_REJECTION_ERROR
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:12]
        return replace(
            result,
            success=False,
            outcome=ACExecutionOutcome.FAILED,
            error=PACKAGE_REJECTION_ERROR,
            check_package_repair=message,
            check_package_failure_class=f"{PACKAGE_FAILURE_CLASS_PREFIX}:{digest}",
        )


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
        self._state = state
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

    def run_options(self) -> dict[str, Any]:
        interpreter = self._state.interpreter or resolve_check_interpreter(self.candidate)
        return {
            "env": scrubbed_check_environment(),
            "interpreter": interpreter.path,
            "interpreter_source": interpreter.source,
        }

    def interfaces(self) -> dict[int, dict[str, Any]]:
        """Root criterion index to the oracle's call kind and input names (no cases)."""
        package = self._state.package
        if package is None:
            return {}
        index = {key: number for number, key in enumerate(self._state.criterion_keys)}
        return {
            index[spec.criterion_key]: spec.interface()
            for spec in package.oracles
            if spec.criterion_key in index
        }

    def install(self, executor: Any) -> None:
        """Make the legacy verifier advisory and the package the repair signal."""
        executor.check_package_gate = self.gate
        executor.check_package_interfaces = self.interfaces()
        self.installed = True

    async def __call__(self, *, seed: Seed, execution_id: str, parallel_result: Any) -> Any:
        if self.outcome is not None:
            # One verdict per run: a second call (for example a resumed
            # parallel pass on the same runner) keeps the first decision.
            return parallel_result
        legacy = existing_outcomes_from_results(parallel_result, gated=self.installed)
        legacy_accepted = bool(legacy) and all(item.passed for item in legacy.values())
        try:
            declared: Mapping[str, list[Any]] = {}
            keys = seed_criterion_keys(seed)
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
                settings=self._settings,
                declared_entry_points=declared,
                base_run_cache=self.base_runs,
            )
            reconciliation = reconcile_acceptance(
                keys,
                verdict.verdicts,
                legacy,
                existing_run_accepted=bool(parallel_result.all_succeeded),
            )
            await BoundaryLedger(self._event_store).record_acceptance_reconciled(
                self._state.boundary_id,
                package_sha256=verdict.package_sha256,
                reconciliation=reconciliation.to_dict(),
            )
            self.outcome = AuthorityOutcome(
                legacy_accepted, verdict=verdict, reconciliation=reconciliation, legacy=legacy
            )
            return apply_reconciliation(parallel_result, reconciliation)
        except Exception as exc:  # noqa: BLE001 - the executor result must survive any failure
            log.warning(
                "boundary.authority.failed",
                execution_id=execution_id,
                boundary_id=self._state.boundary_id,
                error_type=type(exc).__name__,
            )
            self.outcome = AuthorityOutcome(
                legacy_accepted, error=type(exc).__name__, legacy=legacy
            )
            return parallel_result


__all__ = [
    "PACKAGE_FAILURE_CLASS_PREFIX",
    "PACKAGE_INDETERMINATE_ERROR",
    "PACKAGE_REJECTION_ERROR",
    "AuthorityOutcome",
    "CheckPackageAuthority",
    "CheckPackageGate",
    "apply_reconciliation",
    "existing_outcomes_from_results",
]
