"""One execution sandbox for the commands the controller itself runs.

The controller runs some commands on its own authority, not through an agent:
legacy-verifier replay (``orchestrator/evidence/command_replay.py``) re-runs a
transcript command in a copy of the workspace, and a check package runs
model-written checks on a copy of the checkout. Each copy is a working
directory, not a boundary: a script run there can still write any absolute
path the user can write. This module is the boundary, shared by every such
caller.

``confine`` turns an argv into the argv, environment and working directory
that run it confined, or into ``SandboxUnavailable`` (a typed reason; the
caller records the outcome as indeterminate and runs nothing). The caller
spawns the confined argv with its own process runner and timeout, because
callers differ in how they talk to the process (replay collects its output;
a check package exchanges frames over stdin and stdout). Under confinement:

- **Writes** are allowed only beneath the writable roots the caller names
  (the copy) and the per-run temp directory, plus a few character devices
  (``/dev/null`` and friends; on Windows none: an AppContainer cannot open
  the ``NUL`` device at all, so a command that redirects to it fails).
  Everything else, including the live workspace, the user's home directory
  and the system temp directory, is read-only.
- **Shared memory** (``ConfinedCommand.private_dev_shm``): on Linux, POSIX
  shared memory and semaphores (``shm_open``, ``sem_open``, and so
  ``multiprocessing`` locks, queues and ``SharedMemory``) are files in
  ``/dev/shm``, at a path no environment variable redirects. Where an
  unprivileged user and mount namespace works, the command gets a fresh
  tmpfs of its own at ``/dev/shm`` (writable, isolated from the host's
  ``/dev/shm`` and from every other command, gone when the command's
  namespace ends); the host's ``/dev/shm`` is never writable. Where it does
  not (a container under Docker's default seccomp profile), ``/dev/shm`` is
  read-only like the rest, so a command that needs it fails, which fails
  closed. On macOS these are kernel objects, not files: ``sandbox-exec``
  leaves them available and cannot make their names private, so they are in
  the user's IPC namespace (outside this boundary). An AppContainer has its
  own named-object namespace, which is private already.
  Each root is claimed by identity: ``confine`` records its real path,
  device and inode, and the helper opens it without following a symlink and
  refuses to run the command unless it is still that directory (on Linux the
  Landlock rule is bound to that very descriptor). A root holding a regular
  file with another hard link is refused: the other link may be outside, and
  writing through the root would change it; so is one holding a character or
  block device node, and creating one beneath a root is denied. ``confine`` reports that early as
  ``aliased_writable_root``; the helper checks again from the verified
  descriptors after the restriction is in place, immediately before exec, and
  runs nothing if a link appeared in between.
  "Read-only" covers content, names (create, remove, rename, link) and
  metadata the process sets (mode, ownership, timestamps, extended
  attributes, inode flags; on Windows, file attributes, alternate data
  streams and the DACL). Inside the roots, mode, timestamps and extended
  attributes can change (``ConfinedCommand.metadata_writes_in_roots``)
  except on a Linux host without an unprivileged mount namespace, where
  they are denied inside the roots too (see the Linux backend). Where a
  Linux command has the mount namespace, each root is its own mount, so a
  rename or hard link from one root to another (the copy and the temp
  directory) fails with ``EXDEV``; ``shutil.move`` and ``mv`` fall back to
  copying, ``os.replace`` does not. The access time the kernel records when a
  permitted read happens is part of read access, not a write the process
  performs: it changes under ``sandbox-exec`` with every write denied as
  well, and only a mount option (``noatime``) can stop it.
- **Reads** are not restricted on macOS and Linux. An AppContainer can read
  only what its principals are granted, so on Windows the command reads what
  every AppContainer may read (the system directories grant ``ALL
  APPLICATION PACKAGES``), its writable roots, and the paths ``confine``
  grants to the Ouroboros read capability (see the Windows backend). A
  command that reads anything else fails, which fails closed.
- **Network** (``deny_network=True``, reported as ``network_denied``):
  non-loopback IP traffic is denied, and loopback (``localhost``) and
  Unix-domain sockets stay available for local IPC. How far loopback
  reaches differs: under ``sandbox-exec`` and in a container's own
  loopback-only namespace it reaches every local process; in a new Linux
  namespace and in a Windows AppContainer it reaches only the command's own
  processes. An AppContainer never reaches a loopback server outside it,
  even with ``deny_network=False``, unless an administrator has exempted it
  (``CheckNetIsolation LoopbackExempt``), which a per-run container never
  is. That is not reported separately: with the network denied (the
  replay's only mode) a Windows command reaches what it reaches in a new
  Linux namespace, and a check that needs a service outside the sandbox
  cannot be verified by either.
- **Other processes' environments** (``ConfinedCommand.isolates_process_environments``):
  under Landlock a confined process cannot read ``/proc/<pid>/environ``,
  ``mem`` or ``maps`` of any process outside its domain, the controller and
  every other process of the user included, because Landlock denies
  ptrace-mode access across the domain boundary; its own ``/proc/self`` and
  its descendants' stay readable. An AppContainer process cannot open a
  process outside its container for reading its memory (``OpenProcess``
  with ``PROCESS_VM_READ`` is denied by the process DACL and by the
  mandatory integrity policy, since the container runs at low integrity),
  so on Windows the environments of the controller and the user's other
  processes are unreadable too. ``sandbox-exec`` cannot deny the macOS
  equivalent (``sysctl`` ``KERN_PROCARGS2``, which returns a same-user
  process's arguments and environment; neither ``sysctl-read`` nor
  ``process-info*`` rules gate it), so on macOS a confined process can read
  the environment of the controller and of the user's other processes. A
  caller that must keep those secrets from the command requires
  ``isolates_process_environments``.
- **Environment**: built from scratch. Only the variables named in
  ``env_passthrough`` (``DEFAULT_ENV_PASSTHROUGH`` by default: ``PATH``, the
  locale, ``TZ`` and Python I/O settings) are copied from the source
  environment; ``TMPDIR``, ``TMP`` and ``TEMP`` point to the temp directory,
  and so does ``HOME`` unless the caller passes it through (on Windows also
  ``USERPROFILE``, ``APPDATA`` and ``LOCALAPPDATA``, and ``SYSTEMROOT`` is
  set to the Windows directory, without which Winsock cannot start);
  ``env_set`` values are applied last. On Windows, creating an AppContainer
  process then rewrites ``LOCALAPPDATA`` to ``<LOCALAPPDATA>\\Packages\\<name>\\AC``
  and ``TEMP`` and ``TMP`` to its ``Temp`` subdirectory; ``LOCALAPPDATA``
  must lie inside a writable root (it is the temp directory unless the
  caller changes it), so all three stay inside the temp directory. That environment (``ConfinedCommand.command_env``)
  takes effect only when the command itself is exec'd, inside the sandbox:
  every launcher (``sandbox-exec``, ``unshare``, the helper) starts with a
  fixed bootstrap environment (``ConfinedCommand.env``), so a loader control
  the command names (``LD_PRELOAD``, ``DYLD_INSERT_LIBRARIES``) cannot run
  code before confinement.

Backends:

- **macOS**: ``sandbox-exec`` with a generated profile: everything allowed,
  then ``file-write*`` denied except beneath the writable roots (passed as
  profile parameters, so no path is ever spliced into the profile text) and
  the devices; network denial allows only loopback IP.
- **Linux**: Landlock (ABI 3, Linux 6.2, or newer: below it truncation
  cannot be denied) plus a seccomp filter, both applied by the helper
  ``_confine_exec.py`` before it execs the command. Landlock does not mediate
  metadata changes (no ABI has a right for mode, timestamps or extended
  attributes), so the filter denies the chown syscall family and io_uring,
  and admits ``ioctl`` only for an allowlist of fd and terminal queries
  (every other request, such as chattr flags, fs-verity or fscrypt
  policies, is denied). The mode, timestamp and extended attribute families
  are confined by one of two paths, recorded as
  ``ConfinedCommand.metadata_writes_in_roots``. Where an unprivileged user
  and mount namespace works (probed once, end to end), the helper makes
  every mount in the namespace read-only and stacks a writable clone of each
  verified root on the root itself, so those changes succeed inside the
  roots and fail with ``EROFS`` everywhere else, and the filter leaves them
  out; the helper then drops ``CAP_SYS_ADMIN`` from its bounding set, so the
  command cannot change the mounts back. Where it does not, the filter denies them too; it cannot see paths,
  so they are denied inside the writable roots as well (``touch`` on an
  existing file, ``shutil.copy2``/``copystat``, a test runner's cache, tar
  extraction that restores modes), and such a command fails, which fails
  closed. On macOS the same helper runs inside ``sandbox-exec``
  and only applies the command's environment. Landlock and the filter are
  unprivileged and need no mount or user namespace, so the backend works in
  containers. ``confine`` selects one
  ``NetworkPlan``: a new unprivileged network namespace
  (``unshare --user --map-root-user --net``, with ``lo`` brought up), or the
  current one when it has only loopback (a container started with
  ``--network none``). The namespace state is never cached (only the
  ``unshare`` capability is), and the parent's choice is never the proof:
  the helper checks that its namespace has only ``lo`` immediately before
  exec and runs nothing otherwise. Independently of the network plan, when
  ``unshare --user --map-root-user --mount``, the read-only mounts and a
  tmpfs mount inside it work here (probed once, end to end), the helper also
  starts in a new mount namespace, applies the mounts above and mounts the
  private ``/dev/shm`` (see the helper). Any new
  user namespace maps the user to root inside it: the command sees uid 0 but
  has no privilege over anything the user does not own.
- **Windows**: an AppContainer, set up by the launcher
  ``_confine_windows.py``, which stays outside the container, starts the
  command inside it and waits for it (an AppContainer is applied when a
  process is created, not by the process itself). Each ``confine`` call
  names a fresh AppContainer (a random name). Concurrent commands therefore
  never share a principal: one command's grants cannot reach another's copy.
  ``CreateProcessW`` requires a registered profile, so the launcher creates
  one and deletes it after creating the command's process and before
  resuming it: the profile folder, which grants the container full control,
  and its registry storage are gone before the command runs anything. The
  launcher grants that SID modify on the writable roots, through the handles
  it verified (keeping the rest of each DACL, its protection included; a
  root with a NULL DACL, which means no access control, is refused rather
  than rewritten),
  creates the command suspended with ``PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES``
  (no network capability when the network is denied; the client and server
  capabilities otherwise) and ``PROC_THREAD_ATTRIBUTE_HANDLE_LIST`` (exactly
  its standard handles), puts it in a Job Object that kills every process in
  it when its last handle closes, resumes it, and when it exits terminates
  the rest of its tree and revokes the grants. The caller's timeout kills
  the launcher (directly or through the caller's own job); its job handle
  then closes and the kernel kills the whole tree. The revocation cannot run
  then, so the grants stay on the roots until the caller deletes them (it
  owns and removes them; see the precondition), naming a SID that no process
  holds any more.
  Reading needs grants too. Read and execute on the controller's
  interpreter (``sys.prefix``, ``sys.base_prefix``) and on the targets of
  the links beneath the writable roots (the live dependency trees a
  workspace copy links to) are granted to one stable capability SID,
  ``_confine_windows.READ_CAPABILITY``, which every run's container holds.
  **That grant is persistent**: it is applied once, inherited by files
  created there later, and never revoked by a run, because a per-run read
  grant would rewrite the DACL of every file in the user's live virtual
  environment twice per command, concurrent commands rewriting the same DACL
  can drop each other's grant, and a killed launcher could not revoke it. It
  widens access only for processes holding that capability, which only the
  sandbox gives out, and only to reading, which the other backends do not
  restrict. A path is granted only when containers cannot already read it,
  never when it is a volume root, contains a writable root, the user's home
  directory, the Windows directory or a Program Files directory, or lies
  inside one of the last two. Every grant is bound to its object: the
  object's volume and file id are recorded in
  ``~/.ouroboros/exec-sandbox/read-grants.jsonl`` before its DACL changes,
  through the same handle, and ``remove_persistent_read_grants`` reopens each
  object by that id (so one renamed since is still cleaned, and a new object
  at the old name is not touched) and removes every such grant.
- **Anything else** (a Linux kernel without Landlock, a macOS process that
  is already sandboxed, another operating system): no backend, and
  ``confine`` returns ``SandboxUnavailable``. A command is never run
  unconfined as a fallback.

Each backend is probed once per process with ``_sandbox_probe.py``: every
mutation class it can perform unconfined on this host (content, names and
metadata; the required ones must all be possible) must be denied outside the
writable root when confined, ordinary writes inside must still work, and the
outside directory must be unchanged. Anything less reports the backend
unavailable.

Unsafe off switch: ``confine(..., enabled=False)`` returns the argv
unchanged with ``backend=DISABLED`` and ``network_denied=False`` (the
environment is still built the same way). ``enabled`` defaults to ``True``.
This module never reads configuration: the caller owns where its policy comes
from, keeps it fixed for the life of its run, and records it.

Precondition: the caller owns its writable roots for the life of the
command (for example, directories under a private ``mkdtemp``). The sandbox
confines the command, not other processes: the roots are verified at
activation (identity, no hard-linked file anywhere beneath them) and the
command cannot create an alias to an outside file itself, but a different,
unconfined process of the same user that links an outside file into a root
while the command runs is outside the boundary. Such a process already holds
the authority to change that file directly.

Outside this boundary: effects the confined process asks another, unconfined
process to perform over IPC (a user service manager, a desktop automation
service, a container daemon), changes other unconfined processes make to the
roots while the command runs, and reads of anything the user can read. On
Windows also: objects every AppContainer may write (an ACL that grants ``ALL
APPLICATION PACKAGES`` write), the container's named-object namespace, and
per-run grants left on roots whose launcher was killed (their caller deletes
the roots), and the profile of a launcher killed in the moment between
creating the command's AppContainer profile and deleting it (before the
command is resumed): its folder and registry key stay behind.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import functools
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from typing import Any

import structlog

from ouroboros.runtime import _sandbox_probe as probe_module

log = structlog.get_logger(__name__)


# Copied from the source environment by default: what any program needs to
# find executables and decode text, and nothing that names a credential,
# a configuration file or an import path.
DEFAULT_ENV_PASSTHROUGH: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_COLLATE",
    "LC_MESSAGES",
    "LC_MONETARY",
    "LC_NUMERIC",
    "LC_TIME",
    "TZ",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
)
TEMP_DIRECTORY_VARIABLES: tuple[str, ...] = ("TMPDIR", "TMP", "TEMP")

_PROBE_TIMEOUT_SECONDS = 10.0
_CONFINE_HELPER = Path(__file__).with_name("_confine_exec.py")
_WINDOWS_LAUNCHER = Path(__file__).with_name("_confine_windows.py")
# Must match ``_confine_exec.COMMAND_ENV_VARIABLE`` (the helper is not imported).
_COMMAND_ENV_VARIABLE = "OUROBOROS_SANDBOX_COMMAND_ENV"

# macOS: profile parameters ``W0``..``Wn`` carry the writable roots.
# Writing through any device node is denied (a node inside a root would alias
# a device), then the few harmless devices are allowed again.
_DARWIN_DEVICE_RULES = (
    "(deny file-write* (vnode-type CHARACTER-DEVICE BLOCK-DEVICE))"
    '(allow file-write* (literal "/dev/null") (literal "/dev/zero")'
    ' (literal "/dev/random") (literal "/dev/urandom") (literal "/dev/tty")'
    ' (literal "/dev/dtracehelper") (subpath "/dev/fd"))'
)
_DARWIN_NETWORK_RULES = (
    "(deny network-outbound (remote ip))"
    '(allow network-outbound (remote ip "localhost:*"))'
    "(deny network-inbound (local ip))"
    '(allow network-inbound (local ip "localhost:*"))'
)


class SandboxBackend(StrEnum):
    """The mechanism confining a command."""

    SANDBOX_EXEC = "sandbox_exec"
    LANDLOCK = "landlock"
    APPCONTAINER = "appcontainer"
    DISABLED = "disabled"
    """The unsafe off switch is set: the command runs unconfined."""


class NetworkPlan(StrEnum):
    """How a confined command's network is handled; selected once by ``confine``."""

    ALLOW = "allow"
    """``deny_network=False``: the network is not restricted."""
    PROFILE = "profile"
    """macOS: the ``sandbox-exec`` profile denies non-loopback IP."""
    NEW_NAMESPACE = "new_namespace"
    """Linux: a new, empty network namespace (``unshare``) with ``lo`` brought up."""
    CURRENT_NAMESPACE = "current_namespace"
    """Linux: this process's namespace had only ``lo`` when ``confine`` ran."""
    NO_CAPABILITY = "no_capability"
    """Windows: the AppContainer holds no network capability (its own loopback stays)."""


