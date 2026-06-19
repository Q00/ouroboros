"""Focused unit tests for the Antigravity CLI runtime.

Antigravity is Google's successor to the Gemini CLI (the ``agy`` binary). The
runtime extends :class:`GeminiCLIRuntime` but speaks ``agy``'s headless
contract, which differs in three ways covered here:

1. ``agy -p`` (no ``--output-format``) — plain-text stdout, so
   ``capabilities.structured_output`` is ``False``.
2. ``--dangerously-skip-permissions`` is the only auto-approve mode; both
   ``acceptEdits`` and ``bypassPermissions`` map to it, and interactive
   ``default`` is coerced to a safe non-blocking mode with an audit log.
3. The sentinel model id is never forwarded on the command line.
"""

from __future__ import annotations

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


def test_build_command_has_no_output_format_flag() -> None:
    """agy emits plain text — there is no --output-format flag to pass."""
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="x")
    assert "--output-format" not in cmd


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


def test_capabilities_mark_plain_text_and_no_resume() -> None:
    runtime = _make_runtime()
    assert runtime.capabilities.skill_dispatch is True
    assert runtime.capabilities.targeted_resume is False
    # Plain-text stdout (no stream-json) → structured output unavailable.
    assert runtime.capabilities.structured_output is False
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
