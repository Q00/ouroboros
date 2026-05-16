"""Auto adapter compatibility with interview client-gate metadata."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ouroboros.auto.adapters import HandlerError, HandlerInterviewBackend, HandlerSeedGenerator
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
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


@pytest.mark.asyncio
async def test_auto_interview_backend_forwards_last_question_for_reopened_answers(
    tmp_path,
) -> None:
    """The handler adapter must preserve the driver's seed-ready reopen probe."""
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
                meta={"session_id": "interview_auto", "seed_ready": True},
            )
        )
    )
    handler.resolved_state_dir.return_value = tmp_path
    backend = HandlerInterviewBackend(handler, cwd=str(tmp_path))

    await backend.answer(
        "interview_auto",
        "[from-auto] No cloud sync",
        last_question="[driver gap-reopen 'non_goals': backend_completed=True ledger_done=False]",
    )

    handler.handle.assert_awaited_once_with(
        {
            "session_id": "interview_auto",
            "answer": "[from-auto] No cloud sync",
            "last_question": (
                "[driver gap-reopen 'non_goals': backend_completed=True ledger_done=False]"
            ),
        }
    )


@pytest.mark.asyncio
async def test_auto_seed_generator_passes_client_gate_acknowledgements() -> None:
    """The opt-in hard gate must not break maintained auto seed generation."""
    handler = AsyncMock()
    handler.handle = AsyncMock(
        return_value=Result.err(
            MCPToolError("stop after capturing arguments", tool_name="ouroboros_generate_seed")
        )
    )
    generator = HandlerSeedGenerator(handler)

    with pytest.raises(HandlerError):
        await generator("interview_auto")

    handler.handle.assert_awaited_once_with(
        {
            "session_id": "interview_auto",
            "client_gates": (
                "seed_ready_acceptance_guard",
                "restate_goal_approved",
            ),
        }
    )
