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


def test_grant_resets_legacy_unbound_record_on_subject_bound_grant(tmp_path: Path) -> None:
    """Regression for the bot's BLOCKING finding on trust_store.py:318
    (`_subject_matches`).

    A pre-RFC ``trust.json`` has blank ``source_type`` /
    ``source_identity`` / ``artifact_digest`` columns. Without this
    fix, a follow-up ``grant()`` that plumbs the new install subject
    would silently "upgrade" the legacy record by appending one new
    scope while every previously stored scope carries over unconsented
    for THIS subject — defeating the trust-subject binding the rest of
    this PR enforces. The grant write path must treat any legacy /
    unbound existing record as a subject change and reset it before
    appending the new scope.
    """
    store = TrustStore(root=tmp_path)
    # Pre-RFC grant: only `version` + `scope`, no subject columns.
    store.grant(plugin="test-plugin", version="0.1.0", scope="github:read", granted_by="u")
    legacy = store.read("test-plugin")
    assert legacy is not None
    assert legacy.has_scope("github:read")
    assert legacy.source_type == ""

    # New grant under the post-RFC contract — full install subject
    # plumbed through. The legacy scope MUST be dropped because it was
    # never consented for THIS subject.
    record = store.grant(
        plugin="test-plugin",
        version="0.1.0",
        scope="github:repo:read",
        granted_by="u",
        source_type="local_path",
        source_identity="/tmp/installs/test-plugin",
        artifact_digest="sha256:" + "0" * 64,
    )
    assert record.source_type == "local_path"
    assert record.source_identity == "/tmp/installs/test-plugin"
    assert record.artifact_digest == "sha256:" + "0" * 64
    # Only the freshly granted scope survives — the legacy
    # `github:read` did not carry over.
    assert [g.scope for g in record.granted_scopes] == ["github:repo:read"]
    assert not record.has_scope("github:read")


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
    """Test 6: remove() deletes the trust file. The parent directory is
    not pruned because the per-plugin POSIX lock file
    (``trust.json.lock``) is intentionally kept on disk to preserve
    flock semantics across grant/remove cycles — see
    ``test_remove_keeps_lock_file_to_avoid_inode_race``.
    """
    store = TrustStore(root=tmp_path)
    store.grant(plugin="test-plugin", version="0.1.0", scope="github:read", granted_by="u")
    file_path = tmp_path / "test-plugin" / "trust.json"
    assert file_path.is_file()
    assert store.remove("test-plugin") is True
    assert not file_path.exists()
    # Removing again is a no-op.
    assert store.remove("test-plugin") is False


def test_remove_keeps_lock_file_to_avoid_inode_race(tmp_path: Path) -> None:
    """Regression: `remove()` used to also unlink `trust.json.lock`
    inside its critical section, but POSIX `flock` is attached to
    the inode behind the lock-file path. Removing the lock-file
    while still holding the flock orphans the inode: a concurrent
    `grant()` would `open(lock_path, "w")` against a brand-new
    inode, `flock` *that* exclusively, and run in parallel with
    the still-active `remove()` — reopening the very race the
    per-plugin lock was added to close. The lock-file is a
    synchronization primitive that must outlive individual
    operations, so `remove()` now leaves it in place.
    """
    store = TrustStore(root=tmp_path)
    store.grant(plugin="test-plugin", version="0.1.0", scope="github:read", granted_by="u")
    lock_path = tmp_path / "test-plugin" / "trust.json.lock"
    assert lock_path.exists(), "fixture sanity: lock file must have been created"
    assert store.remove("test-plugin") is True
    # The trust.json itself is gone, but the lock file is preserved
    # so subsequent grant/remove operations on the same plugin name
    # share the same inode-stable synchronization primitive.
    assert not (tmp_path / "test-plugin" / "trust.json").exists()
    assert lock_path.exists(), "lock file must persist across remove() to keep flock semantics safe"


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


