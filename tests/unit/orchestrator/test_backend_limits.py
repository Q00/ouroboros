"""Unit tests for backend-aware fan-out concurrency planning.

Ouroboros must plan delivery fan-out to comply with the connected backend's
concurrency/rate constraints rather than relying on the agent runtime to manage
it. Backends whose underlying LLM limits Ouroboros cannot know (the CLI
runtimes — hermes, codex, gemini, ...) are serialized by default and raised
only by explicit override. See ``docs`` RCA (P1 / R3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.orchestrator import backend_limits as backend_limits_module
from ouroboros.orchestrator.backend_limits import (
    BACKEND_LIMITS_PATH_ENV,
    DEFAULT_UNKNOWN_MAX_CONCURRENCY,
    MAX_CONCURRENCY_ENV,
    BackendConcurrencyLimits,
    plan_fan_out_concurrency,
    resolve_backend_limits,
)


@pytest.fixture(autouse=True)
def _isolate_backend_limits_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point limit resolution at a non-existent config path and clear the cache.

    Keeps every test hermetic from a real ``~/.ouroboros/backend_limits.yaml``
    and from cross-test cache bleed (the loader caches by resolved path/mtime).
    """
    monkeypatch.setenv(BACKEND_LIMITS_PATH_ENV, str(tmp_path / "absent.yaml"))
    backend_limits_module._RAW_LIMITS_CACHE.clear()


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


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    config_path = tmp_path / "backend_limits.yaml"
    config_path.write_text(body, encoding="utf-8")
    monkeypatch.setenv(BACKEND_LIMITS_PATH_ENV, str(config_path))
    backend_limits_module._RAW_LIMITS_CACHE.clear()
    return config_path


class TestRateLimitsDormantByDefault:
    """Without configuration, rate pacing stays off for CLI backends."""

    @pytest.mark.parametrize("backend", ["hermes_cli", "codex_cli", "opencode", "goose"])
    def test_cli_backends_have_no_rate_limits_by_default(self, backend: str) -> None:
        limits = resolve_backend_limits(backend)

        assert limits.requests_per_minute is None
        assert limits.tokens_per_minute is None


class TestPerBackendRateEnvOverrides:
    """``OUROBOROS_<BACKEND>_RPM`` / ``_TPM`` declare a dormant backend's budget."""

    def test_env_declares_rpm_and_tpm_for_cli_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OUROBOROS_HERMES_CLI_RPM", "5")
        monkeypatch.setenv("OUROBOROS_HERMES_CLI_TPM", "12000")

        limits = resolve_backend_limits("hermes_cli")

        assert limits.requests_per_minute == 5
        assert limits.tokens_per_minute == 12000
        # Declaring a rate budget does not change the fan-out cap.
        assert limits.max_concurrency == DEFAULT_UNKNOWN_MAX_CONCURRENCY

    def test_env_prefix_canonicalizes_backend_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "opencode" → OUROBOROS_OPENCODE_RPM
        monkeypatch.setenv("OUROBOROS_OPENCODE_RPM", "3")

        assert resolve_backend_limits("opencode").requests_per_minute == 3

    @pytest.mark.parametrize("value", ["0", "-1", "nope", ""])
    def test_invalid_rate_env_is_ignored(self, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OUROBOROS_HERMES_CLI_RPM", value)

        assert resolve_backend_limits("hermes_cli").requests_per_minute is None


class TestConfigFileLimits:
    """Limits are data-driven via ``~/.ouroboros/backend_limits.yaml``."""

    def test_backends_block_declares_cli_rate_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(
            tmp_path,
            monkeypatch,
            "backends:\n  hermes_cli:\n    requests_per_minute: 2\n    tokens_per_minute: 8000\n",
        )

        limits = resolve_backend_limits("hermes_cli")

        assert limits.requests_per_minute == 2
        assert limits.tokens_per_minute == 8000

    def test_flat_top_level_mapping_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(tmp_path, monkeypatch, "opencode:\n  requests_per_minute: 7\n")

        assert resolve_backend_limits("opencode").requests_per_minute == 7

    def test_claude_ceilings_are_overridable_without_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(
            tmp_path,
            monkeypatch,
            "backends:\n  claude:\n    requests_per_minute: 100\n    tokens_per_minute: 90000\n",
        )

        limits = resolve_backend_limits("claude")

        assert limits.requests_per_minute == 100
        assert limits.tokens_per_minute == 90000
        assert limits.max_concurrency is None  # still self-governed

    def test_default_cap_is_overridable_without_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(tmp_path, monkeypatch, "default:\n  max_concurrency: 3\n")

        # The default block applies to any backend lacking its own entry.
        assert resolve_backend_limits("totally-made-up").max_concurrency == 3

    def test_config_key_aliases_are_canonicalized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(
            tmp_path, monkeypatch, "backends:\n  claude_code:\n    requests_per_minute: 55\n"
        )

        # "claude_code" alias in config applies to the canonical "claude" backend.
        assert resolve_backend_limits("claude").requests_per_minute == 55

    @pytest.mark.parametrize("value", ["0", "-2"])
    def test_non_positive_config_values_are_ignored(
        self, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(
            tmp_path, monkeypatch, f"backends:\n  hermes_cli:\n    requests_per_minute: {value}\n"
        )

        assert resolve_backend_limits("hermes_cli").requests_per_minute is None


class TestConfigFileFaultTolerance:
    """A broken config must never break limit resolution."""

    def test_missing_file_falls_back_to_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(BACKEND_LIMITS_PATH_ENV, str(tmp_path / "nope.yaml"))
        backend_limits_module._RAW_LIMITS_CACHE.clear()

        assert resolve_backend_limits("hermes_cli").max_concurrency == 1

    def test_malformed_yaml_falls_back_to_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(tmp_path, monkeypatch, "backends:\n  hermes_cli: [unclosed\n")

        assert resolve_backend_limits("hermes_cli").max_concurrency == 1
        assert resolve_backend_limits("hermes_cli").requests_per_minute is None

    def test_non_mapping_yaml_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(tmp_path, monkeypatch, "- just\n- a\n- list\n")

        assert resolve_backend_limits("hermes_cli").requests_per_minute is None


class TestResolutionPrecedence:
    """env override > config file > registry > default."""

    def test_env_rpm_beats_config_rpm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(
            tmp_path, monkeypatch, "backends:\n  hermes_cli:\n    requests_per_minute: 2\n"
        )
        monkeypatch.setenv("OUROBOROS_HERMES_CLI_RPM", "9")

        assert resolve_backend_limits("hermes_cli").requests_per_minute == 9

    def test_env_max_concurrency_beats_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(tmp_path, monkeypatch, "backends:\n  hermes_cli:\n    max_concurrency: 2\n")
        monkeypatch.setenv(MAX_CONCURRENCY_ENV, "6")

        assert resolve_backend_limits("hermes_cli").max_concurrency == 6

    def test_config_beats_registry_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(tmp_path, monkeypatch, "backends:\n  hermes_cli:\n    max_concurrency: 4\n")

        assert resolve_backend_limits("hermes_cli").max_concurrency == 4

    def test_backend_entry_beats_default_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_config(
            tmp_path,
            monkeypatch,
            "default:\n  max_concurrency: 3\nbackends:\n  hermes_cli:\n    max_concurrency: 5\n",
        )

        assert resolve_backend_limits("hermes_cli").max_concurrency == 5
