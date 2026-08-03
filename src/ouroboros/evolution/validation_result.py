"""Normalize optional validator results without inventing success evidence."""

from __future__ import annotations

from typing import Any


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
    """Accept only an explicit validator success receipt."""
    return bool(output and output.strip().lower().startswith("validation passed"))


__all__ = ["normalize_validation_result", "validation_passed"]
