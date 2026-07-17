"""Deterministic reference contrast helpers."""

from __future__ import annotations

from ouroboros.core.requirement_candidate import (
    CandidateContentSource,
    CandidateResolution,
    ConfirmationAuthority,
    RequirementCandidate,
    RequirementEvidence,
    RequirementEvidenceKind,
    RequirementSection,
)
from ouroboros.interview_adapters.models import (
    ReferenceContrastResolution,
    ReferenceCue,
    ReferenceResolutionStatus,
)


def build_reference_contrast_question(cue: ReferenceCue) -> str:
    """Build the v1 deterministic contrast question for a reference cue."""

    context = f"Reference `{cue.label}`"
    if cue.url:
        context = f"{context} ({cue.url})"
    if cue.excerpt:
        context = f"{context}: {cue.excerpt}"
    return (
        f"{context}\n"
        "What should this project copy or avoid from that reference across: "
        "surface look and language, workflow or structure, interaction qualities, "
        "desired outcome, and assumptions we should reject?"
    )


def next_unresolved_reference(
    cues: tuple[ReferenceCue, ...],
    resolutions: tuple[ReferenceContrastResolution, ...] = (),
) -> ReferenceCue | None:
    """Return the first cue that has not been asked or resolved."""

    by_id = {resolution.reference_id: resolution for resolution in resolutions}
    for cue in cues:
        resolution = by_id.get(cue.reference_id)
        if resolution is None or resolution.status is ReferenceResolutionStatus.UNRESOLVED:
            return cue
    return None


def candidates_from_contrast_answer(
    *,
    cue: ReferenceCue,
    answer: str,
    candidate_id_prefix: str = "reference",
) -> tuple[RequirementEvidence, RequirementCandidate]:
    """Create a reference-derived candidate that still requires confirmation."""

    answer = answer.strip()
    if not answer:
        raise ValueError("contrast answer must not be blank")
    evidence_id = f"reference-contrast:{cue.reference_id}"
    evidence = RequirementEvidence(
        evidence_id=evidence_id,
        kind=RequirementEvidenceKind.REFERENCE_CONTRAST,
        text=answer,
        reference_id=cue.reference_id,
    )
    candidate = RequirementCandidate(
        candidate_id=f"{candidate_id_prefix}:{cue.reference_id}:contrast",
        section=RequirementSection.CONTEXT,
        text=answer,
        content_source=CandidateContentSource.REFERENCE_DERIVED,
        resolution=CandidateResolution.NEEDS_CONFIRMATION,
        confirmation_authority=ConfirmationAuthority.NONE,
        reference_ids=(cue.reference_id,),
        evidence_ids=(evidence_id,),
        required=False,
    )
    return evidence, candidate


__all__ = [
    "build_reference_contrast_question",
    "candidates_from_contrast_answer",
    "next_unresolved_reference",
]
