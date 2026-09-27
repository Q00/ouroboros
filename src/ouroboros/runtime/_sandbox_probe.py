"""The mutation matrix a sandbox backend must deny outside its writable root.

Run as a standalone script (standard library only):

    python -I -S -B _sandbox_probe.py INSIDE OUTSIDE

It tries every mutation class on ``OUTSIDE`` (a directory prepared by
``prepare``) and a few ordinary writes inside ``INSIDE``, and prints one JSON
object: ``{"outside": {class: "ok" | "denied" | "unsupported"}, "inside": ...}``.
``unsupported`` means the platform or file system lacks the operation
(``ENOSYS``, ``ENOTSUP``, ``ENOTTY``, a missing ``os`` function).

``runtime.exec_sandbox.filesystem_backend`` runs it twice: unconfined, where
every ``REQUIRED`` class must be ``ok`` (so the probe can prove anything), and
confined, where every class that was ``ok`` unconfined must be ``denied``, the
inside writes must be ``ok``, and ``snapshot`` of the outside directory must be
unchanged. Otherwise the backend is reported unavailable.
"""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import errno
import json
import os
import stat
import sys

# Classes every backend must be able to deny; the probe is invalid (and the
# backend unavailable) if the unconfined run cannot perform one of them.
REQUIRED = (
    "create",
    "append",
    "truncate",
    "unlink",
    "rename_out",
    "rename_in",
    "mkdir",
    "rmdir",
    "symlink",
    "hardlink",
    "chmod",
    "fchmod_readonly_fd",
    "chown_to_self",
    "utime",
)
_UNSUPPORTED = {errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOTTY}
_AT_FDCWD = -100
_FS_IOC_GETFLAGS = 0x80086601
_FS_IOC_SETFLAGS = 0x40086602
_FS_NODUMP_FL = 0x00000040


def prepare(outside: str) -> None:
    """Lay out the victims in ``outside`` (run by the controller, unconfined)."""
    for name in ("victim", "unlink_me", "rename_me", "link_source"):
        with open(os.path.join(outside, name), "w") as handle:
            handle.write("keep")
        os.chmod(os.path.join(outside, name), 0o600)
    os.mkdir(os.path.join(outside, "rmdir_me"))


def snapshot(outside: str) -> dict[str, object]:
    """Every entry under ``outside`` with its type, mode, times, content and xattrs."""
    entries: dict[str, object] = {}
    for name in sorted(os.listdir(outside)):
        path = os.path.join(outside, name)
        status = os.lstat(path)
        content = None
        if stat.S_ISREG(status.st_mode):
            with open(path, "rb") as handle:
                content = handle.read().decode("latin-1")
        xattrs: list[str] = []
        if hasattr(os, "listxattr"):
            try:
                xattrs = sorted(os.listxattr(path, follow_symlinks=False))
            except OSError:
                pass
        entries[name] = [
            status.st_mode,
            status.st_uid,
            status.st_nlink,
            status.st_mtime_ns,
            content,
            xattrs,
            getattr(status, "st_flags", 0),
        ]
    return entries


def _setxattr(path: str) -> None:
    if not hasattr(os, "setxattr"):
        raise OSError(errno.ENOTSUP, "no setxattr")
    os.setxattr(path, "user.ouroboros_probe", b"1")


def _chflags(path: str) -> None:
    if not hasattr(os, "chflags"):
        raise OSError(errno.ENOTSUP, "no chflags")
    os.chflags(path, stat.UF_NODUMP)


def _syscall(number: int, *args: object) -> None:
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOSYS, "linux only")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    if libc.syscall(ctypes.c_long(number), *args) < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _fchmodat2(path: str) -> None:
    _syscall(
        452,
        ctypes.c_int(_AT_FDCWD),
        ctypes.c_char_p(path.encode()),
        ctypes.c_uint(0o640),
        ctypes.c_uint(0),
    )


