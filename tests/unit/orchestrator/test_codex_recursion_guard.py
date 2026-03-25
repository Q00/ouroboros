"""Tests for Codex recursive startup prevention (#185)."""

from __future__ import annotations

import os
from unittest.mock import patch

from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter


class TestCodexCliRuntimeChildEnv:
    """Test _build_child_env strips dangerous env vars."""

    def test_strips_ouroboros_agent_runtime(self) -> None:
        """Child env must not contain OUROBOROS_AGENT_RUNTIME."""
        runtime = CodexCliRuntime.__new__(CodexCliRuntime)
        with patch.dict(os.environ, {"OUROBOROS_AGENT_RUNTIME": "codex"}):
            env = runtime._build_child_env()
        assert "OUROBOROS_AGENT_RUNTIME" not in env

    def test_strips_ouroboros_llm_backend(self) -> None:
        """Child env must not contain OUROBOROS_LLM_BACKEND."""
        runtime = CodexCliRuntime.__new__(CodexCliRuntime)
        with patch.dict(os.environ, {"OUROBOROS_LLM_BACKEND": "codex"}):
            env = runtime._build_child_env()
        assert "OUROBOROS_LLM_BACKEND" not in env

    def test_increments_depth_counter(self) -> None:
        """Each child process increments _OUROBOROS_DEPTH."""
        runtime = CodexCliRuntime.__new__(CodexCliRuntime)
        with patch.dict(os.environ, {"_OUROBOROS_DEPTH": "2"}, clear=False):
            env = runtime._build_child_env()
        assert env["_OUROBOROS_DEPTH"] == "3"

    def test_depth_starts_at_one(self) -> None:
        """First child starts at depth 1 when parent has no depth var."""
        runtime = CodexCliRuntime.__new__(CodexCliRuntime)
        with patch.dict(os.environ, {}, clear=False):
            env = runtime._build_child_env()
        assert env["_OUROBOROS_DEPTH"] == "1"

    def test_preserves_other_env_vars(self) -> None:
        """Non-Ouroboros env vars are preserved."""
        runtime = CodexCliRuntime.__new__(CodexCliRuntime)
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/test"}):
            env = runtime._build_child_env()
        assert env.get("PATH") == "/usr/bin"
        assert env.get("HOME") == "/home/test"


class TestCodexCliAdapterChildEnv:
    """Test that CodexCliLLMAdapter also strips env vars."""

    def test_strips_ouroboros_agent_runtime(self) -> None:
        """Adapter child env must not contain OUROBOROS_AGENT_RUNTIME."""
        with patch.dict(os.environ, {"OUROBOROS_AGENT_RUNTIME": "codex"}):
            env = CodexCliLLMAdapter._build_child_env()
        assert "OUROBOROS_AGENT_RUNTIME" not in env

    def test_increments_depth(self) -> None:
        """Adapter also tracks recursion depth."""
        with patch.dict(os.environ, {"_OUROBOROS_DEPTH": "0"}, clear=False):
            env = CodexCliLLMAdapter._build_child_env()
        assert env["_OUROBOROS_DEPTH"] == "1"
