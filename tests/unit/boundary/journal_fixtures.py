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
from ouroboros.boundary.package import CheckPackage, CheckRole, CheckSpec
from ouroboros.boundary.receipts import (
    AdmissionResult,
    CandidateVerdict,
    CandidateVerification,
    CheckExecution,
    CheckStatus,
    PackageVerdict,
)
from ouroboros.boundary.tree import tree_digest

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
    execution = expected_execution(check)
    if package.oracle_for(check.check_id) is None:
        return execution
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
        }
    )


def admission_receipt(
    package: CheckPackage, checkout: Path, verdict: PackageVerdict = PackageVerdict.ADMITTED
) -> AdmissionResult:
    """An admission receipt for ``package`` on ``checkout``, built as data (nothing runs).

    An admitted receipt is one admission can write: one expected result and a
    tier per check, and the interpreter pin.
    """
    now = datetime.now(UTC)
    digest = tree_digest(checkout)
    admitted = verdict is PackageVerdict.ADMITTED
    return AdmissionResult(
        package_sha256=package.sha256,
        package_id=package.package_id if package.sealed else None,
        seed_digest=package.seed_digest,
        base_tree_digest=digest,
        base_tree_digest_after=digest,
        verdict=verdict,
        reasons=(),
        protected_bytes_mutated=False,
        timeout_seconds=120,
        checks=tuple(_base_execution(package, check) for check in package.checks)
        if admitted
        else (),
        started_at=now,
        completed_at=now,
        interpreter_sha256=PIN if admitted else None,
        interpreter_realpath_sha256=PIN if admitted else None,
        check_tiers={
            c.check_id: CheckTier.A if package.oracle_for(c.check_id) else CheckTier.S
            for c in package.checks
        }
        if admitted
        else None,
    )


def verification_receipt(
    package: CheckPackage, checkout: Path, verdict: CandidateVerdict = CandidateVerdict.FAIL
) -> CandidateVerification:
    """A candidate verification of ``package`` on ``checkout``, built as data (nothing runs)."""
    now = datetime.now(UTC)
    digest = tree_digest(checkout)
    return CandidateVerification(
        package_sha256=package.sha256,
        package_id=package.package_id if package.sealed else None,
        seed_digest=package.seed_digest,
        artifact_tree_digest=digest,
        artifact_tree_digest_after=digest,
        verdict=verdict,
        reasons=(),
        protected_bytes_mutated=False,
        timeout_seconds=120,
        checks=(),
        started_at=now,
        completed_at=now,
    )


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