class SandboxUnavailableReason(StrEnum):
    """Why a command cannot be run confined (its outcome is indeterminate)."""

    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    """No filesystem confinement backend works on this host."""
    NETWORK_ISOLATION_UNAVAILABLE = "network_isolation_unavailable"
    """Network denial was requested and no mechanism for it works here."""
    INVALID_WRITABLE_ROOT = "invalid_writable_root"
    """A writable root or the temp directory is not an existing directory."""
    ALIASED_WRITABLE_ROOT = "aliased_writable_root"
    """A regular file under a writable root has another hard link, which may be
    outside the roots (writing it through the root would change that file
    too), or part of the root cannot be inspected to rule that out."""
    WINDOWS_BATCH_FILE = "windows_batch_file"
    """Windows: the command is a batch file (``.cmd``, ``.bat``). Inside an
    AppContainer ``cmd.exe`` refuses to run one ("Access is denied", measured
    on ``windows-latest`` with the file readable and the directory writable),
    so it is not run."""


@dataclass(frozen=True, slots=True)
class SandboxUnavailable:
    """The command was not run; record its outcome as indeterminate."""

    reason: SandboxUnavailableReason
    detail: str = ""

    @property
    def outcome(self) -> str:
        return "indeterminate"


@dataclass(frozen=True, slots=True)
class ConfinedCommand:
    """What to spawn: ``argv`` in ``cwd`` with exactly ``env``.

    Spawn contract: the sandbox governs what the command opens, not what it
    inherits. Spawn it with ``stdin`` set to ``DEVNULL`` or a pipe the caller
    owns, ``stdout``/``stderr`` to pipes, and no other descriptors (the
    default ``close_fds``); never hand it the controller's own stdin or a
    descriptor for a file outside the writable roots.

    ``env`` is the launchers' fixed bootstrap environment, which carries
    ``command_env``; the command itself runs with exactly ``command_env``.
    With the sandbox switched off the two are the same.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str]
    command_env: Mapping[str, str]
    cwd: str
    backend: SandboxBackend
    writable_roots: tuple[str, ...]
    network_denied: bool
    isolates_process_environments: bool
    """Whether the command cannot read other processes' environments (Landlock, AppContainer)."""
    private_dev_shm: bool = False
    """Linux: whether the command gets its own writable tmpfs at ``/dev/shm``."""
    metadata_writes_in_roots: bool = False
    """Whether mode, timestamps and extended attributes can change inside the
    writable roots (outside them they never can). False only on Linux without
    an unprivileged mount namespace, where the seccomp filter denies them
    everywhere."""


