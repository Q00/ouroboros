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
    """Test 6: remove() deletes the trust file and is idempotent.

    The sidecar `trust.json.lock` is intentionally left in place
    (see TrustStore.remove docstring) so the per-plugin flock keeps
    serialising concurrent writers that might still be racing the
    removal. Caller-side directory cleanup is `ooo plugin remove`'s
    job, not the trust store's.
    """
    store = TrustStore(root=tmp_path)
    store.grant(plugin="X", version="0.1.0", scope="github:read", granted_by="u")
    file_path = tmp_path / "X" / "trust.json"
    assert file_path.is_file()
    assert store.remove("X") is True
    assert not file_path.exists()
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


def _grant_in_subprocess(root_str: str, plugin: str, version: str, scope: str) -> None:
    """Module-level worker so multiprocessing.spawn can pickle it on macOS."""
    from pathlib import Path

    from ouroboros.plugin.trust_store import TrustStore

    TrustStore(root=Path(root_str)).grant(
        plugin=plugin, version=version, scope=scope, granted_by="user:test"
    )


def _reset_in_subprocess(root_str: str, plugin: str, new_version: str) -> None:
    from pathlib import Path

    from ouroboros.plugin.trust_store import TrustStore

    TrustStore(root=Path(root_str)).reset_for_version_bump(plugin, new_version)


def _remove_in_subprocess(root_str: str, plugin: str) -> None:
    from pathlib import Path

    from ouroboros.plugin.trust_store import TrustStore

    TrustStore(root=Path(root_str)).remove(plugin)


def test_remove_keeps_lock_file_sticky_for_serialisation(tmp_path: Path) -> None:
    """`remove()` must NOT unlink the sidecar lock file.

    Regression for ouroboros-agent[bot] BLOCKING finding on PR #749 commit
    093873e: previously remove() unlinked `trust.json.lock` after
    releasing the flock, opening a race in which a concurrent grant()
    locked the old inode and the next writer locked a different
    (recreated) inode — so the two processes no longer serialised.
    The sidecar is now sticky so every future writer locks the same
    inode that any concurrent process still holds open.
    """
    store = TrustStore(root=tmp_path)
    store.grant(plugin="X", version="0.1.0", scope="github:read", granted_by="u")
    lock_path = tmp_path / "X" / "trust.json.lock"
    # Force the lock-file path to exist by triggering a second mutation
    # (grant takes the lock, which creates the sidecar).
    store.grant(plugin="X", version="0.1.0", scope="github:write", granted_by="u")
    assert lock_path.is_file(), "lock file should exist after a mutation"

    assert store.remove("X") is True
    # The trust file is gone, but the lock file remains so future
    # writers serialise against the same inode.
    assert not (tmp_path / "X" / "trust.json").exists()
    assert lock_path.is_file()


def test_concurrent_remove_and_grants_keep_state_consistent(tmp_path: Path) -> None:
    """A `remove()` racing concurrent `grant()`s must not corrupt the
    trust file or leave it in an inconsistent (plugin, version) state.

    Same regression class as the earlier concurrent-grant test but
    extended to cover the third mutating path. With the sticky-lock
    fix, all writers serialise on the same lock inode, so any final
    state is internally coherent.
    """
    import json as _json
    import multiprocessing as mp

    plugin = "concurrent-remove"
    ctx = mp.get_context("spawn")

    TrustStore(root=tmp_path).grant(plugin=plugin, version="0.1.0", scope="seed", granted_by="u")

    workers = [
        ctx.Process(target=_grant_in_subprocess, args=(str(tmp_path), plugin, "0.1.0", "alpha")),
        ctx.Process(target=_remove_in_subprocess, args=(str(tmp_path), plugin)),
        ctx.Process(target=_grant_in_subprocess, args=(str(tmp_path), plugin, "0.1.0", "beta")),
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)
        assert w.exitcode == 0, f"worker failed: pid={w.pid} exit={w.exitcode}"

    trust_file = tmp_path / plugin / "trust.json"
    if trust_file.is_file():
        # If a grant won the race, the file must be a coherent JSON
        # document with the expected schema_version and plugin name.
        payload = _json.loads(trust_file.read_text())
        assert payload["plugin"] == plugin
        assert payload["schema_version"] == "0.1"
        assert payload["version"] == "0.1.0"
        # And the public reader must succeed without raising.
        assert TrustStore(root=tmp_path).read(plugin) is not None
    # Sidecar lock file persists either way, so future serialisation works.
    assert (tmp_path / plugin / "trust.json.lock").is_file()


