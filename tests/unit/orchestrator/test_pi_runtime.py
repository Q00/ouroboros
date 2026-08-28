"""Unit tests for PiRuntime."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import AgentMessage, ParamSupport, RuntimeHandle
from ouroboros.orchestrator.execution_authority import runtime_effect_capabilities_contract
from ouroboros.orchestrator.pi_runtime import PiRuntime
from ouroboros.orchestrator.runner import OrchestratorRunner

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
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project", model="fast")

    command = runtime._build_command(prompt="Do the task", resume_session_id="sess_123-OK")

    assert command == [
        "/tmp/pi",
        "--mode",
        "json",
        "--model",
        "fast",
        "--session",
        "sess_123-OK",
        "Do the task",
    ]


def test_build_command_passes_native_system_prompt_and_tools_when_supported() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, True)

    command = runtime._build_command(
        prompt="Do the task",
        system_prompt="Be a careful test runner.",
        tools=["Read", "Edit", "Bash", "custom_tool"],
    )

    assert command == [
        "/tmp/pi",
        "--mode",
        "json",
        "--append-system-prompt",
        "Be a careful test runner.",
        "--tools",
        "read,edit,bash,custom_tool",
        "Do the task",
    ]


def test_build_command_skips_native_flags_when_unsupported() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (False, False, False)

    command = runtime._build_command(
        prompt="Do the task",
        system_prompt="Be a careful test runner.",
        tools=["Read"],
    )

    assert command == ["/tmp/pi", "--mode", "json", "Do the task"]


@pytest.mark.parametrize(
    ("help_text", "expected_flags"),
    [
        ("Usage: pi [options]\n  --append-system-prompt <prompt>\n", (True, False, False)),
        ("Usage: pi [options]\n  --tools <tools>\n", (False, True, False)),
    ],
    ids=["append-system-prompt-only", "tools-only"],
)
def test_build_command_falls_back_when_probe_finds_only_one_native_param_flag(
    help_text: str,
    expected_flags: tuple[bool, bool, bool],
) -> None:
    with patch("ouroboros.orchestrator.pi_runtime.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = help_text
        mock_run.return_value.stderr = ""
        runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
        command = runtime._build_command(
            prompt="Do the task",
            system_prompt="Be a careful test runner.",
            tools=["Read"],
        )

    assert runtime._native_param_flags == expected_flags
    assert command == ["/tmp/pi", "--mode", "json", "Do the task"]
    mock_run.assert_called_once_with(
        ["/tmp/pi", "--help"],
        capture_output=True,
        text=True,
        timeout=10.0,
    )


@pytest.mark.asyncio
async def test_execute_task_does_not_probe_blocking_help_on_event_loop() -> None:
    """Capability probing completes before async execution begins."""
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Done."}],
                        },
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )
    with patch("ouroboros.orchestrator.pi_runtime.subprocess.run") as mock_probe:
        mock_probe.return_value.returncode = 0
        mock_probe.return_value.stdout = "Usage: pi [options]\\n"
        mock_probe.return_value.stderr = ""
        runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with (
        patch("ouroboros.orchestrator.pi_runtime.subprocess.run", side_effect=AssertionError),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert mock_probe.call_count == 1
    result = [message for message in messages if message.type == "result"][-1]
    assert result.is_error is not True


def test_capabilities_follow_probed_native_param_support() -> None:
    native = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    native._native_param_flags = (True, True, True)
    legacy = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    legacy._native_param_flags = (False, False, False)

    assert native.capabilities.system_prompt_support == ParamSupport.NATIVE
    assert native.capabilities.tool_restriction_support == ParamSupport.NATIVE
    assert native.capabilities.empty_tool_restriction_support == ParamSupport.NATIVE
    assert legacy.capabilities.system_prompt_support == ParamSupport.TRANSLATED
    assert legacy.capabilities.tool_restriction_support == ParamSupport.TRANSLATED
    assert legacy.capabilities.empty_tool_restriction_support == ParamSupport.IGNORED
    # Pi has no approval gate: permission mode stays ignored either way.
    assert native.capabilities.permission_mode_support == ParamSupport.IGNORED
    assert legacy.capabilities.permission_mode_support == ParamSupport.IGNORED


def test_capabilities_preserve_positive_and_empty_tool_authority_independently() -> None:
    partial = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    partial._native_param_flags = (True, True, False)

    caps = partial.capabilities

    assert caps.system_prompt_support == ParamSupport.NATIVE
    assert caps.tool_restriction_support == ParamSupport.NATIVE
    assert caps.empty_tool_restriction_support == ParamSupport.IGNORED


def test_no_tools_only_capability_changes_durable_execution_fingerprint() -> None:
    no_tools = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    no_tools._native_param_flags = (False, False, True)
    incapable = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    incapable._native_param_flags = (False, False, False)

    no_tools_contract = runtime_effect_capabilities_contract(no_tools)
    incapable_contract = runtime_effect_capabilities_contract(incapable)

    assert no_tools_contract["empty_tool_restriction_support"] == "native"
    assert incapable_contract["empty_tool_restriction_support"] == "ignored"
    assert no_tools_contract != incapable_contract
    assert OrchestratorRunner._execution_semantics_fingerprint(
        {"runtime_effect_capabilities": no_tools_contract}
    ) != OrchestratorRunner._execution_semantics_fingerprint(
        {"runtime_effect_capabilities": incapable_contract}
    )


def test_tracks_requested_permission_mode_and_declares_ignored_support() -> None:
    default_runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    requested_runtime = PiRuntime(
        cli_path="/tmp/pi",
        cwd="/tmp/project",
        permission_mode="acceptEdits",
    )

    assert default_runtime.permission_mode is None
    assert default_runtime.permission_mode_requested is False
    assert requested_runtime.permission_mode == "acceptEdits"
    assert requested_runtime.permission_mode_requested is True
    assert requested_runtime.capabilities.system_prompt_support is ParamSupport.TRANSLATED
    assert requested_runtime.capabilities.tool_restriction_support is ParamSupport.TRANSLATED
    assert requested_runtime.capabilities.permission_mode_support is ParamSupport.IGNORED
    assert requested_runtime.capabilities.session_signals.after_turn_delivery is True
    assert requested_runtime.capabilities.empty_tool_restriction_support is ParamSupport.IGNORED


def test_build_command_rejects_unsafe_resume_session_id() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with pytest.raises(ValueError, match="Invalid resume_session_id"):
        runtime._build_command(prompt="Do it", resume_session_id="../../bad")


def test_extract_content_delta_reads_documented_assistant_message_event() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    delta = runtime._extract_content_delta(
        {
            "type": "message_update",
            "message": {"role": "assistant", "content": []},
            "assistantMessageEvent": {"type": "text_delta", "delta": "Hello"},
        }
    )

    assert delta == "Hello"


def test_extract_content_delta_ignores_documented_text_end_event() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    delta = runtime._extract_content_delta(
        {
            "type": "message_update",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
            "assistantMessageEvent": {"type": "text_end", "content": "Hello"},
        }
    )

    assert delta is None


def test_extract_final_content_reads_agent_end_messages() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    content = runtime._extract_final_content(
        {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": "request"},
                {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
            ],
        }
    )

    assert content == "Done."


def test_extract_error_content_reads_agent_end_stop_reason_error() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    content = runtime._extract_error_content(
        {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": "request"},
                {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "OpenAI API error (401)",
                },
            ],
        }
    )

    assert content == "OpenAI API error (401)"


def test_build_runtime_handle_from_session_header() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project", permission_mode="acceptEdits")

    sid = runtime._extract_session_id({"type": "session", "id": "session-1"})
    handle = runtime._build_runtime_handle(sid)

    assert handle is not None
    assert handle.backend == "pi"
    assert handle.kind == "agent_runtime"
    assert handle.native_session_id == "session-1"
    assert handle.cwd == _EXPECTED_CWD
    assert handle.approval_mode == "acceptEdits"


@pytest.mark.asyncio
async def test_execute_task_dispatches_ooo_skill_before_spawning_pi() -> None:
    captured: dict[str, Any] = {}
    dispatched_handle = RuntimeHandle(backend="pi", native_session_id="skill-session")

    async def skill_dispatcher(intercept: Any, current_handle: RuntimeHandle | None):
        captured["skill_name"] = intercept.skill_name
        captured["command_prefix"] = intercept.command_prefix
        captured["current_handle"] = current_handle
        return (
            AgentMessage(
                type="tool",
                content="Calling tool: ouroboros_start_auto",
                tool_name=intercept.mcp_tool,
                data={"command_prefix": intercept.command_prefix},
                resume_handle=dispatched_handle,
            ),
            AgentMessage(
                type="result",
                content="auto started",
                data={"subtype": "success"},
                resume_handle=dispatched_handle,
            ),
        )

    runtime = PiRuntime(
        cli_path="/tmp/pi",
        cwd="/tmp/project",
        skill_dispatcher=skill_dispatcher,
    )

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        messages = [msg async for msg in runtime.execute_task("ooo auto Build docs")]

    mock_exec.assert_not_called()
    assert captured["skill_name"] == "auto"
    assert captured["command_prefix"] == "ooo auto"
    assert [message.content for message in messages] == [
        "Calling tool: ouroboros_start_auto",
        "auto started",
    ]
    assert messages[-1].resume_handle == dispatched_handle


def test_pi_runtime_accepts_stream_timeout_overrides() -> None:
    runtime = PiRuntime(
        cli_path="/tmp/pi",
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
                    "message": {"role": "assistant", "content": []},
                    "assistantMessageEvent": {"type": "text_delta", "delta": "Hel"},
                }
            ),
            _jsonl_event(
                {
                    "type": "message_update",
                    "message": {"role": "assistant", "content": []},
                    "assistantMessageEvent": {"type": "text_delta", "delta": "lo"},
                }
            ),
            _jsonl_event(
                {
                    "type": "message_update",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hello"}],
                    },
                    "assistantMessageEvent": {"type": "text_end", "content": "Hello"},
                }
            ),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}
                    ],
                }
            ),
        ],
        stderr_lines=[
            "Extension loading...",
            "Extension loaded: /loop, /loop-stop, /loop-list, /loop-stop-all",
        ],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert mock_exec.call_args.args == ("/tmp/pi", "--mode", "json", "Do it")
    assert mock_exec.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert [m.content for m in messages if m.type == "assistant"] == ["Hel", "lo"]
    result = [m for m in messages if m.type == "result"][-1]
    assert result.content == "Hello"
    assert result.data == {"subtype": "success", "returncode": 0}
    assert result.resume_handle is not None
    assert result.resume_handle.native_session_id == "session-1"


@pytest.mark.asyncio
async def test_execute_task_passes_params_natively_when_supported() -> None:
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, True)

    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        _ = [
            msg async for msg in runtime.execute_task("Do it", tools=["Read"], system_prompt="Sys")
        ]

    assert mock_exec.call_args.args == (
        "/tmp/pi",
        "--mode",
        "json",
        "--append-system-prompt",
        "Sys",
        "--tools",
        "read",
        "Do it",
    )


@pytest.mark.asyncio
async def test_execute_task_composes_params_into_prompt_when_unsupported() -> None:
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (False, False, False)

    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        _ = [
            msg async for msg in runtime.execute_task("Do it", tools=["Read"], system_prompt="Sys")
        ]

    assert mock_exec.call_args.args == (
        "/tmp/pi",
        "--mode",
        "json",
        "## System Instructions\nSys\n\n## Tooling Guidance\nPrefer these tools:\n- Read\n\nDo it",
    )


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
            )
        ],
        stderr_lines=["pi failed"],
        returncode=7,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    result = [m for m in messages if m.type == "result"][-1]
    assert result.is_error
    assert result.content == "pi failed"
    assert result.data["subtype"] == "error"
    assert result.data["returncode"] == 7
    assert result.data["error_type"] == "PiError"


@pytest.mark.asyncio
async def test_agent_stop_reason_error_overrides_zero_exit() -> None:
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "request"}]},
                        {
                            "role": "assistant",
                            "content": [],
                            "stopReason": "error",
                            "errorMessage": "OpenAI API error (401)",
                        },
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    result = [m for m in messages if m.type == "result"][-1]
    assert result.is_error
    assert result.content == "OpenAI API error (401)"
    assert result.data == {
        "subtype": "error",
        "returncode": 0,
        "error_type": "PiError",
    }
    assert result.resume_handle is not None
    assert result.resume_handle.native_session_id == "session-1"


@pytest.mark.asyncio
async def test_execute_task_reports_malformed_json_event() -> None:
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            "not-json",
        ],
        stderr_lines=[],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    result = [m for m in messages if m.type == "result"][-1]
    assert result.is_error
    assert result.content == "Malformed Pi JSON event: not-json"
    assert result.data == {
        "subtype": "error",
        "error_type": "MalformedPiEvent",
    }
    assert result.resume_handle is not None
    assert result.resume_handle.native_session_id == "session-1"
    assert process.terminated


@pytest.mark.asyncio
async def test_execute_task_to_result_maps_malformed_event_to_provider_error() -> None:
    process = _FakeProcess(stdout_lines=["[bad-json]"], stderr_lines=[], returncode=0)
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process):
        result = await runtime.execute_task_to_result("Do it")

    assert result.is_err
    error = result.error
    assert error is not None
    assert error.provider == "pi"
    assert error.message == "Malformed Pi JSON event: [bad-json]"
    assert error.details == {"messages": ["Malformed Pi JSON event: [bad-json]"]}
    assert process.terminated


def test_runtime_factory_constructs_pi_runtime() -> None:
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    runtime = create_agent_runtime(backend="pi", cli_path="/tmp/pi", cwd="/tmp/project")

    assert isinstance(runtime, PiRuntime)
    assert runtime.runtime_backend == "pi"
    assert runtime.working_directory == _EXPECTED_CWD


def test_runtime_factory_passes_pi_stream_timeout_overrides() -> None:
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    with patch(
        "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
        return_value=object(),
    ):
        runtime = create_agent_runtime(
            backend="pi",
            cli_path="/tmp/pi",
            cwd="/tmp/project",
            startup_output_timeout_seconds=0,
            stdout_idle_timeout_seconds=0,
        )

    assert isinstance(runtime, PiRuntime)
    assert runtime._startup_output_timeout_seconds is None
    assert runtime._stdout_idle_timeout_seconds is None


def test_runtime_handle_accepts_pi_backend() -> None:
    handle = RuntimeHandle(backend="pi_cli", native_session_id="session-1")

    assert handle.backend == "pi"


# ---------------------------------------------------------------------------
# Regression tests for PR #2203 review blockers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "native_param_flags",
    [
        (False, False, True),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
    ids=["no-tools-only", "append-and-no-tools", "tools-and-no-tools", "all"],
)
def test_build_command_emits_no_tools_for_explicit_empty_list(
    native_param_flags: tuple[bool, bool, bool],
) -> None:
    """tools=[] uses independently available --no-tools in every probe state."""
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = native_param_flags

    command = runtime._build_command(prompt="Do the task", tools=[])

    assert "--no-tools" in command
    assert "--tools" not in command
    assert command == ["/tmp/pi", "--mode", "json", "--no-tools", "Do the task"]


def test_build_command_omits_tools_flag_for_none() -> None:
    """tools=None means 'use defaults'; neither --tools nor --no-tools should appear."""
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, True)

    command = runtime._build_command(prompt="Do the task", tools=None)

    assert "--tools" not in command
    assert "--no-tools" not in command
    assert command == ["/tmp/pi", "--mode", "json", "Do the task"]


def test_build_command_empty_tools_without_no_tools_support() -> None:
    """Command assembly omits unsafe flags; execute_task rejects before spawn."""
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, False)  # has --tools but not --no-tools

    command = runtime._build_command(prompt="Do the task", tools=[])

    assert "--tools" not in command
    assert "--no-tools" not in command
    assert command == ["/tmp/pi", "--mode", "json", "Do the task"]


def test_glob_maps_to_pi_find_builtin() -> None:
    """Blocker 2: Glob must map to Pi's 'find', not nonexistent 'glob'."""
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, True)

    command = runtime._build_command(prompt="Do it", tools=["Glob"])

    assert command == ["/tmp/pi", "--mode", "json", "--tools", "find", "Do it"]


