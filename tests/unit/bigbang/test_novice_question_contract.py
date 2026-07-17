"""Acceptance tests for answerable interview turn presentations."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from pydantic import ValidationError as PydanticValidationError
import pytest
from structlog.testing import capture_logs

from ouroboros.agents.loader import load_agent_prompt
from ouroboros.bigbang.ambiguity import (
    AMBIGUITY_THRESHOLD,
    AmbiguityScore,
    ComponentScore,
    ScoreBreakdown,
    is_ready_for_seed,
    qualifies_for_seed_completion,
)
from ouroboros.bigbang.interview import (
    NOVICE_FRIENDLY_QUESTION_CONTRACT,
    InterviewEngine,
    InterviewState,
)
from ouroboros.core.types import Result
from ouroboros.interview_adapters import (
    QUESTION_PRESENTATION_CONTRACT_VERSION,
    InterviewQuestionChoice,
    InterviewQuestionPresentation,
    InterviewQuestionRecommendation,
    QuestionChoiceProvenance,
    generated_question_contract_failures,
    parse_question_presentation,
    render_question_presentation,
    rendered_question_contract_failures,
    repair_question_presentation,
)
from ouroboros.providers.base import CompletionResponse, UsageInfo


def _completion(content: str) -> CompletionResponse:
    return CompletionResponse(
        content=content,
        model="test-model",
        usage=UsageInfo(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        finish_reason="stop",
    )


def _presentation(
    *,
    question: str = "Which first outcome should this product focus on?",
    target_dimension: str = "goal_clarity",
    locale: str = "en",
    choice_count: int = 3,
    recommendation: int | None = 1,
    provenance: QuestionChoiceProvenance = QuestionChoiceProvenance.GENERATED_HYPOTHESIS,
) -> InterviewQuestionPresentation:
    choices = tuple(
        InterviewQuestionChoice(
            choice_id=index,
            meaning_key=f"outcome_{index}",
            label=f"Outcome {index}.",
            provenance=provenance,
        )
        for index in range(1, choice_count + 1)
    )
    if locale == "ko":
        recommendation_label = "권장"
        free_text_prompt = "번호로 답하거나 직접 답변을 작성하세요."
    else:
        recommendation_label = "Recommended"
        free_text_prompt = "Reply with the number, or write your own answer."
    return InterviewQuestionPresentation(
        decision_id="first_outcome",
        target_dimension=target_dimension,
        locale=locale,
        question=question,
        choices=choices,
        recommendation=(
            InterviewQuestionRecommendation(
                choice_id=recommendation,
                label=recommendation_label,
                reason="this gives the next step a concrete target",
            )
            if recommendation is not None
            else None
        ),
        free_text_prompt=free_text_prompt,
    )


@pytest.mark.parametrize(
    "question",
    (
        "Which first outcome should this product idea focus on?",
        "Which report result would help maintainers most?",
        "Which onboarding result should a new nontechnical user see first?",
    ),
)
def test_golden_presentations_round_trip(question: str) -> None:
    presentation = _presentation(question=question)

    parsed = parse_question_presentation(presentation.model_dump_json())

    assert parsed == presentation
    assert render_question_presentation(parsed).startswith(question)


def test_four_choices_and_optional_recommendation_are_supported() -> None:
    presentation = _presentation(choice_count=4, recommendation=None)

    rendered = render_question_presentation(presentation)

    assert len(presentation.choices) == 4
    assert "4. Outcome 4." in rendered
    assert "Recommended:" not in rendered


def test_non_english_presentation_is_validated_structurally() -> None:
    presentation = _presentation(
        question="다음으로 어떤 결과를 먼저 정해야 하나요?",
        locale="ko",
    )

    parsed = parse_question_presentation(presentation.model_dump_json())
    rendered = render_question_presentation(presentation)

    assert parsed == presentation
    assert "권장:" in rendered
    assert rendered.endswith("번호로 답하거나 직접 답변을 작성하세요.")
    assert "Recommended" not in rendered
    assert "Reply with" not in rendered
    assert rendered_question_contract_failures(rendered) == ()


def test_repair_preserves_decision_without_inventing_false_choices() -> None:
    repaired = repair_question_presentation(
        "What should this do and how should success be measured?",
        target_dimension="goal_clarity",
        locale="en",
    )

    assert repaired is not None
    assert repaired.question == "What should this do?"
    assert repaired.choices == ()
    assert repaired.recommendation is None
    assert render_question_presentation(repaired).endswith("Write your answer in your own words.")


def test_hidden_multiple_questions_are_rejected() -> None:
    with pytest.raises(PydanticValidationError, match="one reply objective"):
        _presentation(question="Which users should this serve? Which workflow comes first?")


def test_overlapping_choice_meanings_are_rejected() -> None:
    choices = (
        InterviewQuestionChoice(choice_id=1, meaning_key="fast", label="Fast."),
        InterviewQuestionChoice(choice_id=2, meaning_key="fast", label="Fast and cheap."),
    )

    with pytest.raises(PydanticValidationError, match="distinct meaning_key"):
        InterviewQuestionPresentation(
            decision_id="delivery_priority",
            target_dimension="constraint_clarity",
            question="Which delivery priority matters most?",
            choices=choices,
            free_text_prompt="Reply with the number, or write your own answer.",
        )


def test_invalid_recommendation_is_rejected() -> None:
    with pytest.raises(PydanticValidationError, match="presented choice"):
        InterviewQuestionPresentation(
            decision_id="first_outcome",
            target_dimension="goal_clarity",
            question="Which outcome should come first?",
            choices=_presentation(choice_count=2).choices,
            recommendation=InterviewQuestionRecommendation(
                choice_id=3,
                reason="it is not actually present",
            ),
            free_text_prompt="Reply with the number, or write your own answer.",
        )


def test_missing_direct_input_escape_is_rejected() -> None:
    payload = _presentation().model_dump(mode="json")
    payload["allow_free_text"] = False

    with pytest.raises(PydanticValidationError, match="free-text answers"):
        InterviewQuestionPresentation.model_validate(payload)


def test_generated_choices_cannot_claim_user_intent() -> None:
    presentation = _presentation(provenance=QuestionChoiceProvenance.USER_GOAL)

    assert generated_question_contract_failures(presentation) == (
        "provider-generated choices must remain generated hypotheses",
    )


@pytest.mark.parametrize(
    "field_value",
    (
        "Which ambiguity dimension should we reduce?",
        "Should we choose the ontology boundary?",
        "Which path improves the clarity score?",
    ),
)
def test_internal_process_terms_cannot_leak(field_value: str) -> None:
    payload = _presentation().model_dump(mode="json")
    payload["question"] = field_value

    with pytest.raises(PydanticValidationError, match="internal interview terminology"):
        InterviewQuestionPresentation.model_validate(payload)


def test_interview_prompt_includes_structured_contract_once(tmp_path) -> None:
    engine = InterviewEngine(llm_adapter=object(), state_dir=tmp_path)
    prompt = engine._build_system_prompt(
        InterviewState(
            interview_id="novice_contract",
            initial_context="Build a simple project planning app.",
        )
    )

    assert prompt.count(NOVICE_FRIENDLY_QUESTION_CONTRACT) == 1
    assert QUESTION_PRESENTATION_CONTRACT_VERSION in prompt


def test_toolless_prompt_includes_structured_contract_once(tmp_path) -> None:
    engine = InterviewEngine(
        llm_adapter=object(),
        state_dir=tmp_path,
        suppress_tool_use_prompt_cues=True,
    )

    prompt = engine._build_system_prompt(
        InterviewState(
            interview_id="novice_contract_toolless",
            initial_context="Build a simple project planning app.",
        )
    )

    assert prompt.count(NOVICE_FRIENDLY_QUESTION_CONTRACT) == 1


def test_agent_prompt_delegates_to_runtime_contract() -> None:
    prompt = load_agent_prompt("socratic-interviewer")

    assert "MUST always end with a question" not in prompt
    assert "Keep questions focused (1-2 sentences)" not in prompt
    assert "Answerable Interview Turn Contract" in prompt
    assert "exactly one JSON presentation object" in prompt


@pytest.mark.asyncio
async def test_runtime_renders_valid_structured_question_with_one_provider_call(tmp_path) -> None:
    presentation = _presentation()
    adapter = MagicMock()
    adapter.complete = AsyncMock(
        return_value=Result.ok(_completion(presentation.model_dump_json()))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path)
    state = InterviewState(
        interview_id="valid_runtime_question",
        initial_context="Build a simple project planning app.",
    )

    result = await engine.ask_next_question(state)

    assert result.is_ok
    assert result.value == render_question_presentation(presentation)
    assert state.pending_question_presentation == presentation
    adapter.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_repairs_invalid_output_without_changing_its_decision(tmp_path) -> None:
    invalid_question = "What should this do and how should success be measured?"
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=Result.ok(_completion(invalid_question)))
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path)
    state = InterviewState(
        interview_id="invalid_runtime_question",
        initial_context="Build a project planning tool.",
    )
    state.ambiguity_breakdown = {
        "goal_clarity": {"clarity_score": 0.8},
        "constraint_clarity": {"clarity_score": 0.2},
    }

    result = await engine.ask_next_question(state)

    assert result.is_ok
    assert state.pending_question_presentation is not None
    assert state.pending_question_presentation.target_dimension == "constraint_clarity"
    assert state.pending_question_presentation.locale == "en"
    assert state.pending_question_presentation.question == "What should this do?"
    assert state.pending_question_presentation.choices == ()
    assert result.value == ("What should this do?\nWrite your answer in your own words.")
    adapter.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_uses_observable_localized_fallback_when_repair_is_unsafe(
    tmp_path,
) -> None:
    adapter = MagicMock()
    adapter.complete = AsyncMock(
        return_value=Result.ok(
            _completion("What should this do and how should success be measured?")
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path)
    state = InterviewState(
        interview_id="unsafe_cross_locale_question",
        initial_context="프로젝트 계획 도구를 만듭니다.",
    )
    state.ambiguity_breakdown = {
        "goal_clarity": {"clarity_score": 0.8},
        "constraint_clarity": {"clarity_score": 0.2},
    }

    with capture_logs() as captured:
        result = await engine.ask_next_question(state)

    assert result.is_ok
    assert state.pending_question_presentation is not None
    assert state.pending_question_presentation.target_dimension == "constraint_clarity"
    assert state.pending_question_presentation.locale == "ko"
    assert state.pending_question_presentation.choices == ()
    assert result.value == (
        "이 프로젝트가 반드시 지켜야 할 제한이나 요구사항은 무엇인가요?\n직접 답변을 작성하세요."
    )
    violation = next(
        event for event in captured if event.get("event") == "interview.question_contract_violation"
    )
    assert violation["fallback_used"] is True
    assert violation["repair_attempted"] is False
    adapter.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_repairs_generated_provenance_spoofing(tmp_path) -> None:
    spoofed = _presentation(provenance=QuestionChoiceProvenance.USER_GOAL)
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=Result.ok(_completion(spoofed.model_dump_json())))
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path)
    state = InterviewState(
        interview_id="spoofed_provenance",
        initial_context="Build a simple project planning app.",
    )

    result = await engine.ask_next_question(state)

    assert result.is_ok
    assert state.pending_question_presentation is not None
    assert all(
        choice.provenance is QuestionChoiceProvenance.GENERATED_HYPOTHESIS
        for choice in state.pending_question_presentation.choices
    )
    adapter.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_response_persists_the_exact_question_presentation(tmp_path) -> None:
    presentation = _presentation()
    adapter = MagicMock()
    adapter.complete = AsyncMock(
        return_value=Result.ok(_completion(presentation.model_dump_json()))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path)
    state = InterviewState(interview_id="persist_presentation", initial_context="Build a tool")
    question = (await engine.ask_next_question(state)).value

    result = await engine.record_response(state, "1", question)

    assert result.is_ok
    assert state.rounds[-1].question_presentation == presentation
    assert state.pending_question_presentation is None


def test_existing_seed_readiness_semantics_remain_unchanged() -> None:
    breakdown = ScoreBreakdown(
        goal_clarity=ComponentScore(
            name="Goal Clarity",
            clarity_score=0.9,
            weight=0.4,
            justification="Clear.",
        ),
        constraint_clarity=ComponentScore(
            name="Constraint Clarity",
            clarity_score=0.9,
            weight=0.3,
            justification="Clear.",
        ),
        success_criteria_clarity=ComponentScore(
            name="Success Criteria Clarity",
            clarity_score=0.9,
            weight=0.3,
            justification="Clear.",
        ),
    )

    assert AMBIGUITY_THRESHOLD == 0.2
    assert is_ready_for_seed(AmbiguityScore(0.2, breakdown)) is True
    assert is_ready_for_seed(AmbiguityScore(0.21, breakdown)) is False
    assert qualifies_for_seed_completion(
        AmbiguityScore(0.2, breakdown),
        is_brownfield=False,
    )

    below_floor = ScoreBreakdown(
        goal_clarity=breakdown.goal_clarity,
        constraint_clarity=ComponentScore(
            name="Constraint Clarity",
            clarity_score=0.6,
            weight=0.3,
            justification="Below the completion floor.",
        ),
        success_criteria_clarity=breakdown.success_criteria_clarity,
    )
    assert not qualifies_for_seed_completion(
        AmbiguityScore(0.2, below_floor),
        is_brownfield=False,
    )
