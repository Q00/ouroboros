"""Tests for generic reference-aware requirement distillation and filtering."""

from unittest.mock import AsyncMock

import pytest

from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.bigbang.requirement_distillation import (
    apply_requirement_distillation,
    build_requirement_distillation,
)
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.requirement_candidate import (
    CandidateContentSource,
    CandidateResolution,
    ConfirmationAuthority,
    RequirementCandidate,
    RequirementDistillation,
    RequirementEvidence,
    RequirementEvidenceKind,
    RequirementSection,
)
from ouroboros.core.types import Result
from ouroboros.interview_adapters import (
    InterviewTurnContext,
    ReferenceContrastResolution,
    ReferenceCue,
    ReferenceOrigin,
    ReferenceResolutionStatus,
    build_reference_contrast_question,
)
from ouroboros.providers.base import CompletionResponse, UsageInfo


def _reference_state(*, confirmation: str | None = None) -> InterviewState:
    cue = ReferenceCue(
        reference_id="linear",
        label="Linear-like",
        origin=ReferenceOrigin.USER_TEXT,
    )
    contrast_question = build_reference_contrast_question(cue)
    rounds = [
        InterviewRound(
            round_number=1,
            question="What outcome matters most?",
            user_response="Fast issue triage.",
        ),
        InterviewRound(
            round_number=2,
            question=contrast_question,
            user_response="Copy the workflow speed, not the command menu.",
        ),
    ]
    if confirmation:
        rounds.append(
            InterviewRound(
                round_number=3,
                question="Which reference traits are actual requirements?",
                user_response=confirmation,
            )
        )
    return InterviewState(
        interview_id="reference-test",
        initial_context="Build a Linear-like issue tool",
        rounds=rounds,
        reference_cues=(cue,),
        reference_resolutions=(
            ReferenceContrastResolution(
                reference_id="linear",
                status=ReferenceResolutionStatus.RESOLVED,
                asked_question=contrast_question,
                answer="Copy the workflow speed, not the command menu.",
            ),
        ),
    )


def _requirements() -> dict[str, object]:
    return {
        "goal": "Build an issue tool",
        "constraints": "Python",
        "acceptance_criteria": (
            "Keyboard-first command menu | Queue navigation | Fast issue triage"
        ),
        "ontology_name": "IssueTool",
        "ontology_description": "Issue workflow",
    }


def _low_ambiguity() -> AmbiguityScore:
    return AmbiguityScore(
        overall_score=0.1,
        breakdown=ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal",
                clarity_score=0.9,
                weight=0.4,
                justification="clear",
            ),
            constraint_clarity=ComponentScore(
                name="Constraints",
                clarity_score=0.9,
                weight=0.3,
                justification="clear",
            ),
            success_criteria_clarity=ComponentScore(
                name="Success",
                clarity_score=0.9,
                weight=0.3,
                justification="clear",
            ),
        ),
    )


def _extraction_response() -> CompletionResponse:
    return CompletionResponse(
        content="""GOAL: Build an issue tool
CONSTRAINTS: Python
ACCEPTANCE_CRITERIA:
AC: Keyboard-first command menu | verify: NONE | artifacts: NONE | expect: NONE
AC: Queue navigation | verify: NONE | artifacts: NONE | expect: NONE
ONTOLOGY_NAME: IssueTool
ONTOLOGY_DESCRIPTION: Issue workflow
ONTOLOGY_FIELDS: issue:string:Issue
EVALUATION_PRINCIPLES: correctness:Requirements are met:1.0
EXIT_CONDITIONS: done:All criteria met:All criteria pass
PROJECT_TYPE: greenfield""",
        model="test-model",
        usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        finish_reason="stop",
    )


def test_reference_contrast_does_not_promote_inferred_acceptance_criteria() -> None:
    distillation = build_requirement_distillation(_reference_state())

    applied = apply_requirement_distillation(_requirements(), distillation)

    assert applied.promotion.is_ready_for_seed
    assert applied.requirements["acceptance_criteria"] == ""


def test_unresolved_reference_blocks_seed_generation() -> None:
    cue = ReferenceCue(
        reference_id="linear",
        label="Linear-like",
        origin=ReferenceOrigin.USER_TEXT,
    )
    state = InterviewState(
        interview_id="unresolved-reference",
        initial_context="Build an issue tool",
        rounds=[
            InterviewRound(
                round_number=1,
                question="What outcome matters most?",
                user_response="Fast issue triage.",
            )
        ],
        reference_cues=(cue,),
    )

    applied = apply_requirement_distillation({}, build_requirement_distillation(state))

    assert not applied.promotion.is_ready_for_seed
    blocker = applied.promotion.blockers[0]
    assert blocker.candidate.content_source is CandidateContentSource.REFERENCE_DERIVED
    assert blocker.candidate.resolution is CandidateResolution.UNKNOWN
    assert blocker.reason == "required_unknown"


def test_explicit_reference_confirmation_promotes_exact_user_statement() -> None:
    state = _reference_state(
        confirmation="For the Linear-like reference, keyboard-first navigation is required."
    )
    distillation = build_requirement_distillation(state)

    applied = apply_requirement_distillation(_requirements(), distillation)

    assert applied.promotion.is_ready_for_seed
    assert applied.requirements["acceptance_criteria"] == (
        "For the Linear-like reference, keyboard-first navigation is required."
    )
    promoted = [
        candidate
        for candidate in applied.promotion.promoted
        if candidate.candidate_id == "round-3:requirement"
    ][0]
    assert promoted.content_source.value == "reference_derived"
    assert promoted.confirmation_authority.value == "user"


