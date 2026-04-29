"""Focused unit tests for the Gemini CLI runtime.

These tests cover the regressions surfaced during PR #312 review:

1. ``_convert_event`` surfaces the terminal ``result`` event as the final
   assistant message (the original PR dropped it).
2. ``_build_command`` includes ``--non-interactive`` so the CLI never blocks
   on a TTY prompt during headless execution.
3. ``--prompt`` carries the actual request (no empty-string regression).
4. The recursion guard refuses to launch beyond the configured depth.
5. ``runtime_factory.resolve_agent_runtime_backend`` accepts ``gemini`` and
   the rejection message lists every supported backend.
"""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.gemini_cli_runtime import (
    _MAX_OUROBOROS_DEPTH,
    GeminiCLIRuntime,
)
from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend

# ---------------------------------------------------------------------------
# _convert_event: terminal `result` event
# ---------------------------------------------------------------------------


def _make_runtime() -> GeminiCLIRuntime:
    return GeminiCLIRuntime(cli_path="/usr/bin/gemini")


def test_convert_event_surfaces_result_response_as_terminal_assistant_message() -> None:
    runtime = _make_runtime()

    # The normalizer maps `response` into `content`. We feed an already-normalized
    # event to keep this test focused on the runtime's terminal handling.
    event = {
        "type": "result",
        "content": "All tests passed.",
        "metadata": {"session_id": "sess-42"},
        "is_error": False,
        "raw": {"type": "result", "response": "All tests passed."},
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].content == "All tests passed."
    assert messages[0].data is not None
    assert messages[0].data.get("terminal") is True


def test_convert_event_emits_marker_when_result_has_no_response_text() -> None:
    runtime = _make_runtime()
    event = {
        "type": "result",
        "content": "",
        "metadata": {"session_id": "sess-7"},
        "is_error": False,
        "raw": {"type": "result"},
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].data is not None
    assert messages[0].data.get("terminal") is True


def test_convert_event_routes_normalizer_response_field_through_result() -> None:
    """End-to-end: normalizer + runtime surface `result.response` as final answer."""
    runtime = _make_runtime()
    raw_line = '{"type":"result","response":"final answer text"}'
    normalized = runtime._parse_json_event(raw_line)
    assert normalized is not None
    messages = runtime._convert_event(normalized, current_handle=None)
    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].content == "final answer text"


# ---------------------------------------------------------------------------
# _build_command: headless flags
# ---------------------------------------------------------------------------


def test_build_command_includes_non_interactive_flag() -> None:
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="hello")
    assert "--non-interactive" in cmd, f"--non-interactive missing from headless command: {cmd!r}"


def test_build_command_passes_prompt_through_prompt_flag() -> None:
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="fix the bug")
    # Locate `--prompt` and check the next arg is our payload.
    assert "--prompt" in cmd
    idx = cmd.index("--prompt")
    assert cmd[idx + 1] == "fix the bug"


def test_build_command_uses_stream_json_output_format() -> None:
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="x")
    assert "--output-format" in cmd
    idx = cmd.index("--output-format")
    assert cmd[idx + 1] == "stream-json"


def test_runtime_does_not_feed_prompt_via_stdin() -> None:
    runtime = _make_runtime()
    assert runtime._feeds_prompt_via_stdin() is False
    assert runtime._requires_process_stdin() is False


# ---------------------------------------------------------------------------
# Recursion guard
# ---------------------------------------------------------------------------


def test_recursion_guard_increments_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_OUROBOROS_DEPTH", "1")
    runtime = _make_runtime()
    env = runtime._build_child_env()
    assert env["_OUROBOROS_DEPTH"] == "2"


def test_recursion_guard_raises_at_max_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("_OUROBOROS_DEPTH", str(_MAX_OUROBOROS_DEPTH))
    runtime = _make_runtime()
    with pytest.raises(RuntimeError, match="Maximum Ouroboros nesting depth"):
        runtime._build_child_env()


def test_recursion_guard_strips_oroboros_runtime_envs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "gemini")
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "gemini")
    runtime = _make_runtime()
    env = runtime._build_child_env()
    assert "OUROBOROS_AGENT_RUNTIME" not in env
    assert "OUROBOROS_LLM_BACKEND" not in env


# ---------------------------------------------------------------------------
# runtime_factory: gemini registration & rejection message
# ---------------------------------------------------------------------------


def test_factory_resolves_gemini_alias() -> None:
    assert resolve_agent_runtime_backend("gemini") == "gemini"
    assert resolve_agent_runtime_backend("gemini_cli") == "gemini"
    assert resolve_agent_runtime_backend("GEMINI") == "gemini"


def test_factory_rejection_message_lists_supported_backends() -> None:
    with pytest.raises(ValueError) as exc_info:
        resolve_agent_runtime_backend("nonsense-backend")
    msg = str(exc_info.value)
    for name in ("claude", "codex", "opencode", "hermes", "gemini"):
        assert name in msg, f"rejection message missing {name!r}: {msg!r}"


# ---------------------------------------------------------------------------
# mcp.py: LLMBackend includes GEMINI
# ---------------------------------------------------------------------------


def test_mcp_llm_backend_enum_includes_gemini() -> None:
    from ouroboros.cli.commands.mcp import AgentRuntimeBackend, LLMBackend

    assert LLMBackend("gemini") is LLMBackend.GEMINI
    assert AgentRuntimeBackend("gemini") is AgentRuntimeBackend.GEMINI
