"""Check-package boundary: pre-artifact executable checks for a frozen Seed.

Public API:

- ``CheckPackage`` and its parts (``CheckSpec``, ``CheckRole``,
  ``AssertionLink``, ``PackageFile``, ``BaseFileRef``,
  ``UncoveredObligation``): a separately hashed package of executable checks
  linked to a Seed digest and criterion keys.
- ``seed_digest``, ``seed_criterion_keys``, ``validate_package_for_seed``,
  ``write_check_package``, ``load_check_package``.
- ``worker_criteria``, ``find_workspace_leaks``, ``find_text_leaks``: the
  worker receives criterion descriptions only.
- ``admit_check_package``: whole-package admission on isolated copies of the
  pinned base checkout.
- ``verify_candidate`` and ``select_incumbent``: a candidate replaces the
  incumbent only by passing the unchanged frozen package plus identity
  revalidation.
- ``BoundaryLedger`` and ``verify_boundary_order``: EventStore ordering guard
  (package digest persisted before any actor starts).
"""

from ouroboros.boundary.admission import (
    ADMISSION_TIMEOUT_SECONDS,
    AdmissionResult,
    CandidateVerdict,
    CandidateVerification,
    CheckExecution,
    CheckStatus,
    PackageVerdict,
    admit_check_package,
    verify_candidate,
    write_receipt,
)
from ouroboros.boundary.ledger import (
    BoundaryLeakError,
    BoundaryLedger,
    BoundaryOrderError,
    verify_boundary_order,
)
from ouroboros.boundary.package import (
    CHECK_PACKAGE_SCHEMA,
    AssertionLink,
    BaseFileRef,
    CheckPackage,
    CheckPackageError,
    CheckRole,
    CheckSpec,
    PackageFile,
    UncoveredObligation,
    WorkerCriterion,
    find_text_leaks,
    find_workspace_leaks,
    load_check_package,
    seed_criterion_keys,
    seed_digest,
    validate_package_for_seed,
    worker_criteria,
    write_check_package,
)
from ouroboros.boundary.selection import (
    ArtifactRef,
    SelectionDecision,
    SelectionError,
    SelectionReason,
    select_incumbent,
)
from ouroboros.boundary.tree import tree_digest, tree_manifest

__all__ = [
    "ADMISSION_TIMEOUT_SECONDS",
    "CHECK_PACKAGE_SCHEMA",
    "AdmissionResult",
    "ArtifactRef",
    "AssertionLink",
    "BaseFileRef",
    "BoundaryLeakError",
    "BoundaryLedger",
    "BoundaryOrderError",
    "CandidateVerdict",
    "CandidateVerification",
    "CheckExecution",
    "CheckPackage",
    "CheckPackageError",
    "CheckRole",
    "CheckSpec",
    "CheckStatus",
    "PackageFile",
    "PackageVerdict",
    "SelectionDecision",
    "SelectionError",
    "SelectionReason",
    "UncoveredObligation",
    "WorkerCriterion",
    "admit_check_package",
    "find_text_leaks",
    "find_workspace_leaks",
    "load_check_package",
    "seed_criterion_keys",
    "seed_digest",
    "select_incumbent",
    "tree_digest",
    "tree_manifest",
    "validate_package_for_seed",
    "verify_boundary_order",
    "verify_candidate",
    "worker_criteria",
    "write_check_package",
    "write_receipt",
]