def _darwin_profile(root_count: int, *, deny_network: bool) -> str:
    roots = " ".join(f'(subpath (param "W{index}"))' for index in range(root_count))
    profile = f"(version 1)(allow default)(deny file-write*)(allow file-write* {roots})"
    profile += _DARWIN_DEVICE_RULES
    return profile + (_DARWIN_NETWORK_RULES if deny_network else "")


# A writable root as ``confine`` validated it: real path, device, inode. The
# helper refuses to run the command unless the path still opens (without
# following a symlink) to that same directory.
_RootClaim = tuple[str, int, int]


def _claim_root(path: str) -> _RootClaim:
    status = os.stat(path)
    return (path, status.st_dev, status.st_ino)


def _hard_linked_file(root: str) -> str | None:
    """A regular file beneath ``root`` with more than one link, or a character
    or block device node (both alias storage outside the root), or None.

    Symlinks and linked directories are not followed. A directory that cannot
    be listed or an entry that cannot be examined is returned as well: it may
    hold such a link, so the root cannot be shown to be free of aliases.
    """
    failures: list[str] = []

    def unreadable(error: OSError) -> None:
        failures.append(f"{error.filename}: {error.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=unreadable):
        # ``os.walk`` follows Windows junctions even without ``followlinks``.
        dirnames[:] = [
            name for name in dirnames if not os.path.isjunction(os.path.join(dirpath, name))
        ]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                status = os.lstat(path)
            except OSError as exc:
                return f"{path}: {exc.strerror}"
            if stat.S_ISREG(status.st_mode) and status.st_nlink > 1:
                return path
            if stat.S_ISCHR(status.st_mode) or stat.S_ISBLK(status.st_mode):
                return path
    return failures[0] if failures else None


# Launchers run before confinement, so they never come from ``PATH``: only
# from these system directories, as canonical absolute paths, and only when
# the file and its directory are owned by root and writable by no one else.
_TRUSTED_LAUNCHER_DIRECTORIES = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _root_owned_and_unwritable(path: str) -> bool:
    try:
        status = os.stat(path)
    except OSError:
        return False
    return status.st_uid == 0 and not status.st_mode & 0o022


def _trusted_launcher(
    name: str, directories: Sequence[str] = _TRUSTED_LAUNCHER_DIRECTORIES
) -> str | None:
    """The canonical absolute path of system executable ``name``, or None.

    ``PATH`` is never consulted. A candidate counts only if its resolved
    file and that file's directory are root-owned and not group- or
    other-writable, so no user-controlled program can take its place.
    """
    for directory in directories:
        real = os.path.realpath(os.path.join(directory, name))
        if (
            os.path.isabs(real)
            and os.path.isfile(real)
            and os.access(real, os.X_OK)
            and _root_owned_and_unwritable(real)
            and _root_owned_and_unwritable(os.path.dirname(real))
        ):
            return real
    return None


def _interpreter() -> str:
    """This controller's own interpreter, as an absolute path (never ``PATH``)."""
    return os.path.abspath(sys.executable)


def _backend_argv(
    backend: SandboxBackend,
    argv: Sequence[str],
    roots: Sequence[_RootClaim],
    network: NetworkPlan,
    readable: Sequence[str] = (),
) -> tuple[str, ...]:
    """The argv running ``argv`` under ``backend`` with the ``network`` plan.

    On macOS and Linux every backend ends in ``_confine_exec.py``, which
    applies the command's environment only when it execs the command, inside
    the sandbox. On Linux, whenever the network is denied, the helper checks
    immediately before exec that its namespace has only ``lo``
    (``--require-loopback-only``): the plan is a parent-side choice, never
    the final proof. On Windows the launcher ``_confine_windows.py`` creates
    the command inside a fresh AppContainer, with read grants on ``readable``.
    """
    claims = [part for path, dev, ino in roots for part in ("--root", path, str(dev), str(ino))]
    if backend is SandboxBackend.APPCONTAINER:
        options = [
            "--appcontainer",
            _appcontainer_name(),
            "--manifest",
            str(_read_grant_manifest()),
        ]
        if network is NetworkPlan.ALLOW:
            options.append("--network")
        for path in readable:
            options += ["--read", path]
        launcher = (_interpreter(), "-I", "-S", "-B", str(_WINDOWS_LAUNCHER))
        return (*launcher, *options, *claims, "--", *argv)
    helper = _helper_argv()
    if backend is SandboxBackend.SANDBOX_EXEC:
        executable = _trusted_launcher("sandbox-exec")
        if executable is None:  # pragma: no cover - the backend was probed with it
            raise RuntimeError("sandbox-exec is not available")
        profile = _darwin_profile(len(roots), deny_network=network is NetworkPlan.PROFILE)
        params = [
            part for index, (path, _, _) in enumerate(roots) for part in ("-D", f"W{index}={path}")
        ]
        return (executable, "-p", profile, *params, "--", *helper, *claims, "--", *argv)
    executable = _private_mounts_unshare()
    namespaces: tuple[str, ...] = ("--mount",) if executable else ()
    flags: tuple[str, ...] = ("--private-mounts",) if executable else ()
    if network is NetworkPlan.NEW_NAMESPACE:
        unshare = _unshare_prefix()
        if unshare is None:  # pragma: no cover - the plan was selected from it
            raise RuntimeError("unshare is not available")
        executable = unshare[0]
        namespaces += ("--net",)
        # A fresh namespace starts with ``lo`` down; bring it up so only
        # non-loopback traffic is denied.
        flags += ("--loopback-up", "--require-loopback-only")
    elif network is NetworkPlan.CURRENT_NAMESPACE:
        flags += ("--require-loopback-only",)
    launcher = _unshare_argv(executable, *namespaces) if executable else ()
    return (*launcher, *helper, *flags, "--landlock", *claims, "--", *argv)


def _helper_argv() -> tuple[str, ...]:
    return (_interpreter(), "-I", "-S", "-B", str(_CONFINE_HELPER))


def _unshare_argv(executable: str, *namespaces: str) -> tuple[str, ...]:
    """``unshare`` into a new user namespace (the user mapped to root) plus ``namespaces``."""
    return (executable, "--user", "--map-root-user", *namespaces, "--")


def _appcontainer_name() -> str:
    """A fresh AppContainer name: every command gets its own principal."""
    return f"ouroboros.sandbox.{secrets.token_hex(16)}"


def _read_grant_manifest() -> Path:
    """Where the Windows launcher records each persistent read grant."""
    return Path.home() / ".ouroboros" / "exec-sandbox" / "read-grants.jsonl"


def _contains(parent: str, child: str) -> bool:
    """Whether ``child`` is ``parent`` or lies beneath it (both real paths)."""
    parent, child = os.path.normcase(parent), os.path.normcase(child)
    try:
        return os.path.commonpath((parent, child)) == parent
    except ValueError:  # different drives
        return False


def _system_anchors() -> tuple[str, ...]:
    """The Windows directory and the Program Files directories that exist here."""
    names = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
    found = (os.environ.get(name) for name in names)
    return tuple(os.path.realpath(path) for path in found if path and os.path.isdir(path))


def _persistent_read_allowed(path: str, roots: Sequence[str]) -> bool:
    """Whether ``path`` may receive the persistent read grant (see the module docstring).

    Never a volume root; never a path that is or contains a writable root or
    the user's home directory (a grant there would cover the scratch
    directories and the user's whole profile); never the Windows directory or
    a Program Files directory, nor anything above or inside them (system
    managed; what containers may read there is already granted).
    """
    if os.path.ismount(path):
        return False
    home = os.path.realpath(Path.home())
    if _contains(path, home) or any(
        _contains(path, root) or _contains(root, path) for root in roots
    ):
        return False
    return not any(
        _contains(path, anchor) or _contains(anchor, path) for anchor in _system_anchors()
    )


def _link_targets(roots: Sequence[str]) -> list[str]:
    """The real paths of the symbolic links and junctions beneath ``roots``."""
    targets: list[str] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            links = [
                name
                for name in (*dirnames, *filenames)
                if os.path.islink(os.path.join(dirpath, name))
                or os.path.isjunction(os.path.join(dirpath, name))
            ]
            dirnames[:] = [name for name in dirnames if name not in links]
            targets += [os.path.realpath(os.path.join(dirpath, name)) for name in links]
    return targets


def _windows_read_paths(roots: Sequence[str], extra: Sequence[str] = ()) -> tuple[str, ...]:
    """What an AppContainer command is granted to read: the controller's
    interpreter, ``extra``, and the targets of links beneath ``roots``;
    only paths that exist and may take the persistent grant, and none that
    lies beneath another."""
    prefixes = (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix)
    candidates: list[str] = []
    for path in (*prefixes, *extra, *_link_targets(roots)):
        real = os.path.realpath(path)
        if real in candidates or not os.path.exists(real):
            continue
        if _persistent_read_allowed(real, roots):
            candidates.append(real)
        else:
            log.info("exec_sandbox.read_grant_refused", path=real)
    return tuple(
        path
        for path in candidates
        if not any(other != path and _contains(other, path) for other in candidates)
    )


def remove_persistent_read_grants() -> tuple[str, ...]:
    """Remove every persistent Windows read grant the sandbox has made.

    Reads the manifest (``~/.ouroboros/exec-sandbox/read-grants.jsonl``),
    reopens each recorded object by its volume and file id (wherever it has
    been renamed to on that volume), removes every ACE for the Ouroboros read
    capability from it, and deletes the manifest once every object is clean
    or gone. Returns the recorded paths of the objects cleaned; empty on
    other platforms. Run it while
    no Ouroboros command is running: a confined command then loses what it
    was reading. The next confined command grants what it needs again.
    """
    if sys.platform != "win32":
        return ()
    from ouroboros.runtime import _confine_windows

    return tuple(_confine_windows.remove_read_grants(str(_read_grant_manifest())))


def _bootstrap_environment(command_env: Mapping[str, str]) -> dict[str, str]:
    """The fixed environment the launchers start with; the command's rides along.

    Nothing in ``command_env`` is in effect until ``_confine_exec.py`` execs
    the command, so a loader or interpreter control it names (``LD_PRELOAD``,
    ``DYLD_INSERT_LIBRARIES``) cannot run before confinement.
    """
    return {"PATH": os.defpath, _COMMAND_ENV_VARIABLE: json.dumps(dict(command_env))}


_PROBE = Path(__file__).with_name("_sandbox_probe.py")


def _probe_matrix(argv_prefix: Sequence[str] | None, root: Path) -> dict[str, Any] | None:
    """Run ``_sandbox_probe.py`` on a fresh layout under ``root``; None on failure.

    ``argv_prefix`` None runs it unconfined; otherwise it is the backend argv
    builder's output for the probe command.
    """
    inside, outside = root / "inside", root / "outside"
    inside.mkdir(exist_ok=True)
    outside.mkdir()
    probe_module.prepare(str(outside))
    before = probe_module.snapshot(str(outside))
    command = (_interpreter(), "-I", "-S", "-B", str(_PROBE), str(inside), str(outside))
    argv = command if argv_prefix is None else (*argv_prefix, *command)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            # The probe command gets the environment a real command would.
            env=_bootstrap_environment(build_environment(str(inside), source={"PATH": os.defpath})),
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        report = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(report, dict):
        log.info(
            "exec_sandbox.probe_failed",
            returncode=result.returncode,
            stderr=result.stderr.decode("utf-8", errors="replace")[-500:],
        )
        return None
    report["unchanged"] = probe_module.snapshot(str(outside)) == before
    return report


def _matrix_confines(baseline: dict[str, Any] | None, confined: dict[str, Any] | None) -> bool:
    """Whether ``confined`` denied every mutation ``baseline`` proved possible."""
    if baseline is None or confined is None:
        return False
    possible = {name for name, outcome in baseline["outside"].items() if outcome == "ok"}
    if not set(probe_module.REQUIRED) <= possible:
        return False
    return (
        all(confined["outside"].get(name) == "denied" for name in possible)
        and all(outcome == "ok" for outcome in confined["inside"].values())
        and confined["unchanged"] is True
    )


@functools.cache
def filesystem_backend() -> SandboxBackend | None:
    """The backend that confines writes on this host, or None; probed once.

    The backend must deny, outside its writable root, every mutation class of
    ``_sandbox_probe.py`` (content, names and metadata) that the same probe
    can perform unconfined on this host, and leave the outside unchanged.
    """
    if sys.platform == "darwin":
        if _trusted_launcher("sandbox-exec") is None:
            return None
        candidate = SandboxBackend.SANDBOX_EXEC
    elif sys.platform.startswith("linux"):
        candidate = SandboxBackend.LANDLOCK
    elif sys.platform == "win32":
        candidate = SandboxBackend.APPCONTAINER
    else:
        return None
    probe_root = Path(tempfile.mkdtemp(prefix="ouroboros-sandbox-probe-")).resolve()
    try:
        (probe_root / "baseline").mkdir()
        (probe_root / "confined").mkdir()
        baseline = _probe_matrix(None, probe_root / "baseline")
        (probe_root / "confined" / "inside").mkdir()
        inside = _claim_root(str(probe_root / "confined" / "inside"))
        readable: tuple[str, ...] = ()
        if candidate is SandboxBackend.APPCONTAINER:
            readable = _windows_read_paths((inside[0],), (str(_PROBE.parent),))
        prefix = _backend_argv(candidate, (), (inside,), NetworkPlan.ALLOW, readable)
        confined = _probe_matrix(prefix, probe_root / "confined")
        if not _matrix_confines(baseline, confined):
            log.info(
                "exec_sandbox.backend_unavailable",
                backend=candidate.value,
                baseline=baseline,
                confined=confined,
            )
            return None
        return candidate
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)


