from __future__ import annotations

from ouroboros.core.requirement_candidate import (
    CandidateContentSource,
    CandidateResolution,
    RequirementEvidenceKind,
)
from ouroboros.interview_adapters.models import (
    ReferenceContrastResolution,
    ReferenceCue,
    ReferenceResolutionStatus,
)
from ouroboros.interview_adapters.reference_contrast import (
    build_reference_contrast_question,
    candidates_from_contrast_answer,
    next_unresolved_reference,
)


def _cue() -> ReferenceCue:
    return ReferenceCue(
        reference_id="linear",
        label="Linear",
        origin="url",
        url="https://linear.app",
        excerpt="Fast issue triage.",
    )


def test_initial_reference_cue_is_absent_from_first_question_prompt() -> None:
    cue = _cue()

    first_question = "What outcome should the project achieve?"

    assert cue.label not in first_question
    assert next_unresolved_reference((cue,), ()) == cue


def test_after_base_answer_unresolved_cue_emits_deterministic_contrast_question() -> None:
    question = build_reference_contrast_question(_cue())

    assert question == (
        "Reference `Linear` (https://linear.app): Fast issue triage.\n"
        "What should this project copy or avoid from that reference across: "
        "surface look and language, workflow or structure, interaction qualities, "
        "desired outcome, and assumptions we should reject?"
    )
    for required_phrase in (
        "surface look",
        "workflow or structure",
        "interaction qualities",
        "desired outcome",
        "assumptions we should reject",
    ):
        assert required_phrase in question


def test_contrast_answer_creates_reference_derived_candidate_requiring_confirmation() -> None:
    evidence, candidate = candidates_from_contrast_answer(
        cue=_cue(),
        answer="Copy the fast triage workflow, but reject queue navigation assumptions.",
    )

    assert evidence.kind is RequirementEvidenceKind.REFERENCE_CONTRAST
    assert candidate.content_source is CandidateContentSource.REFERENCE_DERIVED
    assert candidate.resolution is CandidateResolution.NEEDS_CONFIRMATION
    assert candidate.reference_ids == ("linear",)


def test_repeated_resume_does_not_ask_resolved_reference() -> None:
    cue = _cue()
    resolution = ReferenceContrastResolution(
        reference_id=cue.reference_id,
        status=ReferenceResolutionStatus.RESOLVED,
        asked_question=build_reference_contrast_question(cue),
        answer="Use speed only.",
    )

    assert next_unresolved_reference((cue,), (resolution,)) is None
