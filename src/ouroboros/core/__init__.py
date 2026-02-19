"""Ouroboros core module - shared types, errors, and protocols."""

from ouroboros.core.context import (
    CompressionResult,
    ContextMetrics,
    FilteredContext,
    WorkflowContext,
    compress_context,
    compress_context_with_llm,
    count_context_tokens,
    count_tokens,
    create_filtered_context,
    get_context_metrics,
)
from ouroboros.core.errors import (
    ConfigError,
    OuroborosError,
    PersistenceError,
    ProviderError,
    ValidationError,
)
from ouroboros.core.security import (
    InputValidator,
    mask_api_key,
    sanitize_for_logging,
    validate_api_key_format,
)
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import CostUnits, DriftScore, EventPayload, Result

__all__ = [
    # Types
    "Result",
    "EventPayload",
    "CostUnits",
    "DriftScore",
    # Errors
    "OuroborosError",
    "ProviderError",
    "ConfigError",
    "PersistenceError",
    "ValidationError",
    # Seed (Immutable Specification)
    "Seed",
    "SeedMetadata",
    "OntologySchema",
    "OntologyField",
    "EvaluationPrinciple",
    "ExitCondition",
    # Context Management
    "WorkflowContext",
    "ContextMetrics",
    "CompressionResult",
    "FilteredContext",
    "count_tokens",
    "count_context_tokens",
    "get_context_metrics",
    "compress_context",
    "compress_context_with_llm",
    "create_filtered_context",
    # Security utilities
    "InputValidator",
    "mask_api_key",
    "validate_api_key_format",
    "sanitize_for_logging",
]
