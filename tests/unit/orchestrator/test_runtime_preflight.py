"""Runtime preflight: a runtime that cannot start says so once, before dispatch.

In the July event store, "Claude Agent SDK is not installed" accounted for 15
AC-session failures: the SDK import happens lazily inside ``execute_task``, so
every AC paid for a dispatch, its retries, and route escalation to learn the
same configuration fact. ``preflight_agent_runtime`` asks the runtime up front.
"""

from __future__ import annotations

from unittest.mock import patch

from ouroboros.core.retry import NON_TRANSIENT_OVERRIDE_PATTERNS, is_transient_error
from ouroboros.orchestrator.adapter import ClaudeAgentAdapter
from ouroboros.orchestrator.runtime_factory import preflight_agent_runtime


class TestClaudeAdapterPreflight:
    def test_missing_sdk_is_reported_with_the_two_remedies(self) -> None:
        adapter = ClaudeAgentAdapter(cwd="/tmp/project")
        with patch("ouroboros.orchestrator.adapter.importlib.util.find_spec", return_value=None):
            reason = adapter.preflight()
            assert preflight_agent_runtime(adapter) == reason

        assert reason is not None
        assert "claude-agent-sdk" in reason
        assert "--runtime claude-cli" in reason

    def test_installed_sdk_passes_preflight(self) -> None:
        adapter = ClaudeAgentAdapter(cwd="/tmp/project")
        with patch(
            "ouroboros.orchestrator.adapter.importlib.util.find_spec", return_value=object()
        ):
            assert adapter.preflight() is None
            assert preflight_agent_runtime(adapter) is None


class TestPreflightHelper:
    def test_runtime_without_preflight_is_not_blocked(self) -> None:
        class Runtime:
            pass

        assert preflight_agent_runtime(Runtime()) is None  # type: ignore[arg-type]

    def test_blank_or_non_string_reasons_are_ignored(self) -> None:
        class Blank:
            def preflight(self) -> str:
                return "   "

        class Wrong:
            def preflight(self) -> object:
                return {"reason": "x"}

        assert preflight_agent_runtime(Blank()) is None  # type: ignore[arg-type]
        assert preflight_agent_runtime(Wrong()) is None  # type: ignore[arg-type]

    def test_reason_is_passed_through(self) -> None:
        class Blocked:
            def preflight(self) -> str:
                return "CLI missing"

        assert preflight_agent_runtime(Blocked()) == "CLI missing"  # type: ignore[arg-type]


class TestNonTransientOverrides:
    """A 5xx that wraps a configuration failure must not be retried."""

    def test_proxy_auth_and_provider_failures_are_not_transient(self) -> None:
        for message in (
            "unexpected status 503 Service Unavailable: auth_unavailable: no auth available "
            "(providers=codex, model=gpt-5.6)",
            "unexpected status 502 Bad Gateway: unknown provider for model gpt-4o, "
            "url: https://proxy.example/v1/responses",
            "HTTP 401 Unauthorized: invalid_api_key",
            "status 403 Forbidden: permission denied for this model",
        ):
            assert is_transient_error(message) is False, message

    def test_genuine_gateway_failures_stay_transient(self) -> None:
        for message in (
            "unexpected status 503 Service Unavailable",
            "HTTP 502 Bad Gateway",
            "rate_limit_error: too many requests (429)",
            "overloaded_error: Anthropic is overloaded (529)",
            "stream disconnected before completion: connection reset",
        ):
            assert is_transient_error(message) is True, message

    def test_override_vocabulary_is_closed_and_lowercase(self) -> None:
        assert NON_TRANSIENT_OVERRIDE_PATTERNS
        assert all(pattern == pattern.lower() for pattern in NON_TRANSIENT_OVERRIDE_PATTERNS)