def test_missing_returns_required_in_input_order(tmp_path: Path) -> None:
    """Test 9: TrustRecord.missing() returns missing required scopes in
    the input iteration order — useful for predictable error messages."""
    store = TrustStore(root=tmp_path)
    record = store.grant(plugin="test-plugin", version="0.1.0", scope="github:read", granted_by="u")
    # `github:read` is granted; the others are missing.
    missing = record.missing(["github:pull_request:write", "github:read", "shell:execute"])
    assert missing == ["github:pull_request:write", "shell:execute"]


@pytest.mark.parametrize(
    "bad_name",
    [
        "../escape",
        "..",
        "x/y",
        "x\\y",
        ".hidden",
        "X",  # uppercase, fails the locked manifest pattern
        "",
        "ab",  # too short
        "-leading-dash",
        "trailing-dash-",
        "with space",
    ],
)
def test_invalid_plugin_name_rejected(tmp_path: Path, bad_name: str) -> None:
    """Test 11: every public TrustStore method that takes a plugin name must
    reject names that could escape the trust root via path separators or
    parent traversal, or that violate the locked manifest name pattern.

    The bot review flagged ``self.root / plugin / "trust.json"`` as a
    boundary that must defensively validate caller input even when higher
    layers also validate.
    """
    store = TrustStore(root=tmp_path)
    with pytest.raises(ValueError, match="invalid plugin name"):
        store.read(bad_name)
    with pytest.raises(ValueError, match="invalid plugin name"):
        store.grant(
            plugin=bad_name,
            version="0.1.0",
            scope="github:read",
            granted_by="user:tester",
        )
    with pytest.raises(ValueError, match="invalid plugin name"):
        store.reset_for_version_bump(bad_name, new_version="0.2.0")
    with pytest.raises(ValueError, match="invalid plugin name"):
        store.remove(bad_name)


def test_concurrent_grants_do_not_lose_scopes(tmp_path: Path) -> None:
    """Regression: `TrustStore.grant()` was an unlocked
    read-modify-write. Two concurrent grants for different scopes
    on the same plugin could both observe the same prior file and
    each overwrite it with a one-scope payload, so the last writer
    silently deleted the other grant — real trust-state data loss.

    The store now brackets the cycle in a per-plugin POSIX file lock.
    This test fans out enough concurrent grants for distinct scopes
    that the prior racy implementation would lose at least one with
    high probability; under the new lock all scopes must survive.
    """
    import threading

    store = TrustStore(root=tmp_path)
    scopes = [f"scope:{i}" for i in range(20)]
    barrier = threading.Barrier(len(scopes))

    def _grant(scope: str) -> None:
        # Hit the lock at roughly the same instant from every thread.
        barrier.wait()
        store.grant(
            plugin="concurrent-plugin",
            version="0.1.0",
            scope=scope,
            granted_by="user:test",
        )

    threads = [threading.Thread(target=_grant, args=(s,)) for s in scopes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    record = store.read("concurrent-plugin")
    assert record is not None
    persisted = {g.scope for g in record.granted_scopes}
    assert persisted == set(scopes), (
        f"trust store lost {set(scopes) - persisted} under concurrent grants"
    )


def test_read_disable_raises_value_error_on_malformed_json(tmp_path):
    """Regression for the bot's BLOCKING finding on trust_store.py:461.

    ``read_disable`` is read by the firewall, ``inspect``, ``list``, and
    the top-level dispatch path. Those callers catch
    ``(ValueError, OSError)`` only — a raw ``json.JSONDecodeError``
    would escape as a traceback in the very commands operators use to
    repair plugin state. Truncated / non-object ``disabled.json`` files
    must surface as ``ValueError`` so the friendly recovery hint shape
    holds end to end.
    """
    from ouroboros.plugin.trust_store import TrustStore

    store = TrustStore(root=tmp_path)
    plugin_root = tmp_path / "broken-plugin"
    plugin_root.mkdir()

    # Truncated JSON.
    disabled = plugin_root / "disabled.json"
    disabled.write_text("{ truncated")
    with pytest.raises(ValueError, match="not valid JSON"):
        store.read_disable("broken-plugin")

    # Parseable JSON but a non-object root (e.g. a stray array).
    disabled.write_text("[]")
    with pytest.raises(ValueError, match="not a JSON object"):
        store.read_disable("broken-plugin")
