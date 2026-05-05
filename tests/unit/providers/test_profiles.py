"""Unit tests for provider-neutral LLM profile resolution."""

from unittest.mock import patch

import pytest

from ouroboros.config.models import OuroborosConfig
from ouroboros.core.errors import ConfigError, ProviderError
from ouroboros.providers.base import CompletionConfig
from ouroboros.providers.profiles import (
    resolve_completion_profile,
    resolve_completion_profile_result,
)


def test_resolve_completion_profile_uses_codex_backend_profile() -> None:
    """Codex can map an Ouroboros task profile to a Codex CLI profile."""
    config = OuroborosConfig(
        llm_profiles={
            "fast": {
                "temperature": 0.2,
                "max_turns": 2,
                "providers": {
                    "codex": {
                        "profile": "ouroboros-fast",
                        "model": "gpt-5.3-codex-spark",
                        "max_turns": 1,
                    },
                },
            },
        },
        llm_role_profiles={"qa": "fast"},
    )
    request = CompletionConfig(model="default", role="qa", temperature=0.7)

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        resolved = resolve_completion_profile(request, backend="codex")

    assert resolved.profile_name == "fast"
    assert resolved.backend_profile == "ouroboros-fast"
    assert resolved.config.model == "gpt-5.3-codex-spark"
    assert resolved.config.temperature == 0.7
    assert resolved.config.max_turns == 1


def test_resolve_completion_profile_uses_provider_aliases() -> None:
    """Provider aliases let OpenRouter mappings apply to the LiteLLM backend."""
    config = OuroborosConfig(
        llm_profiles={
            "deep": {
                "temperature": 0.4,
                "providers": {
                    "openrouter": {
                        "model": "openrouter/anthropic/claude-opus-4-6",
                        "max_tokens": 8192,
                    },
                },
            },
        },
        llm_role_profiles={"semantic_evaluation": "deep"},
    )
    request = CompletionConfig(model="default", role="semantic_evaluation")

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        resolved = resolve_completion_profile(request, backend="litellm")

    assert resolved.backend_profile is None
    assert resolved.config.model == "openrouter/anthropic/claude-opus-4-6"
    assert resolved.config.temperature == 0.7
    assert resolved.config.max_tokens == 4096


def test_resolve_completion_profile_explicit_profile_overrides_role() -> None:
    """Explicit per-request profile wins over role mapping."""
    config = OuroborosConfig(
        llm_profiles={
            "fast": {"model": "fast-model"},
            "deep": {"model": "deep-model"},
        },
        llm_role_profiles={"qa": "fast"},
    )
    request = CompletionConfig(model="fallback", role="qa", profile="deep")

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        resolved = resolve_completion_profile(request, backend="litellm")

    assert resolved.profile_name == "deep"
    assert resolved.config.model == "deep-model"


def test_resolve_completion_profile_preserves_explicit_role_model() -> None:
    """Role mappings should not replace an explicit request-level model."""
    config = OuroborosConfig(
        llm_profiles={
            "fast": {
                "model": "profile-model",
                "temperature": 0.2,
            },
        },
        llm_role_profiles={"qa": "fast"},
    )
    request = CompletionConfig(
        model="request-model",
        role="qa",
        model_is_explicit=True,
        temperature=0.7,
    )

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        resolved = resolve_completion_profile(request, backend="litellm")

    assert resolved.profile_name == "fast"
    assert resolved.config.model == "request-model"
    assert resolved.config.temperature == 0.7


def test_resolve_completion_profile_suppresses_backend_profile_for_explicit_model() -> None:
    """Codex native profiles must not shadow explicit request model pins."""
    config = OuroborosConfig(
        llm_profiles={
            "fast": {
                "providers": {"codex": {"profile": "ouroboros-fast"}},
            },
        },
        llm_role_profiles={"atomicity": "fast"},
    )
    request = CompletionConfig(
        model="custom-codex-model",
        role="atomicity",
        model_is_explicit=True,
    )

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        resolved = resolve_completion_profile(request, backend="codex")

    assert resolved.profile_name == "fast"
    assert resolved.backend_profile is None
    assert resolved.config.model == "custom-codex-model"


def test_resolve_completion_profile_replaces_implicit_legacy_model() -> None:
    """Role profiles should replace helper/config defaults that are not request pins."""
    config = OuroborosConfig(
        llm_profiles={"fast": {"model": "profile-model"}},
        llm_role_profiles={"qa": "fast"},
    )
    request = CompletionConfig(model="legacy-helper-default", role="qa")

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        resolved = resolve_completion_profile(request, backend="litellm")

    assert resolved.profile_name == "fast"
    assert resolved.config.model == "profile-model"


def test_resolve_completion_profile_ignores_explicit_flag_for_sentinel_models() -> None:
    """Empty/default model sentinels are not request-level pins even with explicit flags."""
    config = OuroborosConfig(
        llm_profiles={"deep": {"model": "profile-model"}},
        llm_role_profiles={"consensus_perspective": "deep"},
    )

    for sentinel in ("", "default"):
        request = CompletionConfig(
            model=sentinel,
            role="consensus_perspective",
            model_is_explicit=True,
        )
        with patch("ouroboros.providers.profiles.load_config", return_value=config):
            resolved = resolve_completion_profile(request, backend="litellm")

        assert resolved.profile_name == "deep"
        assert resolved.config.model == "profile-model"


