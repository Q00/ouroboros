"""Acceptance authority: the admitted check package decides the criteria it covers.

Policy:
executable verification is authoritative and semantic or evidence-form
judgments are advisory. With the check package on, each acceptance criterion
is decided as follows once the worker has stopped:

- the package covers the criterion and every linked check passed on the
  candidate: the criterion is accepted, even when the existing per-criterion
  verifier rejected it (for example with an evidence-form mismatch);
- the package covers the criterion and a linked check failed with a
  counterexample: the criterion fails, even when the existing verifier
  accepted it;
- the package is indeterminate for the criterion, or does not cover it: the
  existing verifier's verdict stands.

The existing verifier's verdict is always recorded next to the package's, as
advisory. A package pass cannot resurrect a criterion the existing harness
never judged (blocked, invalid, cancelled, or missing), because there is no
worker attempt to accept.

This module only decides. ``boundary/authority.py`` applies the decision
inside the runner, before the terminal acceptance plan and the session status
are persisted, and records it as ``boundary.acceptance.reconciled`` on the
boundary aggregate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ouroboros.boundary.admission import CandidateVerification, CheckStatus

if TYPE_CHECKING:
    from ouroboros.boundary.package import CheckPackage
    from ouroboros.persistence.event_store import EventStore

ACCEPTANCE_FINAL_EVENT_TYPE = "execution.ac.acceptance_finalized"
_EXISTING_PASS_OUTCOMES = frozenset({"succeeded", "satisfied_externally"})


class PackageCriterionStatus(StrEnum):
    """The package's verdict for one criterion on the candidate."""

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"
    UNCOVERED = "uncovered"


class Governor(StrEnum):
    """Which signal decided a criterion."""

    CHECK_PACKAGE = "check_package"
    EXISTING_VERIFIER = "existing_verifier"


def package_criterion_statuses(
    package: CheckPackage,
    verification: CandidateVerification,
    *,
    candidate_identity_ok: bool = True,
) -> dict[str, PackageCriterionStatus]:
    """Map every criterion key to the package's verdict for it.

    A criterion no assertion links to is ``uncovered``. A covered criterion
    fails when any linked check was violated, passes when every linked check
    ran and met its contract, and is otherwise indeterminate. A protected-byte
    mutation, a precondition failure (no check executed), or a candidate tree
    that changed under verification makes every covered criterion
    indeterminate.
    """
    linked: dict[str, set[str]] = {key: set() for key in package.criterion_keys}
    for check in package.checks:
        for link in check.assertions:
            linked.setdefault(link.criterion_key, set()).add(check.check_id)
    by_id = {check.check_id: check.status for check in verification.checks}
    trusted = (
        candidate_identity_ok
        and not verification.protected_bytes_mutated
        and verification.package_sha256 == package.sha256
        and bool(verification.checks)
    )
    statuses: dict[str, PackageCriterionStatus] = {}
    for key, check_ids in linked.items():
        if not check_ids:
            statuses[key] = PackageCriterionStatus.UNCOVERED
        elif not trusted:
            statuses[key] = PackageCriterionStatus.INDETERMINATE
        elif any(by_id.get(check_id) is CheckStatus.VIOLATED for check_id in check_ids):
            statuses[key] = PackageCriterionStatus.FAIL
        elif all(by_id.get(check_id) is CheckStatus.EXPECTED for check_id in check_ids):
            statuses[key] = PackageCriterionStatus.PASS
        else:
            statuses[key] = PackageCriterionStatus.INDETERMINATE
    return statuses


@dataclass(frozen=True, slots=True)
class ExistingOutcome:
    """The existing harness's final decision for one root criterion."""

    root_ac_index: int
    outcome: str
    disposition: str
    terminal_status: str

    @property
    def passed(self) -> bool:
        """The existing per-criterion verifier accepted the worker's attempt."""
        return self.outcome in _EXISTING_PASS_OUTCOMES

    @property
    def rejected_attempt(self) -> bool:
        """The worker attempted the criterion and the existing verifier rejected it."""
        return self.outcome == "failed" and self.terminal_status == "failed"


