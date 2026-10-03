"""Acceptance authority: the admitted check package decides every criterion.

Policy (check package on). Executable verification is authoritative; the
existing per-criterion verifier (typed evidence plus the runtime-transcript
verifier) only annotates: its verdict and failure class are recorded next to
the package's, and they decide nothing. Each criterion gets one package
status once the worker has stopped:

- ``pass`` (a verified pass): every linked check ran through a validated
  binding (tier ``A`` or ``A_prime``) and met its contract, and at least one
  of them is an oracle check with the ``reproduction`` role (admission
  proved it fails on the base) that passed with at least one held-out case
  (a case the worker never saw) passing that its base run failed at
  admission (``per_check.base_failing_held_out``). Nothing weaker is a
  verified pass: a criterion whose passing checks are all preservation
  checks (``no_reproduction_check``), or whose reproduction oracle passed no
  held-out case the base failed (``no_held_out_case``, which is also every
  per-attempt gate run, since the gate runs visible cases only), is
  ``unverified``; a held-out case the base already passed shows nothing the
  candidate fixed. Why a held-out case:
  an oracle observation is reported by code the candidate controls (it runs
  in the target process and can write the report itself,
  ``boundary/oracle_run.py``), so a pass is evidence of what the candidate
  computed, never proof that the bound callable ran. A visible case's
  expected value is in the specification, so a reported match proves
  nothing; a held-out case's inputs reach the candidate only in the terminal
  verification, so a match means the candidate produced the rule's output
  for an input it had never seen. Every oracle carries at least one
  held-out case for this reason (``oracle.OracleSpec``). A ``no_raise``
  case states no output, so a target that returns anything passes it: it
  can fail a candidate, but its pass never verifies (``OracleCase.verifies``);
- ``fail``: a linked check was violated; its counterexample is reported.
  Also an artifact check's executed failure on a criterion the package left
  unverified or uncovered (see below);
- ``indeterminate``: a check could not be judged (timeout, launch failure,
  protected-byte mutation, no failure signature, untrusted verification), or
  a worker-declared binding was invalid (``binding_invalid:*``,
  ``binding_admission_timeout``);
- ``unverified``: a check exists but no binding reaches the target
  (``no_binding``), or a linked check was not run for that reason, or every
  passing check is a model-written script check (``script_check_advisory``:
  a script check runs the workspace in the process that decides its exit
  code, so its pass is advisory; only an oracle check is a verified pass,
  while a script check's fail still fails the criterion);
- ``uncovered``: no executable check exists for the criterion (tier ``U``).

``unverified`` and ``uncovered`` are both "unverified": they never count as a
pass in any aggregate. A criterion is accepted when the worker attempted it
and its status is ``pass``, ``unverified`` or ``uncovered``, except that a
``pass`` that rests on a worker-declared binding (``declared_binding_pass``:
no reproduction oracle passed a held-out case through the product's own
binding, tier ``A``) only corroborates: over a legacy rejection it is not
accepted (``a_prime_corroborates_only``, decided by the legacy verifier).
The display tier (the weakest over the linked checks) decides nothing: an
``A_prime`` oracle plus an advisory script shows ``S``. A criterion
nobody attempted (blocked, invalid, cancelled, or missing on a failed run)
is not accepted, whatever the package says.

With ``legacy_decides_unverified`` (the product with the check package on,
by design) an unverified or uncovered criterion, whatever the
reason it has no admitted check, is decided by the legacy verifier instead:
its rejection fails the criterion and the run (``governed_by: existing_verifier``,
"legacy-decided"). Only a criterion for which the legacy verifier has no
evidence either (``ExistingOutcome.no_evidence``: transcript unavailable,
environment unverifiable, no verifier verdict, or worker-cited transcript
calls of which none qualifies, ``NO_CALL_EVIDENCE``, which is no evidence and
never fabrication) stays ``unverified`` and is accepted; the run then reports
insufficient verification (``verification_coverage``).

Artifact checks (``boundary/base_regression.py``, on unless
``boundary.base_regression: off``). The controller also checks the whole
candidate itself: the base tree's existing tests that pair with or import a
changed module, restored to their base bytes and run on the base twice and on
the candidate once, and each test file the worker added. An executed failure
(a test that passed on both base runs fails on the candidate, or an added
test file's run exits 1 with a failing test) fails every criterion the
package left ``unverified`` or ``uncovered`` (``CriterionVerdict.artifact_check``)
before the legacy rule applies, so the legacy verifier cannot accept it; a
verified ``pass``, a package ``fail`` and an ``indeterminate`` criterion keep
the package's verdict. A regressed test is exempt (``base_regression.regressions_to_keep``)
when its failing run produced a footprint and every changed function it
entered was also entered by an admitted oracle check that passed on the
candidate; with more than 20 regressions, or no passing oracle, nothing is
exempt, and when every regression is exempt the check decides nothing. A
check with no observation (a timeout, a base on
which the runner wrote no report, no selected file, a project runner it does
not drive, a run the sandbox could not confine) decides nothing. They run
with no admitted package too: every criterion is then uncovered, so a failure
fails them all, recorded on the version sealed without a package; a resumed
run replays the recorded fails and never runs the checks again.

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
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ouroboros.boundary.binding import CheckTier, TierAssignment, tier_summary
from ouroboros.boundary.events import (
    LEGACY_RULE_SCHEMA,
    ReconciliationPayload,
    artifact_verdict_of,
    coverage_of,
)
from ouroboros.boundary.oracle import failed_heldout_only
from ouroboros.boundary.package import CheckRole
from ouroboros.boundary.per_check import base_failing_held_out, criteria_without_admitted_check
from ouroboros.boundary.receipts import (
    AdmissionResult,
    CandidateVerification,
    CheckExecution,
    CheckStatus,
)

if TYPE_CHECKING:
    from ouroboros.boundary.package import CheckPackage

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
    # The legacy (existing) verifier: decides an unverified or uncovered
    # criterion under ``legacy_decides_unverified``.
    EXISTING_VERIFIER = "existing_verifier"


class VerificationCoverage(StrEnum):
    """How much of a run the package decided (``verification_coverage``)."""

    FULL = "full"
    """The package decided every criterion."""
    PARTIAL = "partial"
    """Some criteria were not decided by the package, under half, none unverified."""
    LOW = "low"
    """Half or more were not decided by the package, or a criterion is unverified."""


class ArtifactCheck(StrEnum):
    """A controller-run check of the whole artifact (``boundary/base_regression.py``).

    It only fails: an executed failure fails every criterion the package left
    unverified or uncovered (``CriterionVerdict.artifact_check``).
    """

    BASE_REGRESSION = "base_regression"
    """Existing tests that passed on the base fail on the candidate."""
    WORKER_TESTS = "worker_tests"
    """A test file the worker added fails when the controller runs it."""


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
    declared_binding_pass: bool = field(kw_only=True)
    """The ``pass`` rests on a worker-declared binding: no reproduction oracle
    passed a held-out case through a tier ``A`` binding. ``False`` unless
    ``status`` is ``pass``. It, not ``tier``, decides corroboration; it has no
    default, so every verdict states its provenance."""
    artifact_check: ArtifactCheck | None = field(default=None, kw_only=True)
    """The artifact check that failed this otherwise undecided criterion, if any."""

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
            "declared_binding_pass": self.declared_binding_pass,
            **({"artifact_check": self.artifact_check.value} if self.artifact_check else {}),
        }


_TRANSIENT_REASONS = frozenset(
    {"timeout", "launch_failed", "protected_bytes_mutated", "failure_signature_absent"}
)


SCRIPT_CHECK_ADVISORY = "script_check_advisory"
"""Reason of a criterion whose only passing checks are model-written scripts."""
NO_REPRODUCTION_CHECK = "no_reproduction_check"
"""Reason of a criterion no passing reproduction oracle check covers (preservation only)."""
NO_HELD_OUT_CASE = "no_held_out_case"
"""Reason of a criterion whose passing reproduction oracle passed no held-out case the base failed."""
A_PRIME_CORROBORATES_ONLY = "a_prime_corroborates_only"
"""A pass through a worker-declared binding cannot overrule a legacy rejection."""


def _base_failing_held_out_passed(execution: CheckExecution, base_failing: frozenset[str]) -> bool:
    """A held-out case the base failed at admission passed on the candidate.

    Only such a case verifies a pass: a held-out case the base already
    passed shows nothing the candidate fixed (``per_check.base_failing_held_out``).
    """
    result = execution.oracle_result
    return result is not None and any(
        case.held_out and case.passed and case.case_id in base_failing for case in result.cases
    )


def _verifying(
    package: CheckPackage, base_failing: Mapping[str, frozenset[str]]
) -> dict[str, frozenset[str]]:
    """``base_failing`` without the cases whose pass cannot verify (``no_raise``)."""
    kept: dict[str, frozenset[str]] = {}
    for check_id, case_ids in base_failing.items():
        spec = package.oracle_for(check_id)
        verifying = {case.case_id for case in spec.cases if case.verifies} if spec else set()
        kept[check_id] = frozenset(case_ids & verifying)
    return kept


def rerunnable_checks(verification: CandidateVerification) -> tuple[str, ...]:
    """Checks whose indeterminate result one zero-model re-run may resolve (R3)."""
    return tuple(
        check.check_id
        for check in verification.checks
        if check.status is CheckStatus.INDETERMINATE and check.reason in _TRANSIENT_REASONS
    )


def _weakest(tiers: Iterable[CheckTier]) -> CheckTier:
    values = set(tiers)
    for tier in (CheckTier.C, CheckTier.U, CheckTier.S, CheckTier.A_PRIME):
        if tier in values:
            return tier
    return CheckTier.A


def _deciding_binding(
    check_ids: Sequence[str],
    tier: CheckTier,
    bound: Mapping[str, tuple[CheckTier, dict[str, Any], str | None]],
) -> tuple[dict[str, Any] | None, str | None]:
    """The binding of a check that decided the verdict, preferring the verdict's tier."""
    candidates = [check_id for check_id in check_ids if check_id in bound]
    if not candidates:
        return None, None
    chosen = next((c for c in candidates if bound[c][0] is tier), candidates[0])
    _tier, binding, source = bound[chosen]
    return binding, source


