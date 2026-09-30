"""Setup helpers for classifying shared model configuration."""

from __future__ import annotations

from ouroboros.config._model_defaults import (
    DEFAULT_CONSENSUS_OPUS_MODEL,
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
    recognized_shipped_defaults,
)
from ouroboros.config.model_selection import AUTO_MODEL

_DEFAULT_CONSENSUS_MODELS = (
    "openrouter/openai/gpt-4o",
    DEFAULT_CONSENSUS_OPUS_MODEL,
    "openrouter/google/gemini-2.5-pro",
)
_MISSING = object()

_CODEX_ROLE_MODEL_OVERRIDE_DEFAULTS: dict[str, tuple[tuple[tuple[str, ...], object], ...]] = {
    "ambiguity": ((("clarification", "default_model"), DEFAULT_OPUS_MODEL),),
    "assertion_extraction": ((("evaluation", "assertion_extraction_model"), DEFAULT_SONNET_MODEL),),
    "brownfield_explore": ((("clarification", "default_model"), DEFAULT_OPUS_MODEL),),
    "clarification": ((("clarification", "default_model"), DEFAULT_OPUS_MODEL),),
    "consensus_advocate": ((("consensus", "advocate_model"), DEFAULT_CONSENSUS_OPUS_MODEL),),
    "consensus_judge": ((("consensus", "judge_model"), "openrouter/google/gemini-2.5-pro"),),
    "consensus_vote": ((("consensus", "models"), _DEFAULT_CONSENSUS_MODELS),),
    "context_compression": ((("llm", "context_compression_model"), "gpt-4"),),
    "dependency_analysis": ((("llm", "dependency_analysis_model"), DEFAULT_SONNET_MODEL),),
    "mechanical_detection": ((("evaluation", "assertion_extraction_model"), DEFAULT_SONNET_MODEL),),
    "ontology_analysis": (
        (("llm", "ontology_analysis_model"), DEFAULT_SONNET_MODEL),
        (("consensus", "devil_model"), "openrouter/openai/gpt-4o"),
    ),
    "pm_interview": ((("clarification", "default_model"), DEFAULT_OPUS_MODEL),),
    "qa": ((("llm", "qa_model"), DEFAULT_SONNET_MODEL),),
    "reflect": ((("resilience", "reflect_model"), DEFAULT_OPUS_MODEL),),
    "seed_generation": ((("clarification", "default_model"), DEFAULT_OPUS_MODEL),),
    "semantic_evaluation": ((("evaluation", "semantic_model"), DEFAULT_OPUS_MODEL),),
    "wonder": ((("resilience", "wonder_model"), DEFAULT_OPUS_MODEL),),
}


def is_shipped_default_roster(
    current: list | tuple,
    shipped_roster: tuple[str, ...],
) -> bool:
    """Return whether a model roster matches current or legacy shipped defaults."""
    current_tuple = tuple(current)
    if current_tuple == (AUTO_MODEL,):
        return True
    if len(current_tuple) != len(shipped_roster):
        return False
    return all(
        str(candidate) in recognized_shipped_defaults(default)
        for candidate, default in zip(current_tuple, shipped_roster, strict=True)
    )


def _get_nested_value(config_dict: dict, path: tuple[str, ...]) -> object:
    current: object = config_dict
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def has_explicit_codex_model_override(config_dict: dict, role: str) -> bool:
    """Return True only when an existing legacy setting is a real user pin."""
    for path, default in _CODEX_ROLE_MODEL_OVERRIDE_DEFAULTS.get(role, ()):
        value = _get_nested_value(config_dict, path)
        if value is _MISSING:
            continue
        if isinstance(default, str) and value in (
            AUTO_MODEL,
            *recognized_shipped_defaults(default),
        ):
            continue
        if (
            isinstance(default, tuple)
            and isinstance(value, (list, tuple))
            and is_shipped_default_roster(value, default)
        ):
            continue
        if value != default:
            return True
    return False


__all__ = [
    "has_explicit_codex_model_override",
    "is_shipped_default_roster",
]
