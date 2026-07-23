"""Build and apply the derived requirement projection for interview Seeds."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ouroboros.bigbang.interview import INITIAL_CONTEXT_SUMMARY_QUESTION, InterviewState
from ouroboros.core.requirement_candidate import (
    CandidateContentSource,
    CandidateResolution,
    ConfirmationAuthority,
    PromotionResult,
    RequirementCandidate,
    RequirementDistillation,
    RequirementEvidence,
    RequirementEvidenceKind,
    RequirementSection,
    evaluate_promotion,
)
from ouroboros.core.seed import (
    BrownfieldContext,
    ContextReference,
    EvaluationPrinciple,
    ExitCondition,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.interview_adapters import (
    ReferenceResolutionStatus,
    candidates_from_contrast_answer,
)

_EXPLICIT_REQUIREMENT_RE = re.compile(
    r"(?:"
    r"\b(?:must|need(?:s|ed)? to|required?|requirement|acceptance criteri(?:on|a)|"
    r"confirm(?:ed|ing)?|shall)\b"
    r"|(?:확인|확정)(?:된|한)?\s*(?:요구\s*사항|조건)"
    r"|요구\s*사항|필수|반드시|해야\s*(?:한다|합니다|함)|되어야\s*(?:한다|합니다|함)"
    r"|確認済み|確定(?:した|済み)?|要件|必須|必要(?:です|がある)|"
    r"なければならない|べき(?:です|だ)?"
    r")",
    re.IGNORECASE,
)
_CONSTRAINT_RE = re.compile(
    r"\b(?:constraint|must not|cannot|can't|no external|only|at most|at least)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AppliedRequirementDistillation:
    """Requirements after the deterministic reference-aware promotion gate."""

    requirements: dict[str, Any]
    distillation: RequirementDistillation
    promotion: PromotionResult


def is_reference_aware_distillation(distillation: RequirementDistillation) -> bool:
    """Return whether reference evidence activates the deterministic Seed path."""
    return any(
        item.kind
        in {
            RequirementEvidenceKind.REFERENCE_CUE,
            RequirementEvidenceKind.REFERENCE_CONTRAST,
        }
        for item in distillation.evidence
    ) or any(
        candidate.content_source is CandidateContentSource.REFERENCE_DERIVED
        for candidate in distillation.candidates
    )


def build_requirement_distillation(state: InterviewState) -> RequirementDistillation:
    """Derive a conservative candidate projection from canonical interview inputs."""
    fingerprint = state.requirement_input_fingerprint()
    cached = state.requirement_distillation
    if cached is not None and cached.is_current(
        input_revision=state.requirement_input_revision,
        input_fingerprint=fingerprint,
    ):
        return cached

    evidence: list[RequirementEvidence] = []
    candidates: list[RequirementCandidate] = []

    if state.initial_context.strip():
        evidence_id = "initial-context"
        evidence.append(
            RequirementEvidence(
                evidence_id=evidence_id,
                kind=RequirementEvidenceKind.USER_STATEMENT,
                text=state.initial_context.strip(),
            )
        )
        candidates.append(
            RequirementCandidate(
                candidate_id="initial-goal",
                section=RequirementSection.GOAL,
                text=state.initial_context.strip(),
                content_source=CandidateContentSource.USER_STATED,
                resolution=CandidateResolution.CONFIRMED,
                confirmation_authority=ConfirmationAuthority.USER,
                evidence_ids=(evidence_id,),
                required=True,
            )
        )

    reference_by_question = {
        resolution.asked_question: cue
        for resolution in state.reference_resolutions
        for cue in state.reference_cues
        if (
            resolution.status is ReferenceResolutionStatus.RESOLVED
            and resolution.asked_question
            and resolution.answer
            and resolution.reference_id == cue.reference_id
        )
    }
    resolved_reference_ids: set[str] = set()

    for round_data in state.rounds:
        answer = (round_data.user_response or "").strip()
        if not answer or round_data.question == INITIAL_CONTEXT_SUMMARY_QUESTION:
            continue
        reference_cue = reference_by_question.get(round_data.question)
        if reference_cue is not None:
            if reference_cue.reference_id in resolved_reference_ids:
                continue
            contrast_evidence, contrast_candidate = candidates_from_contrast_answer(
                cue=reference_cue,
                answer=answer,
                candidate_id_prefix=f"round-{round_data.round_number}",
            )
            evidence.append(contrast_evidence)
            candidates.append(contrast_candidate)
            resolved_reference_ids.add(reference_cue.reference_id)
            continue

        evidence_id = f"round-{round_data.round_number}:user"
        evidence.append(
            RequirementEvidence(
                evidence_id=evidence_id,
                kind=RequirementEvidenceKind.USER_STATEMENT,
                text=answer,
            )
        )
        explicitly_required = bool(_EXPLICIT_REQUIREMENT_RE.search(answer))
        if not explicitly_required:
            continue

        referenced = tuple(
            cue.reference_id
            for cue in state.reference_cues
            if cue.reference_id.casefold() in answer.casefold()
            or cue.label.casefold() in answer.casefold()
        )
        candidate_evidence_ids = [evidence_id]
        for reference_id in referenced:
            reference_evidence_id = f"round-{round_data.round_number}:reference:{reference_id}"
            evidence.append(
                RequirementEvidence(
                    evidence_id=reference_evidence_id,
                    kind=RequirementEvidenceKind.REFERENCE_CUE,
                    text=answer,
                    reference_id=reference_id,
                )
            )
            candidate_evidence_ids.append(reference_evidence_id)
        section = (
            RequirementSection.CONSTRAINT
            if _CONSTRAINT_RE.search(answer)
            else RequirementSection.ACCEPTANCE_CRITERION
        )
        candidates.append(
            RequirementCandidate(
                candidate_id=f"round-{round_data.round_number}:requirement",
                section=section,
                text=answer,
                content_source=(
                    CandidateContentSource.REFERENCE_DERIVED
                    if referenced
                    else CandidateContentSource.USER_STATED
                ),
                resolution=CandidateResolution.CONFIRMED,
                confirmation_authority=ConfirmationAuthority.USER,
                reference_ids=referenced,
                evidence_ids=tuple(candidate_evidence_ids),
                required=True,
            )
        )

    for index, cue in enumerate(state.reference_cues):
        if cue.reference_id in resolved_reference_ids:
            continue
        evidence_id = f"reference-{index}:cue"
        evidence.append(
            RequirementEvidence(
                evidence_id=evidence_id,
                kind=RequirementEvidenceKind.REFERENCE_CUE,
                text=cue.excerpt or cue.label,
                reference_id=cue.reference_id,
            )
        )
        candidates.append(
            RequirementCandidate(
                candidate_id=f"reference-{index}:contrast-required",
                section=RequirementSection.CONTEXT,
                text=f"Reference contrast is unresolved for {cue.label}.",
                content_source=CandidateContentSource.REFERENCE_DERIVED,
                resolution=CandidateResolution.UNKNOWN,
                confirmation_authority=ConfirmationAuthority.NONE,
                reference_ids=(cue.reference_id,),
                evidence_ids=(evidence_id,),
                required=True,
            )
        )

    return RequirementDistillation(
        candidates=tuple(candidates),
        evidence=tuple(evidence),
        input_revision=state.requirement_input_revision,
        input_fingerprint=fingerprint,
    )


def apply_requirement_distillation(
    requirements: dict[str, Any],
    distillation: RequirementDistillation,
) -> AppliedRequirementDistillation:
    """Apply the deterministic gate while preserving legacy non-reference behavior."""
    promotion = evaluate_promotion(distillation)
    if promotion.blockers:
        return AppliedRequirementDistillation(
            requirements=dict(requirements),
            distillation=distillation,
            promotion=promotion,
        )

    has_reference_context = is_reference_aware_distillation(distillation)
    if not has_reference_context:
        return AppliedRequirementDistillation(
            requirements=dict(requirements),
            distillation=distillation,
            promotion=promotion,
        )

    promoted_goals = [
        candidate.text
        for candidate in promotion.promoted
        if candidate.section is RequirementSection.GOAL
    ]
    promoted_criteria = [
        candidate.text
        for candidate in promotion.promoted
        if candidate.section is RequirementSection.ACCEPTANCE_CRITERION
    ]
    promoted_constraints = [
        candidate.text
        for candidate in promotion.promoted
        if candidate.section
        in {RequirementSection.CONSTRAINT, RequirementSection.EXISTING_CONSTRAINT}
    ]
    filtered = {
        "goal": promoted_goals[0] if promoted_goals else "Confirmed interview requirements",
        "constraints": tuple(dict.fromkeys(promoted_constraints)),
        "acceptance_criteria": tuple(promoted_criteria),
        "ontology_name": "ConfirmedRequirementContract",
        "ontology_description": "Only user-authorized interview requirements.",
        "ontology_fields": "",
        "evaluation_principles": (
            "confirmed_requirements:Evaluate only promoted user-authorized requirements:1.0"
        ),
        "exit_conditions": (
            "confirmed_requirements_met:All promoted requirements are satisfied:"
            "Every promoted acceptance criterion passes"
        ),
        "project_type": "greenfield",
    }

    return AppliedRequirementDistillation(
        requirements=filtered,
        distillation=distillation,
        promotion=promotion,
    )


def _normalized_requirement_values(raw_value: object) -> tuple[str, ...]:
    """Normalize promoted requirement values without splitting literal pipes.

    Promoted candidates carry verbatim user statements, so a delimiter-based
    round trip would corrupt values that legitimately contain ``|`` (#1696).
    A plain string therefore stays one value instead of being pipe-split.
    """
    if isinstance(raw_value, str):
        value = raw_value.strip()
        return (value,) if value else ()
    if isinstance(raw_value, list | tuple):
        return tuple(item for item in (str(entry).strip() for entry in raw_value) if item)
    return ()


def build_promoted_reference_seed(
    state: InterviewState,
    distillation: RequirementDistillation,
    *,
    ambiguity_score: float,
) -> Seed:
    """Build a Seed without exposing reference-aware sessions to LLM extraction."""
    applied = apply_requirement_distillation({}, distillation)
    if applied.promotion.blockers:
        raise ValueError(seed_readiness_details(applied.promotion))
    requirements = applied.requirements
    constraints = _normalized_requirement_values(requirements.get("constraints"))
    criteria = _normalized_requirement_values(requirements.get("acceptance_criteria"))
    context_references = tuple(
        ContextReference(
            path=entry.get("path", ""),
            role=entry.get("role", "reference"),
            summary=state.codebase_context,
        )
        for entry in state.codebase_paths
        if entry.get("path")
    )
    brownfield_context = BrownfieldContext(
        project_type="brownfield" if state.is_brownfield else "greenfield",
        context_references=context_references,
    )
    return Seed(
        goal=str(requirements["goal"]),
        constraints=constraints,
        acceptance_criteria=criteria,
        ontology_schema=OntologySchema(
            name="ConfirmedRequirementContract",
            description="Only user-authorized interview requirements.",
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="confirmed_requirements",
                description="Evaluate only promoted user-authorized requirements.",
                weight=1.0,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="confirmed_requirements_met",
                description="All promoted requirements are satisfied.",
                evaluation_criteria="Every promoted acceptance criterion passes.",
            ),
        ),
        brownfield_context=brownfield_context,
        metadata=SeedMetadata(
            ambiguity_score=ambiguity_score,
            interview_id=state.interview_id,
        ),
    )


def seed_readiness_details(promotion: PromotionResult) -> dict[str, Any]:
    """Return typed caller metadata for a blocking promotion result."""
    blockers = []
    for decision in promotion.blockers:
        candidate = decision.candidate
        code = decision.reason
        if (
            candidate.content_source is CandidateContentSource.REFERENCE_DERIVED
            and candidate.resolution is not CandidateResolution.CONFIRMED
        ):
            code = "reference_confirmation_required"
        blockers.append(
            {
                "candidate_id": candidate.candidate_id,
                "code": code,
                "reason": decision.reason,
                "section": candidate.section.value,
                "reference_ids": list(candidate.reference_ids),
            }
        )
    return {
        "code": "interview_reopen_required",
        "blockers": blockers,
    }


__all__ = [
    "AppliedRequirementDistillation",
    "apply_requirement_distillation",
    "build_promoted_reference_seed",
    "build_requirement_distillation",
    "is_reference_aware_distillation",
    "seed_readiness_details",
]
