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

    def test_extra_patterns_keep_literal_substring_semantics(self) -> None:
        assert is_transient_error(
            "custom cli startupsequence failed",
            extra_patterns=("startup",),
        )


class TestNoFalsePositives:
    """Plain-substring matching used to retry deterministic failures forever.

    ``"rate"`` fired inside ``generate``/``iterate``/``accurate`` and the HTTP
    status codes fired inside any digit run (a token count, a price), so a
    non-retryable error burned the whole retry budget before surfacing.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "prompt uses 15000 tokens, over the limit",
            "token count 15000 tokens exceeded limit",
            "failed to generate the response",
            "could not generate an accurate estimate",
            "iterate over the plan failed: schema invalid",
            "corporate policy forbids this model",
            "billing: cost was 0.500 usd",
            "prompt exceeds 5000 chars",
            "invalid api key: 401 unauthorized",
            "zipcode: 500 is invalid",
            "sample rate: 44100 is unsupported",
            "rate",
            "request rate exceeded",
            "received 500 tokens but the model limit is 200",
            "model returned 500 characters",
            "got 429 schema violations",
            "code: 500 is not a supported application value",
        ],
    )
    def test_digit_runs_and_word_fragments_are_not_transient(self, message: str) -> None:
        assert not is_transient_error(message)

    @pytest.mark.parametrize(
        "message",
        [
            # Status codes in a real status-code position.
            "http 500",
            "APIStatusError: 500 Internal Server Error",
            "upstream returned 500",
            "GitHub API error: 502",
            "Reconnecting... 1/5 (502 Bad Gateway)",
            "502 Bad Gateway final",
            "429 from https://api.openai.com/v1/responses",
            "received a 503 from upstream",
            "504 Gateway Timeout",
            "requests.exceptions.HTTPError: 503 Server Error: Service Unavailable for url",
            "429 Client Error Too Many Requests",
            "500 Server Error Internal Server Error",
            # Rate-limit spellings that must survive the boundary tightening.
            "rate limit exceeded",
            "rate-limited, retry in 30s",
            "rate_limit_exceeded",
            "RateLimitError: slow down",
            # Substring patterns deliberately left broad.
            "requests.exceptions.ReadTimeout",
            "TimeoutException: deadline exceeded",
            "service temporarily unavailable",
        ],
    )
    def test_genuine_transient_messages_still_match(self, message: str) -> None:
        assert is_transient_error(message)

    def test_status_code_matches_on_a_later_line(self) -> None:
        # Multi-line stderr must not hide a leading status code.
        assert is_transient_error("Reconnecting...\n429 slow down")


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
