"""Strict versioned schema for reference-only disposable manifests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from ouroboros.core.disposable_memory import (
    ARTIFACT_REF_PATTERN,
    DISPOSABLE_CONTRACT_ID_PATTERN,
)

MANIFEST_MAX_BYTES: Final[int] = 64 * 1024
_MANIFEST_FIELDS: Final[frozenset[str]] = frozenset(
    {"schema_version", "contract_id", "active", "retain_until", "updated_at", "events"}
)
_EVENT_FIELDS: Final[dict[str, frozenset[str]]] = {
    "artifact.referenced": frozenset(
        {"type", "timestamp", "artifact_ref", "size_bytes", "envelope"}
    ),
    "artifact.tombstoned": frozenset({"type", "timestamp", "artifact_ref", "reason"}),
}


def require_fields(raw: dict[str, Any]) -> None:
    """Reject missing or unknown manifest/event fields for schema version 1."""
    actual = frozenset(raw)
    if actual != _MANIFEST_FIELDS:
        raise ValueError(
            f"manifest fields differ: missing={sorted(_MANIFEST_FIELDS - actual)}, "
            f"unknown={sorted(actual - _MANIFEST_FIELDS)}"
        )
    for index, event in enumerate(raw["events"] if isinstance(raw.get("events"), list) else ()):
        if not isinstance(event, dict):
            continue
        expected = _EVENT_FIELDS.get(event.get("type"))
        if expected is None:
            continue
        actual_event = frozenset(event)
        if actual_event != expected:
            raise ValueError(
                f"event {index} fields differ: missing={sorted(expected - actual_event)}, "
                f"unknown={sorted(actual_event - expected)}"
            )
        if event.get("type") == "artifact.tombstoned" and not isinstance(event.get("reason"), str):
            raise ValueError(f"event {index} tombstone reason must be a string")
    for field in ("retain_until", "updated_at"):
        value = raw[field]
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"manifest {field} must be an ISO-8601 string or null")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"manifest {field} must be valid ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"manifest {field} must include a timezone")


def validate_contract_id(contract_id: str) -> str:
    """Return one path-safe disposable contract identity."""
    if not DISPOSABLE_CONTRACT_ID_PATTERN.fullmatch(contract_id):
        raise ValueError(
            "contract_id must be 1-128 path-safe ASCII characters beginning with alphanumeric"
        )
    return contract_id


def digest_from_ref(artifact_ref: str) -> str:
    """Extract a validated lowercase SHA-256 digest."""
    if not ARTIFACT_REF_PATTERN.fullmatch(artifact_ref):
        raise ValueError("invalid artifact_ref")
    return artifact_ref.removeprefix("sha256:")


def as_utc(value: datetime) -> datetime:
    """Normalize one timezone-aware timestamp to UTC."""
    if value.tzinfo is None:
        raise ValueError("datetime values must include a timezone")
    return value.astimezone(UTC)


__all__ = [
    "MANIFEST_MAX_BYTES",
    "as_utc",
    "digest_from_ref",
    "require_fields",
    "validate_contract_id",
]
