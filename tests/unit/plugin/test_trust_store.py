"""Tests for the per-plugin trust store (Q00/ouroboros#732)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.plugin.trust_store import (
    TRUST_SCHEMA_VERSION,
    TrustRecord,
    TrustStore,
)


def test_grant_then_read(tmp_path: Path) -> None:
    """Test 1: grant a scope, read it back. File at locked Q5 path."""
    store = TrustStore(root=tmp_path)
    record = store.grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="user:shaun0927",
    )
    assert record.has_scope("github:read")

    file_path = tmp_path / "github-pr-ops" / "trust.json"
    assert file_path.is_file()
    data = json.loads(file_path.read_text())
    assert data["schema_version"] == TRUST_SCHEMA_VERSION
    assert data["plugin"] == "github-pr-ops"
    assert data["version"] == "0.1.0"
    assert data["granted_scopes"][0]["scope"] == "github:read"
    assert data["granted_scopes"][0]["granted_by"] == "user:shaun0927"


def test_grant_is_idempotent(tmp_path: Path) -> None:
    """Test 2: granting the same scope twice does not duplicate."""
    store = TrustStore(root=tmp_path)
    store.grant(plugin="X", version="0.1.0", scope="github:read", granted_by="u")
    record = store.grant(plugin="X", version="0.1.0", scope="github:read", granted_by="u")
    assert len(record.granted_scopes) == 1


def test_exact_scope_only(tmp_path: Path) -> None:
    """Test 3: parent scope does NOT imply child (Q3 lock).

    Granting `github:pull_request` does not satisfy `github:pull_request:write`.
    """
    store = TrustStore(root=tmp_path)
    record = store.grant(
        plugin="X",
        version="0.1.0",
        scope="github:pull_request",
        granted_by="u",
    )
    assert record.has_scope("github:pull_request")
    assert not record.has_scope("github:pull_request:write")
    assert record.missing(["github:pull_request:write"]) == ["github:pull_request:write"]


def test_version_bump_invalidates_trust(tmp_path: Path) -> None:
    """Test 4: granting against a new version drops the previous grants
    (Q00/ouroboros-plugins#9 Q4 lock)."""
    store = TrustStore(root=tmp_path)
    store.grant(plugin="X", version="0.1.0", scope="github:read", granted_by="u")
    store.grant(plugin="X", version="0.1.0", scope="github:repo:read", granted_by="u")

    # Now bump to 0.2.0 and grant a different scope.
    record = store.grant(plugin="X", version="0.2.0", scope="github:read", granted_by="u")
    assert record.version == "0.2.0"
    # Previous github:repo:read grant is invalidated.
    assert not record.has_scope("github:repo:read")
    # The newly granted scope on the new version is present.
    assert record.has_scope("github:read")


def test_reset_for_version_bump(tmp_path: Path) -> None:
    """Test 5: explicit version-bump reset writes an empty grant list."""
    store = TrustStore(root=tmp_path)
    store.grant(plugin="X", version="0.1.0", scope="github:read", granted_by="u")
    store.reset_for_version_bump("X", new_version="0.2.0")

    record = store.read("X")
    assert isinstance(record, TrustRecord)
    assert record.version == "0.2.0"
    assert record.granted_scopes == ()


def test_remove_drops_trust_file(tmp_path: Path) -> None:
    """Test 6: remove() deletes the trust file and prunes the empty dir."""
    store = TrustStore(root=tmp_path)
    store.grant(plugin="X", version="0.1.0", scope="github:read", granted_by="u")
    file_path = tmp_path / "X" / "trust.json"
    assert file_path.is_file()
    assert store.remove("X") is True
    assert not file_path.exists()
    # Directory pruned.
    assert not file_path.parent.exists()
    # Removing again is a no-op.
    assert store.remove("X") is False


def test_unsupported_schema_version_rejected(tmp_path: Path) -> None:
    """Test 7: a trust file with the wrong schema_version raises on read."""
    plugin_dir = tmp_path / "X"
    plugin_dir.mkdir()
    (plugin_dir / "trust.json").write_text(
        json.dumps(
            {
                "schema_version": "99.0",
                "plugin": "X",
                "version": "0.1.0",
                "granted_scopes": [],
            }
        )
    )
    store = TrustStore(root=tmp_path)
    with pytest.raises(ValueError, match="unsupported trust file schema_version"):
        store.read("X")


def test_no_raw_token_in_persisted_file(tmp_path: Path) -> None:
    """Test 8: scope strings and granted_by are persisted, but nothing
    else. The store offers no API for tokens; this test is a sanity
    check that future contributors don't add one without notice."""
    store = TrustStore(root=tmp_path)
    store.grant(
        plugin="X",
        version="0.1.0",
        scope="github:read",
        granted_by="user:shaun0927",
    )
    raw = (tmp_path / "X" / "trust.json").read_text()
    # Keys present
    assert '"scope"' in raw
    assert '"granted_by"' in raw
    assert '"granted_at"' in raw
    # Nothing token-shaped (no "token", "secret", "auth", "Bearer")
    for forbidden in ("token", "secret", "auth", "Bearer", "ghp_"):
        assert forbidden.lower() not in raw.lower(), f"forbidden marker {forbidden!r} in trust file"


def test_missing_returns_required_in_input_order(tmp_path: Path) -> None:
    """Test 9: TrustRecord.missing() returns missing required scopes in
    the input iteration order — useful for predictable error messages."""
    store = TrustStore(root=tmp_path)
    record = store.grant(plugin="X", version="0.1.0", scope="github:read", granted_by="u")
    # `github:read` is granted; the others are missing.
    missing = record.missing(["github:pull_request:write", "github:read", "shell:execute"])
    assert missing == ["github:pull_request:write", "shell:execute"]
