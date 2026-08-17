"""Identity, timestamp, and canonical-JSON rules for disposable artifacts.

These are the validation rules the SQLite-backed store still needs.  The
manifest-shape validation that used to live beside them belonged to the
directory store and left with it.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import math
from typing import Any

from ouroboros.persistence.artifact_errors import ArtifactStoreError


def validate_contract_id(contract_id: str) -> str:
    """Return one bounded disposable contract identity.

    Length is the whole rule.  The character restriction this used to enforce
    was a path-safe-filename rule, and a contract id stopped being a filename
    when the store stopped being a directory.  The envelope validator in
    ``ouroboros.core.disposable_memory`` grants the same permission, so the two
    loosened together.
    """
    if not isinstance(contract_id, str) or not 1 <= len(contract_id) <= 128:
        raise ValueError("contract_id must be 1-128 characters")
    return contract_id


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
    """Encode a JSON artifact deterministically for equality and exact limits."""
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


def as_utc(value: datetime) -> datetime:
    """Normalize one timezone-aware timestamp to UTC."""
    if value.tzinfo is None:
        raise ValueError("datetime values must include a timezone")
    return value.astimezone(UTC)


__all__ = [
    "as_utc",
    "canonical_artifact_bytes",
    "validate_contract_id",
    "validate_json_native",
]
