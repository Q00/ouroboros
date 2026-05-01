from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ouroboros.auto.adapters import HandlerInterviewBackend
from ouroboros.cli.main import app
from ouroboros.core.types import Result
from ouroboros.mcp.tools.auto_handler import AutoHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult


def test_cli_auto_help_is_registered() -> None:
    result = CliRunner().invoke(app, ["auto", "--help"])

    assert result.exit_code == 0
    assert "--max-interview-rounds" in result.output
    assert "--skip-run" in result.output


def test_auto_skill_frontmatter_dispatches_to_mcp_tool() -> None:
    skill = Path(__file__).parents[3] / "skills" / "auto" / "SKILL.md"
    content = skill.read_text(encoding="utf-8")

    assert "name: auto" in content
    assert "mcp_tool: ouroboros_auto" in content
    assert 'goal: "$1"' in content


def test_auto_handler_schema_contains_hang_safe_options() -> None:
    definition = AutoHandler().definition

    assert definition.name == "ouroboros_auto"
    names = {param.name for param in definition.parameters}
    assert {"goal", "resume", "max_interview_rounds", "max_repair_rounds", "skip_run"} <= names


class _FakeInterviewHandler:
    async def handle(self, arguments):
        assert arguments == {"session_id": "interview_1"}
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="Pending question?"),),
                is_error=False,
                meta={"session_id": "interview_1"},
            )
        )


@pytest.mark.asyncio
async def test_handler_interview_backend_resume_fetches_pending_question() -> None:
    turn = await HandlerInterviewBackend(_FakeInterviewHandler(), cwd=".").resume("interview_1")

    assert turn.session_id == "interview_1"
    assert turn.question == "Pending question?"


class _FakeErrorInterviewHandler:
    async def handle(self, arguments):  # noqa: ARG002
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="recoverable failure"),),
                is_error=True,
                meta={"recoverable": True},
            )
        )


@pytest.mark.asyncio
async def test_handler_interview_backend_rejects_mcp_error_payloads() -> None:
    with pytest.raises(RuntimeError, match="recoverable failure"):
        await HandlerInterviewBackend(_FakeErrorInterviewHandler(), cwd=".").start("goal", cwd=".")


def test_auto_handler_uses_synchronous_authoring_mode_for_opencode_plugin() -> None:
    handler = AutoHandler(agent_runtime_backend="opencode", opencode_mode="plugin")

    assert handler.agent_runtime_backend == "opencode"
    assert handler.opencode_mode == "plugin"


def test_get_ouroboros_tools_includes_auto_for_runtime_dispatch() -> None:
    from ouroboros.mcp.tools.definitions import get_ouroboros_tools

    names = {handler.definition.name for handler in get_ouroboros_tools()}

    assert "ouroboros_auto" in names
