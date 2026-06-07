"""Unit tests for backend-aware fan-out concurrency planning.

Ouroboros must plan delivery fan-out to comply with the connected backend's
concurrency/rate constraints rather than relying on the agent runtime to manage
it. Backends whose underlying LLM limits Ouroboros cannot know (the CLI
runtimes — hermes, codex, gemini, ...) are serialized by default and raised
only by explicit override. See ``docs`` RCA (P1 / R3).
"""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.backend_limits import (
    DEFAULT_UNKNOWN_MAX_CONCURRENCY,
    MAX_CONCURRENCY_ENV,
    BackendConcurrencyLimits,
    plan_fan_out_concurrency,
    resolve_backend_limits,
)


class TestResolveBackendLimits:
    """Resolution of per-backend limits from the registry and overrides."""

    def test_native_claude_is_not_concurrency_capped(self) -> None:
        # The native Claude adapter has its own shared RPM/TPM bucket, so it is
        # governed there rather than by a fan-out concurrency cap.
        limits = resolve_backend_limits("claude")

        assert limits.max_concurrency is None
        assert limits.requests_per_minute == 40
        assert limits.tokens_per_minute == 32_000

    def test_anthropic_alias_resolves_to_claude(self) -> None:
        assert resolve_backend_limits("anthropic").max_concurrency is None

    @pytest.mark.parametrize(
        "backend",
        ["hermes_cli", "hermes", "codex_cli", "gemini_cli", "opencode", "goose", "pi", "copilot"],
    )
    def test_cli_backends_are_serialized_by_default(self, backend: str) -> None:
        limits = resolve_backend_limits(backend)

        assert limits.max_concurrency == DEFAULT_UNKNOWN_MAX_CONCURRENCY == 1

    def test_unknown_or_missing_backend_is_serialized(self) -> None:
        assert resolve_backend_limits(None).max_concurrency == 1
        assert resolve_backend_limits("").max_concurrency == 1
        assert resolve_backend_limits("totally-made-up").max_concurrency == 1

    def test_backend_name_is_normalized(self) -> None:
        assert resolve_backend_limits("  Hermes_CLI ").max_concurrency == 1
        assert resolve_backend_limits("CLAUDE").max_concurrency is None

    def test_env_override_raises_cli_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MAX_CONCURRENCY_ENV, "4")

        assert resolve_backend_limits("hermes_cli").max_concurrency == 4

    def test_env_override_applies_to_known_backend_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MAX_CONCURRENCY_ENV, "2")

        assert resolve_backend_limits("claude").max_concurrency == 2

    @pytest.mark.parametrize("value", ["0", "-3", "not-a-number", ""])
    def test_invalid_env_override_is_ignored(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(MAX_CONCURRENCY_ENV, value)

        # Falls back to the registry default (serialize for a CLI backend).
        assert resolve_backend_limits("hermes_cli").max_concurrency == 1


class TestPlanFanOutConcurrency:
    """The pure planning function that caps requested workers."""

    def test_caps_requested_to_backend_max(self) -> None:
        limits = BackendConcurrencyLimits(backend="hermes", max_concurrency=1)

        assert plan_fan_out_concurrency(3, limits) == 1

    def test_uncapped_backend_respects_requested(self) -> None:
        limits = BackendConcurrencyLimits(backend="claude", max_concurrency=None)

        assert plan_fan_out_concurrency(3, limits) == 3

    def test_requested_below_cap_is_unchanged(self) -> None:
        limits = BackendConcurrencyLimits(backend="x", max_concurrency=8)

        assert plan_fan_out_concurrency(1, limits) == 1

    def test_never_returns_below_one(self) -> None:
        limits = BackendConcurrencyLimits(backend="x", max_concurrency=1)

        assert plan_fan_out_concurrency(0, limits) == 1
        assert plan_fan_out_concurrency(-5, limits) == 1