def criterion_verdicts(
    package: CheckPackage,
    verification: CandidateVerification | None,
    *,
    admission: AdmissionResult,
    assignments: Mapping[str, TierAssignment] | None = None,
    candidate_identity_ok: bool = True,
) -> dict[str, CriterionVerdict]:
    """Every criterion's package verdict, with its tier.

    ``admission`` is the package's admission on the base: the checks it
    excluded (``excluded_checks``) do not count for their criterion, and a
    reproduction oracle verifies a pass only through a held-out case its
    base run failed (``per_check.base_failing_held_out``). ``assignments``
    maps a check id to its tier assignment; a check without one is tier
    ``A`` and was expected to run. A check assigned ``U`` is not
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
    # Checks excluded at admission (tier ``C``, ``boundary/per_check.py``) do
    # not count for their criterion; a criterion left without an admitted
    # check (or a reproduction-type one without an admitted reproduction
    # check) is uncovered.
    roles = {check.check_id: check.role for check in package.checks}
    excluded: dict[str, str] = dict(admission.excluded_checks or {})
    lost = criteria_without_admitted_check(package, excluded)
    base_failing = _verifying(package, base_failing_held_out(admission.checks, excluded))
    verdicts: dict[str, CriterionVerdict] = {}
    for key, all_check_ids in linked.items():
        check_ids = [check_id for check_id in all_check_ids if check_id not in excluded]
        if key in lost:
            verdicts[key] = CriterionVerdict(
                key,
                PackageCriterionStatus.UNCOVERED,
                CheckTier.U,
                f"uncovered:{lost[key]}",
                tuple(all_check_ids),
                declared_binding_pass=False,
            )
            continue
        if not check_ids:
            verdicts[key] = CriterionVerdict(
                key,
                PackageCriterionStatus.UNCOVERED,
                CheckTier.U,
                f"uncovered:{uncovered.get(key, 'no_check')}",
                declared_binding_pass=False,
            )
            continue
        tiers: list[CheckTier] = []
        # The tier of the checks that decided each outcome: a criterion
        # failed through a tier A check is "fail, tier A" even when it also
        # links an unverified check.
        decided_by: dict[str, list[CheckTier]] = {"fail": [], "undecided": [], "unverified": []}
        failed: list[CheckExecution] = []
        undecided: list[str] = []
        unverified: list[str] = []
        passed = 0
        advisory = 0
        reproduced = 0
        held_out_verified = 0
        # Of those, the ones through the product's own binding (tier A); a
        # pass without one rests on a worker-declared binding.
        held_out_by_default = 0
        # The binding each check ran through, and which checks decided each
        # outcome, so the verdict names the binding of a deciding check.
        bound: dict[str, tuple[CheckTier, dict[str, Any], str | None]] = {}
        deciding: dict[str, list[str]] = {"fail": [], "undecided": [], "unverified": []}
        for check_id in check_ids:
            assignment = assignments.get(check_id)
            check_tier = assignment.tier if assignment else CheckTier.A
            tiers.append(check_tier)
            if assignment is not None and assignment.binding is not None:
                bound[check_id] = (
                    check_tier,
                    assignment.binding.to_dict(),
                    assignment.binding_source.value if assignment.binding_source else None,
                )
            if assignment is not None and assignment.tier is CheckTier.U:
                bucket = "undecided" if assignment.status_hint == "indeterminate" else "unverified"
                (undecided if bucket == "undecided" else unverified).append(assignment.reason)
                decided_by[bucket].append(check_tier)
                deciding[bucket].append(check_id)
                continue
            execution = executions.get(check_id)
            if not trusted or execution is None:
                undecided.append("verification_untrusted" if verification else "not_run")
                decided_by["undecided"].append(check_tier)
                deciding["undecided"].append(check_id)
            elif execution.status is CheckStatus.VIOLATED:
                failed.append(execution)
                decided_by["fail"].append(check_tier)
                deciding["fail"].append(check_id)
            elif execution.status is CheckStatus.EXPECTED:
                if package.oracle_for(check_id) is None:
                    # A model-written script check imports the workspace in
                    # the process whose exit code is its verdict, so the code
                    # under test (or a forged interpreter or sitecustomize)
                    # can produce that exit code. Its pass is advisory; only
                    # an oracle check, compared in the controller's own
                    # interpreter, is a verified pass. Its fail still counts.
                    advisory += 1
                else:
                    passed += 1
                    if roles.get(check_id) is CheckRole.REPRODUCTION:
                        reproduced += 1
                        if _base_failing_held_out_passed(
                            execution, base_failing.get(check_id, frozenset())
                        ):
                            held_out_verified += 1
                            held_out_by_default += int(check_tier is CheckTier.A)
            else:
                undecided.append(execution.reason)
                decided_by["undecided"].append(check_tier)
                deciding["undecided"].append(check_id)
        if failed:
            status, reason = PackageCriterionStatus.FAIL, failed[0].reason
            heldout_only = all(failed_heldout_only(item.oracle_result) for item in failed)
            tier = _weakest(decided_by["fail"])
        elif undecided:
            status, reason, heldout_only = PackageCriterionStatus.INDETERMINATE, undecided[0], False
            tier = _weakest(decided_by["undecided"])
        elif unverified or not passed:
            reason = unverified[0] if unverified else SCRIPT_CHECK_ADVISORY
            status, heldout_only = PackageCriterionStatus.UNVERIFIED, False
            tier = _weakest(decided_by["unverified"]) if unverified else CheckTier.U
        elif not held_out_verified:
            # Every linked check passed, but nothing shows the criterion's rule
            # beyond what the base already did or what the worker was shown.
            reason = NO_HELD_OUT_CASE if reproduced else NO_REPRODUCTION_CHECK
            status, heldout_only = PackageCriterionStatus.UNVERIFIED, False
            tier = _weakest(tiers)
        else:
            status, reason, heldout_only = PackageCriterionStatus.PASS, "passed", False
            tier = _weakest(tiers)
        declared_binding_pass = status is PackageCriterionStatus.PASS and not held_out_by_default
        bucket = (
            "fail" if failed else "undecided" if undecided else "unverified" if unverified else None
        )
        binding, source = _deciding_binding(
            deciding[bucket] if bucket else list(check_ids), tier, bound
        )
        verdicts[key] = CriterionVerdict(
            key,
            status,
            tier,
            reason,
            tuple(check_ids),
            heldout_only,
            binding,
            source,
            declared_binding_pass=declared_binding_pass,
        )
    return verdicts


def artifact_verdict(statuses: Iterable[PackageCriterionStatus]) -> ArtifactVerdict:
    """Precedence: fail, then indeterminate, then pass (one verified pass), else unverified.

    The journal's rule (``events.artifact_verdict_of``), so a recorded
    decision's verdict always agrees with its criteria.
    """
    return ArtifactVerdict(artifact_verdict_of(status.value for status in statuses))


class LegacyNoEvidenceReason(StrEnum):
    """Why the legacy verifier accepted a criterion without evidence (closed set).

    Decided where ``ExistingOutcome.no_evidence`` is
    (``authority.existing_outcomes_from_results``) from the result's typed
    fields; it decides nothing and is recorded for anonymous telemetry only.
    """

    ENVIRONMENT_UNVERIFIABLE = "environment_unverifiable"
    """The ``verify_command`` environment could not run the command."""
    TRANSCRIPT_UNAVAILABLE = "transcript_unavailable"
    """The runtime transcript was unavailable (``TRANSCRIPT_MISSING_INFRASTRUCTURE``)."""
    NO_VERIFIER_VERDICT = "no_verifier_verdict"
    """No verifier verdict was recorded for the attempt."""
    SCRIPT_ABSENT_FROM_ARTIFACT = "script_absent_from_artifact"
    """Every unproven claim is a recorded run whose script left the workspace,
    so it was not replayed (``SCRIPT_ABSENT_FROM_ARTIFACT``)."""
    NO_CALL_EVIDENCE = "no_call_evidence"
    """The worker cited transcript calls by number and none qualified as
    evidence (``NO_CALL_EVIDENCE``)."""
    VERIFIER_VERDICT_NOT_PASSED = "verifier_verdict_not_passed"
    """A verdict that did not pass, with no rejection the executor made."""


@dataclass(frozen=True, slots=True)
class ExistingOutcome:
    """The existing harness's final decision for one root criterion."""

    root_ac_index: int
    outcome: str
    disposition: str
    terminal_status: str
    failure_class: str | None = None
    no_evidence: bool = False
    """The legacy verifier accepted without evidence (no verdict, transcript
    unavailable, environment unverifiable); never set on a rejection."""
    no_evidence_reason: LegacyNoEvidenceReason | None = None
    """Why ``no_evidence`` is set; ``None`` when it is not. Decides nothing."""

    @property
    def passed(self) -> bool:
        """The existing harness finally accepted the worker's attempt.

        The runner's canonical rule (``execution_authority``'s terminal
        acceptance plan): accepted exactly when the terminal status is
        ``completed``, the outcome is a success, and the disposition says so.
        A successful outcome whose final verification rejected it
        (``disposition="rejected"``, ``terminal_status="failed"``) is not a pass.
        """
        return (
            self.terminal_status == "completed"
            and self.outcome in _EXISTING_PASS_OUTCOMES
            and self.disposition == "accepted"
        )

    @property
    def rejected_attempt(self) -> bool:
        """The worker attempted the criterion and the existing harness rejected it.

        Either the attempt failed, or it succeeded and the final verification
        rejected it; both end with ``terminal_status="failed"``. A cancelled,
        blocked or invalid criterion is not an attempt.
        """
        return self.terminal_status == "failed" and (
            self.outcome == "failed" or self.outcome in _EXISTING_PASS_OUTCOMES
        )

    @property
    def attempted(self) -> bool:
        """A worker attempt exists (accepted or rejected by the existing verifier)."""
        return self.passed or self.rejected_attempt

    @property
    def failed_outside_package(self) -> bool:
        """The worker's attempt failed a gate the package does not decide.

        A runtime failure, a failed ``verify_command``, or a package gate
        failure the final settlement did not find holding
        (``authority.existing_outcomes_from``): the worker ran, but this is
        not an attempt the package may accept, so it is recorded as not
        attempted with the outcome ``failed``. A blocked, invalid or cancelled
        criterion was never run.
        """
        return self.terminal_status == "not_attempted" and self.outcome == "failed"


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
    declared_binding_pass: bool = field(kw_only=True)
    """The package's ``pass`` rests on a worker-declared binding (``CriterionVerdict``)."""
    failed_outside_package: bool = field(default=False, kw_only=True)
    """Not accepted because the worker's attempt failed a gate the package does
    not decide (``ExistingOutcome.failed_outside_package``); display only."""
    artifact_check: ArtifactCheck | None = field(default=None, kw_only=True)
    """The artifact check that failed this criterion (``CriterionVerdict``)."""

    @property
    def legacy_decided(self) -> bool:
        """The legacy verifier decided this criterion (``legacy_decides_unverified``)."""
        return self.governed_by is Governor.EXISTING_VERIFIER

    @property
    def unverified(self) -> bool:
        """No verifier had evidence for it: the package did not, and no legacy decision."""
        return self.package_status.is_unverified and not self.legacy_decided

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
            "declared_binding_pass": self.declared_binding_pass,
            **({"artifact_check": self.artifact_check.value} if self.artifact_check else {}),
        }


