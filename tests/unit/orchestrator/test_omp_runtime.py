"""Unit tests for OmpRuntime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import ParamSupport, RuntimeHandle
from ouroboros.orchestrator.execution_authority import runtime_effect_capabilities_contract
from ouroboros.orchestrator.omp_runtime import _OMP_TOOL_FLAG_NAMES, OMP_BUILTIN_TOOLS, OmpRuntime

_EXPECTED_CWD = str(Path("/tmp/project").resolve())


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


class _FakeProcess:
    def __init__(
        self, stdout_lines: list[str], stderr_lines: list[str], returncode: int = 0
    ) -> None:
        self.stdin = None
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self._returncode = returncode
        self.returncode = None
        self.pid = 1234
        self.terminated = False

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._returncode

    def kill(self) -> None:
        self.returncode = self._returncode


def _jsonl_event(event: dict[str, object]) -> str:
    return json.dumps(event)


def test_build_command_uses_documented_json_prompt_argument() -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project", model="fast")

    command = runtime._build_command(prompt="Do the task", resume_session_id="sess_123-OK")

    assert command == [
        "/tmp/omp",
        "--mode",
        "json",
        "--model",
        "fast",
        "--resume",
        "sess_123-OK",
        "Do the task",
    ]


def test_build_command_omits_default_model_sentinel() -> None:
    """The generic "default" sentinel must not reach omp's fuzzy --model."""
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project", model="default")

    command = runtime._build_command(prompt="Do the task")

    assert command == ["/tmp/omp", "--mode", "json", "Do the task"]


def test_build_command_passes_system_prompt_and_tools_natively() -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    command = runtime._build_command(
        prompt="Do the task",
        system_prompt="Be terse.",
        tools=["Read", "Grep"],
    )

    assert command == [
        "/tmp/omp",
        "--mode",
        "json",
        "--append-system-prompt",
        "Be terse.",
        "--tools",
        "read,grep",
        "Do the task",
    ]


def test_build_command_rejects_unsafe_resume_session_id() -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    with pytest.raises(ValueError, match="Invalid resume_session_id"):
        runtime._build_command(prompt="Do it", resume_session_id="../../bad")


@pytest.mark.parametrize(
    "native_tools",
    [
        ["Read"],
        ["Glob"],
        ["LS"],
        ["Ls"],
        ["Bash"],
        ["Command"],
        ["Execute"],
        ["Write", "Edit", "Grep"],
    ],
)
def test_build_command_maps_tool_vocabulary(native_tools: list[str]) -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    command = runtime._build_command(prompt="Do it", tools=native_tools)
    mapped = command[command.index("--tools") + 1]

    assert all(name in OMP_BUILTIN_TOOLS for name in mapped.split(","))


def test_glob_maps_to_omp_glob_builtin() -> None:
    """Glob maps to OMP's canonical `glob` tool, not Pi's `find`."""
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    command = runtime._build_command(prompt="Do it", tools=["Glob"])

    assert command == ["/tmp/omp", "--mode", "json", "--tools", "glob", "Do it"]


def test_ls_maps_to_omp_glob_builtin() -> None:
    """OMP has no `ls` built-in; LS maps to `glob` (directory enumeration)."""
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    command = runtime._build_command(prompt="Do it", tools=["LS"])

    assert command == ["/tmp/omp", "--mode", "json", "--tools", "glob", "Do it"]


def test_unknown_tool_names_pass_through() -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    command = runtime._build_command(prompt="Do it", tools=["mcp__custom_tool"])

    assert command == [
        "/tmp/omp",
        "--mode",
        "json",
        "--tools",
        "mcp__custom_tool",
        "Do it",
    ]


def test_all_known_ouroboros_builtins_map_to_valid_omp_tools() -> None:
    """Every known Ouroboros built-in must map to a documented OMP tool name."""
    for ouroboros_name, omp_name in _OMP_TOOL_FLAG_NAMES.items():
        assert omp_name in OMP_BUILTIN_TOOLS, (
            f"Ouroboros tool {ouroboros_name!r} maps to {omp_name!r} "
            f"which is not a documented OMP built-in: {sorted(OMP_BUILTIN_TOOLS)}"
        )


