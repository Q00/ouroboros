"""Unit tests for CodexCliRuntime."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.config.models import OuroborosConfig
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
import ouroboros.orchestrator.codex_cli_runtime as codex_cli_runtime_module
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.router import Resolved, ResolveRequest
from ouroboros.router.dispatch import SkillDispatchRouter as SharedSkillDispatchRouter

_EXPECTED_CODEX_PATH = str(Path("/usr/local/bin/codex"))
_EXPECTED_PROJECT_CWD = str(Path("/tmp/project"))


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        encoded = "".join(f"{line}\n" for line in lines).encode()
        self._buffer = bytearray(encoded)

    async def readline(self) -> bytes:
        if not self._buffer:
            return b""
        newline_index = self._buffer.find(b"\n")
        if newline_index < 0:
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        data = bytes(self._buffer[: newline_index + 1])
        del self._buffer[: newline_index + 1]
        return data

    async def read(self, n: int = -1) -> bytes:
        if not self._buffer:
            return b""
        if n < 0 or n >= len(self._buffer):
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data


class _FailingReadlineStream(_FakeStream):
    async def readline(self) -> bytes:
        msg = "readline() should not be used for Codex CLI stream parsing"
        raise AssertionError(msg)


class _FakeStdin:
    """Fake stdin that captures written data."""

    def __init__(self) -> None:
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProcess:
    def __init__(
        self,
        stdout_lines: list[str],
        stderr_lines: list[str],
        returncode: int = 0,
        *,
        stdout_stream: _FakeStream | None = None,
        stderr_stream: _FakeStream | None = None,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = stdout_stream or _FakeStream(stdout_lines)
        self.stderr = stderr_stream or _FakeStream(stderr_lines)
        self._returncode = returncode

    async def wait(self) -> int:
        return self._returncode


class _BlockingStream:
    async def readline(self) -> bytes:
        await asyncio.Future()  # type: ignore[misc]
        return b""  # unreachable, satisfies mypy

    async def read(self, n: int = -1) -> bytes:
        del n
        await asyncio.Future()  # type: ignore[misc]
        return b""  # unreachable, satisfies mypy


class _TerminableProcess:
    def __init__(self) -> None:
        self.stdout = _BlockingStream()
        self.stderr = _BlockingStream()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._done = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return -1 if self.returncode is None else self.returncode


class _ControlledBlockingStream:
    def __init__(self, done: asyncio.Event) -> None:
        self._done = done

    async def readline(self) -> bytes:
        await self._done.wait()
        return b""

    async def read(self, n: int = -1) -> bytes:
        del n
        await self._done.wait()
        return b""


class _TimeoutTerminableProcess:
    def __init__(self) -> None:
        self._done = asyncio.Event()
        self.stdout = _ControlledBlockingStream(self._done)
        self.stderr = _ControlledBlockingStream(self._done)
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return -1 if self.returncode is None else self.returncode


class TestCodexCliRuntime:
    """Tests for CodexCliRuntime."""

    @staticmethod
    def _write_wrapper(path: Path) -> Path:
        path.write_bytes(b"\xcf\xfa\xed\xfe")
        path.chmod(0o755)
        return path

    @staticmethod
    def _write_real_cli(path: Path) -> Path:
        path.write_text("#!/usr/bin/env node\nconsole.log('codex')\n", encoding="utf-8")
        path.chmod(0o755)
        return path

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
        """Builds a new-session exec command (prompt fed via stdin, not args)."""
        runtime = CodexCliRuntime(
            cli_path="/usr/local/bin/codex",
            permission_mode="acceptEdits",
            model="o3",
            cwd="/tmp/project",
        )

        command = runtime._build_command(
            output_last_message_path="/tmp/out.txt",
        )

        assert command[:2] == [_EXPECTED_CODEX_PATH, "exec"]
        assert "--json" in command
        assert "--full-auto" in command
        assert "--model" in command
        assert "o3" in command
        assert "-C" in command
        assert _EXPECTED_PROJECT_CWD in command

    def test_build_command_for_resume(self) -> None:
        """Builds an exec resume command when a session id is provided."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

        command = runtime._build_command(
            output_last_message_path="/tmp/out.txt",
            resume_session_id="thread-123",
        )

        assert command[:2] == ["codex", "exec"]
        assert command[-2:] == ["resume", "thread-123"]
        resume_index = command.index("resume")
        assert command.index("--json") < resume_index
        assert command.index("--skip-git-repo-check") < resume_index
        assert command.index("--output-last-message") < resume_index
        assert command.index("-C") < resume_index
        assert command[command.index("-C") + 1] == _EXPECTED_PROJECT_CWD

    def test_build_command_uses_profile_for_runtime_session_role(self) -> None:
        """Agent runtime sessions should resolve Codex profiles from session_role."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )
        config = OuroborosConfig(
            llm_profiles={
                "standard": {
                    "providers": {"codex": {"profile": "ouroboros-standard"}},
                },
            },
            llm_role_profiles={"agent_runtime_implementation": "standard"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--profile" in command
        assert command[command.index("--profile") + 1] == "ouroboros-standard"
        assert "--model" not in command

    def test_build_command_uses_default_runtime_profile_for_resumed_roleless_handle(self) -> None:
        """Resumed role-less agent_runtime handles keep using the documented fallback role."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="agent_runtime",
            native_session_id="thread-123",
            metadata={},
        )
        config = OuroborosConfig(
            llm_profiles={
                "standard": {
                    "providers": {"codex": {"profile": "ouroboros-standard"}},
                },
            },
            llm_role_profiles={"agent_runtime": "standard"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
                resume_session_id="thread-123",
            )

        assert "--profile" in command
        assert command[command.index("--profile") + 1] == "ouroboros-standard"
        assert "--model" not in command

    def test_build_command_does_not_double_prefix_prefixed_runtime_handle_kind(self) -> None:
        """Already-prefixed runtime handle kinds are treated as logical role keys."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="agent_runtime_evaluation",
            metadata={},
        )
        config = OuroborosConfig(
            llm_profiles={
                "deep": {
                    "providers": {"codex": {"profile": "ouroboros-deep"}},
                },
            },
            llm_role_profiles={"agent_runtime_evaluation": "deep"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--profile" in command
        assert command[command.index("--profile") + 1] == "ouroboros-deep"
        assert "--model" not in command

    def test_build_command_uses_runtime_profile_provider_model_fallback(self) -> None:
        """Codex runtime profiles without Codex-native profile anchors should use models."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )
        config = OuroborosConfig(
            llm_profiles={
                "standard": {
                    "providers": {"codex": {"model": "gpt-5.3-codex"}},
                },
            },
            llm_role_profiles={"agent_runtime_implementation": "standard"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--model" in command
        assert command[command.index("--model") + 1] == "gpt-5.3-codex"
        assert "--profile" not in command

    def test_build_command_uses_runtime_profile_top_level_model_fallback(self) -> None:
        """Agent runtime should honor provider-neutral profile model fallback."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )
        config = OuroborosConfig(
            llm_profiles={"standard": {"model": "gpt-5.3-codex"}},
            llm_role_profiles={"agent_runtime_implementation": "standard"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--model" in command
        assert command[command.index("--model") + 1] == "gpt-5.3-codex"
        assert "--profile" not in command

    def test_build_command_explicit_model_wins_over_runtime_profile(self) -> None:
        """Explicit runtime model overrides keep existing --model behavior."""
        runtime = CodexCliRuntime(cli_path="codex", model="gpt-5.5", cwd="/tmp/project")
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            metadata={"session_role": "implementation"},
        )
        config = OuroborosConfig(
            llm_profiles={
                "standard": {
                    "providers": {"codex": {"profile": "ouroboros-standard"}},
                },
            },
            llm_role_profiles={"agent_runtime_implementation": "standard"},
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--model" in command
        assert command[command.index("--model") + 1] == "gpt-5.5"
        assert "--profile" not in command

    def test_build_command_uses_explicit_runtime_profile_metadata(self) -> None:
        """Runtime metadata can directly select an Ouroboros profile."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="evaluation_session",
            metadata={"llm_profile": "deep"},
        )
        config = OuroborosConfig(
            llm_profiles={
                "deep": {
                    "providers": {"codex": {"profile": "ouroboros-deep"}},
                },
            },
        )

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--profile" in command
        assert command[command.index("--profile") + 1] == "ouroboros-deep"

    def test_build_command_uses_explicit_runtime_profile_model_fallback(self) -> None:
        """Explicit Ouroboros profile metadata should still fall back to model."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        runtime_handle = RuntimeHandle(
            backend="codex_cli",
            kind="evaluation_session",
            metadata={"llm_profile": "deep"},
        )
        config = OuroborosConfig(llm_profiles={"deep": {"model": "gpt-5.5"}})

        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                runtime_handle=runtime_handle,
            )

        assert "--model" in command
        assert command[command.index("--model") + 1] == "gpt-5.5"
        assert "--profile" not in command

    def test_resolve_cli_path_falls_back_from_wrapper(self, tmp_path: Path) -> None:
        """Runtime should bypass wrappers the same way provider adapters do."""
        wrapper = self._write_wrapper(tmp_path / "codex-wrapper")
        real_dir = tmp_path / "bin"
        real_dir.mkdir()
        real_cli = self._write_real_cli(real_dir / "codex")

        with (
            patch.dict(os.environ, {"PATH": str(real_dir)}),
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch("ouroboros.orchestrator.codex_cli_runtime.log.info") as mock_info,
        ):
            runtime = CodexCliRuntime(cli_path=wrapper)

        assert runtime._cli_path == str(real_cli)
        mock_warning.assert_called_once_with(
            "codex_cli_runtime.cli_wrapper_detected",
            wrapper_path=str(wrapper),
            hint="Searching PATH for the real Node.js codex CLI.",
        )
        mock_info.assert_any_call(
            "codex_cli_runtime.cli_resolved_via_fallback",
            fallback_path=str(real_cli),
        )

    def test_build_command_uses_read_only_for_default_permission_mode(self) -> None:
        """Default permission mode keeps the runtime in read-only mode."""
        runtime = CodexCliRuntime(cli_path="codex", permission_mode="default")

        command = runtime._build_command(
            output_last_message_path="/tmp/out.txt",
        )

        assert "--sandbox" in command
        assert "read-only" in command

    def test_build_command_uses_dangerous_bypass_for_bypass_permissions(self) -> None:
        """bypassPermissions uses Codex's no-approval/no-sandbox mode."""
        runtime = CodexCliRuntime(cli_path="codex", permission_mode="bypassPermissions")

        command = runtime._build_command(
            output_last_message_path="/tmp/out.txt",
        )

        assert "--dangerously-bypass-approvals-and-sandbox" in command

    @pytest.mark.asyncio
    async def test_execute_task_marks_resume_bootstrap_failures_recoverable(self) -> None:
        """Resume failures before any Codex event should stay retryable."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            del command, kwargs
            return _FakeProcess(
                stdout_lines=[],
                stderr_lines=["error: unexpected argument '-C' found"],
                returncode=2,
            )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            messages = [
                message
                async for message in runtime.execute_task(
                    "resume the task",
                    resume_session_id="thread-123",
                )
            ]

        assert len(messages) == 1
        assert messages[0].is_error
        assert messages[0].data["error_type"] == "CodexCliError"
        assert messages[0].data["recoverable"] is True
        assert messages[0].data["recovery"]["kind"] == "resume_retry"
        assert messages[0].data["recovery"]["resume_session_id"] == "thread-123"

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

    def test_convert_thread_started_event_preserves_existing_handle_metadata(self) -> None:
        """Fresh runtime handles retain pre-seeded scope metadata when the thread starts."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        seeded_handle = RuntimeHandle(
            backend="codex_cli",
            kind="level_coordinator",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "level",
                "level_number": 2,
                "session_role": "coordinator",
            },
        )

        messages = runtime._convert_event(
            {"type": "thread.started", "thread_id": "thread-123"},
            current_handle=seeded_handle,
        )

        assert len(messages) == 1
        message = messages[0]
        assert message.resume_handle is not None
        assert message.resume_handle.native_session_id == "thread-123"
        assert message.resume_handle.kind == "level_coordinator"
        assert message.resume_handle.cwd == seeded_handle.cwd
        assert message.resume_handle.approval_mode == "acceptEdits"
        assert message.resume_handle.metadata == seeded_handle.metadata

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

    def test_runtime_does_not_expose_local_dispatch_parser_helpers(self) -> None:
        """Dispatch parsing and metadata resolution live in the shared router."""
        obsolete_helpers = {
            "_extract_first_argument",
            "_load_skill_frontmatter",
            "_normalize_mcp_frontmatter",
            "_resolve_dispatch_templates",
            "_resolve_skill_dispatch",
            "_resolve_skill_intercept",
        }

        assert obsolete_helpers.isdisjoint(dir(CodexCliRuntime))

    def test_runtime_source_does_not_reference_removed_dispatch_parser_helpers(self) -> None:
        """Removed local parser helpers should not remain referenced by the runtime."""
        runtime_source = inspect.getsource(codex_cli_runtime_module)
        obsolete_helper_references = {
            "_extract_first_argument(",
            "_load_skill_frontmatter(",
            "_normalize_mcp_frontmatter(",
            "_resolve_dispatch_templates(",
            "_resolve_skill_dispatch(",
            "_resolve_skill_intercept(",
            "SkillInterceptRequest",
        }

        assert all(reference not in runtime_source for reference in obsolete_helper_references)

    @pytest.mark.asyncio
    async def test_execute_task_routes_ooo_input_through_shared_stateless_router(
        self,
        tmp_path: Path,
    ) -> None:
        """Codex CLI runtime should pass through the router's Resolved result."""
        resolved_sentinel = Resolved(
            skill_name="router-skill",
            command_prefix="ooo router-skill",
            prompt="ooo run seed.yaml",
            skill_path=tmp_path / "router-skill" / "SKILL.md",
            mcp_tool="router_only_tool",
            mcp_args={
                "seed_path": "resolved-by-router.yaml",
                "nested": {"source": "router"},
            },
            first_argument="resolved-first-argument",
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

        with (
            patch.object(
                SharedSkillDispatchRouter,
                "resolve",
                autospec=True,
                return_value=resolved_sentinel,
            ) as mock_resolve,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_resolve.assert_called_once()
        assert isinstance(mock_resolve.call_args.args[0], SharedSkillDispatchRouter)
        request = mock_resolve.call_args.args[1]
        assert isinstance(request, ResolveRequest)
        assert request.prompt == "ooo run seed.yaml"
        assert request.cwd == _EXPECTED_PROJECT_CWD
        assert request.skills_dir == tmp_path
        dispatcher.assert_awaited_once()
        intercept_request = dispatcher.await_args.args[0]
        assert intercept_request is resolved_sentinel
        assert dispatcher.await_args.args[1] is None
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == ["Dispatching", "Intercepted"]

    @pytest.mark.asyncio
    async def test_execute_task_builtin_dispatcher_consumes_resolved_router_result(
        self,
        tmp_path: Path,
    ) -> None:
        """Built-in dispatch should consume Resolved metadata without re-parsing."""
        resolved = Resolved(
            skill_name="router-skill",
            command_prefix="ooo router-skill",
            prompt="ooo run prompt-derived.yaml",
            skill_path=tmp_path / "router-skill" / "SKILL.md",
            mcp_tool="router_only_tool",
            mcp_args={
                "seed_path": "resolved-by-router.yaml",
                "nested": {"source": "router"},
            },
            first_argument="resolved-first-argument",
        )
        fake_handler = AsyncMock()
        fake_handler.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="Router dispatch"),),
                    meta={"execution_id": "exec-router"},
                )
            )
        )
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
        )

        with (
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.resolve_skill_dispatch",
                return_value=resolved,
            ) as mock_resolve,
            patch.object(
                runtime, "_get_mcp_tool_handler", return_value=fake_handler
            ) as mock_lookup,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [
                message async for message in runtime.execute_task("ooo run prompt-derived.yaml")
            ]

        mock_resolve.assert_called_once()
        mock_lookup.assert_called_once_with("router_only_tool")
        fake_handler.handle.assert_awaited_once_with(
            {
                "seed_path": "resolved-by-router.yaml",
                "nested": {"source": "router"},
            }
        )
        mock_exec.assert_not_called()
        assert messages[0].tool_name == "router_only_tool"
        assert messages[0].data["tool_input"] == {
            "seed_path": "resolved-by-router.yaml",
            "nested": {"source": "router"},
        }
        assert messages[0].data["skill_name"] == "router-skill"
        assert messages[0].data["command_prefix"] == "ooo router-skill"
        assert messages[1].content == "Router dispatch"
        assert messages[1].data["execution_id"] == "exec-router"

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
    async def test_execute_task_handles_large_jsonl_events_without_readline(self) -> None:
        """Large Codex JSONL events should stream without relying on readline()."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        large_text = "A" * 200_000

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Final answer", encoding="utf-8")
            stdout_lines = [
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "content": [{"text": large_text}],
                        },
                    }
                ),
            ]
            return _FakeProcess(
                stdout_lines=[],
                stderr_lines=[],
                returncode=0,
                stdout_stream=_FailingReadlineStream(stdout_lines),
                stderr_stream=_FailingReadlineStream([]),
            )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            messages = [message async for message in runtime.execute_task("Do the work")]

        assert [message.type for message in messages] == ["system", "assistant", "result"]
        assert messages[1].content == large_text
        assert messages[-1].content == "Final answer"

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

        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            # Prompt is now fed via stdin, not as CLI arg
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo help")]

        assert captured_processes[0].stdin.written == b"ooo help"
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
    async def test_execute_task_logs_legacy_frontmatter_missing_event_name(
        self,
        tmp_path: Path,
    ) -> None:
        """Missing MCP metadata preserves the legacy Codex structured log event."""
        self._write_skill(
            tmp_path,
            "help",
            [
                "name: help",
                'description: "Full reference guide for Ouroboros commands and agents"',
                "mcp_tool: ouroboros_help",
            ],
        )
        dispatcher = AsyncMock()
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )
        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo help")]

        assert captured_processes[0].stdin.written == b"ooo help"
        dispatcher.assert_not_awaited()
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "codex_cli_runtime.skill_intercept_frontmatter_missing"
        )
        assert (
            mock_warning.call_args.kwargs["error"] == "missing required frontmatter key: mcp_args"
        )
        assert messages[-1].content == "Codex fallback"

    @pytest.mark.asyncio
    async def test_execute_task_falls_through_when_router_returns_not_handled(
        self,
        tmp_path: Path,
    ) -> None:
        """Router NotHandled outcomes preserve normal Codex pass-through behavior."""
        dispatcher = AsyncMock()
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )
        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch("ouroboros.orchestrator.codex_cli_runtime.log.info") as mock_info,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo missing seed.yaml")]

        assert captured_processes[0].stdin.written == b"ooo missing seed.yaml"
        dispatcher.assert_not_awaited()
        mock_exec.assert_called_once()
        mock_warning.assert_not_called()
        mock_info.assert_called_once()
        assert mock_info.call_args.args[0] == "codex_cli_runtime.task_started"
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

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch("ouroboros.orchestrator.codex_cli_runtime.log.info") as mock_info,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        intercept_request = dispatcher.await_args.args[0]
        assert isinstance(intercept_request, Resolved)
        assert intercept_request.skill_name == "run"
        assert intercept_request.mcp_tool == "ouroboros_execute_seed"
        assert intercept_request.first_argument == "seed.yaml"
        assert intercept_request.mcp_args == {"seed_path": "seed.yaml"}
        mock_exec.assert_not_called()
        mock_warning.assert_not_called()
        mock_info.assert_not_called()
        assert [message.content for message in messages] == ["Dispatching", "Intercepted"]

    @pytest.mark.asyncio
    async def test_execute_task_uses_dispatcher_for_slash_prefix_intercepts(
        self, tmp_path: Path
    ) -> None:
        """Legacy slash prefixes remain routed through the shared router."""
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
            messages = [
                message async for message in runtime.execute_task("/ouroboros:run seed.yaml")
            ]

        dispatcher.assert_awaited_once()
        intercept_request = dispatcher.await_args.args[0]
        assert isinstance(intercept_request, Resolved)
        assert intercept_request.skill_name == "run"
        assert intercept_request.command_prefix == "/ouroboros:run"
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

        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch.object(runtime, "_get_mcp_tool_handler", return_value=fake_handler),
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        assert captured_processes[0].stdin.written == b"ooo run seed.yaml"
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

        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback after timeout", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        assert captured_processes[0].stdin.written == b"ooo run seed.yaml"
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
    async def test_execute_task_terminates_child_process_when_cancelled(self) -> None:
        """Cancelling task consumption should terminate the spawned Codex process."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        process = _TerminableProcess()

        async def _consume() -> list[AgentMessage]:
            return [message async for message in runtime.execute_task("Do the work")]

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ):
            consumer = asyncio.create_task(_consume())
            await asyncio.sleep(0)
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer

        assert process.terminated or process.killed

    @pytest.mark.asyncio
    async def test_execute_task_times_out_when_codex_never_emits_output(self) -> None:
        """Silent Codex startups should fail fast instead of hanging forever."""
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        runtime._startup_output_timeout_seconds = 0.01
        runtime._stdout_idle_timeout_seconds = 0.01
        process = _TimeoutTerminableProcess()

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ):
            messages = [message async for message in runtime.execute_task("Do the work")]

        assert len(messages) == 1
        assert messages[0].type == "result"
        assert messages[0].is_error
        assert messages[0].data["error_type"] == "TimeoutError"
        assert process.terminated or process.killed

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
            "cwd": _EXPECTED_PROJECT_CWD,
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
    async def test_execute_task_local_interview_dispatch_preserves_resume_handle(
        self,
        tmp_path: Path,
    ) -> None:
        """Local interview dispatch reuses the native runtime handle and interview session."""
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
        resume_handle = RuntimeHandle(
            backend="codex_cli",
            native_session_id="thread-123",
            metadata={"ouroboros_interview_session_id": "interview-123"},
        )

        class _FakeInterviewHandler:
            def __init__(self) -> None:
                self.calls: list[dict[str, str]] = []

            async def handle(
                self, arguments: dict[str, str]
            ) -> Result[MCPToolResult, MCPToolError]:
                self.calls.append(arguments)
                return Result.ok(
                    MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text="Next question"),),
                        is_error=False,
                        meta={"session_id": "interview-456"},
                    )
                )

        handler = _FakeInterviewHandler()
        runtime = CodexCliRuntime(
            cli_path="codex",
            cwd="/tmp/project",
            skills_dir=tmp_path,
        )
        runtime._builtin_mcp_handlers = {"ouroboros_interview": handler}

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

        mock_exec.assert_not_called()
        # Resume must drop initial_context so InterviewHandler branches on
        # session_id instead of restarting a new interview.
        assert len(handler.calls) == 1
        call_args = handler.calls[0]
        assert call_args["session_id"] == "interview-123"
        assert call_args["answer"] == "Use PostgreSQL"
        assert "initial_context" not in call_args
        assert messages[0].resume_handle is not None
        assert messages[0].resume_handle.native_session_id == "thread-123"
        assert messages[-1].resume_handle is not None
        assert messages[-1].resume_handle.native_session_id == "thread-123"
        assert (
            messages[-1].resume_handle.metadata["ouroboros_interview_session_id"] == "interview-456"
        )
        assert messages[-1].content == "Next question"

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

        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

        with (
            patch("ouroboros.orchestrator.codex_cli_runtime.log.warning") as mock_warning,
            patch("ouroboros.orchestrator.codex_cli_runtime.log.info") as mock_info,
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        assert captured_processes[0].stdin.written == b"ooo run seed.yaml"
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
        mock_info.assert_called_once()
        assert mock_info.call_args.args[0] == "codex_cli_runtime.task_started"
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

        captured_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> _FakeProcess:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text("Codex fallback", encoding="utf-8")
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
            captured_processes.append(proc)
            return proc

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

        assert captured_processes[0].stdin.written == b'ooo interview "Build a REST API"'
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
