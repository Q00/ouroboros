"""Cross-platform file locking utilities using only the Python standard library."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import errno
import os
from pathlib import Path
from typing import TextIO

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:  # pragma: no branch
    import fcntl


@contextmanager
def file_lock(
    file_path: Path,
    exclusive: bool = True,
    *,
    blocking: bool = True,
) -> Iterator[None]:
    """Context manager for cross-platform file locking.

    ``blocking=False`` performs a single acquisition attempt and raises
    :class:`BlockingIOError` when another process holds an incompatible lock.
    """
    lock_path = file_path.with_suffix(file_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as handle:
        _ensure_lockfile_content(handle)
        _acquire_lock(handle, exclusive=exclusive, blocking=blocking)
        try:
            yield
        finally:
            _release_lock(handle)


def _ensure_lockfile_content(handle: TextIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write("0")
        handle.flush()
        handle.seek(0)


def _acquire_lock(
    handle: TextIO,
    *,
    exclusive: bool = True,
    blocking: bool = True,
) -> None:
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            handle.seek(0)
            if blocking:
                mode = msvcrt.LK_LOCK if exclusive else msvcrt.LK_RLCK
            else:
                mode = msvcrt.LK_NBLCK if exclusive else msvcrt.LK_NBRLCK
            msvcrt.locking(handle.fileno(), mode, 1)
            return

        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            mode |= fcntl.LOCK_NB
        fcntl.flock(handle.fileno(), mode)
    except OSError as exc:
        if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            raise BlockingIOError(
                errno.EWOULDBLOCK,
                "file lock is already held by another process",
            ) from exc
        raise


def _release_lock(handle: TextIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
