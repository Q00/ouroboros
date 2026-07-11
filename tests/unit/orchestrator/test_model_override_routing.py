"""Per-call model-tier override routing across agent runtimes (RFC #1405 sibling).

Sibling of ``test_reasoning_effort_routing``: the orchestrator hands a runtime a
per-call ``model`` override (cheaper model for a decomposed child, escalate on
retry) and each runtime either ENFORCES it through its native per-call mechanism
(declaring ``model_override_support = NATIVE``) or honestly declares it cannot
(the IGNORED default). Frugality routing only routes a model to runtimes that
enforce it, so the distinction is tested directly rather than assumed.
"""

from __future__ import annotations

from dataclasses import replace

from ouroboros.orchestrator.adapter import (
    FULL_CAPABILITIES,
    ClaudeAgentAdapter,
    ParamSupport,
    RuntimeCapabilities,
)
from ouroboros.orchestrator.claude_worker_runtime import build_claude_worker_runtime
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.codex_mcp_runtime import build_codex_mcp_worker_runtime


class TestCapabilityDeclarations:
    def test_default_and_full_capabilities_ignore_model_override(self) -> None:
        """A runtime that does not opt in must NOT claim native model support.

        Mirrors the reasoning_effort guard: ``replace(FULL_CAPABILITIES, …)``
        runtimes (opencode, gjc, …) must inherit IGNORED, never a stray NATIVE.
        """
        bare = RuntimeCapabilities(
            skill_dispatch=True, targeted_resume=True, structured_output=True
        )
        assert bare.model_override_support is ParamSupport.IGNORED
        assert FULL_CAPABILITIES.model_override_support is ParamSupport.IGNORED
        inherited = replace(FULL_CAPABILITIES, system_prompt_support=ParamSupport.TRANSLATED)
        assert inherited.model_override_support is ParamSupport.IGNORED

    def test_claude_adapter_declares_native_model_override(self) -> None:
        adapter = ClaudeAgentAdapter(api_key="test", cwd="/tmp")
        assert adapter.capabilities.model_override_support is ParamSupport.NATIVE

    def test_codex_runtime_declares_native_model_override(self) -> None:
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp")
        assert runtime.capabilities.model_override_support is ParamSupport.NATIVE

    def test_claude_worker_declares_native_model_override(self) -> None:
        rt = build_claude_worker_runtime(cwd="/tmp")
        assert rt.capabilities.model_override_support is ParamSupport.NATIVE

    def test_codex_mcp_declares_ignored_model_override(self) -> None:
        """codex-reply cannot re-target a warm thread's model, so codex_mcp must
        NOT claim NATIVE — the per-call override is not honored on resume."""
        rt = build_codex_mcp_worker_runtime(cwd="/tmp")
        assert rt.capabilities.model_override_support is ParamSupport.IGNORED

    def test_advised_codex_subclasses_ignore_model_override(self) -> None:
        """Gemini/Goose share the codex base but do not opt in, so they stay
        IGNORED (the orchestrator therefore never routes them a model)."""
        from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
        from ouroboros.orchestrator.goose_runtime import GooseCliRuntime

        gemini = GeminiCLIRuntime(cli_path="gemini", cwd="/tmp")
        goose = GooseCliRuntime(cli_path="goose", cwd="/tmp")
        assert gemini.capabilities.model_override_support is ParamSupport.IGNORED
        assert goose.capabilities.model_override_support is ParamSupport.IGNORED

    def test_opencode_ignores_model_override(self) -> None:
        """OpenCode uses composite ``provider/model`` ids where a bare routed id
        would break, so it is intentionally left un-opted-in (IGNORED)."""
        from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime

        rt = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        assert rt.capabilities.model_override_support is ParamSupport.IGNORED


class TestCodexModelOverrideEnforcement:
    def _runtime(self, *, model: str | None = None) -> CodexCliRuntime:
        return CodexCliRuntime(cli_path="codex", cwd="/tmp", model=model)

    def test_per_call_model_is_enforced_via_flag(self) -> None:
        command = self._runtime()._build_command(
            output_last_message_path="/tmp/out.txt",
            model="claude-haiku-4-5",
        )
        assert "--model" in command
        assert command[command.index("--model") + 1] == "claude-haiku-4-5"

    def test_per_call_model_wins_over_constructor_pin(self) -> None:
        command = self._runtime(model="o3")._build_command(
            output_last_message_path="/tmp/out.txt",
            model="gpt-5.5",
        )
        assert command[command.index("--model") + 1] == "gpt-5.5"
        assert "o3" not in command

    def test_no_per_call_model_falls_back_to_constructor(self) -> None:
        command = self._runtime(model="o3")._build_command(
            output_last_message_path="/tmp/out.txt",
        )
        assert command[command.index("--model") + 1] == "o3"

    def test_default_sentinel_per_call_model_is_a_noop(self) -> None:
        """``_normalize_model`` collapses the ``default`` sentinel to None, so a
        ``model="default"`` override is byte-identical to passing no override —
        both fall through to the unchanged constructor/runtime-profile fallback."""
        runtime = self._runtime()
        with_sentinel = runtime._build_command(
            output_last_message_path="/tmp/out.txt", model="default"
        )
        without_override = runtime._build_command(output_last_message_path="/tmp/out.txt")
        assert with_sentinel == without_override