@dataclass(frozen=True, slots=True)
class AcceptanceReconciliation:
    """Per-criterion decisions plus whether the run as a whole is accepted."""

    decisions: tuple[CriterionDecision, ...]
    run_accepted: bool
    existing_run_accepted: bool
    verdict: ArtifactVerdict = ArtifactVerdict.UNVERIFIED
    tiers: dict[str, int] = field(default_factory=dict)
    legacy_rule: bool = False
    """Decided under ``legacy_decides_unverified`` (schema v3)."""

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

    @property
    def legacy_decided(self) -> tuple[CriterionDecision, ...]:
        return tuple(d for d in self.decisions if d.legacy_decided)

    @property
    def not_package_decided(self) -> tuple[CriterionDecision, ...]:
        """Criteria the package did not decide (unverified or uncovered), attempted or not."""
        return tuple(d for d in self.decisions if d.package_status.is_unverified)

    @property
    def accepted_unverified(self) -> tuple[CriterionDecision, ...]:
        """Attempted criteria accepted with no evidence from any verifier."""
        return tuple(
            d for d in self.decisions if d.unverified and d.governed_by is not Governor.EXECUTION
        )

    @property
    def coverage(self) -> VerificationCoverage:
        return verification_coverage(
            len(self.decisions), len(self.not_package_decided), len(self.accepted_unverified)
        )

    @property
    def insufficient_verification(self) -> bool:
        return self.coverage is VerificationCoverage.LOW

    def to_dict(self) -> dict[str, Any]:
        data = {
            "schema_version": LEGACY_RULE_SCHEMA if self.legacy_rule else RECONCILIATION_SCHEMA,
            "run_accepted": self.run_accepted,
            "existing_run_accepted": self.existing_run_accepted,
            "artifact_verdict": self.verdict.value,
            "verified_pass_count": self.verified_pass_count,
            "unverified_count": len(self.unverified),
            "criterion_count": len(self.decisions),
            "tier_summary": dict(self.tiers),
            "criteria": [decision.to_dict() for decision in self.decisions],
        }
        if self.legacy_rule:
            data.update(
                {
                    "legacy_decided_count": len(self.legacy_decided),
                    "verification_coverage": self.coverage.value,
                }
            )
        return data

    def to_payload(self) -> ReconciliationPayload:
        """The typed journal payload of ``boundary.acceptance.reconciled``."""
        return ReconciliationPayload.model_validate(self.to_dict())


