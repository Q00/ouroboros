"""Runtime-handle contract for intercepted interview control turns."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.router.types import Resolved

INTERVIEW_SESSION_METADATA_KEY = "ouroboros_interview_session_id"
INTERVIEW_CALIBRATION_METADATA_KEY = "ouroboros_interview_calibration"


def build_interview_tool_arguments(
    intercept: Resolved,
    current_handle: RuntimeHandle | None,
) -> dict[str, Any]:
    """Overlay session state while keeping calibration turns out of answer slots."""
    arguments: dict[str, Any] = dict(intercept.mcp_args)
    if intercept.mcp_tool != "ouroboros_interview" or current_handle is None:
        return arguments
    session_id = current_handle.metadata.get(INTERVIEW_SESSION_METADATA_KEY)
    if isinstance(session_id, str) and session_id.strip():
        arguments.pop("initial_context", None)
        arguments["session_id"] = session_id.strip()
        if intercept.skill_name != "idk" and intercept.first_argument is not None:
            arguments["answer"] = intercept.first_argument
    calibration = current_handle.metadata.get(INTERVIEW_CALIBRATION_METADATA_KEY)
    if intercept.skill_name != "idk" and isinstance(calibration, Mapping):
        arguments["interview_calibration"] = dict(calibration)
    return arguments
