"""Unit tests for CodexCliRuntime."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [f"{line}\n".encode() for line in lines]

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeProcess:
    def __init__(
        self, stdout_lines: list[str], stderr_lines: list[str], returncode: int = 0
    ) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self._returncode = returncode

    async def wait(self) -> int:
        return self._returncode


class TestCodexCliRuntime:
    """Tests for CodexCliRuntime."""

    @staticmethod
    def _write_skill(
        skills_dir: Path,
        skill_name: str,
        frontmatter_lines: list[str],
    ) -> Path:
        skill_dir = skills_dir / skill_name
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        frontmatter = "\n".join(frontmatter_lines)
        skill_md.write_text(
            f"---\n{frontmatter}\n---\n\n# {skill_name}\n",
            encoding="utf-8",
        )
        return skill_md

    def test_build_command_for_new_session(self) -> None:
        """Builds a new-session exec command."""
        runtime = CodexCliRuntime(
            cli_path="/usr/local/bin/codex",
            permission_mode="acceptEdits",
            model="o3",
            cwd="/tmp/project",
        )

        command = runtime._build_command(
            output_last_message_path="/tmp/out.txt",
            prompt="Fix the bug",
        )

        assert command[:2] == ["/usr/local/bin/codex", "exec"]
        assert "--json" in command
        assert "--full-auto" in command
        assert "--model" in command
        assert "o3" in command
        assert "-C" in command
        assert "/tmp/project" in command
        assert command[-1] == "Fix the bug"

    def test_build_command_for_resume(self) -> None:
        """Builds an exec resume command when a session id is provided."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

        command = runtime._build_command(
            output_last_message_path="/tmp/out.txt",
            prompt="Continue",
            resume_session_id="thread-123",
        )

        assert command[:4] == ["codex", "exec", "resume", "thread-123"]

    def test_build_command_uses_read_only_for_default_permission_mode(self) -> None:
        """Default permission mode keeps the runtime in read-only mode."""
        runtime = CodexCliRuntime(cli_path="codex", permission_mode="default")

        command = runtime._build_command(
            output_last_message_path="/tmp/out.txt",
            prompt="Inspect the repo",
        )

        assert "--sandbox" in command
        assert "read-only" in command

    def test_build_command_uses_dangerous_bypass_for_bypass_permissions(self) -> None:
        """bypassPermissions uses Codex's no-approval/no-sandbox mode."""
        runtime = CodexCliRuntime(cli_path="codex", permission_mode="bypassPermissions")

        command = runtime._build_command(
            output_last_message_path="/tmp/out.txt",
            prompt="Apply the fix",
        )

        assert "--dangerously-bypass-approvals-and-sandbox" in command

    def test_convert_thread_started_event(self) -> None:
        """Converts thread.started to a system message with a resume handle."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(
            {"type": "thread.started", "thread_id": "thread-123"},
            current_handle=None,
        )

        assert len(messages) == 1
        message = messages[0]
        assert message.type == "system"
        assert message.resume_handle is not None
        assert message.resume_handle.backend == "codex_cli"
        assert message.resume_handle.native_session_id == "thread-123"

    def test_convert_command_execution_event(self) -> None:
        """Converts command execution items to Bash tool messages."""
        runtime = CodexCliRuntime(cli_path="codex")

        messages = runtime._convert_event(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "pytest -q"},
            },
            current_handle=None,
        )

        assert len(messages) == 1
        message = messages[0]
        assert message.tool_name == "Bash"
        assert message.data["tool_input"]["command"] == "pytest -q"

    def test_resolve_skill_intercept_requires_exact_prefix_match(self, tmp_path: Path) -> None:
        """Only exact `ooo` and `/ouroboros:` prefixes are intercept candidates."""
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
        runtime = CodexCliRuntime(cli_path="codex", skills_dir=tmp_path)

        intercept = runtime._resolve_skill_intercept('ooo run "seed spec.yaml"')

        assert intercept is not None
        assert intercept.skill_name == "run"
        assert intercept.command_prefix == "ooo run"
        assert intercept.first_argument == "seed spec.yaml"
        assert intercept.mcp_args == {"seed_path": "seed spec.yaml"}
        assert runtime._resolve_skill_intercept('please ooo run "seed spec.yaml"') is None
        assert runtime._resolve_skill_intercept("ooo:run seed.yaml") is None

    def test_resolve_skill_intercept_maps_interview_argument_to_initial_context(
        self,
        tmp_path: Path,
    ) -> None:
        """`ooo interview <topic>` resolves frontmatter templates before dispatch."""
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
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project", skills_dir=tmp_path)

        intercept = runtime._resolve_skill_intercept('ooo interview "Build a REST API"')

        assert intercept is not None
        assert intercept.mcp_tool == "ouroboros_interview"
        assert intercept.first_argument == "Build a REST API"
        assert intercept.mcp_args == {
            "initial_context": "Build a REST API",
            "cwd": "/tmp/project",
        }

    def test_resolve_skill_intercept_uses_packaged_skill_helper_without_override(
        self,
        tmp_path: Path,
    ) -> None:
        """Default intercept resolution should read packaged skills via the shared helper."""
        skill_md_path = self._write_skill(
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
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

        def fake_resolve_packaged_skill(skill_name: str, *, skills_dir: Path | None = None):
            assert skill_name == "interview"
            assert skills_dir is None

            class _ResolvedSkill:
                def __enter__(self) -> Path:
                    return skill_md_path

                def __exit__(self, exc_type, exc, tb) -> None:
                    return None

            return _ResolvedSkill()

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.resolve_packaged_codex_skill_path",
            side_effect=fake_resolve_packaged_skill,
        ) as mock_resolve:
            intercept = runtime._resolve_skill_intercept('ooo interview "Build a REST API"')

        mock_resolve.assert_called_once_with("interview", skills_dir=None)
        assert intercept is not None
        assert intercept.mcp_tool == "ouroboros_interview"
        assert intercept.mcp_args == {
            "initial_context": "Build a REST API",
            "cwd": "/tmp/project",
        }

    def test_resolve_skill_intercept_bypasses_incomplete_frontmatter(self, tmp_path: Path) -> None:
        """Missing `mcp_tool` or `mcp_args` disables deterministic intercept."""
        self._write_skill(
            tmp_path,
            "help",
            [
                "name: help",
                'description: "Full reference guide for Ouroboros commands and agents"',
                "mcp_tool: ouroboros_help",
            ],
        )
        runtime = CodexCliRuntime(cli_path="codex", skills_dir=tmp_path)

        with patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning:
            intercept = runtime._resolve_skill_intercept("ooo help")

        assert intercept is None
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_frontmatter_missing"
        )
        assert (
            mock_warning.call_args.kwargs["error"] == "missing required frontmatter key: mcp_args"
        )

    def test_resolve_skill_intercept_bypasses_invalid_mcp_tool_frontmatter(
        self,
        tmp_path: Path,
    ) -> None:
        """Invalid `mcp_tool` values disable deterministic intercept."""
        self._write_skill(
            tmp_path,
            "help",
            [
                "name: help",
                'description: "Full reference guide for Ouroboros commands and agents"',
                'mcp_tool: "ouroboros help"',
                "mcp_args:",
                '  query: "$1"',
            ],
        )
        runtime = CodexCliRuntime(cli_path="codex", skills_dir=tmp_path)

        with patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning:
            intercept = runtime._resolve_skill_intercept("ooo help topic")

        assert intercept is None
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_frontmatter_invalid"
        )
        assert (
            mock_warning.call_args.kwargs["error"]
            == "mcp_tool must contain only letters, digits, and underscores"
        )

    @pytest.mark.asyncio
    async def test_execute_task_streams_messages_and_final_result(self) -> None:
        """Streams parsed JSON events and returns the final output file content."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Final answer", encoding="utf-8")
            return _FakeProcess(
                stdout_lines=[
                    json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "content": [{"text": "Working..."}],
                            },
                        }
                    ),
                ],
                stderr_lines=[],
                returncode=0,
            )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            messages = [message async for message in runtime.execute_task("Do the work")]

        assert [message.type for message in messages] == ["system", "assistant", "result"]
        assert messages[-1].content == "Final answer"
        assert messages[-1].resume_handle is not None
        assert messages[-1].resume_handle.native_session_id == "thread-123"

    @pytest.mark.asyncio
    async def test_execute_task_falls_through_when_intercept_frontmatter_is_invalid(
        self,
        tmp_path: Path,
    ) -> None:
        """Invalid frontmatter bypasses intercept and preserves the original prompt."""
        self._write_skill(
            tmp_path,
            "help",
            [
                "name: help",
                'description: "Full reference guide for Ouroboros commands and agents"',
                "mcp_tool: ouroboros_help",
                "mcp_args:",
                '  - "$1"',
            ],
        )
        dispatcher = AsyncMock()
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            assert command[-1] == "ooo help"
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo help")]

        dispatcher.assert_not_awaited()
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_frontmatter_invalid"
        )
        assert (
            mock_warning.call_args.kwargs["error"]
            == "mcp_args must be a mapping with string keys and YAML-safe values"
        )
        assert messages[-1].content == "Codex fallback"

    @pytest.mark.asyncio
    async def test_execute_task_uses_dispatcher_for_valid_intercepts(self, tmp_path: Path) -> None:
        """Exact prefixes with valid frontmatter dispatch before Codex CLI."""
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
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(type="result", content="Intercepted", data={"subtype": "success"}),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        ) as mock_exec:
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        intercept_request = dispatcher.await_args.args[0]
        assert intercept_request.skill_name == "run"
        assert intercept_request.mcp_tool == "ouroboros_execute_seed"
        assert intercept_request.first_argument == "seed.yaml"
        assert intercept_request.mcp_args == {"seed_path": "seed.yaml"}
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == ["Dispatching", "Intercepted"]

    @pytest.mark.asyncio
    async def test_execute_task_uses_builtin_dispatcher_for_run_intercepts(
        self,
        tmp_path: Path,
    ) -> None:
        """`ooo run` dispatches to the local execute-seed MCP handler by default."""
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
        fake_handler = AsyncMock()
        fake_handler.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="Seed Execution SUCCESS"),),
                    meta={
                        "session_id": "sess-123",
                        "execution_id": "exec-456",
                    },
                )
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
        )

        with (
            patch.object(
                runtime, "_get_mcp_tool_handler", return_value=fake_handler
            ) as mock_lookup,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_lookup.assert_called_once_with("ouroboros_execute_seed")
        fake_handler.handle.assert_awaited_once_with({"seed_path": "seed.yaml"})
        mock_exec.assert_not_called()
        assert messages[0].tool_name == "ouroboros_execute_seed"
        assert messages[0].data["tool_input"] == {"seed_path": "seed.yaml"}
        assert messages[1].type == "result"
        assert messages[1].content == "Seed Execution SUCCESS"
        assert messages[1].data["subtype"] == "success"
        assert messages[1].data["session_id"] == "sess-123"
        assert messages[1].data["execution_id"] == "exec-456"

    @pytest.mark.asyncio
    async def test_execute_task_falls_back_when_builtin_dispatcher_returns_recoverable_error(
        self,
        tmp_path: Path,
    ) -> None:
        """Recoverable local MCP errors fall back to normal Codex execution."""
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
        fake_handler = AsyncMock()
        fake_handler.handle = AsyncMock(
            return_value=Result.err(
                MCPToolError(
                    "Seed tool unavailable",
                    tool_name="ouroboros_execute_seed",
                )
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            assert command[-1] == "ooo run seed.yaml"
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

        with (
            patch.object(runtime, "_get_mcp_tool_handler", return_value=fake_handler),
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        fake_handler.handle.assert_awaited_once_with({"seed_path": "seed.yaml"})
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_dispatch_failed"
        assert mock_warning.call_args.kwargs["error_type"] == "MCPToolError"
        assert mock_warning.call_args.kwargs["error"] == "Seed tool unavailable"
        assert mock_warning.call_args.kwargs["recoverable"] is True
        assert messages[-1].content == "Codex fallback"

    @pytest.mark.asyncio
    async def test_execute_task_falls_through_on_recoverable_dispatch_failure(
        self,
        tmp_path: Path,
    ) -> None:
        """Recoverable MCP dispatch errors should fall through to the Codex CLI."""
        skill_md = self._write_skill(
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
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(
                    type="result",
                    content="Tool call timed out",
                    data={
                        "subtype": "error",
                        "recoverable": True,
                        "error_type": "MCPTimeoutError",
                    },
                ),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            assert command[-1] == "ooo run seed.yaml"
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback after timeout", encoding="utf-8")
            return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_dispatch_failed"
        assert mock_warning.call_args.kwargs["skill"] == "run"
        assert mock_warning.call_args.kwargs["tool"] == "ouroboros_execute_seed"
        assert mock_warning.call_args.kwargs["command_prefix"] == "ooo run"
        assert mock_warning.call_args.kwargs["path"] == str(skill_md)
        assert mock_warning.call_args.kwargs["recoverable"] is True
        assert mock_warning.call_args.kwargs["error_type"] == "MCPTimeoutError"
        assert mock_warning.call_args.kwargs["error"] == "Tool call timed out"
        assert messages[-1].content == "Codex fallback after timeout"

    @pytest.mark.asyncio
    async def test_execute_task_dispatches_interview_with_initial_context(
        self,
        tmp_path: Path,
    ) -> None:
        """`ooo interview` resolves templates before dispatching to the tool handler."""
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
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Starting interview"),
                AgentMessage(
                    type="result", content="Interview started", data={"subtype": "success"}
                ),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        ) as mock_exec:
            messages = [
                message
                async for message in runtime.execute_task('ooo interview "Build a REST API"')
            ]

        dispatcher.assert_awaited_once()
        intercept_request = dispatcher.await_args.args[0]
        assert intercept_request.mcp_tool == "ouroboros_interview"
        assert intercept_request.first_argument == "Build a REST API"
        assert intercept_request.mcp_args == {
            "initial_context": "Build a REST API",
            "cwd": "/tmp/project",
        }
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == [
            "Starting interview",
            "Interview started",
        ]

    @pytest.mark.asyncio
    async def test_execute_task_passes_runtime_handle_into_interview_dispatcher(
        self,
        tmp_path: Path,
    ) -> None:
        """Interview intercepts forward the current runtime handle for session reuse."""
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
        resume_handle = RuntimeHandle(
            backend="codex_cli",
            native_session_id="thread-123",
            metadata={"ouroboros_interview_session_id": "interview-123"},
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Continuing interview"),
                AgentMessage(type="result", content="Next question", data={"subtype": "success"}),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        ) as mock_exec:
            messages = [
                message
                async for message in runtime.execute_task(
                    'ooo interview "Use PostgreSQL"',
                    resume_handle=resume_handle,
                )
            ]

        dispatcher.assert_awaited_once()
        assert dispatcher.await_args.args[1] == resume_handle
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == [
            "Continuing interview",
            "Next question",
        ]

    @pytest.mark.asyncio
    async def test_execute_task_preserves_nonrecoverable_dispatch_errors(
        self,
        tmp_path: Path,
    ) -> None:
        """Non-recoverable intercepted errors should be returned directly."""
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
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(
                    type="result",
                    content="Seed validation failed",
                    data={"subtype": "error", "error_type": "MCPToolError"},
                ),
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        ) as mock_exec:
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == [
            "Dispatching",
            "Seed validation failed",
        ]
        assert messages[-1].is_error is True

    @pytest.mark.asyncio
    async def test_execute_task_logs_dispatch_failure_context_and_falls_back(
        self,
        tmp_path: Path,
    ) -> None:
        """Intercept dispatcher failures warn with context and fall through to Codex."""
        skill_md = self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification through the workflow engine"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
                '  mode: "fast"',
            ],
        )
        dispatcher = AsyncMock(side_effect=RuntimeError("tool unavailable"))
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            assert command[-1] == "ooo run seed.yaml"
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_dispatch_failed"
        assert mock_warning.call_args.kwargs["skill"] == "run"
        assert mock_warning.call_args.kwargs["tool"] == "ouroboros_execute_seed"
        assert mock_warning.call_args.kwargs["command_prefix"] == "ooo run"
        assert mock_warning.call_args.kwargs["path"] == str(skill_md)
        assert mock_warning.call_args.kwargs["first_argument"] == "seed.yaml"
        assert mock_warning.call_args.kwargs["prompt_preview"] == "ooo run seed.yaml"
        assert mock_warning.call_args.kwargs["mcp_arg_keys"] == ("mode", "seed_path")
        assert mock_warning.call_args.kwargs["mcp_args_preview"] == {
            "seed_path": "seed.yaml",
            "mode": "fast",
        }
        assert mock_warning.call_args.kwargs["fallback"] == "pass_through_to_codex"
        assert mock_warning.call_args.kwargs["error_type"] == "RuntimeError"
        assert mock_warning.call_args.kwargs["error"] == "tool unavailable"
        assert mock_warning.call_args.kwargs["exc_info"] is True
        assert messages[-1].content == "Codex fallback"

    @pytest.mark.asyncio
    async def test_execute_task_falls_through_when_interview_intercept_dispatcher_raises(
        self,
        tmp_path: Path,
    ) -> None:
        """Dispatcher failures log a warning and pass `ooo interview` through to Codex."""
        self._write_skill(
            tmp_path,
            "interview",
            [
                "name: interview",
                'description: "Socratic interview to crystallize vague requirements"',
                "mcp_tool: ouroboros_interview",
                "mcp_args:",
                '  initial_context: "$1"',
            ],
        )
        dispatcher = AsyncMock(side_effect=RuntimeError("Interview session unavailable"))
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            assert command[-1] == 'ooo interview "Build a REST API"'
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            return _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [
                message
                async for message in runtime.execute_task('ooo interview "Build a REST API"')
            ]

        dispatcher.assert_awaited_once()
        intercept_request = dispatcher.await_args.args[0]
        assert intercept_request.skill_name == "interview"
        assert intercept_request.mcp_tool == "ouroboros_interview"
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_dispatch_failed"
        assert mock_warning.call_args.kwargs["skill"] == "interview"
        assert mock_warning.call_args.kwargs["tool"] == "ouroboros_interview"
        assert mock_warning.call_args.kwargs["error"] == "Interview session unavailable"
        assert messages[-1].content == "Codex fallback"

    def test_template_resolver_returns_empty_string_for_null_first_argument(
        self, tmp_path: Path
    ) -> None:
        """$1 resolves to empty string when no argument is given, not None."""
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                'description: "Execute a Seed specification"',
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        runtime = CodexCliRuntime(cli_path="codex", skills_dir=tmp_path)

        intercept = runtime._resolve_skill_intercept("ooo run")

        assert intercept is not None
        # $1 with no argument should be empty string, not None
        assert intercept.mcp_args["seed_path"] == ""
        assert intercept.first_argument is None

    def test_llm_backend_propagated_to_builtin_handlers(self) -> None:
        """llm_backend param is used in _get_builtin_mcp_handlers, not hardcoded."""
        runtime = CodexCliRuntime(cli_path="codex", llm_backend="litellm")
        assert runtime._llm_backend == "litellm"

    @pytest.mark.asyncio
    async def test_execute_task_file_not_found_yields_error(self) -> None:
        """FileNotFoundError when codex binary is missing yields an error result."""
        runtime = CodexCliRuntime(cli_path="/nonexistent/codex", cwd="/tmp/project")

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("/nonexistent/codex"),
        ):
            messages = [message async for message in runtime.execute_task("hello")]

        assert len(messages) == 1
        assert messages[0].type == "result"
        assert messages[0].is_error
        assert (
            "not found" in messages[0].content.lower() or "FileNotFoundError" in messages[0].content
        )
