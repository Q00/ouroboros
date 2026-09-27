"""Finish confining this process, then exec the command with its environment.

Run as a standalone script, never imported into the controller:

    python -I -S -B _confine_exec.py [--loopback-up] [--require-loopback-only] [--landlock]
        --root DIR DEV INO ... -- ARGV...

It is the last step of every ``ouroboros.runtime.exec_sandbox`` backend. It is
started with a fixed bootstrap environment, so nothing the command's own
environment names (``LD_PRELOAD``, ``DYLD_INSERT_LIBRARIES``, ...) can run
before confinement. The command's environment arrives as JSON in
``OUROBOROS_SANDBOX_COMMAND_ENV`` and is applied only by the final
``execvpe``, after:

- on every platform, each writable root (``--root DIR DEV INO``) is opened
  without following a symlink and must still be the directory ``confine``
  validated (same device and inode), or nothing runs; and once the
  restriction below is in place, no regular file beneath a root may have
  another hard link (it may be outside), or nothing runs;
- on Linux (``--landlock``), a Landlock ruleset that handles every filesystem
  right that creates, changes, truncates or removes something and grants them
  only beneath those verified root descriptors (plus writing to ``/dev/null`` and a
  few other character devices). Reading and executing are not handled. Landlock
  ABI 3 (Linux 6.2) is the minimum: below it, truncation cannot be denied.
  The restriction is inherited by everything the command execs or forks, and
  Landlock also denies ptrace-mode access (``/proc/<pid>/environ``, ``mem``,
  ``maps``) to processes outside the domain; then the metadata seccomp
  filter below;
- on macOS, nothing more: ``sandbox-exec`` already confined this process.

It depends on nothing but the standard library, so it starts with ``-S`` (no
``site``) and ``-I`` (no environment-controlled import paths). Every failure
exits before the command runs: status 125 when the sandbox could not be
applied, 126 or 127 when the command could not be executed.
"""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import json
import os
import stat
import struct
import sys
from typing import Any

# Generic syscall numbers, the same on x86_64 and aarch64 (and every other
# architecture that uses the unified table).
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
# ``os.O_PATH`` exists only on Linux builds of Python.
_O_PATH: int = getattr(os, "O_PATH", 0o10000000)

_ACCESS_FS_WRITE_FILE = 1 << 1
_ACCESS_FS_REMOVE_DIR = 1 << 4
_ACCESS_FS_REMOVE_FILE = 1 << 5
_ACCESS_FS_MAKE_CHAR = 1 << 6
_ACCESS_FS_MAKE_DIR = 1 << 7
_ACCESS_FS_MAKE_REG = 1 << 8
_ACCESS_FS_MAKE_SOCK = 1 << 9
_ACCESS_FS_MAKE_FIFO = 1 << 10
_ACCESS_FS_MAKE_BLOCK = 1 << 11
_ACCESS_FS_MAKE_SYM = 1 << 12
_ACCESS_FS_REFER = 1 << 13  # ABI 2
_ACCESS_FS_TRUNCATE = 1 << 14  # ABI 3

# ABI 1 rights that change the filesystem.
_WRITE_ACCESS_ABI1 = (
    _ACCESS_FS_WRITE_FILE
    | _ACCESS_FS_REMOVE_DIR
    | _ACCESS_FS_REMOVE_FILE
    | _ACCESS_FS_MAKE_CHAR
    | _ACCESS_FS_MAKE_DIR
    | _ACCESS_FS_MAKE_REG
    | _ACCESS_FS_MAKE_SOCK
    | _ACCESS_FS_MAKE_FIFO
    | _ACCESS_FS_MAKE_BLOCK
    | _ACCESS_FS_MAKE_SYM
)

# Devices a process may write without leaving a trace outside the sandbox.
WRITABLE_DEVICES = (
    "/dev/null",
    "/dev/zero",
    "/dev/full",
    "/dev/random",
    "/dev/urandom",
    "/dev/tty",
)

# Below ABI 3, Landlock cannot deny truncating a file outside the writable roots.
MIN_LANDLOCK_ABI = 3
COMMAND_ENV_VARIABLE = "OUROBOROS_SANDBOX_COMMAND_ENV"

EXIT_SANDBOX_FAILED = 125
EXIT_NOT_EXECUTABLE = 126
EXIT_NOT_FOUND = 127


