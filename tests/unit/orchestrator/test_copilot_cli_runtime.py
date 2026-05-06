"""Focused unit tests for the Copilot CLI runtime.

Covers the orchestrator-side wiring that completes Phase 4 of the Copilot
integration: extending ``OrchestratorConfig.runtime_backend`` with
``copilot``, registering the runtime in ``runtime_factory``, and the
Copilot-specific command construction.

The Copilot adapter logic at the LLM layer is exercised separately in
``tests/unit/providers/test_copilot_cli_adapter.py``; this file only tests
what the orchestrator runtime adds on top.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from ouroboros.orchestrator.copilot_cli_runtime import (
    _MAX_OUROBOROS_DEPTH,
    CopilotCliRuntime,
)
from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend


def _make_runtime(model: str | None = None, cwd: str = "/work") -> CopilotCliRuntime:
    return CopilotCliRuntime(cli_path="/usr/bin/copilot", model=model, cwd=cwd)


# ---------------------------------------------------------------------------
# resolve_agent_runtime_backend recognises copilot + alias
# ---------------------------------------------------------------------------


def test_resolve_agent_runtime_backend_accepts_copilot() -> None:
    assert resolve_agent_runtime_backend("copilot") == "copilot"


def test_resolve_agent_runtime_backend_accepts_copilot_cli_alias() -> None:
    assert resolve_agent_runtime_backend("copilot_cli") == "copilot"


def test_resolve_agent_runtime_backend_rejection_lists_copilot() -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_agent_runtime_backend("not-a-backend")
    assert "copilot" in str(excinfo.value)


# ---------------------------------------------------------------------------
# _build_command: Copilot-specific argv shape
# ---------------------------------------------------------------------------


def test_build_command_emits_copilot_flags_in_documented_order(tmp_path) -> None:
    cwd = str(tmp_path)
    runtime = CopilotCliRuntime(cli_path="/usr/bin/copilot", cwd=cwd)
    command = runtime._build_command(
        output_last_message_path="/tmp/ignored",
        prompt="hello world",
    )

    assert command[0].endswith("copilot")
    assert "--no-color" in command
    assert command[command.index("--add-dir") + 1] == cwd
    assert command[-2:] == ["-p", "hello world"]


def test_build_command_omits_model_flag_when_no_model_set() -> None:
    runtime = _make_runtime(model=None)
    command = runtime._build_command(
        output_last_message_path="/tmp/ignored",
        prompt="task",
    )
    assert "--model" not in command


def test_build_command_maps_anthropic_hyphen_id_to_dotted_form() -> None:
    runtime = _make_runtime(model="claude-opus-4-6")
    command = runtime._build_command(
        output_last_message_path="/tmp/ignored",
        prompt="task",
    )
    idx = command.index("--model")
    assert command[idx + 1] == "claude-opus-4.6"


def test_build_command_passes_dotted_model_through_unchanged() -> None:
    runtime = _make_runtime(model="claude-sonnet-4.5")
    command = runtime._build_command(
        output_last_message_path="/tmp/ignored",
        prompt="task",
    )
    idx = command.index("--model")
    assert command[idx + 1] == "claude-sonnet-4.5"


def test_build_command_ignores_resume_session_id() -> None:
    """Copilot CLI has no resume API; the runtime must ignore the param."""
    runtime = _make_runtime()
    command = runtime._build_command(
        output_last_message_path="/tmp/ignored",
        prompt="task",
        resume_session_id="sess-123",
    )
    assert "resume" not in command
    assert "sess-123" not in command


def test_constructor_runtime_profile_emits_copilot_agent_flag() -> None:
    runtime = CopilotCliRuntime(
        cli_path="/usr/bin/copilot",
        cwd="/work",
        runtime_profile="worker",
    )

    command = runtime._build_command(
        output_last_message_path="/tmp/ignored",
        prompt="task",
    )

    assert command[command.index("--agent") + 1] == "ouroboros-worker"
    assert "--model" not in command


def test_runtime_handle_profile_metadata_emits_copilot_agent_flag() -> None:
    runtime = _make_runtime()
    handle = runtime._build_runtime_handle("sess-1")
    assert handle is not None
    handle = replace(handle, metadata={"agent_runtime_profile": "worker"})

    command = runtime._build_command(
        output_last_message_path="/tmp/ignored",
        prompt="task",
        runtime_handle=handle,
    )

    assert command[command.index("--agent") + 1] == "ouroboros-worker"
    assert "--model" not in command


def test_build_command_uses_p_flag_not_stdin() -> None:
    runtime = _make_runtime()
    assert runtime._feeds_prompt_via_stdin() is False
    assert runtime._requires_process_stdin() is False


# ---------------------------------------------------------------------------
# Copilot JSONL event conversion
# ---------------------------------------------------------------------------


def test_convert_event_returns_copilot_agent_message_content() -> None:
    runtime = _make_runtime()

    messages = runtime._convert_event(
        {"type": "agent.message", "message": {"text": "All clear."}},
        current_handle=None,
    )

    assert len(messages) == 1
    assert messages[0].type == "assistant"
    assert messages[0].content == "All clear."


def test_convert_event_binds_copilot_session_started_handle() -> None:
    runtime = _make_runtime()

    messages = runtime._convert_event(
        {"type": "session.started", "session_id": "sess-1"},
        current_handle=None,
    )

    assert len(messages) == 1
    assert messages[0].type == "system"
    assert messages[0].resume_handle is not None
    assert messages[0].resume_handle.native_session_id == "sess-1"


def test_convert_event_maps_copilot_tool_and_error_events() -> None:
    runtime = _make_runtime()

    tool_messages = runtime._convert_event(
        {"type": "tool_call", "name": "read", "input": {"path": "README.md"}},
        current_handle=None,
    )
    error_messages = runtime._convert_event(
        {"type": "turn.failed", "error": {"message": "nope"}},
        current_handle=None,
    )

    assert tool_messages[0].tool_name == "read"
    assert tool_messages[0].data["tool_input"] == {"path": "README.md"}
    assert error_messages[0].is_final
    assert error_messages[0].is_error
    assert error_messages[0].content == "nope"


# ---------------------------------------------------------------------------
# Resume capability is explicitly disabled
# ---------------------------------------------------------------------------


def test_build_resume_recovery_returns_none() -> None:
    runtime = _make_runtime()
    result = runtime._build_resume_recovery(
        attempted_resume_session_id="sess-1",
        current_handle=None,
        returncode=1,
        final_message="",
        stderr_lines=[],
    )
    assert result is None


def test_runtime_class_attributes_use_copilot_backend_identity() -> None:
    runtime = _make_runtime()
    assert runtime._runtime_backend == "copilot"
    assert runtime._runtime_handle_backend == "copilot_cli"
    assert runtime._provider_name == "copilot_cli"
    assert runtime._default_cli_name == "copilot"
    assert runtime._max_resume_retries == 0


# ---------------------------------------------------------------------------
# Recursion guard is wired through the Copilot child env helper
# ---------------------------------------------------------------------------


def test_child_env_increments_ouroboros_depth_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("_OUROBOROS_DEPTH", "2")
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "copilot")
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "copilot")
    runtime = _make_runtime()

    env = runtime._build_child_env()

    assert env["_OUROBOROS_DEPTH"] == "3"
    # Recursion markers stripped so the child does not re-enter Ouroboros MCP.
    assert "OUROBOROS_AGENT_RUNTIME" not in env
    assert "OUROBOROS_LLM_BACKEND" not in env


def test_child_env_refuses_to_exceed_max_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("_OUROBOROS_DEPTH", str(_MAX_OUROBOROS_DEPTH))
    runtime = _make_runtime()

    with pytest.raises(RuntimeError, match="nesting depth"):
        runtime._build_child_env()


# ---------------------------------------------------------------------------
# Permission mode mapping
# ---------------------------------------------------------------------------


def test_resolve_permission_mode_passes_through_known_values() -> None:
    runtime = _make_runtime()
    assert runtime._resolve_permission_mode("acceptEdits") == "acceptEdits"
    assert runtime._resolve_permission_mode("bypassPermissions") == "bypassPermissions"
    assert runtime._resolve_permission_mode("default") == "default"


def test_resolve_permission_mode_falls_back_to_default_on_unknown() -> None:
    runtime = _make_runtime()
    assert runtime._resolve_permission_mode(None) == "default"


def test_build_permission_args_returns_list() -> None:
    """Sanity check that the Copilot envelope flags come back as a list."""
    runtime = _make_runtime()
    args = runtime._build_permission_args()
    assert isinstance(args, list)
