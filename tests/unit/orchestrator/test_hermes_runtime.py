"""Unit tests for HermesCliRuntime."""

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
import ouroboros.orchestrator.hermes_runtime as hermes_runtime_module
from ouroboros.orchestrator.hermes_runtime import HermesCliRuntime, _parse_quiet_output
from ouroboros.router import Resolved, ResolveRequest, SkillDispatchRouter


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
        assert runtime._startup_output_timeout_seconds == 180.0
        assert runtime._stdout_idle_timeout_seconds == 600.0

    def test_constructor_accepts_llm_backend(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", llm_backend="opencode")
        assert runtime._llm_backend == "opencode"

    def test_constructor_accepts_provider(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", provider="gemini")
        assert runtime._provider == "gemini"

    def test_timeout_defaults_can_be_overridden_with_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OUROBOROS_HERMES_STARTUP_TIMEOUT_SECONDS": "240",
                "OUROBOROS_HERMES_STDOUT_IDLE_TIMEOUT_SECONDS": "900",
            },
        ):
            runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")

        assert runtime._startup_output_timeout_seconds == 240.0
        assert runtime._stdout_idle_timeout_seconds == 900.0

    def test_invalid_timeout_env_values_fall_back_to_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OUROBOROS_HERMES_STARTUP_TIMEOUT_SECONDS": "nope",
                "OUROBOROS_HERMES_STDOUT_IDLE_TIMEOUT_SECONDS": "0",
            },
        ):
            runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")

        assert runtime._startup_output_timeout_seconds == 180.0
        assert runtime._stdout_idle_timeout_seconds == 600.0

    def test_research_tasks_get_web_capable_toolsets_by_default(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        assert (
            runtime._configured_toolsets("Create a source-backed evidence ledger", None)
            == "web,browser,terminal,file"
        )

    def test_toolsets_can_be_overridden_with_env(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        with patch.dict(os.environ, {"OUROBOROS_HERMES_TOOLSETS": "web, terminal"}):
            assert runtime._configured_toolsets("plain coding task", None) == "web,terminal"

    def test_toolsets_can_be_disabled_with_env(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        with patch.dict(os.environ, {"OUROBOROS_HERMES_TOOLSETS": ""}):
            assert runtime._configured_toolsets("research evidence", None) is None

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

        assert obsolete_helpers.isdisjoint(dir(HermesCliRuntime))

    def test_runtime_source_does_not_reference_removed_dispatch_parser_helpers(self) -> None:
        """Removed local parser helpers should not remain referenced by the runtime."""
        runtime_source = inspect.getsource(hermes_runtime_module)
        obsolete_helper_references = {
            "_extract_first_argument(",
            "_load_skill_frontmatter(",
            "_normalize_mcp_frontmatter(",
            "_resolve_dispatch_templates(",
            "_resolve_skill_dispatch(",
            "_resolve_skill_intercept(",
            "SkillInterceptRequest",
            "dispatch_target",
        }

        assert all(reference not in runtime_source for reference in obsolete_helper_references)

    @pytest.mark.asyncio
    async def test_execute_task_uses_dispatcher_for_valid_intercept(
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
                AgentMessage(type="result", content="Intercepted", data={"subtype": "success"}),
            )
        )
        runtime = HermesCliRuntime(
            cli_path="hermes",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch("ouroboros.orchestrator.hermes_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec"
            ) as mock_exec,
        ):
            messages = [
                message async for message in runtime.execute_task('ooo run "seed spec.yaml"')
            ]

        dispatcher.assert_awaited_once()
        mock_exec.assert_not_called()
        mock_warning.assert_not_called()
        intercept = dispatcher.await_args.args[0]
        assert isinstance(intercept, Resolved)
        assert intercept.skill_name == "run"
        assert intercept.command_prefix == "ooo run"
        assert intercept.first_argument == "seed spec.yaml"
        assert intercept.mcp_args == {"seed_path": "seed spec.yaml"}
        assert messages[-1].content == "Intercepted"

    @pytest.mark.asyncio
    async def test_execute_task_uses_shared_router_for_normalized_ooo_dispatch(
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
                '  cwd: "$CWD"',
                '  label: "cwd=$CWD seed=$1"',
                "  nested:",
                "    values:",
                '      - "$1"',
                '      - "$CWD"',
            ],
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(type="result", content="Intercepted", data={"subtype": "success"}),
            )
        )
        runtime = HermesCliRuntime(
            cli_path="hermes",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )
        prompt = ' \tOOO   Run\t"seed spec.yaml" --max-iterations 2'
        observed_requests: list[object] = []
        original_resolve = SkillDispatchRouter.resolve

        def resolve_spy(self, request, *, skills_dir=None, cwd=None):
            observed_requests.append(request)
            return original_resolve(self, request, skills_dir=skills_dir, cwd=cwd)

        with (
            patch.object(SkillDispatchRouter, "resolve", new=resolve_spy),
            patch(
                "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec"
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task(prompt)]

        assert len(observed_requests) == 1
        resolve_request = observed_requests[0]
        assert isinstance(resolve_request, ResolveRequest)
        assert resolve_request.prompt == prompt
        assert resolve_request.cwd == "/tmp/project"
        assert resolve_request.skills_dir == tmp_path
        dispatcher.assert_awaited_once()
        mock_exec.assert_not_called()
        intercept = dispatcher.await_args.args[0]
        assert intercept.skill_name == "run"
        assert intercept.command_prefix == "ooo run"
        assert intercept.prompt == prompt
        expected_argument = "seed spec.yaml --max-iterations 2"
        assert intercept.first_argument == expected_argument
        assert intercept.mcp_args == {
            "seed_path": expected_argument,
            "cwd": "/tmp/project",
            "label": f"cwd=/tmp/project seed={expected_argument}",
            "nested": {"values": [expected_argument, "/tmp/project"]},
        }
        assert messages[-1].content == "Intercepted"

    @pytest.mark.asyncio
    async def test_execute_task_passes_resolved_router_result_and_handle_to_dispatcher(
        self,
        tmp_path: Path,
    ) -> None:
        resolved = Resolved(
            skill_name="router-skill",
            command_prefix="ooo router-skill",
            prompt="ooo run seed.yaml",
            skill_path=tmp_path / "router-skill" / "SKILL.md",
            mcp_tool="router_only_tool",
            mcp_args={"seed_path": "resolved-by-router.yaml"},
            first_argument="resolved-first-argument",
        )
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(type="result", content="Intercepted", data={"subtype": "success"}),
            )
        )
        resume_handle = RuntimeHandle(
            backend="hermes_cli",
            native_session_id="20260412_090000_cafebabe",
            metadata={"handoff": "runtime-handle"},
        )
        runtime = HermesCliRuntime(
            cli_path="hermes",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch(
                "ouroboros.orchestrator.hermes_runtime.resolve_skill_dispatch",
                return_value=resolved,
            ) as mock_resolve,
            patch(
                "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec"
            ) as mock_exec,
        ):
            messages = [
                message
                async for message in runtime.execute_task(
                    "ooo run seed.yaml",
                    resume_handle=resume_handle,
                )
            ]

        mock_resolve.assert_called_once()
        request = mock_resolve.call_args.args[0]
        assert isinstance(request, ResolveRequest)
        assert request.prompt == "ooo run seed.yaml"
        assert request.cwd == "/tmp/project"
        assert request.skills_dir == tmp_path
        dispatcher.assert_awaited_once()
        assert dispatcher.await_args.args[0] is resolved
        assert dispatcher.await_args.args[1] is resume_handle
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == ["Dispatching", "Intercepted"]

    @pytest.mark.asyncio
    async def test_execute_task_builtin_dispatcher_emits_router_payload_fields(
        self,
        tmp_path: Path,
    ) -> None:
        """Built-in dispatch preserves Hermes message fields from router metadata."""
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
        runtime = HermesCliRuntime(
            cli_path="hermes",
            cwd="/tmp/project",
            skills_dir=tmp_path,
        )

        with (
            patch(
                "ouroboros.orchestrator.hermes_runtime.resolve_skill_dispatch",
                return_value=resolved,
            ) as mock_resolve,
            patch.object(
                runtime, "_get_mcp_tool_handler", return_value=fake_handler
            ) as mock_lookup,
            patch("ouroboros.orchestrator.hermes_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec"
            ) as mock_exec,
        ):
            messages = [
                message async for message in runtime.execute_task("ooo run prompt-derived.yaml")
            ]

        mock_resolve.assert_called_once()
        request = mock_resolve.call_args.args[0]
        assert isinstance(request, ResolveRequest)
        assert request.prompt == "ooo run prompt-derived.yaml"
        assert request.cwd == "/tmp/project"
        assert request.skills_dir == tmp_path
        mock_lookup.assert_called_once_with("router_only_tool")
        fake_handler.handle.assert_awaited_once_with(
            {
                "seed_path": "resolved-by-router.yaml",
                "nested": {"source": "router"},
            }
        )
        mock_exec.assert_not_called()
        mock_warning.assert_not_called()
        assert len(messages) == 2
        assert messages[0].type == "assistant"
        assert messages[0].tool_name == "router_only_tool"
        assert messages[0].content == "Calling tool: router_only_tool"
        assert messages[0].data == {
            "tool_input": {
                "seed_path": "resolved-by-router.yaml",
                "nested": {"source": "router"},
            },
            "command_prefix": "ooo router-skill",
            "skill_name": "router-skill",
        }
        assert messages[1].type == "result"
        assert messages[1].content == "Router dispatch"
        assert messages[1].data == {
            "subtype": "success",
            "tool_name": "router_only_tool",
            "mcp_meta": {"execution_id": "exec-router"},
            "execution_id": "exec-router",
        }

    @pytest.mark.asyncio
    async def test_execute_task_requires_exact_skill_prefix_through_shared_router(
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
        dispatcher = AsyncMock()
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
            messages = [
                message async for message in runtime.execute_task("please ooo run seed.yaml")
            ]

        dispatcher.assert_not_awaited()
        mock_exec.assert_called_once()
        mock_warning.assert_not_called()
        assert messages[-1].content == "Hermes fallback completed"

    @pytest.mark.asyncio
    async def test_execute_task_passes_provider_to_hermes_cli(self) -> None:
        runtime = HermesCliRuntime(
            cli_path="hermes",
            provider="gemini",
            model="gemini-2.0-flash",
            cwd="/tmp/project",
        )
        process = _FakeProcess("Hermes fallback completed\nsession_id: 20260413_120000_deadbeef\n")

        with patch(
            "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ) as mock_exec:
            messages = [message async for message in runtime.execute_task("do the thing")]

        mock_exec.assert_called_once()
        args = mock_exec.call_args.args
        assert args[:2] == ("hermes", "chat")
        assert "--provider" in args
        assert args[args.index("--provider") + 1] == "gemini"
        assert "--model" in args
        assert args[args.index("--model") + 1] == "gemini-2.0-flash"
        assert args.index("--provider") < args.index("--model")
        assert messages[-1].content == "Hermes fallback completed"

    @pytest.mark.asyncio
    async def test_execute_task_passes_research_toolsets_to_hermes_cli(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        process = _FakeProcess("Hermes fallback completed\nsession_id: 20260413_120000_deadbeef\n")

        with patch(
            "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ) as mock_exec:
            messages = [
                message
                async for message in runtime.execute_task(
                    "Research source URLs and produce an evidence ledger"
                )
            ]

        mock_exec.assert_called_once()
        args = mock_exec.call_args.args
        assert "--toolsets" in args
        assert args[args.index("--toolsets") + 1] == "web,browser,terminal,file"
        assert messages[-1].content == "Hermes fallback completed"

    @pytest.mark.asyncio
    async def test_execute_task_omits_toolsets_for_plain_coding_task(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        process = _FakeProcess("Hermes fallback completed\nsession_id: 20260413_120000_deadbeef\n")

        with patch(
            "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ) as mock_exec:
            _ = [message async for message in runtime.execute_task("Refactor this module")]

        assert "--toolsets" not in mock_exec.call_args.args

    @pytest.mark.asyncio
    async def test_execute_task_maps_interview_argument_to_initial_context(
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
        dispatcher = AsyncMock(
            return_value=(
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(type="result", content="Intercepted", data={"subtype": "success"}),
            )
        )
        runtime = HermesCliRuntime(
            cli_path="hermes",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        messages = [
            message async for message in runtime.execute_task('ooo interview "Build a REST API"')
        ]

        dispatcher.assert_awaited_once()
        intercept = dispatcher.await_args.args[0]
        assert intercept.mcp_tool == "ouroboros_interview"
        assert intercept.mcp_args == {
            "initial_context": "Build a REST API",
            "cwd": "/tmp/project",
        }
        assert messages[-1].content == "Intercepted"

    @pytest.mark.asyncio
    async def test_execute_task_bypasses_unterminated_frontmatter(
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
        process = _FakeProcess("Hermes fallback completed\nsession_id: 20260413_120000_deadbeef\n")

        with (
            patch("ouroboros.orchestrator.hermes_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
                return_value=process,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "hermes_cli_runtime.skill_intercept_frontmatter_invalid"
        )
        assert "Unterminated frontmatter" in mock_warning.call_args.kwargs["error"]
        assert messages[-1].content == "Hermes fallback completed"

    @pytest.mark.asyncio
    async def test_execute_task_bypasses_non_mapping_frontmatter(
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
        process = _FakeProcess("Hermes fallback completed\nsession_id: 20260413_120000_deadbeef\n")

        with (
            patch("ouroboros.orchestrator.hermes_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
                return_value=process,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "hermes_cli_runtime.skill_intercept_frontmatter_invalid"
        )
        assert "Frontmatter must be a mapping" in mock_warning.call_args.kwargs["error"]
        assert messages[-1].content == "Hermes fallback completed"

    @pytest.mark.asyncio
    async def test_execute_task_bypasses_missing_frontmatter_key(
        self,
        tmp_path: Path,
    ) -> None:
        skill_path = self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                "mcp_tool: ouroboros_execute_seed",
            ],
        )
        runtime = HermesCliRuntime(cli_path="hermes", skills_dir=tmp_path)
        process = _FakeProcess("Hermes fallback completed\nsession_id: 20260413_120000_deadbeef\n")
        events: list[tuple[str, str]] = []

        def record_warning(event_name: str, **_: object) -> None:
            events.append(("warning", event_name))

        async def record_forward(*args: object, **_: object) -> _FakeProcess:
            events.append(("forward", str(args[0])))
            return process

        with (
            patch(
                "ouroboros.orchestrator.hermes_runtime.log.warning",
                side_effect=record_warning,
            ) as mock_warning,
            patch(
                "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
                side_effect=record_forward,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert events == [
            ("warning", "hermes_cli_runtime.skill_intercept_frontmatter_missing"),
            ("forward", "hermes"),
        ]
        assert (
            mock_warning.call_args[0][0] == "hermes_cli_runtime.skill_intercept_frontmatter_missing"
        )
        assert mock_warning.call_args.kwargs == {
            "skill": "run",
            "path": str(skill_path),
            "error": "missing required frontmatter key: mcp_args",
        }
        assert messages[-1].content == "Hermes fallback completed"

    @pytest.mark.asyncio
    async def test_execute_task_maps_granular_mcp_args_errors_to_legacy_log_payload(
        self,
        tmp_path: Path,
    ) -> None:
        skill_path = self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                "  created_at: 2026-04-20",
            ],
        )
        runtime = HermesCliRuntime(cli_path="hermes", skills_dir=tmp_path)
        process = _FakeProcess("Hermes fallback completed\nsession_id: 20260413_120000_deadbeef\n")

        with (
            patch("ouroboros.orchestrator.hermes_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
                return_value=process,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "hermes_cli_runtime.skill_intercept_frontmatter_invalid"
        )
        assert mock_warning.call_args.kwargs == {
            "skill": "run",
            "path": str(skill_path),
            "error": "mcp_args must be a mapping with string keys and YAML-safe values",
        }
        assert messages[-1].content == "Hermes fallback completed"

    def test_build_tool_arguments_reuses_interview_session_from_handle(self) -> None:
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        intercept = Resolved(
            skill_name="interview",
            command_prefix="ooo interview",
            prompt="ooo interview Next answer",
            skill_path=Path("/tmp/skills/interview/SKILL.md"),
            mcp_tool="ouroboros_interview",
            mcp_args={"initial_context": "Build a REST API"},
            first_argument="Next answer",
        )
        handle = RuntimeHandle(
            backend="hermes_cli",
            metadata={"ouroboros_interview_session_id": "interview-123"},
        )

        arguments = runtime._build_tool_arguments(intercept, handle)

        assert "initial_context" not in arguments
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
        intercept = Resolved(
            skill_name="interview",
            command_prefix="ooo interview",
            prompt="ooo interview",
            skill_path=Path("/tmp/skills/interview/SKILL.md"),
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
        intercept = Resolved(
            skill_name="run",
            command_prefix="ooo run",
            prompt="ooo run seed.yaml",
            skill_path=Path("/tmp/skills/run/SKILL.md"),
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
        skill_path = self._write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        events: list[tuple[str, str]] = []

        async def dispatch(
            intercept: Resolved,
            handle: RuntimeHandle | None,
        ) -> tuple[AgentMessage, ...]:
            events.append(("dispatch", intercept.command_prefix))
            return (
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

        dispatcher = AsyncMock(side_effect=dispatch)
        runtime = HermesCliRuntime(
            cli_path="hermes",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )
        process = _FakeProcess("Hermes fallback completed\nsession_id: 20260413_120000_deadbeef\n")

        def record_warning(event_name: str, **_: object) -> None:
            events.append(("warning", event_name))

        async def record_forward(*args: object, **_: object) -> _FakeProcess:
            events.append(("forward", str(args[0])))
            return process

        with (
            patch(
                "ouroboros.orchestrator.hermes_runtime.log.warning",
                side_effect=record_warning,
            ) as mock_warning,
            patch(
                "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
                side_effect=record_forward,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert events == [
            ("dispatch", "ooo run"),
            ("warning", "hermes_cli_runtime.skill_intercept_dispatch_failed"),
            ("forward", "hermes"),
        ]
        assert mock_warning.call_args[0][0] == "hermes_cli_runtime.skill_intercept_dispatch_failed"
        assert mock_warning.call_args.kwargs == {
            "skill": "run",
            "tool": "ouroboros_execute_seed",
            "command_prefix": "ooo run",
            "path": str(skill_path),
            "error_type": "MCPTimeoutError",
            "error": "Tool call timed out",
            "recoverable": True,
        }
        assert messages[-1].content == "Hermes fallback completed"

    @pytest.mark.asyncio
    async def test_execute_task_falls_through_when_router_returns_not_handled(
        self,
        tmp_path: Path,
    ) -> None:
        dispatcher = AsyncMock()
        runtime = HermesCliRuntime(
            cli_path="hermes",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )
        process = _FakeProcess("Hermes fallback completed\nsession_id: 20260413_120000_deadbeef\n")
        events: list[tuple[str, str]] = []

        async def record_forward(*args: object, **_: object) -> _FakeProcess:
            events.append(("forward", str(args[0])))
            return process

        with (
            patch("ouroboros.orchestrator.hermes_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
                side_effect=record_forward,
            ) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo missing seed.yaml")]

        dispatcher.assert_not_awaited()
        mock_exec.assert_called_once()
        mock_warning.assert_not_called()
        assert events == [("forward", "hermes")]
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
                message
                async for message in runtime.execute_task("Do the thing", resume_handle=handle)
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

    @pytest.mark.asyncio
    async def test_execute_task_resumes_from_resume_handle(self) -> None:
        """Protocol contract: ``resume_handle`` feeds ``hermes chat --resume``.

        Regression guard for the PR #457 review finding — previously the
        runtime accepted a non-standard ``handle=`` kwarg and silently
        swallowed the protocol's ``resume_handle``, so multi-turn
        orchestrator flows always started a fresh session.
        """
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        process = _FakeProcess("Continued work\nsession_id: 20260413_120000_deadbeef\n")
        handle = RuntimeHandle(backend="hermes_cli", native_session_id="20260412_090000_cafebabe")

        with patch(
            "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ) as mock_exec:
            messages = [
                message
                async for message in runtime.execute_task("Continue the task", resume_handle=handle)
            ]

        assert len(messages) == 1
        call_args = mock_exec.call_args.args
        assert "--resume" in call_args
        assert "20260412_090000_cafebabe" in call_args

    @pytest.mark.asyncio
    async def test_execute_task_resumes_from_legacy_resume_session_id(self) -> None:
        """Legacy ``resume_session_id`` fallback still resumes correctly."""
        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        process = _FakeProcess("ok\n")

        with patch(
            "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ) as mock_exec:
            _ = [
                message
                async for message in runtime.execute_task(
                    "Continue",
                    resume_session_id="20260412_090000_cafebabe",
                )
            ]

        call_args = mock_exec.call_args.args
        assert "--resume" in call_args
        assert "20260412_090000_cafebabe" in call_args

    @pytest.mark.asyncio
    async def test_execute_task_to_result_returns_task_result_on_success(self) -> None:
        """Protocol contract: returns ``Result[TaskResult, ProviderError]``.

        Regression guard for the PR #457 review finding — previously the
        runtime returned ``Result[AgentMessage, RuntimeError]``, breaking
        substitutability with other runtimes.
        """
        from ouroboros.core.errors import ProviderError
        from ouroboros.orchestrator.adapter import TaskResult

        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        process = _FakeProcess("Completed\nsession_id: 20260413_120000_deadbeef\n")

        with patch(
            "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ):
            result = await runtime.execute_task_to_result("Do the thing")

        assert result.is_ok
        task_result = result.unwrap()
        assert isinstance(task_result, TaskResult)
        assert task_result.success is True
        assert task_result.final_message == "Completed"
        assert task_result.session_id == "20260413_120000_deadbeef"
        assert task_result.resume_handle is not None
        assert task_result.resume_handle.native_session_id == "20260413_120000_deadbeef"
        # Substitutability contract: the error branch type is ProviderError.
        assert issubclass(ProviderError, Exception)

    @pytest.mark.asyncio
    async def test_execute_task_to_result_returns_provider_error_on_failure(
        self,
    ) -> None:
        """Failure path returns ``Result.err(ProviderError(...))`` per protocol."""
        from ouroboros.core.errors import ProviderError

        runtime = HermesCliRuntime(cli_path="hermes", cwd="/tmp/project")
        process = _FakeProcess("", stderr="kaboom", returncode=1)

        with patch(
            "ouroboros.orchestrator.hermes_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ):
            result = await runtime.execute_task_to_result("Do the thing")

        assert result.is_err
        assert isinstance(result.error, ProviderError)


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
