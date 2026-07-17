"""Reference-aware interview state and first-question regression tests."""

from unittest.mock import AsyncMock

import pytest

from ouroboros.bigbang.interview import (
    InterviewEngine,
    InterviewRound,
    InterviewState,
)
from ouroboros.core.requirement_candidate import RequirementDistillation
from ouroboros.core.types import Result
from ouroboros.interview_adapters import (
    InterviewQuestionChoice,
    InterviewQuestionPresentation,
    InterviewQuestionRecommendation,
    InterviewTurnContext,
    ReferenceCue,
    ReferenceOrigin,
    render_question_presentation,
)
from ouroboros.providers.base import CompletionResponse, UsageInfo


def _completion(text: str) -> CompletionResponse:
    return CompletionResponse(
        content=text,
        model="test-model",
        usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        finish_reason="stop",
    )


def _base_presentation() -> InterviewQuestionPresentation:
    return InterviewQuestionPresentation(
        decision_id="first_project_outcome",
        target_dimension="goal_clarity",
        question="Which outcome should guide this project first?",
        choices=(
            InterviewQuestionChoice(
                choice_id=1,
                meaning_key="faster_triage",
                label="Faster issue triage.",
            ),
            InterviewQuestionChoice(
                choice_id=2,
                meaning_key="clearer_ownership",
                label="Clearer ownership.",
            ),
            InterviewQuestionChoice(
                choice_id=3,
                meaning_key="better_reporting",
                label="Better reporting.",
            ),
        ),
        recommendation=InterviewQuestionRecommendation(
            choice_id=1,
            reason="the first workflow should anchor later decisions",
        ),
        free_text_prompt="Reply with the number, or write your own answer.",
    )


def _base_question() -> str:
    return render_question_presentation(_base_presentation())


def _reference_context() -> InterviewTurnContext:
    return InterviewTurnContext(
        references=(
            ReferenceCue(
                reference_id="linear",
                label="Linear-like",
                origin=ReferenceOrigin.USER_TEXT,
            ),
        )
    )


def test_legacy_state_loads_with_empty_adapter_defaults() -> None:
    state = InterviewState.model_validate_json(
        '{"interview_id":"legacy","rounds":[],"initial_context":"Build a tool"}'
    )

    assert state.reference_cues == ()
    assert state.reference_resolutions == ()
    assert state.pending_confused_terms == ()
    assert state.requirement_input_revision == 0
    assert state.requirement_distillation is None


def test_reference_merge_is_idempotent_and_invalidates_changed_inputs() -> None:
    state = InterviewState(interview_id="test", initial_context="Build a tool")

    assert state.merge_turn_context(_reference_context())
    assert state.requirement_input_revision == 1
    assert state.reference_cues[0].reference_id == "linear"

    assert not state.merge_turn_context(_reference_context())
    assert state.requirement_input_revision == 1


@pytest.mark.asyncio
async def test_first_question_never_injects_queued_reference(tmp_path) -> None:
    adapter = AsyncMock()
    presentation = _base_presentation()
    question = render_question_presentation(presentation)
    adapter.complete.return_value = Result.ok(_completion(presentation.model_dump_json()))
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="test-model")
    state = InterviewState(interview_id="test", initial_context="Build a tool")
    state.merge_turn_context(_reference_context())

    result = await engine.ask_next_question(state)

    assert result.value == question
    assert state.pending_question_presentation == presentation
    assert "Linear" not in result.value
    adapter.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_reference_contrast_runs_after_base_answer_without_llm_call(tmp_path) -> None:
    adapter = AsyncMock()
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="test-model")
    state = InterviewState(
        interview_id="test",
        initial_context="Build a tool",
        rounds=(
            InterviewRound(
                round_number=1,
                question="What outcome matters most?",
                user_response="Fast issue triage.",
            ),
        ),
    )
    state.merge_turn_context(_reference_context())

    result = await engine.ask_next_question(state)

    assert "Linear-like" in result.value
    assert "surface look" in result.value
    assert state.pending_question_presentation is not None
    assert result.value == render_question_presentation(state.pending_question_presentation)
    assert state.reference_resolutions[0].status.value == "asked"
    adapter.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_confusion_injects_bounded_glossary_after_base_answer(tmp_path) -> None:
    adapter = AsyncMock()
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="test-model")
    state = InterviewState(
        interview_id="test",
        initial_context="Build a tool",
        rounds=(
            InterviewRound(
                round_number=1,
                question="What outcome matters most?",
                user_response="I do not understand affordance.",
            ),
        ),
        pending_confused_terms=("affordance",),
    )

    result = await engine.ask_next_question(state)

    assert "Glossary help (ui_ux_basics)" in result.value
    assert "affordance" in result.value
    assert state.pending_question_presentation is not None
    assert result.value == render_question_presentation(state.pending_question_presentation)
    assert state.pending_confused_terms == ()
    adapter.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_response_resolves_reference_and_invalidates_cache(tmp_path) -> None:
    adapter = AsyncMock()
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="test-model")
    state = InterviewState(
        interview_id="test",
        initial_context="Build a tool",
        rounds=(
            InterviewRound(
                round_number=1,
                question="What outcome matters most?",
                user_response="Fast issue triage.",
            ),
        ),
    )
    state.merge_turn_context(_reference_context())
    contrast = state.next_adapter_question()
    assert contrast is not None
    state.requirement_distillation = RequirementDistillation(
        input_revision=state.requirement_input_revision,
        input_fingerprint=state.requirement_input_fingerprint(),
    )

    result = await engine.record_response(
        state,
        "Copy the speed, not the command menu.",
        contrast,
    )

    assert result.is_ok
    assert state.reference_resolutions[0].status.value == "resolved"
    assert state.reference_resolutions[0].answer == "Copy the speed, not the command menu."
    assert state.requirement_distillation is None
    assert state.requirement_input_revision == 2


