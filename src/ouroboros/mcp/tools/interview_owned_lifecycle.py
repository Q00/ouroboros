"""Owned interview stores settle events after their final concurrent call."""

import asyncio
from typing import TYPE_CHECKING, Any

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.types import MCPToolResult

if TYPE_CHECKING:
    from ouroboros.mcp.tools.authoring_handlers import InterviewHandler


async def _release_owned_call(handler: "InterviewHandler") -> None:
    async with handler._owned_call_lock:
        handler._active_owned_calls -= 1
        if handler._active_owned_calls == 0:
            await handler.close()


async def run_owned_interview_call(
    handler: "InterviewHandler", arguments: dict[str, Any]
) -> Result[MCPToolResult, MCPServerError]:
    """Allow concurrent calls, but drain the owned store after the last call."""
    async with handler._owned_call_lock:
        if handler._active_owned_calls == 0:
            handler._closed = False
        handler._active_owned_calls += 1
    try:
        return await handler._handle_request(arguments)
    finally:
        cleanup = asyncio.create_task(_release_owned_call(handler))
        interrupted = False
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                if cleanup.cancelled():
                    raise
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError
