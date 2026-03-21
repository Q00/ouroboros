"""Unit tests for the Gemini CLI LLM adapter."""

from __future__ import annotations

import pytest

from ouroboros.providers.gemini_cli_adapter import GeminiCliLLMAdapter


class TestGeminiCliLLMAdapterInit:
    """Tests for adapter initialization."""

    def test_default_provider_name(self) -> None:
        adapter = GeminiCliLLMAdapter(cwd="/tmp")
        assert adapter._provider_name == "gemini_cli"
        assert adapter._display_name == "Gemini CLI"
        assert adapter._default_cli_name == "gemini"

    def test_explicit_cli_path(self) -> None:
        adapter = GeminiCliLLMAdapter(cli_path="/usr/local/bin/gemini", cwd="/tmp")
        assert adapter._cli_path == "/usr/local/bin/gemini"

    def test_configured_cli_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ouroboros.providers.gemini_cli_adapter.get_gemini_cli_path",
            lambda: "/opt/gemini",
        )
        adapter = GeminiCliLLMAdapter(cwd="/tmp")
        assert adapter._cli_path == "/opt/gemini"


class TestGeminiPermissionArgs:
    """Tests for permission flag translation."""

    def test_default_permission_mode(self) -> None:
        adapter = GeminiCliLLMAdapter(cwd="/tmp", permission_mode="default")
        args = adapter._build_permission_args()
        assert "--sandbox" in args
        assert "--approval-mode" in args

    def test_accept_edits_permission_mode(self) -> None:
        adapter = GeminiCliLLMAdapter(cwd="/tmp", permission_mode="acceptEdits")
        args = adapter._build_permission_args()
        assert "--approval-mode" in args
        assert "auto_edit" in args

    def test_bypass_permissions_mode(self) -> None:
        adapter = GeminiCliLLMAdapter(cwd="/tmp", permission_mode="bypassPermissions")
        args = adapter._build_permission_args()
        assert "--yolo" in args

    def test_invalid_permission_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported Gemini permission mode"):
            GeminiCliLLMAdapter(cwd="/tmp", permission_mode="invalid")


class TestGeminiBuildCommand:
    """Tests for CLI command construction."""

    def test_basic_command_structure(self) -> None:
        adapter = GeminiCliLLMAdapter(
            cli_path="/usr/bin/gemini",
            cwd="/tmp",
            permission_mode="acceptEdits",
        )
        cmd = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )
        assert cmd[0] == "/usr/bin/gemini"
        assert "-p" in cmd
        assert "-o" in cmd
        assert "json" in cmd

    def test_command_includes_model_flag(self) -> None:
        adapter = GeminiCliLLMAdapter(
            cli_path="/usr/bin/gemini",
            cwd="/tmp",
            permission_mode="acceptEdits",
        )
        cmd = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model="gemini-2.5-pro",
        )
        assert "-m" in cmd
        idx = cmd.index("-m")
        assert cmd[idx + 1] == "gemini-2.5-pro"


class TestGeminiSessionIdExtraction:
    """Tests for session ID extraction from events."""

    def test_extracts_session_id_from_event(self) -> None:
        adapter = GeminiCliLLMAdapter(cwd="/tmp")
        event = {"session_id": "gemini-abc-123"}
        assert adapter._extract_session_id_from_event(event) == "gemini-abc-123"

    def test_returns_none_for_missing_session_id(self) -> None:
        adapter = GeminiCliLLMAdapter(cwd="/tmp")
        event = {"type": "message"}
        assert adapter._extract_session_id_from_event(event) is None

    def test_extracts_session_id_from_stdout_lines(self) -> None:
        adapter = GeminiCliLLMAdapter(cwd="/tmp")
        lines = [
            '{"type": "start"}',
            '{"session_id": "gemini-session-42"}',
        ]
        assert adapter._extract_session_id(lines) == "gemini-session-42"
