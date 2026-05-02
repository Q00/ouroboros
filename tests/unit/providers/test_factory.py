"""Unit tests for provider factory helpers."""

import builtins
import sys

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.events.io_recorder import IOJournalRecorder
from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.factory import (
    create_llm_adapter,
    resolve_llm_backend,
    resolve_llm_permission_mode,
)
from ouroboros.providers.litellm_adapter import LiteLLMAdapter
from ouroboros.providers.opencode_adapter import OpenCodeLLMAdapter


class _FakeEventStore:
    async def append(self, event: BaseEvent) -> None:
        pass


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

    def test_resolves_opencode_aliases(self) -> None:
        """OpenCode aliases normalize to opencode."""
        assert resolve_llm_backend("opencode") == "opencode"
        assert resolve_llm_backend("opencode_cli") == "opencode"

    def test_falls_back_to_configured_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Configured backend is used when no explicit backend is provided."""
        monkeypatch.setattr("ouroboros.providers.factory.get_llm_backend", lambda: "openai")
        assert resolve_llm_backend() == "litellm"

    def test_rejects_unknown_backend(self) -> None:
        """Unknown backend names raise ValueError."""
        with pytest.raises(ValueError, match="Unsupported LLM backend"):
            resolve_llm_backend("invalid")

    def test_rejects_hermes_backend(self) -> None:
        """Hermes is runtime-only until an LLM adapter exists."""
        with pytest.raises(ValueError, match="Unsupported LLM backend"):
            resolve_llm_backend("hermes")


class TestCreateLLMAdapter:
    """Tests for adapter construction."""

    def test_creates_claude_code_adapter(self) -> None:
        """Claude backend returns ClaudeCodeAdapter."""
        adapter = create_llm_adapter(backend="claude_code")
        assert isinstance(adapter, ClaudeCodeAdapter)

    def test_passes_timeout_to_claude_code_adapter(self) -> None:
        """Claude backend forwards application-level timeout to the adapter."""
        adapter = create_llm_adapter(backend="claude_code", timeout=42.0)
        assert isinstance(adapter, ClaudeCodeAdapter)
        assert adapter._timeout == 42.0

    def test_passes_cwd_to_claude_code_adapter(self) -> None:
        """Claude backend forwards cwd to the SDK adapter."""
        adapter = create_llm_adapter(backend="claude_code", cwd="/tmp/project")
        assert isinstance(adapter, ClaudeCodeAdapter)
        assert adapter._cwd == "/tmp/project"

    def test_creates_litellm_adapter(self) -> None:
        """LiteLLM backend returns LiteLLMAdapter."""
        adapter = create_llm_adapter(backend="litellm")
        assert isinstance(adapter, LiteLLMAdapter)

    def test_forwards_io_recorder_to_litellm_adapter(self) -> None:
        """LiteLLM factory path preserves explicit recorder wiring."""
        recorder = IOJournalRecorder(
            event_store=_FakeEventStore(),
            target_type="execution",
            target_id="exec_factory",
        )

        adapter = create_llm_adapter(backend="litellm", io_recorder=recorder)

        assert isinstance(adapter, LiteLLMAdapter)
        assert adapter._io_recorder is recorder

    def test_litellm_import_error_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing litellm dependency raises a helpful RuntimeError."""
        module_name = "ouroboros.providers.litellm_adapter"
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
            if name == module_name:
                raise ImportError("No module named 'litellm'")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.delitem(sys.modules, module_name, raising=False)
        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(
            RuntimeError, match="litellm backend requested but litellm is not installed"
        ):
            create_llm_adapter(backend="litellm")

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

    def test_creates_opencode_adapter(self) -> None:
        """OpenCode backend returns OpenCodeLLMAdapter."""
        adapter = create_llm_adapter(backend="opencode", cwd="/tmp/project")
        assert isinstance(adapter, OpenCodeLLMAdapter)
        assert adapter._cwd == "/tmp/project"

    def test_creates_opencode_adapter_uses_configured_cli_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OpenCode factory consumes the shared CLI path helper when no explicit path is passed."""
        monkeypatch.setattr(
            "ouroboros.providers.opencode_adapter.get_opencode_cli_path",
            lambda: "/tmp/opencode",
        )

        adapter = create_llm_adapter(backend="opencode", cwd="/tmp/project")

        assert isinstance(adapter, OpenCodeLLMAdapter)
        assert adapter._cli_path == "/tmp/opencode"

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

        assert isinstance(adapter, OpenCodeLLMAdapter)
        assert adapter._cwd == "/tmp/project"
        assert adapter._permission_mode == "acceptEdits"
        assert adapter._allowed_tools == ["Read"]
        assert adapter._max_turns == 2

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

    def test_opencode_adapter_uses_backend_specific_permission_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OpenCode uses its dedicated auto-approve default rather than the generic LLM mode."""
        monkeypatch.setattr(
            "ouroboros.providers.factory.get_llm_permission_mode",
            lambda backend=None: "acceptEdits" if backend == "opencode" else "default",
        )

        adapter = create_llm_adapter(backend="opencode", cwd="/tmp/project")

        assert isinstance(adapter, OpenCodeLLMAdapter)
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

    def test_interview_mode_escalates_to_bypass_for_opencode(self) -> None:
        """Interview needs bypassPermissions for OpenCode — read-only sandbox blocks LLM output."""
        assert (
            resolve_llm_permission_mode(backend="opencode", use_case="interview")
            == "bypassPermissions"
        )


