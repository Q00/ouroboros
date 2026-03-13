"""Ouroboros core module - shared types, errors, and protocols.

This package uses lazy re-exports so importing submodules such as
`ouroboros.core.errors` does not eagerly import heavier modules like
`ouroboros.core.context` and create circular import chains during CLI startup.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    # Types
    "Result": ("ouroboros.core.types", "Result"),
    "EventPayload": ("ouroboros.core.types", "EventPayload"),
    "CostUnits": ("ouroboros.core.types", "CostUnits"),
    "DriftScore": ("ouroboros.core.types", "DriftScore"),
    # Errors
    "OuroborosError": ("ouroboros.core.errors", "OuroborosError"),
    "ProviderError": ("ouroboros.core.errors", "ProviderError"),
    "ConfigError": ("ouroboros.core.errors", "ConfigError"),
    "PersistenceError": ("ouroboros.core.errors", "PersistenceError"),
    "ValidationError": ("ouroboros.core.errors", "ValidationError"),
    # Seed
    "Seed": ("ouroboros.core.seed", "Seed"),
    "SeedMetadata": ("ouroboros.core.seed", "SeedMetadata"),
    "OntologySchema": ("ouroboros.core.seed", "OntologySchema"),
    "OntologyField": ("ouroboros.core.seed", "OntologyField"),
    "EvaluationPrinciple": ("ouroboros.core.seed", "EvaluationPrinciple"),
    "ExitCondition": ("ouroboros.core.seed", "ExitCondition"),
    # Context management
    "WorkflowContext": ("ouroboros.core.context", "WorkflowContext"),
    "ContextMetrics": ("ouroboros.core.context", "ContextMetrics"),
    "CompressionResult": ("ouroboros.core.context", "CompressionResult"),
    "FilteredContext": ("ouroboros.core.context", "FilteredContext"),
    "count_tokens": ("ouroboros.core.context", "count_tokens"),
    "count_context_tokens": ("ouroboros.core.context", "count_context_tokens"),
    "get_context_metrics": ("ouroboros.core.context", "get_context_metrics"),
    "compress_context": ("ouroboros.core.context", "compress_context"),
    "compress_context_with_llm": ("ouroboros.core.context", "compress_context_with_llm"),
    "create_filtered_context": ("ouroboros.core.context", "create_filtered_context"),
    # Git workflow
    "GitWorkflowConfig": ("ouroboros.core.git_workflow", "GitWorkflowConfig"),
    "detect_git_workflow": ("ouroboros.core.git_workflow", "detect_git_workflow"),
    "is_on_protected_branch": ("ouroboros.core.git_workflow", "is_on_protected_branch"),
    # Security utilities
    "InputValidator": ("ouroboros.core.security", "InputValidator"),
    "mask_api_key": ("ouroboros.core.security", "mask_api_key"),
    "validate_api_key_format": ("ouroboros.core.security", "validate_api_key_format"),
    "sanitize_for_logging": ("ouroboros.core.security", "sanitize_for_logging"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Lazily import shared core symbols on first access."""
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module 'ouroboros.core' has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose lazy exports to interactive tooling."""
    return sorted(set(globals()) | set(__all__))