def test_command_and_execute_map_to_pi_bash_builtin() -> None:
    """Command and Execute must map to Pi's 'bash' built-in."""
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, True)

    command = runtime._build_command(prompt="Do it", tools=["Command", "Execute"])

    assert command == ["/tmp/pi", "--mode", "json", "--tools", "bash,bash", "Do it"]


def test_all_known_ouroboros_builtins_map_to_valid_pi_tools() -> None:
    """Every known Ouroboros built-in must map to a documented Pi tool name."""
    from ouroboros.orchestrator.pi_runtime import _PI_TOOL_FLAG_NAMES

    # Pi's documented built-in tools
    pi_builtins = {"read", "write", "edit", "bash", "grep", "find", "ls"}

    for ouroboros_name, pi_name in _PI_TOOL_FLAG_NAMES.items():
        assert pi_name in pi_builtins, (
            f"Ouroboros tool {ouroboros_name!r} maps to {pi_name!r} "
            f"which is not a documented Pi built-in: {pi_builtins}"
        )


def test_ls_maps_to_pi_ls_builtin() -> None:
    """LS/Ls must map to Pi's 'ls' built-in."""
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, True)

    command_upper = runtime._build_command(prompt="Do it", tools=["LS"])
    command_title = runtime._build_command(prompt="Do it", tools=["Ls"])

    assert "--tools" in command_upper
    assert command_upper[command_upper.index("--tools") + 1] == "ls"
    assert command_title[command_title.index("--tools") + 1] == "ls"


