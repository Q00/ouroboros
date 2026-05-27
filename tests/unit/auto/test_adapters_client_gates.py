"""Auto adapter compatibility with interview client-gate metadata."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from ouroboros.auto.adapters import (
    HandlerError,
    HandlerInterviewBackend,
    HandlerSeedGenerator,
    HandlerSynchronousRunStarter,
)
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult


class _FakeSeed:
    def to_dict(self) -> dict[str, object]:
        return {"goal": "Create hello_auto.py", "acceptance_criteria": ()}


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
async def test_synchronous_run_starter_skips_execute_seed_qa(tmp_path) -> None:
    """Auto complete-product uses exact execution evidence, not execute_seed QA teardown."""
    handler = AsyncMock()
    handler.handle = AsyncMock(
        return_value=Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="done"),),
                is_error=False,
                meta={
                    "session_id": "orch_sync",
                    "execution_id": "exec_sync",
                    "status": "completed",
                    "success": True,
                },
            )
        )
    )
    starter = HandlerSynchronousRunStarter(handler, cwd=str(tmp_path))

    result = await starter(_FakeSeed())  # type: ignore[arg-type]

    arguments = handler.handle.await_args.args[0]
    assert arguments["skip_qa"] is True
    assert result["status"] == "completed"
    assert result["success"] is True


@pytest.mark.asyncio
async def test_synchronous_run_starter_returns_persisted_terminal_meta_before_teardown(
    tmp_path,
) -> None:
    """Auto does not wait for execute_seed teardown after the session is terminal."""
    handler = AsyncMock()

    async def slow_handle(*_args: object, **_kwargs: object) -> object:
        await asyncio.sleep(60)
        raise AssertionError("terminal recovery should return first")

    handler.handle = AsyncMock(side_effect=slow_handle)

    class RecoveringStarter(HandlerSynchronousRunStarter):
        async def recover_timed_out_run(self) -> dict[str, object] | None:
            return {
                "job_id": None,
                "session_id": "orch_recovered",
                "execution_id": "exec_recovered",
                "status": "completed",
                "success": True,
                "_allow_deadline_completion_grace": True,
            }

    starter = RecoveringStarter(
        handler,
        cwd=str(tmp_path),
        terminal_poll_interval_seconds=0.1,
    )

    result = await starter(_FakeSeed())  # type: ignore[arg-type]

    assert result["session_id"] == "orch_recovered"
    assert result["execution_id"] == "exec_recovered"
    assert result["success"] is True


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


@pytest.mark.asyncio
async def test_auto_seed_generator_forwards_force_kwarg_to_handler() -> None:
    """PR-β / SSOT #1157 *Closure Policy* (2026-05-27) — Bot review #1 BLOCKER.

    When ``HandlerSeedGenerator`` is called with ``force=True`` (the
    contract ``AutoPipeline`` honors for ``ledger_only`` / ``safe_default``
    closure modes), the adapter must forward ``"force": True`` into the
    handler arguments. That argument is what causes ``GenerateSeedHandler``
    to call ``SeedGenerator.generate(..., force=True)`` and bypass the 0.2
    ambiguity gate at ``bigbang/seed_generator.py:141``. Without this
    forwarding, the driver-side ledger-primary closure would only move the
    block from INTERVIEW to SEED_GENERATION at exactly the same threshold.
    """
    handler = AsyncMock()
    handler.handle = AsyncMock(
        return_value=Result.err(
            MCPToolError("stop after capturing arguments", tool_name="ouroboros_generate_seed")
        )
    )
    generator = HandlerSeedGenerator(handler)

    with pytest.raises(HandlerError):
        await generator("interview_auto", force=True)

    handler.handle.assert_awaited_once_with(
        {
            "session_id": "interview_auto",
            "client_gates": (
                "seed_ready_acceptance_guard",
                "restate_goal_approved",
            ),
            "force": True,
        }
    )


@pytest.mark.asyncio
async def test_auto_seed_generator_omits_force_when_not_requested() -> None:
    """Default contract preserved: when ``AutoPipeline`` does NOT request
    a force (i.e. ``mutual_agreement`` closures), the adapter must omit the
    ``force`` key entirely so the maintained MCP gate keeps its legacy
    semantics. Pin both directions of the new contract.
    """
    handler = AsyncMock()
    handler.handle = AsyncMock(
        return_value=Result.err(
            MCPToolError("stop after capturing arguments", tool_name="ouroboros_generate_seed")
        )
    )
    generator = HandlerSeedGenerator(handler)

    with pytest.raises(HandlerError):
        await generator("interview_auto")

    arguments = handler.handle.await_args.args[0]
    assert "force" not in arguments