@pytest.mark.asyncio
async def test_reference_fallback_matches_persisted_question(tmp_path) -> None:
    adapter = AsyncMock()
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="test-model")
    state = InterviewState(
        interview_id="test-fallback",
        initial_context="Build a tool",
        rounds=(
            InterviewRound(
                round_number=1,
                question=_base_question(),
                user_response="Fast issue triage.",
            ),
        ),
    )
    state.merge_turn_context(
        InterviewTurnContext(
            references=(
                ReferenceCue(
                    reference_id="internal-term",
                    label="Ambiguity dimension",
                    origin=ReferenceOrigin.USER_TEXT,
                ),
            )
        )
    )

    question_result = await engine.ask_next_question(state)

    assert question_result.is_ok
    assert state.pending_question_presentation is not None
    assert question_result.value == render_question_presentation(
        state.pending_question_presentation
    )
    assert state.reference_resolutions[0].asked_question == question_result.value

    response_result = await engine.record_response(
        state,
        "Use it only as a comparison.",
        question_result.value,
    )

    assert response_result.is_ok
    assert state.reference_resolutions[0].status.value == "resolved"


@pytest.mark.asyncio
async def test_repaired_reference_question_is_persisted_exactly(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _malformed_reference_question(*_args: object, **_kwargs: object) -> str:
        return """Which role should this reference play and how should we use it?
1. Use it only as a comparison.
2. Avoid its approach.
Reply with the number, or write your own answer."""

    monkeypatch.setattr(
        "ouroboros.bigbang.interview.build_reference_contrast_presentation",
        _malformed_reference_question,
    )
    engine = InterviewEngine(llm_adapter=AsyncMock(), state_dir=tmp_path, model="test-model")
    state = InterviewState(
        interview_id="test-repaired-reference",
        initial_context="Build a tool",
        rounds=(
            InterviewRound(
                round_number=1,
                question=_base_question(),
                user_response="Fast issue triage.",
            ),
        ),
    )
    state.merge_turn_context(_reference_context())

    question_result = await engine.ask_next_question(state)

    assert question_result.is_ok
    assert question_result.value == (
        "Which role should this reference play?\n"
        "1. Use it only as a comparison.\n"
        "2. Avoid its approach.\n"
        "Reply with the number, or write your own answer."
    )
    assert state.reference_resolutions[0].asked_question == question_result.value
    assert state.pending_question_presentation is not None
    assert state.pending_question_presentation.question == (
        "Which role should this reference play?"
    )


def test_stale_distillation_is_discarded() -> None:
    state = InterviewState(interview_id="test", initial_context="Build a tool")
    state.requirement_distillation = RequirementDistillation(
        input_revision=0,
        input_fingerprint=state.requirement_input_fingerprint(),
    )
    state.rounds.append(InterviewRound(round_number=1, question="Q", user_response="A"))

    assert state.discard_stale_requirement_distillation()
    assert state.requirement_distillation is None