@pytest.mark.parametrize(
    "native_param_flags",
    [
        (False, False, True),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
    ids=["no-tools-only", "append-and-no-tools", "tools-and-no-tools", "all"],
)
@pytest.mark.asyncio
async def test_execute_task_emits_no_tools_for_empty_list(
    native_param_flags: tuple[bool, bool, bool],
) -> None:
    """Execution honors independently probed --no-tools before spawning Pi."""
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = native_param_flags

    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        _ = [msg async for msg in runtime.execute_task("Do it", tools=[])]

    args = mock_exec.call_args.args
    assert "--no-tools" in args
    assert "--tools" not in args


@pytest.mark.asyncio
async def test_execute_task_tools_none_omits_all_tool_flags() -> None:
    """End-to-end: execute_task with tools=None should omit --tools and --no-tools."""
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, True)

    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        _ = [msg async for msg in runtime.execute_task("Do it", tools=None)]

    args = mock_exec.call_args.args
    assert "--no-tools" not in args
    assert "--tools" not in args


# ---------------------------------------------------------------------------
# PR #2203 Round 2: Partial support negotiation + fail-closed regressions
# ---------------------------------------------------------------------------


def test_negotiation_partial_pi_tools_empty_reports_ignored() -> None:
    """When Pi has --tools but not --no-tools, requesting tools=[] must surface
    as IGNORED through the parameter negotiation layer — never silently widen."""
    from ouroboros.orchestrator.runtime_param_negotiation import (
        negotiate_execution_params,
    )

    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, False)  # partial support

    degradations = negotiate_execution_params(
        runtime.capabilities,
        system_prompt=None,
        tools=[],
        permission_mode=None,
    )

    assert len(degradations) == 1
    d = degradations[0]
    assert d.parameter == "tools"
    assert d.support == ParamSupport.IGNORED
    assert "silently dropped" in d.detail


