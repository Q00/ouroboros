"""Tests for stdlib-backed file locking."""

from __future__ import annotations

import errno
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