def _setxattrat(path: str) -> None:
    # struct xattr_args { u64 value; u32 size; u32 flags; }
    value = ctypes.create_string_buffer(b"1", 1)
    args = (ctypes.c_uint64 * 2)(ctypes.addressof(value), 1)
    _syscall(
        463,
        ctypes.c_int(_AT_FDCWD),
        ctypes.c_char_p(path.encode()),
        ctypes.c_uint(0),
        ctypes.c_char_p(b"user.ouroboros_probe_at"),
        ctypes.byref(args),
        ctypes.c_size_t(16),
    )


def _set_inode_flags(path: str) -> None:
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOTSUP, "linux only")
    import fcntl

    fd = os.open(path, os.O_RDONLY)
    try:
        flags = ctypes.c_long(0)
        fcntl.ioctl(fd, _FS_IOC_GETFLAGS, flags)
        flags.value |= _FS_NODUMP_FL
        fcntl.ioctl(fd, _FS_IOC_SETFLAGS, flags)
    finally:
        os.close(fd)


def _fchmod_readonly(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fchmod(fd, 0o640)
    finally:
        os.close(fd)


def _append(path: str) -> None:
    with open(path, "a") as handle:
        handle.write("x")


def _create(path: str) -> None:
    with open(path, "w") as handle:
        handle.write("x")


def _outside_attempts(inside: str, outside: str) -> dict[str, Callable[[], object]]:
    victim = os.path.join(outside, "victim")
    return {
        "create": lambda: _create(os.path.join(outside, "created")),
        "append": lambda: _append(victim),
        "truncate": lambda: os.truncate(victim, 0),
        "unlink": lambda: os.unlink(os.path.join(outside, "unlink_me")),
        "rename_out": lambda: os.rename(
            os.path.join(inside, "move_me"), os.path.join(outside, "moved_in")
        ),
        "rename_in": lambda: os.rename(
            os.path.join(outside, "rename_me"), os.path.join(inside, "taken")
        ),
        "mkdir": lambda: os.mkdir(os.path.join(outside, "made")),
        "rmdir": lambda: os.rmdir(os.path.join(outside, "rmdir_me")),
        "symlink": lambda: os.symlink("victim", os.path.join(outside, "linked")),
        "hardlink": lambda: os.link(
            os.path.join(outside, "link_source"), os.path.join(inside, "hardlinked")
        ),
        "chmod": lambda: os.chmod(victim, 0o644),
        "fchmod_readonly_fd": lambda: _fchmod_readonly(victim),
        "fchmodat2": lambda: _fchmodat2(victim),
        "chown_to_self": lambda: os.chown(victim, os.getuid(), -1),
        "utime": lambda: os.utime(victim, (0, 0)),
        "setxattr": lambda: _setxattr(victim),
        "setxattrat": lambda: _setxattrat(victim),
        "chflags": lambda: _chflags(victim),
        "inode_flags_ioctl": lambda: _set_inode_flags(victim),
    }


def _inside_attempts(inside: str) -> dict[str, Callable[[], object]]:
    return {
        "create": lambda: _create(os.path.join(inside, "created")),
        "mkdir": lambda: os.mkdir(os.path.join(inside, "made")),
        "rename": lambda: os.rename(
            os.path.join(inside, "created"), os.path.join(inside, "made", "renamed")
        ),
        "unlink": lambda: os.unlink(os.path.join(inside, "made", "renamed")),
    }


def _outcome(attempt: Callable[[], object]) -> str:
    try:
        attempt()
    except OSError as exc:
        return "unsupported" if exc.errno in _UNSUPPORTED else "denied"
    return "ok"


def run(inside: str, outside: str) -> dict[str, dict[str, str]]:
    _create(os.path.join(inside, "move_me"))
    outside_results = {
        name: _outcome(attempt) for name, attempt in _outside_attempts(inside, outside).items()
    }
    inside_results = {name: _outcome(attempt) for name, attempt in _inside_attempts(inside).items()}
    return {"outside": outside_results, "inside": inside_results}


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1], sys.argv[2])))
