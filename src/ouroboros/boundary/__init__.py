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

Oracle and binding (``boundary/oracle.py``, ``boundary/binding.py``,
``boundary/binding_flow.py``): the package freezes per-criterion oracles
(cases, held-out cases, default binding, failure signature) under its hash
before dispatch; after the worker stops a late binding is validated
(``validate_declared_binding``: grammar, static rules, one base run), every
check gets a tier (``assign_tiers``: ``A``, ``A_prime``, ``U``; ``C`` at
admission), and the candidate runs through the recorded bindings
(``verify_with_bindings``); ``criterion_verdicts`` and ``artifact_verdict``
decide.
"""

from ouroboros.boundary.acceptance import (
    ArtifactVerdict,
    CriterionVerdict,
    PackageCriterionStatus,
    artifact_verdict,
    criterion_verdicts,
    reconcile_acceptance,
)
from ouroboros.boundary.admission import (
    ADMISSION_TIMEOUT_SECONDS,
    BINDING_ADMISSION_TIMEOUT_SECONDS,
    AdmissionResult,
    BindingAdmission,
    CandidateVerdict,
    CandidateVerification,
    CheckExecution,
    CheckStatus,
    PackageVerdict,
    admit_binding,
    admit_check_package,
    verify_candidate,
    write_receipt,
)
from ouroboros.boundary.admission_rules import script_rule_violations, unsafe_checks
from ouroboros.boundary.binding import (
    BINDING_GRAMMAR,
    Binding,
    BindingError,
    BindingValidation,
    CallKind,
    CheckTier,
    TierAssignment,
    assign_tier,
    entry_points_request,
    locate_symbol,
    parse_binding,
    tier_summary,
    validate_declared_binding_static,
)
from ouroboros.boundary.binding_flow import (
    BoundVerification,
    DeclaredBindingResult,
    admission_tiers,
    assign_tiers,
    bindings_payload,
    snapshot_base,
    validate_declared_binding,
    verify_with_bindings,
)
from ouroboros.boundary.ledger import (
    BoundaryLeakError,
    BoundaryLedger,
    BoundaryOrderError,
    verify_boundary_order,
)
from ouroboros.boundary.oracle import (
    OracleCase,
    OracleExpectation,
    OracleSpec,
    case_in_text,
    failed_heldout_only,
    mark_held_out,
    repair_lines,
    worker_visible_seed_text,
)
from ouroboros.boundary.oracle_build import assemble_package, build_oracle_spec
from ouroboros.boundary.package import (
    CHECK_PACKAGE_SCHEMA,
    ORACLE_PACKAGE_SCHEMA,
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
    "BINDING_ADMISSION_TIMEOUT_SECONDS",
    "BINDING_GRAMMAR",
    "CHECK_PACKAGE_SCHEMA",
    "ORACLE_PACKAGE_SCHEMA",
    "AdmissionResult",
    "ArtifactRef",
    "ArtifactVerdict",
    "AssertionLink",
    "BaseFileRef",
    "Binding",
    "BindingAdmission",
    "BindingError",
    "BindingValidation",
    "BoundVerification",
    "BoundaryLeakError",
    "BoundaryLedger",
    "BoundaryOrderError",
    "CallKind",
    "CandidateVerdict",
    "CandidateVerification",
    "CheckExecution",
    "CheckPackage",
    "CheckPackageError",
    "CheckRole",
    "CheckSpec",
    "CheckStatus",
    "CheckTier",
    "CriterionVerdict",
    "DeclaredBindingResult",
    "OracleCase",
    "OracleExpectation",
    "OracleSpec",
    "PackageCriterionStatus",
    "PackageFile",
    "PackageVerdict",
    "SelectionDecision",
    "SelectionError",
    "SelectionReason",
    "TierAssignment",
    "UncoveredObligation",
    "WorkerCriterion",
    "admission_tiers",
    "admit_binding",
    "admit_check_package",
    "artifact_verdict",
    "assemble_package",
    "assign_tier",
    "assign_tiers",
    "bindings_payload",
    "build_oracle_spec",
    "case_in_text",
    "criterion_verdicts",
    "entry_points_request",
    "failed_heldout_only",
    "find_text_leaks",
    "find_workspace_leaks",
    "load_check_package",
    "locate_symbol",
    "mark_held_out",
    "parse_binding",
    "reconcile_acceptance",
    "repair_lines",
    "script_rule_violations",
    "seed_criterion_keys",
    "seed_digest",
    "select_incumbent",
    "snapshot_base",
    "tier_summary",
    "tree_digest",
    "tree_manifest",
    "unsafe_checks",
    "validate_declared_binding",
    "validate_declared_binding_static",
    "validate_package_for_seed",
    "verify_boundary_order",
    "verify_candidate",
    "verify_with_bindings",
    "worker_criteria",
    "worker_visible_seed_text",
    "write_check_package",
    "write_receipt",
]
