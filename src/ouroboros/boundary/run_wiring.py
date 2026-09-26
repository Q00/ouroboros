"""Opt-in check-package boundary around one ``ooo run`` execution.

Order for a new run (flag ``boundary.check_package: on``):

1. ``prepare_check_package``: construct a package from the frozen Seed with
   one read-only model call, persist it by digest, record
   ``boundary.check_package.frozen``, admit it on isolated copies of the
   worker's starting checkout (the base), record the admission, and finally
   record ``boundary.actor.started`` for the run's execution id. The ledger
   refuses the actor start unless the bound version is sealed and admitted
   and the worker workspace holds no generated check file. The caller
   dispatches the worker only after this returns.
2. ``verify_check_package``: after the worker stops, run the unchanged
   package on the candidate workspace, record ``boundary.candidate.verified``,
   then ``select_incumbent`` (base versus candidate) and record
   ``boundary.selection.decided``.

Regeneration policy (``RegenerationPolicy``). Each attempt is its own boundary
version ``<execution_id>/check_package/v<n>``. With the product policy a
version that is not admitted is superseded by the next attempt, and the
constructor is told why the earlier version was not admitted. When no attempt
is admitted, a final version is sealed as ``construction_failed`` so the
worker can still start (it never depended on the package) and the run's
verdict is indeterminate. The study policy allows exactly one attempt.

The worker never receives the package: it runs from the unchanged Seed, whose
worker prompt already omits ``verify_command`` and ``output_assertion``.
Packages and full receipts are stored under ``<store_dir>/packages`` and
``<store_dir>/receipts``, outside every checkout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.boundary.acceptance import (
    PackageCriterionStatus,
    package_criterion_statuses,
)
from ouroboros.boundary.admission import (
    AdmissionResult,
    CandidateVerdict,
    CandidateVerification,
    CheckStatus,
    PackageVerdict,
    admit_check_package,
    verify_candidate,
    write_receipt,
)
from ouroboros.boundary.check_env import (
    CheckInterpreter,
    resolve_check_interpreter,
    scrubbed_check_environment,
)
from ouroboros.boundary.constructor import ALL_CRITERIA_UNCOVERED
from ouroboros.boundary.ledger import BoundaryLedger
from ouroboros.boundary.package import (
    CheckPackage,
    seed_digest,
    write_check_package,
)
from ouroboros.boundary.rollout import (
    CheckPackageAssignment,
    resolve_check_package_assignment,
)
from ouroboros.boundary.selection import (
    ArtifactRef,
    SelectionDecision,
    SelectionReason,
    select_incumbent,
)
from ouroboros.boundary.tree import tree_digest

if TYPE_CHECKING:
    from ouroboros.boundary.constructor import CheckConstructor
    from ouroboros.core.seed import Seed
    from ouroboros.persistence.event_store import EventStore

_FEEDBACK_TAIL_CHARS = 400
_COUNTEREXAMPLE_TAIL_CHARS = 1500


class RegenerationPolicy(StrEnum):
    """How many package versions one boundary slot may go through."""

    PRODUCT = "product"
    """Regenerate a non-admitted package as a new, superseding version."""

    STUDY = "study"
    """Exactly one package per boundary; no regeneration."""


@dataclass(frozen=True, slots=True)
class CheckPackageSettings:
    """Resolved configuration for one run."""

    enabled: bool
    constructor_timeout_seconds: int = 600
    check_timeout_seconds: int = 120
    max_construction_attempts: int = 2
    policy: RegenerationPolicy = RegenerationPolicy.PRODUCT
    assignment: CheckPackageAssignment | None = None

    @property
    def attempts(self) -> int:
        if self.policy is RegenerationPolicy.STUDY:
            return 1
        return max(1, self.max_construction_attempts)


def _load_boundary_config() -> Any:
    from ouroboros.config.loader import load_config

    return load_config().boundary


def resolve_check_package_settings(cli_value: bool | None = None) -> CheckPackageSettings:
    """Resolve the arm (``boundary/rollout.py``) and the budgets for one run.

    Precedence: CLI flag, then ``OUROBOROS_CHECK_PACKAGE``, then
    ``boundary.check_package`` in config, then the installation's randomized
    arm; ``off`` when none applies. Budgets always come from ``boundary`` in
    config. An unreadable config contributes no setting (and disables
    telemetry, so no randomized arm either).
    """
    try:
        config = _load_boundary_config()
        configured = config.check_package
        budgets: dict[str, Any] = {
            "constructor_timeout_seconds": config.constructor_timeout_seconds,
            "check_timeout_seconds": config.check_timeout_seconds,
            "max_construction_attempts": config.max_construction_attempts,
        }
    except Exception:
        configured = None
        budgets = {}
    assignment = resolve_check_package_assignment(cli_value, configured=configured)
    return CheckPackageSettings(enabled=assignment.enabled, assignment=assignment, **budgets)


def default_store_dir(execution_id: str) -> Path:
    """``~/.ouroboros/boundary/<execution_id>``: outside every checkout."""
    from ouroboros.config.models import get_config_dir

    return get_config_dir() / "boundary" / execution_id


@dataclass(frozen=True, slots=True)
class BoundaryRunState:
    """What the run holds between worker dispatch and candidate verification."""

    execution_id: str
    boundary_id: str
    versions: tuple[str, ...]
    seed_digest: str
    base_checkout: Path
    package: CheckPackage | None
    admission: AdmissionResult | None
    failure_reason: str | None
    store_dir: Path
    package_path: Path | None = None
    interpreter: CheckInterpreter | None = None

    @property
    def admitted(self) -> bool:
        return self.admission is not None and self.admission.verdict is PackageVerdict.ADMITTED


@dataclass(frozen=True, slots=True)
class Counterexample:
    """One failing check on the candidate, for the person running the command."""

    check_id: str
    role: str
    reason: str
    return_code: int | None
    output_tail: str


@dataclass(frozen=True, slots=True)
class BoundaryVerdict:
    """Final verdict of the boundary for one run: ``pass``, ``fail`` or ``indeterminate``."""

    verdict: str
    reasons: tuple[str, ...]
    boundary_id: str
    package_sha256: str | None
    counterexamples: tuple[Counterexample, ...] = ()
    selection: SelectionDecision | None = None
    receipt_path: Path | None = None
    uncovered: tuple[str, ...] = field(default_factory=tuple)
    criteria: dict[str, PackageCriterionStatus] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """JSON-safe summary for run output (no check code, no argv)."""
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "boundary_id": self.boundary_id,
            "package_sha256": self.package_sha256,
            "failing_checks": [example.check_id for example in self.counterexamples],
            "uncovered_criteria": list(self.uncovered),
            "criteria": {key: status.value for key, status in self.criteria.items()},
            "selection": (
                None
                if self.selection is None
                else {
                    "replaced": self.selection.replaced,
                    "reason": self.selection.reason.value,
                }
            ),
            "receipt_path": str(self.receipt_path) if self.receipt_path else None,
        }


def _admission_feedback(admission: AdmissionResult) -> list[str]:
    feedback = [f"verdict {admission.verdict.value}", *admission.reasons]
    for check in admission.checks:
        if check.status is not CheckStatus.EXPECTED:
            tail = (check.output_tail or "").strip()[-_FEEDBACK_TAIL_CHARS:]
            feedback.append(
                f"{check.check_id} ({check.role.value}): {check.reason}, "
                f"exit {check.return_code}; output tail: {tail!r}"
            )
    return feedback


async def prepare_check_package(
    seed: Seed,
    *,
    event_store: EventStore,
    constructor: CheckConstructor,
    execution_id: str,
    base_checkout: Path,
    worker_workspace: Path,
    runtime_label: str | None,
    settings: CheckPackageSettings,
    store_dir: Path | None = None,
) -> BoundaryRunState:
    """Construct, freeze, and admit a package, then record the actor start.

    Raises ``BoundaryOrderError`` / ``BoundaryLeakError`` from the ledger; the
    caller must not dispatch the worker in that case.
    """
    store = store_dir or default_store_dir(execution_id)
    ledger = BoundaryLedger(event_store)
    digest = seed_digest(seed)
    base = base_checkout.resolve()
    versions: list[str] = []
    feedback: list[str] = []
    previous: str | None = None
    previous_reason = ""
    package: CheckPackage | None = None
    admission: AdmissionResult | None = None
    package_path: Path | None = None
    failure_reason: str | None = None
    # Model-written checks run with a scrubbed environment and the project's
    # interpreter when one exists (boundary/check_env.py).
    interpreter = resolve_check_interpreter(base)

    for attempt in range(1, settings.attempts + 1):
        boundary_id = f"{execution_id}/check_package/v{attempt}"
        outcome = await constructor.construct(seed, base, feedback=feedback)
        package, admission, package_path = outcome.package, None, None
        if package is None:
            failure_reason = outcome.failure_reason or "constructor_failed"
            await ledger.record_construction_failed(
                boundary_id,
                seed_digest=digest,
                input_digest=outcome.input_digest,
                reason=failure_reason,
            )
            feedback = [failure_reason]
        else:
            package_path = write_check_package(package, store / "packages")
            await ledger.record_package_frozen(boundary_id, package, seed=seed)
            admission = await admit_check_package(
                package,
                base,
                timeout_seconds=settings.check_timeout_seconds,
                env=scrubbed_check_environment(),
                interpreter=interpreter.path,
                interpreter_source=interpreter.source,
                reject_prose_only_checks=True,
            )
            write_receipt(admission, store / "receipts")
            await ledger.record_admission(boundary_id, admission)
            failure_reason = (
                None
                if admission.verdict is PackageVerdict.ADMITTED
                else f"package_{admission.verdict.value}"
            )
            feedback = _admission_feedback(admission)
        versions.append(boundary_id)
        if previous is not None:
            await ledger.record_superseded(
                previous, superseded_by=boundary_id, reason=previous_reason
            )
        if failure_reason is None or failure_reason == ALL_CRITERIA_UNCOVERED:
            # Admitted, or every criterion declared not executable (the
            # existing verifier decides them all; regenerating cannot help).
            break
        previous, previous_reason = boundary_id, failure_reason

    bound = versions[-1]
    if package is not None and failure_reason is not None:
        # A frozen but unadmitted version cannot host a worker. Seal a final
        # version that records the absence of an admitted package.
        final_id = f"{execution_id}/check_package/v{len(versions) + 1}"
        await ledger.record_construction_failed(
            final_id,
            seed_digest=digest,
            input_digest=package.input_digest,
            reason=f"no_admitted_package:{failure_reason}",
        )
        await ledger.record_superseded(bound, superseded_by=final_id, reason=failure_reason)
        versions.append(final_id)
        bound = final_id

    await ledger.record_actor_started(
        execution_id, [bound], workspace=worker_workspace, runtime=runtime_label
    )
    admitted = admission is not None and admission.verdict is PackageVerdict.ADMITTED
    return BoundaryRunState(
        execution_id=execution_id,
        boundary_id=bound,
        versions=tuple(versions),
        seed_digest=digest,
        base_checkout=base,
        package=package if admitted else None,
        admission=admission if admitted else None,
        failure_reason=failure_reason,
        store_dir=store,
        package_path=package_path if admitted else None,
        interpreter=interpreter,
    )


def _counterexamples(verification: CandidateVerification) -> tuple[Counterexample, ...]:
    return tuple(
        Counterexample(
            check_id=check.check_id,
            role=check.role.value,
            reason=check.reason,
            return_code=check.return_code,
            output_tail=(check.output_tail or "")[-_COUNTEREXAMPLE_TAIL_CHARS:],
        )
        for check in verification.checks
        if check.status is not CheckStatus.EXPECTED
    )


async def verify_check_package(
    state: BoundaryRunState,
    *,
    event_store: EventStore,
    candidate_checkout: Path,
    settings: CheckPackageSettings,
) -> BoundaryVerdict:
    """Run the unchanged package on the candidate and record verdict and selection."""
    if state.package is None or state.admission is None:
        return BoundaryVerdict(
            verdict=CandidateVerdict.INDETERMINATE.value,
            reasons=(state.failure_reason or "no_admitted_package",),
            boundary_id=state.boundary_id,
            package_sha256=None,
        )
    ledger = BoundaryLedger(event_store)
    package = state.package
    candidate = candidate_checkout.resolve()
    candidate_ref = ArtifactRef(
        artifact_id=f"{state.execution_id}:candidate",
        tree_digest=tree_digest(candidate),
        seed_digest=package.seed_digest,
    )
    interpreter = state.interpreter or resolve_check_interpreter(candidate)
    verification = await verify_candidate(
        package,
        candidate,
        timeout_seconds=settings.check_timeout_seconds,
        env=scrubbed_check_environment(),
        interpreter=interpreter.path,
        interpreter_source=interpreter.source,
    )
    receipt = write_receipt(verification, state.store_dir / "receipts")
    await ledger.record_candidate_verification(state.boundary_id, verification)
    decision = select_incumbent(
        incumbent=ArtifactRef(
            artifact_id=f"{state.execution_id}:base",
            tree_digest=state.admission.base_tree_digest,
            seed_digest=package.seed_digest,
        ),
        candidate=candidate_ref,
        package=package,
        admission=state.admission,
        verification=verification,
        candidate_checkout=candidate,
    )
    await ledger.record_selection(state.boundary_id, decision)
    criteria = package_criterion_statuses(
        package,
        verification,
        candidate_identity_ok=decision.reason is not SelectionReason.CANDIDATE_IDENTITY_MISMATCH,
    )
    return BoundaryVerdict(
        verdict=verification.verdict.value,
        reasons=verification.reasons,
        boundary_id=state.boundary_id,
        package_sha256=package.sha256,
        counterexamples=_counterexamples(verification),
        selection=decision,
        receipt_path=receipt,
        uncovered=tuple(item.criterion_key for item in package.uncovered),
        criteria=criteria,
    )


def render_preparation(state: BoundaryRunState) -> list[str]:
    """Plain-text lines describing the boundary the worker is bound to."""
    lines = [f"Check package boundary: {state.boundary_id}"]
    if len(state.versions) > 1:
        lines.append(f"Superseded versions: {', '.join(state.versions[:-1])}")
    if state.admitted and state.package is not None:
        package = state.package
        roles = ", ".join(f"{check.check_id} ({check.role.value})" for check in package.checks)
        lines.append(f"Package {package.sha256[:16]} admitted on the base: {roles}")
        if state.interpreter is not None:
            lines.append(
                f"Checks run with {state.interpreter.path} ({state.interpreter.source}) "
                "and a scrubbed environment"
            )
        if package.uncovered:
            lines.append(
                f"Uncovered criteria: {len(package.uncovered)} of {len(package.criterion_keys)}"
            )
    else:
        lines.append(
            f"No admitted package ({state.failure_reason}); the run continues and its "
            "check verdict will be indeterminate."
        )
    return lines


def render_verdict(verdict: BoundaryVerdict) -> list[str]:
    """Plain-text lines: verdict first, then each counterexample."""
    lines = [f"Check package verdict: {verdict.verdict}"]
    if verdict.selection is not None:
        outcome = "accepted" if verdict.selection.replaced else "not accepted"
        lines.append(f"Candidate {outcome} ({verdict.selection.reason.value})")
    if verdict.verdict != CandidateVerdict.PASS.value and verdict.reasons:
        lines.append(f"Reasons: {', '.join(verdict.reasons)}")
    for example in verdict.counterexamples:
        lines.append(
            f"- {example.check_id} ({example.role}): {example.reason}, exit {example.return_code}"
        )
        if example.output_tail.strip():
            lines.append(example.output_tail.rstrip())
    if verdict.uncovered:
        lines.append(f"Criteria without a check (not verified): {len(verdict.uncovered)}")
    if verdict.receipt_path is not None:
        lines.append(f"Receipt: {verdict.receipt_path}")
    return lines
