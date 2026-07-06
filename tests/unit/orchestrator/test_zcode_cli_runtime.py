"""Focused unit tests for the Zcode CLI runtime.

These tests cover:
1. ``_convert_event`` surfaces assistant/tool/result events correctly
2. ``_extract_event_session_id`` captures session IDs for --resume
3. ``_build_command`` maps permission modes correctly
4. Runtime capabilities are declared correctly
"""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.adapter import ParamSupport
from ouroboros.orchestrator.zcode_cli_runtime import (
    _MAX_OUROBOROS_DEPTH,
    ZcodeCLIRuntime,
)
from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend

# ---------------------------------------------------------------------------
# _convert_event: event type handling
# ---------------------------------------------------------------------------


def _make_runtime() -> ZcodeCLIRuntime:
    return ZcodeCLIRuntime(cli_path="/usr/bin/zcode")


def test_convert_event_handles_text_message() -> None:
    """Text/message events produce assistant messages."""
    runtime = _make_runtime()

    event = {
        "type": "text",
        "content": "Hello, world!",
        "metadata": {},
        "is_error": False,
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].content == "Hello, world!"


def test_convert_event_handles_message_event() -> None:
    """Message events produce assistant messages."""
    runtime = _make_runtime()

    event = {
        "type": "message",
        "content": "Processing your request...",
        "metadata": {},
        "is_error": False,
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].content == "Processing your request..."


def test_convert_event_handles_thinking() -> None:
    """Thinking events produce assistant messages with thinking data."""
    runtime = _make_runtime()

    event = {
        "type": "thinking",
        "content": "Analyzing the problem...",
        "metadata": {},
        "is_error": False,
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].content == "Analyzing the problem..."
    assert messages[0].data is not None
    assert messages[0].data.get("thinking") == "Analyzing the problem..."


def test_convert_event_handles_tool_use() -> None:
    """Tool use events produce assistant messages with tool_name and data."""
    runtime = _make_runtime()

    event = {
        "type": "tool_use",
        "content": "Reading file",
        "metadata": {
            "name": "Read",
            "input": {"file_path": "/path/to/file.txt"},
        },
        "is_error": False,
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].tool_name == "Read"
    assert messages[0].data is not None
    assert messages[0].data.get("tool_input") == {"file_path": "/path/to/file.txt"}


