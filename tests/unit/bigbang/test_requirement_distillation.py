"""Tests for generic reference-aware requirement distillation and filtering."""

from unittest.mock import AsyncMock

import pytest

from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.bigbang.requirement_distillation import (
    apply_requirement_distillation,
    build_promoted_reference_seed,
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
CONSTRAINTS: ["Python"]
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
    assert applied.requirements["acceptance_criteria"] == ()


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
        "For the Linear-like reference, keyboard-first navigation is required.",
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
    assert applied.requirements["acceptance_criteria"] == (confirmation,)
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
    assert applied.requirements["acceptance_criteria"] == ()
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


def test_promoted_reference_seed_preserves_literal_pipe_in_constraint() -> None:
    state = _reference_state(
        confirmation="The CLI must accept only --lang ko|en as the language flag."
    )
    state.rounds.append(
        InterviewRound(
            round_number=4,
            question="How is successful language selection confirmed?",
            user_response="The confirmed requirement is that the selected language is printed.",
        )
    )
    distillation = build_requirement_distillation(state)

    seed = build_promoted_reference_seed(state, distillation, ambiguity_score=0.1)

    assert seed.constraints == ("The CLI must accept only --lang ko|en as the language flag.",)


def test_promoted_reference_seed_preserves_literal_pipe_in_acceptance_criterion() -> None:
    state = _reference_state(
        confirmation="The confirmed requirement is that output must show ko|en verbatim."
    )
    distillation = build_requirement_distillation(state)

    seed = build_promoted_reference_seed(state, distillation, ambiguity_score=0.1)

    assert tuple(str(item) for item in seed.acceptance_criteria) == (
        "The confirmed requirement is that output must show ko|en verbatim.",
    )


def test_promoted_reference_seed_rejects_empty_contract() -> None:
    state = _reference_state()
    distillation = build_requirement_distillation(state)

    with pytest.raises(ValueError, match="no_promoted_acceptance_criteria"):
        build_promoted_reference_seed(state, distillation, ambiguity_score=0.1)


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
async def test_seed_generator_reopens_when_no_reference_acs_are_promoted(tmp_path) -> None:
    adapter = AsyncMock()
    adapter.complete.return_value = Result.ok(_extraction_response())
    generator = SeedGenerator(
        llm_adapter=adapter,
        model="test-model",
        output_dir=tmp_path,
    )

    result = await generator.generate(_reference_state(), _low_ambiguity())

    assert result.is_err
    assert result.error.details["code"] == "interview_reopen_required"
    assert result.error.details["blockers"][0]["code"] == "no_promoted_acceptance_criteria"
    adapter.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_reference_seed_never_succeeds_with_an_empty_contract(tmp_path) -> None:
    cue = ReferenceCue(
        reference_id="dashboard-shot",
        label="dashboard reference screenshot",
        origin=ReferenceOrigin.FILE_REFERENCE,
        excerpt="A dense desktop dashboard with a sidebar and review queue.",
    )
    contrast_question = build_reference_contrast_question(cue)
    contrast_answer = (
        "Copy the compact hierarchy and sidebar placement; avoid the reference colors and branding."
    )
    rounds = [
        InterviewRound(
            round_number=1,
            question=contrast_question,
            user_response=contrast_answer,
        )
    ]
    rounds.extend(
        InterviewRound(
            round_number=index,
            question=f"Clarify dashboard behavior {index}.",
            user_response=f"""[from-user][refined]
Decision: Panel {index} stays visible after refresh.

Constraints (user-stated):
- Panel {index} uses the shared spacing scale.

Out of scope (user-stated):
- Custom themes for panel {index}.""",
        )
        for index in range(2, 12)
    )
    state = InterviewState(
        interview_id="synthetic-reference-refined",
        initial_context="Create a responsive review dashboard from a reference screenshot.",
        rounds=rounds,
        reference_cues=(cue,),
        reference_resolutions=(
            ReferenceContrastResolution(
                reference_id=cue.reference_id,
                status=ReferenceResolutionStatus.RESOLVED,
                asked_question=contrast_question,
                answer=contrast_answer,
            ),
        ),
    )
    adapter = AsyncMock()
    generator = SeedGenerator(
        llm_adapter=adapter,
        model="test-model",
        output_dir=tmp_path,
    )

    result = await generator.generate(state, _low_ambiguity())

    assert len(state.rounds) == 11
    assert result.is_err
    assert result.error.details["code"] == "interview_reopen_required"
    assert result.error.details["blockers"][0]["code"] == "no_promoted_acceptance_criteria"
    adapter.complete.assert_not_awaited()


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


def test_resolved_reference_with_drifted_round_text_does_not_reblock() -> None:
    # The 0.52.0 field incident: the resolution ledger said RESOLVED, but the
    # persisted round question had drifted from asked_question (host-rendered
    # echo), so the round walk missed it and the unresolved-contrast blocker
    # was resurrected -- Seed generation pinned on
    # reference_confirmation_required with no way out. The ledger is the
    # authority: a RESOLVED reference must never re-block on round-text drift.
    state = _reference_state()
    drifted_rounds = list(state.rounds)
    drifted_rounds[1] = InterviewRound(
        round_number=2,
        question="[rendered by host] " + drifted_rounds[1].question[:40],
        user_response=drifted_rounds[1].user_response,
    )
    state = state.model_copy(update={"rounds": drifted_rounds})

    applied = apply_requirement_distillation(_requirements(), build_requirement_distillation(state))

    assert applied.promotion.is_ready_for_seed
    contrast = [
        candidate
        for candidate in applied.distillation.candidates
        if candidate.reference_ids == ("linear",)
        and candidate.resolution is CandidateResolution.NEEDS_CONFIRMATION
    ]
    assert len(contrast) == 1
    assert contrast[0].text == "Copy the workflow speed, not the command menu."


def test_whitespace_drifted_round_text_still_promotes_contrast_via_round_walk() -> None:
    state = _reference_state()
    drifted_rounds = list(state.rounds)
    drifted_rounds[1] = InterviewRound(
        round_number=2,
        question="  " + drifted_rounds[1].question.replace("\n", " \n ").upper(),
        user_response=drifted_rounds[1].user_response,
    )
    state = state.model_copy(update={"rounds": drifted_rounds})

    distillation = build_requirement_distillation(state)

    contrast = [
        candidate
        for candidate in distillation.candidates
        if candidate.reference_ids == ("linear",)
        and candidate.resolution is CandidateResolution.NEEDS_CONFIRMATION
    ]
    assert len(contrast) == 1
    assert contrast[0].candidate_id.startswith("round-2")


def test_anchor_backstop_appends_dropped_promoted_requirement() -> None:
    from ouroboros.bigbang.requirement_distillation import anchor_promoted_requirements

    committed = "The exporter must emit RFC 3339 timestamps, required."
    state = _reference_state(confirmation=committed)
    distillation = build_requirement_distillation(state)
    applied = apply_requirement_distillation(_requirements(), distillation)

    # Simulate the LLM extraction dropping/paraphrasing the committed line.
    # "must" without a constraint marker classifies it as an acceptance
    # criterion (_CONSTRAINT_RE), so that is where the verbatim copy lands.
    requirements = {"goal": "Build an issue tool", "acceptance_criteria": ["Fast triage"]}
    updated, appended = anchor_promoted_requirements(requirements, applied.promotion)

    assert appended == 1
    assert updated["acceptance_criteria"] == ["Fast triage", committed]


def test_anchor_backstop_is_a_noop_when_wording_survived() -> None:
    from ouroboros.bigbang.requirement_distillation import anchor_promoted_requirements

    committed = "The exporter must emit RFC 3339 timestamps, required."
    state = _reference_state(confirmation=committed)
    distillation = build_requirement_distillation(state)
    applied = apply_requirement_distillation(_requirements(), distillation)

    # Extraction kept the wording (whitespace/case drift tolerated) — the
    # backstop must not duplicate it.
    survived = "  The exporter must emit  RFC 3339 timestamps, REQUIRED. "
    requirements = {
        "goal": "Build an issue tool",
        "acceptance_criteria": [survived],
        "constraints": [],
    }
    updated, appended = anchor_promoted_requirements(requirements, applied.promotion)

    assert appended == 0
    assert updated["acceptance_criteria"] == [survived]
    assert updated["constraints"] == []
