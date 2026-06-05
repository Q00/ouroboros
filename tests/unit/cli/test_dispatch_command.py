"""Tests for the hidden external-frontdoor dispatch CLI."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from ouroboros.cli.commands import dispatch as dispatch_cmd
from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.router.types import InvalidSkill, NotHandled, Resolved


def test_dispatch_prompt_routes_resolved_ooo_command(capsys) -> None:
    resolved = Resolved(
        skill_name="auto",
        command_prefix="ooo auto",
        prompt="ooo auto build it",
        skill_path=Path("skills/auto/SKILL.md"),
        mcp_tool="ouroboros_start_auto",
        mcp_args={"goal": "build it"},
        first_argument="build it",
    )
    captured: dict[str, object] = {}

    async def fake_dispatch(intercept, current_handle):
        captured["intercept"] = intercept
        captured["current_handle"] = current_handle
        return (
            AgentMessage(type="assistant", content="Calling tool: ouroboros_start_auto"),
            AgentMessage(type="result", content="queued auto", data={"subtype": "success"}),
        )

    with (
        patch("ouroboros.cli.commands.dispatch.resolve_skill_dispatch", return_value=resolved),
        patch(
            "ouroboros.cli.commands.dispatch.create_codex_command_dispatcher",
            return_value=fake_dispatch,
        ) as make_dispatcher,
    ):
        exit_code = asyncio.run(
            dispatch_cmd._dispatch_prompt(
                "ooo auto build it",
                runtime="pi",
                llm_backend=None,
                cwd=Path("/tmp/project"),
            )
        )

    assert exit_code == 0
    assert captured["intercept"] is resolved
    assert captured["current_handle"] is None
    make_dispatcher.assert_called_once_with(
        cwd=Path("/tmp/project"),
        runtime_backend="pi",
        llm_backend=None,
    )
    assert "queued auto" in capsys.readouterr().out


def test_dispatch_prompt_returns_unsupported_for_non_mcp_skill(capsys) -> None:
    invalid = InvalidSkill(
        reason="missing required frontmatter key: mcp_tool",
        skill_path=Path("skills/help/SKILL.md"),
    )

    with patch("ouroboros.cli.commands.dispatch.resolve_skill_dispatch", return_value=invalid):
        exit_code = asyncio.run(
            dispatch_cmd._dispatch_prompt(
                "ooo help",
                runtime="pi",
                llm_backend=None,
                cwd=Path("/tmp/project"),
            )
        )

    assert exit_code == dispatch_cmd._UNSUPPORTED_DISPATCH_EXIT_CODE
    assert capsys.readouterr().out == ""


def test_dispatch_prompt_returns_unsupported_for_unhandled_prompt(capsys) -> None:
    with patch(
        "ouroboros.cli.commands.dispatch.resolve_skill_dispatch",
        return_value=NotHandled(reason="skill not found"),
    ):
        exit_code = asyncio.run(
            dispatch_cmd._dispatch_prompt(
                "ooo no-such-skill",
                runtime="pi",
                llm_backend=None,
                cwd=Path("/tmp/project"),
            )
        )

    assert exit_code == dispatch_cmd._UNSUPPORTED_DISPATCH_EXIT_CODE
    assert capsys.readouterr().out == ""


def test_dispatch_prompt_reports_malformed_skill_without_traceback(capsys) -> None:
    invalid = InvalidSkill(
        reason="mcp_tool must be a non-empty string",
        skill_path=Path("skills/help/SKILL.md"),
    )

    with patch("ouroboros.cli.commands.dispatch.resolve_skill_dispatch", return_value=invalid):
        exit_code = asyncio.run(
            dispatch_cmd._dispatch_prompt(
                "ooo help",
                runtime="pi",
                llm_backend=None,
                cwd=Path("/tmp/project"),
            )
        )

    assert exit_code == 2
    output = capsys.readouterr().out
    assert "Invalid Ouroboros skill command: mcp_tool must be a non-empty string" in output
    assert "skills/help/SKILL.md" in output


def test_dispatch_prompt_monitors_auto_job_until_result(capsys) -> None:
    resolved = Resolved(
        skill_name="auto",
        command_prefix="ooo auto",
        prompt="ooo auto build it",
        skill_path=Path("skills/auto/SKILL.md"),
        mcp_tool="ouroboros_start_auto",
        mcp_args={"goal": "build it"},
        first_argument="build it",
    )

    async def fake_dispatch(intercept, current_handle):
        return (
            AgentMessage(type="assistant", content="Calling tool: ouroboros_start_auto"),
            AgentMessage(
                type="result",
                content="started auto",
                data={"subtype": "success", "job_id": "job_auto_123", "cursor": 1},
            ),
        )

    class FakeWaitHandler:
        async def handle(self, arguments):
            assert arguments["job_id"] == "job_auto_123"
            assert arguments["cursor"] == 1
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="job completed"),),
                    meta={
                        "job_id": "job_auto_123",
                        "cursor": 2,
                        "changed": True,
                        "is_terminal": True,
                        "status": "completed",
                    },
                )
            )

    class FakeResultHandler:
        async def handle(self, arguments):
            assert arguments == {"job_id": "job_auto_123"}
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="final auto result"),),
                    meta={"job_id": "job_auto_123", "is_terminal": True},
                )
            )

    with (
        patch("ouroboros.cli.commands.dispatch.resolve_skill_dispatch", return_value=resolved),
        patch(
            "ouroboros.cli.commands.dispatch.create_codex_command_dispatcher",
            return_value=fake_dispatch,
        ),
        patch("ouroboros.cli.commands.dispatch.JobWaitHandler", return_value=FakeWaitHandler()),
        patch("ouroboros.cli.commands.dispatch.JobResultHandler", return_value=FakeResultHandler()),
    ):
        exit_code = asyncio.run(
            dispatch_cmd._dispatch_prompt(
                "ooo auto build it",
                runtime="pi",
                llm_backend=None,
                cwd=Path("/tmp/project"),
            )
        )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "started auto" in output
    assert "Monitoring auto job: job_auto_123" in output
    assert "job completed" in output
    assert "final auto result" in output
