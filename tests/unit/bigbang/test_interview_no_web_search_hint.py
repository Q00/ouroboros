"""Tests for dead OUROBOROS_WEB_SEARCH_TOOL removal.

Regression coverage verifying that the system prompt no longer injects
a web search hint that the interview LLM cannot use (allowed_tools=[]).

See: https://github.com/Q00/ouroboros/issues/285
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from ouroboros.bigbang.interview import (
    InterviewEngine,
    InterviewState,
    InterviewStatus,
)


def _make_engine() -> InterviewEngine:
    """Create an InterviewEngine with mocked adapter."""
    return InterviewEngine(
        llm_adapter=MagicMock(),
        state_dir=MagicMock(),
        model="test-model",
    )


def _make_state() -> InterviewState:
    """Create a minimal InterviewState."""
    return InterviewState(
        interview_id="test-001",
        initial_context="Build an app",
        status=InterviewStatus.IN_PROGRESS,
    )


class TestWebSearchHintRemoved:
    """OUROBOROS_WEB_SEARCH_TOOL hint must NOT appear in system prompts."""

    def test_system_prompt_has_no_web_search_hint(self) -> None:
        """System prompt does not contain web search tool references."""
        engine = _make_engine()
        state = _make_state()

        with patch.dict(os.environ, {"OUROBOROS_WEB_SEARCH_TOOL": "mcp__tavily__search"}):
            prompt = engine._build_system_prompt(state)

        assert "mcp__tavily__search" not in prompt
        assert "PREFERRED: Use" not in prompt
        assert (
            "web search" not in prompt.lower().split("tool usage")[0]
            if "tool usage" in prompt.lower()
            else True
        )

    def test_system_prompt_without_env_var(self) -> None:
        """System prompt works normally without the env var."""
        engine = _make_engine()
        state = _make_state()

        # Ensure env var is NOT set
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OUROBOROS_WEB_SEARCH_TOOL", None)
            prompt = engine._build_system_prompt(state)

        assert "expert requirements engineer" in prompt
        assert "Round 1" in prompt

    def test_env_var_not_read(self) -> None:
        """The env var OUROBOROS_WEB_SEARCH_TOOL is no longer read."""
        engine = _make_engine()
        state = _make_state()

        with patch.dict(os.environ, {"OUROBOROS_WEB_SEARCH_TOOL": "should_not_appear"}):
            prompt = engine._build_system_prompt(state)

        assert "should_not_appear" not in prompt
