"""Receipts built as data, for journal tests (nothing runs)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ouroboros.boundary.binding import CheckTier, tier_summary
from ouroboros.boundary.events import (
    LEGACY_RULE_SCHEMA,
    BindingsPayload,
    artifact_verdict_of,
    coverage_of,
)
from ouroboros.boundary.oracle import CaseResult, OracleResult
from ouroboros.boundary.package import CheckPackage, CheckRole, CheckSpec, seed_digest
from ouroboros.boundary.receipts import (
    AdmissionResult,
    CandidateVerdict,
    CandidateVerification,
    CheckExecution,
    CheckStatus,
    PackageVerdict,
)
from ouroboros.boundary.tree import tree_digest
from ouroboros.core.seed import Seed

PIN = "d" * 64
"""A stand-in interpreter digest (binary and real path) for receipts built as data."""


def expected_execution(check: CheckSpec) -> CheckExecution:
    """The base result admission records for a check that met its role (built as data)."""
    return CheckExecution(
        check_id=check.check_id,
        role=check.role,
        argv=check.argv,
        cwd=check.cwd,
        status=CheckStatus.EXPECTED,
        reason=(
            "reached_failing_assertion"
            if check.role is CheckRole.REPRODUCTION
            else "preservation_passed"
        ),
        return_code=1 if check.role is CheckRole.REPRODUCTION else 0,
        timed_out=False,
        duration_seconds=0.0,
        signature_seen=check.role is CheckRole.REPRODUCTION,
        stdout_sha256="0" * 64,
        stderr_sha256="0" * 64,
        output_tail="",
        protected_digest_before="0" * 64,
        protected_digest_after="0" * 64,
        mutated_paths=(),
        scratch_outputs=(),
        undeclared_outputs=(),
    )


def oracle_result(package: CheckPackage, check_id: str, passed: Mapping[str, bool]) -> OracleResult:
    """The result of ``check_id``'s oracle: each case passes as ``passed`` says (default no)."""
    spec = package.oracle_for(check_id)
    assert spec is not None
    return OracleResult(
        check_id=check_id,
        criterion_key=spec.criterion_key,
        binding_source="default",
        symbol=spec.default_binding.symbol,
        call_kind=spec.call_kind,
        resolve="ok",
        cases=tuple(
            CaseResult(
                case_id=case.case_id, held_out=case.held_out, passed=passed.get(case.case_id, False)
            )
            for case in spec.cases
        ),
    )


def _base_execution(package: CheckPackage, check: CheckSpec) -> CheckExecution:
    """What admission records for a check that met its role on the base.

    A reproduction oracle reproduced the bug: its base run failed every case,
    held-out cases included.
    """
    oracle = package.oracle_for(check.check_id)
    execution = expected_execution(check)
    if oracle is None:
        return execution.model_copy(update={"tier": CheckTier.S})
    return execution.model_copy(
        update={"tier": CheckTier.A, "oracle_result": oracle_result(package, check.check_id, {})}
    )


def candidate_execution(check: CheckSpec, *, met: bool) -> CheckExecution:
    """A script check's run on a candidate, as admission's classifier records it.

    Met: exit 0, ``passed``. Not met: exit 1; a reproduction check reached
    its failure signature (``reproduction_still_failing``), a preservation
    check failed (``preservation_failed``).
    """
    reproduction = check.role is CheckRole.REPRODUCTION
    return expected_execution(check).model_copy(
        update={
            "status": CheckStatus.EXPECTED if met else CheckStatus.VIOLATED,
            "reason": "passed"
            if met
            else ("reproduction_still_failing" if reproduction else "preservation_failed"),
            "return_code": 0 if met else 1,
            "signature_seen": not met and reproduction,
            "tier": CheckTier.S,
        }
    )


# What admission and verification write over their checks (``admission.py``):
# the mutation flag, the reasons and the verdict, and on the base the
# per-check exclusions and tiers. Receipts built as data go through these so
# they are the ones the product writes.
_PRECONDITION = "package_path_collision:probe/test_add.py"


def _summary(
    checks: tuple[CheckExecution, ...], before: str, after: str, preconditions: tuple[str, ...]
) -> tuple[bool, list[str], str]:
    mutation = ["source_checkout_mutated"] if before != after else []
    mutation += [f"protected_bytes_mutated:{c.check_id}" for c in checks if c.mutated_paths]
    violated = [c for c in checks if c.status is CheckStatus.VIOLATED]
    undecided = [c for c in checks if c.status is CheckStatus.INDETERMINATE]
    reasons = [
        *preconditions,
        *mutation,
        *(f"{c.reason}:{c.check_id}" for c in violated),
        *(f"{c.reason}:{c.check_id}" for c in undecided if c.reason != "protected_bytes_mutated"),
    ]
    if preconditions or mutation:
        verdict = "indeterminate"
    elif violated:
        verdict = "violated"
    elif undecided:
        verdict = "indeterminate"
    else:
        verdict = "met"
    return bool(mutation), reasons, verdict


