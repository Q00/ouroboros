"""Contract bindings anchored outside the replaceable artifact generation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

BINDING_MAX_BYTES: Final[int] = 8 * 1024
BINDING_VERSION: Final[int] = 2
BINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "contract_id",
        "artifact_ref",
        "size_bytes",
        "envelope",
        "referenced_at",
        "initial_active",
        "initial_retain_until",
    }
)


def _digest(value: str, *, length: int = 64) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def binding_path(root: Path, contract_id: str) -> Path:
    """Return the replaceable store-generation cache path for one contract."""
    return root / f"{_digest(contract_id)}.json"


def authority_prefix(store_root: Path) -> str:
    """Namespace anchors for one artifact store inside its trusted parent."""
    return f".ouroboros-artifact-authority-{_digest(str(store_root), length=24)}-"


def authority_path(parent: Path, store_root: Path, contract_id: str) -> Path:
    """Return one write-once authority path in the stable parent boundary."""
    return parent / f"{authority_prefix(store_root)}{_digest(contract_id)}.json"


def completion_path(anchor: Path) -> Path:
    """Return the stable marker proving initial publication once completed."""
    return anchor.with_suffix(".committed")


def binding_record(
    event: dict[str, Any],
    *,
    contract_id: str,
    active: bool,
    retain_until: str,
) -> dict[str, Any]:
    """Build the complete immutable publication record needed for recovery."""
    return {
        "schema_version": BINDING_VERSION,
        "contract_id": contract_id,
        "artifact_ref": event.get("artifact_ref"),
        "size_bytes": event.get("size_bytes"),
        "envelope": event.get("envelope"),
        "referenced_at": event.get("timestamp"),
        "initial_active": active,
        "initial_retain_until": retain_until,
    }


def encode_record(record: dict[str, Any]) -> bytes:
    """Encode one immutable record for byte-identical retry comparison."""
    return (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")


def completion_payload(record: dict[str, Any]) -> bytes:
    """Bind the completion marker to the exact authority bytes."""
    return (hashlib.sha256(encode_record(record)).hexdigest() + "\n").encode("ascii")


def validate_authority(raw: Any, *, contract_id: str) -> dict[str, Any]:
    """Validate the exact independently anchored record schema."""
    if not isinstance(raw, dict) or frozenset(raw) != BINDING_FIELDS:
        raise ValueError("authority fields do not match the exact versioned schema")
    if raw.get("schema_version") != BINDING_VERSION or raw.get("contract_id") != contract_id:
        raise ValueError("authority identity or schema version is invalid")
    if not isinstance(raw.get("initial_active"), bool):
        raise ValueError("authority initial_active must be boolean")
    for field in ("artifact_ref", "referenced_at", "initial_retain_until"):
        if not isinstance(raw.get(field), str):
            raise ValueError(f"authority {field} must be a string")
    size = raw.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("authority size_bytes must be a non-negative integer")
    if not isinstance(raw.get("envelope"), dict):
        raise ValueError("authority envelope must be an object")
    return raw


def validate_binding(raw: Any, authority: dict[str, Any]) -> None:
    """Require the replaceable cache to equal the stable authority exactly."""
    if raw != authority:
        raise ValueError("binding does not match the independently anchored authority")


def validate_manifest_authority(
    manifest: dict[str, Any],
    authority: dict[str, Any],
) -> None:
    """Require replay and tombstone history to match stable authority."""
    references = [
        event for event in manifest["events"] if event.get("type") == "artifact.referenced"
    ]
    if len(references) != 1:
        raise ValueError("manifest must contain exactly one artifact reference")
    reference = references[0]
    expected = {
        "artifact_ref": authority["artifact_ref"],
        "size_bytes": authority["size_bytes"],
        "envelope": authority["envelope"],
        "timestamp": authority["referenced_at"],
    }
    if any(reference.get(field) != value for field, value in expected.items()):
        raise ValueError("manifest reference does not match independently anchored authority")
    tombstones = [
        event for event in manifest["events"] if event.get("type") == "artifact.tombstoned"
    ]
    if len(tombstones) > 1 or any(
        event.get("artifact_ref") != authority["artifact_ref"] for event in tombstones
    ):
        raise ValueError("manifest tombstone does not match independently anchored authority")


def manifest_from_authority(authority: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct mutable metadata after a partial initial publication."""
    timestamp = authority["referenced_at"]
    return {
        "schema_version": 1,
        "contract_id": authority["contract_id"],
        "active": authority["initial_active"],
        "retain_until": authority["initial_retain_until"],
        "updated_at": timestamp,
        "events": [
            {
                "type": "artifact.referenced",
                "timestamp": timestamp,
                "artifact_ref": authority["artifact_ref"],
                "size_bytes": authority["size_bytes"],
                "envelope": authority["envelope"],
            }
        ],
    }


__all__ = [
    "BINDING_MAX_BYTES",
    "authority_path",
    "authority_prefix",
    "binding_path",
    "binding_record",
    "completion_path",
    "completion_payload",
    "encode_record",
    "manifest_from_authority",
    "validate_authority",
    "validate_binding",
    "validate_manifest_authority",
]