def test_convert_event_handles_tool_result() -> None:
    """Tool result events produce tool messages."""
    runtime = _make_runtime()

    event = {
        "type": "tool_result",
        "content": "File content read successfully",
        "metadata": {
            "name": "Read",
        },
        "is_error": False,
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 1
    assert messages[0].type == "tool"
    assert messages[0].tool_name == "Read"
    assert messages[0].content == "File content read successfully"


def test_convert_event_handles_tool_result_error() -> None:
    """Tool result errors carry is_error in data."""
    runtime = _make_runtime()

    event = {
        "type": "tool_result",
        "content": "File not found",
        "metadata": {
            "name": "Read",
        },
        "is_error": True,
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 1
    assert messages[0].type == "tool"
    assert messages[0].data is not None
    assert messages[0].data.get("is_error") is True


def test_convert_event_handles_error() -> None:
    """Error events produce system messages."""
    runtime = _make_runtime()

    event = {
        "type": "error",
        "content": "API rate limit exceeded",
        "metadata": {},
        "is_error": False,
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 1
    assert messages[0].type == "system"
    assert "Zcode Error:" in messages[0].content
    assert messages[0].data is not None
    assert messages[0].data.get("is_error") is True


def test_convert_event_handles_result_with_content() -> None:
    """Result events with content produce terminal assistant messages."""
    runtime = _make_runtime()

    event = {
        "type": "result",
        "content": "Task completed successfully",
        "metadata": {"session_id": "sess-123"},
        "is_error": False,
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].content == "Task completed successfully"
    assert messages[0].data is not None
    assert messages[0].data.get("terminal") is True


def test_convert_event_handles_result_without_content() -> None:
    """Result events without content emit a marker message."""
    runtime = _make_runtime()

    event = {
        "type": "result",
        "content": "",
        "metadata": {"session_id": "sess-empty"},
        "is_error": False,
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].content == ""
    assert messages[0].data is not None
    assert messages[0].data.get("terminal") is True


def test_convert_event_ignores_unknown_events() -> None:
    """Unknown event types return empty list."""
    runtime = _make_runtime()

    event = {
        "type": "unknown_event_type",
        "content": "Should be ignored",
        "metadata": {},
        "is_error": False,
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 0


def test_convert_event_returns_empty_for_text_without_content() -> None:
    """Text events without content produce no messages."""
    runtime = _make_runtime()

    event = {
        "type": "text",
        "content": "",
        "metadata": {},
        "is_error": False,
    }

    messages = runtime._convert_event(event, current_handle=None)

    assert len(messages) == 0


# ---------------------------------------------------------------------------
# _extract_event_session_id: session ID extraction
# ---------------------------------------------------------------------------


def test_extract_session_id_from_metadata() -> None:
    """Session IDs are extracted from the metadata field."""
    runtime = _make_runtime()

    event = {
        "type": "init",
        "metadata": {
            "session_id": "sess-from-metadata",
        },
    }

    session_id = runtime._extract_event_session_id(event)

    assert session_id == "sess-from-metadata"


def test_extract_session_id_from_raw() -> None:
    """Session IDs are extracted from the raw field."""
    runtime = _make_runtime()

    event = {
        "type": "init",
        "raw": {
            "session_id": "sess-from-raw",
        },
    }

    session_id = runtime._extract_event_session_id(event)

    assert session_id == "sess-from-raw"


def test_extract_session_id_from_top_level() -> None:
    """Session IDs are extracted from top-level keys."""
    runtime = _make_runtime()

    event = {
        "type": "init",
        "session_id": "sess-toplevel",
    }

    session_id = runtime._extract_event_session_id(event)

    assert session_id == "sess-toplevel"


def test_extract_session_id_returns_none_when_missing() -> None:
    """Events without session ID return None."""
    runtime = _make_runtime()

    event = {
        "type": "message",
        "content": "No session here",
    }

    session_id = runtime._extract_event_session_id(event)

    assert session_id is None


# ---------------------------------------------------------------------------
# _build_command: permission mode mapping
# ---------------------------------------------------------------------------


def test_build_command_maps_bypass_permissions_to_yolo() -> None:
    """bypassPermissions maps to zcode's yolo mode."""
    runtime = ZcodeCLIRuntime(
        cli_path="/usr/bin/zcode",
        permission_mode="bypassPermissions",
    )
    cmd = runtime._build_command("/tmp/unused", prompt="test")

    assert "--approval-mode" in cmd
    idx = cmd.index("--approval-mode")
    assert cmd[idx + 1] == "yolo"


def test_build_command_maps_accept_edits_to_auto_edit() -> None:
    """acceptEdits maps to zcode's auto_edit mode."""
    runtime = ZcodeCLIRuntime(
        cli_path="/usr/bin/zcode",
        permission_mode="acceptEdits",
    )
    cmd = runtime._build_command("/tmp/unused", prompt="test")

    assert "--approval-mode" in cmd
    idx = cmd.index("--approval-mode")
    assert cmd[idx + 1] == "auto_edit"


def test_default_permission_mode_is_accept_edits() -> None:
    """Default permission mode is acceptEdits (auto_edit)."""
    runtime = ZcodeCLIRuntime(cli_path="/usr/bin/zcode")

    assert runtime.permission_mode == "acceptEdits"

    cmd = runtime._build_command("/tmp/unused", prompt="test")
    assert "--approval-mode" in cmd
    idx = cmd.index("--approval-mode")
    assert cmd[idx + 1] == "auto_edit"


def test_unknown_permission_mode_raises_value_error() -> None:
    """Unknown permission modes raise ValueError."""
    with pytest.raises(ValueError, match="Unsupported Zcode permission mode"):
        ZcodeCLIRuntime(
            cli_path="/usr/bin/zcode",
            permission_mode="unknown_mode",
        )


def test_permission_mode_default_normalized_to_accept_edits() -> None:
    """The 'default' mode is normalized to acceptEdits."""
    from structlog.testing import capture_logs

    with capture_logs() as cap_logs:
        runtime = ZcodeCLIRuntime(
            cli_path="/usr/bin/zcode",
            permission_mode="default",
        )

    assert runtime.permission_mode == "acceptEdits"
    coerced = [
        e for e in cap_logs if e.get("event") == "zcode_cli_runtime.permission_mode_coerced"
    ]
    assert len(coerced) == 1
    assert coerced[0]["requested"] == "default"
    assert coerced[0]["resolved"] == "acceptEdits"


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_build_command_includes_json_flag() -> None:
    """--json flag is always present for structured output."""
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="test")

    assert "--json" in cmd


def test_build_command_includes_non_interactive_flag() -> None:
    """--non-interactive ensures headless execution."""
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="test")

    assert "--non-interactive" in cmd


def test_build_command_passes_prompt_via_prompt_flag() -> None:
    """Prompt is passed via --prompt flag."""
    runtime = _make_runtime()
    cmd = runtime._build_command("/tmp/unused", prompt="fix the bug")

    assert "--prompt" in cmd
    idx = cmd.index("--prompt")
    assert cmd[idx + 1] == "fix the bug"


def test_build_command_includes_model_when_provided() -> None:
    """Custom model is passed via --model flag."""
    runtime = ZcodeCLIRuntime(
        cli_path="/usr/bin/zcode",
        model="glm-5-custom",
    )
    cmd = runtime._build_command("/tmp/unused", prompt="test")

    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "glm-5-custom"


def test_build_command_includes_resume_session_id() -> None:
    """Resume session ID is passed via --resume flag."""
    runtime = _make_runtime()
    cmd = runtime._build_command(
        "/tmp/unused",
        prompt="continue",
        resume_session_id="sess-abc123",
    )

    assert "--resume" in cmd
    idx = cmd.index("--resume")
    assert cmd[idx + 1] == "sess-abc123"


# ---------------------------------------------------------------------------
# Runtime capabilities
# ---------------------------------------------------------------------------


def test_runtime_capabilities_declare_targeted_resume() -> None:
    """Zcode supports targeted resume via --resume flag."""
    runtime = _make_runtime()

    assert runtime.capabilities.skill_dispatch is True
    assert runtime.capabilities.targeted_resume is True
    assert runtime.capabilities.structured_output is True


def test_runtime_capabilities_translated_system_prompt() -> None:
    """System prompts are translated (composed into user message)."""
    runtime = _make_runtime()

    assert runtime.capabilities.system_prompt_support == ParamSupport.TRANSLATED
    assert runtime.capabilities.tool_restriction_support == ParamSupport.TRANSLATED


def test_runtime_capabilities_ignore_reasoning_effort() -> None:
    """Reasoning effort is ignored (not enforced)."""
    runtime = _make_runtime()

    assert runtime.capabilities.reasoning_effort_support == ParamSupport.IGNORED


# ---------------------------------------------------------------------------
# Stdin handling
# ---------------------------------------------------------------------------


def test_runtime_does_not_feed_prompt_via_stdin() -> None:
    """Zcode uses --prompt flag, not stdin."""
    runtime = _make_runtime()

    assert runtime._feeds_prompt_via_stdin() is False
    assert runtime._requires_process_stdin() is False


# ---------------------------------------------------------------------------
# Recursion guard
# ---------------------------------------------------------------------------


def test_recursion_guard_increments_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recursion guard increments OUROBOROS_DEPTH."""
    monkeypatch.setenv("_OUROBOROS_DEPTH", "1")
    runtime = _make_runtime()
    env = runtime._build_child_env()

    assert env["_OUROBOROS_DEPTH"] == "2"


def test_recursion_guard_raises_at_max_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recursion guard raises RuntimeError at max depth."""
    monkeypatch.setenv("_OUROBOROS_DEPTH", str(_MAX_OUROBOROS_DEPTH))
    runtime = _make_runtime()

    with pytest.raises(RuntimeError, match="Maximum Ouroboros nesting depth"):
        runtime._build_child_env()


def test_recursion_guard_strips_ouroburos_runtime_envs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recursion guard strips OUROBOROS_AGENT_RUNTIME and OUROBOROS_LLM_BACKEND."""
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "zcode")
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "zcode")
    runtime = _make_runtime()
    env = runtime._build_child_env()

    assert "OUROBOROS_AGENT_RUNTIME" not in env
    assert "OUROBOROS_LLM_BACKEND" not in env


# ---------------------------------------------------------------------------
# Factory registration
# ---------------------------------------------------------------------------


def test_factory_resolves_zcode_alias() -> None:
    """Factory accepts 'zcode' and 'zcode_cli' aliases."""
    assert resolve_agent_runtime_backend("zcode") == "zcode"
    assert resolve_agent_runtime_backend("zcode_cli") == "zcode"
    assert resolve_agent_runtime_backend("ZCODE") == "zcode"
