"""Integration smoke tests for Codex exact-prefix fallback behavior."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.mcp.errors import MCPTimeoutError
from ouroboros.orchestrator.runtime_factory import create_agent_runtime


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [f"{line}\n".encode() for line in lines]

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], stderr_lines: list[str], returncode: int = 0) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self._returncode = returncode

    async def wait(self) -> int:
        return self._returncode


@pytest.mark.asyncio
async def test_codex_mcp_timeout_falls_back_to_pass_through_cli_flow(tmp_path: Path) -> None:
    """A recoverable MCP failure should fall through to normal Codex execution."""
    runtime = create_agent_runtime(
        backend="codex",
        cli_path="codex",
        cwd=tmp_path,
        permission_mode="acceptEdits",
    )

    fake_server = AsyncMock()
    fake_server.call_tool = AsyncMock(
        side_effect=MCPTimeoutError(
            "Tool call timed out",
            server_name="ouroboros-codex-dispatch",
        )
    )

    async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
        assert command[-1] == "ooo run seed.yaml"
        assert kwargs["cwd"] == str(tmp_path)
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text("Codex fallback completed", encoding="utf-8")
        return _FakeProcess(
            stdout_lines=[
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "content": [{"text": "Handling request through Codex"}],
                        },
                    }
                ),
            ],
            stderr_lines=[],
            returncode=0,
        )

    with (
        patch("ouroboros.mcp.server.adapter.create_ouroboros_server", return_value=fake_server),
        patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ) as mock_exec,
    ):
        messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

    fake_server.call_tool.assert_awaited_once_with(
        "ouroboros_execute_seed",
        {"seed_path": "seed.yaml", "cwd": str(tmp_path)},
    )
    mock_exec.assert_called_once()
    mock_warning.assert_called_once()
    assert mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_dispatch_failed"
    assert mock_warning.call_args.kwargs["skill"] == "run"
    assert mock_warning.call_args.kwargs["tool"] == "ouroboros_execute_seed"
    assert mock_warning.call_args.kwargs["command_prefix"] == "ooo run"
    assert mock_warning.call_args.kwargs["recoverable"] is True
    assert mock_warning.call_args.kwargs["error_type"] == "MCPTimeoutError"
    assert [message.type for message in messages] == ["system", "assistant", "result"]
    assert messages[0].data["session_id"] == "thread-123"
    assert messages[1].content == "Handling request through Codex"
    assert messages[-1].content == "Codex fallback completed"
    assert messages[-1].data["subtype"] == "success"
