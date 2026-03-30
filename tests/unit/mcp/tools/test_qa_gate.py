"""Tests for QA handler AgentMode (State/Agent layer separation).

Verifies:
1. Default (native) mode: requires agent_verdict, no LLM call
2. Native mode without agent_verdict: returns error
3. agent_verdict parameter: parses pre-computed verdict without LLM call
4. agent_verdict with invalid JSON: returns error
5. Native mode with agent_verdict: parses correctly
6. Internal mode (opt-in): calls LLM internally
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.layers.gate import AgentMode
from ouroboros.mcp.tools.qa import QAHandler


VALID_VERDICT_JSON = json.dumps({
    "score": 0.85,
    "verdict": "pass",
    "dimensions": {"correctness": 0.9, "style": 0.8},
    "differences": ["minor formatting issue"],
    "suggestions": ["add docstrings"],
    "reasoning": "Overall good quality code.",
})

INVALID_VERDICT = "this is not json"

BASE_ARGS = {
    "artifact": "def foo(): return 42",
    "quality_bar": "All functions must have docstrings",
    "artifact_type": "code",
}


class TestQAAgentMode:
    """Test QA handler agent mode behavior."""

    async def test_agent_verdict_skips_llm(self) -> None:
        """When agent_verdict is provided, no LLM call is made."""
        handler = QAHandler()

        result = await handler.handle({
            **BASE_ARGS,
            "agent_verdict": VALID_VERDICT_JSON,
        })

        assert result.is_ok
        meta = result.value.meta
        assert meta["score"] == 0.85
        assert meta["verdict"] == "pass"
        assert meta["passed"] is True
        assert meta["dimensions"] == {"correctness": 0.9, "style": 0.8}
        assert "minor formatting issue" in meta["differences"]
        assert "add docstrings" in meta["suggestions"]

    async def test_agent_verdict_invalid_returns_error(self) -> None:
        """Invalid agent_verdict returns parse error."""
        handler = QAHandler()

        result = await handler.handle({
            **BASE_ARGS,
            "agent_verdict": INVALID_VERDICT,
        })

        assert result.is_err
        assert "Failed to parse agent verdict" in str(result.error)

    async def test_native_mode_without_verdict_returns_error(self) -> None:
        """In native mode without agent_verdict, returns error."""
        handler = QAHandler(agent_mode=AgentMode.NATIVE)

        result = await handler.handle(BASE_ARGS)

        assert result.is_err
        assert "agent_verdict is required in native mode" in str(result.error)

    async def test_native_mode_with_verdict_processes_normally(self) -> None:
        """In native mode, agent_verdict is processed directly."""
        handler = QAHandler(agent_mode=AgentMode.NATIVE)

        result = await handler.handle({
            **BASE_ARGS,
            "agent_verdict": VALID_VERDICT_JSON,
        })

        assert result.is_ok
        assert result.value.meta["score"] == 0.85

    async def test_internal_mode_calls_llm(self) -> None:
        """In internal mode (opt-in via OUROBOROS_AGENT_MODE=internal), LLM is called."""
        handler = QAHandler(agent_mode=AgentMode.INTERNAL)

        mock_response = AsyncMock()
        mock_response.content = VALID_VERDICT_JSON

        mock_adapter = AsyncMock()
        mock_adapter.complete = AsyncMock(return_value=Result.ok(mock_response))

        handler.llm_adapter = mock_adapter

        with patch("ouroboros.mcp.tools.qa._get_qa_system_prompt", return_value="system"):
            result = await handler.handle(BASE_ARGS)

        assert result.is_ok
        mock_adapter.complete.assert_awaited_once()
        assert result.value.meta["score"] == 0.85

    async def test_env_agent_mode_native(self) -> None:
        """OUROBOROS_AGENT_MODE=native activates native mode."""
        handler = QAHandler()  # No explicit agent_mode

        with patch.dict("os.environ", {"OUROBOROS_AGENT_MODE": "native"}):
            result = await handler.handle(BASE_ARGS)

        assert result.is_err
        assert "agent_verdict is required in native mode" in str(result.error)

    async def test_default_mode_is_native(self) -> None:
        """Default agent mode is native — requires agent_verdict, no LLM call."""
        handler = QAHandler()

        result = await handler.handle(BASE_ARGS)

        assert result.is_err
        assert "agent_verdict is required in native mode" in str(result.error)

    async def test_agent_verdict_preserves_session_id(self) -> None:
        """agent_verdict path preserves qa_session_id."""
        handler = QAHandler()

        result = await handler.handle({
            **BASE_ARGS,
            "agent_verdict": VALID_VERDICT_JSON,
            "qa_session_id": "my-session-123",
        })

        assert result.is_ok
        assert result.value.meta["qa_session_id"] == "my-session-123"

    async def test_agent_verdict_respects_iteration_history(self) -> None:
        """agent_verdict correctly computes iteration number from history."""
        handler = QAHandler()

        history = [
            {"iteration": 1, "score": 0.5, "verdict": "revise"},
            {"iteration": 2, "score": 0.7, "verdict": "revise"},
        ]

        result = await handler.handle({
            **BASE_ARGS,
            "agent_verdict": VALID_VERDICT_JSON,
            "iteration_history": history,
        })

        assert result.is_ok
        assert result.value.meta["iteration"] == 3