class TestGeminiSoftToolEnforcement:
    """Gemini accepts ``allowed_tools`` but enforces them softly.

    The Gemini CLI has no ``--allowed-tools`` flag, so the adapter injects
    the envelope as a system-prompt directive and detects out-of-envelope
    ``tool_use`` events post-hoc.  The factory's job is to (a) still
    construct the adapter — failing fast would turn every interview or
    evaluation on Gemini into a hard error with no recovery path — and
    (b) make the soft-enforcement trade-off visible at construction time
    so operators can tell it apart from hard-enforced sessions.
    """

    def _stub_gemini_cli(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        fake_cli = tmp_path / "gemini"
        fake_cli.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        fake_cli.chmod(0o755)
        monkeypatch.setenv("OUROBOROS_GEMINI_CLI_PATH", str(fake_cli))

    def test_gemini_backend_accepts_allowed_tools_with_soft_enforcement(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Factory still builds a working adapter when Gemini gets an envelope."""
        self._stub_gemini_cli(monkeypatch, tmp_path)

        adapter = create_llm_adapter(
            backend="gemini",
            allowed_tools=["Read", "Grep", "Glob", "WebFetch", "WebSearch"],
        )

        assert adapter.__class__.__name__ == "GeminiCLIAdapter"
        # The adapter keeps the envelope for prompt injection + post-hoc
        # violation detection.
        assert adapter._allowed_tools == (  # type: ignore[attr-defined]
            "Read",
            "Grep",
            "Glob",
            "WebFetch",
            "WebSearch",
        )

    def test_gemini_backend_accepts_empty_allowed_tools(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """An explicit empty envelope still produces a working adapter —
        the prompt directive becomes "no tools allowed" instead of a
        named allowlist.
        """
        self._stub_gemini_cli(monkeypatch, tmp_path)

        adapter = create_llm_adapter(backend="gemini", allowed_tools=[])

        assert adapter.__class__.__name__ == "GeminiCLIAdapter"
        assert adapter._allowed_tools == ()  # type: ignore[attr-defined]

    def test_gemini_backend_accepts_unrestricted_callers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """``allowed_tools=None`` means "caller did not request enforcement"."""
        self._stub_gemini_cli(monkeypatch, tmp_path)

        adapter = create_llm_adapter(backend="gemini", allowed_tools=None)

        assert adapter.__class__.__name__ == "GeminiCLIAdapter"
        assert adapter._allowed_tools is None  # type: ignore[attr-defined]
