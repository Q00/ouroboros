"""Compatibility exports for provider transient-error classification."""

from __future__ import annotations

from ouroboros.core.retry import BASE_TRANSIENT_PATTERNS, is_transient_error

TRANSIENT_ERROR_PATTERNS = BASE_TRANSIENT_PATTERNS

__all__ = [
    "BASE_TRANSIENT_PATTERNS",
    "TRANSIENT_ERROR_PATTERNS",
    "is_transient_error",
]
