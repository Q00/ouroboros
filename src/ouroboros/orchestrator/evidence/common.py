"""Common evidence value normalization helpers."""

from __future__ import annotations

from collections.abc import Mapping
import math

_MAX_LEAF_RESULT_CHARS = 1200


def finite_number(value: object) -> float | None:
    """Return a finite numeric value, rejecting booleans and malformed inputs."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def strict_bool(value: object) -> bool | None:
    """Return a real boolean without applying truthiness coercion."""
    return value if isinstance(value, bool) else None


def event_type(event: object) -> str | None:
    """Read an event type from mapping-style or object-style events."""
    if isinstance(event, Mapping):
        value = event.get("type") or event.get("event_type")
    else:
        value = getattr(event, "type", None) or getattr(event, "event_type", None)
    return value if isinstance(value, str) else None


def event_data(event: object) -> Mapping[str, object]:
    """Read an event payload from mapping-style or object-style events."""
    if isinstance(event, Mapping):
        data = event.get("data") or event.get("payload") or {}
    else:
        data = getattr(event, "data", None) or getattr(event, "payload", None) or {}
    return data if isinstance(data, Mapping) else {}


def event_id(event: object) -> str | None:
    """Read a non-empty event identifier from mapping- or object-style events."""
    value = event.get("id") if isinstance(event, Mapping) else getattr(event, "id", None)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def execution_run_anchor(data: Mapping[str, object]) -> str | None:
    """Return the persisted run anchor shared by execution evidence events."""
    run = data.get("seed_run_id") or data.get("execution_id")
    return str(run) if run is not None else None


def parse_retry_attempt(data: Mapping[str, object]) -> int | None:
    """Parse the required zero-based retry identity without defaulting missing data."""
    if "retry_attempt" not in data:
        return None
    value = data.get("retry_attempt")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_root_ac_index(data: Mapping[str, object]) -> int | None:
    """Return the first valid zero-based root AC index from the evidence aliases."""
    for key in ("root_ac_index", "parent_ac_index", "ac_index"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


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
