"""Oracle first, binding after the worker stops: the verification flow.

Order (enforced by ``BoundaryLedger``):

1. before dispatch: the oracle package is hashed and sealed
   (``record_package_frozen``), admitted on the base with the default
   bindings (``admit_check_package(..., check_tiers=admission_tiers(...))``),
   and the actor start is recorded;
2. after the worker stops: every oracle whose default binding does not
   resolve takes the worker's declared binding, if any, through
   ``validate_declared_binding`` (grammar, static rules, one base run, no
   model call); ``assign_tiers`` fixes the tier and binding of every check;
   ``BoundaryLedger.record_bindings`` records them;
3. ``verify_with_bindings`` runs the unchanged package on the candidate
   through those bindings (checks of unverified criteria are not run), with
   one zero-model re-run of checks that ended indeterminate for a transient
   reason; ``acceptance.criterion_verdicts`` turns the result into
   per-criterion verdicts and ``acceptance.artifact_verdict`` into the
   artifact verdict.

The base must still be available after the worker stops, for the static diff
rule and the base run of a declared binding. ``snapshot_base`` keeps a copy
outside every checkout, pinned by the admission's base tree digest; a
snapshot whose digest no longer matches is not used (the declared binding is
then indeterminate, ``base_snapshot_mismatch``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from typing import Any

from ouroboros.boundary.acceptance import rerunnable_checks
from ouroboros.boundary.admission import (
    ADMISSION_TIMEOUT_SECONDS,
    BINDING_ADMISSION_TIMEOUT_SECONDS,
    BindingAdmission,
    CandidateVerdict,
    CandidateVerification,
    CheckStatus,
    admit_binding,
    verify_candidate,
)
from ouroboros.boundary.binding import (
    BindingValidation,
    CheckTier,
    TierAssignment,
    assign_tier,
    script_check_tier,
    validate_declared_binding_static,
)
from ouroboros.boundary.oracle_build import criterion_text
from ouroboros.boundary.package import CheckPackage, seed_criterion_keys
from ouroboros.boundary.tree import copy_checkout, tree_digest, tree_manifest
from ouroboros.core.seed import Seed

BASE_SNAPSHOT_DIR = "base_snapshot"
BASE_MANIFEST_FILE = "base_manifest.json"


def snapshot_base(base: Path, store_dir: Path) -> tuple[Path, Path]:
    """Copy the base checkout and its file manifest under ``store_dir``."""
    snapshot = store_dir / BASE_SNAPSHOT_DIR
    if snapshot.exists():
        shutil.rmtree(snapshot)
    store_dir.mkdir(parents=True, exist_ok=True)
    copy_checkout(base.resolve(), snapshot)
    manifest_path = store_dir / BASE_MANIFEST_FILE
    manifest_path.write_text(json.dumps(tree_manifest(snapshot), sort_keys=True), "utf-8")
    return snapshot, manifest_path


def _criterion_texts(seed: Seed) -> dict[str, str]:
    keys = seed_criterion_keys(seed)
    return {key: criterion_text(seed, index) for index, key in enumerate(keys)}


def admission_tiers(package: CheckPackage, seed: Seed, base: Path) -> dict[str, str]:
    """Tier of every check as admitted on the base (before any worker binding).

    An oracle check is ``A`` when its default binding resolves, else ``U``
    (it may still become ``A_prime`` through a declared binding). A
    model-written script check is ``A`` when its imports resolve at the base
    or are named in the criterion text, else ``U``.
    """
    texts = _criterion_texts(seed)
    files = {item.path: item.content for item in package.files}
    tiers: dict[str, str] = {}
    for check in package.checks:
        oracle = package.oracle_for(check.check_id)
        if oracle is not None:
            tiers[check.check_id] = (CheckTier.A if oracle.default_resolves else CheckTier.U).value
            continue
        script = next((files[arg] for arg in check.argv[1:] if arg in files), None)
        text = " ".join(texts.get(link.criterion_key, "") for link in check.assertions)
        tier, _reason = (
            script_check_tier(script, base=base, criterion_text=text)
            if script is not None
            else (CheckTier.A, "no_script")
        )
        tiers[check.check_id] = tier.value
    return tiers


@dataclass(frozen=True, slots=True)
class DeclaredBindingResult:
    """Static validation and, when it passed, the base run of one declared binding."""

    validation: BindingValidation
    admission: BindingAdmission | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_key": self.validation.criterion_key,
            "valid": self.validation.valid,
            "indeterminate": self.validation.indeterminate,
            "reason": self.validation.reason,
            "binding": self.validation.binding.to_dict() if self.validation.binding else None,
            "location": self.validation.location,
            "exists_at_base": self.validation.exists_at_base,
            "changed_by_artifact": self.validation.changed_by_artifact,
            "base_run": (
                None
                if self.admission is None
                else {
                    "status": self.admission.execution.status.value,
                    "reason": self.admission.reason,
                    "signature_seen": self.admission.execution.signature_seen,
                    "return_code": self.admission.execution.return_code,
                }
            ),
        }


async def validate_declared_binding(
    package: CheckPackage,
    check_id: str,
    raw: object,
    *,
    artifact: Path,
    base: Path,
    base_manifest: Mapping[str, str] | None = None,
    expected_base_digest: str | None = None,
    timeout_seconds: int = BINDING_ADMISSION_TIMEOUT_SECONDS,
    run_options: Mapping[str, Any] | None = None,
    base_run_cache: dict[str, BindingAdmission] | None = None,
) -> DeclaredBindingResult:
    """Validate one worker-declared binding for an oracle check.

    Static rules first (``binding.validate_declared_binding_static``); then
    exactly one run of the frozen oracle through the binding on an isolated
    copy of ``base`` (``admission.admit_binding``), whose outcome must match
    the oracle's role. ``expected_base_digest`` pins ``base`` (for example to
    the admission's ``base_tree_digest``); a mismatch is indeterminate.
    ``run_options`` are passed to ``admit_binding`` (for example ``env`` and
    ``interpreter`` where the admission executor accepts them).
    ``base_run_cache`` keeps one base run per (check, binding): a caller that
    validates the same late binding again (a later repair attempt, then the
    final verification) reuses it, so each late binding has exactly one base
    run, with no retry after a timeout.
    """
    oracle = package.oracle_for(check_id)
    if oracle is None:
        raise ValueError(f"{check_id} is not an oracle check")
    validation = validate_declared_binding_static(
        raw,
        criterion_key=oracle.criterion_key,
        params=oracle.params,
        call_kind=oracle.call_kind,
        artifact=artifact,
        base=base,
        base_manifest=base_manifest,
    )
    if not validation.valid or validation.binding is None:
        return DeclaredBindingResult(validation)
    if expected_base_digest is not None and tree_digest(base) != expected_base_digest:
        return DeclaredBindingResult(
            validation.model_copy(
                update={
                    "valid": False,
                    "indeterminate": True,
                    "reason": "base_snapshot_mismatch",
                }
            )
        )
    key = f"{check_id}:{json.dumps(validation.binding.to_dict(), sort_keys=True)}"
    admission = (base_run_cache or {}).get(key)
    if admission is None:
        admission = await admit_binding(
            package,
            check_id,
            validation.binding,
            base,
            timeout_seconds=timeout_seconds,
            **dict(run_options or {}),
        )
        if base_run_cache is not None:
            base_run_cache[key] = admission
    if admission.valid:
        return DeclaredBindingResult(
            validation.model_copy(update={"reason": admission.reason}), admission
        )
    return DeclaredBindingResult(
        validation.model_copy(
            update={
                "valid": False,
                "indeterminate": admission.indeterminate,
                "reason": admission.reason,
            }
        ),
        admission,
    )


async def assign_tiers(
    package: CheckPackage,
    *,
    artifact: Path,
    base: Path | None,
    declared: Mapping[str, Sequence[Any]] | None = None,
    base_manifest: Mapping[str, str] | None = None,
    expected_base_digest: str | None = None,
    admitted_tiers: Mapping[str, str] | None = None,
    timeout_seconds: int = BINDING_ADMISSION_TIMEOUT_SECONDS,
    run_options: Mapping[str, Any] | None = None,
    base_run_cache: dict[str, BindingAdmission] | None = None,
) -> tuple[dict[str, TierAssignment], dict[str, DeclaredBindingResult]]:
    """Tier and binding for every check, after the worker has stopped.

    ``declared`` maps a criterion key to the raw ``entry_points`` the worker
    declared for it; the first entry is used. A declared binding is consulted
    only where the default binding does not resolve. Without a usable base
    (``base`` is ``None``) a declared binding cannot be admitted and is
    indeterminate (``base_unavailable``).
    """
    declared = declared or {}
    admitted = dict(admitted_tiers or {})
    assignments: dict[str, TierAssignment] = {}
    results: dict[str, DeclaredBindingResult] = {}
    for check in package.checks:
        oracle = package.oracle_for(check.check_id)
        key = next(iter(link.criterion_key for link in check.assertions))
        if oracle is None:
            tier = CheckTier(admitted.get(check.check_id, CheckTier.A.value))
            assignments[check.check_id] = TierAssignment(
                key,
                check.check_id,
                tier,
                None,
                None,
                "run" if tier is CheckTier.A else "unverified",
                "script_imports_resolve" if tier is CheckTier.A else "no_binding",
            )
            continue
        entries = list(declared.get(oracle.criterion_key) or ())
        result: DeclaredBindingResult | None = None
        if not oracle.default_resolves and entries:
            if base is None:
                result = DeclaredBindingResult(
                    BindingValidation(
                        criterion_key=oracle.criterion_key,
                        valid=False,
                        indeterminate=True,
                        reason="base_unavailable",
                    )
                )
            else:
                result = await validate_declared_binding(
                    package,
                    check.check_id,
                    entries[0],
                    artifact=artifact,
                    base=base,
                    base_manifest=base_manifest,
                    expected_base_digest=expected_base_digest,
                    timeout_seconds=timeout_seconds,
                    run_options=run_options,
                    base_run_cache=base_run_cache,
                )
            results[check.check_id] = result
        assignment = assign_tier(
            criterion_key=oracle.criterion_key,
            check_id=check.check_id,
            default_binding=oracle.default_binding,
            default_resolves=oracle.default_resolves,
            declared=None if result is None else result.validation,
        )
        assignments[check.check_id] = assignment
    return assignments, results


@dataclass(frozen=True, slots=True)
class BoundVerification:
    """The candidate run through the recorded bindings, and its R3 re-run."""

    first: CandidateVerification | None
    rerun: CandidateVerification | None

    @property
    def effective(self) -> CandidateVerification | None:
        """The verification that decides: the re-run when one happened."""
        if self.first is None or self.rerun is None:
            return self.first
        rerun = {check.check_id: check for check in self.rerun.checks}
        merged = tuple(rerun.get(check.check_id, check) for check in self.first.checks)
        mutated = any(check.mutated_paths for check in merged) or (
            self.first.artifact_tree_digest != self.first.artifact_tree_digest_after
            or self.rerun.artifact_tree_digest != self.rerun.artifact_tree_digest_after
        )
        statuses = {check.status for check in merged}
        # Same order as verify_candidate: mutation, then failure, then undecided.
        if mutated:
            verdict = CandidateVerdict.INDETERMINATE
        elif CheckStatus.VIOLATED in statuses:
            verdict = CandidateVerdict.FAIL
        elif CheckStatus.INDETERMINATE in statuses:
            verdict = CandidateVerdict.INDETERMINATE
        else:
            verdict = CandidateVerdict.PASS
        return self.rerun.model_copy(
            update={
                "checks": merged,
                "verdict": verdict,
                "reasons": tuple(
                    f"{check.reason}:{check.check_id}"
                    for check in merged
                    if check.status is not CheckStatus.EXPECTED
                ),
                "protected_bytes_mutated": mutated,
                "artifact_tree_digest": self.first.artifact_tree_digest,
                "artifact_tree_digest_after": self.rerun.artifact_tree_digest_after,
                "started_at": self.first.started_at,
            }
        )


async def verify_with_bindings(
    package: CheckPackage,
    candidate: Path,
    assignments: Mapping[str, TierAssignment],
    *,
    timeout_seconds: int = ADMISSION_TIMEOUT_SECONDS,
    rerun_indeterminate: bool = True,
    **run_options: Any,
) -> BoundVerification:
    """Run the checks whose tier is ``A`` or ``A_prime`` through their bindings.

    ``run_options`` are passed to ``verify_candidate`` (for example ``env`` and
    ``interpreter`` where the admission executor accepts them).
    """
    to_run = [
        check_id
        for check_id, assignment in assignments.items()
        if assignment.tier in (CheckTier.A, CheckTier.A_PRIME)
    ]
    if not to_run:
        return BoundVerification(None, None)
    bindings = {
        check_id: assignment.binding
        for check_id, assignment in assignments.items()
        if assignment.binding is not None and check_id in to_run
    }
    tiers = {check_id: assignments[check_id].tier for check_id in to_run}
    first = await verify_candidate(
        package,
        candidate,
        timeout_seconds=timeout_seconds,
        bindings=bindings,
        only_checks=to_run,
        check_tiers=tiers,
        **run_options,
    )
    again = rerunnable_checks(first) if rerun_indeterminate else ()
    if not again:
        return BoundVerification(first, None)
    rerun = await verify_candidate(
        package,
        candidate,
        timeout_seconds=timeout_seconds,
        bindings={key: value for key, value in bindings.items() if key in again},
        only_checks=list(again),
        check_tiers={key: tiers[key] for key in again},
        **run_options,
    )
    return BoundVerification(first, rerun)


def bindings_payload(
    assignments: Mapping[str, TierAssignment],
    results: Mapping[str, DeclaredBindingResult],
    *,
    phase: str,
) -> dict[str, Any]:
    """Journal payload for ``boundary.binding.recorded`` (bindings are data, no code)."""
    return {
        "schema_version": "ouroboros.binding_record.v1",
        "phase": phase,
        "checks": [
            {
                **assignment.to_dict(),
                "declared": results[check_id].to_dict() if check_id in results else None,
            }
            for check_id, assignment in sorted(assignments.items())
        ],
    }


__all__ = [
    "BoundVerification",
    "DeclaredBindingResult",
    "admission_tiers",
    "assign_tiers",
    "bindings_payload",
    "snapshot_base",
    "validate_declared_binding",
    "verify_with_bindings",
]
