"""Cross-platform file locking utilities using only the Python standard library."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
from typing import TextIO

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:  # pragma: no branch
    import fcntl


_LOCK_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK})
_DIRECTORY_FD_LOCK_OPEN_SUPPORTED = bool(
    os.name != "nt"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.unlink in os.supports_dir_fd
)


@contextmanager
def file_lock(
    file_path: Path,
    exclusive: bool = True,
    *,
    blocking: bool = True,
    parent_fd: int | None = None,
) -> Iterator[None]:
    """Context manager for blocking or fail-fast cross-platform file locking."""
    lock_path = file_path.with_suffix(file_path.suffix + ".lock")
    if parent_fd is None:
        lock_path.parent.mkdir(parents=True, exist_ok=True)

    with _open_lockfile(lock_path, parent_fd=parent_fd) as handle:
        _ensure_lockfile_content(handle)
        _acquire_lock(handle, exclusive=exclusive, blocking=blocking)
        try:
            yield
        finally:
            _release_lock(handle)


@contextmanager
def _open_lockfile(lock_path: Path, *, parent_fd: int | None = None) -> Iterator[TextIO]:
    """Open one lockfile without following a swapped parent or lock symlink."""
    if _supports_directory_fd_lock_open():
        with _open_lockfile_at(lock_path, directory_fd=parent_fd) as handle:
            yield handle
        return
    if parent_fd is not None:
        raise OSError(
            getattr(errno, "ENOTSUP", errno.EPERM),
            "directory-relative lockfile creation is unavailable",
            str(lock_path),
        )
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        with _open_lockfile_guarded(lock_path) as handle:
            yield handle
        return
    raise OSError(
        getattr(errno, "ENOTSUP", errno.EPERM),
        "safe directory-relative lockfile creation is unavailable",
        str(lock_path),
    )


def _supports_directory_fd_lock_open() -> bool:
    """Return whether Python exposes the required openat-style primitives."""
    return _DIRECTORY_FD_LOCK_OPEN_SUPPORTED


def _validate_lock_parent_binding(directory_fd: int, parent: Path) -> None:
    """Prove a held directory descriptor still names the live lock parent."""
    try:
        opened = os.fstat(directory_fd)
        current = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        raise OSError(
            getattr(errno, "ESTALE", errno.EIO),
            "lockfile parent changed during open",
            str(parent),
        ) from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise OSError(
            getattr(errno, "ESTALE", errno.EIO),
            "lockfile parent changed during open",
            str(parent),
        )


def _open_lockfile_descriptor_at(directory_fd: int, name: str) -> tuple[int, bool]:
    """Open an existing lockfile or create it with unambiguous ownership."""
    flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    for _attempt in range(3):
        try:
            return os.open(name, flags, dir_fd=directory_fd), False
        except FileNotFoundError:
            try:
                return (
                    os.open(
                        name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory_fd,
                    ),
                    True,
                )
            except FileExistsError:
                continue
    raise OSError(errno.EAGAIN, "lockfile changed repeatedly during open", name)


@contextmanager
def _open_lockfile_at(
    lock_path: Path,
    *,
    directory_fd: int | None = None,
) -> Iterator[TextIO]:
    """Open a lockfile relative to a pinned, no-follow parent directory."""
    if directory_fd is None:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            directory_fd = os.open(lock_path.parent, directory_flags)
        except OSError as exc:
            raise OSError(
                exc.errno,
                "lockfile parent is not a safe local directory",
                str(lock_path.parent),
            ) from exc
    else:
        directory_fd = os.dup(directory_fd)

    lock_fd = -1
    created = False
    try:
        _validate_lock_parent_binding(directory_fd, lock_path.parent)
        lock_fd, created = _open_lockfile_descriptor_at(directory_fd, lock_path.name)
        try:
            opened = os.fstat(lock_fd)
            current = os.stat(
                lock_path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            _validate_lock_parent_binding(directory_fd, lock_path.parent)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise OSError(
                    getattr(errno, "ESTALE", errno.EIO),
                    "lockfile changed during open",
                    str(lock_path),
                )
        except BaseException:
            if created:
                try:
                    os.unlink(lock_path.name, dir_fd=directory_fd)
                except OSError:
                    pass
            raise

        with open(lock_fd, "a+", encoding="utf-8", closefd=True) as handle:
            lock_fd = -1
            yield handle
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


@contextmanager
def _windows_directory_lease(path: Path) -> Iterator[None]:
    """Pin a non-reparse directory without allowing rename or deletion."""
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        0,
        0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        raise OSError(ctypes.get_last_error(), "lockfile parent lease failed", str(path))

    try:
        information = _ByHandleFileInformation()
        if (
            not get_information(handle, ctypes.byref(information))
            or information.dwFileAttributes & 0x00000400
            or not information.dwFileAttributes & 0x00000010
        ):
            raise OSError(errno.EPERM, "lockfile parent is a link or junction", str(path))
        yield
    finally:
        close_handle(handle)


@contextmanager
def _open_lockfile_guarded(lock_path: Path) -> Iterator[TextIO]:
    """Open a no-follow Windows lockfile while its parent cannot be replaced."""
    import ctypes
    from ctypes import wintypes
    import msvcrt as windows_msvcrt

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    with _windows_directory_lease(lock_path.parent):
        native_handle = create_file(
            str(lock_path),
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
            None,
            4,  # OPEN_ALWAYS
            0x00000080 | 0x00200000,  # ATTRIBUTE_NORMAL | OPEN_REPARSE_POINT
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if native_handle in {None, invalid_handle}:
            raise OSError(
                ctypes.get_last_error(),
                "lockfile could not be opened without following links",
                str(lock_path),
            )

        fd = -1
        try:
            information = _ByHandleFileInformation()
            if (
                not get_information(native_handle, ctypes.byref(information))
                or information.dwFileAttributes & 0x00000400
                or information.dwFileAttributes & 0x00000010
            ):
                raise OSError(
                    errno.EPERM,
                    "lockfile must be a regular non-reparse file",
                    str(lock_path),
                )
            fd = windows_msvcrt.open_osfhandle(
                int(native_handle),
                os.O_RDWR | os.O_APPEND,
            )
            native_handle = None
            with open(fd, "a+", encoding="utf-8", closefd=True) as handle:
                fd = -1
                opened = os.fstat(handle.fileno())
                current = lock_path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or not stat.S_ISREG(current.st_mode)
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                ):
                    raise OSError(errno.EPERM, "lockfile changed during open", str(lock_path))
                yield handle
        finally:
            if fd >= 0:
                os.close(fd)
            if native_handle not in {None, invalid_handle}:
                close_handle(native_handle)


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
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        handle.seek(0)
        if exclusive:
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        else:
            mode = msvcrt.LK_RLCK if blocking else msvcrt.LK_NBRLCK
        try:
            msvcrt.locking(handle.fileno(), mode, 1)
        except OSError as exc:
            if not blocking and exc.errno in _LOCK_BUSY_ERRNOS:
                raise BlockingIOError(exc.errno, "file lock is already held") from exc
            raise
        return

    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        mode |= fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), mode)
    except OSError as exc:
        if not blocking and exc.errno in _LOCK_BUSY_ERRNOS:
            raise BlockingIOError(exc.errno, "file lock is already held") from exc
        raise


def _release_lock(handle: TextIO) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
