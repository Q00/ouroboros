"""Cross-platform file locking utilities using only the Python standard library."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
from typing import TextIO

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:  # pragma: no branch
    import fcntl


@contextmanager
def file_lock(file_path: Path, exclusive: bool = True) -> Iterator[None]:
    """Context manager for cross-platform file locking."""
    lock_path = file_path.with_suffix(file_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+", encoding="utf-8") as handle:
        _ensure_lockfile_content(handle)
        _acquire_lock(handle, exclusive=exclusive)
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


def _acquire_lock(handle: TextIO, *, exclusive: bool = True) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_RLCK if not exclusive else msvcrt.LK_LOCK, 1)
        return

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)


def _release_lock(handle: TextIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
