"""Provider-neutral LLM task profile resolution."""

from __future__ import annotations

from dataclasses import dataclass, replace

from ouroboros.config.loader import ConfigError, load_config
from ouroboros.config.models import LLMProviderProfileConfig, LLMTaskProfileConfig
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionConfig

_BACKEND_ALIASES = {
    "anthropic": "anthropic",
    "anthropic_api": "anthropic",
    "claude": "claude_code",
    "claude_code": "claude_code",
    "codex": "codex",
    "codex_cli": "codex",
    "gemini": "gemini",
    "gemini_cli": "gemini",
    "opencode": "opencode",
    "opencode_cli": "opencode",
    "litellm": "litellm",
    "openai": "litellm",
    "openrouter": "litellm",
}


@dataclass(frozen=True, slots=True)
class ResolvedCompletionProfile:
    """Completion config plus backend-native profile metadata."""

    config: CompletionConfig
    profile_name: str | None = None
    backend_profile: str | None = None


def _normalize_backend(backend: str) -> str:
    return _BACKEND_ALIASES.get(backend.strip().lower(), backend.strip().lower())


def _provider_config(
    profile: LLMTaskProfileConfig,
    backend: str,
) -> LLMProviderProfileConfig | None:
    providers = profile.providers
    if backend in providers:
        return providers[backend]

    for key, value in providers.items():
        if _normalize_backend(key) == backend:
            return value

    return None


def _coalesce[T](specific: T | None, general: T | None, fallback: T) -> T:
    if specific is not None:
        return specific
    if general is not None:
        return general
    return fallback


def _has_request_model_override(config: CompletionConfig) -> bool:
    """Return True when a role-based profile should preserve the request model."""
    model = config.model.strip()
    return bool(
        config.role
        and not config.profile
        and config.model_is_explicit
        and model
        and model != "default"
    )


def resolve_completion_profile(
    config: CompletionConfig,
    *,
    backend: str,
) -> ResolvedCompletionProfile:
    """Resolve an Ouroboros LLM task profile for a backend.

    Existing callers keep their configured ``model`` behavior unless they pass
    ``CompletionConfig.role`` or ``CompletionConfig.profile`` and that value
    resolves through ``llm_role_profiles`` / ``llm_profiles``.
    """
    if not config.profile and not config.role:
        return ResolvedCompletionProfile(config=config)

    try:
        ouroboros_config = load_config()
    except ConfigError as exc:
        if config.profile:
            msg = f"LLM profile {config.profile!r} was requested, but Ouroboros config could not be loaded"
            raise ConfigError(msg, config_key="llm_profiles") from exc
        return ResolvedCompletionProfile(config=config)

    profile_name = config.profile
    if not profile_name and config.role:
        profile_name = ouroboros_config.llm_role_profiles.get(config.role)
    if not profile_name:
        return ResolvedCompletionProfile(config=config)

    profile = ouroboros_config.llm_profiles.get(profile_name)
    if profile is None:
        source = (
            f"role {config.role!r}" if config.role and not config.profile else "explicit request"
        )
        msg = f"LLM profile {profile_name!r} referenced by {source} is not defined"
        raise ConfigError(msg, config_key=f"llm_profiles.{profile_name}")

    normalized_backend = _normalize_backend(backend)
    provider = _provider_config(profile, normalized_backend)
    model_override = _has_request_model_override(config)
    explicit_profile = config.profile is not None

    effective = replace(
        config,
        model=config.model
        if model_override
        else _coalesce(
            provider.model if provider is not None else None,
            profile.model,
            config.model,
        ),
        temperature=_coalesce(
            provider.temperature if provider is not None else None,
            profile.temperature,
            config.temperature,
        )
        if explicit_profile
        else config.temperature,
        max_tokens=_coalesce(
            provider.max_tokens if provider is not None else None,
            profile.max_tokens,
            config.max_tokens,
        )
        if explicit_profile
        else config.max_tokens,
        top_p=_coalesce(
            provider.top_p if provider is not None else None,
            profile.top_p,
            config.top_p,
        )
        if explicit_profile
        else config.top_p,
        max_turns=_coalesce(
            provider.max_turns if provider is not None else None,
            profile.max_turns,
            config.max_turns,
        ),
    )

    return ResolvedCompletionProfile(
        config=effective,
        profile_name=profile_name,
        backend_profile=None
        if model_override
        else provider.profile
        if provider is not None
        else None,
    )


def resolve_completion_profile_result(
    config: CompletionConfig,
    *,
    backend: str,
) -> Result[ResolvedCompletionProfile, ProviderError]:
    """Resolve a completion profile without leaking ConfigError from adapters."""
    try:
        return Result.ok(resolve_completion_profile(config, backend=backend))
    except ConfigError as exc:
        return Result.err(
            ProviderError(
                message=f"Invalid LLM profile configuration: {exc.message}",
                provider=backend,
                details={
                    "original_exception": type(exc).__name__,
                    "config_key": getattr(exc, "config_key", None),
                    "config_file": str(getattr(exc, "config_file", "") or ""),
                },
            )
        )


__all__ = [
    "ResolvedCompletionProfile",
    "resolve_completion_profile",
    "resolve_completion_profile_result",
]
