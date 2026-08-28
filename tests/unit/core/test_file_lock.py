"""Tests for stdlib-backed file locking."""

from __future__ import annotations

from contextvars import copy_context
import errno
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import ouroboros.core.file_lock as file_lock_module
from ouroboros.core.file_lock import (
    _acquire_lock,
    _release_lock,
    _run_release_steps,
    file_lock,
)


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


def _enter_in_copied_context(manager: object) -> None:
    """Enter a context manager under a copied context, as asyncio.to_thread does."""
    copy_context().run(manager.__enter__)  # type: ignore[attr-defined]


def _fail_next_parent_authority_close(
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    """Raise after closing the next stable-parent authority descriptor."""
    authority_fd: list[int] = []
    failed_closes: list[int] = []
    original_acquire = file_lock_module._acquire_posix_lock
    original_close = file_lock_module.os.close

    def record_acquire(
        file_descriptor: int,
        *,
        exclusive: bool,
        blocking: bool,
    ) -> None:
        if not authority_fd:
            authority_fd.append(file_descriptor)
        original_acquire(
            file_descriptor,
            exclusive=exclusive,
            blocking=blocking,
        )

    def fail_close(file_descriptor: int) -> None:
        original_close(file_descriptor)
        if authority_fd == [file_descriptor] and not failed_closes:
            failed_closes.append(file_descriptor)
            raise OSError(errno.EIO, "directory close failed")

    monkeypatch.setattr(file_lock_module, "_acquire_posix_lock", record_acquire)
    monkeypatch.setattr(file_lock_module.os, "close", fail_close)
    return failed_closes


@pytest.mark.skipif(os.name == "nt", reason="POSIX stable-parent authority only")
def test_cross_context_exit_releases_authority_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A lock entered and exited in different contexts must still unwind cleanly.

    ContextVar tokens are context-bound, so resetting one from a foreign context
    raises ValueError. That must neither surface to the caller nor skip the
    explicit release of the parent authority lock.
    """
    target = tmp_path / "state.json"
    released: list[int] = []
    original_release = file_lock_module._release_posix_lock

    def record_release(file_descriptor: int) -> None:
        released.append(file_descriptor)
        original_release(file_descriptor)

    monkeypatch.setattr(file_lock_module, "_release_posix_lock", record_release)

    manager = file_lock(target, stable_parent_authority=True)
    _enter_in_copied_context(manager)

    assert manager.__exit__(None, None, None) in {None, False}
    # Both the lockfile lock and the parent authority lock are released
    # explicitly; before the fix the parent release was skipped entirely.
    assert len(released) == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX stable-parent authority only")
def test_cross_context_exit_does_not_mask_body_exception(tmp_path: Path) -> None:
    """A foreign-context token must not replace the caller's real exception."""
    target = tmp_path / "state.json"

    manager = file_lock(target, stable_parent_authority=True)
    _enter_in_copied_context(manager)

    body_error = RuntimeError("the caller's real failure")
    handled = manager.__exit__(type(body_error), body_error, body_error.__traceback__)

    # Exiting must not raise on its own, and must not claim to have handled the
    # body error -- the caller keeps propagating its own exception.
    assert not handled


@pytest.mark.skipif(os.name == "nt", reason="POSIX stable-parent authority only")
def test_cross_context_exit_leaves_authority_reacquirable(tmp_path: Path) -> None:
    """After a cross-context exit the parent authority must be free again."""
    target = tmp_path / "state.json"
    contender = tmp_path / "contender.json"

    manager = file_lock(target, stable_parent_authority=True)
    _enter_in_copied_context(manager)
    manager.__exit__(None, None, None)

    with file_lock(contender, blocking=False, stable_parent_authority=True):
        pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX stable-parent authority only")
def test_parent_directory_close_failure_does_not_mask_body_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    contender = tmp_path / "contender.json"
    failed_closes = _fail_next_parent_authority_close(monkeypatch)

    with pytest.raises(RuntimeError, match="the caller's real failure"):
        with file_lock(target, stable_parent_authority=True):
            raise RuntimeError("the caller's real failure")

    assert len(failed_closes) == 1
    with file_lock(contender, blocking=False, stable_parent_authority=True):
        pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX stable-parent authority only")
def test_parent_directory_close_failure_surfaces_after_clean_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    contender = tmp_path / "contender.json"
    failed_closes = _fail_next_parent_authority_close(monkeypatch)

    with pytest.raises(OSError, match="directory close failed") as raised:
        with file_lock(target, stable_parent_authority=True):
            pass

    assert raised.value.errno == errno.EIO
    assert len(failed_closes) == 1
    with file_lock(contender, blocking=False, stable_parent_authority=True):
        pass


def test_release_failure_does_not_mask_body_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"

    def failing_release(handle: object) -> None:
        raise OSError(errno.EBADF, "bad descriptor")

    monkeypatch.setattr(file_lock_module, "_release_lock", failing_release)

    with pytest.raises(RuntimeError, match="the caller's real failure"):
        with file_lock(target):
            raise RuntimeError("the caller's real failure")


def test_release_failure_surfaces_when_body_succeeded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Suppression is scoped to masking only; a lone release fault still raises."""
    target = tmp_path / "state.json"

    def failing_release(handle: object) -> None:
        raise OSError(errno.EBADF, "bad descriptor")

    monkeypatch.setattr(file_lock_module, "_release_lock", failing_release)

    with pytest.raises(OSError) as raised:
        with file_lock(target):
            pass

    assert raised.value.errno == errno.EBADF


def test_run_release_steps_attempts_every_step_and_reraises_first_error() -> None:
    attempted: list[str] = []

    def first() -> None:
        attempted.append("first")
        raise OSError(errno.EBADF, "first failed")

    def second() -> None:
        attempted.append("second")
        raise ValueError("second failed")

    def third() -> None:
        attempted.append("third")

    with pytest.raises(OSError, match="first failed"):
        _run_release_steps(first, second, third, suppress_errors=False)

    assert attempted == ["first", "second", "third"]


def test_run_release_steps_suppresses_errors_for_a_failing_body() -> None:
    attempted: list[str] = []

    def failing() -> None:
        attempted.append("failing")
        raise OSError(errno.EBADF, "release failed")

    def following() -> None:
        attempted.append("following")

    _run_release_steps(failing, following, suppress_errors=True)

    assert attempted == ["failing", "following"]


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