class _RulesetAttr(ctypes.Structure):
    # The ABI 1 layout; newer fields (network, scopes) are left unset, which
    # the kernel accepts for a shorter structure.
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class SandboxError(Exception):
    """Landlock could not be applied; the command must not run."""


def handled_write_access(abi: int) -> int:
    """The write rights to handle under Landlock ABI ``abi`` (0 below ABI 1)."""
    if abi < 1:
        return 0
    access = _WRITE_ACCESS_ABI1
    if abi >= 2:
        access |= _ACCESS_FS_REFER
    if abi >= 3:
        access |= _ACCESS_FS_TRUNCATE
    return access


def root_write_access(abi: int) -> int:
    """The rights granted beneath a writable root: every handled right except
    creating a character or block device node, which would alias a device."""
    return handled_write_access(abi) & ~(_ACCESS_FS_MAKE_CHAR | _ACCESS_FS_MAKE_BLOCK)


def device_write_access(abi: int) -> int:
    """The rights granted on a writable device (file rights only)."""
    return _ACCESS_FS_WRITE_FILE | (_ACCESS_FS_TRUNCATE if abi >= 3 else 0)


def _syscall() -> Callable[..., Any]:
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.syscall
    function.restype = ctypes.c_long
    return function


def _check(result: int, what: str) -> int:
    if result < 0:
        errno = ctypes.get_errno()
        raise SandboxError(f"{what} failed: {os.strerror(errno)} (errno {errno})")
    return result


def landlock_abi() -> int:
    """The running kernel's Landlock ABI version; 0 when Landlock is unavailable."""
    if not sys.platform.startswith("linux"):
        return 0
    result = _syscall()(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(None),
        ctypes.c_size_t(0),
        ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    return max(int(result), 0)


def _add_rule_fd(
    syscall: Callable[..., Any], ruleset: int, fd: int, access: int, what: str
) -> None:
    # struct landlock_path_beneath_attr is packed: u64 allowed_access, s32 parent_fd.
    attr = ctypes.create_string_buffer(struct.pack("=Qi", access, fd), 12)
    _check(
        syscall(
            _SYS_LANDLOCK_ADD_RULE,
            ctypes.c_int(ruleset),
            ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
            ctypes.byref(attr),
            ctypes.c_uint32(0),
        ),
        f"landlock_add_rule({what})",
    )


def _add_rule(syscall: Callable[..., Any], ruleset: int, path: str, access: int) -> None:
    fd = os.open(path, _O_PATH | os.O_CLOEXEC)
    try:
        _add_rule_fd(syscall, ruleset, fd, access, path)
    finally:
        os.close(fd)


def open_verified_roots(roots: list[tuple[str, int, int]]) -> list[int]:
    """Open each writable root without following a symlink and check its identity.

    ``roots`` are ``(path, st_dev, st_ino)`` as ``exec_sandbox.confine``
    validated them. A root that was renamed, replaced or swapped for a symlink
    since then fails here and the command never runs. On Linux the returned
    descriptors are the very objects the Landlock rules are bound to.
    """
    flags = os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC
    flags |= _O_PATH if sys.platform.startswith("linux") else os.O_RDONLY
    opened: list[int] = []
    try:
        for path, device, inode in roots:
            try:
                fd = os.open(path, flags)
            except OSError as exc:
                raise SandboxError(
                    f"writable root {path} cannot be opened: {exc.strerror}"
                ) from None
            opened.append(fd)
            status = os.fstat(fd)
            if (status.st_dev, status.st_ino) != (device, inode):
                raise SandboxError(f"writable root {path} is not the directory that was confined")
    except BaseException:
        for fd in opened:
            os.close(fd)
        raise
    return opened


def refuse_root_aliases(root_fds: list[int]) -> None:
    """Refuse to run when anything beneath a verified root aliases outside storage.

    Aliases: a regular file with another hard link (the other link may be
    outside the roots) and a character or block device node (writing it
    writes the device).

    The walk must be complete: a directory that cannot be listed or an entry
    that cannot be examined refuses too, since it may hold such a link.

    The other link may be outside the roots, and writing through the root
    would change that file. Walked from the verified descriptors, without
    following symlinks, after the restriction is in place and immediately
    before exec, so a link added after ``confine`` is seen; the confined
    command itself cannot add one (linking an outside file in is denied).
    """

    def unreadable(error: OSError) -> None:
        raise SandboxError(
            f"a writable root cannot be fully inspected for aliases: {error}"
        ) from None

    for fd in root_fds:
        walk = os.fwalk(".", dir_fd=fd, follow_symlinks=False, onerror=unreadable)
        for dirpath, _dirnames, filenames, dirfd in walk:
            for name in filenames:
                try:
                    status = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                except OSError as exc:
                    unreadable(exc)
                if stat.S_ISREG(status.st_mode) and status.st_nlink > 1:
                    raise SandboxError(
                        f"{os.path.join(dirpath, name)} in a writable root has "
                        f"{status.st_nlink} hard links"
                    )
                if stat.S_ISCHR(status.st_mode) or stat.S_ISBLK(status.st_mode):
                    raise SandboxError(
                        f"{os.path.join(dirpath, name)} in a writable root is a device node"
                    )


def restrict_writes(root_fds: list[int]) -> int:
    """Allow writes only beneath the verified root descriptors; return the ABI."""
    abi = landlock_abi()
    if abi < MIN_LANDLOCK_ABI:
        raise SandboxError(
            f"Landlock ABI {abi} is below {MIN_LANDLOCK_ABI} (unsupported, disabled, "
            "or unable to deny truncation)"
        )
    syscall = _syscall()
    handled = handled_write_access(abi)
    attr = _RulesetAttr(handled_access_fs=handled)
    ruleset = _check(
        syscall(
            _SYS_LANDLOCK_CREATE_RULESET,
            ctypes.byref(attr),
            ctypes.c_size_t(ctypes.sizeof(attr)),
            ctypes.c_uint32(0),
        ),
        "landlock_create_ruleset",
    )
    try:
        for fd in root_fds:
            _add_rule_fd(syscall, ruleset, fd, root_write_access(abi), f"root fd {fd}")
        for device in WRITABLE_DEVICES:
            if os.path.exists(device):
                _add_rule(syscall, ruleset, device, device_write_access(abi))
        libc = ctypes.CDLL(None, use_errno=True)
        _check(
            libc.prctl(
                ctypes.c_int(_PR_SET_NO_NEW_PRIVS),
                ctypes.c_ulong(1),
                ctypes.c_ulong(0),
                ctypes.c_ulong(0),
                ctypes.c_ulong(0),
            ),
            "prctl(PR_SET_NO_NEW_PRIVS)",
        )
        _check(
            syscall(_SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)),
            "landlock_restrict_self",
        )
    finally:
        os.close(ruleset)
    return abi