def test_negotiation_partial_pi_tools_nonempty_is_native() -> None:
    """A paired Pi path enforces non-empty tools despite lacking --no-tools."""
    from ouroboros.orchestrator.runtime_param_negotiation import (
        negotiate_execution_params,
    )

    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, False)

    degradations = negotiate_execution_params(
        runtime.capabilities,
        system_prompt=None,
        tools=["Read", "Edit"],
        permission_mode=None,
    )

    assert degradations == ()


def test_negotiation_full_pi_tools_empty_no_degradation() -> None:
    """Full Pi support (has --tools AND --no-tools): tools=[] is NATIVE, no degradation."""
    from ouroboros.orchestrator.runtime_param_negotiation import (
        negotiate_execution_params,
    )

    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, True)

    degradations = negotiate_execution_params(
        runtime.capabilities,
        system_prompt=None,
        tools=[],
        permission_mode=None,
    )

    assert len(degradations) == 0


def test_negotiation_no_tools_only_pi_empty_has_no_degradation() -> None:
    """Independent --no-tools authority makes tools=[] natively enforceable."""
    from ouroboros.orchestrator.runtime_param_negotiation import (
        negotiate_execution_params,
    )

    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (False, False, True)

    assert (
        negotiate_execution_params(
            runtime.capabilities,
            system_prompt=None,
            tools=[],
            permission_mode=None,
        )
        == ()
    )


