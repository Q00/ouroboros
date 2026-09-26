"""Acceptance authority: the admitted check package decides every criterion.

Policy (check package on). Executable verification is authoritative; the
existing per-criterion verifier (typed evidence plus the runtime-transcript
verifier) only annotates: its verdict and failure class are recorded next to
the package's, and they decide nothing. Each criterion gets one package
status once the worker has stopped:

- ``pass``: every linked check ran through a validated binding (tier ``A`` or
  ``A_prime``) and met its contract, held-out cases included;
- ``fail``: a linked check was violated; its counterexample is reported;
- ``indeterminate``: a check could not be judged (timeout, launch failure,
  protected-byte mutation, no failure signature, untrusted verification), or
  a worker-declared binding was invalid (``binding_invalid:*``,
  ``binding_admission_timeout``);
- ``unverified``: a check exists but no binding reaches the target
  (``no_binding``), or a linked check was not run for that reason;
- ``uncovered``: no executable check exists for the criterion (tier ``U``).

``unverified`` and ``uncovered`` are both "unverified": they never count as a
pass in any aggregate, and they never fail a run. A criterion is accepted
when the worker attempted it and its status is ``pass``, ``unverified`` or
``uncovered``. A criterion nobody attempted (blocked, invalid, cancelled, or
missing on a failed run) is not accepted, whatever the package says.

Artifact verdict (precedence): ``fail`` if any criterion fails; else
``indeterminate`` if any is indeterminate; else ``pass`` if at least one
criterion has a verified pass; else ``unverified`` (all unverified). A run
exits 0 exactly when every criterion is accepted, that is, no failure and no
indeterminate criterion is left; the unverified ones are listed.

This module only decides. The caller records the decision as
``boundary.acceptance.reconciled`` on the boundary aggregate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ouroboros.boundary.admission import CandidateVerification, CheckExecution, CheckStatus
from ouroboros.boundary.binding import CheckTier, TierAssignment, tier_summary
from ouroboros.boundary.oracle import failed_heldout_only

if TYPE_CHECKING:
    from ouroboros.boundary.package import CheckPackage
    from ouroboros.persistence.event_store import EventStore

ACCEPTANCE_FINAL_EVENT_TYPE = "execution.ac.acceptance_finalized"
RECONCILIATION_SCHEMA = "ouroboros.acceptance_reconciliation.v2"
_EXISTING_PASS_OUTCOMES = frozenset({"succeeded", "satisfied_externally"})


class PackageCriterionStatus(StrEnum):
    """The package's verdict for one criterion on the candidate."""

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"
    UNVERIFIED = "unverified"
    UNCOVERED = "uncovered"

    @property
    def is_unverified(self) -> bool:
        """``unverified`` or ``uncovered``: never a pass, never a failure."""
        return self in (PackageCriterionStatus.UNVERIFIED, PackageCriterionStatus.UNCOVERED)


class Governor(StrEnum):
    """Which signal decided a criterion."""

    CHECK_PACKAGE = "check_package"
    EXECUTION = "execution"
    # Kept for reading receipts recorded before the legacy verifier became
    # advisory; no decision made by this module uses it.
    EXISTING_VERIFIER = "existing_verifier"


class ArtifactVerdict(StrEnum):
    """Artifact-level verdict, by precedence."""

    FAIL = "fail"
    INDETERMINATE = "indeterminate"
    PASS = "pass"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class CriterionVerdict:
    """The package's verdict for one criterion and what it rests on."""

    criterion_key: str
    status: PackageCriterionStatus
    tier: CheckTier
    reason: str
    check_ids: tuple[str, ...] = ()
    failed_heldout_only: bool = False
    binding: dict[str, Any] | None = None
    binding_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_key": self.criterion_key,
            "status": self.status.value,
            "tier": self.tier.value,
            "reason": self.reason,
            "check_ids": list(self.check_ids),
            "failed_heldout_only": self.failed_heldout_only,
            "binding": self.binding,
            "binding_source": self.binding_source,
        }