def settled_verification(receipt: CandidateVerification) -> CandidateVerification:
    """``receipt`` with the flag, reasons and verdict ``verify_candidate`` gives its checks."""
    preconditions = () if receipt.checks else (_PRECONDITION,)
    mutated, reasons, verdict = _summary(
        receipt.checks,
        receipt.artifact_tree_digest,
        receipt.artifact_tree_digest_after,
        preconditions,
    )
    names = {"violated": CandidateVerdict.FAIL, "met": CandidateVerdict.PASS}
    return receipt.model_copy(
        update={
            "protected_bytes_mutated": mutated,
            "reasons": tuple(reasons),
            "verdict": names.get(verdict, CandidateVerdict.INDETERMINATE),
        }
    )


def settled_admission(package: CheckPackage, receipt: AdmissionResult) -> AdmissionResult:
    """``receipt`` as ``admit_check_package`` and ``per_check_admission`` write its checks."""
    from ouroboros.boundary.per_check import per_check_admission

    preconditions = () if receipt.checks else (_PRECONDITION,)
    mutated, reasons, verdict = _summary(
        receipt.checks, receipt.base_tree_digest, receipt.base_tree_digest_after, preconditions
    )
    tiers: dict[str, CheckTier] = {}
    for check in receipt.checks:
        oracle = package.oracle_for(check.check_id)
        resolve = check.oracle_result.resolve if check.oracle_result is not None else None
        tiers[check.check_id] = CheckTier.S if oracle is None else oracle.base_run_tier(resolve)
    names = {"violated": PackageVerdict.REJECTED, "met": PackageVerdict.ADMITTED}
    settled = receipt.model_copy(
        update={
            "protected_bytes_mutated": mutated,
            "reasons": tuple(reasons),
            "verdict": names.get(verdict, PackageVerdict.INDETERMINATE),
            "checks": tuple(
                c.model_copy(update={"tier": tiers[c.check_id]}) for c in receipt.checks
            ),
            "check_tiers": tiers or None,
            "excluded_checks": None,
        }
    )
    return per_check_admission(settled)


def admission_receipt(
    package: CheckPackage, checkout: Path, verdict: PackageVerdict = PackageVerdict.ADMITTED
) -> AdmissionResult:
    """An admission receipt for ``package`` on ``checkout``, built as data (nothing runs).

    Admitted: every check met its role on the base, with the interpreter pin.
    Rejected: the first check's reproduction passed on the base while the
    second timed out, so the per-check rule cannot admit the rest.
    """
    now = datetime.now(UTC)
    digest = tree_digest(checkout)
    admitted = verdict is PackageVerdict.ADMITTED
    checks = [_base_execution(package, check) for check in package.checks]
    if not admitted:
        checks[0] = checks[0].model_copy(
            update={
                "status": CheckStatus.VIOLATED,
                "reason": "reproduction_passed_on_base",
                "return_code": 0,
                "signature_seen": False,
            }
        )
        checks[1] = checks[1].model_copy(
            update={
                "status": CheckStatus.INDETERMINATE,
                "reason": "timeout",
                "timed_out": True,
                "return_code": None,
                "signature_seen": False,
            }
        )
    receipt = AdmissionResult(
        package_sha256=package.sha256,
        package_id=package.package_id if package.sealed else None,
        seed_digest=package.seed_digest,
        base_tree_digest=digest,
        base_tree_digest_after=digest,
        verdict=verdict,
        reasons=(),
        protected_bytes_mutated=False,
        timeout_seconds=120,
        checks=tuple(checks),
        started_at=now,
        completed_at=now,
        interpreter_sha256=PIN if admitted else None,
        interpreter_realpath_sha256=PIN if admitted else None,
    )
    return settled_admission(package, receipt)


