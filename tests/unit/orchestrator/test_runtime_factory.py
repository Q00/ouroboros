"""Unit tests for orchestrator runtime factory helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import ClaudeAgentAdapter
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

# TODO: uncomment when OpenCode runtime is shipped
# from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime
from ouroboros.orchestrator.runtime_factory import (
    create_agent_runtime,
    resolve_agent_runtime_backend,
)


class TestResolveAgentRuntimeBackend:
    """Tests for backend resolution."""

    def test_resolve_explicit_codex_alias(self) -> None:
        """Normalizes the codex_cli alias to codex."""
        assert resolve_agent_runtime_backend("codex_cli") == "codex"

    def test_resolve_uses_config_helper(self) -> None:
        """Falls back to config/env helper when no explicit backend is provided."""
        with patch(
            "ouroboros.orchestrator.runtime_factory.get_agent_runtime_backend",
            return_value="codex",
        ):
            assert resolve_agent_runtime_backend() == "codex"

    def test_resolve_rejects_opencode_at_boundary(self) -> None:
        """OpenCode is rejected at resolve time since it is not yet shipped."""
        with pytest.raises(ValueError, match="not yet available"):
            resolve_agent_runtime_backend("opencode")

    def test_resolve_rejects_opencode_cli_alias_at_boundary(self) -> None:
        """OpenCode CLI alias is also rejected at resolve time."""
        with pytest.raises(ValueError, match="not yet available"):
            resolve_agent_runtime_backend("opencode_cli")

    def test_resolve_rejects_unknown_backend(self) -> None:
        """Raises for unsupported backends."""
        with pytest.raises(ValueError):
            resolve_agent_runtime_backend("unknown")


class TestCreateAgentRuntime:
    """Tests for runtime construction."""

    def test_create_claude_runtime(self) -> None:
        """Creates the Claude adapter for the claude backend."""
        runtime = create_agent_runtime(backend="claude", permission_mode="acceptEdits")
        assert isinstance(runtime, ClaudeAgentAdapter)
        assert runtime._cwd

    def test_create_codex_runtime_uses_configured_cli_path(self) -> None:
        """Creates Codex runtime with the configured CLI path."""
        mock_dispatcher = object()

        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_codex_cli_path",
                return_value="/tmp/codex",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=mock_dispatcher,
            ) as mock_create_dispatcher,
        ):
            runtime = create_agent_runtime(
                backend="codex",
                permission_mode="acceptEdits",
                cwd="/tmp/project",
            )

        assert isinstance(runtime, CodexCliRuntime)
        assert runtime._cli_path == "/tmp/codex"
        assert runtime._cwd == "/tmp/project"
        assert runtime._skill_dispatcher is mock_dispatcher
        assert mock_create_dispatcher.call_args.kwargs["cwd"] == "/tmp/project"
        assert mock_create_dispatcher.call_args.kwargs["runtime_backend"] == "codex"

    def test_create_claude_runtime_uses_factory_cwd_and_cli_path(self) -> None:
        """Claude runtime receives the same construction options as other backends."""
        with patch(
            "ouroboros.orchestrator.runtime_factory.get_cli_path",
            return_value="/tmp/claude",
        ):
            runtime = create_agent_runtime(backend="claude", cwd="/tmp/project")

        assert isinstance(runtime, ClaudeAgentAdapter)
        assert runtime._cwd == "/tmp/project"
        assert runtime._cli_path == "/tmp/claude"

    @pytest.mark.skip(reason="OpenCode runtime not yet shipped")
    def test_create_opencode_runtime_uses_configured_cli_path(self) -> None:
        """Creates OpenCode runtime with the configured CLI path."""
        mock_dispatcher = object()

        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_opencode_cli_path",
                return_value="/tmp/opencode",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=mock_dispatcher,
            ) as mock_create_dispatcher,
        ):
            runtime = create_agent_runtime(
                backend="opencode",
                permission_mode="acceptEdits",
                cwd="/tmp/project",
            )

        assert isinstance(runtime, OpenCodeRuntime)  # noqa: F821
        assert runtime._cli_path == "/tmp/opencode"
        assert runtime._cwd == "/tmp/project"
        assert runtime._skill_dispatcher is mock_dispatcher
        assert mock_create_dispatcher.call_args.kwargs["cwd"] == "/tmp/project"
        assert mock_create_dispatcher.call_args.kwargs["runtime_backend"] == "opencode"

    @pytest.mark.skip(reason="OpenCode runtime not yet shipped")
    def test_create_runtime_uses_configured_opencode_alias_when_backend_omitted(self) -> None:
        """Configured OpenCode aliases should resolve through the shared runtime factory."""
        mock_dispatcher = object()

        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_agent_runtime_backend",
                return_value="opencode_cli",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.get_agent_permission_mode",
                return_value="acceptEdits",
            ) as mock_get_permission_mode,
            patch(
                "ouroboros.orchestrator.runtime_factory.get_llm_backend",
                return_value="opencode",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.get_opencode_cli_path",
                return_value="/tmp/opencode",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=mock_dispatcher,
            ) as mock_create_dispatcher,
        ):
            runtime = create_agent_runtime(cwd="/tmp/project")

        assert isinstance(runtime, OpenCodeRuntime)  # noqa: F821
        assert runtime._cli_path == "/tmp/opencode"
        assert runtime._cwd == "/tmp/project"
        assert runtime._permission_mode == "acceptEdits"
        assert runtime._skill_dispatcher is mock_dispatcher
        assert mock_get_permission_mode.call_args.kwargs["backend"] == "opencode"
        assert mock_create_dispatcher.call_args.kwargs["runtime_backend"] == "opencode"

    def test_create_runtime_uses_configured_permission_mode(self) -> None:
        """Runtime factory uses config/env permission defaults when omitted."""
        with patch(
            "ouroboros.orchestrator.runtime_factory.get_agent_permission_mode",
            return_value="bypassPermissions",
        ):
            runtime = create_agent_runtime(backend="codex")

        assert isinstance(runtime, CodexCliRuntime)
        assert runtime._permission_mode == "bypassPermissions"

    @pytest.mark.skip(reason="OpenCode runtime not yet shipped")
    def test_create_opencode_runtime_uses_backend_specific_permission_default(self) -> None:
        """OpenCode runtime asks the shared config helper for the OpenCode-specific mode."""
        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_agent_permission_mode",
                return_value="bypassPermissions",
            ) as mock_get_permission_mode,
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=object(),
            ),
        ):
            runtime = create_agent_runtime(backend="opencode")

        assert isinstance(runtime, OpenCodeRuntime)  # noqa: F821
        assert runtime._permission_mode == "bypassPermissions"
        assert mock_get_permission_mode.call_args.kwargs["backend"] == "opencode"

    def test_create_runtime_uses_configured_llm_backend_when_omitted(self) -> None:
        """Runtime factory reuses config/env llm backend defaults for builtin tool dispatch."""
        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_llm_backend",
                return_value="opencode",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=object(),
            ),
        ):
            runtime = create_agent_runtime(backend="codex")

        assert isinstance(runtime, CodexCliRuntime)
        assert runtime._llm_backend == "opencode"
