"""Focused unit tests for the megacode harness runtime.

Covers the ``harness -p`` headless contract that differs from the Codex base:

1. ``harness -p --output-format stream-json --mode ouroboros-worker`` with the
   prompt on stdin, ``--sandbox off`` only for ``bypassPermissions``.
2. The documented ``stream-json`` records mapped onto Ouroboros messages,
   including correlated tool start/result pairs and exit-code verdicts.
3. No targeted resume (a headless harness run cannot continue a session).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.megacode_runtime import MEGACODE_WORKER_MODE, MegacodeRuntime
from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._buffer = bytearray("".join(f"{line}\n" for line in lines).encode())

    async def readline(self) -> bytes:
        if not self._buffer:
            return b""
        idx = self._buffer.find(b"\n")
        if idx < 0:
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        data = bytes(self._buffer[: idx + 1])
        del self._buffer[: idx + 1]
        return data

    async def read(self, n: int = -1) -> bytes:
        del n
        data = bytes(self._buffer)
        self._buffer.clear()
        return data


class _FakeStdin:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], returncode: int = 0) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream([])
        self.pid = 4323
        self.returncode: int | None = None
        self._returncode = returncode

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self._returncode


def _test_cli_path() -> str:
    return str(Path(os.environ["OUROBOROS_TEST_CLI_DIR"]) / "harness")


def _make_runtime(**kwargs: object) -> MegacodeRuntime:
    return MegacodeRuntime(cli_path=_test_cli_path(), **kwargs)  # type: ignore[arg-type]


def _convert(runtime: MegacodeRuntime, record: dict[str, object]):
    event = runtime._parse_json_event(json.dumps(record))
    assert event is not None
    return runtime._convert_event(event, current_handle=None)


# --- Command construction ---------------------------------------------------


def test_build_command_reads_prompt_from_stdin_in_worker_mode() -> None:
    runtime = _make_runtime()
    command = runtime._build_command("/tmp/out", prompt="do the thing")

    assert command[1:] == ["-p", "--output-format", "stream-json", "--mode", MEGACODE_WORKER_MODE]
    assert "do the thing" not in command
    assert runtime._feeds_prompt_via_stdin() is True
    assert runtime._requires_process_stdin() is True


def test_build_command_turns_sandbox_off_only_for_bypass_permissions() -> None:
    assert "--sandbox" not in _make_runtime()._build_command("/tmp/out")
    assert "--sandbox" not in _make_runtime(permission_mode="acceptEdits")._build_command("/tmp/o")
    command = _make_runtime(permission_mode="bypassPermissions")._build_command("/tmp/out")
    assert command[command.index("--sandbox") + 1] == "off"


def test_build_command_omits_sentinel_model_and_forwards_explicit_model() -> None:
    assert "--model" not in _make_runtime(model="default")._build_command("/tmp/out")
    command = _make_runtime(model="mega:full")._build_command("/tmp/out")
    assert command[command.index("--model") + 1] == "mega:full"


def test_default_permission_mode_is_coerced() -> None:
    assert _make_runtime(permission_mode="default").permission_mode == "acceptEdits"
    assert _make_runtime().permission_mode == "acceptEdits"


def test_unknown_permission_mode_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unsupported megacode permission mode"):
        _make_runtime(permission_mode="yolo")


# --- stream-json record conversion ------------------------------------------


def test_init_record_reports_session() -> None:
    messages = _convert(
        _make_runtime(),
        {"type": "init", "v": 1, "session_id": "S1", "model": "mega:full", "mode": "w"},
    )
    assert messages[0].type == "system"
    assert messages[0].data["session_id"] == "S1"


def test_assistant_and_reasoning_records() -> None:
    runtime = _make_runtime()
    [answer] = _convert(runtime, {"type": "assistant", "text": "done"})
    [thought] = _convert(runtime, {"type": "reasoning", "text": "hmm"})
    assert (answer.type, answer.content) == ("assistant", "done")
    assert thought.data == {"thinking": "hmm"}


def test_write_tool_pair_is_correlated_and_mapped() -> None:
    runtime = _make_runtime()
    [start] = _convert(
        runtime,
        {"type": "tool_use", "id": "T1", "tool": "write_file", "input": {"path": "a.py"}},
    )
    [done] = _convert(
        runtime,
        {
            "type": "tool_result",
            "id": "T1",
            "tool": "write_file",
            "status": "ok",
            "output": {"lines": 1, "path": "$PROJECT/a.py"},
        },
    )
    assert start.tool_name == done.tool_name == "Write"
    assert start.data["tool_input"]["file_path"] == "a.py"
    assert start.data["tool_call_id"] == done.data["tool_call_id"] == "T1"
    assert done.data["tool_result"]["is_error"] is False


def test_bash_non_zero_exit_is_a_failed_tool_result() -> None:
    runtime = _make_runtime()
    [done] = _convert(
        runtime,
        {
            "type": "tool_result",
            "id": "B1",
            "tool": "bash",
            "status": "ok",
            "output": {"code": 1, "outcome": "exited", "stderr": "nope\n", "stdout": ""},
        },
    )
    assert done.tool_name == "Bash"
    assert done.data["tool_result"]["is_error"] is True
    assert done.data["tool_result"]["meta"]["exit_status"] == 1
    assert done.content == "nope\n"


def test_failed_status_is_a_failed_tool_result() -> None:
    [done] = _convert(
        _make_runtime(),
        {"type": "tool_result", "id": "R1", "tool": "read_file", "status": "failed", "error": "x"},
    )
    assert done.tool_name == "Read"
    assert done.data["tool_result"]["is_error"] is True


def test_exec_tool_input_gets_a_command_string() -> None:
    [start] = _convert(
        _make_runtime(),
        {
            "type": "tool_use",
            "id": "E1",
            "tool": "exec",
            "input": {"program": "ls", "args": ["-a"]},
        },
    )
    assert start.tool_name == "Bash"
    assert start.data["tool_input"]["command"] == "ls -a"


def test_denied_approval_is_reported() -> None:
    runtime = _make_runtime()
    assert _convert(runtime, {"type": "approval", "id": "A", "decision": "allowed_once"}) == []
    [refused] = _convert(
        runtime, {"type": "approval", "id": "A", "tool": "bash", "decision": "refused"}
    )
    assert refused.type == "system"
    assert refused.data["is_error"] is True


def test_result_record_carries_answer_or_error() -> None:
    runtime = _make_runtime()
    [ok] = _convert(
        runtime, {"type": "result", "is_error": False, "result": "ANSWER", "outcome": "completed"}
    )
    [err] = _convert(
        runtime,
        {"type": "result", "is_error": True, "result": None, "error": "401", "outcome": "x"},
    )
    assert ok.content == "ANSWER"
    assert err.content == "401"
    assert err.data["is_error"] is True


def test_capabilities_declare_no_targeted_resume() -> None:
    capabilities = _make_runtime().capabilities
    assert capabilities.targeted_resume is False
    assert capabilities.structured_output is True


# --- Registration -----------------------------------------------------------


def test_factory_resolves_megacode_aliases() -> None:
    assert resolve_agent_runtime_backend("megacode") == "megacode"
    assert resolve_agent_runtime_backend("megacode_cli") == "megacode"


def test_factory_builds_megacode_runtime() -> None:
    from ouroboros.orchestrator import create_agent_runtime

    with (
        patch(
            "ouroboros.orchestrator.runtime_factory.get_agent_permission_mode",
            return_value=None,
        ),
        patch(
            "ouroboros.orchestrator.runtime_factory.get_llm_backend",
            return_value="claude",
        ),
        patch("ouroboros.config.get_megacode_cli_path", return_value=_test_cli_path()),
        patch(
            "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
            return_value=None,
        ),
    ):
        runtime = create_agent_runtime(backend="megacode")

    assert isinstance(runtime, MegacodeRuntime)
    assert runtime.permission_mode == "acceptEdits"


def test_cli_runtime_enums_accept_megacode() -> None:
    from ouroboros.cli.commands.auto import AgentRuntimeBackend as AutoBackend
    from ouroboros.cli.commands.mcp import LLMBackend as McpLLMBackend
    from ouroboros.cli.commands.run import AgentRuntimeBackend as RunBackend

    assert RunBackend("megacode") is RunBackend.MEGACODE
    assert AutoBackend("megacode") is AutoBackend.MEGACODE
    assert McpLLMBackend("megacode") is McpLLMBackend.MEGACODE


# --- End to end over a fake process -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constructor_model", "call_model", "expected_model"),
    [
        (None, "mega:full", "mega:full"),
        ("mega:fast", "mega:full", "mega:full"),
        ("mega:fast", None, "mega:fast"),
        ("mega:fast", "default", None),
        ("default", "mega:full", "mega:full"),
        ("default", None, None),
        (None, "default", None),
        (None, None, None),
    ],
)
async def test_execute_task_launches_with_effective_model(
    tmp_path: Path,
    constructor_model: str | None,
    call_model: str | None,
    expected_model: str | None,
) -> None:
    harness = tmp_path / "harness"
    harness.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "payload = {'argv': sys.argv[1:], 'prompt': sys.stdin.read()}\n"
        "print(json.dumps({'type': 'result', 'outcome': 'completed', "
        "'is_error': False, 'result': json.dumps(payload)}))\n"
    )
    harness.chmod(0o755)
    runtime = MegacodeRuntime(cli_path=harness, cwd=tmp_path, model=constructor_model)

    messages = [message async for message in runtime.execute_task("hello", model=call_model)]

    final = messages[-1]
    assert final.type == "result"
    assert final.data["subtype"] == "success", final.content
    payload = json.loads(final.content)
    expected_argv = ["-p", "--output-format", "stream-json", "--mode", MEGACODE_WORKER_MODE]
    if expected_model is not None:
        expected_argv.extend(["--model", expected_model])
    assert payload["argv"] == expected_argv
    assert "hello" in payload["prompt"]


@pytest.mark.asyncio
async def test_execute_task_feeds_prompt_on_stdin_and_returns_result_text() -> None:
    runtime = MegacodeRuntime(cli_path=_test_cli_path(), cwd="/tmp")
    processes: list[_FakeProcess] = []

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        del command, kwargs
        process = _FakeProcess(
            stdout_lines=[
                json.dumps({"type": "init", "v": 1, "session_id": "S1", "model": "m"}),
                json.dumps({"type": "user", "text": "ignored"}),
                json.dumps({"type": "assistant", "text": "Working on it"}),
                json.dumps(
                    {
                        "type": "result",
                        "v": 1,
                        "session_id": "S1",
                        "outcome": "completed",
                        "is_error": False,
                        "result": "All done",
                        "error": None,
                    }
                ),
            ]
        )
        processes.append(process)
        return process

    with patch(
        "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        result = await runtime.execute_task_to_result("build it")

    assert result.is_ok
    assert result.value.final_message == "All done"
    assert b"build it" in processes[0].stdin.written
    assert processes[0].stdin.closed is True


@pytest.mark.asyncio
async def test_execute_task_reports_failure_on_non_zero_exit() -> None:
    runtime = MegacodeRuntime(cli_path=_test_cli_path(), cwd="/tmp")

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return _FakeProcess(
            stdout_lines=[
                json.dumps(
                    {
                        "type": "result",
                        "outcome": "model_failed",
                        "is_error": True,
                        "result": None,
                        "error": "io: 401 Unauthorized",
                    }
                )
            ],
            returncode=1,
        )

    with patch(
        "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        messages = [message async for message in runtime.execute_task("build it")]

    final = messages[-1]
    assert final.type == "result"
    assert final.data["subtype"] == "error"
    assert "401 Unauthorized" in final.content
