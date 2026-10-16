"""Tests for the session-local interview language-calibration contract."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.interview import InterviewEngine, InterviewRound, InterviewState
from ouroboros.core.types import Result
from ouroboros.interview_calibration import infer_interview_calibration
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from ouroboros.mcp.tools.interview_calibration import (
    _engine_supports_calibration,
    handle_interview_calibration_turn,
)
from ouroboros.mcp.tools.subagent import build_interview_subagent
from ouroboros.providers.base import CompletionResponse, UsageInfo
from ouroboros.router import SkillDispatchRouter
from ouroboros.router.types import Resolved


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
@pytest.mark.parametrize(
    ("evidence", "unknown_terms"),
    [
        ("I do not know idempotency; I built REST APIs", ["idempotency"]),
        ("idempotency", ["idempotency"]),
        ("idempotency, event sourcing", ["idempotency", "event sourcing"]),
        ("idempotency or event sourcing", ["idempotency", "event sourcing"]),
        ("CAN bus", ["CAN bus"]),
        ("user experience", ["user experience"]),
    ],
)
async def test_bare_idk_reasks_pending_question_without_recording_an_answer(
    tmp_path, evidence, unknown_terms
) -> None:
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

    resolved = SkillDispatchRouter().resolve(
        f"ooo idk {evidence}",
        skills_dir=Path(__file__).resolve().parents[4] / "skills",
    )
    assert isinstance(resolved, Resolved)
    result = await handler.handle({**resolved.mcp_args, "session_id": state.interview_id})

    assert result.is_ok
    calibration = result.value.meta["interview_calibration"]
    assert isinstance(calibration, dict)
    assert calibration["level"] == "foundational"
    assert calibration["unknown_terms"] == unknown_terms
    assert result.value.meta["pending_question_preserved"] is True
    assert result.value.meta["question_rephrased"] is True
    assert result.value.meta["pending_question"] == state.rounds[0].question
    assert "돈이 두 번 빠지지" in result.value.text_content
    reloaded = await engine.load_state(state.interview_id)
    assert reloaded.is_ok
    assert len(reloaded.value.rounds) == 1
    assert reloaded.value.rounds[0].user_response is None


@pytest.mark.parametrize(
    "evidence", ["familiar with OAuth", "some experience with OAuth", "comfortable with Python"]
)
async def test_bare_idk_preserves_positive_familiarity(evidence) -> None:
    resolved = SkillDispatchRouter().resolve(
        f"ooo idk {evidence}",
        skills_dir=Path(__file__).resolve().parents[4] / "skills",
    )
    assert isinstance(resolved, Resolved)

    result = await InterviewHandler().handle(resolved.mcp_args)

    assert result.is_ok
    calibration = result.value.meta["interview_calibration"]
    assert isinstance(calibration, dict)
    assert calibration["level"] == "working"
    assert calibration["unknown_terms"] == []


@pytest.mark.parametrize("failure", [RuntimeError("provider unavailable"), TimeoutError("timeout")])
@pytest.mark.parametrize("owns_event_store", [False, True])
async def test_rephrase_exception_preserves_calibration_and_pending_state(
    tmp_path, failure, owns_event_store
) -> None:
    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path)
    state = InterviewState(
        interview_id="interview_1234567890abcdef",
        initial_context="payments",
        rounds=[InterviewRound(round_number=1, question="What idempotency guarantee is required?")],
    )
    assert (await engine.save_state(state)).is_ok
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    engine.rephrase_pending_question = AsyncMock(side_effect=failure)
    handler = MagicMock()
    handler._create_interview_engine.return_value = (engine, None)
    handler._owns_event_store = owns_event_store
    handler.close = AsyncMock()

    result = await handle_interview_calibration_turn(
        handler, "I do not know idempotency", session_id=state.interview_id
    )

    assert result.is_ok and not result.value.is_error
    assert "plainer language" not in result.value.text_content
    assert "pending question is unchanged" in result.value.text_content
    assert state.rounds[0].question in result.value.text_content
    assert result.value.meta["question_rephrased"] is False
    assert result.value.meta["pending_question_preserved"] is True
    assert result.value.meta["calibration_updated"] is True
    assert result.value.meta["interview_calibration"]["unknown_terms"] == ["idempotency"]
    assert result.value.meta["session_id"] == state.interview_id
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert handler.close.await_count == int(owns_event_store)


async def test_rephrase_cancellation_propagates_and_closes_owned_store() -> None:
    engine = MagicMock()
    engine.load_state = AsyncMock(
        return_value=Result.ok(
            InterviewState(
                interview_id="interview_1234567890abcdef",
                initial_context="payments",
                rounds=[InterviewRound(round_number=1, question="What idempotency guarantee?")],
            )
        )
    )
    engine.rephrase_pending_question = AsyncMock(side_effect=asyncio.CancelledError)
    handler = MagicMock()
    handler._create_interview_engine.return_value = (engine, None)
    handler._owns_event_store = True
    handler.close = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await handle_interview_calibration_turn(
            handler, "I do not know idempotency", session_id="interview_1234567890abcdef"
        )
    handler.close.assert_awaited_once()


class _LegacyEngine:
    """Fake engine with old one-argument ask_next_question contract."""

    async def ask_next_question(self, state: Any) -> Result[str, Any]:
        return Result.ok("What is the primary goal?")


class _CalibrationAwareEngine:
    """Fake engine that accepts language_calibration keyword."""

    async def ask_next_question(
        self,
        state: Any,
        *,
        language_calibration: Any | None = None,
    ) -> Result[str, Any]:
        return Result.ok("What is the primary goal?")


def test_engine_supports_calibration_detects_old_engine() -> None:
    engine = _LegacyEngine()
    assert _engine_supports_calibration(engine) is False


def test_engine_supports_calibration_detects_new_engine() -> None:
    engine = _CalibrationAwareEngine()
    assert _engine_supports_calibration(engine) is True


@pytest.mark.asyncio
async def test_ask_next_question_safe_with_legacy_engine() -> None:
    from ouroboros.mcp.tools.interview_calibration import _ask_next_question

    engine = _LegacyEngine()
    calibration = infer_interview_calibration("I do not know idempotency")
    # Must not raise TypeError about unexpected keyword argument
    result = await _ask_next_question(engine, "fake_state", calibration)
    assert result.is_ok


@pytest.mark.asyncio
async def test_ask_next_question_passes_calibration_to_aware_engine() -> None:
    from ouroboros.mcp.tools.interview_calibration import _ask_next_question

    engine = _CalibrationAwareEngine()
    calibration = infer_interview_calibration("I do not know event sourcing")
    result = await _ask_next_question(engine, "fake_state", calibration)
    assert result.is_ok


@pytest.mark.asyncio
async def test_ask_next_question_no_calibration_uses_simple_call() -> None:
    from ouroboros.mcp.tools.interview_calibration import _ask_next_question

    engine = _LegacyEngine()
    # None calibration should always work
    result = await _ask_next_question(engine, "fake_state", None)
    assert result.is_ok


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rephrase_result",
    [Result.err(Exception("provider error")), Result.ok(""), Result.ok(" \t\n")],
    ids=["provider-error", "empty", "whitespace"],
)
async def test_calibration_turn_rephrase_failure_is_truthful(rephrase_result) -> None:
    """When rephrasing fails, the response must not claim successful adaptation."""
    from ouroboros.mcp.tools.interview_calibration import (
        handle_interview_calibration_turn,
    )

    # Mock a handler with an engine that has a pending question but rephrase fails
    handler = AsyncMock()
    handler._owns_event_store = False

    # Create a fake state with a pending question
    mock_state = AsyncMock()
    mock_state.rounds = [
        AsyncMock(question="What idempotency guarantee is required?", user_response=None)
    ]
    mock_state.interview_id = "test-session"

    mock_engine = AsyncMock()
    mock_engine.load_state = AsyncMock(return_value=Result.ok(mock_state))
    mock_engine.rephrase_pending_question = AsyncMock(return_value=rephrase_result)
    handler._create_interview_engine = lambda: (mock_engine, None)

    result = await handle_interview_calibration_turn(
        handler,
        "I do not know idempotency",
        session_id="test-session",
    )
    assert result.is_ok
    text = result.value.content[0].text
    # Must NOT claim "plainer language" when rephrasing failed
    assert "plainer language" not in text
    # Must indicate rephrasing was not available
    assert "not available" in text.lower() or "unchanged" in text.lower()
    # Must show the original question
    assert "idempotency" in text
    # Meta must indicate rephrasing did not succeed
    assert result.value.meta["question_rephrased"] is False


@pytest.mark.asyncio
async def test_calibration_turn_rephrase_success_shows_plainer() -> None:
    """When rephrasing succeeds, the response should show the rephrased question."""
    from ouroboros.mcp.tools.interview_calibration import (
        handle_interview_calibration_turn,
    )

    handler = AsyncMock()
    handler._owns_event_store = False

    mock_state = AsyncMock()
    mock_state.rounds = [
        AsyncMock(question="What idempotency guarantee is required?", user_response=None)
    ]
    mock_state.interview_id = "test-session"

    mock_engine = AsyncMock()
    mock_engine.load_state = AsyncMock(return_value=Result.ok(mock_state))
    mock_engine.rephrase_pending_question = AsyncMock(
        return_value=Result.ok("Should the system prevent duplicate operations?")
    )
    handler._create_interview_engine = lambda: (mock_engine, None)

    result = await handle_interview_calibration_turn(
        handler,
        "I do not know idempotency",
        session_id="test-session",
    )
    assert result.is_ok
    text = result.value.content[0].text
    # Should claim successful adaptation
    assert "plainer language" in text
    assert "duplicate operations" in text
    assert result.value.meta["question_rephrased"] is True


class _LegacyEngineWithoutRephrase:
    """Fake engine that has load_state but NOT rephrase_pending_question."""

    async def load_state(self, session_id: str) -> Any:
        mock_state = AsyncMock()
        mock_state.rounds = [
            AsyncMock(question="What idempotency guarantee is required?", user_response=None)
        ]
        return Result.ok(mock_state)

    async def ask_next_question(self, state: Any) -> Any:
        return Result.ok("What is the primary goal?")


@pytest.mark.asyncio
async def test_calibration_turn_legacy_engine_without_rephrase_no_attributeerror() -> None:
    """Legacy engines without rephrase_pending_question must not raise AttributeError."""
    from ouroboros.mcp.tools.interview_calibration import (
        handle_interview_calibration_turn,
    )

    handler = AsyncMock()
    handler._owns_event_store = False

    # Create a fake state with a pending question
    mock_state = AsyncMock()
    mock_state.rounds = [
        AsyncMock(question="What idempotency guarantee is required?", user_response=None)
    ]

    engine = _LegacyEngineWithoutRephrase()
    # Patch load_state to return our controlled state
    engine.load_state = AsyncMock(return_value=Result.ok(mock_state))  # type: ignore[method-assign]
    handler._create_interview_engine = lambda: (engine, None)

    # This must NOT raise AttributeError
    result = await handle_interview_calibration_turn(
        handler,
        "I do not know idempotency",
        session_id="test-session",
    )
    assert result.is_ok
    text = result.value.content[0].text
    # Must indicate rephrasing was not available (truthful fallback)
    assert "not available" in text.lower() or "unchanged" in text.lower()
    # Must still show the pending question
    assert "idempotency" in text
    # Meta must correctly reflect that rephrase did not happen
    assert result.value.meta["question_rephrased"] is False
    assert result.value.meta["pending_question_preserved"] is True


@pytest.mark.asyncio
async def test_calibration_turn_engine_with_rephrase_still_works() -> None:
    """Engines that DO have rephrase_pending_question still work as before."""
    from ouroboros.mcp.tools.interview_calibration import (
        handle_interview_calibration_turn,
    )

    handler = AsyncMock()
    handler._owns_event_store = False

    mock_state = AsyncMock()
    mock_state.rounds = [
        AsyncMock(question="What idempotency guarantee is required?", user_response=None)
    ]

    mock_engine = AsyncMock()
    mock_engine.load_state = AsyncMock(return_value=Result.ok(mock_state))
    mock_engine.rephrase_pending_question = AsyncMock(
        return_value=Result.ok("Should the system prevent duplicate operations?")
    )
    handler._create_interview_engine = lambda: (mock_engine, None)

    result = await handle_interview_calibration_turn(
        handler,
        "I do not know idempotency",
        session_id="test-session",
    )
    assert result.is_ok
    text = result.value.content[0].text
    assert "plainer language" in text
    assert "duplicate operations" in text
    assert result.value.meta["question_rephrased"] is True


@pytest.mark.asyncio
async def test_calibration_turn_engine_rephrase_is_none_is_truthful() -> None:
    """When rephrase_pending_question is present but returns None, fallback is truthful."""
    from ouroboros.mcp.tools.interview_calibration import (
        handle_interview_calibration_turn,
    )

    handler = AsyncMock()
    handler._owns_event_store = False

    mock_state = AsyncMock()
    mock_state.rounds = [
        AsyncMock(question="What PKCE flow variant do you need?", user_response=None)
    ]

    mock_engine = AsyncMock()
    mock_engine.load_state = AsyncMock(return_value=Result.ok(mock_state))
    # Return Ok(None) — rephrase didn't produce content
    mock_engine.rephrase_pending_question = AsyncMock(return_value=Result.ok(None))
    handler._create_interview_engine = lambda: (mock_engine, None)

    result = await handle_interview_calibration_turn(
        handler,
        "I cannot explain PKCE",
        session_id="test-session",
    )
    assert result.is_ok
    text = result.value.content[0].text
    # Should show "not available" or "unchanged" because rephrase returned None
    assert "not available" in text.lower() or "unchanged" in text.lower()
    assert result.value.meta["question_rephrased"] is False