@pytest.mark.parametrize(
    "native_param_flags",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, False),
    ],
    ids=["none", "append-only", "tools-only", "paired-without-no-tools"],
)
@pytest.mark.asyncio
async def test_execute_task_fails_closed_tools_empty_without_no_tools_support(
    native_param_flags: tuple[bool, bool, bool],
) -> None:
    """Every probe state lacking --no-tools must reject tools=[] before spawn."""
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = native_param_flags

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        messages = [msg async for msg in runtime.execute_task("Do it", tools=[])]

    # Must NOT have spawned a process — fail closed means no execution.
    mock_exec.assert_not_called()

    # Must emit exactly one error result.
    assert len(messages) == 1
    result = messages[0]
    assert result.type == "result"
    assert result.is_error is True
    assert "ToolRestrictionUnenforced" in (result.data or {}).get("error_type", "")
    assert "cannot enforce tools=[]" in result.content
    assert (result.data or {}).get("requested") == []
    assert (result.data or {}).get("effective") == "unrestricted"


@pytest.mark.asyncio
async def test_execute_task_fails_closed_tools_empty_from_tools_only_help_probe() -> None:
    with patch("ouroboros.orchestrator.pi_runtime.subprocess.run") as mock_probe:
        mock_probe.return_value.returncode = 0
        mock_probe.return_value.stdout = "Usage: pi [options]\n  --tools <tools>\n"
        mock_probe.return_value.stderr = ""
        runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        messages = [msg async for msg in runtime.execute_task("Do it", tools=[])]

    assert runtime._native_param_flags == (False, True, False)
    mock_exec.assert_not_called()
    assert len(messages) == 1
    result = messages[0]
    assert result.type == "result"
    assert result.is_error is True
    assert (result.data or {}).get("error_type") == "ToolRestrictionUnenforced"
    assert (result.data or {}).get("requested") == []
    assert (result.data or {}).get("effective") == "unrestricted"