def verification_coverage(total: int, not_decided: int, unverified: int) -> VerificationCoverage:
    """``low`` when half or more of ``total`` were not decided by the package or any is unverified.

    The journal's rule (``events.coverage_of``).
    """
    return VerificationCoverage(coverage_of(total, not_decided, unverified))


def reconcile_acceptance(
    criterion_keys: Sequence[str],
    package_statuses: Mapping[str, PackageCriterionStatus | CriterionVerdict],
    existing: Mapping[int, ExistingOutcome],
    *,
    existing_run_accepted: bool,
    legacy_decides_unverified: bool = False,
) -> AcceptanceReconciliation:
    """Apply the authority rule to every root criterion, in Seed order.

    The existing verifier's verdict is kept per criterion as advisory; it
    decides nothing, except with ``legacy_decides_unverified``: then it
    decides every attempted criterion the package left unverified or
    uncovered for which it has evidence (see the
    module docstring). ``existing_run_accepted`` only tells whether a missing
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
                # A bare status names no binding: a pass the caller states
                # directly is the package's own (tier A).
                declared_binding_pass=False,
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
        elif (
            legacy_decides_unverified
            and status.is_unverified
            and prior is not None
            and not prior.no_evidence
        ):
            accepted, governor = prior.passed, Governor.EXISTING_VERIFIER
        elif verdict.declared_binding_pass and prior is not None and prior.rejected_attempt:
            # A pass that rests on a worker-declared binding corroborates; it
            # never overrules. Its provenance decides, never the display tier.
            accepted, governor = False, Governor.EXISTING_VERIFIER
            verdict = replace(verdict, reason=A_PRIME_CORROBORATES_ONLY)
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
                declared_binding_pass=verdict.declared_binding_pass,
                failed_outside_package=prior is not None and prior.failed_outside_package,
                artifact_check=verdict.artifact_check,
            )
        )
    run_accepted = bool(decisions) and all(decision.accepted for decision in decisions)
    return AcceptanceReconciliation(
        decisions=tuple(decisions),
        run_accepted=run_accepted,
        existing_run_accepted=existing_run_accepted,
        verdict=artifact_verdict(d.package_status for d in decisions),
        tiers=tier_summary(d.tier for d in decisions),
        legacy_rule=legacy_decides_unverified,
    )


def render_reconciliation(reconciliation: AcceptanceReconciliation) -> list[str]:
    """Plain-text lines: one per criterion, then the unverified criteria with reasons."""
    lines: list[str] = []
    for decision in reconciliation.decisions:
        verdict = "accepted" if decision.accepted else "not accepted"
        existing = decision.existing_outcome or "no decision"
        if decision.governed_by is Governor.EXECUTION and decision.failed_outside_package:
            cause = decision.existing_failure_class or "no failure class"
            lines.append(
                f"AC {decision.root_ac_index + 1}: {verdict}; the worker's attempt failed "
                f"outside the check package ({cause}), so the package cannot accept it; "
                f"check package: {decision.package_status.value}"
            )
            continue
        if decision.governed_by is Governor.EXECUTION:
            lines.append(
                f"AC {decision.root_ac_index + 1}: {verdict}; the worker never attempted it "
                f"({existing}); check package: {decision.package_status.value}"
            )
            continue
        if decision.legacy_decided:
            lines.append(
                f"AC {decision.root_ac_index + 1}: {verdict} by the legacy verifier "
                f"(legacy-decided: {decision.reason}); legacy verdict: {existing}"
            )
            continue
        label = "unverified" if decision.unverified else decision.package_status.value
        lines.append(
            f"AC {decision.root_ac_index + 1}: {verdict} by the check package ({label}, "
            f"tier {decision.tier.label}); existing verifier (advisory): {existing}"
        )
    unverified = reconciliation.unverified
    total = len(reconciliation.decisions)
    if not reconciliation.legacy_rule:
        lines.append(
            f"Verified: {reconciliation.verified_pass_count} of {total} passed; "
            f"unverified: {len(unverified)} (never counted as a pass)"
        )
    else:
        lines.append(
            f"Verified by the check package: {reconciliation.verified_pass_count} of {total} "
            f"passed; legacy-decided: {len(reconciliation.legacy_decided)}; "
            f"unverified: {len(unverified)} (never counted as a pass)"
        )
    for decision in unverified:
        lines.append(f"- unverified AC {decision.root_ac_index + 1}: {decision.reason}")
    if reconciliation.legacy_rule and reconciliation.insufficient_verification:
        lines.append(insufficient_verification_line(reconciliation))
    return lines


def insufficient_verification_line(reconciliation: AcceptanceReconciliation) -> str:
    """The warning printed when ``verification_coverage`` is ``low``."""
    total = len(reconciliation.decisions)
    return (
        "WARNING: insufficient verification: the check package decided "
        f"{total - len(reconciliation.not_package_decided)} of {total} criteria; "
        f"{len(reconciliation.accepted_unverified)} had no evidence from any verifier "
        "(verification_coverage=low)."
    )
