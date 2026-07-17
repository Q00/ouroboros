"""Bridge promoted requirement candidates into the auto Seed draft ledger.

This module is intentionally narrow: it consumes the shared pre-Seed
``RequirementDistillation`` read model, applies the shared deterministic
promotion policy, and writes only promoted candidates into auto's mutable
``SeedDraftLedger``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from ouroboros.auto.ledger import (
    DecisionProvenance,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.core.requirement_candidate import (
    CandidateContentSource,
    CandidateResolution,
    ConfirmationAuthority,
    PromotionDecision,
    PromotionDisposition,
    RequirementCandidate,
    RequirementDistillation,
    RequirementSection,
    evaluate_promotion,
)

REFERENCE_CONFIRMATION_REQUIRED = "reference_confirmation_required"


@dataclass(frozen=True, slots=True)
class ReferenceCandidateBlocker:
    """A candidate-level blocker surfaced by the auto bridge."""

    candidate_id: str
    code: str
    reason: str
    reference_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OmittedReferenceCandidate:
    """Metadata for a candidate that remained outside the ledger."""

    candidate_id: str
    reason: str
    content_source: str
    resolution: str
    required: bool
    reference_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReferenceCandidateBridgeResult:
    """Result of applying a distillation to the auto ledger."""

    applied_candidate_ids: tuple[str, ...]
    omitted_candidate_ids: tuple[str, ...]
    blockers: tuple[ReferenceCandidateBlocker, ...]
    omitted_candidates: tuple[OmittedReferenceCandidate, ...]

    @property
    def is_ready(self) -> bool:
        """Return whether the bridge found no blocking candidate."""

        return not self.blockers


def apply_requirement_distillation_to_ledger(
    distillation: RequirementDistillation,
    ledger: SeedDraftLedger,
) -> ReferenceCandidateBridgeResult:
    """Apply promoted candidates from ``distillation`` to ``ledger``.

    Non-promoted candidates are observable in the returned metadata and are not
    represented as ledger entries. In particular, unconfirmed reference-derived
    candidates never become ``REPO_FACT`` or ``EXISTING_CONVENTION`` entries.
    """

    promotion = evaluate_promotion(distillation)
    applied: list[str] = []
    omitted: list[OmittedReferenceCandidate] = []
    blockers: list[ReferenceCandidateBlocker] = []

    for decision in promotion.decisions:
        candidate = decision.candidate
        if decision.disposition is PromotionDisposition.PROMOTE:
            section_name, entry = _ledger_entry_for_promoted_candidate(candidate)
            ledger.add_entry(section_name, entry)
            applied.append(candidate.candidate_id)
            continue

        omitted.append(_omitted_candidate(candidate, decision.reason))
        if decision.disposition is PromotionDisposition.BLOCK:
            blockers.append(_blocker_for_decision(decision))

    return ReferenceCandidateBridgeResult(
        applied_candidate_ids=tuple(applied),
        omitted_candidate_ids=tuple(item.candidate_id for item in omitted),
        blockers=tuple(blockers),
        omitted_candidates=tuple(omitted),
    )


def _ledger_entry_for_promoted_candidate(
    candidate: RequirementCandidate,
) -> tuple[str, LedgerEntry]:
    section_name = _ledger_section(candidate.section)
    source = _ledger_source(candidate)
    key = f"{section_name}.candidate.{_slug(candidate.candidate_id)}"
    evidence = [f"requirement_candidate:{candidate.candidate_id}"]
    evidence.extend(f"requirement_evidence:{item}" for item in candidate.evidence_ids)
    evidence.extend(f"reference:{item}" for item in candidate.reference_ids)

    return (
        section_name,
        LedgerEntry(
            key=key,
            value=candidate.text,
            source=source,
            confidence=_confidence(candidate),
            status=LedgerStatus.CONFIRMED,
            reversible=True,
            rationale=_rationale(candidate),
            evidence=evidence,
            provenance=_ledger_provenance(candidate),
        ),
    )


def _ledger_section(section: RequirementSection) -> str:
    return {
        RequirementSection.GOAL: "goal",
        RequirementSection.CONSTRAINT: "constraints",
        RequirementSection.EXISTING_CONSTRAINT: "constraints",
        RequirementSection.ACCEPTANCE_CRITERION: "acceptance_criteria",
        RequirementSection.ONTOLOGY: "constraints",
        RequirementSection.EVALUATION_PRINCIPLE: "verification_plan",
        RequirementSection.EXIT_CONDITION: "verification_plan",
        RequirementSection.NON_GOAL: "non_goals",
        RequirementSection.CONTEXT: "runtime_context",
    }[section]


def _ledger_source(candidate: RequirementCandidate) -> LedgerSource:
    if candidate.section is RequirementSection.NON_GOAL:
        return LedgerSource.NON_GOAL
    if candidate.content_source is CandidateContentSource.REPO_OBSERVED:
        return (
            LedgerSource.EXISTING_CONVENTION
            if candidate.section is RequirementSection.EXISTING_CONSTRAINT
            else LedgerSource.REPO_FACT
        )
    if candidate.content_source is CandidateContentSource.MODEL_INFERRED:
        return LedgerSource.INFERENCE
    if candidate.content_source is CandidateContentSource.REFERENCE_DERIVED:
        return LedgerSource.USER_PREFERENCE
    return LedgerSource.USER_GOAL


def _ledger_provenance(candidate: RequirementCandidate) -> DecisionProvenance:
    if candidate.confirmation_authority is ConfirmationAuthority.USER:
        return DecisionProvenance.USER_CONFIRMED
    if candidate.confirmation_authority is ConfirmationAuthority.REPO_EVIDENCE:
        return DecisionProvenance.USER_CONFIRMED
    return DecisionProvenance.MODEL_INFERRED


def _confidence(candidate: RequirementCandidate) -> float:
    if candidate.confirmation_authority is ConfirmationAuthority.USER:
        return 0.94
    if candidate.confirmation_authority is ConfirmationAuthority.REPO_EVIDENCE:
        return 0.9
    return 0.72


def _rationale(candidate: RequirementCandidate) -> str:
    return (
        "Promoted from requirement distillation candidate "
        f"{candidate.candidate_id}; source={candidate.content_source.value}; "
        f"authority={candidate.confirmation_authority.value}."
    )


def _omitted_candidate(candidate: RequirementCandidate, reason: str) -> OmittedReferenceCandidate:
    return OmittedReferenceCandidate(
        candidate_id=candidate.candidate_id,
        reason=reason,
        content_source=candidate.content_source.value,
        resolution=candidate.resolution.value,
        required=candidate.required,
        reference_ids=candidate.reference_ids,
    )


def _blocker_for_decision(decision: PromotionDecision) -> ReferenceCandidateBlocker:
    candidate = decision.candidate
    code = decision.reason
    if _is_unconfirmed_reference_contract(candidate):
        code = REFERENCE_CONFIRMATION_REQUIRED
    return ReferenceCandidateBlocker(
        candidate_id=candidate.candidate_id,
        code=code,
        reason=decision.reason,
        reference_ids=candidate.reference_ids,
    )


def _is_unconfirmed_reference_contract(candidate: RequirementCandidate) -> bool:
    return (
        candidate.required
        and candidate.content_source is CandidateContentSource.REFERENCE_DERIVED
        and candidate.resolution is not CandidateResolution.CONFIRMED
    )


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    return slug or "candidate"


__all__ = [
    "REFERENCE_CONFIRMATION_REQUIRED",
    "OmittedReferenceCandidate",
    "ReferenceCandidateBlocker",
    "ReferenceCandidateBridgeResult",
    "apply_requirement_distillation_to_ledger",
]
