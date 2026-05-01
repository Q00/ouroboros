"""Unit tests for provider-neutral LLM profile resolution."""

from unittest.mock import patch

from ouroboros.config.models import OuroborosConfig
from ouroboros.core.errors import ConfigError
from ouroboros.providers.base import CompletionConfig
from ouroboros.providers.profiles import resolve_completion_profile


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
    request = CompletionConfig(model="fallback", role="qa", temperature=0.7)

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        resolved = resolve_completion_profile(request, backend="codex")

    assert resolved.profile_name == "fast"
    assert resolved.backend_profile == "ouroboros-fast"
    assert resolved.config.model == "gpt-5.3-codex-spark"
    assert resolved.config.temperature == 0.2
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
    request = CompletionConfig(model="fallback", role="semantic_evaluation")

    with patch("ouroboros.providers.profiles.load_config", return_value=config):
        resolved = resolve_completion_profile(request, backend="litellm")

    assert resolved.backend_profile is None
    assert resolved.config.model == "openrouter/anthropic/claude-opus-4-6"
    assert resolved.config.temperature == 0.4
    assert resolved.config.max_tokens == 8192


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
