"""Incumbent retention: a candidate replaces the incumbent only on proof.

The selector is a pure decision over recorded receipts. A candidate replaces
the incumbent only when all of the following hold:

1. the frozen package was admitted, and the admission receipt names that
   package's digest;
2. a candidate verification exists and ran that same, unchanged package;
3. identity revalidation succeeds: the verified tree digest equals the
   candidate's declared digest, the candidate tree was not changed by the
   verification run, the candidate, incumbent, and package share one Seed
   digest, and (when a checkout path is given) the candidate tree still hashes
   to the declared digest at selection time;
4. the verification verdict is ``pass``.

Otherwise the incumbent is kept and the decision records why. The selector
reads only its arguments (and, optionally, the candidate checkout it is told
to rehash); it never reads reference patches, private tests, or grader output.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ouroboros.boundary.admission import (
    AdmissionResult,
    CandidateVerdict,
    CandidateVerification,
    PackageVerdict,
)
from ouroboros.boundary.package import CheckPackage
from ouroboros.boundary.tree import DEFAULT_UNPROTECTED_NAMES, tree_digest


class SelectionError(ValueError):
    """The selector was called with an incumbent from a different contract."""


class ArtifactRef(BaseModel, frozen=True):
    """Identity of one candidate or incumbent artifact."""

    artifact_id: str
    tree_digest: str
    seed_digest: str
    patch_sha256: str | None = None


class SelectionReason(StrEnum):
    """Why the selector kept or replaced the incumbent."""

    CANDIDATE_PASSED = "candidate_passed_frozen_package"
    NO_CANDIDATE = "no_candidate"
    PACKAGE_NOT_ADMITTED = "package_not_admitted"
    ADMISSION_PACKAGE_MISMATCH = "admission_package_mismatch"
    VERIFICATION_MISSING = "verification_missing"
    VERIFICATION_PACKAGE_MISMATCH = "verification_package_mismatch"
    CANDIDATE_SEED_MISMATCH = "candidate_seed_mismatch"
    CANDIDATE_IDENTITY_MISMATCH = "candidate_identity_mismatch"
    CANDIDATE_FAILED = "candidate_failed"
    CANDIDATE_INDETERMINATE = "candidate_indeterminate"


class SelectionDecision(BaseModel, frozen=True):
    """Recorded selection: what was delivered, why, and under which digests."""

    schema_version: Literal["ouroboros.selection.v1"] = "ouroboros.selection.v1"
    selected: ArtifactRef
    replaced: bool
    reason: SelectionReason
    incumbent: ArtifactRef
    candidate: ArtifactRef | None
    package_sha256: str
    admission_verdict: PackageVerdict
    verification_verdict: CandidateVerdict | None
    verified_tree_digest: str | None


def select_incumbent(
    *,
    incumbent: ArtifactRef,
    candidate: ArtifactRef | None,
    package: CheckPackage,
    admission: AdmissionResult,
    verification: CandidateVerification | None,
    candidate_checkout: Path | None = None,
    unprotected_names: frozenset[str] = DEFAULT_UNPROTECTED_NAMES,
) -> SelectionDecision:
    """Keep the incumbent unless the candidate passes the unchanged package."""
    if incumbent.seed_digest != package.seed_digest:
        raise SelectionError("incumbent does not belong to the package's Seed")

    def decide(reason: SelectionReason, *, replace: bool = False) -> SelectionDecision:
        return SelectionDecision(
            selected=candidate if replace and candidate is not None else incumbent,
            replaced=replace,
            reason=reason,
            incumbent=incumbent,
            candidate=candidate,
            package_sha256=package.sha256,
            admission_verdict=admission.verdict,
            verification_verdict=verification.verdict if verification else None,
            verified_tree_digest=verification.artifact_tree_digest if verification else None,
        )

    if admission.package_sha256 != package.sha256:
        return decide(SelectionReason.ADMISSION_PACKAGE_MISMATCH)
    if admission.verdict is not PackageVerdict.ADMITTED:
        return decide(SelectionReason.PACKAGE_NOT_ADMITTED)
    if candidate is None:
        return decide(SelectionReason.NO_CANDIDATE)
    if verification is None:
        return decide(SelectionReason.VERIFICATION_MISSING)
    if verification.package_sha256 != package.sha256:
        return decide(SelectionReason.VERIFICATION_PACKAGE_MISMATCH)
    if candidate.seed_digest != package.seed_digest:
        return decide(SelectionReason.CANDIDATE_SEED_MISMATCH)
    identity_ok = (
        verification.artifact_tree_digest == candidate.tree_digest
        and verification.artifact_tree_digest_after == candidate.tree_digest
    )
    if identity_ok and candidate_checkout is not None:
        current = tree_digest(candidate_checkout, unprotected_names=unprotected_names)
        identity_ok = current == candidate.tree_digest
    if not identity_ok:
        return decide(SelectionReason.CANDIDATE_IDENTITY_MISMATCH)
    if verification.verdict is CandidateVerdict.PASS:
        return decide(SelectionReason.CANDIDATE_PASSED, replace=True)
    if verification.verdict is CandidateVerdict.FAIL:
        return decide(SelectionReason.CANDIDATE_FAILED)
    return decide(SelectionReason.CANDIDATE_INDETERMINATE)
