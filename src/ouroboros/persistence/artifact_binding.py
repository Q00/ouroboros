"""Contract bindings anchored outside the replaceable artifact generation."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from pathlib import Path
from typing import Any, Final, Protocol

from ouroboros.persistence.artifact_errors import ArtifactManifestError
from ouroboros.persistence.artifact_validation import validate_manifest

BINDING_MAX_BYTES: Final[int] = 8 * 1024
BINDING_VERSION: Final[int] = 2
TOMBSTONE_VERSION: Final[int] = 1
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
TOMBSTONE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "contract_id",
        "artifact_ref",
        "timestamp",
        "reason",
        "authority_sha256",
    }
)


class AuthorityStore(Protocol):
    root: Path
    _bindings_root: Path
    _directory_anchor: Path
    _lock_directory_anchor: Path

    def _anchor_path(self, contract_id: str) -> Path: ...

    def _binding_path(self, contract_id: str) -> Path: ...

    def _manifest_path(self, contract_id: str) -> Path: ...

    def _read_authority_locked(self, contract_id: str) -> dict[str, Any] | None: ...

    def _write_record_locked(
        self,
        path: Path,
        payload: bytes,
        *,
        stable: bool,
        authority_check: Callable[[], None],
    ) -> None: ...

    def _write_manifest_locked(
        self,
        contract_id: str,
        manifest: dict[str, Any],
        *,
        authority_check: Callable[[], None] | None = None,
    ) -> None: ...


class BoundedReader(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        max_bytes: int,
        root: Path,
        anchor: Path,
        label: str,
    ) -> bytes: ...


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


def tombstone_path(anchor: Path) -> Path:
    """Return the stable write-once terminal authority for one contract."""
    return anchor.with_suffix(".tombstoned")


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


def tombstone_record(
    event: dict[str, Any],
    authority: dict[str, Any],
) -> dict[str, Any]:
    """Bind terminal lifecycle evidence to the immutable initial authority."""
    return {
        "schema_version": TOMBSTONE_VERSION,
        "contract_id": authority["contract_id"],
        "artifact_ref": authority["artifact_ref"],
        "timestamp": event.get("timestamp"),
        "reason": event.get("reason"),
        "authority_sha256": hashlib.sha256(encode_record(authority)).hexdigest(),
    }


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


def validate_tombstone_authority(
    raw: Any,
    authority: dict[str, Any],
) -> dict[str, Any]:
    """Validate exact monotonic terminal state against initial authority."""
    if not isinstance(raw, dict) or frozenset(raw) != TOMBSTONE_FIELDS:
        raise ValueError("tombstone authority fields do not match the exact schema")
    expected = {
        "schema_version": TOMBSTONE_VERSION,
        "contract_id": authority["contract_id"],
        "artifact_ref": authority["artifact_ref"],
        "authority_sha256": hashlib.sha256(encode_record(authority)).hexdigest(),
    }
    if any(raw.get(field) != value for field, value in expected.items()):
        raise ValueError("tombstone authority does not match initial authority")
    if not isinstance(raw.get("timestamp"), str) or not isinstance(raw.get("reason"), str):
        raise ValueError("tombstone authority timestamp and reason must be strings")
    return raw


def tombstone_event(record: dict[str, Any]) -> dict[str, Any]:
    """Return the exact manifest projection of stable terminal authority."""
    return {
        "type": "artifact.tombstoned",
        "timestamp": record["timestamp"],
        "artifact_ref": record["artifact_ref"],
        "reason": record["reason"],
    }


def validate_manifest_authority(
    manifest: dict[str, Any],
    authority: dict[str, Any],
    terminal: dict[str, Any] | None = None,
) -> bool:
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
    if terminal is None:
        if tombstones:
            raise ValueError("manifest tombstone is missing monotonic terminal authority")
        if manifest["events"] != references:
            raise ValueError("manifest history does not match initial authority")
        return False
    expected_tombstone = tombstone_event(terminal)
    if tombstones:
        if tombstones != [expected_tombstone] or manifest["events"] != [
            reference,
            expected_tombstone,
        ]:
            raise ValueError("manifest tombstone does not match terminal authority")
        return False
    if manifest["events"] != references:
        raise ValueError("manifest history does not match terminal authority")
    manifest["events"].append(expected_tombstone)
    manifest["updated_at"] = terminal["timestamp"]
    return True


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


def reconcile_contract_authority(
    store: AuthorityStore,
    contract_id: str,
    manifest: dict[str, Any],
    *,
    authority_check: Callable[[], None],
    read_bounded: BoundedReader,
) -> dict[str, Any]:
    """Reconcile replaceable metadata with monotonic stable authorities."""
    anchor = store._read_authority_locked(contract_id)
    binding = store._binding_path(contract_id)
    if anchor is None:
        if manifest["events"] or binding.exists():
            raise ArtifactManifestError(
                "Contract metadata is missing independently anchored authority",
                operation="read",
                details={"contract_id": contract_id},
            )
        return manifest
    anchor_path = store._anchor_path(contract_id)
    marker = completion_path(anchor_path)
    terminal_path = tombstone_path(anchor_path)
    try:
        terminal = validate_tombstone_authority(
            json.loads(
                read_bounded(
                    terminal_path,
                    max_bytes=BINDING_MAX_BYTES,
                    root=store.root.parent,
                    anchor=store._lock_directory_anchor,
                    label="artifact tombstone authority",
                )
            ),
            anchor,
        )
    except FileNotFoundError:
        terminal = None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ArtifactManifestError(
            "Artifact tombstone authority is invalid",
            operation="read",
            details={"contract_id": contract_id, "path": str(terminal_path)},
        ) from exc
    if terminal is not None and not marker.exists():
        raise ArtifactManifestError(
            "Terminal authority requires completed initial publication",
            operation="read",
            details={"contract_id": contract_id},
        )
    recovering = not manifest["events"]
    terminal_recovered = False
    if recovering:
        if marker.exists():
            raise ArtifactManifestError(
                "Committed contract manifest binding is missing; recovery refused",
                operation="read",
                details={"contract_id": contract_id},
            )
        manifest = validate_manifest(
            manifest_from_authority(anchor),
            contract_id=contract_id,
            path=store._manifest_path(contract_id),
        )
    else:
        try:
            terminal_recovered = validate_manifest_authority(manifest, anchor, terminal)
        except ValueError as exc:
            raise ArtifactManifestError(
                "Contract manifest binding does not match independently anchored initial or terminal authority",
                operation="read",
                details={"contract_id": contract_id},
            ) from exc
    try:
        cached = json.loads(
            read_bounded(
                binding,
                max_bytes=BINDING_MAX_BYTES,
                root=store._bindings_root,
                anchor=store._directory_anchor,
                label="artifact binding",
            )
        )
        validate_binding(cached, anchor)
    except FileNotFoundError:
        store._write_record_locked(
            binding,
            encode_record(anchor),
            stable=False,
            authority_check=authority_check,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ArtifactManifestError(
            "Artifact binding does not match independently anchored authority",
            operation="read",
            details={"contract_id": contract_id},
        ) from exc
    if recovering or terminal_recovered:
        store._write_manifest_locked(
            contract_id,
            manifest,
            authority_check=authority_check,
        )
    expected_marker = completion_payload(anchor)
    if marker.exists():
        actual_marker = read_bounded(
            marker,
            max_bytes=len(expected_marker),
            root=store.root.parent,
            anchor=store._lock_directory_anchor,
            label="artifact authority completion",
        )
        if actual_marker != expected_marker:
            raise ArtifactManifestError(
                "Artifact authority completion marker is invalid",
                operation="read",
                details={"contract_id": contract_id},
            )
    else:
        store._write_record_locked(
            marker,
            expected_marker,
            stable=True,
            authority_check=authority_check,
        )
    return manifest


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
    "reconcile_contract_authority",
    "tombstone_event",
    "tombstone_path",
    "tombstone_record",
    "validate_authority",
    "validate_binding",
    "validate_manifest_authority",
    "validate_tombstone_authority",
]
