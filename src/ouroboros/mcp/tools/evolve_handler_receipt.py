"""Bounded durable receipts for complete evolve MCP handler calls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ouroboros.core.text import truncate_head_tail
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

MAX_HANDLER_TEXT = 50_000
MAX_HANDLER_ERROR = 2_000


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return truncate_head_tail(value, head=limit // 2, tail=limit // 2)


def encode_evolve_handler_result(
    result: Result[MCPToolResult, MCPServerError],
) -> dict[str, Any]:
    """Encode the public result needed by a cross-process duplicate."""
    if result.is_err:
        return {
            "ok": False,
            "error": _bounded(str(result.error), MAX_HANDLER_ERROR),
            "is_retriable": result.error.is_retriable,
        }
    tool_result = result.value
    content: list[dict[str, Any]] = []
    for item in tool_result.content:
        if item.type is not ContentType.TEXT:
            raise ValueError("Evolve handler durable receipt supports text content only")
        content.append(
            {
                "type": item.type.value,
                "text": _bounded(item.text or "", MAX_HANDLER_TEXT),
                "meta": item.meta,
            }
        )
    action = tool_result.meta.get("action")
    return {
        "ok": True,
        "action": action if isinstance(action, str) else None,
        "content": content,
        "is_error": tool_result.is_error,
        "meta": tool_result.meta,
        "structured_content": tool_result.structured_content,
        "result_type": tool_result.result_type,
    }


def decode_evolve_handler_result(
    payload: Mapping[str, Any],
) -> Result[MCPToolResult, MCPServerError]:
    """Decode the durable public result without rerunning evidence or QA."""
    if not bool(payload.get("ok")):
        return Result.err(
            MCPToolError(
                str(payload.get("error") or "evolve_step failed"),
                tool_name="ouroboros_evolve_step",
                is_retriable=bool(payload.get("is_retriable")),
            )
        )
    raw_content = payload.get("content")
    if not isinstance(raw_content, list):
        raise ValueError("Durable evolve handler receipt content must be a list")
    content: list[MCPContentItem] = []
    for raw_item in raw_content:
        if not isinstance(raw_item, Mapping) or raw_item.get("type") != ContentType.TEXT.value:
            raise ValueError("Durable evolve handler receipt contains invalid content")
        raw_meta = raw_item.get("meta")
        content.append(
            MCPContentItem(
                type=ContentType.TEXT,
                text=str(raw_item.get("text") or ""),
                meta=dict(raw_meta) if isinstance(raw_meta, Mapping) else {},
            )
        )
    raw_meta = payload.get("meta")
    return Result.ok(
        MCPToolResult(
            content=tuple(content),
            is_error=bool(payload.get("is_error")),
            meta=dict(raw_meta) if isinstance(raw_meta, Mapping) else {},
            structured_content=payload.get("structured_content"),
            result_type=str(payload.get("result_type") or "complete"),
        )
    )
