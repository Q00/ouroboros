"""Unit tests for HermesCliRuntime."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.hermes_runtime import HermesCliRuntime, _parse_quiet_output


class _FakeStream:
    def __init__(self, text: str) -> None:
        self._buffer = bytearray(text.encode("utf-8"))

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


class _FakeProcess:
    def __init__(
        self,
        stdout: str,
        stderr: str = "",
        returncode: int = 0,
        *,
        stdout_stream: _FakeStream | None = None,
        stderr_stream: _FakeStream | None = None,
    ) -> None:
        self.stdout = stdout_stream or _FakeStream(stdout)
        self.stderr = stderr_stream or _FakeStream(stderr)
        self.returncode: int | None = returncode

    async def wait(self) -> int:
        return 0 if self.returncode is None else self.returncode


class _ControlledBlockingStream:
    def __init__(self, done: asyncio.Event) -> None:
        self._done = done

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


class _FakeHandler:
    def __init__(self, result: MCPToolResult) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def handle(self, arguments: dict[str, object]) -> object:
        self.calls.append(arguments)
        return Result.ok(self._result)


class TestHermesCliRuntime:
    """Tests for HermesCliRuntime."""

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

    def test_runtime_properties(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        assert runtime.runtime_backend == "hermes_cli"
        assert runtime.working_directory == "/tmp/project"
        assert runtime.permission_mode == "default"

    def test_constructor_accepts_llm_backend(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", llm_backend="opencode")
        assert runtime._llm_backend == "opencode"

    def test_resolve_skill_intercept_requires_exact_prefix_match(self, tmp_path: Path) -> None:
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        runtime = HermesCliRuntime(cli_path="hermes", skills_dir=tmp_path)

        intercept = runtime._resolve_skill_intercept('ooo run "seed spec.yaml"')

        assert intercept is not None
        assert intercept.skill_name == "run"
        assert intercept.command_prefix == "ooo run"
        assert intercept.first_argument == "seed spec.yaml"
        assert intercept.mcp_args == {"seed_path": "seed spec.yaml"}
        assert runtime._resolve_skill_intercept('please ooo run "seed spec.yaml"') is None

    def test_resolve_skill_intercept_maps_interview_argument_to_initial_context(
        self,
        tmp_path: Path,
    ) -> None:
        self._write_skill(
            tmp_path,
            "interview",
            [
                "name: interview",
                "mcp_tool: ouroboros_interview",
                "mcp_args:",
                '  initial_context: "$1"',
                '  cwd: "$CWD"',
            ],
        )
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project", skills_dir=tmp_path)

        intercept = runtime._resolve_skill_intercept('ooo interview "Build a REST API"')

        assert intercept is not None
        assert intercept.mcp_tool == "ouroboros_interview"
        assert intercept.mcp_args == {
            "initial_context": "Build a REST API",
            "cwd": "/tmp/project",
        }

    def test_resolve_skill_intercept_bypasses_unterminated_frontmatter(
        self,
        tmp_path: Path,
    ) -> None:
        skill_dir = tmp_path / "run"
        skill_dir.mkdir(parents=True)
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(
            "---\nname: run\nmcp_tool: ouroboros_execute_seed\n",
            encoding="utf-8",
        )
        runtime = HermesCliRuntime(cli_path="hermes", skills_dir=tmp_path)

        with patch("ouroboros.orchestrator.hermes_runtime.log.warning") as mock_warning:
            intercept = runtime._resolve_skill_intercept("ooo run seed.yaml")

        assert intercept is None
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "hermes_cli_runtime.skill_intercept_frontmatter_invalid"
        )
        assert "Unterminated frontmatter" in mock_warning.call_args.kwargs["error"]

    def test_resolve_skill_intercept_bypasses_non_mapping_frontmatter(
        self,
        tmp_path: Path,
    ) -> None:
        skill_dir = tmp_path / "run"
        skill_dir.mkdir(parents=True)
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(
            "---\n- not\n- a\n- mapping\n---\n",
            encoding="utf-8",
        )
        runtime = HermesCliRuntime(cli_path="hermes", skills_dir=tmp_path)

        with patch("ouroboros.orchestrator.hermes_runtime.log.warning") as mock_warning:
            intercept = runtime._resolve_skill_intercept("ooo run seed.yaml")

        assert intercept is None
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "hermes_cli_runtime.skill_intercept_frontmatter_invalid"
        )
        assert "Frontmatter must be a mapping" in mock_warning.call_args.kwargs["error"]

    def test_build_tool_arguments_reuses_interview_session_from_handle(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        intercept = SimpleNamespace(
            mcp_tool="ouroboros_interview",
            mcp_args={"initial_context": "Build a REST API"},
            first_argument="Next answer",
        )
        handle = RuntimeHandle(
            backend="hermes_cli",
            metadata={"ouroboros_interview_session_id": "interview-123"},
        )

        arguments = runtime._build_tool_arguments(intercept, handle)

        assert arguments["initial_context"] == "Build a REST API"
        assert arguments["session_id"] == "interview-123"
        assert arguments["answer"] == "Next answer"

    @pytest.mark.asyncio
    async def test_dispatch_skill_intercept_attaches_resume_handle_metadata(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project", llm_backend="codex")
        handler = _FakeHandler(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="Question 1"),),
                meta={"session_id": "interview-123"},
            )
        )
        runtime._builtin_mcp_handlers = {"ouroboros_interview": handler}
        intercept = SimpleNamespace(
            skill_name="interview",
            command_prefix="ooo interview",
            mcp_tool="ouroboros_interview",
            mcp_args={"initial_context": "Build a REST API"},
            first_argument=None,
        )

        messages = await runtime._dispatch_skill_intercept_locally(intercept, None)

        assert len(messages) == 2
        assert messages[0].tool_name == "ouroboros_interview"
        assert messages[1].data["subtype"] == "success"
        assert messages[1].resume_handle is not None
        assert (
            messages[1].resume_handle.metadata["ouroboros_interview_session_id"] == "interview-123"
        )

    @pytest.mark.asyncio
    async def test_dispatch_skill_intercept_returns_recoverable_error_tuple(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        fake_handler = AsyncMock()
        fake_handler.handle = AsyncMock(
            return_value=Result.err(
                MCPToolError(
                    "Seed tool unavailable",
                    tool_name="ouroboros_execute_seed",
                )
            )
        )
        runtime._builtin_mcp_handlers = {"ouroboros_execute_seed": fake_handler}
        intercept = SimpleNamespace(
            skill_name="run",
            command_prefix="ooo run",
            mcp_tool="ouroboros_execute_seed",
            mcp_args={"seed_path": "seed.yaml"},
            first_argument="seed.yaml",
        )

        messages = await runtime._dispatch_skill_intercept_locally(intercept, None)

        assert len(messages) == 2
        assert messages[0].tool_name == "ouroboros_execute_seed"
        assert messages[1].data["recoverable"] is True
        assert messages[1].data["error_type"] == "MCPToolError"

    @pytest.mark.asyncio
    async def test_execute_task_parses_session_id_and_returns_handle(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        process = _FakeProcess("Finished work\nsession_id: 20260413_120000_deadbeef\n")

        with patch(
            "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ):
            messages = [message async for message in runtime.execute_task("Do the thing")]

        assert len(messages) == 1
        assert messages[0].content == "Finished work"
        assert messages[0].resume_handle is not None
        assert messages[0].resume_handle.native_session_id == "20260413_120000_deadbeef"

    @pytest.mark.asyncio
    async def test_execute_task_falls_through_on_recoverable_dispatch_failure(
        self,
        tmp_path: Path,
    ) -> None:
        self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
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
        runtime = HermesCliRuntime(
            cli_path="hermes",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )
        process = _FakeProcess("Hermes fallback completed\nsession_id: 20260413_120000_deadbeef\n")

        with (
            patch("ouroboros.orchestrator.hermes_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
                return_value=process,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args[0][0] == "hermes_cli_runtime.skill_intercept_dispatch_failed"
        assert mock_warning.call_args.kwargs["skill"] == "run"
        assert mock_warning.call_args.kwargs["tool"] == "ouroboros_execute_seed"
        assert mock_warning.call_args.kwargs["command_prefix"] == "ooo run"
        assert mock_warning.call_args.kwargs["recoverable"] is True
        assert messages[-1].content == "Hermes fallback completed"

    def test_parse_quiet_output_strips_reasoning_banner(self) -> None:
        content, session_id = _parse_quiet_output(
            "┌─ Reasoning ─────────────────────────────────────────────────────────────┐\n"
            "OK\n\n"
            "session_id: 20260414_101114_37f5fa"
        )

        assert content == "OK"
        assert session_id == "20260414_101114_37f5fa"

    def test_parse_quiet_output_strips_hermes_banner(self) -> None:
        content, session_id = _parse_quiet_output(
            "╭─ ⚕ Hermes ───────────────────────────────────────────────────────────────╮\n"
            "OK\n\n"
            "session_id: 20260414_102135_d38d07"
        )

        assert content == "OK"
        assert session_id == "20260414_102135_d38d07"

    def test_parse_quiet_output_strips_full_reasoning_box(self) -> None:
        content, session_id = _parse_quiet_output(
            "┌─ Reasoning ─────────┐\n"
            "│ think step 1       │\n"
            "│ think step 2       │\n"
            "└────────────────────┘\n"
            "\n"
            "Final answer\n"
            "session_id: 20260413_120000_deadbeef"
        )

        assert content == "Final answer"
        assert session_id == "20260413_120000_deadbeef"

    def test_parse_quiet_output_preserves_text_after_session_marker(self) -> None:
        content, session_id = _parse_quiet_output(
            "alpha\nsession_id: 20260414_102135_d38d07\nomega"
        )

        assert content == "alpha\nomega"
        assert session_id == "20260414_102135_d38d07"

    def test_parse_quiet_output_without_session_id_preserves_plain_text(self) -> None:
        content, session_id = _parse_quiet_output("Plain response")

        assert content == "Plain response"
        assert session_id is None

    @pytest.mark.asyncio
    async def test_execute_task_returns_error_result_on_nonzero_exit(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        process = _FakeProcess("", stderr="boom", returncode=1)
        handle = RuntimeHandle(backend="hermes_cli", native_session_id="session-123")

        with patch(
            "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ):
            messages = [
                message async for message in runtime.execute_task("Do the thing", handle=handle)
            ]

        assert len(messages) == 1
        assert messages[0].data["subtype"] == "error"
        assert messages[0].content == "Hermes execution failed:\nboom"
        assert messages[0].resume_handle == handle

    @pytest.mark.asyncio
    async def test_execute_task_times_out_when_hermes_never_emits_output(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        runtime._startup_output_timeout_seconds = 0.01
        runtime._stdout_idle_timeout_seconds = 0.01
        process = _TimeoutTerminableProcess()

        with patch(
            "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ):
            messages = [message async for message in runtime.execute_task("Do the thing")]

        assert len(messages) == 1
        assert messages[0].type == "result"
        assert messages[0].is_error
        assert messages[0].data["error_type"] == "TimeoutError"
        assert process.terminated or process.killed


class TestHermesCliRuntimeChildEnv:
    """Tests for Hermes child process environment isolation."""

    def test_strips_ouroboros_vars(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp")
        with patch.dict(
            os.environ,
            {
                "OUROBOROS_AGENT_RUNTIME": "hermes",
                "OUROBOROS_LLM_BACKEND": "claude_code",
            },
        ):
            env = runtime._build_child_env()

        assert "OUROBOROS_AGENT_RUNTIME" not in env
        assert "OUROBOROS_LLM_BACKEND" not in env

    def test_increments_depth(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp")
        with patch.dict(os.environ, {"_OUROBOROS_DEPTH": "2"}):
            env = runtime._build_child_env()

        assert env["_OUROBOROS_DEPTH"] == "3"

    def test_depth_guard(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp")
        with patch.dict(os.environ, {"_OUROBOROS_DEPTH": "5"}):
            with pytest.raises(RuntimeError, match="Maximum Ouroboros nesting depth"):
                runtime._build_child_env()
