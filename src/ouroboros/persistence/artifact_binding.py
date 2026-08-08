"""Immutable contract-to-artifact binding records.

Mutable retention manifests are not sufficient authority for replay or GC: a
schema-valid replacement could otherwise redirect a contract to another
contract's existing blob.  These records are published once at a deterministic
contract-derived path and survive manifest tombstones.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

BINDING_MAX_BYTES: Final[int] = 8 * 1024
BINDING_VERSION: Final[int] = 1
BINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {"schema_version", "contract_id", "artifact_ref", "size_bytes", "envelope"}
)


def binding_path(root: Path, contract_id: str) -> Path:
    """Return the non-enumerating deterministic path for one contract."""
    digest = hashlib.sha256(contract_id.encode("utf-8")).hexdigest()
    return root / f"{digest}.json"


def binding_record(event: dict[str, Any], *, contract_id: str) -> dict[str, Any]:
    """Build the exact immutable subset of one validated reference event."""
    return {
        "schema_version": BINDING_VERSION,
        "contract_id": contract_id,
        "artifact_ref": event.get("artifact_ref"),
        "size_bytes": event.get("size_bytes"),
        "envelope": event.get("envelope"),
    }


def encode_binding(event: dict[str, Any], *, contract_id: str) -> bytes:
    """Encode one binding canonically so identical retries compare byte-for-byte."""
    return (
        json.dumps(binding_record(event, contract_id=contract_id), sort_keys=True) + "\n"
    ).encode("utf-8")


def validate_binding(
    raw: Any,
    manifest: dict[str, Any],
    *,
    contract_id: str,
) -> None:
    """Require a manifest's sole reference and tombstone to match its binding."""
    references = [
        event for event in manifest["events"] if event.get("type") == "artifact.referenced"
    ]
    if len(references) != 1:
        raise ValueError("manifest must contain exactly one artifact reference")
    if not isinstance(raw, dict) or frozenset(raw) != BINDING_FIELDS:
        raise ValueError("binding fields do not match the exact versioned schema")
    expected = binding_record(references[0], contract_id=contract_id)
    if raw != expected:
        raise ValueError("binding does not match the contract manifest")
    tombstones = [
        event for event in manifest["events"] if event.get("type") == "artifact.tombstoned"
    ]
    if len(tombstones) > 1 or any(
        event.get("artifact_ref") != expected["artifact_ref"] for event in tombstones
    ):
        raise ValueError("manifest tombstone does not match the durable binding")


__all__ = [
    "BINDING_MAX_BYTES",
    "binding_path",
    "encode_binding",
    "validate_binding",
]
