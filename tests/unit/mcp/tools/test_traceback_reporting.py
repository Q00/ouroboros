"""Regression tests for sanitized MCP tool errors with server-side traceback logging.

Issue #289: unexpected QA/evaluate crashes should log tracebacks for diagnosis
without surfacing internal stack frames to MCP callers.

Follow-up from PR #308 review: configuration errors (ValueError, RuntimeError)
should remain actionable, while truly unexpected errors are sanitized.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ouroboros.mcp.tools.evaluation_handlers import EvaluateHandler
from ouroboros.mcp.tools.qa import QAHandler

# ---------------------------------------------------------------------------
# QA Handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_handler_surfaces_config_error() -> None:
    """ValueError/RuntimeError from adapter creation should be user-visible."""
    handler = QAHandler()

    with patch(
        "ouroboros.mcp.tools.qa.create_llm_adapter",
        side_effect=ValueError("Unsupported LLM backend: foobar"),
    ):
        result = await handler.handle(
            {
                "artifact": "print('hi')",
                "quality_bar": "Output should be valid Python.",
            }
        )

    assert result.is_err
    error_text = str(result.error)
    # Config errors are actionable — the original message must be preserved.
    assert "Unsupported LLM backend: foobar" in error_text


@pytest.mark.asyncio
async def test_qa_handler_sanitizes_unexpected_exception() -> None:
    """Truly unexpected exceptions must NOT leak internals to MCP callers."""
    handler = QAHandler()

    with (
        patch(
            "ouroboros.mcp.tools.qa.create_llm_adapter",
            side_effect=TypeError("cannot assign to field 'content'"),
        ),
        patch("ouroboros.mcp.tools.qa.log") as mock_log,
    ):
        result = await handler.handle(
            {
                "artifact": "print('hi')",
                "quality_bar": "Output should be valid Python.",
            }
        )

    assert result.is_err
    error_text = str(result.error)

    # Internal details must NOT appear in client-visible error text.
    assert "cannot assign to field" not in error_text
    assert "TypeError" not in error_text
    # Traceback should be logged server-side via log.exception.
    mock_log.exception.assert_called_once()


# ---------------------------------------------------------------------------
# Evaluate Handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_handler_surfaces_config_error() -> None:
    """ValueError/RuntimeError from adapter creation should be user-visible."""
    handler = EvaluateHandler()

    with patch(
        "ouroboros.mcp.tools.evaluation_handlers.create_llm_adapter",
        side_effect=RuntimeError("litellm backend requested but litellm is not installed."),
    ):
        result = await handler.handle(
            {
                "session_id": "sess-289",
                "artifact": "stub artifact",
            }
        )

    assert result.is_err
    error_text = str(result.error)
    assert "litellm is not installed" in error_text


@pytest.mark.asyncio
async def test_evaluate_handler_sanitizes_unexpected_exception() -> None:
    """Truly unexpected exceptions must NOT leak internals to MCP callers."""
    handler = EvaluateHandler()

    with (
        patch(
            "ouroboros.mcp.tools.evaluation_handlers.create_llm_adapter",
            side_effect=TypeError("cannot assign to field 'content'"),
        ),
        patch("ouroboros.mcp.tools.evaluation_handlers.log") as mock_log,
    ):
        result = await handler.handle(
            {
                "session_id": "sess-289",
                "artifact": "stub artifact",
            }
        )

    assert result.is_err
    error_text = str(result.error)

    assert "cannot assign to field" not in error_text
    assert "TypeError" not in error_text
    mock_log.exception.assert_called_once()
