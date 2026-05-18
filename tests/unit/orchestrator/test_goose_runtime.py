"""Unit tests for GooseCliRuntime."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.goose_runtime import GooseCliRuntime


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._buffer = bytearray("".join(f"{line}\n" for line in lines).encode())

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
    def __init__(self) -> None:
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], returncode: int = 0) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream([])
        self._returncode = returncode
        self.pid = 12345

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        return self._returncode


def test_goose_default_permission_mode_preserves_approval_gate() -> None:
    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project")

    assert runtime.permission_mode == "approve"
    assert runtime._build_child_env()["GOOSE_MODE"] == "approve"


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "accept_edits", "acceptedits"])
def test_goose_safe_permission_aliases_preserve_approval_gate(mode: str) -> None:
    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode=mode)

    assert runtime.permission_mode == "approve"
    assert runtime._build_child_env()["GOOSE_MODE"] == "approve"


@pytest.mark.parametrize("mode", ["auto", "bypassPermissions", "bypass_permissions"])
def test_goose_explicit_bypass_permission_aliases_map_to_auto(mode: str) -> None:
    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode=mode)

    assert runtime.permission_mode == "auto"
    assert runtime._build_child_env()["GOOSE_MODE"] == "auto"


def test_goose_command_uses_run_stream_json_and_stdin() -> None:
    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")

    command = runtime._build_command("/tmp/out.txt", resume_session_id="session-1")

    assert command[:4] == ["/tmp/goose", "run", "--output-format", "stream-json"]
    assert "--resume" in command
    assert command[-2:] == ["-i", "-"]
    assert "session-1" in command


def test_goose_runtime_makes_streamed_generated_handle_resumable() -> None:
    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")
    initial_handle = runtime._build_runtime_handle(None)

    assert initial_handle is not None
    assert runtime._resolve_resume_session_id(initial_handle) is None

    messages = runtime._convert_event(
        {"type": "assistant.message", "text": "Working"},
        initial_handle,
    )

    assert messages[0].resume_handle is not None
    assert messages[0].resume_handle.native_session_id == initial_handle.native_session_id
    assert runtime._resolve_resume_session_id(messages[0].resume_handle) == (
        initial_handle.native_session_id
    )


@pytest.mark.asyncio
async def test_goose_runtime_collects_stream_json_messages() -> None:
    stdout = [
        json.dumps({"type": "session.started", "session": {"name": "session-1"}}),
        json.dumps({"type": "assistant.message", "text": "Working"}),
        json.dumps({"type": "tool.call", "tool_name": "Bash", "input": {"command": "echo hi"}}),
        json.dumps({"type": "completed", "text": "Done"}),
    ]
    fake_process = _FakeProcess(stdout)

    async def fake_exec(*args: object, **kwargs: object) -> _FakeProcess:
        return fake_process

    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")

    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ) as mock_exec,
        patch.object(runtime, "_maybe_dispatch_skill_intercept", return_value=None),
    ):
        messages = [m async for m in runtime.execute_task("do it", tools=["Bash"])]

    assert mock_exec.call_args.args[:4] == ("/tmp/goose", "run", "--output-format", "stream-json")
    assert b"do it" in fake_process.stdin.written
    assert any(m.type == "system" and m.resume_handle for m in messages)
    assert any(m.content == "Working" for m in messages)
    assert any(m.tool_name == "Bash" for m in messages)
    assert messages[-1].is_final
    assert messages[-1].content == "Done"


@pytest.mark.asyncio
async def test_goose_runtime_preserves_generated_session_name_without_name_echo() -> None:
    stdout = [
        json.dumps({"type": "session.started", "session_id": "opaque-session-id"}),
        json.dumps({"type": "completed", "text": "Done"}),
    ]
    fake_process = _FakeProcess(stdout)

    async def fake_exec(*args: object, **kwargs: object) -> _FakeProcess:
        return fake_process

    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")

    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ) as mock_exec,
        patch.object(runtime, "_maybe_dispatch_skill_intercept", return_value=None),
    ):
        messages = [m async for m in runtime.execute_task("do it")]

    command = mock_exec.call_args.args
    name_index = command.index("-n")
    session_name = command[name_index + 1]

    assert session_name.startswith("ouroboros-")
    assert "--resume" not in command
    assert messages[-1].resume_handle is not None
    assert messages[-1].resume_handle.native_session_id == session_name

    resumed_process = _FakeProcess([json.dumps({"type": "completed", "text": "Again"})])

    async def fake_resume_exec(*args: object, **kwargs: object) -> _FakeProcess:
        return resumed_process

    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_resume_exec,
        ) as resume_exec,
        patch.object(runtime, "_maybe_dispatch_skill_intercept", return_value=None),
    ):
        await runtime.execute_task_to_result("again", resume_handle=messages[-1].resume_handle)

    resumed_command = resume_exec.call_args.args
    assert "--resume" in resumed_command
    assert resumed_command[resumed_command.index("-n") + 1] == session_name


@pytest.mark.asyncio
async def test_goose_runtime_honors_explicit_resume_session_id() -> None:
    fake_process = _FakeProcess([json.dumps({"type": "completed", "text": "Done"})])

    async def fake_exec(*args: object, **kwargs: object) -> _FakeProcess:
        return fake_process

    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")

    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ) as mock_exec,
        patch.object(runtime, "_maybe_dispatch_skill_intercept", return_value=None),
    ):
        await runtime.execute_task_to_result("do it", resume_session_id="existing-session")

    command = mock_exec.call_args.args
    assert "--resume" in command
    assert command[command.index("-n") + 1] == "existing-session"


@pytest.mark.asyncio
async def test_goose_runtime_completion_event_does_not_mask_nonzero_exit() -> None:
    fake_process = _FakeProcess(
        [json.dumps({"type": "completed", "text": "Done"})],
        returncode=2,
    )

    async def fake_exec(*args: object, **kwargs: object) -> _FakeProcess:
        return fake_process

    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")

    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        patch.object(runtime, "_maybe_dispatch_skill_intercept", return_value=None),
    ):
        messages = [m async for m in runtime.execute_task("do it")]

    assert messages[-1].is_final
    assert messages[-1].data["subtype"] == "error"
    assert messages[-1].data["returncode"] == 2
    assert messages[-1].data["error_type"] == "GooseCliError"


@pytest.mark.asyncio
async def test_goose_runtime_accumulates_stream_chunks_for_final_fallback() -> None:
    stdout = [
        json.dumps({"type": "session.started", "session": {"name": "session-1"}}),
        json.dumps({"type": "assistant.delta", "text": "Hel"}),
        json.dumps({"type": "assistant.delta", "text": "lo"}),
    ]
    fake_process = _FakeProcess(stdout)

    async def fake_exec(*args: object, **kwargs: object) -> _FakeProcess:
        return fake_process

    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")

    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        patch.object(runtime, "_maybe_dispatch_skill_intercept", return_value=None),
    ):
        result = await runtime.execute_task_to_result("say hello")

    assert result.is_ok
    assert result.value.final_message == "Hello"


@pytest.mark.asyncio
async def test_goose_runtime_keeps_tool_output_out_of_final_fallback() -> None:
    stdout = [
        json.dumps({"type": "session.started", "session": {"name": "session-1"}}),
        json.dumps({"type": "assistant.delta", "text": "Answer"}),
        json.dumps({"type": "tool.output", "name": "Bash", "output": "raw shell output"}),
        json.dumps({"type": "complete"}),
    ]
    fake_process = _FakeProcess(stdout)

    async def fake_exec(*args: object, **kwargs: object) -> _FakeProcess:
        return fake_process

    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")

    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        patch.object(runtime, "_maybe_dispatch_skill_intercept", return_value=None),
    ):
        result = await runtime.execute_task_to_result("run tool then answer")

    assert result.is_ok
    assert result.value.final_message == "Answer"
    assert any(
        message.type == "tool" and message.content == "raw shell output"
        for message in result.value.messages
    )


@pytest.mark.asyncio
async def test_goose_runtime_preserves_nested_completion_payload() -> None:
    stdout = [
        json.dumps({"type": "session.started", "session": {"name": "session-1"}}),
        json.dumps({"type": "completed", "result": {"text": "Done"}}),
    ]
    fake_process = _FakeProcess(stdout)

    async def fake_exec(*args: object, **kwargs: object) -> _FakeProcess:
        return fake_process

    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")

    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        patch.object(runtime, "_maybe_dispatch_skill_intercept", return_value=None),
    ):
        result = await runtime.execute_task_to_result("finish")

    assert result.is_ok
    assert result.value.final_message == "Done"


def test_goose_runtime_classifies_tool_failed_as_error_result() -> None:
    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")

    messages = runtime._convert_event({"type": "tool.failed", "error": "permission denied"}, None)

    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].is_error
    assert messages[0].content == "permission denied"


def test_goose_child_env_sets_nested_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "goose")
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "goose")
    monkeypatch.setenv("OUROBOROS_RUNTIME", "goose")
    monkeypatch.setenv("GOOSE_PROVIDER", "anthropic")
    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="approve")

    env = runtime._build_child_env()

    assert env["_OUROBOROS_NESTED"] == "1"
    assert env["GOOSE_MODE"] == "approve"
    assert env["GOOSE_WORKING_DIR"] == "/tmp/project"
    assert env["GOOSE_PROVIDER"] == "anthropic"
    assert "OUROBOROS_AGENT_RUNTIME" not in env
    assert "OUROBOROS_LLM_BACKEND" not in env
    assert "OUROBOROS_RUNTIME" not in env


def test_goose_child_env_does_not_map_ouroboros_llm_backend_to_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOSE_PROVIDER", raising=False)
    runtime = GooseCliRuntime(
        cli_path="/tmp/goose",
        cwd="/tmp/project",
        permission_mode="auto",
        llm_backend="claude_code",
    )

    assert "GOOSE_PROVIDER" not in runtime._build_child_env()


def test_goose_session_id_extraction_ignores_generic_tool_and_message_ids() -> None:
    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")

    assert (
        runtime._extract_event_session_id({"type": "session.started", "session_name": "sess-1"})
        == "sess-1"
    )
    assert (
        runtime._extract_event_session_id({"type": "session.started", "name": "sess-2"}) == "sess-2"
    )
    assert (
        runtime._extract_event_session_id(
            {"type": "session.started", "session": {"id": "opaque-id", "name": "stable-name"}}
        )
        == "stable-name"
    )
    assert (
        runtime._extract_event_session_id(
            {
                "type": "session.started",
                "session_id": "opaque-top-level-id",
                "session": {"id": "opaque-id", "name": "stable-name"},
            }
        )
        == "stable-name"
    )
    assert (
        runtime._extract_event_session_id(
            {"type": "session.started", "session_id": "opaque-top-level-id"}
        )
        is None
    )
    assert runtime._extract_event_session_id({"type": "tool.call", "name": "Bash"}) is None
    assert runtime._extract_event_session_id({"type": "message", "id": "msg-1"}) is None
