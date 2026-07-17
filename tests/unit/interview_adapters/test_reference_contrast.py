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
        "Which role should this reference play in the project?\n"
        "Reference `Linear` (https://linear.app): Fast issue triage.\n"
        "1. Copy selected traits from its surface look and language, workflow or "
        "structure, or interaction qualities.\n"
        "2. Use it only to compare the desired outcome and assumptions we should "
        "reject, without copying it directly.\n"
        "3. Avoid its approach and state what should differ.\n"
        "Recommended: 2, references should inform decisions without silently "
        "becoming requirements.\n"
        "Reply with the number, or write your own answer."
    )
    for required_phrase in (
        "surface look",
        "workflow or structure",
        "interaction qualities",
        "desired outcome",
        "assumptions we should reject",
    ):
        assert required_phrase in question


def test_reference_contrast_uses_chinese_presentation_when_requested() -> None:
    question = build_reference_contrast_question(_cue(), locale="zh")

    assert question.startswith("这个参考资料在项目中应该发挥什么作用？")
    assert "参考资料 `Linear`" in question
    assert "1. 选择性参考它的视觉表达、工作流程或交互特征。" in question
    assert "建议: 2" in question
    assert question.endswith("可回复编号，或直接写出你的答案。")
    assert "Which role" not in question
    assert "Reply with" not in question


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
