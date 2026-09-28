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
   package on the candidate workspace and record
   ``boundary.candidate.verified``. The candidate's tree digest is taken
   before verification and again after it; a verification that did not run
   on that exact tree is untrusted.

Regeneration. Each attempt is its own boundary version
``<execution_id>/check_package/v<n>`` (at most ``max_construction_attempts``).
A version that is not admitted is superseded by the next attempt, and the
constructor is told why the earlier version was not admitted. When no attempt
is admitted, a final version is sealed as ``construction_failed`` so the
worker can still start (it never depended on the package) and the legacy
verifier decides the run.

The worker never receives the package: it runs from the unchanged Seed, whose
worker prompt already omits ``verify_command`` and ``output_assertion``.
Package records and receipts are stored under ``<store_dir>/packages`` and
``<store_dir>/receipts``, outside every checkout. Held-out inputs and
expected values are never written to disk: they stay in this process's
memory for the run. The package record keeps a held-out case's id only
(``package.package_record``), no constructor reply is written anywhere,
and a stored receipt keeps a held-out case's id and pass/fail only
(``oracle.redact_held_out``). The store directory is owner-only (0700).

Every package is sealed before it is frozen (``package.seal_package``): the
journal, the record and every receipt cite its opaque id and the Seed
digest, never an unkeyed digest of the package, so nothing in the store or
the journal lets held-out values be confirmed by enumeration. A run that
resumes in another process cannot recover the held-out cases, so it runs no
check and leaves the covered criteria undecided (``boundary/resume.py``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ouroboros.boundary.acceptance import (
    ArtifactVerdict,
    CriterionVerdict,
    PackageCriterionStatus,
    artifact_verdict,
    criterion_verdicts,
)
from ouroboros.boundary.admission import admit_check_package
from ouroboros.boundary.binding import CheckTier, TierAssignment
from ouroboros.boundary.binding_flow import (
    DeclaredBindingResult,
    assign_tiers,
    bindings_payload,
    snapshot_base,
    verify_with_bindings,
)
from ouroboros.boundary.check_env import (
    CheckInterpreter,
    resolve_check_interpreter,
)
from ouroboros.boundary.constructor import ALL_CRITERIA_UNCOVERED
from ouroboros.boundary.coverage import (
    merge_replacement,
    replacement_targets,
    why_excluded,
)
from ouroboros.boundary.events import ReferenceCheckPayload, RunContract, boundary_version_id
from ouroboros.boundary.ledger import BoundaryLedger
from ouroboros.boundary.oracle import OracleResult, OracleSpec
from ouroboros.boundary.package import (
    CheckPackage,
    CheckPackageError,
    canonical_json_bytes,
    seal_package,
    seed_criterion_keys,
    seed_digest,
    validate_package_for_seed,
    write_package_record,
)
from ouroboros.boundary.per_check import criteria_without_admitted_check
from ouroboros.boundary.receipts import (
    AdmissionResult,
    CandidateVerdict,
    CandidateVerification,
    CheckStatus,
    PackageVerdict,
    write_receipt,
)
from ouroboros.boundary.reference_check import (
    REFERENCE_LEFT_NO_CHECKS,
    ReferenceCheck,
    check_references,
)
from ouroboros.boundary.tree import tree_digest

if TYPE_CHECKING:
    from ouroboros.boundary.constructor import CheckConstructor
    from ouroboros.core.seed import Seed
    from ouroboros.persistence.event_store import EventStore

REPLACEMENT_REASON = "replacement_checks"
_FEEDBACK_TAIL_CHARS = 400
_COUNTEREXAMPLE_TAIL_CHARS = 1500


@dataclass(frozen=True, slots=True)
class CheckPackageSettings:
    """Resolved configuration for one run."""

    enabled: bool
    constructor_timeout_seconds: int = 600
    check_timeout_seconds: int = 120
    max_construction_attempts: int = 2

    @property
    def attempts(self) -> int:
        return max(1, self.max_construction_attempts)


def default_store_dir(execution_id: str) -> Path:
    """``~/.ouroboros/boundary/<execution_id>``: outside every checkout.

    The product owns this root, so it is resolved once here (a config dir or
    home that is a symlink is followed to its real directory); every
    publication below it then opens each directory without following a link
    (``package.publish_exact``).
    """
    from ouroboros.config.models import get_config_dir

    return Path(os.path.realpath(get_config_dir())) / "boundary" / execution_id


def private_store_dir(store: Path) -> Path:
    """Create ``store`` owner-only (0700); the ``boundary`` parent too when it is one.

    The store holds the package record, receipts, and the base snapshot, none of which carries a held-out input or
    expected value. The mode keeps other users out; code running as the same
    user can still read it (the execution sandbox confines writes, not
    reads), which is why nothing in it may carry held-out values.
    """
    store.mkdir(parents=True, exist_ok=True)
    targets = [store]
    if store.parent.name == "boundary":
        targets.append(store.parent)
    for target in targets:
        try:
            os.chmod(target, 0o700)
        except OSError:
            continue
    return store


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
    interpreter: CheckInterpreter
    """Pinned before the worker starts (``check_env.CheckInterpreter``); every check uses it."""
    contract: RunContract
    """The run contract recorded on the enabled record; every check of the run uses its timeout."""
    package_path: Path | None = None
    criterion_keys: tuple[str, ...] = ()
    base_snapshot: Path | None = None
    reference_check: ReferenceCheck | None = None
    """What the reference check excluded from the bound version (``None``: not run)."""
    exclusions: tuple[tuple[str, str, str], ...] = ()
    """``(boundary_id, check_id, reason)`` for every check excluded by per-check admission."""
    replacement_calls: int = 0
    """Replacement constructor calls made before the worker started (0 or 1)."""
    replacement_outcome: str | None = None

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
    # The package id the journal cites (``CheckPackage.package_id``); ``None``
    # without an admitted package.
    package_id: str | None
    counterexamples: tuple[Counterexample, ...] = ()
    receipt_path: Path | None = None
    uncovered: tuple[str, ...] = field(default_factory=tuple)
    criteria: dict[str, PackageCriterionStatus] = field(default_factory=dict)
    verdicts: dict[str, CriterionVerdict] = field(default_factory=dict)
    artifact_verdict: ArtifactVerdict | None = None
    assignments: dict[str, TierAssignment] = field(default_factory=dict)
    binding_results: dict[str, DeclaredBindingResult] = field(default_factory=dict)
    oracle_results: dict[str, OracleResult] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """JSON-safe summary for run output (no check code, no argv)."""
        return {
            "verdict": self.verdict,
            "artifact_verdict": self.artifact_verdict.value if self.artifact_verdict else None,
            "tiers": {key: item.tier.value for key, item in self.verdicts.items()},
            "reasons": list(self.reasons),
            "boundary_id": self.boundary_id,
            "package_id": self.package_id,
            "failing_checks": [example.check_id for example in self.counterexamples],
            "uncovered_criteria": list(self.uncovered),
            "criteria": {key: status.value for key, status in self.criteria.items()},
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


@dataclass
class _Sealer:
    """Seal, record and admit one package version (shared by every version of a run)."""

    ledger: BoundaryLedger
    seed: Seed
    base: Path
    store: Path
    contract: RunContract
    """The run contract recorded before construction: every check run uses its timeout."""
    interpreter: CheckInterpreter
    execution_id: str
    exclusions: list[tuple[str, str, str]] = field(default_factory=list)

    async def seal_and_admit(
        self, boundary_id: str, package: CheckPackage, reference_check: ReferenceCheck | None
    ) -> tuple[CheckPackage, AdmissionResult, Path]:
        # No record is written for a package not bound to the run's Seed
        # (the ledger checks the same at the freeze, after the write).
        validate_package_for_seed(package, self.seed)
        # A fresh opaque id per package; the record, the journal and every
        # receipt cite it (I2 orders it before the worker).
        package = seal_package(package)
        package_path = write_package_record(package, self.store / "packages")
        # The frozen event records the digest of the same record bytes before
        # the worker starts: a resume in another process detects any edit of
        # the record, visible case values included.
        await self.ledger.record_package_frozen(boundary_id, package, seed=self.seed)
        if reference_check is not None:
            await self.ledger.record_reference_checked(
                boundary_id,
                package_id=package.package_id,
                payload=ReferenceCheckPayload.model_validate(reference_check.payload()),
            )
        admission = await admit_check_package(
            package,
            self.base,
            timeout_seconds=self.contract.check_timeout_seconds,
            interpreter=self.interpreter,
        )
        write_receipt(admission, self.store / "receipts")
        await self.ledger.record_admission(boundary_id, admission)
        self.exclusions.extend(
            (boundary_id, check_id, reason)
            for check_id, reason in sorted((admission.excluded_checks or {}).items())
        )
        return package, admission, package_path

    async def reference_checked(
        self, package: CheckPackage, references: Any
    ) -> tuple[CheckPackage | None, ReferenceCheck | None]:
        """Derived-expectation admission (``boundary/reference_check.py``), before the seal."""
        if references is None or not package.oracles:
            return package, None
        checked, report = await check_references(
            package,
            references,
            seed=self.seed,
            interpreter=self.interpreter,
            timeout_seconds=self.contract.check_timeout_seconds,
        )
        return (checked if checked.checks else None), report


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

    The constructor is asked for a check for every criterion; nothing is
    removed or skipped by kind. Each version is admitted per check
    (``boundary/per_check.py``). With the product policy, every criterion an
    admitted version leaves without an admitted check gets one replacement
    call before the worker starts (``_replace_uncovered``). A criterion still
    without an admitted check is uncovered and the legacy verifier decides it.

    Raises ``BoundaryOrderError`` / ``BoundaryLeakError`` from the ledger; the
    caller must not dispatch the worker in that case.
    """
    ledger = BoundaryLedger(event_store)
    # First, before anything that can fail: the run had the check package on.
    # A resume reads it back, so a missing boundary is undecided, never legacy.
    # The one run contract: admission and every later check read its timeout.
    contract = RunContract(check_timeout_seconds=settings.check_timeout_seconds)
    await ledger.record_check_package_enabled(execution_id, contract)
    store = private_store_dir(store_dir or default_store_dir(execution_id))
    digest = seed_digest(seed)
    base = base_checkout.resolve()
    keys = seed_criterion_keys(seed)
    versions: list[str] = []
    feedback: list[str] = []
    previous: str | None = None
    previous_reason = ""
    package: CheckPackage | None = None
    admission: AdmissionResult | None = None
    package_path: Path | None = None
    failure_reason: str | None = None
    # Every check runs confined by the execution sandbox, with the project's
    # interpreter when one exists, pinned here (boundary/check_env.py).
    interpreter = resolve_check_interpreter(base)
    sealer = _Sealer(ledger, seed, base, store, contract, interpreter, execution_id)
    reference_check: ReferenceCheck | None = None

    for attempt in range(1, settings.attempts + 1):
        boundary_id = boundary_version_id(execution_id, attempt)
        outcome = await constructor.construct(seed, base, feedback=feedback)
        package, admission, package_path = outcome.package, None, None
        references = getattr(outcome, "references", None)
        reference_check = None
        if package is not None:
            package, reference_check = await sealer.reference_checked(package, references)
            if package is None:
                outcome = replace(outcome, package=None, failure_reason=REFERENCE_LEFT_NO_CHECKS)
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
            package, admission, package_path = await sealer.seal_and_admit(
                boundary_id, package, reference_check
            )
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
    replacement = _ReplacementReport()
    if (
        failure_reason is None
        and package is not None
        and admission is not None
        and package_path is not None
    ):
        replaced = await _replace_uncovered(
            constructor,
            sealer,
            versions,
            bound,
            package,
            admission,
            references_kept=reference_check,
            report=replacement,
        )
        if replaced is not None:
            bound, package, admission, package_path, reference_check = replaced
    if package is not None and failure_reason is not None:
        # A frozen but unadmitted version cannot host a worker. Seal a final
        # version that records the absence of an admitted package.
        final_id = boundary_version_id(execution_id, len(versions) + 1)
        await ledger.record_construction_failed(
            final_id,
            seed_digest=digest,
            input_digest=package.input_digest,
            reason=f"no_admitted_package:{failure_reason}",
        )
        await ledger.record_superseded(bound, superseded_by=final_id, reason=failure_reason)
        versions.append(final_id)
        bound = final_id

    admitted = admission is not None and admission.verdict is PackageVerdict.ADMITTED
    snapshot: Path | None = None
    tiers = admission.check_tiers if admission is not None else None
    if admitted and any(tier == CheckTier.U.value for tier in (tiers or {}).values()):
        # A late binding is validated against the base after the worker has
        # stopped; keep the base outside every checkout until then. Taken
        # before the actor start: the journal records a worker start only
        # once everything that start depends on exists.
        snapshot = snapshot_base(base, store)
    state = BoundaryRunState(
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
        contract=contract,
        criterion_keys=keys,
        base_snapshot=snapshot,
        reference_check=reference_check,
        exclusions=tuple(sealer.exclusions),
        replacement_calls=replacement.calls,
        replacement_outcome=replacement.outcome,
    )
    # Last: the worker start is recorded only after every prerequisite above.
    await ledger.record_actor_started(
        execution_id,
        [bound],
        workspace=worker_workspace,
        runtime=runtime_label,
        packages=(package,) if admitted and package is not None else (),
    )
    if state.admitted:
        _LIVE_STATES[execution_id] = state
    return state


@dataclass
class _ReplacementReport:
    calls: int = 0
    outcome: str | None = None
    """``admitted``, ``not_admitted``, ``construction_failed`` or ``None`` (no call)."""


async def _replace_uncovered(
    constructor: Any,
    sealer: _Sealer,
    versions: list[str],
    bound: str,
    package: CheckPackage,
    admission: AdmissionResult,
    *,
    references_kept: ReferenceCheck | None,
    report: _ReplacementReport,
) -> tuple[str, CheckPackage, AdmissionResult, Path, ReferenceCheck | None] | None:
    """One replacement call for every criterion left without an admitted check.

    The new version (the admitted checks kept, plus the replacements) goes
    through the reference check, the seal and per-check admission, and
    supersedes ``bound`` when admitted. When the call fails, or the new
    version is not admitted, the new version stays sealed (its seal or
    admission receipt says why) and is recorded abandoned in favor of
    ``bound``, which stays the version the worker is bound to (a version is
    superseded only by a later one). Returns the
    new ``(boundary_id, package, admission, path, reference_check)`` or
    ``None``.
    """
    call = getattr(constructor, "construct_replacements", None)
    # The bound version's own exclusions, by its own check ids (ids are
    # re-minted in the merged version, so nothing is carried by id).
    excluded: dict[str, str] = dict(admission.excluded_checks or {})
    targets = replacement_targets(package, excluded)
    if call is None or not targets:
        return None
    keys = package.criterion_keys
    numbers = {keys.index(key) + 1: why_excluded(reason) for key, reason in targets.items()}
    new_id = boundary_version_id(sealer.execution_id, len(versions) + 1)
    report.calls = 1
    outcome = await call(sealer.seed, sealer.base, targets=numbers)
    candidate: CheckPackage | None = outcome.package
    reason = outcome.failure_reason or "constructor_failed"
    reference_check: ReferenceCheck | None = None
    if candidate is not None:
        candidate, reference_check = await sealer.reference_checked(
            candidate, getattr(outcome, "references", None)
        )
        if candidate is None:
            reason = REFERENCE_LEFT_NO_CHECKS
    merged: CheckPackage | None = None
    if candidate is not None:
        merged, _still = merge_replacement(package, excluded, candidate, targets, sealer.seed)
        if len(merged.checks) == len(package.checks) - len(excluded):
            # The replacement linked none of its targets.
            merged, reason = None, "replacement_linked_no_target"
    versions.append(new_id)
    if merged is None:
        report.outcome = "construction_failed"
        await sealer.ledger.record_construction_failed(
            new_id,
            seed_digest=package.seed_digest,
            input_digest=outcome.input_digest,
            reason=f"replacement_failed:{reason}",
        )
        # The failed replacement is closed in favor of ``bound``: a version is
        # superseded only by a later one, and the worker binds ``bound``.
        await sealer.ledger.record_replacement_abandoned(new_id, bound=bound)
        return None
    assert candidate is not None
    merged_report = _merged_reference_check(
        package, excluded, references_kept, candidate, reference_check, targets, merged
    )
    new_package, new_admission, new_path = await sealer.seal_and_admit(
        new_id, merged, merged_report
    )
    if new_admission.verdict is not PackageVerdict.ADMITTED:
        report.outcome = "not_admitted"
        # Its admission receipt records why; it is closed in favor of ``bound``.
        await sealer.ledger.record_replacement_abandoned(new_id, bound=bound)
        return None
    report.outcome = "admitted"
    await sealer.ledger.record_superseded(bound, superseded_by=new_id, reason=REPLACEMENT_REASON)
    return new_id, new_package, new_admission, new_path, merged_report


def _oracle_identity(spec: OracleSpec) -> bytes:
    """An oracle apart from its check id (the one field a merge re-mints)."""
    return canonical_json_bytes(spec.model_dump(mode="json", exclude={"check_id"}))


def _merged_reference_check(
    package: CheckPackage,
    excluded: Mapping[str, str],
    kept_report: ReferenceCheck | None,
    candidate: CheckPackage,
    candidate_report: ReferenceCheck | None,
    targets: Mapping[str, str],
    merged: CheckPackage,
) -> ReferenceCheck | None:
    """The reference check of the merged version, against the merged package's own ids.

    The kept oracles' exclusions were counted against the bound version's ids
    and the replacement's against the candidate's; the merge re-mints every
    id (``coverage.merge_replacement``). Each merged oracle is matched to its
    source oracle by everything but its id, first come first served over the
    bound version's admitted oracles and then the candidate's (the order the
    merge keeps), and carries that source's count. Oracles identical but for
    their id are assigned in that merge order, kept ones first. A criterion is reported
    uncovered only while no check of the merged package links it; the
    replacement's report counts only for its targets.
    """
    if kept_report is None and candidate_report is None:
        return None
    sources: dict[bytes, list[int]] = {}
    for spec in package.oracles:
        if spec.check_id not in excluded:
            count = (kept_report.excluded if kept_report else {}).get(spec.check_id, 0)
            sources.setdefault(_oracle_identity(spec), []).append(count)
    for spec in candidate.oracles:
        count = (candidate_report.excluded if candidate_report else {}).get(spec.check_id, 0)
        sources.setdefault(_oracle_identity(spec), []).append(count)
    counts: dict[str, int] = {}
    for spec in merged.oracles:
        queue = sources.get(_oracle_identity(spec))
        if not queue:
            raise CheckPackageError("a merged oracle has no source oracle")
        count = queue.pop(0)
        if count:
            counts[spec.check_id] = count
    linked = {link.criterion_key for check in merged.checks for link in check.assertions}
    uncovered = {
        **(kept_report.uncovered if kept_report else {}),
        **{
            key: reason
            for key, reason in (candidate_report.uncovered if candidate_report else {}).items()
            if key in targets
        },
    }
    return ReferenceCheck(
        excluded=counts,
        uncovered={key: reason for key, reason in uncovered.items() if key not in linked},
    )


# The admitted boundary of each run still in progress in this process, with
# its held-out cases. A run resumed in the same process (its controller task
# died, the process did not) re-derives the full package decision from here;
# a run resumed in another process finds nothing, runs no check and leaves
# the covered criteria undecided (``boundary/resume.py``). Dropped at the run's
# final verdict.
_LIVE_STATES: dict[str, BoundaryRunState] = {}


def live_state(execution_id: str) -> BoundaryRunState | None:
    """The in-memory boundary state of ``execution_id`` in this process, if any."""
    return _LIVE_STATES.get(execution_id)


def forget_live_state(state: BoundaryRunState | None) -> None:
    """Drop ``state`` from the in-process registry (after the final verdict)."""
    if state is not None and _LIVE_STATES.get(state.execution_id) is state:
        del _LIVE_STATES[state.execution_id]


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


UNAVAILABLE_VERDICT = "unavailable"


def unavailable_line(state: BoundaryRunState) -> str:
    """The one line a run prints when no package was admitted."""
    return (
        f"Check package unavailable ({state.failure_reason or 'no_admitted_package'}); "
        "legacy verification decided this run."
    )


def _candidate_unchanged(
    digest: str, verification: CandidateVerification | None, candidate: Path
) -> bool:
    """Whether verification ran on the tree digested before it, and the tree is still that one.

    Without a verification there is nothing to distrust (every check that
    should have run is undecided anyway).
    """
    if verification is None:
        return True
    return (
        verification.artifact_tree_digest == digest
        and verification.artifact_tree_digest_after == digest
        and tree_digest(candidate) == digest
    )


async def verify_check_package(
    state: BoundaryRunState,
    *,
    event_store: EventStore,
    candidate_checkout: Path,
    declared_entry_points: Mapping[str, Sequence[Any]] | None = None,
    base_run_cache: dict[str, Any] | None = None,
    phase: Literal["final", "resumed"] = "final",
) -> BoundaryVerdict:
    """Bind, then run the unchanged package on the candidate; record everything.

    ``declared_entry_points`` maps a criterion key to the worker's declared
    ``entry_points`` (typed evidence). Late-binding admission and every check
    run under the run contract the run recorded before its worker started
    (``state.contract``), never under settings resolved later. Order: final bindings
    (``boundary.binding.recorded``), candidate verification (plus one R3
    re-run of transiently indeterminate checks). A resumed run records its own
    bindings (``phase="resumed"``) and verification the same way.

    Without an admitted package the verdict is ``unavailable`` with no
    per-criterion verdicts: every criterion is uncovered, so the legacy
    verifier decides it exactly as if the check package were off.
    """
    if state.package is None or state.admission is None:
        return BoundaryVerdict(
            verdict=UNAVAILABLE_VERDICT,
            reasons=(state.failure_reason or "no_admitted_package",),
            boundary_id=state.boundary_id,
            package_id=None,
        )
    ledger = BoundaryLedger(event_store)
    package = state.package
    candidate = candidate_checkout.resolve()
    candidate_digest = tree_digest(candidate)
    assignments, results = await assign_tiers(
        package,
        base=state.base_snapshot,
        contract=state.contract,
        declared=declared_entry_points,
        expected_base_digest=state.admission.base_tree_digest,
        admission=state.admission,
        run_options={"interpreter": state.interpreter},
        base_run_cache=base_run_cache,
    )
    await ledger.record_bindings(
        state.boundary_id,
        package_id=package.package_id,
        payload=bindings_payload(assignments, results, phase=phase),
    )
    bound = await verify_with_bindings(
        package,
        candidate,
        assignments,
        contract=state.contract,
        interpreter=state.interpreter,
    )
    receipt: Path | None = None
    for run in (bound.first, bound.rerun):
        if run is not None:
            receipt = write_receipt(run, state.store_dir / "receipts")
            await ledger.record_candidate_verification(state.boundary_id, run)
    verification = bound.effective
    verdicts = criterion_verdicts(
        package,
        verification,
        admission=state.admission,
        assignments=assignments,
        candidate_identity_ok=_candidate_unchanged(candidate_digest, verification, candidate),
    )
    overall = artifact_verdict(item.status for item in verdicts.values())
    if overall is ArtifactVerdict.UNVERIFIED:
        reasons: tuple[str, ...] = ("all_unverified",)
    else:
        reasons = verification.reasons if verification is not None else ("no_bound_checks",)
    return BoundaryVerdict(
        verdict=overall.value,
        reasons=reasons,
        boundary_id=state.boundary_id,
        package_id=package.package_id,
        counterexamples=_counterexamples(verification) if verification is not None else (),
        receipt_path=receipt,
        uncovered=(
            *(item.criterion_key for item in package.uncovered),
            # Criteria that lost every admitted check to per-check admission.
            *criteria_without_admitted_check(package, state.admission.excluded_checks or {}),
        ),
        criteria={key: item.status for key, item in verdicts.items()},
        verdicts=verdicts,
        artifact_verdict=overall,
        assignments=assignments,
        binding_results=results,
        oracle_results={
            check.check_id: check.oracle_result
            for check in (verification.checks if verification is not None else ())
            if check.oracle_result
        },
    )


def repair_text(verdict: BoundaryVerdict, criterion_key: str) -> str | None:
    """Counterexample repair for one failing criterion, or ``None``.

    Visible cases are shown in full. Held-out cases are never mentioned, not
    even as a count: the per-attempt gate does not run them at all, and a
    message must not say whether any exist. For a worker-declared binding
    (tier A') the message names the binding the check ran through.
    """
    item = verdict.verdicts.get(criterion_key)
    if item is None or item.status is not PackageCriterionStatus.FAIL:
        return None
    lines = ["The frozen check package failed this criterion on your workspace."]
    if item.tier is CheckTier.A_PRIME and item.binding is not None:
        lines.append(
            "It called your declared entry point: "
            f"{item.binding.get('call_kind')} {item.binding.get('symbol')}"
            + (
                f" with arg_map {json.dumps(item.binding.get('arg_map'), sort_keys=True)}"
                if item.binding.get("arg_map")
                else ""
            )
            + "."
        )
    elif item.binding is not None:
        lines.append(f"It called {item.binding.get('symbol')}.")
    shown = False
    for check_id in item.check_ids:
        counter = repair_lines(verdict.oracle_results.get(check_id))
        lines.extend(counter)
        shown = shown or bool(counter)
    if not shown:
        # Script checks only: an oracle check's counterexamples come from its
        # structured result above, never from its output.
        for example in verdict.counterexamples:
            if (
                example.check_id in item.check_ids
                and example.check_id not in verdict.oracle_results
                and example.output_tail.strip()
            ):
                lines.append(example.output_tail.strip()[-600:])
    return "\n".join(lines)


def repair_lines(result: OracleResult | None, *, limit: int = 5) -> list[str]:
    """Counterexamples a worker may see: failing visible cases only."""
    failing = [
        case
        for case in (result.cases if result is not None else ())
        if not case.passed and not case.held_out
    ]
    return [f"- {case.detail or case.case_id}" for case in failing[:limit]]


def render_preparation(state: BoundaryRunState) -> list[str]:
    """Plain-text lines describing the boundary the worker is bound to."""
    lines = [f"Check package boundary: {state.boundary_id}"]
    others = [version for version in state.versions if version != state.boundary_id]
    if others:
        lines.append(f"Other versions (not bound): {', '.join(others)}")
    if state.exclusions:
        lines.append(
            "Checks excluded at admission: "
            + ", ".join(
                f"{version}: {check_id} ({reason})"
                for version, check_id, reason in state.exclusions
            )
        )
    if state.replacement_calls:
        lines.append(f"Replacement checks: one constructor call, {state.replacement_outcome}")
    if state.admitted and state.package is not None:
        package = state.package
        roles = ", ".join(f"{check.check_id} ({check.role.value})" for check in package.checks)
        # The opaque id, never the unkeyed digest: even a 64-bit prefix of
        # that digest would confirm guessed held-out values.
        lines.append(f"Package {package.package_id[:16]} admitted on the base: {roles}")
        lines.append(
            f"Checks run with {state.interpreter.path} ({state.interpreter.source}), pinned by "
            "digest, confined by the execution sandbox (writes only in the check's copy, "
            "no network, allowlisted environment)"
        )
        if package.uncovered:
            lines.append(
                f"Uncovered criteria: {len(package.uncovered)} of {len(package.criterion_keys)}"
            )
    else:
        lines.append(f"No admitted package ({state.failure_reason}).")
    return lines


def render_verdict(verdict: BoundaryVerdict) -> list[str]:
    """Plain-text lines: verdict first, then each counterexample."""
    lines = [f"Check package verdict: {verdict.verdict}"]
    if verdict.verdicts:
        tiers = ", ".join(
            f"{key}: {item.status.value} (tier {item.tier.label})"
            for key, item in verdict.verdicts.items()
        )
        lines.append(f"Criteria: {tiers}")
    if verdict.verdict not in (CandidateVerdict.PASS.value, ArtifactVerdict.UNVERIFIED.value) and (
        verdict.reasons
    ):
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
