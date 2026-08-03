"""Normalize optional validator results without inventing success evidence."""

from __future__ import annotations

import re
from typing import Any

BUILTIN_COLLECTION_ATTEMPT_LIMIT = 3
_TYPED_SUCCESS_RECEIPT = "Validation passed: typed validator returned true"
_COLLECTION_ATTEMPT_RECEIPT = re.compile(
    r"Validation passed \(attempt (?P<attempt>[1-9][0-9]*)/(?P<limit>[1-9][0-9]*)\)"
)
_COLLECTION_REPAIR_RECEIPT = re.compile(
    r"Validation passed after (?P<attempts>[1-9][0-9]*) fix attempts"
)


def normalize_validation_result(result: Any) -> str | None:
    """Preserve missing values so configured validation can fail closed."""
    if result is None or isinstance(result, str):
        return result
    if isinstance(result, bool):
        verdict = "passed" if result else "failed"
        return f"Validation {verdict}: typed validator returned {str(result).lower()}"
    if hasattr(result, "is_ok"):
        if result.is_ok:
            return normalize_validation_result(result.value)
        return f"Validation error: {result.error}"
    return str(result)


def validation_passed(output: str | None) -> bool:
    """Accept only a complete, unambiguous validator success receipt.

    Validator output is persisted as text, so success must use one of the
    closed receipt forms emitted by this module or the built-in collection
    validator.  Prefix matching is deliberately forbidden: it turns
    contradictory or appended error text into success evidence.
    """
    if output == _TYPED_SUCCESS_RECEIPT:
        return True

    attempt_match = _COLLECTION_ATTEMPT_RECEIPT.fullmatch(output or "")
    if attempt_match is not None:
        attempt = int(attempt_match.group("attempt"))
        limit = int(attempt_match.group("limit"))
        return limit == BUILTIN_COLLECTION_ATTEMPT_LIMIT and attempt <= limit

    repair_match = _COLLECTION_REPAIR_RECEIPT.fullmatch(output or "")
    return bool(
        repair_match and int(repair_match.group("attempts")) == BUILTIN_COLLECTION_ATTEMPT_LIMIT
    )


__all__ = [
    "BUILTIN_COLLECTION_ATTEMPT_LIMIT",
    "normalize_validation_result",
    "validation_passed",
]
