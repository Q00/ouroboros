"""Focused unit tests for the Grok Build CLI runtime.

Covers the ``grok`` headless contract that differs from the Codex base:

1. ``grok -p --output-format streaming-json`` with native ``--permission-mode``.
2. The verified ``thought`` / ``text`` / ``end`` event schema mapping.
3. Sentinel-model passthrough (``-m`` only for explicit ids).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import AgentMessage, ParamSupport
from ouroboros.orchestrator.grok_cli_runtime import GrokCliRuntime
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
        self.pid = 4322
        self.returncode: int | None = None
        self._returncode = returncode

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self._returncode


def _make_runtime() -> GrokCliRuntime:
    return GrokCliRuntime(cli_path="/usr/bin/grok")


# ---------------------------------------------------------------------------
# _build_command: grok headless flags
# ---------------------------------------------------------------------------


def test_build_command_uses_single_prompt_and_streaming_json() -> None:
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="fix the bug")
    assert "-p" in cmd
    assert cmd[cmd.index("-p") + 1] == "fix the bug"
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "streaming-json"


def test_build_command_forwards_permission_mode() -> None:
    runtime = GrokCliRuntime(cli_path="/usr/bin/grok", permission_mode="bypassPermissions")
    cmd = runtime._build_command("/tmp/unused", prompt="x")
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"


def test_build_command_defaults_to_accept_edits() -> None:
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="x")
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"


def test_build_command_omits_sentinel_model() -> None:
    runtime = GrokCliRuntime(cli_path="/usr/bin/grok", model="default")
    cmd = runtime._build_command("/tmp/unused", prompt="x")
    assert "-m" not in cmd


def test_build_command_forwards_explicit_model() -> None:
    runtime = GrokCliRuntime(cli_path="/usr/bin/grok", model="grok-build")
    cmd = runtime._build_command("/tmp/unused", prompt="x")
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "grok-build"


def test_build_command_accepts_and_ignores_reasoning_effort() -> None:
    """Effort routing is unwired in v1 — reasoning_effort is accepted (shared
    CodexCliRuntime contract) and ignored, never injected into the command."""
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="x", reasoning_effort="high")
    assert "high" not in cmd
    assert "--reasoning-effort" not in cmd
    injected = runtime._build_command("/tmp/unused", prompt="x", reasoning_effort="; rm -rf /")
    assert "; rm -rf /" not in injected


# ---------------------------------------------------------------------------
# Permission mode mapping
# ---------------------------------------------------------------------------


def test_default_permission_mode_is_coerced_with_audit_log() -> None:
    from structlog.testing import capture_logs

    with capture_logs() as cap_logs:
        runtime = GrokCliRuntime(cli_path="/usr/bin/grok", permission_mode="default")

    assert runtime.permission_mode == "acceptEdits"
    coerced = [e for e in cap_logs if e.get("event") == "grok_cli_runtime.permission_mode_coerced"]
    assert len(coerced) == 1
    assert coerced[0]["resolved"] == "acceptEdits"


def test_unknown_permission_mode_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unsupported Grok permission mode"):
        GrokCliRuntime(cli_path="/usr/bin/grok", permission_mode="yolo")


# ---------------------------------------------------------------------------
# Event conversion: thought / text / end
# ---------------------------------------------------------------------------


def test_text_event_surfaces_as_assistant_message() -> None:
    runtime = _make_runtime()
    event = runtime._parse_json_event('{"type":"text","data":"OK"}')
    assert event is not None
    messages = runtime._convert_event(event, current_handle=None)
    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].content == "OK"


def test_thought_event_surfaces_as_thinking() -> None:
    runtime = _make_runtime()
    event = runtime._parse_json_event('{"type":"thought","data":"reasoning"}')
    assert event is not None
    messages = runtime._convert_event(event, current_handle=None)
    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].data is not None
    assert messages[0].data.get("thinking") == "reasoning"


def test_end_event_is_terminal_marker() -> None:
    runtime = _make_runtime()
    raw = '{"type":"end","stopReason":"EndTurn","sessionId":"abc","requestId":"def"}'
    event = runtime._parse_json_event(raw)
    assert event is not None
    messages = runtime._convert_event(event, current_handle=None)
    assert len(messages) == 1
    assert messages[0].data is not None
    assert messages[0].data.get("terminal") is True


# ---------------------------------------------------------------------------
# Identity, capabilities, factory
# ---------------------------------------------------------------------------


def test_capabilities_declare_structured_output() -> None:
    runtime = _make_runtime()
    assert runtime.capabilities.skill_dispatch is True
    assert runtime.capabilities.targeted_resume is False
    # Grok emits structured streaming-json events.
    assert runtime.capabilities.structured_output is True
    assert runtime.capabilities.system_prompt_support is ParamSupport.TRANSLATED
    assert runtime.capabilities.tool_restriction_support is ParamSupport.TRANSLATED


def test_runtime_identity() -> None:
    runtime = _make_runtime()
    assert runtime._runtime_backend == "grok"
    assert runtime.runtime_backend == "grok_cli"


def test_factory_resolves_grok_aliases() -> None:
    assert resolve_agent_runtime_backend("grok") == "grok"
    assert resolve_agent_runtime_backend("grok_cli") == "grok"
    assert resolve_agent_runtime_backend("grok_build") == "grok"
    assert resolve_agent_runtime_backend("GROK") == "grok"


def test_factory_builds_grok_runtime() -> None:
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
        patch("ouroboros.config.get_grok_cli_path", return_value="/usr/bin/grok"),
        patch(
            "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
            return_value=None,
        ),
    ):
        runtime = create_agent_runtime(backend="grok")

    assert isinstance(runtime, GrokCliRuntime)
    assert runtime.permission_mode == "acceptEdits"


def test_cli_runtime_enums_accept_grok() -> None:
    from ouroboros.cli.commands.init import AgentRuntimeBackend as InitBackend
    from ouroboros.cli.commands.mcp import AgentRuntimeBackend as McpBackend
    from ouroboros.cli.commands.run import AgentRuntimeBackend as RunBackend

    assert InitBackend("grok") is InitBackend.GROK
    assert McpBackend("grok") is McpBackend.GROK
    assert RunBackend("grok") is RunBackend.GROK


def test_auto_cli_runtime_enum_accepts_grok() -> None:
    """`ooo auto --runtime grok` must be selectable (front-door parity)."""
    from ouroboros.cli.commands.auto import AgentRuntimeBackend as AutoBackend

    assert AutoBackend("grok") is AutoBackend.GROK


# ---------------------------------------------------------------------------
# Final-message accumulation — grok streams per-token `text` deltas
# ---------------------------------------------------------------------------


def test_text_events_are_tagged_as_deltas() -> None:
    """`text` events carry the grok_text_delta tag so they accumulate."""
    runtime = _make_runtime()
    event = runtime._parse_json_event('{"type":"text","data":"OK"}')
    assert event is not None
    messages = runtime._convert_event(event, current_handle=None)
    assert messages[0].data.get("grok_text_delta") is True


def test_update_last_content_accumulates_text_deltas_only() -> None:
    """Text deltas concatenate; thought/terminal messages don't pollute the answer."""
    runtime = _make_runtime()
    content = ""
    content = runtime._update_last_content(
        content, AgentMessage(type="assistant", content="Hello", data={"grok_text_delta": True})
    )
    content = runtime._update_last_content(
        content, AgentMessage(type="assistant", content=" world", data={"grok_text_delta": True})
    )
    assert content == "Hello world"
    # A thought (reasoning) message must NOT overwrite or append to the answer.
    content = runtime._update_last_content(
        content, AgentMessage(type="assistant", content="(reasoning)", data={"thinking": "x"})
    )
    assert content == "Hello world"
    # The terminal `end` marker (no delta tag) leaves the answer untouched.
    content = runtime._update_last_content(
        content, AgentMessage(type="assistant", content="", data={"terminal": True})
    )
    assert content == "Hello world"


@pytest.mark.asyncio
async def test_execute_task_to_result_accumulates_streamed_text() -> None:
    """End-to-end: streamed `text` token deltas reach final_message intact.

    Reproduces the reviewer's finding: without accumulation, only the last
    token (`" world"`) would survive into TaskResult.final_message.
    """
    runtime = GrokCliRuntime(cli_path="/usr/bin/grok", cwd="/tmp")

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        del command, kwargs
        return _FakeProcess(
            stdout_lines=[
                json.dumps({"type": "thought", "data": "thinking"}),
                json.dumps({"type": "text", "data": "Hello"}),
                json.dumps({"type": "text", "data": " world"}),
                json.dumps(
                    {"type": "end", "stopReason": "EndTurn", "sessionId": "s1", "requestId": "r1"}
                ),
            ]
        )

    with patch(
        "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
        side_effect=fake_exec,
    ):
        result = await runtime.execute_task_to_result("say hello world")

    assert result.is_ok
    assert result.value.final_message == "Hello world"
