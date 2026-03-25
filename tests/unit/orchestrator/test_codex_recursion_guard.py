"""Tests for Codex recursive startup prevention (#185)."""

from __future__ import annotations

import os
from unittest.mock import patch

from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter


def _make_runtime() -> CodexCliRuntime:
    """Create a bare CodexCliRuntime without __init__ side effects."""
    return CodexCliRuntime.__new__(CodexCliRuntime)


class TestCodexCliRuntimeChildEnv:
    """Test _build_child_env strips dangerous env vars."""

    def test_strips_ouroboros_agent_runtime(self) -> None:
        """Child env must not contain OUROBOROS_AGENT_RUNTIME."""
        runtime = _make_runtime()
        with patch.dict(os.environ, {"OUROBOROS_AGENT_RUNTIME": "codex"}):
            env = runtime._build_child_env()
        assert "OUROBOROS_AGENT_RUNTIME" not in env

    def test_strips_ouroboros_llm_backend(self) -> None:
        """Child env must not contain OUROBOROS_LLM_BACKEND."""
        runtime = _make_runtime()
        with patch.dict(os.environ, {"OUROBOROS_LLM_BACKEND": "codex"}):
            env = runtime._build_child_env()
        assert "OUROBOROS_LLM_BACKEND" not in env

    def test_increments_depth_counter(self) -> None:
        """Each child process increments _OUROBOROS_DEPTH."""
        runtime = _make_runtime()
        with patch.dict(os.environ, {"_OUROBOROS_DEPTH": "2"}):
            env = runtime._build_child_env()
        assert env["_OUROBOROS_DEPTH"] == "3"

    def test_depth_starts_at_one_when_absent(self) -> None:
        """First child starts at depth 1 when parent has no depth var."""
        runtime = _make_runtime()
        env_clean = {k: v for k, v in os.environ.items() if k != "_OUROBOROS_DEPTH"}
        with patch.dict(os.environ, env_clean, clear=True):
            env = runtime._build_child_env()
        assert env["_OUROBOROS_DEPTH"] == "1"

    def test_malformed_depth_defaults_to_one(self) -> None:
        """Non-integer _OUROBOROS_DEPTH falls back to 1 instead of crashing."""
        runtime = _make_runtime()
        with patch.dict(os.environ, {"_OUROBOROS_DEPTH": "not_a_number"}):
            env = runtime._build_child_env()
        assert env["_OUROBOROS_DEPTH"] == "1"

    def test_empty_depth_defaults_to_one(self) -> None:
        """Empty string _OUROBOROS_DEPTH falls back to 1."""
        runtime = _make_runtime()
        with patch.dict(os.environ, {"_OUROBOROS_DEPTH": ""}):
            env = runtime._build_child_env()
        assert env["_OUROBOROS_DEPTH"] == "1"

    def test_preserves_other_env_vars(self) -> None:
        """Non-Ouroboros env vars are preserved."""
        runtime = _make_runtime()
        with patch.dict(os.environ, {"MY_TEST_VAR": "hello"}):
            env = runtime._build_child_env()
        assert env.get("MY_TEST_VAR") == "hello"


class TestCodexCliAdapterChildEnv:
    """Test that CodexCliLLMAdapter also strips env vars."""

    def test_strips_ouroboros_agent_runtime(self) -> None:
        """Adapter child env must not contain OUROBOROS_AGENT_RUNTIME."""
        with patch.dict(os.environ, {"OUROBOROS_AGENT_RUNTIME": "codex"}):
            env = CodexCliLLMAdapter._build_child_env()
        assert "OUROBOROS_AGENT_RUNTIME" not in env

    def test_increments_depth(self) -> None:
        """Adapter also tracks recursion depth."""
        with patch.dict(os.environ, {"_OUROBOROS_DEPTH": "0"}):
            env = CodexCliLLMAdapter._build_child_env()
        assert env["_OUROBOROS_DEPTH"] == "1"

    def test_malformed_depth_defaults_to_one(self) -> None:
        """Adapter handles non-integer _OUROBOROS_DEPTH gracefully."""
        with patch.dict(os.environ, {"_OUROBOROS_DEPTH": "garbage"}):
            env = CodexCliLLMAdapter._build_child_env()
        assert env["_OUROBOROS_DEPTH"] == "1"
