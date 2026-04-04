"""Tests for [from-research] prefix in interview system prompt.

Verifies that the system prompt always includes all three answer prefix
hints ([from-code], [from-user], [from-research]) regardless of whether
the interview is brownfield or greenfield.

See: https://github.com/Q00/ouroboros/issues/287
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ouroboros.bigbang.interview import (
    InterviewEngine,
    InterviewState,
    InterviewStatus,
)


def _make_engine() -> InterviewEngine:
    return InterviewEngine(
        llm_adapter=MagicMock(),
        state_dir=MagicMock(),
        model="test-model",
    )


class TestResearchPrefixInSystemPrompt:
    """[from-research] prefix must appear in the system prompt."""

    def test_greenfield_includes_all_prefixes(self) -> None:
        """Greenfield interviews include all three answer prefixes."""
        engine = _make_engine()
        state = InterviewState(
            interview_id="test-001",
            initial_context="Build an app",
            status=InterviewStatus.IN_PROGRESS,
            is_brownfield=False,
        )

        prompt = engine._build_system_prompt(state)

        assert "[from-code]" in prompt
        assert "[from-user]" in prompt
        assert "[from-research]" in prompt

    def test_brownfield_includes_all_prefixes(self) -> None:
        """Brownfield interviews also include all three answer prefixes."""
        engine = _make_engine()
        state = InterviewState(
            interview_id="test-002",
            initial_context="Add feature to existing app",
            status=InterviewStatus.IN_PROGRESS,
            is_brownfield=True,
        )

        prompt = engine._build_system_prompt(state)

        assert "[from-code]" in prompt
        assert "[from-user]" in prompt
        assert "[from-research]" in prompt
        assert "BROWNFIELD" in prompt

    def test_research_prefix_describes_external_sources(self) -> None:
        """The [from-research] hint mentions external information sources."""
        engine = _make_engine()
        state = InterviewState(
            interview_id="test-003",
            initial_context="Build an app",
            status=InterviewStatus.IN_PROGRESS,
        )

        prompt = engine._build_system_prompt(state)

        # The hint should describe what [from-research] means
        assert "externally researched" in prompt.lower() or "API docs" in prompt
