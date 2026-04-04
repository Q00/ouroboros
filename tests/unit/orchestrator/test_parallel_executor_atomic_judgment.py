"""Regressions for ATOMIC decomposition judgments in ``ParallelACExecutor``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.parallel_executor import (
    MAX_DECOMPOSITION_DEPTH,
    ACExecutionResult,
    ParallelACExecutor,
)


class _AtomicDecompositionRuntime:
    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: object | None = None,
        resume_session_id: str | None = None,
    ):
        del prompt, tools, system_prompt, resume_handle, resume_session_id
        yield AgentMessage(type="result", content="ATOMIC")


@pytest.mark.asyncio
async def test_try_decompose_ac_treats_atomic_response_as_terminal() -> None:
    """Claude's explicit ATOMIC verdict should suppress further decomposition."""
    executor = ParallelACExecutor(
        adapter=_AtomicDecompositionRuntime(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )

    result = await executor._try_decompose_ac(
        ac_content="Implement one focused leaf task.",
        ac_index=0,
        seed_goal="Preserve ATOMIC termination",
        tools=["Read"],
        system_prompt="system",
    )

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "depth",
    range(MAX_DECOMPOSITION_DEPTH),
    ids=lambda depth: f"depth_{depth}",
)
async def test_atomic_judgment_stops_single_ac_recursion_at_any_analyzed_depth(
    depth: int,
) -> None:
    """Nested AC execution should stop recursing once decomposition returns ATOMIC."""
    executor = ParallelACExecutor(
        adapter=MagicMock(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )
    executor._emit_subtask_event = AsyncMock()
    executor._try_decompose_ac = AsyncMock(return_value=None)
    executor._execute_atomic_ac = AsyncMock(
        return_value=ACExecutionResult(
            ac_index=depth + 1,
            ac_content=f"Atomic at depth {depth}",
            success=True,
            final_message="leaf complete",
            depth=depth,
        )
    )

    with patch.object(
        executor,
        "_execute_single_ac",
        wraps=executor._execute_single_ac,
    ) as execute_single_ac_spy:
        result = await executor._execute_single_ac(
            ac_index=depth + 1,
            ac_content=f"Atomic at depth {depth}",
            session_id=f"sess_atomic_depth_{depth}",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Preserve ATOMIC termination",
            depth=depth,
            execution_id=f"exec_atomic_depth_{depth}",
        )

    assert result.success is True
    assert result.is_decomposed is False
    assert result.depth == depth
    executor._try_decompose_ac.assert_awaited_once()
    executor._execute_atomic_ac.assert_awaited_once()
    assert len(execute_single_ac_spy.await_args_list) == 1
    assert execute_single_ac_spy.await_args.kwargs["depth"] == depth
