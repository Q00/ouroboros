"""Unit tests for ParallelACExecutor."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.parallel_executor import ACExecutionResult, ParallelACExecutor


class _MockAdapter:
    """Minimal adapter stub for ParallelACExecutor tests."""

    def __init__(self, messages: list[AgentMessage]) -> None:
        self._messages = messages

    async def execute_task(self, *args, **kwargs) -> AsyncIterator[AgentMessage]:
        for message in self._messages:
            yield message


@pytest.mark.asyncio
async def test_execute_atomic_ac_uses_ascii_safe_tool_output() -> None:
    """Tool progress logs should avoid Unicode markers that break cp1252 consoles."""
    adapter = _MockAdapter(
        [
            AgentMessage(
                type="assistant",
                content="Calling tool: Write: foo.txt",
                tool_name="Write",
                data={"tool_input": {"file_path": "foo.txt"}},
            ),
            AgentMessage(
                type="result",
                content="[TASK_COMPLETE]",
                data={"subtype": "success"},
            ),
        ]
    )
    console = MagicMock()
    event_store = AsyncMock()
    event_store.append = AsyncMock()

    executor = ParallelACExecutor(adapter=adapter, event_store=event_store, console=console)

    result = await executor._execute_atomic_ac(
        ac_index=0,
        ac_content="Write the output file",
        session_id="sess_123",
        tools=["Write"],
        system_prompt="System prompt",
        seed_goal="Seed goal",
        depth=0,
        start_time=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )

    assert result.success is True
    printed = "\n".join(str(call.args[0]) for call in console.print.call_args_list if call.args)
    assert "->" in printed
    assert "→" not in printed


@pytest.mark.asyncio
async def test_execute_single_ac_decomposition_uses_ascii_safe_output() -> None:
    """Decomposition status logs should avoid Unicode markers that break cp1252 consoles."""
    adapter = _MockAdapter([])
    console = MagicMock()
    event_store = AsyncMock()
    event_store.append = AsyncMock()

    executor = ParallelACExecutor(adapter=adapter, event_store=event_store, console=console)

    sub_results = [
        ACExecutionResult(ac_index=100, ac_content="Sub task 1", success=True),
        ACExecutionResult(ac_index=101, ac_content="Sub task 2", success=True),
    ]

    with (
        patch.object(executor, "_try_decompose_ac", AsyncMock(return_value=["Sub task 1", "Sub task 2"])),
        patch.object(executor, "_execute_sub_acs_parallel", AsyncMock(return_value=sub_results)),
    ):
        result = await executor._execute_single_ac(
            ac_index=0,
            ac_content="Complex task",
            session_id="sess_123",
            tools=["Write"],
            system_prompt="System prompt",
            seed_goal="Seed goal",
            depth=0,
            execution_id="exec_123",
        )

    assert result.success is True
    printed = "\n".join(str(call.args[0]) for call in console.print.call_args_list if call.args)
    assert "-> Decomposed into 2 Sub-ACs" in printed
    assert "→" not in printed


def test_safe_console_text_replaces_unencodable_characters() -> None:
    """Console sanitization should protect Windows cp1252 output."""

    class _Cp1252File:
        encoding = "cp1252"

        def flush(self) -> None:
            return None

    console = MagicMock()
    console.file = _Cp1252File()
    event_store = AsyncMock()
    event_store.append = AsyncMock()
    executor = ParallelACExecutor(adapter=_MockAdapter([]), event_store=event_store, console=console)

    safe = executor._safe_console_text("✅ done")

    assert safe == "? done"
