"""Diagnostics for malformed provider tool-use turns.

Some model/provider clients can report ``stop_reason='tool_use'`` while the
message contains no actual tool-use block. That state should be treated as a
runtime-boundary diagnostic instead of forcing a user to manually continue an
ambiguous turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolUseTurnDiagnostic:
    provider: str
    stop_reason: str
    tool_use_count: int
    is_malformed: bool
    retryable: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "stop_reason": self.stop_reason,
            "tool_use_count": self.tool_use_count,
            "is_malformed": self.is_malformed,
            "retryable": self.retryable,
            "reason": self.reason,
        }


def _get_field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _iter_content_blocks(message: Any) -> tuple[Any, ...]:
    content = _get_field(message, "content")
    if content is None:
        return ()
    if isinstance(content, (str, bytes)):
        return ()
    try:
        return tuple(content)
    except TypeError:
        return ()


def _block_type(block: Any) -> str | None:
    block_type = _get_field(block, "type")
    if isinstance(block_type, str):
        return block_type
    return None


def count_tool_use_blocks(message: Any) -> int:
    """Return the number of explicit tool-use content blocks on a provider message."""
    return sum(1 for block in _iter_content_blocks(message) if _block_type(block) == "tool_use")


def diagnose_tool_use_turn(message: Any, *, provider: str) -> ToolUseTurnDiagnostic:
    """Detect ``stop_reason=tool_use`` without matching tool-use content blocks.

    The function is intentionally side-effect free so runtime adapters can call
    it before deciding whether to retry, surface a blocker, or emit telemetry.
    """
    stop_reason = _get_field(message, "stop_reason")
    normalized_stop = stop_reason if isinstance(stop_reason, str) else ""
    tool_use_count = count_tool_use_blocks(message)
    is_malformed = normalized_stop == "tool_use" and tool_use_count == 0
    reason = (
        "stop_reason=tool_use but no tool_use content blocks were present"
        if is_malformed
        else "tool-use turn is internally consistent"
    )
    return ToolUseTurnDiagnostic(
        provider=provider,
        stop_reason=normalized_stop,
        tool_use_count=tool_use_count,
        is_malformed=is_malformed,
        retryable=is_malformed,
        reason=reason,
    )
