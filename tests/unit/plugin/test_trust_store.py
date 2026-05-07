"""Tests for the per-plugin trust store (Q00/ouroboros#732)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.plugin.trust_store import (
    TRUST_SCHEMA_VERSION,
    TrustRecord,
    TrustStore,
    TrustStoreError,
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
    store.grant(plugin="test-plugin", version="0.1.0", scope="github:read", granted_by="u")
    record = store.grant(plugin="test-plugin", version="0.1.0", scope="github:read", granted_by="u")
    assert len(record.granted_scopes) == 1


def test_exact_scope_only(tmp_path: Path) -> None:
    """Test 3: parent scope does NOT imply child (Q3 lock).

    Granting `github:pull_request` does not satisfy `github:pull_request:write`.
    """
    store = TrustStore(root=tmp_path)
    record = store.grant(
        plugin="test-plugin",
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
    store.grant(plugin="test-plugin", version="0.1.0", scope="github:read", granted_by="u")
    store.grant(plugin="test-plugin", version="0.1.0", scope="github:repo:read", granted_by="u")

    # Now bump to 0.2.0 and grant a different scope.
    record = store.grant(plugin="test-plugin", version="0.2.0", scope="github:read", granted_by="u")
    assert record.version == "0.2.0"
    # Previous github:repo:read grant is invalidated.
    assert not record.has_scope("github:repo:read")
    # The newly granted scope on the new version is present.
    assert record.has_scope("github:read")


def test_reset_for_version_bump(tmp_path: Path) -> None:
    """Test 5: explicit version-bump reset writes an empty grant list."""
    store = TrustStore(root=tmp_path)
    store.grant(plugin="test-plugin", version="0.1.0", scope="github:read", granted_by="u")
    store.reset_for_version_bump("test-plugin", new_version="0.2.0")

    record = store.read("test-plugin")
    assert isinstance(record, TrustRecord)
    assert record.version == "0.2.0"
    assert record.granted_scopes == ()


def test_remove_drops_trust_file(tmp_path: Path) -> None:
    """Test 6: remove() deletes the trust file.

    The lock file (`trust.json.lock`) is intentionally retained — see
    `TrustStore.remove` docstring for the cross-process race that
    deleting it would reintroduce. The plugin directory therefore may
    not be empty after `remove()`, so this test only asserts that the
    trust record itself is gone.
    """
    store = TrustStore(root=tmp_path)
    store.grant(plugin="test-plugin", version="0.1.0", scope="github:read", granted_by="u")
    file_path = tmp_path / "test-plugin" / "trust.json"
    assert file_path.is_file()
    assert store.remove("test-plugin") is True
    assert not file_path.exists()
    # Removing again is a no-op (file already gone).
    assert store.remove("test-plugin") is False
    # Lock file is retained on purpose to keep cross-process flock
    # synchronization sound.
    assert (tmp_path / "test-plugin" / "trust.json.lock").exists()


def test_unsupported_schema_version_rejected(tmp_path: Path) -> None:
    """Test 7: a trust file with the wrong schema_version raises on read."""
    plugin_dir = tmp_path / "test-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "trust.json").write_text(
        json.dumps(
            {
                "schema_version": "99.0",
                "plugin": "test-plugin",
                "version": "0.1.0",
                "granted_scopes": [],
            }
        )
    )
    store = TrustStore(root=tmp_path)
    with pytest.raises(ValueError, match="unsupported trust file schema_version"):
        store.read("test-plugin")


def test_no_raw_token_in_persisted_file(tmp_path: Path) -> None:
    """Test 8: scope strings and granted_by are persisted, but nothing
    else. The store offers no API for tokens; this test is a sanity
    check that future contributors don't add one without notice."""
    store = TrustStore(root=tmp_path)
    store.grant(
        plugin="test-plugin",
        version="0.1.0",
        scope="github:read",
        granted_by="user:shaun0927",
    )
    raw = (tmp_path / "test-plugin" / "trust.json").read_text()
    # Keys present
    assert '"scope"' in raw
    assert '"granted_by"' in raw
    assert '"granted_at"' in raw
    # Nothing token-shaped (no "token", "secret", "auth", "Bearer")
    for forbidden in ("token", "secret", "auth", "Bearer", "ghp_"):
        assert forbidden.lower() not in raw.lower(), f"forbidden marker {forbidden!r} in trust file"


