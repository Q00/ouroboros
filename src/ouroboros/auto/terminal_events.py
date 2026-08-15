"""Durable terminal event projections for auto sessions."""

from collections.abc import Awaitable, Callable
from typing import Any


async def emit_blocked_session_event(
    emit: Callable[[str, str, dict[str, Any]], Awaitable[None]], state: Any, result: Any
) -> None:
    """Wake attention observers for every blocked or failed terminal."""
    if result.status not in {"blocked", "failed"}:
        return
    await emit(
        "auto.session.blocked",
        state.auto_session_id,
        {
            "schema_version": 1,
            "auto_session_id": state.auto_session_id,
            "status": result.status,
            "stop_reason_code": state.last_error_code,
            "tool_name": state.last_tool_name,
            # Blockers may contain provider stderr, credentials, and local
            # paths. Keep raw detail on the local result surface only.
            "blocker": "Auto session requires operator attention",
            "resume_capability": result.resume_capability.value,
        },
    )
