"""Cross-platform file locking utilities.

Replaces Unix-only fcntl.flock() with filelock for Windows compatibility.
Provides the same context manager API used throughout the codebase.

The filelock library uses OS-level locking (fcntl on Unix, msvcrt on Windows)
with automatic fallback. Lock files are created alongside the target file
with a .lock extension.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock


@contextmanager
def file_lock(file_path: Path, exclusive: bool = True) -> Iterator[None]:  # noqa: ARG001
    """Context manager for cross-platform file locking.

    Creates a .lock file alongside the target file. Both shared and exclusive
    lock requests use filelock's exclusive locking, which provides sufficient
    protection for the JSON state file access patterns in this codebase
    (brief reads and writes of small files).

    Args:
        file_path: Path to the file to lock (lock file is {path}.lock).
        exclusive: Kept for API compatibility with existing call sites.
                   filelock always uses exclusive locks, which is safe
                   for this project's access patterns.

    Yields:
        None when lock is acquired.
    """
    lock_path = file_path.with_suffix(file_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    lock = FileLock(lock_path)
    with lock:
        yield
