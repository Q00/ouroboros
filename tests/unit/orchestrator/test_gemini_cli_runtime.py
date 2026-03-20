"""Unit tests for the Gemini CLI orchestrator runtime."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.gemini_cli_runtime import GeminiCliRuntime


class TestGeminiCliRuntimeInit:
    """Tests for runtime initialization and class constants."""

    def test_runtime_backend_constant(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="acceptEdits")
        assert runtime._runtime_backend == "gemini"
        assert runtime._runtime_handle_backend == "gemini_cli"
        assert runtime._display_name == "Gemini CLI"
        assert runtime._default_cli_name == "gemini"

    def test_explicit_cli_path(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/opt/gemini", permission_mode="acceptEdits")
        assert runtime._cli_path == "/opt/gemini"

    def test_configured_cli_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ouroboros.orchestrator.gemini_cli_runtime.get_gemini_cli_path",
            lambda: "/opt/gemini",
        )
        runtime = GeminiCliRuntime(permission_mode="acceptEdits")
        assert runtime._cli_path == "/opt/gemini"


class TestGeminiPermissions:
    """Tests for Gemini permission mode resolution."""

    def test_default_permission_mode(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="default")
        assert runtime._permission_mode == "default"
        args = runtime._build_permission_args()
        assert "--sandbox" in args

    def test_accept_edits_permission_mode(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="acceptEdits")
        assert runtime._permission_mode == "acceptEdits"
        args = runtime._build_permission_args()
        assert "--approval-mode" in args
        assert "auto_edit" in args

    def test_bypass_permissions_mode(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="bypassPermissions")
        assert runtime._permission_mode == "bypassPermissions"
        args = runtime._build_permission_args()
        assert "--yolo" in args

    def test_invalid_permission_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported Gemini permission mode"):
            GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="invalid")


class TestGeminiBuildCommand:
    """Tests for CLI command construction."""

    def test_basic_command(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="acceptEdits")
        cmd = runtime._build_command("/tmp/out.txt")
        assert cmd[0] == "/usr/bin/gemini"
        assert "-p" in cmd
        assert "-o" in cmd
        assert "stream-json" in cmd

    def test_command_includes_model_flag(self) -> None:
        runtime = GeminiCliRuntime(
            cli_path="/usr/bin/gemini",
            permission_mode="acceptEdits",
            model="gemini-2.5-pro",
        )
        cmd = runtime._build_command("/tmp/out.txt")
        assert "-m" in cmd
        idx = cmd.index("-m")
        assert cmd[idx + 1] == "gemini-2.5-pro"

    def test_resume_command(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="acceptEdits")
        cmd = runtime._build_command("/tmp/out.txt", resume_session_id="session-123")
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "session-123"

    def test_resume_rejects_unsafe_session_id(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="acceptEdits")
        with pytest.raises(ValueError, match="disallowed characters"):
            runtime._build_command("/tmp/out.txt", resume_session_id="bad;id")


class TestGeminiStdinBehavior:
    """Tests for stdin prompt feeding."""

    def test_feeds_prompt_via_stdin(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="acceptEdits")
        assert runtime._feeds_prompt_via_stdin() is True


class TestGeminiEventSessionId:
    """Tests for session ID extraction from runtime events."""

    def test_extracts_gemini_session_id(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="acceptEdits")
        event = {"session_id": "gemini-abc-123"}
        assert runtime._extract_event_session_id(event) == "gemini-abc-123"

    def test_extracts_camel_case_session_id(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="acceptEdits")
        event = {"sessionId": "gemini-abc-456"}
        assert runtime._extract_event_session_id(event) == "gemini-abc-456"

    def test_falls_back_to_parent_extraction(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="acceptEdits")
        event = {"thread_id": "thread-xyz"}
        assert runtime._extract_event_session_id(event) == "thread-xyz"

    def test_returns_none_for_no_session(self) -> None:
        runtime = GeminiCliRuntime(cli_path="/usr/bin/gemini", permission_mode="acceptEdits")
        event = {"type": "message"}
        assert runtime._extract_event_session_id(event) is None