def test_build_command_emits_no_tools_for_explicit_empty_list() -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    command = runtime._build_command(prompt="Do the task", tools=[])

    assert command == ["/tmp/omp", "--mode", "json", "--no-tools", "Do the task"]


def test_build_command_omits_tool_flags_for_none() -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    command = runtime._build_command(prompt="Do the task", tools=None)

    assert command == ["/tmp/omp", "--mode", "json", "Do the task"]


def test_capabilities_declare_native_support_without_probe() -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")
    caps = runtime.capabilities

    assert caps.system_prompt_support is ParamSupport.NATIVE
    assert caps.tool_restriction_support is ParamSupport.NATIVE
    assert caps.empty_tool_restriction_support is ParamSupport.NATIVE
    assert caps.permission_mode_support is ParamSupport.IGNORED
    assert caps.skill_dispatch is True
    assert caps.targeted_resume is True


def test_no_tools_capability_changes_durable_execution_fingerprint() -> None:
    full = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project").capabilities
    no_tools = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project").capabilities

    assert runtime_effect_capabilities_contract(full)
    assert runtime_effect_capabilities_contract(no_tools)


def test_tracks_requested_permission_mode_and_declares_ignored_support() -> None:
    default_runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")
    requested_runtime = OmpRuntime(
        cli_path="/tmp/omp", cwd="/tmp/project", permission_mode="acceptEdits"
    )

    assert default_runtime.permission_mode_requested is False
    assert requested_runtime.permission_mode_requested is True
    assert requested_runtime.permission_mode == "acceptEdits"
    assert requested_runtime.capabilities.permission_mode_support is ParamSupport.IGNORED


def test_extract_content_delta_reads_documented_assistant_message_event() -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    delta = runtime._extract_content_delta(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "Hello"},
        }
    )

    assert delta == "Hello"


def test_extract_content_delta_ignores_thinking_delta_events() -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    delta = runtime._extract_content_delta(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "thinking_delta", "delta": "internal"},
        }
    )

    assert delta is None


def test_extract_final_content_reads_agent_end_messages() -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    content = runtime._extract_final_content(
        {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": "Do it"},
                {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
            ],
        }
    )

    assert content == "Done."


def test_extract_error_content_reads_agent_end_stop_reason_error() -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    content = runtime._extract_error_content(
        {
            "type": "agent_end",
            "messages": [
                {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "provider auth failed",
                }
            ],
        }
    )

    assert content == "provider auth failed"


def test_build_runtime_handle_from_session_header() -> None:
    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project", permission_mode="acceptEdits")

    handle = runtime._build_runtime_handle("session-1")

    assert handle is not None
    assert handle.backend == "omp"
    assert handle.native_session_id == "session-1"
    assert handle.approval_mode == "acceptEdits"


def test_runtime_accepts_stream_timeout_overrides() -> None:
    runtime = OmpRuntime(
        cli_path="/tmp/omp",
        cwd="/tmp/project",
        startup_output_timeout_seconds=0,
        stdout_idle_timeout_seconds=0,
    )

    assert runtime._startup_output_timeout_seconds is None
    assert runtime._stdout_idle_timeout_seconds is None


@pytest.mark.asyncio
async def test_execute_task_streams_delta_and_final_result() -> None:
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "Hello"},
                }
            ),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Hello world"}]}
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )

    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    with patch(
        "ouroboros.orchestrator.omp_runtime.asyncio.create_subprocess_exec",
        return_value=process,
    ):
        result = await runtime.execute_task_to_result(prompt="Say hello")

    assert result.is_ok
    assert result.value.final_message == "Hello world"
    assert result.value.session_id == "session-1"
    assert result.value.resume_handle is not None
    assert result.value.resume_handle.native_session_id == "session-1"


@pytest.mark.asyncio
async def test_execute_task_passes_resume_flag() -> None:
    captured: dict[str, Any] = {}

    async def fake_exec(*command: str, **_kwargs: Any) -> _FakeProcess:
        captured["command"] = command
        return _FakeProcess(
            stdout_lines=[
                _jsonl_event({"type": "session", "id": "session-2"}),
                _jsonl_event(
                    {
                        "type": "agent_end",
                        "messages": [
                            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
                        ],
                    }
                ),
            ],
            stderr_lines=[],
            returncode=0,
        )

    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    with patch(
        "ouroboros.orchestrator.omp_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        result = await runtime.execute_task_to_result(
            prompt="Continue",
            resume_handle=RuntimeHandle(backend="omp", native_session_id="session-1"),
        )

    assert result.is_ok
    assert "--resume" in captured["command"]
    assert captured["command"][captured["command"].index("--resume") + 1] == "session-1"


