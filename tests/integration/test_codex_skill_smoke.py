"""Integration smoke tests for Codex exact-prefix skill interception."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from ouroboros.codex import resolve_packaged_codex_skill_path
from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.runtime_factory import create_agent_runtime


def _load_skill_frontmatter(skill_md_path: Path) -> dict[str, object]:
    """Load YAML frontmatter from a packaged skill entrypoint."""
    lines = skill_md_path.read_text(encoding="utf-8").splitlines()
    assert lines
    assert lines[0].strip() == "---"

    closing_index = next(
        index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
    )
    frontmatter = yaml.safe_load("\n".join(lines[1:closing_index]))
    assert isinstance(frontmatter, dict)
    return frontmatter


def _resolve_frontmatter_args(
    value: object,
    *,
    cwd: str,
    first_argument: str | None,
) -> object:
    """Resolve the placeholder syntax supported by deterministic intercepts."""
    if isinstance(value, str):
        if value == "$1":
            return first_argument
        if value == "$CWD":
            return cwd
        return value

    if isinstance(value, Mapping):
        return {
            str(key): _resolve_frontmatter_args(
                item,
                cwd=cwd,
                first_argument=first_argument,
            )
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [
            _resolve_frontmatter_args(
                item,
                cwd=cwd,
                first_argument=first_argument,
            )
            for item in value
        ]

    return value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("skill_name", "prompt", "first_argument"),
    [
        ("run", "ooo run smoke-seed.yaml", "smoke-seed.yaml"),
        ("interview", 'ooo interview "Build a REST API"', "Build a REST API"),
    ],
)
async def test_packaged_ooo_prefixes_dispatch_from_skill_frontmatter(
    tmp_path: Path,
    skill_name: str,
    prompt: str,
    first_argument: str,
) -> None:
    """Packaged `SKILL.md` metadata should drive exact-prefix MCP dispatch."""
    runtime = create_agent_runtime(
        backend="codex",
        cli_path="codex",
        permission_mode="acceptEdits",
        cwd=tmp_path,
    )
    assert isinstance(runtime, CodexCliRuntime)

    with resolve_packaged_codex_skill_path(
        skill_name, skills_dir=runtime._skills_dir
    ) as skill_md_path:
        assert skill_md_path.is_file()

        frontmatter = _load_skill_frontmatter(skill_md_path)
    expected_tool = frontmatter["mcp_tool"]
    expected_args = _resolve_frontmatter_args(
        frontmatter["mcp_args"],
        cwd=str(tmp_path),
        first_argument=first_argument,
    )
    assert isinstance(expected_tool, str)
    assert isinstance(expected_args, dict)

    fake_server = AsyncMock()
    fake_server.call_tool = AsyncMock(
        return_value=Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"{skill_name} ok",
                    ),
                ),
                meta={"session_id": f"{skill_name}-session"},
            )
        )
    )

    with (
        patch(
            "ouroboros.mcp.server.adapter.create_ouroboros_server",
            return_value=fake_server,
        ),
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec"
        ) as mock_exec,
    ):
        messages = [message async for message in runtime.execute_task(prompt)]

    fake_server.call_tool.assert_awaited_once_with(expected_tool, expected_args)
    mock_exec.assert_not_called()
    assert messages[0].content == f"Calling tool: {expected_tool}"
    assert messages[-1].content == f"{skill_name} ok"
    assert messages[-1].data["mcp_tool"] == expected_tool
    assert messages[-1].data["mcp_args"] == expected_args
