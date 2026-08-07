"""Durability and fail-closed tests for Auto start leases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.auto.state import AutoStore
from ouroboros.mcp.tools.auto_handler import (
    _claim_start_lease,
    _read_start_lease,
    _release_start_lease,
    _reserve_start_lease,
    _write_start_lease_locked,
)
from ouroboros.mcp.tools.auto_start_lease_store import CORRUPT_START_LEASE

_AUTO_SESSION_ID = "auto_lease_integrity"


def _live_legacy_lease(*, token: str = "original_token") -> dict[str, object]:
    return {
        "token": token,
        "mode": "plugin_dispatched",
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "owner_pid": os.getpid(),
        "owner_start_time": None,
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"\xff\xfe",
        b'{"token":"truncated"',
        b"[]",
        b'{"token":"future","mode":"future","created_at":"2026-08-08T00:00:00+00:00"}',
    ],
)
def test_corrupt_or_truncated_lease_cannot_be_overwritten_or_released(
    tmp_path: Path, raw: bytes
) -> None:
    store = AutoStore(tmp_path)
    path = store.path_for(_AUTO_SESSION_ID).with_suffix(".start_auto_lease.json")
    path.write_bytes(raw)

    assert _read_start_lease(store, _AUTO_SESSION_ID) is CORRUPT_START_LEASE

    token, error = _reserve_start_lease(
        store,
        _AUTO_SESSION_ID,
        mode="plugin_pending",
        ttl_seconds=30.0,
    )
    assert token == ""
    assert error is not None
    assert "corrupt or unreadable" in error.message
    assert path.read_bytes() == raw

    claim_error = _claim_start_lease(
        store,
        _AUTO_SESSION_ID,
        token="truncated",
        ttl_seconds=30.0,
    )
    assert claim_error is not None
    assert "corrupt or unreadable" in claim_error.message
    assert path.read_bytes() == raw

    _release_start_lease(store, _AUTO_SESSION_ID, token="truncated")
    assert path.read_bytes() == raw


def test_unreadable_lease_is_not_treated_as_absent(tmp_path: Path) -> None:
    store = AutoStore(tmp_path)
    path = store.path_for(_AUTO_SESSION_ID).with_suffix(".start_auto_lease.json")
    original = json.dumps(_live_legacy_lease()).encode()
    path.write_bytes(original)

    with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
        assert _read_start_lease(store, _AUTO_SESSION_ID) is CORRUPT_START_LEASE
        token, error = _reserve_start_lease(
            store,
            _AUTO_SESSION_ID,
            mode="plugin_pending",
            ttl_seconds=30.0,
        )
        _release_start_lease(store, _AUTO_SESSION_ID, token="original_token")

    assert token == ""
    assert error is not None
    assert path.read_bytes() == original


def test_release_requires_the_exact_persisted_token(tmp_path: Path) -> None:
    store = AutoStore(tmp_path)
    path = store.path_for(_AUTO_SESSION_ID).with_suffix(".start_auto_lease.json")
    original = json.dumps(_live_legacy_lease()).encode()
    path.write_bytes(original)

    _release_start_lease(store, _AUTO_SESSION_ID, token="wrong_token")
    assert path.read_bytes() == original

    _release_start_lease(store, _AUTO_SESSION_ID, token="original_token")
    assert not path.exists()


def test_atomic_write_failure_preserves_prior_lease_across_restart(tmp_path: Path) -> None:
    store = AutoStore(tmp_path)
    path = store.path_for(_AUTO_SESSION_ID).with_suffix(".start_auto_lease.json")
    original = json.dumps(_live_legacy_lease()).encode()
    path.write_bytes(original)

    replacement = _live_legacy_lease(token="replacement_token")
    with (
        patch("ouroboros.core.owner_only.os.replace", side_effect=OSError("replace failed")),
        pytest.raises(OSError, match="replace failed"),
    ):
        _write_start_lease_locked(path, replacement)

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(f".{path.name}*.tmp-*"))

    restarted_store = AutoStore(tmp_path)
    token, error = _reserve_start_lease(
        restarted_store,
        _AUTO_SESSION_ID,
        mode="plugin_pending",
        ttl_seconds=30.0,
    )
    assert token == ""
    assert error is not None
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "original_token"
