"""Unit tests for provider factory helpers."""

import pytest

from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.factory import (
    create_llm_adapter,
    resolve_llm_backend,
    resolve_llm_permission_mode,
)
from ouroboros.providers.litellm_adapter import LiteLLMAdapter


class TestResolveLLMBackend:
    """Tests for backend normalization."""

    def test_resolves_claude_aliases(self) -> None:
        """Claude aliases normalize to claude_code."""
        assert resolve_llm_backend("claude") == "claude_code"
        assert resolve_llm_backend("claude_code") == "claude_code"

    def test_resolves_litellm_aliases(self) -> None:
        """LiteLLM aliases normalize to litellm."""
        assert resolve_llm_backend("litellm") == "litellm"
        assert resolve_llm_backend("openai") == "litellm"
        assert resolve_llm_backend("openrouter") == "litellm"

    def test_resolves_codex_aliases(self) -> None:
        """Codex aliases normalize to codex."""
        assert resolve_llm_backend("codex") == "codex"
        assert resolve_llm_backend("codex_cli") == "codex"

    def test_falls_back_to_configured_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Configured backend is used when no explicit backend is provided."""
        monkeypatch.setattr("ouroboros.providers.factory.get_llm_backend", lambda: "openai")
        assert resolve_llm_backend() == "litellm"

    def test_rejects_unknown_backend(self) -> None:
        """Unknown backend names raise ValueError."""
        with pytest.raises(ValueError, match="Unsupported LLM backend"):
            resolve_llm_backend("invalid")


class TestCreateLLMAdapter:
    """Tests for adapter construction."""

    def test_creates_claude_code_adapter(self) -> None:
        """Claude backend returns ClaudeCodeAdapter."""
        adapter = create_llm_adapter(backend="claude_code")
        assert isinstance(adapter, ClaudeCodeAdapter)

    def test_creates_litellm_adapter(self) -> None:
        """LiteLLM backend returns LiteLLMAdapter."""
        adapter = create_llm_adapter(backend="litellm")
        assert isinstance(adapter, LiteLLMAdapter)

    def test_creates_codex_adapter(self) -> None:
        """Codex backend returns CodexCliLLMAdapter."""
        adapter = create_llm_adapter(backend="codex", cwd="/tmp/project")
        assert isinstance(adapter, CodexCliLLMAdapter)
        assert adapter._cwd == "/tmp/project"

    def test_forwards_interview_options_to_codex_adapter(self) -> None:
        """Codex backend receives interview/debug options through the factory."""
        callback_calls: list[tuple[str, str]] = []

        def callback(message_type: str, content: str) -> None:
            callback_calls.append((message_type, content))

        adapter = create_llm_adapter(
            backend="codex",
            cwd="/tmp/project",
            use_case="interview",
            allowed_tools=["Read", "Grep"],
            max_turns=5,
            on_message=callback,
        )

        assert isinstance(adapter, CodexCliLLMAdapter)
        assert adapter._allowed_tools == ["Read", "Grep"]
        assert adapter._max_turns == 5
        assert adapter._on_message is callback

    def test_uses_configured_permission_mode_when_omitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Factory uses config/env permission defaults when no explicit mode is provided."""
        monkeypatch.setattr(
            "ouroboros.providers.factory.get_llm_permission_mode",
            lambda: "acceptEdits",
        )

        adapter = create_llm_adapter(backend="codex", cwd="/tmp/project")

        assert isinstance(adapter, CodexCliLLMAdapter)
        assert adapter._permission_mode == "acceptEdits"


class TestResolveLLMPermissionMode:
    """Tests for use-case-aware permission defaults."""

    def test_interview_mode_uses_bypass_for_claude(self) -> None:
        """Claude interview flows keep their permissive legacy behavior."""
        assert (
            resolve_llm_permission_mode(backend="claude_code", use_case="interview")
            == "bypassPermissions"
        )

    def test_interview_mode_uses_accept_edits_for_codex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex interview flows elevate to acceptEdits for codebase read access."""
        monkeypatch.setattr(
            "ouroboros.providers.factory.get_llm_permission_mode",
            lambda: "default",
        )

        assert resolve_llm_permission_mode(backend="codex", use_case="interview") == "acceptEdits"