def _process_has_only_loopback() -> bool:
    """Return True when this process's network namespace has only loopback.

    The interfaces come from the kernel for the process's own namespace, so a
    container started with ``--network none`` reports only ``lo``.
    """
    try:
        names = [name for _index, name in socket.if_nameindex()]
    except (OSError, AttributeError):
        return False
    return bool(names) and all(name == "lo" for name in names)


def _network_plan(backend: SandboxBackend, deny_network: bool) -> NetworkPlan | None:
    """The one network plan for a command under ``backend``, or None if none works.

    The namespace state is read on every call and never cached; only the
    static ``unshare`` capability is probed once (``_unshare_prefix``).
    """
    if not deny_network:
        return NetworkPlan.ALLOW
    if backend is SandboxBackend.SANDBOX_EXEC:
        return NetworkPlan.PROFILE
    if backend is SandboxBackend.APPCONTAINER:
        return NetworkPlan.NO_CAPABILITY
    if _process_has_only_loopback():
        return NetworkPlan.CURRENT_NAMESPACE
    if _unshare_prefix() is not None:
        return NetworkPlan.NEW_NAMESPACE
    return None


@functools.cache
def _unshare_prefix() -> tuple[str, ...] | None:
    """``unshare --user --map-root-user --net`` if it works here; probed once.

    The ``unshare`` found by ``_trusted_launcher`` is both the one probed and
    the one every command's argv uses; the probe runs this controller's own
    interpreter under the fixed bootstrap environment.
    """
    executable = _trusted_launcher("unshare")
    if executable is None:
        return None
    prefix = _unshare_argv(executable, "--net")
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*prefix, _interpreter(), "-I", "-S", "-c", "pass"],
            env=_bootstrap_environment({"PATH": os.defpath}),
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return prefix if result.returncode == 0 else None


