"""Tests for the plugin lockfile (Q00/ouroboros#732)."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from ouroboros.plugin.lockfile import (
    LOCKFILE_SCHEMA_VERSION,
    LockEntry,
    Lockfile,
)


def _make_entry(name: str = "github-pr-ops", version: str = "0.1.0") -> LockEntry:
    return LockEntry(
        name=name,
        version=version,
        source_kind="git",
        repository="https://github.com/Q00/ouroboros-plugins",
        git_sha="b3a91f2",
        manifest_checksum="sha256:abc123",
        installed_at="2026-05-08T03:14:00Z",
        plugin_home=f"~/.ouroboros/plugins/{name}",
    )


def test_install_then_read(tmp_path: Path) -> None:
    """Test 1: install → lockfile entry present; round-trip through TOML."""
    lock = Lockfile(tmp_path / "plugins.lock")
    entry = _make_entry()
    lock.add(entry)

    fresh = Lockfile(tmp_path / "plugins.lock")
    entries = fresh.read()
    assert "github-pr-ops" in entries
    assert entries["github-pr-ops"] == entry


def test_remove_drops_entry(tmp_path: Path) -> None:
    """Test 2: remove → entry gone."""
    lock = Lockfile(tmp_path / "plugins.lock")
    lock.add(_make_entry())
    assert lock.remove("github-pr-ops") is True
    assert lock.read() == {}
    # Removing again is a no-op.
    assert lock.remove("github-pr-ops") is False


def test_lockfile_is_sorted(tmp_path: Path) -> None:
    """Test 3: entries are written in deterministic name-sorted order."""
    lock = Lockfile(tmp_path / "plugins.lock")
    lock.add(_make_entry(name="zebra"))
    lock.add(_make_entry(name="apple"))
    lock.add(_make_entry(name="middle"))

    text = (tmp_path / "plugins.lock").read_text()
    apple_idx = text.find('name = "apple"')
    middle_idx = text.find('name = "middle"')
    zebra_idx = text.find('name = "zebra"')
    assert apple_idx < middle_idx < zebra_idx


def test_lockfile_schema_version_present(tmp_path: Path) -> None:
    """Test 4: lockfile carries a schema_version header."""
    lock = Lockfile(tmp_path / "plugins.lock")
    lock.add(_make_entry())
    text = (tmp_path / "plugins.lock").read_text()
    assert f'schema_version = "{LOCKFILE_SCHEMA_VERSION}"' in text


def test_unsupported_schema_version_rejected(tmp_path: Path) -> None:
    """Test 5: a lockfile with the wrong schema_version raises on read."""
    path = tmp_path / "plugins.lock"
    path.write_text('schema_version = "99.0"\n')
    lock = Lockfile(path)
    with pytest.raises(ValueError, match="unsupported lockfile schema_version"):
        lock.read()


def test_atomic_write_no_partial_file_on_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test 6: simulated crash mid-write leaves the original file intact."""
    lock_path = tmp_path / "plugins.lock"
    lock = Lockfile(lock_path)
    lock.add(_make_entry(name="initial"))

    original = lock_path.read_text()

    # Patch os.replace to fail, simulating a crash before rename.
    import os

    def boom(*args, **kwargs):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="simulated crash"):
        lock.add(_make_entry(name="second"))

    # Original lockfile content must be unchanged.
    assert lock_path.read_text() == original
    # No leftover .plugins.lock.* temp file in the directory.
    leftovers = list(tmp_path.glob(".plugins.lock.*"))
    assert leftovers == []


def _concurrent_writer(target_path_str: str, name: str, count: int) -> None:
    """Worker for concurrent-write test. Adds N entries with distinct names."""
    from pathlib import Path as _Path

    from ouroboros.plugin.lockfile import LockEntry, Lockfile

    lock = Lockfile(_Path(target_path_str))
    for i in range(count):
        lock.add(
            LockEntry(
                name=f"{name}-{i}",
                version="0.1.0",
                source_kind="local",
                repository=None,
                git_sha=None,
                manifest_checksum="sha256:0",
                installed_at="2026-05-08T03:14:00Z",
                plugin_home=f"~/.ouroboros/plugins/{name}-{i}",
            )
        )


def test_concurrent_writes_do_not_corrupt(tmp_path: Path) -> None:
    """Test 7: two processes writing concurrently do not corrupt the file
    or lose entries (POSIX flock holds them serialized)."""
    lock_path = tmp_path / "plugins.lock"
    procs = [
        multiprocessing.Process(target=_concurrent_writer, args=(str(lock_path), "p1", 5)),
        multiprocessing.Process(target=_concurrent_writer, args=(str(lock_path), "p2", 5)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
        assert p.exitcode == 0, f"writer exited {p.exitcode}"

    # Final lockfile is valid TOML and contains all 10 entries.
    entries = Lockfile(lock_path).read()
    assert len(entries) == 10
    assert {e.name for e in entries.values()} == {
        f"{prefix}-{i}" for prefix in ("p1", "p2") for i in range(5)
    }


def test_transaction_allows_nested_add_and_remove(tmp_path: Path) -> None:
    """``Lockfile.transaction()`` holds the file lock across a multi-step
    operation so the caller can combine a lockfile mutation with related
    on-disk work atomically. Inside the ``with`` block, ``add()`` and
    ``remove()`` MUST NOT re-acquire the flock (that would deadlock on
    POSIX where a second ``flock()`` on a different fd against the same
    file blocks). Verifies the re-entrancy guard.
    """
    lock = Lockfile(tmp_path / "plugins.lock")
    with lock.transaction():
        lock.add(_make_entry("github-pr-ops"))
        lock.add(_make_entry("other-plugin"))
        # The transactional remove of one of the entries should also
        # complete without deadlocking on the outer flock.
        removed = lock.remove("other-plugin")
        assert removed is True

    # Outside the transaction the file lock is released and a fresh
    # ``read()`` sees the persisted state.
    entries = Lockfile(tmp_path / "plugins.lock").read()
    assert set(entries) == {"github-pr-ops"}


def test_transaction_serializes_with_external_flock(tmp_path: Path) -> None:
    """A second process MUST block while another holds ``transaction()``.
    This guards the contract that ``remove`` / multi-step lifecycles
    can rely on the lock to suppress concurrent ``install``.
    """
    import fcntl
    import time

    lock = Lockfile(tmp_path / "plugins.lock")
    lock.add(_make_entry("github-pr-ops"))
    lock_path = (tmp_path / "plugins.lock").with_suffix(".lock.lock")

    with lock.transaction():
        # Acquiring a non-blocking exclusive flock from a separate fd
        # MUST fail because the transaction owner is holding the lock.
        with lock_path.open("w") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    # Once we exit the transaction, the same non-blocking acquire
    # succeeds (proves the lock is released, not leaked).
    with lock_path.open("w") as handle:
        # Should not raise.
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    # Touch ``time`` so the import is used (keeps the test self-
    # contained without relying on a real sleep window — the
    # contract is exclusivity, not timing).
    _ = time.monotonic()