@pytest.mark.asyncio
async def test_execute_task_translates_nonempty_tools_from_tools_only_help_probe() -> None:
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )
    with patch("ouroboros.orchestrator.pi_runtime.subprocess.run") as mock_probe:
        mock_probe.return_value.returncode = 0
        mock_probe.return_value.stdout = "Usage: pi [options]\n  --tools <tools>\n"
        mock_probe.return_value.stderr = ""
        runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        messages = [msg async for msg in runtime.execute_task("Do it", tools=["Read"])]

    assert runtime._native_param_flags == (False, True, False)
    assert mock_exec.call_args.args == (
        "/tmp/pi",
        "--mode",
        "json",
        "## Tooling Guidance\nPrefer these tools:\n- Read\n\nDo it",
    )
    result = [message for message in messages if message.type == "result"][-1]
    assert result.is_error is not True


@pytest.mark.asyncio
async def test_execute_task_succeeds_tools_empty_full_support() -> None:
    """Full Pi (has --no-tools): tools=[] must succeed with --no-tools in command."""
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, True)

    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        messages = [msg async for msg in runtime.execute_task("Do it", tools=[])]

    # Process WAS spawned with --no-tools
    mock_exec.assert_called_once()
    args = mock_exec.call_args.args
    assert "--no-tools" in args
    # Last message should be successful result
    result = [m for m in messages if m.type == "result"][-1]
    assert result.is_error is not True


@pytest.mark.asyncio
async def test_execute_task_tools_nonempty_partial_support_proceeds() -> None:
    """Partial Pi with a non-empty tools list should proceed (--tools works)."""
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, False)

    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        messages = [msg async for msg in runtime.execute_task("Do it", tools=["Read"])]

    # Process WAS spawned with --tools (non-empty list works even without --no-tools)
    mock_exec.assert_called_once()
    args = mock_exec.call_args.args
    assert "--tools" in args
    assert "read" in args[args.index("--tools") + 1]
    result = [m for m in messages if m.type == "result"][-1]
    assert result.is_error is not True


@pytest.mark.asyncio
async def test_execute_task_tools_none_partial_support_proceeds() -> None:
    """Partial Pi with tools=None (default tools) should proceed without any flag."""
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    runtime._native_param_flags = (True, True, False)

    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        messages = [msg async for msg in runtime.execute_task("Do it", tools=None)]

    mock_exec.assert_called_once()
    args = mock_exec.call_args.args
    assert "--tools" not in args
    assert "--no-tools" not in args
    result = [m for m in messages if m.type == "result"][-1]
    assert result.is_error is not True
