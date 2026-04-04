"""Focused recursion-depth regressions for ``ParallelACExecutor``."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.orchestrator.parallel_executor import ACExecutionResult, ParallelACExecutor


@pytest.mark.asyncio
async def test_recursive_decomposition_reaches_depth_limit_before_forcing_atomic() -> None:
    """Composite ACs should keep recursing until the soft depth safety net is reached."""
    executor = ParallelACExecutor(
        adapter=MagicMock(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )
    executor._emit_subtask_event = AsyncMock()
    executor._try_decompose_ac = AsyncMock(
        side_effect=[
            ["Composite depth 1", "Atomic depth 1"],
            ["Composite depth 2", "Atomic depth 2"],
            ["Forced atomic depth 3 A", "Forced atomic depth 3 B"],
            None,
            None,
        ]
    )

    async def fake_execute_atomic_ac(**kwargs: Any) -> ACExecutionResult:
        return ACExecutionResult(
            ac_index=int(kwargs["ac_index"]),
            ac_content=str(kwargs["ac_content"]),
            success=True,
            final_message=f"{kwargs['ac_content']} complete",
            depth=int(kwargs["depth"]),
        )

    executor._execute_atomic_ac = AsyncMock(side_effect=fake_execute_atomic_ac)

    with patch.object(
        executor,
        "_execute_single_ac",
        wraps=executor._execute_single_ac,
    ) as execute_single_ac_spy:
        result = await executor._execute_single_ac(
            ac_index=1,
            ac_content="Root composite AC",
            session_id="sess_recursive_depth",
            tools=["Read", "Edit"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Support recursive decomposition",
            depth=0,
            execution_id="exec_recursive_depth",
        )

    assert result.success is True
    assert result.is_decomposed is True

    level_one_composite, level_one_atomic = result.sub_results
    assert level_one_composite.is_decomposed is True
    assert level_one_atomic.depth == 1
    assert level_one_atomic.decomposition_depth_warning is False

    level_two_composite, level_two_atomic = level_one_composite.sub_results
    assert level_two_composite.is_decomposed is True
    assert level_two_atomic.depth == 2
    assert level_two_atomic.decomposition_depth_warning is False

    forced_atomic_a, forced_atomic_b = level_two_composite.sub_results
    assert [forced_atomic_a.depth, forced_atomic_b.depth] == [3, 3]
    assert [
        forced_atomic_a.decomposition_depth_warning,
        forced_atomic_b.decomposition_depth_warning,
    ] == [True, True]

    assert [
        (
            int(call.kwargs["ac_index"]),
            str(call.kwargs["ac_content"]),
            int(call.kwargs["depth"]),
        )
        for call in execute_single_ac_spy.await_args_list
    ] == [
        (1, "Root composite AC", 0),
        (100, "Composite depth 1", 1),
        (10000, "Composite depth 2", 2),
        (1000000, "Forced atomic depth 3 A", 3),
        (1000001, "Forced atomic depth 3 B", 3),
        (10001, "Atomic depth 2", 2),
        (101, "Atomic depth 1", 1),
    ]
    assert executor._try_decompose_ac.await_count == 5
    assert [
        (int(call.kwargs["ac_index"]), int(call.kwargs["depth"]))
        for call in executor._execute_atomic_ac.await_args_list
    ] == [
        (1000000, 3),
        (1000001, 3),
        (10001, 2),
        (101, 1),
    ]
