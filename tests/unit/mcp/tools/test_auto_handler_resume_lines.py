"""Resume-hint rendering for the MCP ``ouroboros_auto`` surface (#688).

These tests assert that :func:`ouroboros.mcp.tools.auto_handler._format_result`
emits the same capability-driven hint substrings as the CLI, but without Rich
markup since MCP renders plain text. They also pin the metadata contract so
that ``meta.resume_command`` only appears when the rendered text invites the
client to resume.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.state import AutoResumeCapability
from ouroboros.mcp.tools.auto_handler import AutoHandler, _format_result


def _result(
    capability: AutoResumeCapability | str,
    *,
    status: str = "blocked",
    session_id: str = "auto_mcp",
) -> AutoPipelineResult:
    cap = (
        capability
        if isinstance(capability, AutoResumeCapability)
        else AutoResumeCapability(capability)
    )
    return AutoPipelineResult(
        status=status,
        auto_session_id=session_id,
        phase=status,
        resume_capability=cap,
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


# --- meta gating (#688 bot follow-up) ---------------------------------------


class _StubAutoHandler(AutoHandler):
    """Test double that bypasses pipeline construction."""

    def __init__(self, fixture: AutoPipelineResult) -> None:
        super().__init__()
        self._fixture = fixture

    async def _run(self, arguments: dict[str, Any]) -> AutoPipelineResult:  # type: ignore[override]
        return self._fixture


def _invoke(handler: AutoHandler) -> dict[str, Any]:
    result = asyncio.run(handler.handle({}))
    assert result.is_ok, result.error
    return dict(result.value.meta)


def test_handle_meta_includes_resume_capability_field() -> None:
    handler = _StubAutoHandler(_result("retry"))
    meta = _invoke(handler)
    assert meta["resume_capability"] == "retry"


def test_handle_meta_resume_command_gated_on_capability_resume() -> None:
    handler = _StubAutoHandler(_result("resume"))
    meta = _invoke(handler)
    assert meta["resume_capability"] == "resume"
    assert meta["resume_command"] == "ooo auto --resume auto_mcp"


def test_handle_meta_resume_command_omitted_for_capability_none() -> None:
    """Bot blocking finding (#724): NONE-capability sessions must not advertise
    a resume_command in MCP metadata, otherwise clients will route users into
    a guaranteed-failing ``--resume`` call."""
    handler = _StubAutoHandler(_result("none", status="complete"))
    meta = _invoke(handler)
    assert meta["resume_capability"] == "none"
    assert "resume_command" not in meta


def test_handle_meta_resume_command_present_for_partial_resume() -> None:
    handler = _StubAutoHandler(_result("partial_resume"))
    meta = _invoke(handler)
    assert meta["resume_capability"] == "partial_resume"
    assert meta["resume_command"] == "ooo auto --resume auto_mcp"
