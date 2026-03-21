"""Unit tests for provider factory helpers."""

import pytest

from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter

# TODO: uncomment when OpenCode adapter is shipped
# from ouroboros.providers.opencode_adapter import OpenCodeLLMAdapter
from ouroboros.providers.factory import (
    create_llm_adapter,
    resolve_llm_backend,
    resolve_llm_permission_mode,
)
from ouroboros.providers.gemini_cli_adapter import GeminiCliLLMAdapter
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

    def test_resolves_gemini_aliases(self) -> None:
        """Gemini aliases normalize to gemini."""
        assert resolve_llm_backend("gemini") == "gemini"
        assert resolve_llm_backend("gemini_cli") == "gemini"

    def test_rejects_opencode_at_boundary(self) -> None:
        """OpenCode is rejected at resolve time since it is not yet shipped."""
        with pytest.raises(ValueError, match="not yet available"):
            resolve_llm_backend("opencode")

    def test_rejects_opencode_cli_alias_at_boundary(self) -> None:
        """OpenCode CLI alias is also rejected at resolve time."""
        with pytest.raises(ValueError, match="not yet available"):
            resolve_llm_backend("opencode_cli")

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

    def test_creates_codex_adapter_uses_configured_cli_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex factory consumes the shared CLI path helper when no explicit path is passed."""
        monkeypatch.setattr("ouroboros.providers.factory.get_codex_cli_path", lambda: "/tmp/codex")

        adapter = create_llm_adapter(backend="codex", cwd="/tmp/project")

        assert isinstance(adapter, CodexCliLLMAdapter)
        assert adapter._cli_path == "/tmp/codex"

    def test_creates_gemini_adapter(self) -> None:
        """Gemini backend returns GeminiCliLLMAdapter."""
        adapter = create_llm_adapter(backend="gemini", cwd="/tmp/project")
        assert isinstance(adapter, GeminiCliLLMAdapter)
        assert adapter._cwd == "/tmp/project"

    def test_creates_gemini_adapter_uses_configured_cli_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Gemini factory consumes the shared CLI path helper when no explicit path is passed."""
        monkeypatch.setattr(
            "ouroboros.providers.factory.get_gemini_cli_path", lambda: "/tmp/gemini"
        )

        adapter = create_llm_adapter(backend="gemini", cwd="/tmp/project")

        assert isinstance(adapter, GeminiCliLLMAdapter)
        assert adapter._cli_path == "/tmp/gemini"

    @pytest.mark.skip(reason="OpenCode adapter not yet shipped")
    def test_creates_opencode_adapter(self) -> None:
        """OpenCode backend returns OpenCodeLLMAdapter."""
        adapter = create_llm_adapter(backend="opencode", cwd="/tmp/project")
        assert isinstance(adapter, OpenCodeLLMAdapter)  # noqa: F821
        assert adapter._cwd == "/tmp/project"
        assert adapter._permission_mode == "acceptEdits"

    @pytest.mark.skip(reason="OpenCode adapter not yet shipped")
    def test_creates_opencode_adapter_uses_configured_cli_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OpenCode factory consumes the shared CLI path helper when no explicit path is passed."""
        monkeypatch.setattr(
            "ouroboros.providers.factory.get_opencode_cli_path",
            lambda: "/tmp/opencode",
        )

        adapter = create_llm_adapter(backend="opencode", cwd="/tmp/project")

        assert isinstance(adapter, OpenCodeLLMAdapter)  # noqa: F821
        assert adapter._cli_path == "/tmp/opencode"

    @pytest.mark.skip(reason="OpenCode adapter not yet shipped")
    def test_uses_configured_opencode_backend_alias_when_backend_omitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Configured OpenCode aliases should wire through the shared factory path."""
        monkeypatch.setattr("ouroboros.providers.factory.get_llm_backend", lambda: "opencode_cli")
        monkeypatch.setattr(
            "ouroboros.providers.factory.get_llm_permission_mode",
            lambda backend=None: "acceptEdits",  # noqa: ARG005
        )

        adapter = create_llm_adapter(cwd="/tmp/project", allowed_tools=["Read"], max_turns=2)

        assert isinstance(adapter, OpenCodeLLMAdapter)  # noqa: F821
        assert adapter._cwd == "/tmp/project"
        assert adapter._permission_mode == "acceptEdits"
        assert adapter._allowed_tools == ["Read"]
        assert adapter._max_turns == 2

    def test_interview_always_uses_claude_regardless_of_configured_backend(self) -> None:
        """Interview must always use Claude, even when Gemini/Codex is the default."""
        for backend in ("gemini", "codex", "codex_cli", "gemini_cli"):
            adapter = create_llm_adapter(backend=backend, use_case="interview")
            assert isinstance(adapter, ClaudeCodeAdapter), (
                f"Interview with backend={backend!r} should return ClaudeCodeAdapter"
            )

    def test_forwards_interview_options_to_claude_adapter_when_codex_backend(self) -> None:
        """Interview with codex backend should forward options to Claude adapter."""
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

        assert isinstance(adapter, ClaudeCodeAdapter)
        assert adapter._on_message is callback

    def test_uses_configured_permission_mode_when_omitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Factory uses config/env permission defaults when no explicit mode is provided."""
        monkeypatch.setattr(
            "ouroboros.providers.factory.get_llm_permission_mode",
            lambda backend=None: "acceptEdits",  # noqa: ARG005
        )

        adapter = create_llm_adapter(backend="codex", cwd="/tmp/project")

        assert isinstance(adapter, CodexCliLLMAdapter)
        assert adapter._permission_mode == "acceptEdits"

    @pytest.mark.skip(reason="OpenCode adapter not yet shipped")
    def test_opencode_adapter_uses_backend_specific_permission_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OpenCode uses its dedicated auto-approve default rather than the generic LLM mode."""
        monkeypatch.setattr(
            "ouroboros.providers.factory.get_llm_permission_mode",
            lambda backend=None: "acceptEdits" if backend == "opencode" else "default",
        )

        adapter = create_llm_adapter(backend="opencode", cwd="/tmp/project")

        assert isinstance(adapter, OpenCodeLLMAdapter)  # noqa: F821
        assert adapter._permission_mode == "acceptEdits"


class TestResolveLLMPermissionMode:
    """Tests for use-case-aware permission defaults."""

    def test_interview_mode_escalates_to_bypass_for_claude(self) -> None:
        """Interview needs bypassPermissions for Claude — read-only sandbox blocks LLM output."""
        assert (
            resolve_llm_permission_mode(backend="claude_code", use_case="interview")
            == "bypassPermissions"
        )

    def test_interview_mode_escalates_to_bypass_for_codex(self) -> None:
        """Interview needs bypassPermissions for Codex — read-only sandbox blocks LLM output."""
        assert (
            resolve_llm_permission_mode(backend="codex", use_case="interview")
            == "bypassPermissions"
        )

    def test_interview_mode_escalates_to_bypass_for_gemini(self) -> None:
        """Interview needs bypassPermissions for Gemini — sandbox blocks LLM output."""
        assert (
            resolve_llm_permission_mode(backend="gemini", use_case="interview")
            == "bypassPermissions"
        )

    def test_interview_mode_rejects_opencode(self) -> None:
        """OpenCode is rejected at resolve time, even for interview use case."""
        with pytest.raises(ValueError, match="not yet available"):
            resolve_llm_permission_mode(backend="opencode", use_case="interview")