@pytest.mark.asyncio
async def test_execute_task_emits_no_tools_for_empty_list() -> None:
    captured: dict[str, Any] = {}

    async def fake_exec(*command: str, **_kwargs: Any) -> _FakeProcess:
        captured["command"] = command
        return _FakeProcess(
            stdout_lines=[
                _jsonl_event(
                    {
                        "type": "agent_end",
                        "messages": [
                            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
                        ],
                    }
                ),
            ],
            stderr_lines=[],
            returncode=0,
        )

    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    with patch(
        "ouroboros.orchestrator.omp_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        result = await runtime.execute_task_to_result(prompt="Do it", tools=[])

    assert result.is_ok
    assert "--no-tools" in captured["command"]
    assert "--tools" not in captured["command"]


@pytest.mark.asyncio
async def test_execute_task_tools_none_omits_all_tool_flags() -> None:
    captured: dict[str, Any] = {}

    async def fake_exec(*command: str, **_kwargs: Any) -> _FakeProcess:
        captured["command"] = command
        return _FakeProcess(
            stdout_lines=[
                _jsonl_event(
                    {
                        "type": "agent_end",
                        "messages": [
                            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
                        ],
                    }
                ),
            ],
            stderr_lines=[],
            returncode=0,
        )

    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    with patch(
        "ouroboros.orchestrator.omp_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        result = await runtime.execute_task_to_result(prompt="Do it", tools=None)

    assert result.is_ok
    assert "--tools" not in captured["command"]
    assert "--no-tools" not in captured["command"]


@pytest.mark.asyncio
async def test_agent_end_does_not_mask_nonzero_exit() -> None:
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Looks done"}]}
                    ],
                }
            ),
        ],
        stderr_lines=["omp: model not found"],
        returncode=1,
    )

    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    with patch(
        "ouroboros.orchestrator.omp_runtime.asyncio.create_subprocess_exec",
        return_value=process,
    ):
        result = await runtime.execute_task_to_result(prompt="Do it")

    assert result.is_err
    assert "model not found" in result.error.message
    assert result.error.provider == "omp"


@pytest.mark.asyncio
async def test_agent_stop_reason_error_overrides_zero_exit() -> None:
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "stopReason": "error",
                            "errorMessage": "zai API error (429)",
                        }
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )

    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    with patch(
        "ouroboros.orchestrator.omp_runtime.asyncio.create_subprocess_exec",
        return_value=process,
    ):
        result = await runtime.execute_task_to_result(prompt="Do it")

    assert result.is_err
    assert "429" in result.error.message


@pytest.mark.asyncio
async def test_execute_task_reports_malformed_json_event() -> None:
    process = _FakeProcess(stdout_lines=["[bad-json]"], stderr_lines=[], returncode=0)

    runtime = OmpRuntime(cli_path="/tmp/omp", cwd="/tmp/project")

    with patch(
        "ouroboros.orchestrator.omp_runtime.asyncio.create_subprocess_exec",
        return_value=process,
    ):
        result = await runtime.execute_task_to_result(prompt="Do it")

    assert result.is_err
    assert process.terminated


def test_runtime_factory_constructs_omp_runtime() -> None:
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    runtime = create_agent_runtime(backend="omp", cwd="/tmp/project")

    assert isinstance(runtime, OmpRuntime)
    assert runtime.working_directory == _EXPECTED_CWD


def test_runtime_factory_passes_omp_stream_timeout_overrides() -> None:
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    runtime = create_agent_runtime(
        backend="omp",
        cwd="/tmp/project",
        startup_output_timeout_seconds=0,
        stdout_idle_timeout_seconds=0,
    )

    assert isinstance(runtime, OmpRuntime)
    assert runtime._startup_output_timeout_seconds is None
    assert runtime._stdout_idle_timeout_seconds is None


def test_runtime_handle_accepts_omp_backend() -> None:
    handle = RuntimeHandle(backend="omp_cli", native_session_id="session-1")

    assert handle.backend == "omp"
