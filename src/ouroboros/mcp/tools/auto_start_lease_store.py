"""Fail-closed persistence primitives for Auto start leases."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from ouroboros.core.owner_only import write_owner_only

CORRUPT_START_LEASE: dict[str, Any] = {"_lease_state": "corrupt_or_unreadable"}
_START_LEASE_MODES = {
    "direct_auto",
    "job",
    "job_pending",
    "plugin_claimed",
    "plugin_dispatched",
    "plugin_pending",
}


def read_start_lease_locked(path: Path) -> dict[str, Any] | None:
    """Read a lease, distinguishing absence from unsafe persisted state."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return CORRUPT_START_LEASE
    if not _valid_start_lease_payload(payload):
        return CORRUPT_START_LEASE
    return payload


def _valid_start_lease_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    for field in ("token", "mode", "created_at"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            return False
    if payload["mode"] not in _START_LEASE_MODES:
        return False
    job_id = payload.get("job_id")
    if job_id is not None and (not isinstance(job_id, str) or not job_id):
        return False
    for field in ("created_at", "updated_at", "expires_at"):
        value = payload.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            return False
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return False
    return True


def write_start_lease_locked(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace a lease without exposing partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    durable = write_owner_only(path, json.dumps(payload, sort_keys=True))
    if not durable:
        raise OSError(f"could not confirm start lease durability: {path}")
