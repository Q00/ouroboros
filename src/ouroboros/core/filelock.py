"""Cross-platform file locking utilities.

Provides a unified locking interface using:
- fcntl.flock() on Unix/macOS
- msvcrt.locking() on Windows
"""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
    import os as _os

    def _lock_fd(fd: int, exclusive: bool) -> None:
        """Acquire a lock on a file descriptor.

        On Windows, msvcrt.locking() always acquires an exclusive lock
        regardless of the exclusive parameter (no shared-read mode).
        """
        _os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _unlock_fd(fd: int) -> None:
        """Release a lock on a file descriptor."""
        _os.lseek(fd, 0, 0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_fd(fd: int, exclusive: bool) -> None:
        """Acquire a shared or exclusive lock on a file descriptor."""
        lock_type = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, lock_type)

    def _unlock_fd(fd: int) -> None:
        """Release a lock on a file descriptor."""
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _file_lock(file_path: Path, exclusive: bool = True) -> Iterator[None]:
    """Cross-platform file lock context manager using a companion lock file.

    Uses fcntl.flock() on Unix/macOS and msvcrt.locking() on Windows.

    Args:
        file_path: Path to the file being protected. A companion .lock
                   file is created alongside it.
        exclusive: If True (default), acquire an exclusive write lock.
                   If False, acquire a shared read lock (Windows always
                   uses exclusive regardless).
    """
    lock_path = file_path.with_suffix(file_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Open in binary mode and write a sentinel byte so Windows has a byte
    # range to lock via msvcrt.locking(). On Unix the content is irrelevant.
    with open(lock_path, "wb+") as lock_file:
        lock_file.write(b"\x00")
        lock_file.flush()
        fd = lock_file.fileno()
        _lock_fd(fd, exclusive)
        try:
            yield
        finally:
            _unlock_fd(fd)
