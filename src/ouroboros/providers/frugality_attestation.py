"""Secret-safe completion configuration receipts for frugality proofs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ouroboros.providers.base import CompletionConfig
from ouroboros.providers.credential_authority import credential_authority_digest
from ouroboros.providers.profiles import ResolvedCompletionProfile


@dataclass(frozen=True, slots=True)
class PreparedFrugalityCompletion:
    """One profile-sealed request and its pre-dispatch evidence contract."""

    config: CompletionConfig
    contract: Mapping[str, object]
    dispatch: object | None = None


def effective_completion_request(
    resolved: ResolvedCompletionProfile,
    *,
    provider_model: str,
) -> dict[str, object]:
    """Return every effect-bearing field after task-profile resolution."""
    config = resolved.config
    return {
        "model": provider_model,
        "profile_model": config.model,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "stop": config.stop,
        "top_p": config.top_p,
        "response_format": config.response_format,
        "role": config.role,
        "profile": config.profile,
        "profile_name": resolved.profile_name,
        "backend_profile": resolved.backend_profile,
        "max_turns": config.max_turns,
        "reasoning_effort": config.reasoning_effort,
        "model_is_explicit": config.model_is_explicit,
    }


__all__ = [
    "PreparedFrugalityCompletion",
    "credential_authority_digest",
    "effective_completion_request",
]
