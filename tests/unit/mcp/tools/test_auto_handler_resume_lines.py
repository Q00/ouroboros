"""Resume-hint rendering for the MCP ``ouroboros_auto`` surface (#688).

These tests assert that :func:`ouroboros.mcp.tools.auto_handler._format_result`
emits the same capability-driven hint substrings as the CLI, but without Rich
markup since MCP renders plain text.
"""

from __future__ import annotations

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.mcp.tools.auto_handler import _format_result


def _result(
    capability: str, *, status: str = "blocked", session_id: str = "auto_mcp"
) -> AutoPipelineResult:
    return AutoPipelineResult(
        status=status,
        auto_session_id=session_id,
        phase=status,
        resume_capability=capability,
    )


def test_format_result_resume_capability_resume_emits_resume_line() -> None:
    output = _format_result(_result("resume", status="complete"))

    assert "Resume: ooo auto --resume auto_mcp" in output
    assert "Resume (partial)" not in output
    assert "Retry:" not in output
    assert "Start fresh" not in output


def test_format_result_resume_capability_partial_emits_partial_resume_line() -> None:
    output = _format_result(_result("partial_resume"))

    assert "Resume (partial): ooo auto --resume auto_mcp" in output
    assert "some progress preserved but the exact pick-up point may be approximate" in output


def test_format_result_resume_capability_retry_emits_retry_line() -> None:
    output = _format_result(_result("retry"))

    assert "Retry: ooo auto --resume auto_mcp" in output
    assert "no prior session context" in output
    assert "re-runs the failed step from scratch" in output


def test_format_result_resume_capability_none_emits_no_resume_line() -> None:
    """``_format_result`` has no ``state.goal`` so NONE prints nothing."""
    output = _format_result(_result("none", status="complete"))

    assert "Resume:" not in output
    assert "Resume (partial)" not in output
    assert "Retry:" not in output
    assert "Start fresh" not in output
