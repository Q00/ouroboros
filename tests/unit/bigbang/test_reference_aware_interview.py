"""Reference-aware interview state and first-question regression tests."""

from unittest.mock import AsyncMock

import pytest

from ouroboros.bigbang.interview import InterviewEngine, InterviewRound, InterviewState
from ouroboros.core.requirement_candidate import RequirementDistillation
from ouroboros.core.types import Result
from ouroboros.interview_adapters import InterviewTurnContext, ReferenceCue, ReferenceOrigin
from ouroboros.providers.base import CompletionResponse, UsageInfo


def _completion(text: str) -> CompletionResponse:
    return CompletionResponse(
        content=text,
        model="test-model",
        usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        finish_reason="stop",
    )


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
    adapter.complete.return_value = Result.ok(_completion("What outcome matters most?"))
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="test-model")
    state = InterviewState(interview_id="test", initial_context="Build a tool")
    state.merge_turn_context(_reference_context())

    result = await engine.ask_next_question(state)

    assert result.value == "What outcome matters most?"
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


def test_stale_distillation_is_discarded() -> None:
    state = InterviewState(interview_id="test", initial_context="Build a tool")
    state.requirement_distillation = RequirementDistillation(
        input_revision=0,
        input_fingerprint=state.requirement_input_fingerprint(),
    )
    state.rounds.append(InterviewRound(round_number=1, question="Q", user_response="A"))

    assert state.discard_stale_requirement_distillation()
    assert state.requirement_distillation is None
