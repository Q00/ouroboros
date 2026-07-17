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
from ouroboros.interview_adapters.presentation import (
    InterviewQuestionChoice,
    InterviewQuestionPresentation,
    InterviewQuestionRecommendation,
    QuestionChoiceProvenance,
    render_question_presentation,
)


def build_reference_contrast_presentation(
    cue: ReferenceCue,
    *,
    locale: str = "en",
) -> InterviewQuestionPresentation:
    """Build the deterministic contrast turn for a reference cue."""
    language = locale.split("-", 1)[0].casefold()
    if language == "ko":
        context = f"참고 자료 `{cue.label}`"
    elif language == "zh":
        context = f"参考资料 `{cue.label}`"
    else:
        context = f"Reference `{cue.label}`"
    if cue.url:
        context = f"{context} ({cue.url})"
    if cue.excerpt:
        context = f"{context}: {cue.excerpt}"
    if language == "ko":
        question = "이 참고 자료를 프로젝트에서 어떻게 활용해야 하나요?"
        labels = (
            "표현 방식, 흐름, 상호작용 특성 중 필요한 부분을 선택해 참고합니다.",
            "직접 복사하지 않고 원하는 결과와 피해야 할 가정만 비교합니다.",
            "해당 접근을 사용하지 않고 달라야 할 점을 정합니다.",
        )
        reason = "참고 자료가 사용자 확인 없이 요구사항이 되는 것을 막을 수 있습니다"
        recommendation_label = "권장"
        free_text_prompt = "번호로 답하거나 직접 답변을 작성하세요."
        normalized_locale = "ko"
    elif language == "zh":
        question = "这个参考资料在项目中应该发挥什么作用？"
        labels = (
            "选择性参考它的视觉表达、工作流程或交互特征。",
            "只比较目标结果和应避免的假设，不直接复制。",
            "不采用它的方法，并明确项目应有的差异。",
        )
        reason = "参考资料应辅助决策，而不能未经用户确认就变成需求"
        recommendation_label = "建议"
        free_text_prompt = "可回复编号，或直接写出你的答案。"
        normalized_locale = "zh"
    else:
        question = "Which role should this reference play in the project?"
        labels = (
            "Copy selected traits from its surface look and language, workflow or structure, or interaction qualities.",
            "Use it only to compare the desired outcome and assumptions we should reject, without copying it directly.",
            "Avoid its approach and state what should differ.",
        )
        reason = "references should inform decisions without silently becoming requirements"
        recommendation_label = "Recommended"
        free_text_prompt = "Reply with the number, or write your own answer."
        normalized_locale = "en"

    meaning_keys = ("selective_copy", "compare_only", "explicitly_avoid")
    return InterviewQuestionPresentation(
        decision_id="reference_role",
        target_dimension="context_clarity",
        locale=normalized_locale,
        question=question,
        context=(context,),
        choices=tuple(
            InterviewQuestionChoice(
                choice_id=index,
                meaning_key=meaning_key,
                label=label,
                provenance=QuestionChoiceProvenance.REFERENCE_DERIVED,
            )
            for index, (meaning_key, label) in enumerate(
                zip(meaning_keys, labels, strict=True),
                start=1,
            )
        ),
        recommendation=InterviewQuestionRecommendation(
            choice_id=2,
            label=recommendation_label,
            reason=reason,
        ),
        free_text_prompt=free_text_prompt,
    )


def build_reference_contrast_question(cue: ReferenceCue, *, locale: str = "en") -> str:
    """Render the deterministic contrast turn for string API compatibility."""
    return render_question_presentation(build_reference_contrast_presentation(cue, locale=locale))


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
    "build_reference_contrast_presentation",
    "build_reference_contrast_question",
    "candidates_from_contrast_answer",
    "next_unresolved_reference",
]
