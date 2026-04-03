"""Regression tests for sanitized MCP tool errors with server-side traceback logging.

Issue #289: unexpected QA/evaluate crashes should log tracebacks for diagnosis
without surfacing internal stack frames to MCP callers.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ouroboros.mcp.tools.evaluation_handlers import EvaluateHandler
from ouroboros.mcp.tools.qa import QAHandler


@pytest.mark.asyncio
async def test_qa_handler_sanitizes_unexpected_exception_and_logs_traceback() -> None:
    """QA handler should keep traceback details in logs, not public error text."""
    handler = QAHandler()

    with (
        patch(
            "ouroboros.mcp.tools.qa.create_llm_adapter",
            side_effect=RuntimeError("cannot assign to field 'content'"),
        ),
        patch("ouroboros.mcp.tools.qa.log.error") as mock_log_error,
    ):
        result = await handler.handle(
            {
                "artifact": "print('hi')",
                "quality_bar": "Output should be valid Python.",
            }
        )

    assert result.is_err
    error_text = str(result.error)
    assert (
        error_text
        == "QA evaluation failed due to an internal error. Check server logs for details."
    )
    assert "Traceback:" not in error_text
    assert "cannot assign to field 'content'" not in error_text
    assert "RuntimeError" not in error_text

    mock_log_error.assert_called_once()
    _, kwargs = mock_log_error.call_args
    assert kwargs["error"] == "cannot assign to field 'content'"
    assert "Traceback" in kwargs["traceback"]
    assert "RuntimeError: cannot assign to field 'content'" in kwargs["traceback"]


@pytest.mark.asyncio
async def test_evaluate_handler_sanitizes_unexpected_exception_and_logs_traceback() -> None:
    """Evaluate handler should keep traceback details in logs, not public error text."""
    handler = EvaluateHandler()

    with (
        patch(
            "ouroboros.mcp.tools.evaluation_handlers.create_llm_adapter",
            side_effect=RuntimeError("cannot assign to field 'content'"),
        ),
        patch("ouroboros.mcp.tools.evaluation_handlers.log.error") as mock_log_error,
    ):
        result = await handler.handle(
            {
                "session_id": "sess-289",
                "artifact": "stub artifact",
            }
        )

    assert result.is_err
    error_text = str(result.error)
    assert (
        error_text
        == "Evaluation failed due to an internal error. Check server logs for details."
    )
    assert "Traceback:" not in error_text
    assert "cannot assign to field 'content'" not in error_text
    assert "RuntimeError" not in error_text

    mock_log_error.assert_called_once()
    _, kwargs = mock_log_error.call_args
    assert kwargs["error"] == "cannot assign to field 'content'"
    assert "Traceback" in kwargs["traceback"]
    assert "RuntimeError: cannot assign to field 'content'" in kwargs["traceback"]
