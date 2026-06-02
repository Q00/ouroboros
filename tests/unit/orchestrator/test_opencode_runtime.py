"""Unit tests for OpenCodeRuntime."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
import ouroboros.orchestrator.opencode_runtime as opencode_runtime_module
from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime
from ouroboros.router import Resolved, ResolveRequest
from ouroboros.router import resolve_skill_dispatch as shared_resolve_skill_dispatch


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        encoded = "".join(f"{line}\n" for line in lines).encode()
        self._buffer = bytearray(encoded)

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


class _FakeStdin:
    """Minimal stdin mock that supports write/drain/close/wait_closed."""

    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class _FakeProcess:
    def __init__(
        self,
        stdout_lines: list[str],
        stderr_lines: list[str],
        returncode: int = 0,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self._returncode = returncode
        self.returncode = None
        self.pid = 12345
        self.terminated = False

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._returncode

    def kill(self) -> None:
        self.returncode = self._returncode


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


def _fake_opencode_text_process(content: str = "OpenCode fallback completed.") -> _FakeProcess:
    text_event = json.dumps(
        {
            "type": "text",
            "sessionID": "sess-fallback",
            "part": {"type": "text", "text": content},
        }
    )
    return _FakeProcess(stdout_lines=[text_event], stderr_lines=[], returncode=0)


class TestOpenCodeRuntimeProperties:
    """Test basic runtime properties."""

    def test_runtime_backend(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        assert runtime.runtime_backend == "opencode"

    def test_working_directory(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp/project")
        assert runtime.working_directory == "/tmp/project"

    def test_permission_mode_default(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        assert runtime.permission_mode == "bypassPermissions"

    def test_permission_mode_custom(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp", permission_mode="acceptEdits")
        assert runtime.permission_mode == "acceptEdits"


class TestOpenCodeRuntimeBuildCommand:
    """Test command building."""

    def test_basic_command(self) -> None:
        runtime = OpenCodeRuntime(cli_path="/usr/bin/opencode", cwd="/tmp")
        cmd = runtime._build_command(prompt="Hello world")
        assert cmd == ["/usr/bin/opencode", "run", "--pure", "--format", "json"]
        assert "Hello world" not in cmd  # prompt piped via stdin, not argv

    def test_command_always_includes_pure(self) -> None:
        """`--pure` is mandatory: disables opencode plugins inside the
        subprocess runtime so the bridge plugin cannot double-dispatch a
        `_subagent` envelope that leaks into this headless execution.
        Regardless of model/session args, `--pure` must be present.
        """
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp", model="m")
        cmd = runtime._build_command(resume_session_id="sess_abc", prompt="p")
        assert "--pure" in cmd
        # --pure comes immediately after 'run', before everything else
        assert cmd[:3] == ["opencode", "run", "--pure"]

    def test_command_with_model(self) -> None:
        runtime = OpenCodeRuntime(
            cli_path="opencode", cwd="/tmp", model="anthropic/claude-sonnet-4-20250514"
        )
        cmd = runtime._build_command(prompt="Hello")
        assert "--model" in cmd
        assert "anthropic/claude-sonnet-4-20250514" in cmd

    def test_command_with_resume_session(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        cmd = runtime._build_command(
            resume_session_id="sess-abc123",
            prompt="Continue",
        )
        assert "--session" in cmd
        assert "sess-abc123" in cmd
        assert "Continue" not in cmd  # prompt piped via stdin

    def test_command_rejects_unsafe_session_id(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        with pytest.raises(ValueError, match="Invalid resume_session_id"):
            runtime._build_command(
                resume_session_id="sess; rm -rf /",
                prompt="Hello",
            )


class TestOpenCodeRuntimeComposePrompt:
    """Test prompt composition."""

    def test_basic_prompt(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        result = runtime._compose_prompt("Do the thing", None, None)
        assert result == "Do the thing"

    def test_prompt_with_system(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        result = runtime._compose_prompt("Do the thing", "Be helpful.", None)
        assert "## System Instructions" in result
        assert "Be helpful." in result
        assert "Do the thing" in result

    def test_prompt_with_tools(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        result = runtime._compose_prompt("Do the thing", None, ["Read", "Edit"])
        assert "## Tooling Guidance" in result
        assert "- Read" in result
        assert "- Edit" in result


class TestOpenCodeRuntimeHandleManagement:
    """Test runtime handle creation."""

    def test_build_runtime_handle_new(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        handle = runtime._build_runtime_handle("sess-123")
        assert handle is not None
        assert handle.backend == "opencode"
        assert handle.native_session_id == "sess-123"
        assert handle.cwd == "/tmp"

    def test_build_runtime_handle_none(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        handle = runtime._build_runtime_handle(None)
        assert handle is None

    def test_build_runtime_handle_update(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        existing = RuntimeHandle(
            backend="opencode",
            native_session_id="sess-old",
            cwd="/tmp",
        )
        updated = runtime._build_runtime_handle("sess-new", existing)
        assert updated is not None
        assert updated.native_session_id == "sess-new"
        assert updated.backend == "opencode"


class TestOpenCodeRuntimeDispatchBoundary:
    def test_runtime_does_not_expose_local_dispatch_parser_helpers(self) -> None:
        obsolete_helpers = {
            "_extract_first_argument",
            "_load_skill_frontmatter",
            "_normalize_mcp_frontmatter",
            "_resolve_dispatch_templates",
            "_resolve_skill_dispatch",
            "_resolve_skill_intercept",
        }

        assert obsolete_helpers.isdisjoint(dir(OpenCodeRuntime))

    def test_runtime_source_does_not_reference_removed_dispatch_parser_helpers(self) -> None:
        """Removed local parser helpers should not remain referenced by the runtime."""
        runtime_source = inspect.getsource(opencode_runtime_module)
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


class TestOpenCodeRuntimeSkillDispatch:
    """Test shared-router skill dispatch integration."""

    @pytest.mark.asyncio
    async def test_execute_task_uses_dispatcher_for_valid_intercept(
        self,
        tmp_path: Path,
    ) -> None:
        _write_skill(
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
        runtime = OpenCodeRuntime(
            cli_path="opencode",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            messages = [
                message async for message in runtime.execute_task('ooo run "seed spec.yaml"')
            ]

        dispatcher.assert_awaited_once()
        mock_exec.assert_not_called()
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
        _write_skill(
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
        runtime = OpenCodeRuntime(
            cli_path="opencode",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )
        prompt = ' \tOOO   Run\t"seed spec.yaml" --max-iterations 2'
        observed_requests: list[object] = []

        def resolve_spy(request, *, skills_dir=None, cwd=None):
            observed_requests.append(request)
            return shared_resolve_skill_dispatch(request, skills_dir=skills_dir, cwd=cwd)

        with (
            patch.object(
                opencode_runtime_module,
                "resolve_skill_dispatch",
                side_effect=resolve_spy,
            ),
            patch("asyncio.create_subprocess_exec") as mock_exec,
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
        assert isinstance(intercept, Resolved)
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
    async def test_execute_task_passes_resolved_router_result_to_dispatcher_without_reconstruction(
        self,
        tmp_path: Path,
    ) -> None:
        """OpenCode runtime should pass the router's Resolved object through directly."""
        resolved = Resolved(
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
        runtime = OpenCodeRuntime(
            cli_path="opencode",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch.object(
                opencode_runtime_module,
                "resolve_skill_dispatch",
                return_value=resolved,
            ) as mock_resolve,
            patch("asyncio.create_subprocess_exec") as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_resolve.assert_called_once()
        request = mock_resolve.call_args.args[0]
        assert isinstance(request, ResolveRequest)
        assert request.prompt == "ooo run seed.yaml"
        assert request.cwd == "/tmp/project"
        assert request.skills_dir == tmp_path
        dispatcher.assert_awaited_once()
        assert dispatcher.await_args.args[0] is resolved
        assert dispatcher.await_args.args[1] is None
        mock_exec.assert_not_called()
        assert [message.content for message in messages] == ["Dispatching", "Intercepted"]

    @pytest.mark.asyncio
    async def test_execute_task_builtin_dispatcher_uses_resolved_router_payload_fields(
        self,
        tmp_path: Path,
    ) -> None:
        """Built-in dispatch should not reparse or reconstruct prompt-derived fields."""
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
        runtime = OpenCodeRuntime(
            cli_path="opencode",
            cwd="/tmp/project",
            skills_dir=tmp_path,
        )

        with (
            patch.object(
                opencode_runtime_module,
                "resolve_skill_dispatch",
                return_value=resolved,
            ) as mock_resolve,
            patch.object(
                runtime,
                "_get_mcp_tool_handler",
                return_value=fake_handler,
            ) as mock_lookup,
            patch("ouroboros.orchestrator.opencode_runtime.log.warning") as mock_warning,
            patch("asyncio.create_subprocess_exec") as mock_exec,
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
            }
        }
        assert messages[1].type == "result"
        assert messages[1].content == "Router dispatch"
        assert messages[1].data == {
            "subtype": "success",
            "tool_name": "router_only_tool",
        }

    @pytest.mark.asyncio
    async def test_execute_task_builtin_dispatcher_uses_resolved_first_argument(
        self,
        tmp_path: Path,
    ) -> None:
        """Built-in dispatch should consume router Resolved fields for interview answers."""
        resolved = Resolved(
            skill_name="interview",
            command_prefix="ooo interview",
            prompt="ooo interview prompt-derived-answer",
            skill_path=tmp_path / "interview" / "SKILL.md",
            mcp_tool="ouroboros_interview",
            mcp_args={"initial_context": "resolved-by-router"},
            first_argument="resolved-answer",
        )
        fake_handler = AsyncMock()
        fake_handler.handle = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="Interview dispatch"),),
                    meta={},
                )
            )
        )
        resume_handle = RuntimeHandle(
            backend="opencode",
            metadata={"ouroboros_interview_session_id": "interview-session"},
        )
        runtime = OpenCodeRuntime(
            cli_path="opencode",
            cwd="/tmp/project",
            skills_dir=tmp_path,
        )

        with (
            patch.object(
                opencode_runtime_module,
                "resolve_skill_dispatch",
                return_value=resolved,
            ) as mock_resolve,
            patch.object(
                runtime,
                "_get_mcp_tool_handler",
                return_value=fake_handler,
            ),
            patch("asyncio.create_subprocess_exec") as mock_exec,
        ):
            messages = [
                message
                async for message in runtime.execute_task(
                    "ooo interview prompt-derived-answer",
                    resume_handle=resume_handle,
                )
            ]

        mock_resolve.assert_called_once()
        request = mock_resolve.call_args.args[0]
        assert isinstance(request, ResolveRequest)
        assert request.prompt == "ooo interview prompt-derived-answer"
        fake_handler.handle.assert_awaited_once_with(
            {
                "session_id": "interview-session",
                "answer": "resolved-answer",
            }
        )
        mock_exec.assert_not_called()
        assert messages[0].data == {
            "tool_input": {
                "session_id": "interview-session",
                "answer": "resolved-answer",
            }
        }
        assert messages[1].content == "Interview dispatch"

    @pytest.mark.asyncio
    async def test_execute_task_bypasses_invalid_frontmatter(self, tmp_path: Path) -> None:
        _write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  - "$1"',
            ],
        )
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp/project", skills_dir=tmp_path)
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-fallback",
                "part": {"type": "text", "text": "OpenCode fallback completed."},
            }
        )
        process = _FakeProcess(stdout_lines=[text_event], stderr_lines=[], returncode=0)

        with (
            patch("ouroboros.orchestrator.opencode_runtime.log.warning") as mock_warning,
            patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "opencode_runtime.skill_intercept_frontmatter_invalid"
        )
        assert (
            mock_warning.call_args.kwargs["error"]
            == "mcp_args must be a mapping with string keys and YAML-safe values"
        )
        assert any(message.content == "OpenCode fallback completed." for message in messages)

    @pytest.mark.asyncio
    async def test_execute_task_bypasses_non_mapping_frontmatter_with_legacy_log_payload(
        self,
        tmp_path: Path,
    ) -> None:
        skill_dir = tmp_path / "run"
        skill_dir.mkdir(parents=True)
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text("---\n- not\n- a\n- mapping\n---\n", encoding="utf-8")
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp/project", skills_dir=tmp_path)
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-fallback",
                "part": {"type": "text", "text": "OpenCode fallback completed."},
            }
        )
        process = _FakeProcess(stdout_lines=[text_event], stderr_lines=[], returncode=0)

        with (
            patch("ouroboros.orchestrator.opencode_runtime.log.warning") as mock_warning,
            patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "opencode_runtime.skill_intercept_frontmatter_invalid"
        )
        assert mock_warning.call_args.kwargs == {
            "skill": "run",
            "error": f"Frontmatter must be a mapping in {skill_path}",
        }
        assert any(message.content == "OpenCode fallback completed." for message in messages)

    @pytest.mark.asyncio
    async def test_execute_task_maps_granular_mcp_args_errors_to_legacy_log_payload(
        self,
        tmp_path: Path,
    ) -> None:
        _write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                "  created_at: 2026-04-20",
            ],
        )
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp/project", skills_dir=tmp_path)
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-fallback",
                "part": {"type": "text", "text": "OpenCode fallback completed."},
            }
        )
        process = _FakeProcess(stdout_lines=[text_event], stderr_lines=[], returncode=0)

        with (
            patch("ouroboros.orchestrator.opencode_runtime.log.warning") as mock_warning,
            patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "opencode_runtime.skill_intercept_frontmatter_invalid"
        )
        assert mock_warning.call_args.kwargs == {
            "skill": "run",
            "error": "mcp_args must be a mapping with string keys and YAML-safe values",
        }
        assert any(message.content == "OpenCode fallback completed." for message in messages)

    @pytest.mark.asyncio
    async def test_execute_task_preserves_dispatch_failure_log_event_name(
        self,
        tmp_path: Path,
    ) -> None:
        _write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-fallback",
                "part": {"type": "text", "text": "OpenCode fallback completed."},
            }
        )
        process = _FakeProcess(stdout_lines=[text_event], stderr_lines=[], returncode=0)
        dispatcher = AsyncMock(side_effect=RuntimeError("dispatch failed"))
        runtime = OpenCodeRuntime(
            cli_path="opencode",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch("ouroboros.orchestrator.opencode_runtime.log.warning") as mock_warning,
            patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert mock_warning.call_args[0][0] == "opencode_runtime.skill_intercept_dispatch_failed"
        assert mock_warning.call_args.kwargs["skill"] == "run"
        assert mock_warning.call_args.kwargs["error"] == "dispatch failed"
        assert any(message.content == "OpenCode fallback completed." for message in messages)

    @pytest.mark.asyncio
    async def test_execute_task_emits_missing_frontmatter_warning_for_opencode(
        self,
        tmp_path: Path,
    ) -> None:
        skill_path = _write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                "mcp_tool: ouroboros_execute_seed",
            ],
        )
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-fallback",
                "part": {"type": "text", "text": "OpenCode fallback completed."},
            }
        )
        process = _FakeProcess(stdout_lines=[text_event], stderr_lines=[], returncode=0)
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp/project", skills_dir=tmp_path)

        with (
            patch("ouroboros.orchestrator.opencode_runtime.log.warning") as mock_warning,
            patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_exec.assert_called_once()
        mock_warning.assert_called_once()
        assert (
            mock_warning.call_args[0][0] == "opencode_runtime.skill_intercept_frontmatter_missing"
        )
        assert mock_warning.call_args.kwargs["skill"] == "run"
        assert (
            mock_warning.call_args.kwargs["error"] == "missing required frontmatter key: mcp_args"
        )
        assert skill_path.exists()
        assert any(message.content == "OpenCode fallback completed." for message in messages)

    @pytest.mark.asyncio
    async def test_execute_task_falls_through_when_router_returns_not_handled(
        self,
        tmp_path: Path,
    ) -> None:
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-fallback",
                "part": {"type": "text", "text": "OpenCode fallback completed."},
            }
        )
        process = _FakeProcess(stdout_lines=[text_event], stderr_lines=[], returncode=0)
        dispatcher = AsyncMock()
        runtime = OpenCodeRuntime(
            cli_path="opencode",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch("ouroboros.orchestrator.opencode_runtime.log.warning") as mock_warning,
            patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo missing seed.yaml")]

        dispatcher.assert_not_awaited()
        mock_exec.assert_called_once()
        mock_warning.assert_not_called()
        assert b"ooo missing seed.yaml" in process.stdin.written
        assert any(message.content == "OpenCode fallback completed." for message in messages)

    @pytest.mark.asyncio
    async def test_execute_task_emits_no_dispatch_logs_or_task_started_on_dispatch_success(
        self,
        tmp_path: Path,
    ) -> None:
        _write_skill(
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

        async def dispatch_success(
            intercept: Resolved,
            current_handle: RuntimeHandle | None,
        ) -> tuple[AgentMessage, ...]:
            events.append(("dispatcher", intercept.skill_name))
            return (
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(type="result", content="Intercepted", data={"subtype": "success"}),
            )

        dispatcher = AsyncMock(side_effect=dispatch_success)
        runtime = OpenCodeRuntime(
            cli_path="opencode",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        with (
            patch("ouroboros.orchestrator.opencode_runtime.log.warning") as mock_warning,
            patch("ouroboros.orchestrator.opencode_runtime.log.info") as mock_info,
            patch("asyncio.create_subprocess_exec") as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        mock_warning.assert_not_called()
        mock_info.assert_not_called()
        mock_exec.assert_not_called()
        assert events == [("dispatcher", "run")]
        assert messages[-1].content == "Intercepted"

    @pytest.mark.asyncio
    async def test_execute_task_logs_invalid_frontmatter_before_fallback_start(
        self,
        tmp_path: Path,
    ) -> None:
        _write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  - "$1"',
            ],
        )
        process = _fake_opencode_text_process()
        events: list[tuple[str, str]] = []
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp/project", skills_dir=tmp_path)

        async def start_process(*args, **kwargs):
            events.append(("subprocess", "start"))
            return process

        def record_warning(event_name: str, **kwargs) -> None:
            events.append(("warning", event_name))

        def record_info(event_name: str, **kwargs) -> None:
            events.append(("info", event_name))

        with (
            patch(
                "ouroboros.orchestrator.opencode_runtime.log.warning",
                side_effect=record_warning,
            ) as mock_warning,
            patch(
                "ouroboros.orchestrator.opencode_runtime.log.info",
                side_effect=record_info,
            ) as mock_info,
            patch("asyncio.create_subprocess_exec", side_effect=start_process) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        mock_warning.assert_called_once()
        mock_info.assert_called_once()
        mock_exec.assert_called_once()
        assert events == [
            ("warning", "opencode_runtime.skill_intercept_frontmatter_invalid"),
            ("info", "opencode_runtime.task_started"),
            ("subprocess", "start"),
        ]
        assert any(message.content == "OpenCode fallback completed." for message in messages)

    @pytest.mark.asyncio
    async def test_execute_task_logs_dispatch_failure_before_fallback_start(
        self,
        tmp_path: Path,
    ) -> None:
        _write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        process = _fake_opencode_text_process()
        events: list[tuple[str, str]] = []

        async def dispatch_failure(
            intercept: Resolved,
            current_handle: RuntimeHandle | None,
        ) -> tuple[AgentMessage, ...]:
            events.append(("dispatcher", intercept.skill_name))
            raise RuntimeError("dispatch failed")

        dispatcher = AsyncMock(side_effect=dispatch_failure)
        runtime = OpenCodeRuntime(
            cli_path="opencode",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        async def start_process(*args, **kwargs):
            events.append(("subprocess", "start"))
            return process

        def record_warning(event_name: str, **kwargs) -> None:
            events.append(("warning", event_name))

        def record_info(event_name: str, **kwargs) -> None:
            events.append(("info", event_name))

        with (
            patch(
                "ouroboros.orchestrator.opencode_runtime.log.warning",
                side_effect=record_warning,
            ) as mock_warning,
            patch(
                "ouroboros.orchestrator.opencode_runtime.log.info",
                side_effect=record_info,
            ) as mock_info,
            patch("asyncio.create_subprocess_exec", side_effect=start_process) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        mock_warning.assert_called_once()
        mock_info.assert_called_once()
        mock_exec.assert_called_once()
        assert events == [
            ("dispatcher", "run"),
            ("warning", "opencode_runtime.skill_intercept_dispatch_failed"),
            ("info", "opencode_runtime.task_started"),
            ("subprocess", "start"),
        ]
        assert any(message.content == "OpenCode fallback completed." for message in messages)

    @pytest.mark.asyncio
    async def test_execute_task_starts_fallback_after_recoverable_dispatch_result(
        self,
        tmp_path: Path,
    ) -> None:
        _write_skill(
            tmp_path,
            "run",
            [
                "name: run",
                "mcp_tool: ouroboros_execute_seed",
                "mcp_args:",
                '  seed_path: "$1"',
            ],
        )
        process = _fake_opencode_text_process()
        events: list[tuple[str, str]] = []

        async def dispatch_recoverable(
            intercept: Resolved,
            current_handle: RuntimeHandle | None,
        ) -> tuple[AgentMessage, ...]:
            events.append(("dispatcher", intercept.skill_name))
            return (
                AgentMessage(type="assistant", content="Dispatching"),
                AgentMessage(
                    type="result",
                    content="Recoverable dispatch error",
                    data={"subtype": "error", "recoverable": True},
                ),
            )

        dispatcher = AsyncMock(side_effect=dispatch_recoverable)
        runtime = OpenCodeRuntime(
            cli_path="opencode",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        async def start_process(*args, **kwargs):
            events.append(("subprocess", "start"))
            return process

        def record_info(event_name: str, **kwargs) -> None:
            events.append(("info", event_name))

        with (
            patch("ouroboros.orchestrator.opencode_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.opencode_runtime.log.info",
                side_effect=record_info,
            ) as mock_info,
            patch("asyncio.create_subprocess_exec", side_effect=start_process) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

        dispatcher.assert_awaited_once()
        mock_warning.assert_not_called()
        mock_info.assert_called_once()
        mock_exec.assert_called_once()
        assert events == [
            ("dispatcher", "run"),
            ("info", "opencode_runtime.task_started"),
            ("subprocess", "start"),
        ]
        assert any(message.content == "OpenCode fallback completed." for message in messages)

    @pytest.mark.asyncio
    async def test_execute_task_logs_only_task_started_for_not_handled_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        process = _fake_opencode_text_process()
        events: list[tuple[str, str]] = []
        dispatcher = AsyncMock()
        runtime = OpenCodeRuntime(
            cli_path="opencode",
            cwd="/tmp/project",
            skills_dir=tmp_path,
            skill_dispatcher=dispatcher,
        )

        async def start_process(*args, **kwargs):
            events.append(("subprocess", "start"))
            return process

        def record_info(event_name: str, **kwargs) -> None:
            events.append(("info", event_name))

        with (
            patch("ouroboros.orchestrator.opencode_runtime.log.warning") as mock_warning,
            patch(
                "ouroboros.orchestrator.opencode_runtime.log.info",
                side_effect=record_info,
            ) as mock_info,
            patch("asyncio.create_subprocess_exec", side_effect=start_process) as mock_exec,
        ):
            messages = [message async for message in runtime.execute_task("ooo missing seed.yaml")]

        dispatcher.assert_not_awaited()
        mock_warning.assert_not_called()
        mock_info.assert_called_once()
        mock_exec.assert_called_once()
        assert events == [
            ("info", "opencode_runtime.task_started"),
            ("subprocess", "start"),
        ]
        assert any(message.content == "OpenCode fallback completed." for message in messages)


class TestOpenCodeRuntimeEventConversion:
    """Test JSON event parsing and conversion."""

    def test_parse_json_event_valid(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        event = runtime._parse_json_event('{"type": "text", "part": {}}')
        assert event == {"type": "text", "part": {}}

    def test_parse_json_event_invalid(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        assert runtime._parse_json_event("not json") is None
        assert runtime._parse_json_event("42") is None
        assert runtime._parse_json_event('"string"') is None

    def test_extract_session_id(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        assert runtime._extract_event_session_id({"sessionID": "sess-1"}) == "sess-1"
        assert runtime._extract_event_session_id({"type": "text"}) is None
        assert runtime._extract_event_session_id({"sessionID": ""}) is None

    def test_convert_text_event(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        from ouroboros.orchestrator.opencode_event_normalizer import OpenCodeEventContext

        ctx = OpenCodeEventContext(session_id="sess-1")
        event = {
            "type": "text",
            "sessionID": "sess-1",
            "part": {"type": "text", "text": "Hello there!"},
        }
        messages = runtime._convert_event(event, ctx)
        assert len(messages) == 1
        assert messages[0].type == "assistant"
        assert messages[0].content == "Hello there!"

    def test_convert_tool_use_event(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        from ouroboros.orchestrator.opencode_event_normalizer import OpenCodeEventContext

        ctx = OpenCodeEventContext(session_id="sess-1")
        event = {
            "type": "tool_use",
            "sessionID": "sess-1",
            "part": {
                "tool": "bash",
                "state": {
                    "input": {"command": "ls -la"},
                    "status": "completed",
                    "output": "total 42",
                },
            },
        }
        messages = runtime._convert_event(event, ctx)
        assert len(messages) == 1
        assert messages[0].tool_name == "Bash"
        assert "ls -la" in messages[0].content

    def test_convert_error_event(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        from ouroboros.orchestrator.opencode_event_normalizer import OpenCodeEventContext

        ctx = OpenCodeEventContext(session_id="sess-1")
        event = {
            "type": "error",
            "sessionID": "sess-1",
            "error": {"name": "AuthError", "data": {"message": "Bad key"}},
        }
        messages = runtime._convert_event(event, ctx)
        assert len(messages) == 1
        assert messages[0].type == "result"
        assert messages[0].is_error
        assert "Bad key" in messages[0].content

    def test_convert_reasoning_event(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        from ouroboros.orchestrator.opencode_event_normalizer import OpenCodeEventContext

        ctx = OpenCodeEventContext(session_id="sess-1")
        event = {
            "type": "reasoning",
            "sessionID": "sess-1",
            "part": {"type": "reasoning", "text": "Let me think..."},
        }
        messages = runtime._convert_event(event, ctx)
        assert len(messages) == 1
        assert messages[0].data.get("thinking") == "Let me think..."


class TestOpenCodeRuntimeExecuteTask:
    """Test execute_task integration."""

    @pytest.mark.asyncio
    async def test_execute_task_success(self) -> None:
        """Successful task execution streams messages and produces a final result."""
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-abc",
                "part": {"type": "text", "text": "Task completed successfully."},
            }
        )

        process = _FakeProcess(
            stdout_lines=[text_event],
            stderr_lines=[],
            returncode=0,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp/project")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Do something"):
                messages.append(msg)

        assert len(messages) >= 1
        # Should have at least the text message and a final result
        text_msgs = [m for m in messages if m.type == "assistant"]
        [m for m in messages if m.type == "result"]
        assert len(text_msgs) >= 1
        assert "Task completed" in text_msgs[0].content


    def test_windows_child_cleanup_invokes_wmic_for_parent_pid(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        process = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)
        process.pid = 4242

        with (
            patch.object(opencode_runtime_module.os, "name", "nt"),
            patch.object(opencode_runtime_module.subprocess, "run") as mock_run,
        ):
            runtime._cleanup_windows_child_processes(process)

        mock_run.assert_called_once_with(
            ["wmic", "process", "where", "(ParentProcessId=4242)", "delete"],
            capture_output=True,
            timeout=5,
            check=False,
        )

    def test_windows_child_cleanup_skips_non_windows(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        process = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

        with (
            patch.object(opencode_runtime_module.os, "name", "posix"),
            patch.object(opencode_runtime_module.subprocess, "run") as mock_run,
        ):
            runtime._cleanup_windows_child_processes(process)

        mock_run.assert_not_called()

    def test_windows_child_cleanup_logs_failures(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        process = _FakeProcess(stdout_lines=[], stderr_lines=[], returncode=0)

        with (
            patch.object(opencode_runtime_module.os, "name", "nt"),
            patch.object(opencode_runtime_module.subprocess, "run", side_effect=TimeoutError("slow")),
            patch.object(opencode_runtime_module.log, "warning") as mock_warning,
        ):
            runtime._cleanup_windows_child_processes(process)

        mock_warning.assert_called_once()
        assert mock_warning.call_args.args[0] == "opencode_runtime.windows_child_cleanup_failed"

    @pytest.mark.asyncio
    async def test_execute_task_windows_child_cleanup_runs_after_success(self) -> None:
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-windows-cleanup",
                "part": {"type": "text", "text": "Done"},
            }
        )
        process = _FakeProcess(stdout_lines=[text_event], stderr_lines=[], returncode=0)
        process.pid = 6789
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        with (
            patch("asyncio.create_subprocess_exec", return_value=process),
            patch.object(opencode_runtime_module.os, "name", "nt"),
            patch.object(opencode_runtime_module.subprocess, "run") as mock_run,
        ):
            messages = [msg async for msg in runtime.execute_task("Hello")]

        assert any(msg.type == "result" for msg in messages)
        mock_run.assert_called_once_with(
            ["wmic", "process", "where", "(ParentProcessId=6789)", "delete"],
            capture_output=True,
            timeout=5,
            check=False,
        )

    @pytest.mark.asyncio
    async def test_execute_task_with_session_tracking(self) -> None:
        """Session ID from events is captured into the runtime handle."""
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-tracked-123",
                "part": {"type": "text", "text": "Working..."},
            }
        )

        process = _FakeProcess(
            stdout_lines=[text_event],
            stderr_lines=[],
            returncode=0,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Do something"):
                messages.append(msg)

        # Find a message with a resume handle that has the session ID
        handles = [m.resume_handle for m in messages if m.resume_handle is not None]
        assert any(h.native_session_id == "sess-tracked-123" for h in handles)

    @pytest.mark.asyncio
    async def test_execute_task_cli_not_found(self) -> None:
        """FileNotFoundError yields a result error message."""
        runtime = OpenCodeRuntime(cli_path="/nonexistent/opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("/nonexistent/opencode"),
        ):
            async for msg in runtime.execute_task("Hello"):
                messages.append(msg)

        assert len(messages) == 1
        assert messages[0].type == "result"
        assert messages[0].is_error
        assert "not found" in messages[0].content.lower()

    @pytest.mark.asyncio
    async def test_execute_task_nonzero_exit(self) -> None:
        """Non-zero exit code produces an error result."""
        process = _FakeProcess(
            stdout_lines=[],
            stderr_lines=["Something went wrong"],
            returncode=1,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Hello"):
                messages.append(msg)

        result_msgs = [m for m in messages if m.type == "result"]
        assert len(result_msgs) == 1
        assert result_msgs[0].data.get("subtype") == "error"
        assert result_msgs[0].data.get("returncode") == 1

    @pytest.mark.asyncio
    async def test_execute_task_error_event_is_final(self) -> None:
        """Error events in the stream are treated as final results."""
        error_event = json.dumps(
            {
                "type": "error",
                "sessionID": "sess-1",
                "error": {"name": "Crash", "data": {"message": "Internal error"}},
            }
        )

        process = _FakeProcess(
            stdout_lines=[error_event],
            stderr_lines=[],
            returncode=1,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Hello"):
                messages.append(msg)

        error_msgs = [m for m in messages if m.type == "result" and m.is_error]
        assert len(error_msgs) >= 1
        assert "Internal error" in error_msgs[0].content

    @pytest.mark.asyncio
    async def test_execute_task_to_result_success(self) -> None:
        """execute_task_to_result collects messages into a TaskResult."""
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-1",
                "part": {"type": "text", "text": "Done!"},
            }
        )

        process = _FakeProcess(
            stdout_lines=[text_event],
            stderr_lines=[],
            returncode=0,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await runtime.execute_task_to_result("Do it")

        assert result.is_ok
        assert result.value.success is True
        assert len(result.value.messages) >= 1

    @pytest.mark.asyncio
    async def test_execute_task_multiple_events(self) -> None:
        """Multiple events in the stream are all converted."""
        events = [
            json.dumps(
                {
                    "type": "tool_use",
                    "sessionID": "sess-1",
                    "part": {
                        "tool": "read",
                        "state": {
                            "input": {"filePath": "README.md"},
                            "status": "completed",
                            "output": "# Hello",
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "sessionID": "sess-1",
                    "part": {"type": "text", "text": "I read the README."},
                }
            ),
        ]

        process = _FakeProcess(
            stdout_lines=events,
            stderr_lines=[],
            returncode=0,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Read README"):
                messages.append(msg)

        tool_msgs = [m for m in messages if m.tool_name is not None]
        [m for m in messages if m.type == "assistant" and m.tool_name is None]
        assert len(tool_msgs) >= 1
        assert tool_msgs[0].tool_name == "Read"


class TestExitCodeDeterminesSuccess:
    """Runtime trusts the process exit code as the authoritative signal.

    Tool-level errors during the run are *not* latched — if the process
    exits 0 the runtime reports success, matching the Codex runtime
    pattern.
    """

    @pytest.mark.asyncio
    async def test_tool_error_with_zero_exit_reports_success(self) -> None:
        """A tool_use with state.error + returncode=0 must still be success.

        The exit code is the ground truth for subprocess runtimes.
        Intermediate tool errors are expected when agents self-correct.
        """
        tool_error_event = json.dumps(
            {
                "type": "tool_use",
                "sessionID": "sess-err",
                "part": {
                    "tool": "bash",
                    "state": {
                        "input": {"command": "bad-cmd"},
                        "status": "completed",
                        "output": "command not found",
                        "error": "command not found",
                    },
                },
            }
        )

        process = _FakeProcess(
            stdout_lines=[tool_error_event],
            stderr_lines=[],
            returncode=0,  # exit code 0 — agent decided it succeeded
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Run bad-cmd"):
                messages.append(msg)

        result_msgs = [m for m in messages if m.type == "result"]
        assert len(result_msgs) >= 1
        final = result_msgs[-1]
        assert final.data.get("subtype") == "success", (
            "Exit code 0 must produce success regardless of tool errors"
        )
        assert final.data.get("returncode") == 0

    @pytest.mark.asyncio
    async def test_clean_exit_without_tool_error_reports_success(self) -> None:
        """No tool errors + returncode=0 must produce subtype 'success'."""
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-ok",
                "part": {"type": "text", "text": "All done."},
            }
        )

        process = _FakeProcess(
            stdout_lines=[text_event],
            stderr_lines=[],
            returncode=0,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Do something safe"):
                messages.append(msg)

        result_msgs = [m for m in messages if m.type == "result"]
        assert len(result_msgs) >= 1
        final = result_msgs[-1]
        assert final.data.get("subtype") == "success"

    @pytest.mark.asyncio
    async def test_nonzero_exit_with_tool_error_reports_error(self) -> None:
        """Exit code != 0 produces error regardless of stream content."""
        tool_error_event = json.dumps(
            {
                "type": "tool_use",
                "sessionID": "sess-r",
                "part": {
                    "tool": "bash",
                    "state": {
                        "input": {"command": "bad-cmd"},
                        "status": "completed",
                        "output": "",
                        "error": "command not found",
                    },
                },
            }
        )

        process = _FakeProcess(
            stdout_lines=[tool_error_event],
            stderr_lines=["fatal error"],
            returncode=1,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Do something"):
                messages.append(msg)

        result_msgs = [m for m in messages if m.type == "result"]
        assert len(result_msgs) >= 1
        final = result_msgs[-1]
        assert final.data.get("subtype") == "error"
        assert final.data.get("returncode") == 1

    """Prompt must be piped via stdin, not argv."""

    @pytest.mark.asyncio
    async def test_prompt_written_to_stdin(self) -> None:
        """execute_task must write the composed prompt to the process stdin."""
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-1",
                "part": {"type": "text", "text": "Done."},
            }
        )
        process = _FakeProcess(stdout_lines=[text_event], stderr_lines=[], returncode=0)
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        captured_process = None

        async def _fake_exec(*args, **kwargs):
            nonlocal captured_process
            captured_process = process
            return process

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec) as mock_exec:
            messages = []
            async for msg in runtime.execute_task("Hello big prompt"):
                messages.append(msg)

            # Prompt must NOT appear in argv
            call_args = mock_exec.call_args[0]
            assert "Hello big prompt" not in call_args

        # Prompt must have been written to stdin
        assert captured_process is not None
        assert b"Hello big prompt" in captured_process.stdin.written
        assert captured_process.stdin.closed

    def test_build_command_excludes_prompt(self) -> None:
        """_build_command must not include the prompt in argv."""
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        cmd = runtime._build_command(prompt="This should not appear")
        assert "This should not appear" not in cmd

    @pytest.mark.asyncio
    async def test_stdin_broken_pipe_yields_error_result(self) -> None:
        """BrokenPipeError on stdin write must not crash — falls through to stderr."""

        class _BrokenStdin(_FakeStdin):
            def write(self, data: bytes) -> None:
                raise BrokenPipeError("opencode exited early")

        process = _FakeProcess(
            stdout_lines=[],
            stderr_lines=["opencode: invalid argument"],
            returncode=1,
        )
        process.stdin = _BrokenStdin()

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Hello"):
                messages.append(msg)

        result_msgs = [m for m in messages if m.type == "result"]
        assert len(result_msgs) >= 1
        final = result_msgs[-1]
        # Should have fallen through to normal stderr reporting
        assert final.data.get("subtype") == "error"
        assert "invalid argument" in final.content


class TestStderrPriorityOnFailure:
    """On non-zero exit, stderr should win over stale last_content."""

    @pytest.mark.asyncio
    async def test_nonzero_exit_prefers_stderr(self) -> None:
        """When process exits non-zero, stderr should be the final message."""
        # Emit a text event (stale content), then exit with error + stderr
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-1",
                "part": {"type": "text", "text": "Calling tool: Bash..."},
            }
        )
        process = _FakeProcess(
            stdout_lines=[text_event],
            stderr_lines=["FATAL: segfault in plugin"],
            returncode=1,
        )
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Do something"):
                messages.append(msg)

        result_msgs = [m for m in messages if m.type == "result"]
        assert len(result_msgs) >= 1
        final = result_msgs[-1]
        assert "segfault" in final.content, (
            "On non-zero exit, stderr must win over stale last_content"
        )
        assert "Calling tool" not in final.content


class TestOpenCodeRuntimeChildEnv:
    """Test child environment construction."""

    def test_strips_ouroboros_vars(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        with patch.dict(
            "os.environ",
            {
                "OUROBOROS_AGENT_RUNTIME": "opencode",
                "OUROBOROS_LLM_BACKEND": "opencode",
            },
        ):
            env = runtime._build_child_env()
        assert "OUROBOROS_AGENT_RUNTIME" not in env
        assert "OUROBOROS_LLM_BACKEND" not in env

    def test_increments_depth(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        with patch.dict("os.environ", {"_OUROBOROS_DEPTH": "2"}):
            env = runtime._build_child_env()
        assert env["_OUROBOROS_DEPTH"] == "3"

    def test_depth_guard(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        with patch.dict("os.environ", {"_OUROBOROS_DEPTH": "5"}):
            with pytest.raises(RuntimeError, match="Maximum Ouroboros nesting depth"):
                runtime._build_child_env()
