"""Round 2 regression tests for PR #1843 blocker fixes.

These tests verify:
1. Legacy engines lacking rephrase_pending_question get a truthful fallback
   instead of raising AttributeError.
2. Packaged idk/interview skills explicitly relay meta.interview_calibration
   into subsequent interview calls for the full idk→answer→next sequence.
3. Deterministic inference aligns with public SKILL.md examples:
   - "cannot explain PKCE" → foundational level, PKCE as unknown term
   - "networking and operators are unfamiliar" → foundational level, terms extracted
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.types import Result
from ouroboros.interview_calibration import infer_interview_calibration

# ─────────────────────────────────────────────────────────────────────────────
# Blocker 1: Legacy engine capability-check for rephrase_pending_question
# ─────────────────────────────────────────────────────────────────────────────


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
    from ouroboros.mcp.tools.interview_calibration import handle_interview_calibration_turn

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
    from ouroboros.mcp.tools.interview_calibration import handle_interview_calibration_turn

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
    from ouroboros.mcp.tools.interview_calibration import handle_interview_calibration_turn

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


def test_claude_plugin_idk_skill_instructs_calibration_relay() -> None:
    test_file = Path(__file__).resolve()
    repo_root = test_file.parent
    while repo_root != repo_root.parent:
        if (repo_root / "pyproject.toml").exists():
            break
        repo_root = repo_root.parent
    skill_path = repo_root / "skills" / "idk" / "SKILL.md"
    content = skill_path.read_text()
    assert "meta.interview_calibration" in content
    assert "interview_calibration" in content
    assert "subsequent" in content.lower()


def test_claude_plugin_interview_skill_instructs_calibration_relay() -> None:
    test_file = Path(__file__).resolve()
    repo_root = test_file.parent
    while repo_root != repo_root.parent:
        if (repo_root / "pyproject.toml").exists():
            break
        repo_root = repo_root.parent
    skill_path = repo_root / "skills" / "interview" / "SKILL.md"
    content = skill_path.read_text()
    assert "interview_calibration" in content
    assert "interview_calibration" in content and "argument" in content.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Blocker 3: Deterministic inference alignment with public SKILL.md examples
# ─────────────────────────────────────────────────────────────────────────────


def test_cannot_explain_pkce_infers_foundational() -> None:
    """'cannot explain PKCE' must trigger foundational level.

    Public SKILL.md example:
      ooo idk OAuth is familiar enough to implement, but I cannot explain PKCE.
    """
    calibration = infer_interview_calibration(
        "OAuth is familiar enough to implement, but I cannot explain PKCE"
    )
    assert calibration.level == "foundational"
    # PKCE should be extracted as an unknown term
    assert any("PKCE" in term for term in calibration.unknown_terms)


def test_cant_explain_shortform_infers_foundational() -> None:
    """'can't explain' contraction also triggers foundational level."""
    calibration = infer_interview_calibration("I can't explain how OAuth refresh tokens work")
    assert calibration.level == "foundational"
    assert any("OAuth" in term or "refresh" in term for term in calibration.unknown_terms)


def test_networking_and_operators_unfamiliar_infers_foundational() -> None:
    """'networking and operators are unfamiliar' must trigger foundational.

    Public SKILL.md example:
      ooo idk Kubernetes: deployed a tutorial once; networking and operators are unfamiliar.
    """
    calibration = infer_interview_calibration(
        "Kubernetes: deployed a tutorial once; networking and operators are unfamiliar"
    )
    assert calibration.level == "foundational"
    # The terms should include networking and/or operators
    terms_lower = [t.casefold() for t in calibration.unknown_terms]
    assert any("networking" in t for t in terms_lower) or any(
        "operators" in t for t in terms_lower
    ), f"Expected networking/operators in unknown_terms, got: {calibration.unknown_terms}"


def test_unfamiliar_with_still_works() -> None:
    """'unfamiliar with X' pattern (pre-existing) must continue to work."""
    calibration = infer_interview_calibration("I am unfamiliar with event sourcing and CQRS")
    assert calibration.level == "foundational"
    assert any("event sourcing" in t for t in calibration.unknown_terms) or any(
        "CQRS" in t for t in calibration.unknown_terms
    )


def test_cannot_explain_extracts_term() -> None:
    """'cannot explain X' should extract X as an unknown term."""
    calibration = infer_interview_calibration("I cannot explain PKCE")
    assert "PKCE" in calibration.unknown_terms


def test_mixed_known_unknown_high_confidence() -> None:
    """Mixed known + unknown evidence results in high confidence.

    Public example: 'I do not know idempotency or event sourcing. I have built REST APIs.'
    """
    calibration = infer_interview_calibration(
        "I do not know idempotency or event sourcing. I have built REST APIs."
    )
    assert calibration.level == "foundational"
    assert calibration.confidence == "high"
    assert "idempotency" in calibration.unknown_terms or any(
        "idempotency" in t for t in calibration.unknown_terms
    )


def test_calibration_meta_is_serializable_for_relay() -> None:
    """The calibration object serializes cleanly for meta transport."""
    calibration = infer_interview_calibration(
        "Kubernetes: deployed a tutorial once; networking and operators are unfamiliar"
    )
    dumped = calibration.model_dump(mode="json")
    assert isinstance(dumped, dict)
    assert dumped["level"] == "foundational"
    assert isinstance(dumped["unknown_terms"], list)
    # Verify round-trip via normalize
    from ouroboros.interview_calibration import normalize_interview_calibration

    restored = normalize_interview_calibration(dumped)
    assert restored is not None
    assert restored.level == calibration.level
    assert restored.unknown_terms == calibration.unknown_terms
