"""Acceptance authority inside the runner, before the terminal status is persisted.

``OrchestratorRunner`` calls an installed authority once, on the parallel
executor's result, after the worker has stopped and before the runner builds
the terminal acceptance plan and persists the session status
(``OrchestratorRunner.acceptance_authority``). ``CheckPackageAuthority``:

1. verifies the finished workspace against the frozen, admitted package
   (``verify_check_package``: ``boundary.candidate.verified`` and
   ``boundary.selection.decided``);
2. reads the legacy verdict of every root criterion from the executor's own
   results (the per-criterion verifier that decides without the package);
3. applies the rule in ``boundary/acceptance.py`` (the package decides the
   criteria it covers; indeterminate or uncovered criteria keep the legacy
   verdict) and records ``boundary.acceptance.reconciled``, which keeps the
   legacy verdict per criterion as advisory;
4. returns the executor result with each overridden root criterion's
   ``success`` and ``outcome`` replaced and the counts recomputed.

Because this happens before the terminal plan is built, the durable
``execution.ac.acceptance_finalized`` records, the session status (``ooo
status``, MCP status tools), the completion panel, and ``workflow_outcome``
all carry the reconciled decision. The worker never sees any of it: the
worker has already stopped, and nothing here feeds a retry.

The authority never raises into the run. On any error it records the error
and returns the executor result unchanged, so the legacy verdict stands.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.boundary.acceptance import (
    AcceptanceReconciliation,
    ExistingOutcome,
    reconcile_acceptance,
)
from ouroboros.boundary.ledger import BoundaryLedger
from ouroboros.boundary.package import seed_criterion_keys
from ouroboros.boundary.run_wiring import (
    BoundaryRunState,
    BoundaryVerdict,
    CheckPackageSettings,
    verify_check_package,
)

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

_ACCEPTED_OUTCOMES = frozenset({"succeeded", "satisfied_externally"})
PACKAGE_REJECTION_ERROR = "check_package: the finished workspace fails the frozen check package"


@dataclass(frozen=True, slots=True)
class AuthorityOutcome:
    """What the authority decided for one run."""

    legacy_run_accepted: bool
    verdict: BoundaryVerdict | None = None
    reconciliation: AcceptanceReconciliation | None = None
    error: str | None = None

    @property
    def package_decided(self) -> bool:
        """Whether the package governed at least one criterion."""
        if self.reconciliation is None:
            return False
        return any(
            decision.governed_by.value == "check_package"
            for decision in self.reconciliation.decisions
        )


def existing_outcomes_from_results(parallel_result: Any) -> dict[int, ExistingOutcome]:
    """The legacy verdict per root criterion, from the executor's results."""
    outcomes: dict[int, ExistingOutcome] = {}
    for result in getattr(parallel_result, "results", ()) or ():
        index = getattr(result, "ac_index", None)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        outcome = getattr(getattr(result, "outcome", None), "value", None)
        if not isinstance(outcome, str):
            outcome = "succeeded" if getattr(result, "success", False) else "failed"
        outcomes[index] = ExistingOutcome(
            root_ac_index=index,
            outcome=outcome,
            disposition="accepted" if outcome in _ACCEPTED_OUTCOMES else outcome,
            # A failed root result is a worker attempt the legacy verifier
            # rejected; that is the only case a package pass may override.
            terminal_status="failed" if outcome == "failed" else "completed",
        )
    return outcomes


def apply_reconciliation(parallel_result: Any, reconciliation: AcceptanceReconciliation) -> Any:
    """Return ``parallel_result`` with overridden root results and recomputed counts."""
    from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome

    overrides: Mapping[int, bool] = {
        decision.root_ac_index: decision.accepted for decision in reconciliation.overridden
    }
    if not overrides:
        return parallel_result
    results = []
    success_delta = failure_delta = external_delta = 0
    for result in parallel_result.results:
        accepted = overrides.get(result.ac_index)
        if accepted is None:
            results.append(result)
            continue
        previous = result.outcome
        if accepted and previous is ACExecutionOutcome.FAILED:
            results.append(
                replace(result, success=True, outcome=ACExecutionOutcome.SUCCEEDED, error=None)
            )
            success_delta += 1
            failure_delta -= 1
        elif not accepted and previous in (
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.SATISFIED_EXTERNALLY,
        ):
            results.append(
                replace(
                    result,
                    success=False,
                    outcome=ACExecutionOutcome.FAILED,
                    error=PACKAGE_REJECTION_ERROR,
                )
            )
            failure_delta += 1
            if previous is ACExecutionOutcome.SUCCEEDED:
                success_delta -= 1
            else:
                external_delta -= 1
        else:
            results.append(result)
    return replace(
        parallel_result,
        results=tuple(results),
        success_count=parallel_result.success_count + success_delta,
        failure_count=parallel_result.failure_count + failure_delta,
        externally_satisfied_count=parallel_result.externally_satisfied_count + external_delta,
    )


class CheckPackageAuthority:
    """The check package as the acceptance authority for the criteria it covers."""

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

    async def __call__(self, *, seed: Seed, execution_id: str, parallel_result: Any) -> Any:
        if self.outcome is not None:
            # One verdict per run: a second call (for example a resumed
            # parallel pass on the same runner) keeps the first decision.
            return parallel_result
        legacy_accepted = bool(parallel_result.all_succeeded)
        try:
            verdict = await verify_check_package(
                self._state,
                event_store=self._event_store,
                candidate_checkout=self._candidate,
                settings=self._settings,
            )
            if verdict.package_sha256 is None or not verdict.criteria:
                self.outcome = AuthorityOutcome(legacy_accepted, verdict=verdict)
                return parallel_result
            reconciliation = reconcile_acceptance(
                seed_criterion_keys(seed),
                verdict.criteria,
                existing_outcomes_from_results(parallel_result),
                existing_run_accepted=legacy_accepted,
            )
            await BoundaryLedger(self._event_store).record_acceptance_reconciled(
                self._state.boundary_id,
                package_sha256=verdict.package_sha256,
                reconciliation=reconciliation.to_dict(),
            )
            self.outcome = AuthorityOutcome(
                legacy_accepted, verdict=verdict, reconciliation=reconciliation
            )
            return apply_reconciliation(parallel_result, reconciliation)
        except Exception as exc:  # noqa: BLE001 - the legacy verdict must survive any failure
            log.warning(
                "boundary.authority.failed",
                execution_id=execution_id,
                boundary_id=self._state.boundary_id,
                error_type=type(exc).__name__,
            )
            self.outcome = AuthorityOutcome(legacy_accepted, error=type(exc).__name__)
            return parallel_result


__all__ = [
    "PACKAGE_REJECTION_ERROR",
    "AuthorityOutcome",
    "CheckPackageAuthority",
    "apply_reconciliation",
    "existing_outcomes_from_results",
]