# Landlock mediates creating, writing, truncating, removing, renaming and
# linking, but not changing an existing inode's metadata: mode, ownership,
# timestamps, extended attributes and inode flags. A seccomp filter denies those
# syscalls with EPERM. It cannot see paths, so the denial is global, inside the
# writable roots too. Numbers come from the kernel's syscall tables
# (arch/x86/entry/syscalls/syscall_64.tbl and scripts/syscall.tbl, v6.15);
# syscalls numbered 424 and up share one number on every architecture. The
# backend probe (``_sandbox_probe.py``) checks each class against a real file,
# so a wrong number reports the sandbox unavailable instead of weakening it.
_METADATA_SYSCALLS: dict[str, dict[str, int]] = {
    "x86_64": {
        "chmod": 90,
        "fchmod": 91,
        "fchmodat": 268,
        "chown": 92,
        "fchown": 93,
        "lchown": 94,
        "fchownat": 260,
        "utime": 132,
        "utimes": 235,
        "futimesat": 261,
        "utimensat": 280,
        "setxattr": 188,
        "lsetxattr": 189,
        "fsetxattr": 190,
        "removexattr": 197,
        "lremovexattr": 198,
        "fremovexattr": 199,
    },
    "aarch64": {
        "fchmod": 52,
        "fchmodat": 53,
        "fchown": 55,
        "fchownat": 54,
        "utimensat": 88,
        "setxattr": 5,
        "lsetxattr": 6,
        "fsetxattr": 7,
        "removexattr": 14,
        "lremovexattr": 15,
        "fremovexattr": 16,
    },
}
_UNIFIED_METADATA_SYSCALLS = {
    "fchmodat2": 452,
    "setxattrat": 463,
    "removexattrat": 466,
    # io_uring has its own setxattr operations, which seccomp never sees.
    "io_uring_setup": 425,
}
_IOCTL_SYSCALL = {"x86_64": 16, "aarch64": 29}
# ioctl is an allowlist: Landlock does not mediate ioctls on regular files or
# directories, and some change an inode opened only for reading (chattr
# flags, fsxattr, fs-verity, fscrypt policies). Only these fd and terminal
# queries pass; every other request gets EPERM. The request numbers are the
# asm-generic ones, the same on x86_64 and aarch64.
ALLOWED_IOCTLS: dict[str, int] = {
    "TCGETS": 0x5401,  # isatty(), tcgetattr()
    "TIOCGPGRP": 0x540F,
    "TIOCGWINSZ": 0x5413,  # terminal size
    "FIONREAD": 0x541B,  # bytes available to read
    "FIONBIO": 0x5421,  # non-blocking flag
    "FIONCLEX": 0x5450,
    "FIOCLEX": 0x5451,  # close-on-exec flag
}
_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
_X32_SYSCALL_BIT = 0x40000000
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO_EPERM = 0x00050000 | 1
_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_JGE_K = 0x35
_BPF_RET_K = 0x06
# struct seccomp_data: int nr; u32 arch; u64 instruction_pointer; u64 args[6].
_SECCOMP_NR = 0
_SECCOMP_ARCH = 4
_SECCOMP_ARG1_LOW = 24  # little-endian low word of args[1]


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def metadata_filter(machine: str) -> list[tuple[int, int, int, int]]:
    """The BPF program denying metadata changes on ``machine``, as (code, jt, jf, k).

    Denied with EPERM: the metadata syscall families above, and every ioctl
    request not in ``ALLOWED_IOCTLS``.

    Any other architecture in ``seccomp_data.arch`` (a 32-bit compat call) and,
    on x86_64, any x32 call is denied outright.
    """
    if machine not in _METADATA_SYSCALLS:
        raise SandboxError(f"no metadata syscall table for {machine}")
    denied = sorted({*_METADATA_SYSCALLS[machine].values(), *_UNIFIED_METADATA_SYSCALLS.values()})
    # Laid out so every check jumps forward to one of the two returns at the
    # end: [.., ioctl allowlist, DENY, ALLOW]. -1 targets DENY and -2 ALLOW;
    # an ioctl request that matches no allowlist entry falls through to DENY.
    body: list[tuple[int, int, int, int | str]] = [
        (_BPF_LD_W_ABS, 0, 0, _SECCOMP_ARCH),
        (_BPF_JEQ_K, 1, 0, _AUDIT_ARCH[machine]),
        (_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO_EPERM),
        (_BPF_LD_W_ABS, 0, 0, _SECCOMP_NR),
    ]
    if machine == "x86_64":
        body.append((_BPF_JGE_K, -1, 0, _X32_SYSCALL_BIT))
    body.extend((_BPF_JEQ_K, -1, 0, number) for number in denied)
    body.append((_BPF_JEQ_K, 0, -2, _IOCTL_SYSCALL[machine]))
    body.append((_BPF_LD_W_ABS, 0, 0, _SECCOMP_ARG1_LOW))
    body.extend((_BPF_JEQ_K, -2, 0, request) for request in sorted(ALLOWED_IOCTLS.values()))
    deny = len(body)
    allow = deny + 1
    body.extend(
        [(_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO_EPERM), (_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW)]
    )
    targets = {-1: deny, -2: allow}
    program: list[tuple[int, int, int, int]] = []
    for index, (code, jt, jf, k) in enumerate(body):
        # Offsets count from the next instruction.
        jt = targets[jt] - index - 1 if jt in targets else jt
        jf = targets[jf] - index - 1 if jf in targets else jf
        program.append((code, jt, jf, int(k)))
    return program


