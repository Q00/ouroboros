"""Explicit failures for runtime-local model requests."""

from typing import Any


class UnsupportedModelOverrideError(ValueError):
    """A requested model cannot be safely combined with this runtime state."""


def safe_retry_metadata(error: Exception, metadata: dict[str, Any]) -> dict[str, Any]:
    """Do not recommend an identical resume retry for an unsupported request."""
    return {} if isinstance(error, UnsupportedModelOverrideError) else metadata
