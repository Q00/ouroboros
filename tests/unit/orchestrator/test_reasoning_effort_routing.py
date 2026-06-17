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
