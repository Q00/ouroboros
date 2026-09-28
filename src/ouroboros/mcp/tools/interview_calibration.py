"""MCP boundary helpers for session-local interview language calibration."""

from __future__ import annotations

from typing import Any

from ouroboros.core.types import Result
from ouroboros.interview_calibration import infer_interview_calibration
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)


def interview_calibration_parameters() -> tuple[MCPToolParameter, ...]:
    """Return the two arguments that extend the interview tool contract."""
    return (
        MCPToolParameter(
            name="calibration_input",
            type=ToolInputType.STRING,
            description=(
                "User-reported topic knowledge for session-local interview wording. "
                "This does not answer a pending question."
            ),
            required=False,
        ),
        MCPToolParameter(
            name="interview_calibration",
            type=ToolInputType.OBJECT,
            description=(
                "Validated session-local language calibration transported by the runtime "
                "handle; it is not persisted in interview state."
            ),
            required=False,
        ),
    )


async def handle_interview_calibration_turn(
    handler: Any,
    evidence: str,
    *,
    session_id: Any,
) -> Result[MCPToolResult, MCPServerError]:
    """Rephrase a pending question without consuming or persisting an answer."""
    calibration = infer_interview_calibration(evidence)
    pending_question: str | None = None
    rephrased_question: str | None = None
    if isinstance(session_id, str) and session_id.strip():
        engine, _ = handler._create_interview_engine()
        try:
            load_result = await engine.load_state(session_id.strip())
            if load_result.is_err:
                return Result.err(
                    MCPToolError(str(load_result.error), tool_name="ouroboros_interview")
                )
            state = load_result.value
            if state.rounds and state.rounds[-1].user_response is None:
                pending_question = state.rounds[-1].question
                rephrase_method = getattr(engine, "rephrase_pending_question", None)
                if callable(rephrase_method):
                    try:
                        rephrase_result = await rephrase_method(pending_question, calibration)
                        if rephrase_result.is_ok and rephrase_result.value.strip():
                            rephrased_question = rephrase_result.value
                    except Exception as exc:
                        # Rephrasing is optional; retain the calibration and the
                        # original pending question. Cancellation still propagates.
                        log.warning(
                            "interview.calibration.rephrase_unavailable",
                            error_type=type(exc).__name__,
                        )
        finally:
            if handler._owns_event_store:
                await handler.close()

    unknown_terms = ", ".join(calibration.unknown_terms) or "none explicitly extracted"
    lines = [
        "Interview calibration",
        f"- Level: {calibration.level.title()} (confidence: {calibration.confidence})",
        f"- Evidence: {calibration.evidence}",
        f"- Unknown terms to define before use: {unknown_terms}",
        "- Adaptation: define terms first and add at most one neutral example.",
    ]
    if pending_question is not None:
        if rephrased_question is not None:
            lines.extend(
                [
                    "",
                    "Here is the same decision in plainer language:",
                    rephrased_question,
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "Rephrasing was not available. The pending question is unchanged:",
                    pending_question,
                ]
            )
    else:
        lines.extend(["", "This calibration applies to the next interview question."])

    meta: dict[str, Any] = {
        "interview_calibration": calibration.model_dump(mode="json"),
        "calibration_updated": True,
        "pending_question_preserved": pending_question is not None,
        "question_rephrased": rephrased_question is not None,
    }
    if isinstance(session_id, str) and session_id.strip():
        meta["session_id"] = session_id.strip()
    if pending_question is not None:
        meta["pending_question"] = pending_question
    if rephrased_question is not None:
        meta["rephrased_question"] = rephrased_question
    return Result.ok(
        MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="\n".join(lines)),),
            is_error=False,
            meta=meta,
        )
    )


def _engine_supports_calibration(engine: Any) -> bool:
    """Return whether *engine* accepts the ``language_calibration`` keyword.

    The check uses signature inspection so that injected/custom/fake engines
    that implement only the established ``ask_next_question(state)`` contract
    are never passed the unsupported keyword.
    """
    import inspect

    method = getattr(engine, "ask_next_question", None)
    if method is None:
        return False
    try:
        sig = inspect.signature(method)
    except (ValueError, TypeError):
        return False
    return "language_calibration" in sig.parameters


async def _ask_next_question(
    engine: Any,
    state: Any,
    calibration: Any | None,
) -> Any:
    """Call *engine.ask_next_question* with optional calibration support.

    If the engine supports the ``language_calibration`` keyword it is
    forwarded; otherwise the call uses the established one-argument form.
    """
    if calibration is not None and _engine_supports_calibration(engine):
        return await engine.ask_next_question(state, language_calibration=calibration)
    return await engine.ask_next_question(state)
