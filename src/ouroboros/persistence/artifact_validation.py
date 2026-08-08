"""Artifact JSON and bounded-manifest validation helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from typing import Any

from ouroboros.core.disposable_memory import (
    ARTIFACT_REF_PATTERN,
    MAX_DISPOSABLE_ARTIFACT_BYTES,
    DisposableResultEnvelope,
)
from ouroboros.persistence.artifact_errors import (
    ArtifactIntegrityError,
    ArtifactManifestError,
    ArtifactStoreError,
)
from ouroboros.persistence.artifact_schema import require_fields


def validate_json_native(
    value: Any,
    *,
    path: str = "$",
    ancestors: set[int] | None = None,
) -> None:
    """Reject Python values that JSON cannot replay without normalization."""
    value_type = type(value)
    if value is None or value_type in {bool, int, str}:
        return
    if value_type is float:
        if math.isfinite(value):
            return
        raise ArtifactStoreError(
            "Disposable artifact contains a non-finite JSON number",
            operation="serialize",
            details={"path": path},
        )
    if value_type not in {dict, list}:
        raise ArtifactStoreError(
            "Disposable artifact values must use JSON-native types",
            operation="serialize",
            details={"path": path, "type": value_type.__name__},
        )
    active = ancestors if ancestors is not None else set()
    marker = id(value)
    if marker in active:
        raise ArtifactStoreError(
            "Disposable artifact contains a circular JSON value",
            operation="serialize",
            details={"path": path},
        )
    active.add(marker)
    try:
        if value_type is list:
            for index, item in enumerate(value):
                validate_json_native(item, path=f"{path}[{index}]", ancestors=active)
            return
        for key, item in value.items():
            if type(key) is not str:
                raise ArtifactStoreError(
                    "Disposable artifact object keys must be JSON strings",
                    operation="serialize",
                    details={"path": path, "key_type": type(key).__name__},
                )
            validate_json_native(item, path=f"{path}.{key}", ancestors=active)
    finally:
        active.remove(marker)


def canonical_artifact_bytes(body: Any) -> bytes:
    """Encode a JSON artifact deterministically for hashing and exact limits."""
    validate_json_native(body)
    try:
        rendered = json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ArtifactStoreError(
            f"Disposable artifact is not canonical JSON: {exc}",
            operation="serialize",
        ) from exc
    return rendered.encode("utf-8")


def event_artifact_ref(event: dict[str, Any], *, contract_id: str) -> str:
    artifact_ref = event.get("artifact_ref")
    if not isinstance(artifact_ref, str) or not ARTIFACT_REF_PATTERN.fullmatch(artifact_ref):
        raise ArtifactManifestError(
            "Artifact manifest event has an invalid artifact_ref",
            operation="read",
            details={"contract_id": contract_id},
        )
    return artifact_ref


def latest_artifact_event(manifest: dict[str, Any]) -> dict[str, Any] | None:
    for event in reversed(manifest["events"]):
        if event.get("type") in {"artifact.referenced", "artifact.tombstoned"}:
            return event
    return None


def envelope_from_event(
    event: dict[str, Any],
    *,
    contract_id: str,
) -> DisposableResultEnvelope:
    envelope = event.get("envelope")
    try:
        parsed = DisposableResultEnvelope.model_validate(envelope)
    except (TypeError, ValueError) as exc:
        raise ArtifactManifestError(
            "Artifact reference event has an invalid bounded envelope",
            operation="read",
        ) from exc
    artifact_ref = event_artifact_ref(event, contract_id=contract_id)
    if parsed.contract_id != contract_id or parsed.artifact_ref != artifact_ref:
        raise ArtifactManifestError(
            "Artifact reference envelope does not match its manifest event",
            operation="read",
            details={"contract_id": contract_id, "artifact_ref": artifact_ref},
        )
    size_bytes = event.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 0 <= size_bytes <= MAX_DISPOSABLE_ARTIFACT_BYTES
    ):
        raise ArtifactManifestError(
            "Artifact reference event has an invalid size_bytes",
            operation="read",
            details={"contract_id": contract_id, "artifact_ref": artifact_ref},
        )
    return parsed


def parse_datetime(value: Any, *, field: str, path: Path) -> datetime:
    if not isinstance(value, str):
        raise ArtifactManifestError(
            f"Artifact manifest {field} must be an ISO-8601 string",
            operation="read",
            details={"path": str(path)},
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ArtifactManifestError(
            f"Artifact manifest {field} is not valid ISO-8601",
            operation="read",
            details={"path": str(path)},
        ) from exc
    if parsed.tzinfo is None:
        raise ArtifactManifestError(
            f"Artifact manifest {field} must include a timezone",
            operation="read",
            details={"path": str(path)},
        )
    return parsed.astimezone(UTC)


def validate_manifest(raw: Any, *, contract_id: str, path: Path) -> dict[str, Any]:
    """Validate exact mutable-manifest schema and bounded reference events."""
    if not isinstance(raw, dict):
        raise ArtifactManifestError(
            "Artifact manifest must be a JSON object",
            operation="read",
            details={"path": str(path)},
        )
    try:
        require_fields(raw)
    except ValueError as exc:
        raise ArtifactManifestError(
            "Artifact manifest fields do not match the exact versioned schema",
            operation="read",
            details={"path": str(path), "contract_id": contract_id},
        ) from exc
    if raw.get("schema_version") != 1 or raw.get("contract_id") != contract_id:
        raise ArtifactManifestError(
            "Artifact manifest identity or schema version is invalid",
            operation="read",
            details={"path": str(path), "contract_id": contract_id},
        )
    if not isinstance(raw.get("active"), bool) or not isinstance(raw.get("events"), list):
        raise ArtifactManifestError(
            "Artifact manifest state is invalid",
            operation="read",
            details={"path": str(path), "contract_id": contract_id},
        )
    for event in raw["events"]:
        if not isinstance(event, dict):
            raise ArtifactManifestError(
                "Artifact manifest events must be objects",
                operation="read",
                details={"path": str(path), "contract_id": contract_id},
            )
        event_type = event.get("type")
        if event_type not in {"artifact.referenced", "artifact.tombstoned"}:
            raise ArtifactManifestError(
                "Artifact manifest contains an unknown event type",
                operation="read",
                details={"path": str(path), "event_type": event_type},
            )
        event_artifact_ref(event, contract_id=contract_id)
        parse_datetime(event.get("timestamp"), field="event.timestamp", path=path)
        if event_type == "artifact.referenced":
            envelope_from_event(event, contract_id=contract_id)
    retain_until = raw.get("retain_until")
    if retain_until is not None:
        parse_datetime(retain_until, field="retain_until", path=path)
    return raw


def manifest_retained(manifest: dict[str, Any], *, now: datetime) -> bool:
    retain_until = manifest.get("retain_until")
    if retain_until is None:
        return True
    return (
        parse_datetime(
            retain_until,
            field="retain_until",
            path=Path(f"contract:{manifest.get('contract_id', 'unknown')}"),
        )
        > now
    )


def validate_project_boundary(
    *,
    root: Path,
    project_root: Path | None,
    contracts_root: Path,
    bindings_root: Path,
    is_link_like: Callable[[Path], bool],
) -> None:
    """Keep all replaceable artifact metadata inside the selected project."""
    if project_root is None:
        return
    try:
        relative_root = root.relative_to(project_root)
    except ValueError as exc:
        raise ArtifactIntegrityError(
            "Project artifact store path escapes the project root",
            operation="path_resolution",
            details={"path": str(root), "project_root": str(project_root)},
        ) from exc
    current = project_root
    for component in relative_root.parts:
        current /= component
        if is_link_like(current):
            raise ArtifactIntegrityError(
                "Project artifact store path must not traverse a symlink",
                operation="path_resolution",
                details={"path": str(current), "project_root": str(project_root)},
            )
    if is_link_like(contracts_root) or is_link_like(bindings_root):
        raise ArtifactIntegrityError(
            "Project artifact metadata paths must not be symlinks",
            operation="path_resolution",
            details={"path": str(root), "project_root": str(project_root)},
        )
    try:
        resolved_root = root.resolve()
        resolved_contracts = contracts_root.resolve()
        resolved_bindings = bindings_root.resolve()
    except (OSError, RuntimeError) as exc:
        raise ArtifactIntegrityError(
            "Project artifact store path could not be resolved safely",
            operation="path_resolution",
            details={"path": str(root), "project_root": str(project_root)},
        ) from exc
    if (
        not resolved_root.is_relative_to(project_root)
        or not resolved_contracts.is_relative_to(resolved_root)
        or not resolved_bindings.is_relative_to(resolved_root)
    ):
        raise ArtifactIntegrityError(
            "Project artifact store path escapes the project-owned store",
            operation="path_resolution",
            details={
                "path": str(resolved_contracts),
                "root": str(resolved_root),
                "project_root": str(project_root),
            },
        )


__all__ = [
    "canonical_artifact_bytes",
    "envelope_from_event",
    "event_artifact_ref",
    "latest_artifact_event",
    "manifest_retained",
    "parse_datetime",
    "validate_manifest",
    "validate_project_boundary",
    "validate_json_native",
]