@pytest.mark.parametrize(
    "confirmation",
    [
        "확인된 요구사항은 키보드만으로 탐색할 수 있어야 한다는 것입니다.",
        "確認済みの要件は、キーボードだけで移動できることです。",
    ],
)
def test_non_english_confirmation_after_reference_contrast_is_preserved(
    confirmation: str,
) -> None:
    distillation = build_requirement_distillation(_reference_state(confirmation=confirmation))

    applied = apply_requirement_distillation(_requirements(), distillation)

    assert applied.promotion.is_ready_for_seed
    assert applied.requirements["acceptance_criteria"] == confirmation
    promoted = [
        candidate
        for candidate in applied.promotion.promoted
        if candidate.candidate_id == "round-3:requirement"
    ][0]
    assert promoted.content_source is CandidateContentSource.USER_STATED
    assert promoted.confirmation_authority is ConfirmationAuthority.USER


def test_ordinary_follow_up_after_reference_contrast_is_not_promoted() -> None:
    distillation = build_requirement_distillation(
        _reference_state(confirmation="Maybe blue is nice.")
    )

    applied = apply_requirement_distillation(_requirements(), distillation)

    assert applied.promotion.is_ready_for_seed
    assert applied.requirements["acceptance_criteria"] == ""
    assert all(
        candidate.candidate_id != "round-3:requirement" for candidate in distillation.candidates
    )


def test_non_reference_interview_preserves_legacy_extraction() -> None:
    state = InterviewState(
        interview_id="legacy",
        initial_context="Build a CLI",
        rounds=[
            InterviewRound(
                round_number=1,
                question="What matters?",
                user_response="It must print hello.",
            )
        ],
    )
    requirements = _requirements()

    applied = apply_requirement_distillation(
        requirements,
        build_requirement_distillation(state),
    )

    assert applied.requirements == requirements


def test_reference_cue_merge_changes_fingerprint_and_revision() -> None:
    state = InterviewState(interview_id="test", initial_context="Build a tool")
    before = build_requirement_distillation(state)
    state.merge_turn_context(
        InterviewTurnContext(
            references=(
                ReferenceCue(
                    reference_id="linear",
                    label="Linear",
                    origin=ReferenceOrigin.USER_TEXT,
                ),
            )
        )
    )
    after = build_requirement_distillation(state)

    assert after.input_revision == before.input_revision + 1
    assert after.input_fingerprint != before.input_fingerprint


@pytest.mark.asyncio
async def test_seed_generator_filters_unconfirmed_reference_acs(tmp_path) -> None:
    adapter = AsyncMock()
    adapter.complete.return_value = Result.ok(_extraction_response())
    generator = SeedGenerator(
        llm_adapter=adapter,
        model="test-model",
        output_dir=tmp_path,
    )

    result = await generator.generate(_reference_state(), _low_ambiguity())

    assert result.is_ok
    assert result.value.acceptance_criteria == ()


@pytest.mark.asyncio
async def test_seed_generator_keeps_explicitly_confirmed_reference_ac(tmp_path) -> None:
    adapter = AsyncMock()
    adapter.complete.return_value = Result.ok(_extraction_response())
    generator = SeedGenerator(
        llm_adapter=adapter,
        model="test-model",
        output_dir=tmp_path,
    )
    state = _reference_state(
        confirmation="For the Linear-like reference, keyboard-first navigation is required."
    )

    result = await generator.generate(state, _low_ambiguity())

    assert result.is_ok
    assert tuple(str(item) for item in result.value.acceptance_criteria) == (
        "For the Linear-like reference, keyboard-first navigation is required.",
    )


@pytest.mark.asyncio
async def test_seed_generator_returns_typed_reopen_error_for_conflict(tmp_path) -> None:
    adapter = AsyncMock()
    generator = SeedGenerator(
        llm_adapter=adapter,
        model="test-model",
        output_dir=tmp_path,
    )
    state = InterviewState(interview_id="conflict", initial_context="Build a tool")
    fingerprint = state.requirement_input_fingerprint()
    state.requirement_distillation = RequirementDistillation(
        candidates=(
            RequirementCandidate(
                candidate_id="conflict-1",
                section=RequirementSection.ACCEPTANCE_CRITERION,
                text="Automate approval but require manual approval.",
                content_source=CandidateContentSource.USER_STATED,
                resolution=CandidateResolution.CONFLICTING,
                confirmation_authority=ConfirmationAuthority.NONE,
                evidence_ids=("user-1",),
                required=True,
            ),
        ),
        evidence=(
            RequirementEvidence(
                evidence_id="user-1",
                kind=RequirementEvidenceKind.USER_STATEMENT,
                text="Automate approval but require manual approval.",
            ),
        ),
        input_revision=state.requirement_input_revision,
        input_fingerprint=fingerprint,
    )

    result = await generator.generate(state, _low_ambiguity())

    assert result.is_err
    assert result.error.details["code"] == "interview_reopen_required"
    assert result.error.details["blockers"][0]["reason"] == "conflict_requires_tradeoff"
    adapter.complete.assert_not_awaited()
