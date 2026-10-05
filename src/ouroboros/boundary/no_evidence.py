"""Why a run accepted criteria without evidence, for anonymous telemetry only.

A criterion the check package did not decide (``unverified`` or
``uncovered``) is decided by the legacy verifier (claim replay); when that
verifier has no evidence either (``ExistingOutcome.no_evidence``) the
criterion stays accepted (``acceptance.reconcile_acceptance``). This module
changes none of that. It reads the decision the authority already made
(``AuthorityOutcome``) and names, per such criterion, why the package did not
decide it (``package_reason``) and why the legacy verifier had no evidence
(``replay_reason``), from the product's own typed state:

- ``package_reason``: ``uncovered`` (no admitted check is linked),
  ``no_admitted_package`` (the run was decided with no admitted package), or
  the unverified reason the verdict carries (``no_binding``,
  ``no_binding_after_request``, ``no_binding_budget_exhausted``,
  ``script_check_advisory``, ``no_held_out_case``, ``no_reproduction_check``),
  matched by exact equality with the product's constants; anything else is
  ``unknown``;
- ``replay_reason``: ``ExistingOutcome.no_evidence_reason``
  (``acceptance.LegacyNoEvidenceReason``), or ``no_legacy_record`` when the
  criterion has no legacy record at all.

``telemetry.capture_acceptance_no_evidence`` folds both axes to its audited
vocabularies again before anything is sent (SSOT pairing: edit both together).
"""

from __future__ import annotations

from ouroboros import telemetry as usage_telemetry
from ouroboros.boundary.acceptance import (
    NO_HELD_OUT_CASE,
    NO_REPRODUCTION_CHECK,
    SCRIPT_CHECK_ADVISORY,
    CriterionDecision,
    PackageCriterionStatus,
)
from ouroboros.boundary.authority import (
    NO_BINDING,
    NO_BINDING_AFTER_REQUEST,
    NO_BINDING_BUDGET_EXHAUSTED,
    AuthorityOutcome,
)

UNCOVERED = "uncovered"
NO_ADMITTED_PACKAGE = "no_admitted_package"
NO_LEGACY_RECORD = "no_legacy_record"
UNKNOWN = "unknown"
_UNVERIFIED_REASONS = frozenset(
    {
        NO_BINDING,
        NO_BINDING_AFTER_REQUEST,
        NO_BINDING_BUDGET_EXHAUSTED,
        SCRIPT_CHECK_ADVISORY,
        NO_HELD_OUT_CASE,
        NO_REPRODUCTION_CHECK,
    }
)


def _package_reason(decision: CriterionDecision, outcome: AuthorityOutcome) -> str:
    if decision.package_status is PackageCriterionStatus.UNCOVERED:
        no_package = outcome.verdict is None or outcome.verdict.package_id is None
        return NO_ADMITTED_PACKAGE if outcome.error is None and no_package else UNCOVERED
    return decision.reason if decision.reason in _UNVERIFIED_REASONS else UNKNOWN


def no_evidence_pairs(outcome: AuthorityOutcome) -> list[tuple[str, str]]:
    """``(package_reason, replay_reason)`` of every criterion accepted without evidence.

    Such a criterion is accepted, the package did not decide it, and the
    legacy verifier had no evidence for it (or no record of it).
    """
    reconciliation = outcome.reconciliation
    if reconciliation is None:
        return []
    pairs: list[tuple[str, str]] = []
    for decision in reconciliation.decisions:
        if not decision.accepted or not decision.package_status.is_unverified:
            continue
        prior = outcome.legacy.get(decision.root_ac_index)
        if prior is not None and not prior.no_evidence:
            continue
        if prior is None:
            replay_reason = NO_LEGACY_RECORD
        elif prior.no_evidence_reason is None:
            replay_reason = UNKNOWN
        else:
            replay_reason = prior.no_evidence_reason.value
        pairs.append((_package_reason(decision, outcome), replay_reason))
    return pairs


def report_no_evidence(
    outcome: AuthorityOutcome,
    *,
    surface: str | None,
    check_package: str,
    check_package_status: str,
    runtime_backend: str | None,
) -> None:
    """Send one ``acceptance_no_evidence`` event when any criterion was accepted without
    evidence, and one ``acceptance_basis`` event counting what each accepted
    criterion rests on (``AcceptedBy``).

    Fire-and-forget: never raises, never changes a decision.
    """
    try:
        reconciliation = outcome.reconciliation
        if reconciliation is None:
            return
        usage_telemetry.capture_acceptance_no_evidence(
            no_evidence_pairs(outcome),
            criterion_count=len(reconciliation.decisions),
            verification_coverage=reconciliation.coverage.value,
            surface=surface,
            check_package=check_package,
            check_package_status=check_package_status,
            runtime_backend=runtime_backend,
        )
        usage_telemetry.capture_acceptance_basis(
            (
                decision.accepted_by.value if decision.accepted_by is not None else None
                for decision in reconciliation.decisions
                if decision.accepted
            ),
            criterion_count=len(reconciliation.decisions),
            surface=surface,
            check_package=check_package,
            runtime_backend=runtime_backend,
        )
    except Exception:  # noqa: BLE001 - telemetry must never affect the run
        pass


__all__ = ["no_evidence_pairs", "report_no_evidence"]