def existing_outcomes_from_events(events: Sequence[Any]) -> dict[int, ExistingOutcome]:
    """Extract the latest final acceptance decision per root criterion."""
    outcomes: dict[int, ExistingOutcome] = {}
    for event in events:
        if getattr(event, "type", None) != ACCEPTANCE_FINAL_EVENT_TYPE:
            continue
        data = getattr(event, "data", None) or {}
        index = data.get("root_ac_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            continue
        outcomes[index] = ExistingOutcome(
            root_ac_index=index,
            outcome=str(data.get("outcome") or ""),
            disposition=str(data.get("disposition") or ""),
            terminal_status=str(data.get("terminal_status") or ""),
        )
    return outcomes


async def load_existing_outcomes(
    event_store: EventStore, execution_id: str
) -> dict[int, ExistingOutcome]:
    """Read the run's ``execution.ac.acceptance_finalized`` decisions."""
    return existing_outcomes_from_events(await event_store.replay("execution", execution_id))


@dataclass(frozen=True, slots=True)
class CriterionDecision:
    """Final acceptance of one criterion and the signals behind it."""

    root_ac_index: int
    criterion_key: str
    package_status: PackageCriterionStatus
    existing_outcome: str | None
    existing_accepted: bool
    accepted: bool
    governed_by: Governor

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_ac_index": self.root_ac_index,
            "criterion_key": self.criterion_key,
            "package_status": self.package_status.value,
            "existing_outcome": self.existing_outcome,
            "existing_accepted": self.existing_accepted,
            "accepted": self.accepted,
            "governed_by": self.governed_by.value,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceReconciliation:
    """Per-criterion decisions plus whether the run as a whole is accepted."""

    decisions: tuple[CriterionDecision, ...]
    run_accepted: bool
    existing_run_accepted: bool

    @property
    def overridden(self) -> tuple[CriterionDecision, ...]:
        """Criteria where the package's verdict replaced the existing one."""
        return tuple(d for d in self.decisions if d.accepted != d.existing_accepted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "ouroboros.acceptance_reconciliation.v1",
            "run_accepted": self.run_accepted,
            "existing_run_accepted": self.existing_run_accepted,
            "criteria": [decision.to_dict() for decision in self.decisions],
        }


def reconcile_acceptance(
    criterion_keys: Sequence[str],
    package_statuses: Mapping[str, PackageCriterionStatus],
    existing: Mapping[int, ExistingOutcome],
    *,
    existing_run_accepted: bool,
) -> AcceptanceReconciliation:
    """Apply the authority rule to every root criterion, in Seed order."""
    decisions: list[CriterionDecision] = []
    for index, key in enumerate(criterion_keys):
        status = package_statuses.get(key, PackageCriterionStatus.UNCOVERED)
        prior = existing.get(index)
        # A run the existing harness completed accepted every root criterion,
        # so a missing per-criterion record cannot mean rejection there.
        existing_accepted = prior.passed if prior is not None else existing_run_accepted
        if status is PackageCriterionStatus.FAIL:
            accepted, governor = False, Governor.CHECK_PACKAGE
        elif status is PackageCriterionStatus.PASS and (
            existing_accepted or (prior is not None and prior.rejected_attempt)
        ):
            accepted, governor = True, Governor.CHECK_PACKAGE
        else:
            accepted, governor = existing_accepted, Governor.EXISTING_VERIFIER
        decisions.append(
            CriterionDecision(
                root_ac_index=index,
                criterion_key=key,
                package_status=status,
                existing_outcome=None if prior is None else prior.outcome,
                existing_accepted=existing_accepted,
                accepted=accepted,
                governed_by=governor,
            )
        )
    run_accepted = bool(decisions) and all(decision.accepted for decision in decisions)
    return AcceptanceReconciliation(
        decisions=tuple(decisions),
        run_accepted=run_accepted,
        existing_run_accepted=existing_run_accepted,
    )


def render_reconciliation(reconciliation: AcceptanceReconciliation) -> list[str]:
    """Plain-text lines: one per criterion, the existing verdict marked advisory."""
    lines: list[str] = []
    for decision in reconciliation.decisions:
        verdict = "accepted" if decision.accepted else "not accepted"
        existing = decision.existing_outcome or "no decision"
        if decision.governed_by is Governor.CHECK_PACKAGE:
            lines.append(
                f"AC {decision.root_ac_index + 1}: {verdict} by the check package "
                f"({decision.package_status.value}); existing verifier (advisory): {existing}"
            )
        else:
            lines.append(
                f"AC {decision.root_ac_index + 1}: {verdict} by the existing verifier "
                f"({existing}); check package: {decision.package_status.value}"
            )
    return lines
