"""Unit tests for orchestrator runtime factory helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import ClaudeAgentAdapter
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
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

    def test_create_runtime_uses_configured_permission_mode(self) -> None:
        """Runtime factory uses config/env permission defaults when omitted."""
        with patch(
            "ouroboros.orchestrator.runtime_factory.get_agent_permission_mode",
            return_value="bypassPermissions",
        ):
            runtime = create_agent_runtime(backend="codex")

        assert isinstance(runtime, CodexCliRuntime)
        assert runtime._permission_mode == "bypassPermissions"
