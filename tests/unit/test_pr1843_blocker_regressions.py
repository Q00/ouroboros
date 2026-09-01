"""Regression tests for PR #1843 blocker fixes.

These tests verify:
1. Engine capability negotiation — engines without language_calibration support
   are not broken by unconditional forwarding.
2. PM composition compatibility — PMInterviewEngine works with calibration.
3. Non-persistence of calibration — calibration is stripped from persisted dicts.
4. Truthful rephrase fallback — failed rephrasing does not claim success.
5. Packaged skill surface — skills/idk/SKILL.md exists.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.types import Result
from ouroboros.interview_calibration import infer_interview_calibration
from ouroboros.mcp.tools.authoring_handlers import _engine_supports_calibration
from ouroboros.orchestrator.adapter import RuntimeHandle

# ─────────────────────────────────────────────────────────────────────────────
# Blocker #1: Engine capability negotiation
# ─────────────────────────────────────────────────────────────────────────────


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
    from ouroboros.mcp.tools.authoring_handlers import _ask_next_question

    engine = _LegacyEngine()
    calibration = infer_interview_calibration("I do not know idempotency")
    # Must not raise TypeError about unexpected keyword argument
    result = await _ask_next_question(engine, "fake_state", calibration)
    assert result.is_ok


@pytest.mark.asyncio
async def test_ask_next_question_passes_calibration_to_aware_engine() -> None:
    from ouroboros.mcp.tools.authoring_handlers import _ask_next_question

    engine = _CalibrationAwareEngine()
    calibration = infer_interview_calibration("I do not know event sourcing")
    result = await _ask_next_question(engine, "fake_state", calibration)
    assert result.is_ok


@pytest.mark.asyncio
async def test_ask_next_question_no_calibration_uses_simple_call() -> None:
    from ouroboros.mcp.tools.authoring_handlers import _ask_next_question

    engine = _LegacyEngine()
    # None calibration should always work
    result = await _ask_next_question(engine, "fake_state", None)
    assert result.is_ok


# ─────────────────────────────────────────────────────────────────────────────
# Blocker #2: PM composition compatibility
# ─────────────────────────────────────────────────────────────────────────────


def test_pm_build_system_prompt_accepts_language_calibration_kwarg() -> None:
    """The PM steering wrapper must accept **kwargs for forward compatibility."""
    from ouroboros.bigbang.pm_interview import PMInterviewEngine
    from ouroboros.providers.base import LLMAdapter

    # Create a minimal PM engine to install steering
    adapter = AsyncMock(spec=LLMAdapter)
    engine = PMInterviewEngine.create(llm_adapter=adapter)
    engine._install_pm_steering()

    # The monkey-patched _build_system_prompt should accept language_calibration
    sig = inspect.signature(engine.inner._build_system_prompt)
    # Must have **kwargs or explicit language_calibration parameter
    has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    has_explicit = "language_calibration" in sig.parameters
    assert has_var_keyword or has_explicit, (
        f"PM _build_system_prompt must accept language_calibration; params: {list(sig.parameters)}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Blocker #3: Non-persistence of calibration
# ─────────────────────────────────────────────────────────────────────────────


def test_runtime_handle_to_persisted_dict_strips_calibration_non_opencode() -> None:
    """Calibration metadata must not be persisted on any runtime."""
    from ouroboros.orchestrator.interview_session import INTERVIEW_CALIBRATION_METADATA_KEY

    handle = RuntimeHandle(
        backend="codex",
        cwd="/tmp/test",
        metadata={
            INTERVIEW_CALIBRATION_METADATA_KEY: {
                "level": "foundational",
                "confidence": "medium",
                "evidence": "I do not know event sourcing",
            },
            "ouroboros_interview_session_id": "session-123",
        },
    )
    persisted = handle.to_persisted_dict()
    assert INTERVIEW_CALIBRATION_METADATA_KEY not in persisted["metadata"]
    # Session ID should still be present
    assert persisted["metadata"]["ouroboros_interview_session_id"] == "session-123"


def test_runtime_handle_to_persisted_dict_strips_calibration_opencode() -> None:
    """OpenCode already filters to allowed keys — calibration must not sneak in."""
    from ouroboros.orchestrator.interview_session import INTERVIEW_CALIBRATION_METADATA_KEY

    handle = RuntimeHandle(
        backend="opencode",
        cwd="/tmp/test",
        metadata={
            INTERVIEW_CALIBRATION_METADATA_KEY: {
                "level": "foundational",
                "confidence": "medium",
                "evidence": "I do not know event sourcing",
            },
        },
    )
    persisted = handle.to_persisted_dict()
    assert INTERVIEW_CALIBRATION_METADATA_KEY not in persisted["metadata"]


# ─────────────────────────────────────────────────────────────────────────────
# Blocker #6: Truthful rephrase fallback
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calibration_turn_rephrase_failure_is_truthful() -> None:
    """When rephrasing fails, the response must not claim successful adaptation."""
    from ouroboros.mcp.tools.interview_calibration import handle_interview_calibration_turn

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
    # Rephrase returns an error result
    mock_engine.rephrase_pending_question = AsyncMock(
        return_value=Result.err(Exception("provider error"))
    )
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
    from ouroboros.mcp.tools.interview_calibration import handle_interview_calibration_turn

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


# ─────────────────────────────────────────────────────────────────────────────
# Blocker #5: Claude plugin skill surface
# ─────────────────────────────────────────────────────────────────────────────


def test_claude_plugin_idk_skill_exists() -> None:
    test_file = Path(__file__).resolve()
    repo_root = test_file.parent
    while repo_root != repo_root.parent:
        if (repo_root / "pyproject.toml").exists():
            break
        repo_root = repo_root.parent
    skill_path = repo_root / "skills" / "idk" / "SKILL.md"
    assert skill_path.exists(), f"Missing: {skill_path}"
    content = skill_path.read_text()
    assert "mcp_tool: ouroboros_interview" in content
    assert "calibration_input" in content