def deny_metadata_changes() -> None:
    """Install the metadata seccomp filter on this process (needs no_new_privs)."""
    program = metadata_filter(os.uname().machine)
    filters = (_SockFilter * len(program))(*(_SockFilter(*item) for item in program))
    fprog = _SockFprog(len(program), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    _check(
        libc.prctl(
            ctypes.c_int(_PR_SET_SECCOMP),
            ctypes.c_ulong(_SECCOMP_MODE_FILTER),
            ctypes.byref(fprog),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        ),
        "prctl(PR_SET_SECCOMP)",
    )


_SIOCGIFFLAGS = 0x8913
_SIOCSIFFLAGS = 0x8914
_IFF_UP = 0x1


def bring_loopback_up() -> None:
    """Bring ``lo`` up in this process's (new, empty) network namespace.

    ``unshare --net`` creates the namespace with ``lo`` down, which would deny
    loopback along with every other address. Needs ``CAP_NET_ADMIN`` in the
    namespace, which the mapped root of ``unshare --map-root-user`` has.
    """
    import fcntl
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        request = struct.pack("16sH14s", b"lo", 0, b"")
        current = struct.unpack("16sH", fcntl.ioctl(sock, _SIOCGIFFLAGS, request)[:18])[1]
        fcntl.ioctl(sock, _SIOCSIFFLAGS, struct.pack("16sH14s", b"lo", current | _IFF_UP, b""))


def require_loopback_only() -> None:
    """Refuse unless this process's network namespace has only ``lo``.

    Checked at activation, in the process that execs the command, so a
    decision made earlier in the controller cannot go stale.
    """
    import socket

    try:
        names = [name for _index, name in socket.if_nameindex()]
    except OSError as exc:
        raise SandboxError(f"network interfaces cannot be listed: {exc}") from None
    if not names or any(name != "lo" for name in names):
        raise SandboxError(f"network is not isolated: interfaces {sorted(names)}")


def _parse(
    arguments: list[str],
) -> tuple[bool, bool, bool, list[tuple[str, int, int]], list[str]]:
    loopback = bool(arguments) and arguments[0] == "--loopback-up"
    if loopback:
        arguments = arguments[1:]
    offline = bool(arguments) and arguments[0] == "--require-loopback-only"
    if offline:
        arguments = arguments[1:]
    landlock = bool(arguments) and arguments[0] == "--landlock"
    index = 1 if landlock else 0
    roots: list[tuple[str, int, int]] = []
    while index < len(arguments) and arguments[index] == "--root":
        if index + 3 >= len(arguments):
            raise SandboxError("--root needs DIR DEV INO")
        try:
            device, inode = int(arguments[index + 2]), int(arguments[index + 3])
        except ValueError:
            raise SandboxError("--root DEV and INO must be integers") from None
        roots.append((arguments[index + 1], device, inode))
        index += 4
    if index >= len(arguments) or arguments[index] != "--" or index + 1 >= len(arguments):
        raise SandboxError(
            "usage: [--loopback-up] [--require-loopback-only] [--landlock] "
            "--root DIR DEV INO ... -- ARGV..."
        )
    if not roots:
        raise SandboxError("at least one --root is required")
    return loopback, offline, landlock, roots, arguments[index + 1 :]


def _command_environment() -> dict[str, str]:
    raw = os.environ.get(COMMAND_ENV_VARIABLE)
    if raw is None:
        raise SandboxError(f"{COMMAND_ENV_VARIABLE} is not set")
    try:
        env = json.loads(raw)
    except ValueError as exc:
        raise SandboxError(f"{COMMAND_ENV_VARIABLE} is not JSON: {exc}") from None
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise SandboxError(f"{COMMAND_ENV_VARIABLE} is not a string mapping")
    return env


def main(arguments: list[str]) -> int:
    try:
        loopback, offline, landlock, roots, command = _parse(arguments)
        env = _command_environment()
        root_fds = open_verified_roots(roots)
        try:
            if loopback:
                bring_loopback_up()
            if landlock:
                restrict_writes(root_fds)
                deny_metadata_changes()
            refuse_root_aliases(root_fds)
        finally:
            for fd in root_fds:
                os.close(fd)
    except (SandboxError, OSError) as exc:
        sys.stderr.write(f"ouroboros exec sandbox: {exc}\n")
        return EXIT_SANDBOX_FAILED
    try:
        if offline:
            # The last check before exec: nothing decided earlier (in the
            # controller, or before the restriction) is trusted as proof.
            require_loopback_only()
    except (SandboxError, OSError) as exc:
        sys.stderr.write(f"ouroboros exec sandbox: {exc}\n")
        return EXIT_SANDBOX_FAILED
    try:
        os.execvpe(command[0], command, env)
    except FileNotFoundError as exc:
        sys.stderr.write(f"ouroboros exec sandbox: {command[0]}: {exc.strerror}\n")
        return EXIT_NOT_FOUND
    except OSError as exc:
        sys.stderr.write(f"ouroboros exec sandbox: {command[0]}: {exc.strerror}\n")
        return EXIT_NOT_EXECUTABLE
    return EXIT_NOT_EXECUTABLE  # pragma: no cover - execvpe does not return


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