def verification_receipt(
    package: CheckPackage,
    checkout: Path,
    checks: tuple[CheckExecution, ...] = (),
    *,
    before: str | None = None,
    after: str | None = None,
    selection: tuple[str, ...] | None = None,
) -> CandidateVerification:
    """A candidate verification of ``package`` on ``checkout``, built as data (nothing runs).

    The run selects every check of ``final_bindings(package)`` (``selection``
    narrows it for a re-run), handing each oracle its default binding; with
    no ``checks`` a precondition stopped it.
    """
    now = datetime.now(UTC)
    digest = tree_digest(checkout)
    selected = selection or tuple(check.check_id for check in package.checks)
    tiers = {
        key: CheckTier.A if package.oracle_for(key) is not None else CheckTier.S for key in selected
    }
    bindings = {}
    for key in selected:
        spec = package.oracle_for(key)
        if spec is not None:
            bindings[key] = spec.default_binding
    receipt = CandidateVerification(
        package_sha256=package.sha256,
        package_id=package.package_id if package.sealed else None,
        seed_digest=package.seed_digest,
        artifact_tree_digest=before or digest,
        artifact_tree_digest_after=after or before or digest,
        verdict=CandidateVerdict.INDETERMINATE,
        reasons=(),
        protected_bytes_mutated=False,
        timeout_seconds=120,
        checks=checks,
        started_at=now,
        completed_at=now,
        check_tiers=tiers,
        bindings=bindings or None,
    )
    return settled_verification(receipt)


def final_bindings(package: CheckPackage, phase: str = "final") -> BindingsPayload:
    """The bindings ``assign_tiers`` records for an admitted ``package`` (every check once).

    An oracle check admitted as tier ``A`` runs through its default binding;
    a script check runs as tier ``S``.
    """
    checks = []
    for check in package.checks:
        oracle = package.oracle_for(check.check_id)
        if oracle is None:
            key, tier, source, binding, reason = (
                check.assertions[0].criterion_key,
                "S",
                None,
                None,
                "script_check",
            )
        else:
            key, tier, source, binding, reason = (
                oracle.criterion_key,
                "A",
                "default",
                oracle.default_binding.to_dict(),
                "default_binding_resolves",
            )
        checks.append(
            {
                "criterion_key": key,
                "check_id": check.check_id,
                "tier": tier,
                "binding_source": source,
                "binding": binding,
                "status_hint": "run",
                "reason": reason,
                "declared": None,
            }
        )
    return BindingsPayload.model_validate({"phase": phase, "checks": checks})


def criterion(index: int, key: str, status: str, **update: Any) -> dict[str, Any]:
    """One criterion decision; by default the package's, attempted and accepted by the legacy."""
    accepted = status in ("pass", "unverified", "uncovered")
    return {
        "root_ac_index": index,
        "criterion_key": key,
        "package_status": status,
        "tier": "U" if status in ("unverified", "uncovered") else "A",
        "reason": status,
        "failed_heldout_only": False,
        "binding": None,
        "existing_outcome": "succeeded",
        "existing_failure_class": None,
        "existing_accepted": True,
        "accepted": accepted,
        "governed_by": "check_package",
        "declared_binding_pass": False,
        **update,
    }


def decision_data(criteria: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """A decision under the product's rule, its summary computed from ``criteria``."""
    statuses = [item["package_status"] for item in criteria]
    not_decided = [
        item for item in criteria if item["package_status"] in ("unverified", "uncovered")
    ]
    unverified = [item for item in not_decided if item["governed_by"] != "existing_verifier"]
    return {
        "schema_version": LEGACY_RULE_SCHEMA,
        "run_accepted": bool(criteria) and all(item["accepted"] for item in criteria),
        "existing_run_accepted": True,
        "artifact_verdict": artifact_verdict_of(statuses),
        "verified_pass_count": statuses.count("pass"),
        "unverified_count": len(unverified),
        "criterion_count": len(criteria),
        "tier_summary": tier_summary(CheckTier(item["tier"]) for item in criteria),
        "criteria": criteria,
        "legacy_decided_count": sum(
            1 for item in criteria if item["governed_by"] == "existing_verifier"
        ),
        "verification_coverage": coverage_of(
            len(criteria),
            len(not_decided),
            len([item for item in unverified if item["governed_by"] != "execution"]),
        ),
        **extra,
    }


def seed_for(package: CheckPackage) -> Seed:
    """The test Seed ``package`` was built for (the freeze gateway validates against it)."""
    from .conftest import make_seed
    from .test_package_identity import _seed

    for seed in (make_seed(), _seed()):
        if seed_digest(seed) == package.seed_digest:
            return seed
    raise AssertionError("no test Seed for this package")


def unresolved_admission(package: CheckPackage, checkout: Path) -> AdmissionResult:
    """Admission where the oracle's default target was missing on the base: its tier is ``U``."""
    admission = admission_receipt(package, checkout)
    checks = tuple(
        check.model_copy(
            update={"oracle_result": check.oracle_result.model_copy(update={"resolve": "missing"})}
        )
        if check.oracle_result is not None
        else check
        for check in admission.checks
    )
    return settled_admission(package, admission.model_copy(update={"checks": checks}))
