"""Artifact JSON and bounded-manifest validation helpers."""

from __future__ import annotations

import math
from typing import Any

from ouroboros.core.disposable_memory import (
    ARTIFACT_REF_PATTERN,
    MAX_DISPOSABLE_ARTIFACT_BYTES,
    DisposableResultEnvelope,
)
from ouroboros.persistence.artifact_errors import ArtifactManifestError, ArtifactStoreError


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


__all__ = [
    "envelope_from_event",
    "event_artifact_ref",
    "latest_artifact_event",
    "validate_json_native",
]
