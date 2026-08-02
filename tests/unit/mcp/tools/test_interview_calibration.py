"""Tests for the session-local interview language-calibration contract."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.interview import InterviewEngine, InterviewRound, InterviewState
from ouroboros.core.types import Result
from ouroboros.interview_calibration import infer_interview_calibration
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from ouroboros.mcp.tools.subagent import build_interview_subagent
from ouroboros.providers.base import CompletionResponse, UsageInfo


def test_inference_uses_mixed_korean_evidence_conservatively() -> None:
    calibration = infer_interview_calibration(
        "멱등성과 이벤트 소싱은 잘 모르고, REST API는 직접 만들어봤어"
    )

    assert calibration.level == "foundational"
    assert calibration.confidence == "high"
    assert calibration.unknown_terms == ("멱등성", "이벤트 소싱")


def test_question_prompt_applies_calibration_without_changing_rigor(tmp_path) -> None:
    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path)
    state = InterviewState(interview_id="interview_1234567890abcdef", initial_context="payments")
    calibration = infer_interview_calibration("I do not know idempotency; I built REST APIs")

    prompt = engine._build_system_prompt(state, language_calibration=calibration)

    assert "Session-local interview language calibration" in prompt
    assert "do not reduce rigor" in prompt
    assert "define necessary domain terms" in prompt
    assert "idempotency" in prompt


def test_plugin_subagent_prompt_receives_the_same_calibration() -> None:
    calibration = infer_interview_calibration("I do not know idempotency; I built REST APIs")

    payload = build_interview_subagent(
        session_id="interview_1234567890abcdef",
        initial_context="payments",
        language_calibration=calibration,
    )

    assert "Session-local interview language calibration" in payload.prompt
    assert "idempotency" in payload.prompt


@pytest.mark.asyncio
async def test_idk_reasks_pending_question_without_recording_an_answer(tmp_path) -> None:
    adapter = MagicMock()
    adapter.complete = AsyncMock(
        return_value=Result.ok(
            CompletionResponse(
                content="쉽게 말해, 결제를 다시 시도해도 돈이 두 번 빠지지 않아야 하나요?",
                model="test",
                usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path)
    state = InterviewState(
        interview_id="interview_1234567890abcdef",
        initial_context="Design payment failure handling",
        rounds=[
            InterviewRound(
                round_number=1,
                question="Should a retry ever create a second charge?",
                user_response=None,
            )
        ],
    )
    assert (await engine.save_state(state)).is_ok
    handler = InterviewHandler(
        interview_engine=engine,
        llm_adapter=adapter,
    )

    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "calibration_input": "I do not know idempotency; I built REST APIs",
        }
    )

    assert result.is_ok
    assert result.value.meta["pending_question_preserved"] is True
    assert result.value.meta["question_rephrased"] is True
    assert result.value.meta["pending_question"] == state.rounds[0].question
    assert "돈이 두 번 빠지지" in result.value.text_content
    reloaded = await engine.load_state(state.interview_id)
    assert reloaded.is_ok
    assert reloaded.value.rounds[0].user_response is None