def test_concurrent_reset_and_grant_produce_valid_file(tmp_path: Path) -> None:
    """`reset_for_version_bump` racing with concurrent `grant()` calls
    must not corrupt the trust file or partially write across writers.

    Regression for ouroboros-agent[bot] BLOCKING finding on PR #749 commit
    e0112d3: previously `reset_for_version_bump()` and `remove()` did not
    take the same per-plugin flock that `grant()` did, so an upgrade/reset
    racing with `ooo plugin trust` could clobber state in either direction.
    All three mutating operations now share the lock; under contention the
    file must remain a well-formed JSON document with the correct
    schema_version and a coherent (plugin, version) pair.
    """
    import json as _json
    import multiprocessing as mp

    plugin = "concurrent-reset"
    ctx = mp.get_context("spawn")

    # Seed the file at v1 with a starting scope so all three mutators
    # operate on an existing record.
    TrustStore(root=tmp_path).grant(
        plugin=plugin, version="0.1.0", scope="github:read", granted_by="u"
    )

    workers = [
        ctx.Process(target=_grant_in_subprocess, args=(str(tmp_path), plugin, "0.1.0", "a")),
        ctx.Process(target=_reset_in_subprocess, args=(str(tmp_path), plugin, "0.2.0")),
        ctx.Process(target=_grant_in_subprocess, args=(str(tmp_path), plugin, "0.1.0", "b")),
        ctx.Process(target=_grant_in_subprocess, args=(str(tmp_path), plugin, "0.2.0", "c")),
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)
        assert w.exitcode == 0, f"worker failed: pid={w.pid} exit={w.exitcode}"

    # The file must still be a syntactically valid JSON object with the
    # expected top-level fields. Race-corrupted partial writes would fail
    # here.
    raw = (tmp_path / plugin / "trust.json").read_text()
    payload = _json.loads(raw)
    assert payload["plugin"] == plugin
    assert payload["schema_version"] == "0.1"
    assert payload["version"] in {"0.1.0", "0.2.0"}
    # Read through the public API: it must succeed without raising.
    assert TrustStore(root=tmp_path).read(plugin) is not None


def test_grant_concurrent_writes_do_not_lose_scopes(tmp_path: Path) -> None:
    """Concurrent grant() calls for different scopes must all persist.

    Regression for ouroboros-agent[bot] BLOCKING finding on PR #749 commit
    78698d0: the read-modify-write in `grant()` was previously unguarded,
    so two processes granting different scopes in parallel could both
    read the same prior file and the second `os.replace()` would silently
    drop the other writer's scope. The store now holds an exclusive
    POSIX flock around the sequence; this test exercises the protection
    by spawning several processes concurrently and asserting all scopes
    survive in the final trust file.
    """
    import multiprocessing as mp

    scopes = [f"github:scope-{i}" for i in range(6)]
    ctx = mp.get_context("spawn")  # cross-platform; matches macOS default
    procs = [
        ctx.Process(
            target=_grant_in_subprocess,
            args=(str(tmp_path), "concurrent-plugin", "0.1.0", scope),
        )
        for scope in scopes
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0, f"worker failed: pid={p.pid} exit={p.exitcode}"

    record = TrustStore(root=tmp_path).read("concurrent-plugin")
    assert record is not None
    persisted = {g.scope for g in record.granted_scopes}
    missing = set(scopes) - persisted
    assert not missing, f"concurrent grants dropped scopes: {sorted(missing)}"