_TRANSIENT_REASONS = frozenset(
    {"timeout", "launch_failed", "protected_bytes_mutated", "failure_signature_absent"}
)


def rerunnable_checks(verification: CandidateVerification) -> tuple[str, ...]:
    """Checks whose indeterminate result one zero-model re-run may resolve (R3)."""
    return tuple(
        check.check_id
        for check in verification.checks
        if check.status is CheckStatus.INDETERMINATE and check.reason in _TRANSIENT_REASONS
    )


def _weakest(tiers: Iterable[CheckTier]) -> CheckTier:
    values = set(tiers)
    for tier in (CheckTier.C, CheckTier.U, CheckTier.A_PRIME):
        if tier in values:
            return tier
    return CheckTier.A


def criterion_verdicts(
    package: CheckPackage,
    verification: CandidateVerification | None,
    *,
    assignments: Mapping[str, TierAssignment] | None = None,
    candidate_identity_ok: bool = True,
) -> dict[str, CriterionVerdict]:
    """Every criterion's package verdict, with its tier.

    ``assignments`` maps a check id to its tier assignment; a check without
    one is tier ``A`` and was expected to run. A check assigned ``U`` is not
    run: ``status_hint`` ``unverified`` makes the criterion unverified and
    ``indeterminate`` (an invalid declared binding) makes it indeterminate.
    A protected-byte mutation, a precondition failure (no check executed), a
    package digest mismatch, or a candidate tree that changed under
    verification makes every check that should have run indeterminate.
    """
    assignments = assignments or {}
    linked: dict[str, list[str]] = {key: [] for key in package.criterion_keys}
    for check in package.checks:
        for key in sorted({link.criterion_key for link in check.assertions}):
            linked.setdefault(key, []).append(check.check_id)
    uncovered = {item.criterion_key: item.reason for item in package.uncovered}
    executions: dict[str, CheckExecution] = (
        {check.check_id: check for check in verification.checks} if verification else {}
    )
    trusted = (
        verification is not None
        and candidate_identity_ok
        and not verification.protected_bytes_mutated
        and verification.package_sha256 == package.sha256
        and bool(verification.checks)
    )
    verdicts: dict[str, CriterionVerdict] = {}
    for key, check_ids in linked.items():
        if not check_ids:
            verdicts[key] = CriterionVerdict(
                key,
                PackageCriterionStatus.UNCOVERED,
                CheckTier.U,
                f"uncovered:{uncovered.get(key, 'no_check')}",
            )
            continue
        tiers: list[CheckTier] = []
        failed: list[CheckExecution] = []
        undecided: list[str] = []
        unverified: list[str] = []
        passed = 0
        binding: dict[str, Any] | None = None
        source: str | None = None
        for check_id in check_ids:
            assignment = assignments.get(check_id)
            tiers.append(assignment.tier if assignment else CheckTier.A)
            if assignment is not None and assignment.binding is not None:
                binding = assignment.binding.to_dict()
                source = assignment.binding_source.value if assignment.binding_source else None
            if assignment is not None and assignment.tier is CheckTier.U:
                if assignment.status_hint == "indeterminate":
                    undecided.append(assignment.reason)
                else:
                    unverified.append(assignment.reason)
                continue
            execution = executions.get(check_id)
            if not trusted or execution is None:
                undecided.append("verification_untrusted" if verification else "not_run")
            elif execution.status is CheckStatus.VIOLATED:
                failed.append(execution)
            elif execution.status is CheckStatus.EXPECTED:
                passed += 1
            else:
                undecided.append(execution.reason)
        tier = _weakest(tiers)
        if failed:
            status, reason = PackageCriterionStatus.FAIL, failed[0].reason
            heldout_only = all(failed_heldout_only(item.oracle_result) for item in failed)
        elif undecided:
            status, reason, heldout_only = PackageCriterionStatus.INDETERMINATE, undecided[0], False
        elif unverified:
            status, reason, heldout_only = PackageCriterionStatus.UNVERIFIED, unverified[0], False
        else:
            status, reason, heldout_only = PackageCriterionStatus.PASS, "passed", False
        verdicts[key] = CriterionVerdict(
            key,
            status,
            tier,
            reason,
            tuple(check_ids),
            heldout_only,
            binding,
            source,
        )
    return verdicts