# Run by the private mounts probe as ``CODE INSIDE OUTSIDE``: a file in the
# private ``/dev/shm``, clearing read-only on every mount (mount_setattr)
# refused, a mode change inside the root that must succeed and one outside
# that must fail with EROFS.
_PRIVATE_MOUNTS_PROBE = """
import ctypes, errno, os, struct, sys
os.close(os.open('/dev/shm/probe', os.O_CREAT | os.O_WRONLY, 0o600))
attr = ctypes.create_string_buffer(struct.pack('=QQQQ', 0, 1, 0, 0), 32)
syscall = ctypes.CDLL(None).syscall
if syscall(ctypes.c_long(442), -100, b'/', ctypes.c_uint(0x8000), attr, ctypes.c_size_t(32)) >= 0:
    sys.exit(5)
os.chmod(sys.argv[1], 0o640)
try:
    os.chmod(sys.argv[2], 0o640)
except OSError as exc:
    sys.exit(0 if exc.errno == errno.EROFS else 3)
sys.exit(4)
"""


@functools.cache
def _private_mounts_unshare() -> str | None:
    """The trusted ``unshare`` if the helper's private mounts work here; probed once.

    Probed end to end with the very argv a command uses: a new user and mount
    namespace, the helper making every mount read-only, stacking the writable
    root, mounting the tmpfs over ``/dev/shm`` and dropping its mount
    authority; then a file created in it, clearing read-only on the mounts
    refused, a mode change inside the root and one outside refused. Linux only; None
    wherever any step fails (no ``unshare``, no unprivileged user namespaces,
    a seccomp or LSM policy denying them or a mount, no ``/dev/shm``), and the
    command then runs without them.
    """
    if not sys.platform.startswith("linux"):
        return None
    executable = _trusted_launcher("unshare")
    if executable is None:
        return None
    root = tempfile.mkdtemp(prefix="ouroboros-mounts-probe-")
    try:
        inside, outside = os.path.join(root, "inside"), os.path.join(root, "outside")
        os.mkdir(inside)
        for name in (os.path.join(inside, "file"), outside):
            with open(name, "w"):
                pass
            os.chmod(name, 0o600)
        path, device, inode = _claim_root(os.path.realpath(inside))
        argv = (
            *_unshare_argv(executable, "--mount"),
            *_helper_argv(),
            "--private-mounts",
            *("--root", path, str(device), str(inode)),
            "--",
            *(_interpreter(), "-I", "-S", "-c", _PRIVATE_MOUNTS_PROBE),
            *(os.path.join(path, "file"), os.path.realpath(outside)),
        )
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            env=_bootstrap_environment({"PATH": os.defpath}),
            capture_output=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if result.returncode != 0:
        log.info(
            "exec_sandbox.private_mounts_unavailable",
            returncode=result.returncode,
            stderr=result.stderr.decode("utf-8", errors="replace")[-500:],
        )
        return None
    return executable


