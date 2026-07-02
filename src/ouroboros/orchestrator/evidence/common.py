"""Common evidence value normalization helpers."""

from __future__ import annotations

_MAX_LEAF_RESULT_CHARS = 1200


def _flatten_evidence_values(value: object) -> tuple[str, ...]:
    """Return concrete string claims from a typed evidence field."""
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, (int, float, bool)):
        return (str(value),)
    if isinstance(value, dict):
        flattened: list[str] = []
        for item in value.values():
            flattened.extend(_flatten_evidence_values(item))
        return tuple(flattened)
    if isinstance(value, (list, tuple, set)):
        flattened_sequence: list[str] = []
        for item in value:
            flattened_sequence.extend(_flatten_evidence_values(item))
        return tuple(flattened_sequence)
    return (str(value),)


def _normalized_evidence_text(text: str) -> str:
    """Normalize transcript/claim text for conservative containment checks."""
    return " ".join(text.lower().split())


def _normalize_command(command: str) -> str:
    """Normalize Bash commands for stable audit output."""
    return " ".join(command.split())


def _normalize_exact_command(command: str) -> str:
    """Normalize command whitespace while preserving case-sensitive exactness."""
    return " ".join(command.split())


def _truncate_text(text: str, limit: int = _MAX_LEAF_RESULT_CHARS) -> str:
    """Truncate long evidence blocks while preserving their beginning."""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + "\n[TRUNCATED]"