def package_criterion_statuses(
    package: CheckPackage,
    verification: CandidateVerification,
    *,
    candidate_identity_ok: bool = True,
    assignments: Mapping[str, TierAssignment] | None = None,
) -> dict[str, PackageCriterionStatus]:
    """Map every criterion key to the package's status for it."""
    return {
        key: verdict.status
        for key, verdict in criterion_verdicts(
            package,
            verification,
            assignments=assignments,
            candidate_identity_ok=candidate_identity_ok,
        ).items()
    }


def artifact_verdict(statuses: Iterable[PackageCriterionStatus]) -> ArtifactVerdict:
    """Precedence: fail, then indeterminate, then pass (one verified pass), else unverified."""
    values = list(statuses)
    if PackageCriterionStatus.FAIL in values:
        return ArtifactVerdict.FAIL
    if PackageCriterionStatus.INDETERMINATE in values:
        return ArtifactVerdict.INDETERMINATE
    if PackageCriterionStatus.PASS in values:
        return ArtifactVerdict.PASS
    return ArtifactVerdict.UNVERIFIED


@dataclass(frozen=True, slots=True)
class ExistingOutcome:
    """The existing harness's final decision for one root criterion."""

    root_ac_index: int
    outcome: str
    disposition: str
    terminal_status: str
    failure_class: str | None = None

    @property
    def passed(self) -> bool:
        """The existing per-criterion verifier accepted the worker's attempt."""
        return self.outcome in _EXISTING_PASS_OUTCOMES

    @property
    def rejected_attempt(self) -> bool:
        """The worker attempted the criterion and the existing verifier rejected it."""
        return self.outcome == "failed" and self.terminal_status == "failed"

    @property
    def attempted(self) -> bool:
        """A worker attempt exists (accepted or rejected by the existing verifier)."""
        return self.passed or self.rejected_attempt


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
    tier: CheckTier = CheckTier.U
    reason: str = ""
    failed_heldout_only: bool = False
    existing_failure_class: str | None = None
    binding: dict[str, Any] | None = None

    @property
    def unverified(self) -> bool:
        return self.package_status.is_unverified

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_ac_index": self.root_ac_index,
            "criterion_key": self.criterion_key,
            "package_status": self.package_status.value,
            "tier": self.tier.value,
            "reason": self.reason,
            "failed_heldout_only": self.failed_heldout_only,
            "binding": self.binding,
            "existing_outcome": self.existing_outcome,
            "existing_failure_class": self.existing_failure_class,
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
    verdict: ArtifactVerdict = ArtifactVerdict.UNVERIFIED
    tiers: dict[str, int] = field(default_factory=dict)

    @property
    def overridden(self) -> tuple[CriterionDecision, ...]:
        """Criteria where the decision differs from the existing verifier's verdict."""
        return tuple(d for d in self.decisions if d.accepted != d.existing_accepted)

    @property
    def unverified(self) -> tuple[CriterionDecision, ...]:
        return tuple(d for d in self.decisions if d.unverified)

    @property
    def verified_pass_count(self) -> int:
        return sum(1 for d in self.decisions if d.package_status is PackageCriterionStatus.PASS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECONCILIATION_SCHEMA,
            "run_accepted": self.run_accepted,
            "existing_run_accepted": self.existing_run_accepted,
            "artifact_verdict": self.verdict.value,
            "verified_pass_count": self.verified_pass_count,
            "unverified_count": len(self.unverified),
            "criterion_count": len(self.decisions),
            "tier_summary": dict(self.tiers),
            "criteria": [decision.to_dict() for decision in self.decisions],
        }


