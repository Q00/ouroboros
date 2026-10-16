"""Focused unit tests for the Antigravity CLI runtime.

Antigravity is Google's successor to the Gemini CLI (the ``agy`` binary). The
runtime extends :class:`GeminiCLIRuntime` but speaks ``agy``'s headless
contract, which differs in three ways covered here:

1. ``agy -p --output-format stream-json`` preserves structured tool evidence.
2. ``--dangerously-skip-permissions`` is the only auto-approve mode; both
   ``acceptEdits`` and ``bypassPermissions`` map to it, and interactive
   ``default`` is coerced to a safe non-blocking mode with an audit log.
3. The sentinel model id is never forwarded on the command line.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import AgentMessage, ParamSupport
from ouroboros.orchestrator.antigravity_cli_runtime import AntigravityCLIRuntime
from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend

# --- Minimal fake subprocess harness (mirrors test_codex_cli_runtime) -------


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
    def write(self, data: bytes) -> None:
        del data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], returncode: int = 0) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream([])
        self.pid = 4321
        self.returncode: int | None = None
        self._returncode = returncode

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self._returncode


def _make_runtime() -> AntigravityCLIRuntime:
    return AntigravityCLIRuntime(cli_path="/usr/bin/agy")


# ---------------------------------------------------------------------------
# _build_command: agy headless flags
# ---------------------------------------------------------------------------


def test_build_command_uses_print_flag_with_prompt() -> None:
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="fix the bug")
    assert "-p" in cmd
    idx = cmd.index("-p")
    assert cmd[idx + 1] == "fix the bug"


def test_build_command_always_skips_permissions() -> None:
    """Headless agy must auto-approve or it would block on a tool prompt."""
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="x")
    assert "--dangerously-skip-permissions" in cmd


def test_build_command_requests_structured_output() -> None:
    """Native stream-json is requested so tool activity is observable."""
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="x")
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"


def test_build_command_omits_sentinel_model() -> None:
    """The 'default' sentinel is CLI-owned and must never be forwarded."""
    runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy", model="default")
    cmd = runtime._build_command("/tmp/unused", prompt="x")
    assert "--model" not in cmd


def test_build_command_forwards_explicit_model() -> None:
    runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy", model="gemini-3.1-pro")
    cmd = runtime._build_command("/tmp/unused", prompt="x")
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "gemini-3.1-pro"


def test_build_command_accepts_and_ignores_reasoning_effort() -> None:
    """agy exposes no effort flag — reasoning_effort is accepted (shared
    CodexCliRuntime contract) and ignored, never injected into the command."""
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="x", reasoning_effort="high")
    assert "high" not in cmd
    injected = runtime._build_command("/tmp/unused", prompt="x", reasoning_effort="; rm -rf /")
    assert "; rm -rf /" not in injected


# ---------------------------------------------------------------------------
# Permission mode mapping
# ---------------------------------------------------------------------------


def test_accept_edits_and_bypass_both_skip_permissions() -> None:
    for mode in ("acceptEdits", "bypassPermissions"):
        runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy", permission_mode=mode)
        assert runtime.permission_mode == mode
        cmd = runtime._build_command("/tmp/unused", prompt="x")
        assert "--dangerously-skip-permissions" in cmd


def test_omitted_permission_mode_resolves_to_accept_edits() -> None:
    runtime = _make_runtime()
    assert runtime.permission_mode == "acceptEdits"


def test_default_permission_mode_is_coerced_with_audit_log() -> None:
    """Interactive 'default' would deadlock headless agy → coerce + log."""
    from structlog.testing import capture_logs

    with capture_logs() as cap_logs:
        runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy", permission_mode="default")

    assert runtime.permission_mode == "acceptEdits"
    coerced = [
        e for e in cap_logs if e.get("event") == "antigravity_cli_runtime.permission_mode_coerced"
    ]
    assert len(coerced) == 1
    assert coerced[0]["requested"] == "default"
    assert coerced[0]["resolved"] == "acceptEdits"


def test_unknown_permission_mode_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unsupported Antigravity permission mode"):
        AntigravityCLIRuntime(cli_path="/usr/bin/agy", permission_mode="acceptedits")


# ---------------------------------------------------------------------------
# Identity & capabilities
# ---------------------------------------------------------------------------


def test_runtime_identity_is_antigravity() -> None:
    runtime = _make_runtime()
    assert isinstance(runtime, GeminiCLIRuntime)  # successor relationship
    assert runtime._runtime_backend == "antigravity"
    assert runtime.runtime_backend == "antigravity_cli"  # the handle name


def test_capabilities_mark_structured_output_and_targeted_resume() -> None:
    runtime = _make_runtime()
    assert runtime.capabilities.skill_dispatch is True
    assert runtime.capabilities.targeted_resume is True
    assert runtime.capabilities.structured_output is True
    assert runtime.capabilities.system_prompt_support is ParamSupport.TRANSLATED
    assert runtime.capabilities.tool_restriction_support is ParamSupport.TRANSLATED


def test_plain_text_stdout_surfaces_as_assistant_message() -> None:
    """A plain (non-JSON) agy stdout line is surfaced as an assistant message."""
    runtime = _make_runtime()
    event = runtime._parse_json_event("All done.")
    assert event is not None
    messages = runtime._convert_event(event, current_handle=None)
    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].content == "All done."


# ---------------------------------------------------------------------------
# runtime_factory registration
# ---------------------------------------------------------------------------


def test_factory_resolves_antigravity_aliases() -> None:
    assert resolve_agent_runtime_backend("antigravity") == "antigravity"
    assert resolve_agent_runtime_backend("agy") == "antigravity"
    assert resolve_agent_runtime_backend("ANTIGRAVITY") == "antigravity"


def test_factory_builds_antigravity_runtime() -> None:
    from unittest.mock import patch

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
        patch(
            "ouroboros.config.get_antigravity_cli_path",
            return_value="/usr/bin/agy",
        ),
        patch(
            "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
            return_value=None,
        ),
    ):
        runtime = create_agent_runtime(backend="antigravity")

    assert isinstance(runtime, AntigravityCLIRuntime)
    assert runtime.permission_mode == "acceptEdits"


def test_cli_runtime_enums_accept_antigravity() -> None:
    """`--runtime antigravity` is selectable on init, mcp, and run."""
    from ouroboros.cli.commands.init import AgentRuntimeBackend as InitBackend
    from ouroboros.cli.commands.mcp import AgentRuntimeBackend as McpBackend
    from ouroboros.cli.commands.run import AgentRuntimeBackend as RunBackend

    assert InitBackend("antigravity") is InitBackend.ANTIGRAVITY
    assert McpBackend("antigravity") is McpBackend.ANTIGRAVITY
    assert RunBackend("antigravity") is RunBackend.ANTIGRAVITY


def test_auto_cli_runtime_enum_accepts_antigravity() -> None:
    """`ooo auto --runtime antigravity` must be selectable (front-door parity)."""
    from ouroboros.cli.commands.auto import AgentRuntimeBackend as AutoBackend

    assert AutoBackend("antigravity") is AutoBackend.ANTIGRAVITY


# ---------------------------------------------------------------------------
# Final-message accumulation — agy prints multi-line plain text
# ---------------------------------------------------------------------------


def test_update_last_content_accumulates_plain_text_lines() -> None:
    """Multi-line agy output must not be truncated to the last line."""
    runtime = _make_runtime()
    content = ""
    content = runtime._update_last_content(
        content, AgentMessage(type="assistant", content="Line one")
    )
    content = runtime._update_last_content(
        content, AgentMessage(type="assistant", content="Line two")
    )
    assert content == "Line one\nLine two"
    # Empty content (e.g. a marker) leaves the accumulation untouched.
    content = runtime._update_last_content(content, AgentMessage(type="assistant", content=""))
    assert content == "Line one\nLine two"


@pytest.mark.asyncio
async def test_execute_task_to_result_preserves_multiline_response() -> None:
    """End-to-end: a multi-line agy response reaches final_message intact."""
    runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy", cwd="/tmp")

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return _FakeProcess(stdout_lines=["First line", "Second line", "Third line"])

    with patch(
        "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        result = await runtime.execute_task_to_result("write three lines")

    assert result.is_ok
    assert result.value.final_message == "First line\nSecond line\nThird line"


@pytest.mark.asyncio
@pytest.mark.parametrize("generated_name", ["a.py", "directory with spaces/a file.py"])
async def test_structured_execution_records_tools_and_preserves_final_answer(
    tmp_path, generated_name
) -> None:
    import json

    from ouroboros.evaluation.artifact_collector import ArtifactCollector
    from ouroboros.orchestrator.parallel_executor import render_parallel_verification_report
    from ouroboros.orchestrator.parallel_executor_models import (
        ACExecutionResult,
        ParallelExecutionResult,
    )
    from ouroboros.orchestrator.runtime_message_projection import project_runtime_message

    generated = tmp_path / generated_name
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text("answer = 42\n")
    runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy", cwd=str(tmp_path))
    events = [
        {"event": "init", "conversation_id": "native-session", "init": {"cwd": "/tmp"}},
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "ACTIVE",
                "tool_name": "write_to_file",
                "tool_info": {"parameters": {"TargetFile": str(generated)}},
            },
        },
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "state": "DONE",
                "tool_name": "write_to_file",
                "tool_info": {"output": "written"},
            },
        },
        {
            "event": "step_update",
            "step_update": {"step_type": "agent_response", "text_delta": "Done"},
        },
        {
            "event": "result",
            "result": {
                "conversation_id": "native-session",
                "status": "SUCCESS",
                "response": "Done\n",
            },
        },
    ]

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        assert command[command.index("--output-format") + 1] == "stream-json"
        return _FakeProcess([json.dumps(e) for e in events])

    with patch(
        "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        result = await runtime.execute_task_to_result("write a.py")
    assert result.is_ok
    transcript = result.value.messages
    calls = [m for m in transcript if m.type == "assistant" and m.tool_name]
    assert len(calls) == 1
    assert calls[0].tool_name == "Write"
    assert calls[0].data["tool_input"]["file_path"] == str(generated)
    assert any(m.type == "tool_result" and m.content == "written" for m in transcript)
    assert result.value.final_message == "Done\n"
    assert result.value.session_id == "native-session"

    # This is the projection the dispatcher counts, not just a parser assertion.
    projected = [project_runtime_message(m) for m in transcript]
    assert sum(m.is_tool_call for m in projected) == 1
    assert sum(m.is_tool_result for m in projected) == 1
    ac = ACExecutionResult(
        ac_index=0,
        ac_content="Write a.py",
        success=True,
        messages=tuple(transcript),
        final_message=result.value.final_message,
    )
    report = render_parallel_verification_report(
        ParallelExecutionResult(results=(ac,), success_count=1, failure_count=0), 1
    )
    assert f"Write: {generated}" in report
    # Disable fallback scanning so it cannot conceal a missing event path hint.
    with patch.object(ArtifactCollector, "_scan_directory", side_effect=AssertionError("no scan")):
        bundle = ArtifactCollector().collect(report, str(tmp_path))
    assert [(f.file_path, f.content) for f in bundle.files] == [(str(generated), "answer = 42\n")]
    from ouroboros.evaluation.artifact_collector import eval_artifact_path, with_artifact_file
    from ouroboros.evaluation.models import EvaluationContext
    from ouroboros.evaluation.semantic import build_evaluation_prompt

    context = EvaluationContext(
        execution_id="native-evidence",
        seed_id="native-evidence",
        current_ac="Write the source",
        artifact=report,
        artifact_type="code",
        artifact_bundle=with_artifact_file(bundle, eval_artifact_path(tmp_path), report),
    )
    prompt = build_evaluation_prompt(context)
    assert "answer = 42" in prompt
    assert f"Write: {generated}" in prompt


def test_plain_text_fallback_reports_unknown_tool_usage() -> None:
    runtime = _make_runtime()
    with patch("ouroboros.orchestrator.antigravity_cli_runtime.log.warning") as warning:
        event = runtime._parse_json_event("Wrapped CLI response")
    assert event["content"] == "Wrapped CLI response"
    warning.assert_called_once()
    assert "unknown" in warning.call_args.kwargs["limitation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("from_handle", [False, True])
async def test_native_resume_forwards_requested_conversation(from_handle: bool) -> None:
    import json

    runtime = _make_runtime()
    handle = runtime._build_runtime_handle("requested-native-session") if from_handle else None
    calls = []

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        calls.append(command)
        assert command[command.index("--conversation") + 1] == "requested-native-session"
        return _FakeProcess(
            [
                json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "conversation_id": "requested-native-session",
                            "status": "SUCCESS",
                            "response": "continued",
                        },
                    }
                )
            ]
        )

    with patch(
        "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        result = await runtime.execute_task_to_result(
            "continue",
            resume_handle=handle,
            resume_session_id=None if from_handle else "requested-native-session",
        )
    assert result.is_ok
    assert result.value.session_id == "requested-native-session"
    assert result.value.final_message == "continued"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unsupported_resume_cli_fails_without_fresh_session_retry() -> None:
    runtime = _make_runtime()
    calls = []

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        calls.append(command)
        assert "--conversation" in command
        process = _FakeProcess([], returncode=2)
        process.stderr = _FakeStream(["unknown flag: --conversation"])
        return process

    with patch(
        "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        result = await runtime.execute_task_to_result(
            "continue", resume_session_id="native-session"
        )
    assert result.is_err
    assert "conversation" in str(result.error)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("init", {}),
        ("step_update", {"step_type": "agent_response", "text_delta": "Hello"}),
        ("step_update", {"step_type": "tool", "state": "ACTIVE", "tool_name": "run_command"}),
        ("step_update", {"step_type": "tool", "state": "DONE", "tool_name": "run_command"}),
        ("result", {"status": "SUCCESS", "response": "done"}),
    ],
)
@pytest.mark.parametrize("location", ["outer", "nested"])
def test_native_identity_uses_only_supported_conversation_id(kind, payload, location):
    runtime = _make_runtime()
    payload = {**payload, "session_id": "payload-spoof", "thread_id": "payload-thread-spoof"}
    raw = {"event": kind, kind: payload, "session_id": "outer-spoof", "thread_id": "outer-thread"}
    (raw if location == "outer" else payload)["conversation_id"] = " native-owned "
    parsed = runtime._parse_json_event(json.dumps(raw))

    assert parsed["raw"] == raw
    assert parsed["metadata"]["session_id"] == "native-owned"
    assert runtime._extract_event_session_id(parsed) == "native-owned"


@pytest.mark.parametrize("identity", [None, "", " ", [], {}, 0, False])
@pytest.mark.parametrize("location", ["outer", "nested"])
def test_invalid_native_identity_does_not_fall_back_to_spoofed_fields(identity, location):
    runtime = _make_runtime()
    payload = {"session_id": "payload-spoof", "thread_id": "payload-thread"}
    raw = {
        "event": "init",
        "init": payload,
        "session_id": "outer-spoof",
        "thread_id": "outer-thread",
    }
    (raw if location == "outer" else payload)["conversation_id"] = identity
    if location == "outer":
        payload["conversation_id"] = "nested-must-not-override-invalid-outer"
    parsed = runtime._parse_json_event(json.dumps(raw))

    assert parsed["raw"] == raw
    assert "session_id" not in parsed["metadata"]
    assert runtime._extract_event_session_id(parsed) is None


@pytest.mark.parametrize(
    "raw",
    [
        {"event": "unrecognized"},
        {"event": []},
        {"event": "message", "type": "tool_result"},
        {"event": "init", "init": None},
        {"event": "step_update", "step_update": {"step_type": []}},
        {
            "event": "step_update",
            "step_update": {"step_type": "tool", "state": "DONE", "tool_name": []},
        },
        {"event": "result", "result": {"status": {"state": "ERROR"}}},
    ],
)
def test_unknown_or_malformed_native_identity_cannot_bind_raw_fields(raw):
    runtime = _make_runtime()
    raw = {
        **raw,
        "conversation_id": "foreign-conversation",
        "session_id": "foreign-session",
        "thread_id": "foreign-thread",
        "metadata": {"session_id": "foreign-metadata"},
    }
    parsed = runtime._parse_json_event(json.dumps(raw))

    assert parsed["raw"] == raw
    assert parsed["type"] == "unknown"
    assert runtime._extract_event_session_id(parsed) is None
    parsed["metadata"]["session_id"] = "normalized-unknown-spoof"
    assert runtime._extract_event_session_id(parsed) is None


@pytest.mark.parametrize("key", ["event", "type", "kind", "category"])
@pytest.mark.parametrize(
    "kind",
    [
        "init",
        "message",
        "text",
        "thinking",
        "tool_use",
        "tool_result",
        "error",
        "result",
        "done",
        "fatal",
        "exception",
        "abort",
    ],
)
@pytest.mark.parametrize("declared_type", [None, [], {}, 0, False, ""])
def test_legacy_identity_retains_parent_gemini_extraction(key, kind, declared_type):
    runtime = _make_runtime()
    parent = GeminiCLIRuntime(cli_path="/usr/bin/gemini")
    raw = {key: kind, "session_id": " legacy-session ", "content": "Hello"}
    if key != "type":
        raw["type"] = declared_type
    line = json.dumps(raw)
    actual = runtime._parse_json_event(line)
    expected = parent._parse_json_event(line)

    assert actual == expected
    if actual["type"] == "unknown":
        # Parent normalization remains exact; unknown no longer grants identity.
        assert runtime._extract_event_session_id(actual) is None
    else:
        assert runtime._extract_event_session_id(actual) == parent._extract_event_session_id(
            expected
        )
        assert runtime._extract_event_session_id(actual) == "legacy-session"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_current_handle", [False, True])
async def test_stream_unknown_foreign_identity_cannot_replace_current_or_bind_first(
    has_current_handle,
):
    runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy", cwd="/tmp")
    handle = runtime._build_runtime_handle("native-owned") if has_current_handle else None
    before = handle.to_dict() if handle else None
    lines = [
        {"event": "unrecognized", "session_id": "foreign", "thread_id": "foreign-thread"},
        {"event": "init", "init": {}, "conversation_id": "native-owned"},
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "done",
                "conversation_id": "native-owned",
                "session_id": "payload-spoof",
            },
        },
    ]

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        return _FakeProcess(stdout_lines=[json.dumps(raw) for raw in lines])

    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
        patch.object(
            runtime, "_build_runtime_handle", wraps=runtime._build_runtime_handle
        ) as build,
    ):
        result = await runtime.execute_task_to_result(
            "synthetic identity probe", resume_handle=handle
        )

    bound_ids = [call.args[0] for call in build.call_args_list if call.args and call.args[0]]
    assert bound_ids == ([] if has_current_handle else ["native-owned"])

    assert result.is_ok
    assert result.value.session_id == "native-owned"
    assert all(
        message.resume_handle.native_session_id == "native-owned"
        for message in result.value.messages
        if message.resume_handle is not None
    )
    if handle:
        assert handle.to_dict() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["init", "step_update", "result"])
@pytest.mark.parametrize("has_current_handle", [False, True])
async def test_conflicting_native_stream_has_no_tool_or_transient_identity_effect(
    kind, has_current_handle
):
    runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy", cwd="/tmp")
    handle = runtime._build_runtime_handle("native-owned") if has_current_handle else None
    payloads = {
        "init": {},
        "step_update": {
            "step_type": "tool",
            "state": "ACTIVE",
            "tool_name": "run_command",
            "tool_input": {"command": "echo"},
        },
        "result": {"status": "SUCCESS", "response": "foreign-finished"},
    }
    events = [
        {"event": kind, "type": "tool_result", kind: payloads[kind], "conversation_id": "foreign"},
        {"event": "init", "init": {}, "conversation_id": "native-owned"},
        {
            "event": "result",
            "result": {"status": "SUCCESS", "response": "done", "conversation_id": "native-owned"},
        },
    ]
    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            return_value=_FakeProcess([json.dumps(raw) for raw in events]),
        ),
        patch.object(
            runtime, "_build_runtime_handle", wraps=runtime._build_runtime_handle
        ) as build,
    ):
        result = await runtime.execute_task_to_result(
            "synthetic conflict probe", resume_handle=handle
        )

    assert result.is_ok
    assert result.value.final_message == "done"
    assert not any(
        message.tool_name is not None
        or message.type == "tool_result"
        or message.content == "foreign-finished"
        for message in result.value.messages
    )
    bound_ids = [call.args[0] for call in build.call_args_list if call.args and call.args[0]]
    assert bound_ids == ([] if has_current_handle else ["native-owned"])


_UNKNOWN_IDENTITY_SHAPES = (
    [{}]
    + [
        {key: value}
        for key in ("event", "type", "kind", "category")
        for value in ("unrecognized", "unknown", "", " ", None, {}, [], 0, False)
    ]
    + [
        {"type": "unrecognized", "kind": "init"},
        {"type": "", "kind": "init"},
        {"kind": "unrecognized", "category": "init"},
        {"kind": "", "category": "init"},
        {"type": "unrecognized", "event": "init", "init": {}},
        {"type": "", "event": "init"},
        {"event": "init", "init": None},
        {"event": "step_update", "step_update": {}},
        {"event": "result", "result": {}},
    ]
)
_IDENTITY_FIELDS = (
    "session_id",
    "thread_id",
    "native_session_id",
    "run_id",
    "conversation_id",
    "session",
)


@pytest.mark.parametrize("shape", _UNKNOWN_IDENTITY_SHAPES)
@pytest.mark.parametrize("identity_key", _IDENTITY_FIELDS)
@pytest.mark.parametrize("location", ["raw", "raw_metadata", "normalized", "normalized_metadata"])
def test_unrecognized_normalized_events_never_gain_identity(shape, identity_key, location):
    runtime = _make_runtime()
    raw = dict(shape)
    identity = {"id": "foreign"} if identity_key == "session" else "foreign"
    if location == "raw":
        raw[identity_key] = identity
    elif location == "raw_metadata":
        raw["metadata"] = {identity_key: identity}
    event = runtime._parse_json_event(json.dumps(raw))
    assert event["type"] in {"unknown", "unrecognized"}
    assert event["raw"] == raw
    if location == "normalized":
        event[identity_key] = identity
    elif location == "normalized_metadata":
        event["metadata"][identity_key] = identity
    assert runtime._extract_event_session_id(event) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("has_current_handle", [False, True])
async def test_flat_unknown_stream_never_binds_or_replaces_identity(has_current_handle):
    runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy", cwd="/tmp")
    handle = runtime._build_runtime_handle("native-owned") if has_current_handle else None
    before = handle.to_dict() if handle else None
    identities = {
        key: {"id": "foreign"} if key == "session" else "foreign" for key in _IDENTITY_FIELDS
    }
    events = [
        raw
        for shape in _UNKNOWN_IDENTITY_SHAPES
        for raw in ({**shape, **identities}, {**shape, "metadata": identities})
    ]
    events.extend(
        [
            {"event": "init", "conversation_id": "native-owned", "init": {}},
            {
                "event": "result",
                "result": {
                    "conversation_id": "native-owned",
                    "status": "SUCCESS",
                    "response": "done",
                },
            },
        ]
    )
    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            return_value=_FakeProcess([json.dumps(raw) for raw in events]),
        ),
        patch.object(
            runtime, "_build_runtime_handle", wraps=runtime._build_runtime_handle
        ) as build,
    ):
        result = await runtime.execute_task_to_result(
            "unknown identity probe", resume_handle=handle
        )
    bound_ids = [call.args[0] for call in build.call_args_list if call.args and call.args[0]]
    assert bound_ids == ([] if has_current_handle else ["native-owned"])
    assert result.is_ok
    assert result.value.session_id == "native-owned"
    assert result.value.final_message == "done"
    assert all(
        message.resume_handle is None or message.resume_handle.native_session_id == "native-owned"
        for message in result.value.messages
    )
    if handle:
        assert handle.to_dict() == before
