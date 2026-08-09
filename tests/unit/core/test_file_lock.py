"""Tests for stdlib-backed file locking."""

from __future__ import annotations

from contextvars import copy_context
import errno
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import ouroboros.core.file_lock as file_lock_module
from ouroboros.core.file_lock import _acquire_lock, _release_lock, file_lock


def test_file_lock_creates_lockfile(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("{}")

    with file_lock(target):
        lock_path = target.with_suffix(".json.lock")
        assert lock_path.exists()
        assert lock_path.read_text() == "0"


def test_file_lock_exclusive_false_acquires_shared_lock(tmp_path: Path) -> None:
    """Shared (non-exclusive) locks should allow concurrent readers."""
    target = tmp_path / "data.json"
    target.write_text("{}")

    with file_lock(target, exclusive=False):
        lock_path = target.with_suffix(".json.lock")
        assert lock_path.exists()
        # A second shared lock on the same file should not block
        with file_lock(target, exclusive=False):
            assert lock_path.exists()


def test_file_lock_windows_shared_uses_read_lock_mode(monkeypatch, tmp_path: Path) -> None:
    """On Windows, non-exclusive lock requests should use a shared/read mode."""
    target = tmp_path / "shared.json"
    target.write_text("{}")
    with target.open("a+", encoding="utf-8") as handle:
        fd = handle.fileno()
        mock_msvcrt = MagicMock()
        mock_msvcrt.LK_LOCK = 1
        mock_msvcrt.LK_RLCK = 2
        mock_msvcrt.LK_UNLCK = 3
        monkeypatch.setattr(file_lock_module, "msvcrt", mock_msvcrt, raising=False)
        monkeypatch.setattr(file_lock_module.os, "name", "nt")

        _acquire_lock(handle, exclusive=False)
        _release_lock(handle)

    mock_msvcrt.locking.assert_any_call(fd, mock_msvcrt.LK_RLCK, 1)
    mock_msvcrt.locking.assert_any_call(fd, mock_msvcrt.LK_UNLCK, 1)


def test_file_lock_nonblocking_exclusive_fails_fast(tmp_path: Path) -> None:
    target = tmp_path / "claimed.json"

    with file_lock(target):
        with pytest.raises(BlockingIOError):
            with file_lock(target, blocking=False):
                pytest.fail("a held exclusive lock must not be reacquired")


def test_sibling_file_locks_do_not_implicitly_lock_their_parent(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    with file_lock(first):
        with file_lock(second, blocking=False):
            pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX stable-parent authority only")
def test_stable_parent_authority_is_reentrant_in_one_context(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    with file_lock(first, stable_parent_authority=True):
        with file_lock(
            second,
            blocking=False,
            stable_parent_authority=True,
        ):
            pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX stable-parent authority only")
def test_copied_context_cannot_reuse_released_parent_authority(tmp_path: Path) -> None:
    original = tmp_path / "original.json"
    current = tmp_path / "current.json"
    contender = tmp_path / "contender.json"

    with file_lock(original, stable_parent_authority=True):
        stale_context = copy_context()

    def acquire_from_stale_context() -> None:
        with file_lock(
            contender,
            blocking=False,
            stable_parent_authority=True,
        ):
            pytest.fail("a released copied context must not retain parent authority")

    with file_lock(current, stable_parent_authority=True):
        with pytest.raises(BlockingIOError):
            stale_context.run(acquire_from_stale_context)


def test_file_lock_windows_nonblocking_uses_fail_fast_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "nonblocking.json"
    target.write_text("{}")
    with target.open("a+", encoding="utf-8") as handle:
        fd = handle.fileno()
        mock_msvcrt = MagicMock()
        mock_msvcrt.LK_LOCK = 1
        mock_msvcrt.LK_RLCK = 2
        mock_msvcrt.LK_UNLCK = 3
        mock_msvcrt.LK_NBLCK = 4
        mock_msvcrt.LK_NBRLCK = 5
        monkeypatch.setattr(file_lock_module, "msvcrt", mock_msvcrt, raising=False)
        monkeypatch.setattr(file_lock_module.os, "name", "nt")

        _acquire_lock(handle, exclusive=True, blocking=False)
        _release_lock(handle)

    mock_msvcrt.locking.assert_any_call(fd, mock_msvcrt.LK_NBLCK, 1)
    mock_msvcrt.locking.assert_any_call(fd, mock_msvcrt.LK_UNLCK, 1)


def test_file_lock_nonblocking_preserves_unexpected_os_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "broken.json"
    target.write_text("{}")
    with target.open("a+", encoding="utf-8") as handle:
        mock_msvcrt = MagicMock()
        mock_msvcrt.LK_NBLCK = 4
        mock_msvcrt.locking.side_effect = OSError(errno.EBADF, "bad descriptor")
        monkeypatch.setattr(file_lock_module, "msvcrt", mock_msvcrt, raising=False)
        monkeypatch.setattr(file_lock_module.os, "name", "nt")

        with pytest.raises(OSError) as raised:
            _acquire_lock(handle, exclusive=True, blocking=False)

    assert not isinstance(raised.value, BlockingIOError)
    assert raised.value.errno == errno.EBADF


def test_file_lock_creation_never_follows_parent_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Mutation inside the creating open cannot create an external lockfile."""
    if not file_lock_module._supports_directory_fd_lock_open():
        pytest.skip("directory-relative lockfile creation is unavailable")

    parent = tmp_path / "local"
    parent.mkdir()
    target = parent / "state.json"
    lock_name = target.with_suffix(".json.lock").name
    external = tmp_path / "external"
    displaced = tmp_path / "displaced"
    external.mkdir()
    original_open = os.open
    original_rename = os.rename
    swapped = False

    def swap_parent_during_create(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if dir_fd is not None and path == lock_name and flags & os.O_CREAT and not swapped:
            original_rename(parent, displaced)
            try:
                parent.symlink_to(external, target_is_directory=True)
            except OSError:
                pytest.skip("directory symlinks are not supported in this environment")
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_parent_during_create)

    with pytest.raises(OSError, match="parent changed"):
        with file_lock(target):
            pytest.fail("a swapped lock parent must fail closed")

    assert swapped
    assert list(external.iterdir()) == []
    assert list(displaced.iterdir()) == []


def test_file_lock_rejects_existing_lockfile_symlink(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    lock_path = target.with_suffix(".json.lock")
    external = tmp_path / "external.lock"
    external.write_text("outside", encoding="utf-8")
    try:
        lock_path.symlink_to(external)
    except OSError:
        pytest.skip("file symlinks are not supported in this environment")

    with pytest.raises(OSError):
        with file_lock(target):
            pytest.fail("a lockfile symlink must fail closed")

    assert external.read_text(encoding="utf-8") == "outside"