def reconcile_acceptance(
    criterion_keys: Sequence[str],
    package_statuses: Mapping[str, PackageCriterionStatus | CriterionVerdict],
    existing: Mapping[int, ExistingOutcome],
    *,
    existing_run_accepted: bool,
) -> AcceptanceReconciliation:
    """Apply the authority rule to every root criterion, in Seed order.

    The existing verifier's verdict is kept per criterion as advisory; it
    decides nothing. ``existing_run_accepted`` only tells whether a missing
    per-criterion record means the criterion was attempted (a completed run
    attempted every root criterion).
    """
    decisions: list[CriterionDecision] = []
    for index, key in enumerate(criterion_keys):
        raw = package_statuses.get(key, PackageCriterionStatus.UNCOVERED)
        verdict = (
            raw
            if isinstance(raw, CriterionVerdict)
            else CriterionVerdict(
                key,
                raw,
                CheckTier.U if raw.is_unverified else CheckTier.A,
                raw.value,
            )
        )
        status = verdict.status
        prior = existing.get(index)
        existing_accepted = prior.passed if prior is not None else existing_run_accepted
        attempted = prior.attempted if prior is not None else existing_run_accepted
        if not attempted:
            accepted, governor = False, Governor.EXECUTION
        elif status in (PackageCriterionStatus.FAIL, PackageCriterionStatus.INDETERMINATE):
            accepted, governor = False, Governor.CHECK_PACKAGE
        else:
            accepted, governor = True, Governor.CHECK_PACKAGE
        decisions.append(
            CriterionDecision(
                root_ac_index=index,
                criterion_key=key,
                package_status=status,
                existing_outcome=None if prior is None else prior.outcome,
                existing_accepted=existing_accepted,
                accepted=accepted,
                governed_by=governor,
                tier=verdict.tier,
                reason=verdict.reason,
                failed_heldout_only=verdict.failed_heldout_only,
                existing_failure_class=None if prior is None else prior.failure_class,
                binding=verdict.binding,
            )
        )
    run_accepted = bool(decisions) and all(decision.accepted for decision in decisions)
    return AcceptanceReconciliation(
        decisions=tuple(decisions),
        run_accepted=run_accepted,
        existing_run_accepted=existing_run_accepted,
        verdict=artifact_verdict(d.package_status for d in decisions),
        tiers=tier_summary(d.tier for d in decisions),
    )


def render_reconciliation(reconciliation: AcceptanceReconciliation) -> list[str]:
    """Plain-text lines: one per criterion, then the unverified criteria with reasons."""
    lines: list[str] = []
    for decision in reconciliation.decisions:
        verdict = "accepted" if decision.accepted else "not accepted"
        existing = decision.existing_outcome or "no decision"
        if decision.governed_by is Governor.EXECUTION:
            lines.append(
                f"AC {decision.root_ac_index + 1}: {verdict}; the worker never attempted it "
                f"({existing}); check package: {decision.package_status.value}"
            )
            continue
        label = "unverified" if decision.unverified else decision.package_status.value
        lines.append(
            f"AC {decision.root_ac_index + 1}: {verdict} by the check package ({label}, "
            f"tier {decision.tier.label}); existing verifier (advisory): {existing}"
        )
    unverified = reconciliation.unverified
    total = len(reconciliation.decisions)
    lines.append(
        f"Verified: {reconciliation.verified_pass_count} of {total} passed; "
        f"unverified: {len(unverified)} (never counted as a pass)"
    )
    for decision in unverified:
        lines.append(f"- unverified AC {decision.root_ac_index + 1}: {decision.reason}")
    return lines