def test_resolve_completion_profile_resolves_empty_role_model() -> None:
    """Empty request models are adapter-default sentinels, not explicit overrides."""
    config = OuroborosConfig(
        llm_profiles={"deep": {"model": "profile-model"}},
        llm_role_profiles={"consensus_perspective": "deep"},
    )
    request = CompletionConfig(model="", role="consensus_perspective")

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        resolved = resolve_completion_profile(request, backend="litellm")

    assert resolved.profile_name == "deep"
    assert resolved.config.model == "profile-model"


def test_resolve_completion_profile_preserves_role_request_sampling_settings() -> None:
    """Role mappings should route models without changing tuned request behavior."""
    config = OuroborosConfig(
        llm_profiles={
            "fast": {
                "model": "profile-model",
                "temperature": 0.2,
                "max_tokens": 1024,
                "top_p": 0.5,
            },
        },
        llm_role_profiles={"brownfield_explore": "fast"},
    )
    request = CompletionConfig(
        model="legacy-helper-default",
        role="brownfield_explore",
        temperature=0.0,
        max_tokens=60,
        top_p=0.9,
    )

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        resolved = resolve_completion_profile(request, backend="litellm")

    assert resolved.profile_name == "fast"
    assert resolved.config.model == "profile-model"
    assert resolved.config.temperature == 0.0
    assert resolved.config.max_tokens == 60
    assert resolved.config.top_p == 0.9


def test_resolve_completion_profile_applies_explicit_profile_sampling_settings() -> None:
    """Explicit profile selection opts into the profile's full tuning envelope."""
    config = OuroborosConfig(
        llm_profiles={
            "deep": {
                "model": "profile-model",
                "temperature": 0.3,
                "max_tokens": 8192,
                "top_p": 0.8,
            },
        },
    )
    request = CompletionConfig(
        model="fallback",
        profile="deep",
        temperature=0.7,
        max_tokens=4096,
        top_p=1.0,
    )

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        resolved = resolve_completion_profile(request, backend="litellm")

    assert resolved.profile_name == "deep"
    assert resolved.config.model == "profile-model"
    assert resolved.config.temperature == 0.3
    assert resolved.config.max_tokens == 8192
    assert resolved.config.top_p == 0.8


def test_resolve_completion_profile_rejects_missing_explicit_profile() -> None:
    """Explicit profile names are strict and should fail fast when misspelled."""
    config = OuroborosConfig(llm_profiles={})
    request = CompletionConfig(model="fallback", profile="typo")

    with (
        patch("ouroboros.providers.profiles.load_config", return_value=config),
        pytest.raises(ConfigError, match="LLM profile 'typo'.*not defined"),
    ):
        resolve_completion_profile(request, backend="codex")


def test_resolve_completion_profile_rejects_missing_role_profile() -> None:
    """Role mappings should not silently fall back when they point to deleted profiles."""
    config = OuroborosConfig(llm_profiles={}, llm_role_profiles={"qa": "deleted"})
    request = CompletionConfig(model="default", role="qa")

    with (
        patch("ouroboros.providers.profiles.load_config", return_value=config),
        pytest.raises(ConfigError, match="LLM profile 'deleted'.*not defined"),
    ):
        resolve_completion_profile(request, backend="codex")


def test_resolve_completion_profile_rejects_explicit_profile_without_config() -> None:
    """An explicit profile request requires loadable profile configuration."""
    request = CompletionConfig(model="fallback", profile="fast")

    with (
        patch(
            "ouroboros.providers.profiles.load_config",
            side_effect=ConfigError("missing config"),
        ),
        pytest.raises(ConfigError, match="profile 'fast'.*config could not be loaded"),
    ):
        resolve_completion_profile(request, backend="codex")


def test_resolve_completion_profile_preserves_model_when_role_unmapped() -> None:
    """A role with no configured mapping remains a backwards-compatible fallback."""
    config = OuroborosConfig(llm_profiles={}, llm_role_profiles={})
    request = CompletionConfig(model="fallback", role="qa")

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        resolved = resolve_completion_profile(request, backend="codex")

    assert resolved.config is request
    assert resolved.profile_name is None
    assert resolved.backend_profile is None


def test_resolve_completion_profile_falls_back_when_config_missing() -> None:
    """Missing user config preserves existing model behavior."""
    request = CompletionConfig(model="fallback", role="qa", temperature=0.1)

    with patch(
        "ouroboros.providers.profiles.load_config",
        side_effect=ConfigError("missing config"),
    ):
        resolved = resolve_completion_profile(request, backend="codex")

    assert resolved.config is request
    assert resolved.profile_name is None
    assert resolved.backend_profile is None


def test_resolve_completion_profile_skips_config_load_without_role_or_profile() -> None:
    """Unprofiled requests preserve existing behavior without config I/O."""
    request = CompletionConfig(model="fallback")

    with patch("ouroboros.providers.profiles.load_config") as mock_load_config:
        resolved = resolve_completion_profile(request, backend="codex")

    mock_load_config.assert_not_called()
    assert resolved.config is request


def test_resolve_completion_profile_result_converts_config_error() -> None:
    """Adapter-facing wrapper should convert bad profile config to ProviderError."""
    request = CompletionConfig(model="gpt-4", profile="missing-profile")

    with patch("ouroboros.providers.profiles.load_config", side_effect=ConfigError("bad config")):
        result = resolve_completion_profile_result(request, backend="litellm")

    assert result.is_err
    assert isinstance(result.error, ProviderError)
    assert result.error.provider == "litellm"
    assert "Invalid LLM profile configuration" in result.error.message