def test_concurrent_grants_do_not_lose_data(tmp_path: Path) -> None:
    """Test 10: concurrent grants on the same plugin must not silently
    drop scopes via read-modify-write race.

    The pre-fix `grant()` did `self.read()` then `self._write_atomic()`
    with no inter-thread serialization. Two concurrent grants would both
    read the same baseline, append different scopes, and the later
    `os.replace` would silently win — losing the earlier grant.

    This test races N threads granting distinct scopes against a shared
    trust file and asserts every scope survives. The fix uses an
    exclusive `fcntl.flock` around the RMW.
    """
    import threading

    store = TrustStore(root=tmp_path)
    n_threads = 12
    barrier = threading.Barrier(n_threads)
    scopes = [f"capability:scope_{i:02d}" for i in range(n_threads)]
    errors: list[BaseException] = []

    def _worker(scope: str) -> None:
        try:
            barrier.wait(timeout=5)
            store.grant(
                plugin="test-plugin",
                version="0.1.0",
                scope=scope,
                granted_by="user:test",
            )
        except BaseException as exc:  # noqa: BLE001 - propagate to assertion
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(s,)) for s in scopes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"worker errors: {errors!r}"

    final = store.read("test-plugin")
    assert final is not None
    persisted = {g.scope for g in final.granted_scopes}
    assert persisted == set(scopes), (
        f"lost scopes under concurrent grant: missing="
        f"{set(scopes) - persisted}, extra={persisted - set(scopes)}"
    )


@pytest.mark.parametrize(
    "evil_name",
    [
        "../../../etc/passwd",
        "..",
        "./relative",
        "subdir/leak",
        "absolute\\path",
        "X",  # uppercase, schema-rejected
        "",  # empty
        "name with spaces",
    ],
)
def test_path_traversal_in_plugin_name_is_rejected(tmp_path: Path, evil_name: str) -> None:
    """Plugin identifiers used as filesystem path segments must not be
    able to escape the configured trust root.

    Pre-fix `TrustStore` interpolated `plugin` directly into the
    `<root>/<plugin>/trust.json` path, so a value containing path
    separators or `..` segments could read, write, or delete arbitrary
    files reachable by the process. The store now validates the
    identifier against the manifest-name pattern at every public
    boundary.
    """
    store = TrustStore(root=tmp_path)
    with pytest.raises(TrustStoreError):
        store.grant(plugin=evil_name, version="0.1.0", scope="x:read", granted_by="u")
    with pytest.raises(TrustStoreError):
        store.read(evil_name)
    with pytest.raises(TrustStoreError):
        store.remove(evil_name)
    with pytest.raises(TrustStoreError):
        store.reset_for_version_bump(evil_name, new_version="0.2.0")
    # Filesystem must be untouched: nothing escaped the tmp_path tree.
    assert list(tmp_path.iterdir()) == []


def test_missing_returns_required_in_input_order(tmp_path: Path) -> None:
    """Test 9: TrustRecord.missing() returns missing required scopes in
    the input iteration order — useful for predictable error messages."""
    store = TrustStore(root=tmp_path)
    record = store.grant(plugin="test-plugin", version="0.1.0", scope="github:read", granted_by="u")
    # `github:read` is granted; the others are missing.
    missing = record.missing(["github:pull_request:write", "github:read", "shell:execute"])
    assert missing == ["github:pull_request:write", "shell:execute"]
