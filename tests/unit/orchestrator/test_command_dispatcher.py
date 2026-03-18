"""Unit tests for deterministic Codex command dispatch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPTimeoutError, MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime, SkillInterceptRequest
from ouroboros.orchestrator.command_dispatcher import create_codex_command_dispatcher


class TestCodexCommandDispatcher:
    """Tests for the in-process dispatcher used by Codex runtimes."""

    @staticmethod
    def _write_skill(
        skills_dir: Path,
        skill_name: str,
        frontmatter_lines: list[str],
    ) -> None:
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir(parents=True)
        frontmatter = "\n".join(frontmatter_lines)
        (skill_dir / "SKILL.md").write_text(
            f"---\n{frontmatter}\n---\n\n# {skill_name}\n",
            encoding="utf-8",
        )

    @staticmethod
    def _make_intercept(
        skills_dir: Path,
        skill_name: str,
        *,
        mcp_tool: str,
        mcp_args: dict[str, object],
        prompt: str,
        first_argument: str | None,
    ) -> SkillInterceptRequest:
        return SkillInterceptRequest(
            skill_name=skill_name,
            command_prefix=f"ooo {skill_name}",
            prompt=prompt,
            skill_path=skills_dir / skill_name / "SKILL.md",
            mcp_tool=mcp_tool,
            mcp_args=mcp_args,
            first_argument=first_argument,
        )

    @pytest.mark.asyncio
    async def test_dispatches_ooo_run_before_codex_exec(self, tmp_path: Path) -> None:
        """`ooo run` should resolve through the dispatcher before Codex model execution."""
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
                '  cwd: "$CWD"',
            ],
        )
        fake_server = AsyncMock()
        fake_server.call_tool = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text="Seed Execution SUCCESS",
                        ),
                    ),
                    meta={"session_id": "sess-123"},
                )
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd=tmp_path,
            skills_dir=tmp_path,
            skill_dispatcher=create_codex_command_dispatcher(
                cwd=tmp_path,
                runtime_backend="codex",
            ),
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
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        fake_server.call_tool.assert_awaited_once_with(
            "ouroboros_execute_seed",
            {"seed_path": "seed.yaml", "cwd": str(tmp_path)},
        )
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == [
            "Calling tool: ouroboros_execute_seed",
            "Seed Execution SUCCESS",
        ]
        assert messages[-1].data["session_id"] == "sess-123"

    @pytest.mark.asyncio
    async def test_dispatches_ooo_interview_with_session_reuse(self, tmp_path: Path) -> None:
        """`ooo interview` should resume the stored interview session and return its MCP result."""
        self._write_skill(
            tmp_path,
            "interview",
            [
                "name: interview",
                'description: "Socratic interview to crystallize vague requirements"',
                "mcp_tool: ouroboros_interview",
                "mcp_args:",
                '  initial_context: "$1"',
                '  cwd: "$CWD"',
            ],
        )
        fake_server = AsyncMock()
        fake_server.call_tool = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text="Session interview-123\n\nWhat database do you want?",
                        ),
                    ),
                    meta={"session_id": "interview-123"},
                    is_error=True,
                )
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd=tmp_path,
            skills_dir=tmp_path,
            skill_dispatcher=create_codex_command_dispatcher(
                cwd=tmp_path,
                runtime_backend="codex",
            ),
        )
        resume_handle = RuntimeHandle(
            backend="codex_cli",
            native_session_id="thread-123",
            metadata={"ouroboros_interview_session_id": "interview-123"},
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
            messages = [
                message
                async for message in runtime.execute_task(
                    'ooo interview "Use PostgreSQL"',
                    resume_handle=resume_handle,
                )
            ]

        fake_server.call_tool.assert_awaited_once_with(
            "ouroboros_interview",
            {
                "session_id": "interview-123",
                "answer": "Use PostgreSQL",
            },
        )
        mock_exec.assert_not_called()
        assert messages[-1].data["subtype"] == "error"
        assert messages[-1].data["tool_error"] is True
        assert messages[-1].resume_handle is not None
        assert messages[-1].resume_handle.native_session_id == "thread-123"
        assert (
            messages[-1].resume_handle.metadata["ouroboros_interview_session_id"] == "interview-123"
        )

    @pytest.mark.asyncio
    async def test_dispatch_returns_recoverable_messages_when_call_tool_fails(
        self,
        tmp_path: Path,
    ) -> None:
        """MCP server Result errors should surface as recoverable dispatcher output."""
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
                '  cwd: "$CWD"',
            ],
        )
        intercept = self._make_intercept(
            tmp_path,
            "run",
            mcp_tool="ouroboros_execute_seed",
            mcp_args={"seed_path": "seed.yaml", "cwd": str(tmp_path)},
            prompt="ooo run seed.yaml",
            first_argument="seed.yaml",
        )
        fake_server = AsyncMock()
        fake_server.call_tool = AsyncMock(
            return_value=Result.err(
                MCPToolError(
                    "Seed tool unavailable",
                    tool_name="ouroboros_execute_seed",
                )
            )
        )
        dispatcher = create_codex_command_dispatcher(cwd=tmp_path, runtime_backend="codex")

        with patch(
            "ouroboros.mcp.server.adapter.create_ouroboros_server",
            return_value=fake_server,
        ):
            messages = await dispatcher(intercept, None)

        assert messages is not None
        assert messages[0].tool_name == "ouroboros_execute_seed"
        assert messages[0].data["tool_input"] == {
            "seed_path": "seed.yaml",
            "cwd": str(tmp_path),
        }
        assert messages[1].is_error is True
        assert messages[1].data["recoverable"] is True
        assert messages[1].data["error_type"] == "MCPToolError"
        assert messages[1].content == "Seed tool unavailable"

    @pytest.mark.asyncio
    async def test_dispatch_returns_recoverable_messages_when_call_tool_raises(
        self,
        tmp_path: Path,
    ) -> None:
        """Transport exceptions should be surfaced as recoverable dispatcher output."""
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        intercept = self._make_intercept(
            tmp_path,
            "run",
            mcp_tool="ouroboros_execute_seed",
            mcp_args={"seed_path": "seed.yaml"},
            prompt="ooo run seed.yaml",
            first_argument="seed.yaml",
        )
        resume_handle = RuntimeHandle(backend="codex_cli", native_session_id="thread-123")
        fake_server = AsyncMock()
        fake_server.call_tool = AsyncMock(
            side_effect=MCPTimeoutError(
                "Tool call timed out",
                server_name="ouroboros-codex-dispatch",
            )
        )
        dispatcher = create_codex_command_dispatcher(cwd=tmp_path, runtime_backend="codex")

        with patch(
            "ouroboros.mcp.server.adapter.create_ouroboros_server",
            return_value=fake_server,
        ):
            messages = await dispatcher(intercept, resume_handle)

        assert messages is not None
        assert messages[0].resume_handle == resume_handle
        assert messages[1].resume_handle == resume_handle
        assert messages[1].is_error is True
        assert messages[1].data["recoverable"] is True
        assert messages[1].data["is_retriable"] is True
        assert messages[1].data["error_type"] == "MCPTimeoutError"
        assert (
            messages[1].content
            == "Tool call timed out server=ouroboros-codex-dispatch retriable=True"
        )

    @pytest.mark.asyncio
    async def test_dispatch_builds_opencode_resume_handle_for_interview_sessions(
        self,
        tmp_path: Path,
    ) -> None:
        """Interview dispatch should persist the selected runtime backend."""
        intercept = self._make_intercept(
            tmp_path,
            "interview",
            mcp_tool="ouroboros_interview",
            mcp_args={"initial_context": "Build a REST API"},
            prompt='ooo interview "Build a REST API"',
            first_argument="Build a REST API",
        )
        fake_server = AsyncMock()
        fake_server.call_tool = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="Question 1"),),
                    meta={"session_id": "interview-123"},
                )
            )
        )
        dispatcher = create_codex_command_dispatcher(cwd=tmp_path, runtime_backend="opencode")

        with patch(
            "ouroboros.mcp.server.adapter.create_ouroboros_server",
            return_value=fake_server,
        ):
            messages = await dispatcher(intercept, None)

        assert messages is not None
        assert messages[1].resume_handle is not None
        assert messages[1].resume_handle.backend == "opencode"
        assert (
            messages[1].resume_handle.metadata["ouroboros_interview_session_id"] == "interview-123"
        )
