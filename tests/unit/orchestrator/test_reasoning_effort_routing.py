"""Effort-first investment dial routing across agent runtimes (RFC #1405).

These tests pin the Agent-OS contract: the orchestrator hands every runtime an
abstract ``reasoning_effort`` level, and each runtime either ENFORCES it through
its native per-call mechanism (declaring ``reasoning_effort_support = NATIVE``)
or honestly declares that it cannot (the IGNORED default → advised). The proof's
"enforced rows" depend on this distinction being truthful, so it is tested
directly rather than assumed.
"""

from __future__ import annotations

from dataclasses import replace

from ouroboros.orchestrator.adapter import (
    FULL_CAPABILITIES,
    ParamSupport,
    RuntimeCapabilities,
)
from ouroboros.orchestrator.codex_cli_runtime import (
    _CODEX_REASONING_EFFORT_LEVELS,
    CodexCliRuntime,
)


class TestCapabilityDeclarations:
    def test_default_and_full_capabilities_ignore_effort(self) -> None:
        """A runtime that does not opt in must NOT claim native effort support.

        Guards the latent bug flagged in review: ``replace(FULL_CAPABILITIES, …)``
        runtimes (opencode, gjc, …) must inherit IGNORED, never a stray NATIVE.
        """
        bare = RuntimeCapabilities(
            skill_dispatch=True, targeted_resume=True, structured_output=True
        )
        assert bare.reasoning_effort_support is ParamSupport.IGNORED
        assert FULL_CAPABILITIES.reasoning_effort_support is ParamSupport.IGNORED
        # An inheriting runtime that overrides only other fields stays IGNORED.
        inherited = replace(FULL_CAPABILITIES, system_prompt_support=ParamSupport.TRANSLATED)
        assert inherited.reasoning_effort_support is ParamSupport.IGNORED

    def test_codex_runtime_declares_native_effort(self) -> None:
        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp")
        assert runtime.capabilities.reasoning_effort_support is ParamSupport.NATIVE

    def test_copilot_runtime_declares_native_effort(self) -> None:
        from ouroboros.orchestrator.copilot_cli_runtime import (
            _COPILOT_REASONING_EFFORT_LEVELS,
            CopilotCliRuntime,
        )

        runtime = CopilotCliRuntime(cli_path="copilot", cwd="/tmp")
        assert runtime.capabilities.reasoning_effort_support is ParamSupport.NATIVE
        # The enforceable vocabulary must be declared so a level the flag does not
        # accept is downgraded to advised instead of recorded as a false "enforced"
        # row. It must match exactly the allow-list _build_command applies.
        assert (
            runtime.capabilities.enforceable_reasoning_efforts == _COPILOT_REASONING_EFFORT_LEVELS
        )

    def test_gemini_and_goose_declare_advised_effort(self) -> None:
        from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
        from ouroboros.orchestrator.goose_runtime import GooseCliRuntime

        gemini = GeminiCLIRuntime(cli_path="gemini", cwd="/tmp")
        goose = GooseCliRuntime(cli_path="goose", cwd="/tmp")
        assert gemini.capabilities.reasoning_effort_support is ParamSupport.IGNORED
        assert goose.capabilities.reasoning_effort_support is ParamSupport.IGNORED


class TestCopilotEffortEnforcement:
    def _runtime(self):
        from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime

        return CopilotCliRuntime(cli_path="copilot", cwd="/tmp")

    def test_known_level_is_enforced_via_flag(self) -> None:
        command = self._runtime()._build_command("out.txt", prompt="hi", reasoning_effort="high")
        assert "--reasoning-effort" in command
        idx = command.index("--reasoning-effort")
        assert command[idx + 1] == "high"

    def test_unknown_level_is_not_injected(self) -> None:
        command = self._runtime()._build_command(
            "out.txt", prompt="hi", reasoning_effort="; rm -rf /"
        )
        assert "--reasoning-effort" not in command

    def test_no_effort_emits_no_flag(self) -> None:
        command = self._runtime()._build_command("out.txt", prompt="hi", reasoning_effort=None)
        assert "--reasoning-effort" not in command

    def test_subclasses_accept_effort_kwarg_without_error(self) -> None:
        """codex execute_task forwards reasoning_effort to _build_command on every
        CodexCliRuntime subclass — each override must accept it (regression guard).
        """
        from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
        from ouroboros.orchestrator.goose_runtime import GooseCliRuntime

        for runtime in (
            GeminiCLIRuntime(cli_path="gemini", cwd="/tmp"),
            GooseCliRuntime(cli_path="goose", cwd="/tmp"),
        ):
            # Must not raise; gemini/goose accept-and-ignore (no per-call flag).
            runtime._build_command("out.txt", prompt="hi", reasoning_effort="high")

    def test_supported_level_is_enforced_unsupported_is_advised(self) -> None:
        """The capability vocabulary keeps proof rows honest end to end.

        decide_effort must mark a level inside Copilot's enforceable set as
        ``enforced`` and a level outside it (e.g. Codex-only ``minimal``) as
        ``advised`` even though Copilot declares NATIVE — because _build_command
        silently drops a flag value it does not accept.
        """
        from ouroboros.orchestrator.copilot_cli_runtime import (
            _COPILOT_REASONING_EFFORT_LEVELS,
        )
        from ouroboros.orchestrator.effort_routing import decide_effort

        enforced = decide_effort(
            ParamSupport.NATIVE,
            base_effort="high",
            is_decomposed_child=False,
            enforceable_levels=_COPILOT_REASONING_EFFORT_LEVELS,
        )
        assert enforced.is_enforced
        assert enforced.mode == "enforced"

        advised = decide_effort(
            ParamSupport.NATIVE,
            base_effort="minimal",  # in EFFORT_LADDER, not in Copilot's flag vocab
            is_decomposed_child=False,
            enforceable_levels=_COPILOT_REASONING_EFFORT_LEVELS,
        )
        assert not advised.is_enforced
        assert advised.mode == "advised"
        assert advised.level == "minimal"  # still recorded, just not guaranteed


class TestCodexEffortEnforcement:
    def _runtime(self) -> CodexCliRuntime:
        return CodexCliRuntime(cli_path="codex", cwd="/tmp")

    def test_known_level_is_enforced_via_config_override(self) -> None:
        command = self._runtime()._build_command(
            output_last_message_path="/tmp/out.txt",
            reasoning_effort="high",
        )
        # Codex enforces it as a per-invocation config override, contiguous pair.
        assert "-c" in command
        idx = command.index("-c")
        assert command[idx + 1] == "model_reasoning_effort=high"

    def test_no_effort_emits_no_override(self) -> None:
        command = self._runtime()._build_command(
            output_last_message_path="/tmp/out.txt",
            reasoning_effort=None,
        )
        assert "model_reasoning_effort=high" not in command
        assert not any(
            isinstance(arg, str) and arg.startswith("model_reasoning_effort=") for arg in command
        )

    def test_unknown_level_is_not_injected(self) -> None:
        """An unexpected token must never reach the ``key=value`` override."""
        command = self._runtime()._build_command(
            output_last_message_path="/tmp/out.txt",
            reasoning_effort="; rm -rf /",
        )
        assert not any(
            isinstance(arg, str) and arg.startswith("model_reasoning_effort=") for arg in command
        )

    def test_every_advertised_level_is_accepted(self) -> None:
        runtime = self._runtime()
        for level in _CODEX_REASONING_EFFORT_LEVELS:
            command = runtime._build_command(
                output_last_message_path="/tmp/out.txt",
                reasoning_effort=level,
            )
            assert f"model_reasoning_effort={level}" in command
