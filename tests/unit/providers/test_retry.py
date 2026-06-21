"""Tests for the shared transient-error classifier.

Locks in the consolidation that ended the per-adapter pattern drift: the three
adapters that route the user's Claude/Codex work must all recognise the same
transient core, and none may lose a signal it previously matched.
"""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.adapter import TRANSIENT_ERROR_PATTERNS as EXEC_PATTERNS
from ouroboros.providers.claude_code_adapter import _RETRYABLE_ERROR_PATTERNS as CLAUDE_PATTERNS
from ouroboros.providers.codex_cli_adapter import _RETRYABLE_ERROR_PATTERNS as CODEX_PATTERNS
from ouroboros.providers.retry import TRANSIENT_ERROR_PATTERNS, is_transient_error


class TestTransientCore:
    def test_core_covers_the_common_transient_signals(self) -> None:
        for term in ("rate", "429", "503", "timeout", "overloaded", "connection"):
            assert term in TRANSIENT_ERROR_PATTERNS

    def test_all_patterns_are_lowercase(self) -> None:
        # Matching lower-cases the message first, so an upper-case pattern would
        # silently never fire.
        assert all(p == p.lower() for p in TRANSIENT_ERROR_PATTERNS)


class TestIsTransientError:
    @pytest.mark.parametrize(
        "message",
        [
            "Error 429 Too Many Requests",
            "anthropic overloaded_error: please retry",
            "HTTP 503 Service Unavailable",
            "Connection reset by peer",
            "Request timed out after 60s",
            "rate limit exceeded",
        ],
    )
    def test_recognises_transient_messages(self, message: str) -> None:
        assert is_transient_error(message)

    def test_non_transient_message_is_not_retried(self) -> None:
        assert not is_transient_error("invalid api key: 401 unauthorized")

    def test_extra_patterns_extend_the_core(self) -> None:
        assert not is_transient_error("custom cli still in startup")
        assert is_transient_error("custom cli still in startup", extra_patterns=("startup",))


class TestNoDriftAcrossAdapters:
    """Each adopting adapter must be a superset of the shared core (no removals)."""

    def test_claude_completion_keeps_core_plus_bootstrap_signals(self) -> None:
        assert set(TRANSIENT_ERROR_PATTERNS).issubset(set(CLAUDE_PATTERNS))
        # Claude-CLI-specific bootstrap signals stay local to the Claude adapter.
        for term in ("empty response", "need retry", "startup"):
            assert term in CLAUDE_PATTERNS

    def test_codex_completion_adopts_core_verbatim(self) -> None:
        assert tuple(CODEX_PATTERNS) == TRANSIENT_ERROR_PATTERNS

    def test_execution_adapter_gains_overloaded_and_keeps_exit_code(self) -> None:
        # The execution adapter previously did NOT retry Anthropic 529 overloaded;
        # adopting the shared core closes that gap.
        assert "overloaded" in EXEC_PATTERNS
        # Its one execution-specific signal survives the consolidation.
        assert "exit code 1" in EXEC_PATTERNS
        assert set(TRANSIENT_ERROR_PATTERNS).issubset(set(EXEC_PATTERNS))
