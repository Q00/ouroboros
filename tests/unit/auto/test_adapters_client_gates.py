"""Auto adapter compatibility with interview client-gate metadata."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ouroboros.auto.adapters import HandlerInterviewBackend
from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult


@pytest.mark.asyncio
async def test_auto_interview_backend_ignores_seed_ready_client_gate_metadata(tmp_path) -> None:
    """New seed-ready metadata must not break the in-flight auto driver adapter."""
    handler = AsyncMock()
    handler.handle = AsyncMock(
        return_value=Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text="Session interview_auto\n\nSeed-ready.",
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": "interview_auto",
                    "seed_ready": True,
                    "required_client_gates": (
                        "seed_ready_acceptance_guard",
                        "restate_goal_approved",
                    ),
                },
            )
        )
    )
    handler.resolved_state_dir.return_value = tmp_path
    backend = HandlerInterviewBackend(handler, cwd=str(tmp_path))

    turn = await backend.resume("interview_auto")

    assert turn.session_id == "interview_auto"
    assert turn.seed_ready is True