def sandbox_unavailable_reason(
    *, deny_network: bool = True, enabled: bool = True
) -> SandboxUnavailableReason | None:
    """Diagnose why ``confine`` would refuse on this host, or None.

    A diagnosis only: ``confine`` makes its own selection, and on Linux the
    helper makes the final network check. None as well when the caller's
    policy switches the sandbox off (``enabled=False``).
    """
    if not enabled:
        return None
    backend = filesystem_backend()
    if backend is None:
        return SandboxUnavailableReason.SANDBOX_UNAVAILABLE
    if _network_plan(backend, deny_network) is None:
        return SandboxUnavailableReason.NETWORK_ISOLATION_UNAVAILABLE
    return None


def build_environment(
    temp_dir: str,
    *,
    source: Mapping[str, str] | None = None,
    passthrough: Sequence[str] = DEFAULT_ENV_PASSTHROUGH,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The complete environment of a confined command (see the module docstring)."""
    values = os.environ if source is None else source
    env = {name: values[name] for name in passthrough if name in values}
    for name in TEMP_DIRECTORY_VARIABLES:
        env[name] = temp_dir
    for name in _HOME_VARIABLES:
        env.setdefault(name, temp_dir)
    if sys.platform == "win32":
        env.setdefault("SYSTEMROOT", _windows_directory())
    env.update(overrides or {})
    return env


# Where programs keep per-user state: pointed at the temp directory unless
# passed through. On Windows the profile directories play the part of HOME.
_HOME_VARIABLES: tuple[str, ...] = (
    ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA") if sys.platform == "win32" else ("HOME",)
)


def _windows_directory() -> str:
    """The Windows directory, from the system (never from an environment)."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    length = ctypes.WinDLL("kernel32").GetSystemWindowsDirectoryW(buffer, len(buffer))  # type: ignore[attr-defined]
    if not length:
        raise OSError("GetSystemWindowsDirectoryW failed")
    return buffer.value


def confine(
    argv: Sequence[str],
    *,
    cwd: str,
    writable_roots: Sequence[str],
    temp_dir: str,
    deny_network: bool = True,
    env_source: Mapping[str, str] | None = None,
    env_passthrough: Sequence[str] = DEFAULT_ENV_PASSTHROUGH,
    env_set: Mapping[str, str] | None = None,
    enabled: bool = True,
) -> ConfinedCommand | SandboxUnavailable:
    """Return how to run ``argv`` confined, or why it cannot be.

    ``writable_roots`` and ``temp_dir`` must be existing directories; the
    caller creates them and removes them afterwards. ``temp_dir`` is writable
    too. ``argv`` is run directly, never through a shell. On Windows it goes
    through the repository's dispatch policy
    (``evaluation.command_dispatch.prepare_command``): a bare name is resolved
    on absolute ``PATH`` entries only, as ``execvpe`` does on POSIX: the
    working directory and relative entries are never searched implicitly,
    and an absolute entry the command names is honored, the copy included; a
    batch file is refused as ``windows_batch_file``, since ``cmd.exe`` will
    not run one inside an AppContainer. ``enabled`` is the
    caller's sandbox policy; ``False`` is the unsafe off switch (the caller
    owns where that choice comes from and records it).
    """
    real_temp = os.path.realpath(temp_dir)
    roots: list[str] = []
    for root in (*writable_roots, temp_dir):
        real = os.path.realpath(root)
        if not os.path.isabs(root) or not os.path.isdir(real):
            return SandboxUnavailable(SandboxUnavailableReason.INVALID_WRITABLE_ROOT, root)
        if real not in roots:
            roots.append(real)
    claims = [_claim_root(root) for root in roots]
    env = build_environment(
        real_temp, source=env_source, passthrough=env_passthrough, overrides=env_set
    )
    if not enabled:
        _warn_disabled()
        return ConfinedCommand(
            argv=tuple(argv),
            env=env,
            command_env=env,
            cwd=cwd,
            backend=SandboxBackend.DISABLED,
            writable_roots=tuple(roots),
            network_denied=False,
            isolates_process_environments=False,
            metadata_writes_in_roots=True,
        )
    backend = filesystem_backend()
    if backend is None:
        return SandboxUnavailable(SandboxUnavailableReason.SANDBOX_UNAVAILABLE)
    network = _network_plan(backend, deny_network)
    if network is None:
        return SandboxUnavailable(SandboxUnavailableReason.NETWORK_ISOLATION_UNAVAILABLE)
    for root in roots:
        aliased = _hard_linked_file(root)
        if aliased is not None:
            return SandboxUnavailable(SandboxUnavailableReason.ALIASED_WRITABLE_ROOT, aliased)
    command = tuple(argv)
    readable: tuple[str, ...] = ()
    if backend is SandboxBackend.APPCONTAINER:
        # The repository's Windows dispatch policy: a bare name is resolved on
        # absolute PATH entries only, so the working directory (the copy) is
        # never searched implicitly, as with execvpe on POSIX. The launcher
        # then runs exactly this executable.
        from ouroboros.evaluation.command_dispatch import prepare_command

        try:
            command = prepare_command(command, env)
        except ValueError:
            # Raised only for a batch file whose arguments cmd.exe would reinterpret.
            return SandboxUnavailable(SandboxUnavailableReason.WINDOWS_BATCH_FILE, command[0])
        if os.path.splitext(command[0])[1].lower() in (".cmd", ".bat"):
            return SandboxUnavailable(SandboxUnavailableReason.WINDOWS_BATCH_FILE, command[0])
        readable = _windows_read_paths(roots)
    private_mounts = backend is SandboxBackend.LANDLOCK and _private_mounts_unshare() is not None
    return ConfinedCommand(
        argv=_backend_argv(backend, command, claims, network, readable),
        env=_bootstrap_environment(env),
        command_env=env,
        cwd=cwd,
        backend=backend,
        writable_roots=tuple(roots),
        network_denied=network is not NetworkPlan.ALLOW,
        isolates_process_environments=backend
        in (SandboxBackend.LANDLOCK, SandboxBackend.APPCONTAINER),
        private_dev_shm=private_mounts,
        metadata_writes_in_roots=backend is not SandboxBackend.LANDLOCK or private_mounts,
    )


@functools.cache
def _warn_disabled() -> None:
    log.warning(
        "exec_sandbox.disabled",
        detail="the caller switched the execution sandbox off; commands are not confined",
    )


__all__ = [
    "DEFAULT_ENV_PASSTHROUGH",
    "TEMP_DIRECTORY_VARIABLES",
    "ConfinedCommand",
    "SandboxBackend",
    "SandboxUnavailable",
    "SandboxUnavailableReason",
    "build_environment",
    "confine",
    "filesystem_backend",
    "NetworkPlan",
    "remove_persistent_read_grants",
    "sandbox_unavailable_reason",
]
