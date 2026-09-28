"""Oracle first, binding after the worker stops: the verification flow.

Order (enforced by ``BoundaryLedger``):

1. before dispatch: the oracle package is hashed and sealed
   (``record_package_frozen``), admitted on the base with the default
   bindings (``admit_check_package``, which also fixes each check's tier from
   what its base run showed), and the actor start is recorded;
2. after the worker stops: every oracle whose admitted tier is not ``A``
   takes the worker's declared binding, if any, through
   ``validate_declared_binding`` (grammar, then one base run, no model
   call); ``assign_tiers`` fixes the tier and binding of every check;
   ``BoundaryLedger.record_bindings`` records them;
3. ``verify_with_bindings`` runs the unchanged package on the candidate
   through those bindings (checks of unverified criteria are not run), with
   one zero-model re-run of checks that ended indeterminate for a transient
   reason; ``acceptance.criterion_verdicts`` turns the result into
   per-criterion verdicts and ``acceptance.artifact_verdict`` into the
   artifact verdict.

The base must still be available after the worker stops, for the base run
of a declared binding. ``snapshot_base`` keeps a copy
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
from typing import Any, Literal

from ouroboros.boundary.acceptance import rerunnable_checks
from ouroboros.boundary.admission import (
    ADMISSION_TIMEOUT_SECONDS,
    BINDING_ADMISSION_TIMEOUT_SECONDS,
    BindingAdmission,
    admit_binding,
    verify_candidate,
)
from ouroboros.boundary.binding import (
    BindingValidation,
    CheckTier,
    TierAssignment,
    assign_tier,
    parse_declared_binding,
)
from ouroboros.boundary.events import BindingsPayload
from ouroboros.boundary.package import CheckPackage
from ouroboros.boundary.per_check import EXCLUDED_STATUS_HINT, exclusion_reason_for_role
from ouroboros.boundary.receipts import CandidateVerdict, CandidateVerification, CheckStatus
from ouroboros.boundary.tree import copy_checkout, tree_digest

BASE_SNAPSHOT_DIR = "base_snapshot"


def snapshot_base(base: Path, store_dir: Path) -> Path:
    """Copy the base checkout under ``store_dir`` (for the base run of a declared binding)."""
    snapshot = store_dir / BASE_SNAPSHOT_DIR
    if snapshot.exists():
        shutil.rmtree(snapshot)
    store_dir.mkdir(parents=True, exist_ok=True)
    copy_checkout(base.resolve(), snapshot)
    return snapshot


RUNNABLE_TIERS = frozenset({CheckTier.A, CheckTier.A_PRIME, CheckTier.S})
"""Tiers a verification runs; ``U`` and ``C`` checks are never run."""


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
    base: Path,
    expected_base_digest: str | None = None,
    timeout_seconds: int = BINDING_ADMISSION_TIMEOUT_SECONDS,
    run_options: Mapping[str, Any] | None = None,
    base_run_cache: dict[str, BindingAdmission] | None = None,
) -> DeclaredBindingResult:
    """Validate one worker-declared binding for an oracle check.

    The grammar first (``binding.parse_declared_binding``); then
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
    validation = parse_declared_binding(
        raw,
        criterion_key=oracle.criterion_key,
        params=oracle.params,
        call_kind=oracle.call_kind,
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
    # One base run per (check, binding, case selection): the gate's visible-only
    # run never stands in for the final run with every case.
    selection = "all" if (run_options or {}).get("include_held_out", True) else "visible"
    key = f"{check_id}:{selection}:{json.dumps(validation.binding.to_dict(), sort_keys=True)}"
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
    base: Path | None,
    declared: Mapping[str, Sequence[Any]] | None = None,
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
    indeterminate (``base_unavailable``). A check whose admitted tier is
    ``C`` (excluded by the per-check rule) is assigned ``C`` and never run.
    """
    declared = declared or {}
    admitted = dict(admitted_tiers or {})
    assignments: dict[str, TierAssignment] = {}
    results: dict[str, DeclaredBindingResult] = {}
    for check in package.checks:
        oracle = package.oracle_for(check.check_id)
        key = next(iter(link.criterion_key for link in check.assertions))
        if admitted.get(check.check_id) == CheckTier.C.value:
            # Excluded at admission (per-check rule, ``boundary/per_check.py``):
            # never run, never counted for its criterion.
            assignments[check.check_id] = TierAssignment(
                key,
                check.check_id,
                CheckTier.C,
                None,
                None,
                EXCLUDED_STATUS_HINT,
                exclusion_reason_for_role(check.role),
            )
            continue
        if oracle is None:
            # A script check claims no target: it runs, its failure counts,
            # and its pass is advisory (``acceptance.criterion_verdicts``).
            assignments[check.check_id] = TierAssignment(
                key, check.check_id, CheckTier.S, None, None, "run", "script_check"
            )
            continue
        entries = list(declared.get(oracle.criterion_key) or ())
        result: DeclaredBindingResult | None = None
        tier_a = admitted.get(check.check_id) == CheckTier.A.value
        if not tier_a and entries:
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
                    base=base,
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
            default_tier_a=tier_a,
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
    """Run the checks with a runnable tier (``A``, ``A_prime``, ``S``) through their bindings.

    ``run_options`` are passed to ``verify_candidate`` (for example ``env`` and
    ``interpreter`` where the admission executor accepts them).
    """
    to_run = [
        check_id
        for check_id, assignment in assignments.items()
        if assignment.tier in RUNNABLE_TIERS
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
    phase: Literal["final", "repair"],
    **attempt: Any,
) -> BindingsPayload:
    """Journal payload for ``boundary.binding.recorded`` (bindings are data, no code).

    ``attempt`` carries a repair record's ``root_ac_index``, ``retry_attempt``
    and ``status``; the payload model refuses anything else.
    """
    return BindingsPayload.model_validate(
        {
            "phase": phase,
            "checks": [
                {
                    **assignment.to_dict(),
                    "declared": results[check_id].to_dict() if check_id in results else None,
                }
                for check_id, assignment in sorted(assignments.items())
            ],
            **attempt,
        }
    )


__all__ = [
    "BoundVerification",
    "DeclaredBindingResult",
    "assign_tiers",
    "bindings_payload",
    "snapshot_base",
    "validate_declared_binding",
    "verify_with_bindings",
]
