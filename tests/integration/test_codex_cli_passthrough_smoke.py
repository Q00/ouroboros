"""Integration smoke tests for Codex exact-prefix pass-through behavior."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.codex import resolve_packaged_codex_skill_path
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.runtime_factory import create_agent_runtime


class _FakeStream:
    def __init__(self, lines: list[str] | None = None) -> None:
        self._lines = [f"{line}\n".encode() for line in (lines or [])]

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeProcess:
    def __init__(self, returncode: int = 0) -> None:
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self._returncode = returncode

    async def wait(self) -> int:
        return self._returncode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "expected_warning", "expected_error"),
    [
        (
            "ooo unsupported seed.yaml",
            None,
            None,
        ),
        (
            "ooo help",
            "codex_cli_runtime.skill_intercept_frontmatter_missing",
            "missing required frontmatter key: mcp_tool",
        ),
    ],
)
async def test_unhandled_ooo_commands_pass_through_to_codex_unchanged(
    tmp_path: Path,
    prompt: str,
    expected_warning: str | None,
    expected_error: str | None,
) -> None:
    """Unsupported and plugin-only `ooo` commands should bypass intercept dispatch."""
    runtime = create_agent_runtime(
        backend="codex",
        cli_path="/tmp/codex",
        permission_mode="acceptEdits",
        cwd=tmp_path,
    )

    assert isinstance(runtime, CodexCliRuntime)
    assert runtime._skill_dispatcher is not None
    with resolve_packaged_codex_skill_path("help", skills_dir=runtime._skills_dir) as skill_md_path:
        assert skill_md_path.is_file()

    async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
        assert kwargs["cwd"] == str(tmp_path)
        assert command[-1] == prompt
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(
            f"Codex pass-through: {prompt}",
            encoding="utf-8",
        )
        return _FakeProcess(returncode=0)

    with (
        patch("ouroboros.mcp.server.adapter.create_ouroboros_server") as mock_create_server,
        patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ) as mock_exec,
    ):
        messages = [message async for message in runtime.execute_task(prompt)]

    mock_exec.assert_called_once()
    mock_create_server.assert_not_called()
    assert messages[-1].content == f"Codex pass-through: {prompt}"
    assert messages[-1].data["subtype"] == "success"

    if expected_warning is None:
        mock_warning.assert_not_called()
    else:
        mock_warning.assert_called_once()
        assert mock_warning.call_args[0][0] == expected_warning
        assert mock_warning.call_args.kwargs["error"] == expected_error
