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
  (``/dev/null`` and friends). Everything else, including the live
  workspace, the user's home directory and the system temp directory, is
  read-only. Reading and executing are not restricted.
  Each root is claimed by identity: ``confine`` records its real path,
  device and inode, and the helper opens it without following a symlink and
  refuses to run the command unless it is still that directory (on Linux the
  Landlock rule is bound to that very descriptor). A root holding a regular
  file with another hard link is refused: the other link may be outside, and
  writing through the root would change it. ``confine`` reports that early as
  ``aliased_writable_root``; the helper checks again from the verified
  descriptors after the restriction is in place, immediately before exec, and
  runs nothing if a link appeared in between.
  "Read-only" covers content, names (create, remove, rename, link) and
  metadata the process sets (mode, ownership, timestamps, extended
  attributes, inode flags). The access time the kernel records when a
  permitted read happens is part of read access, not a write the process
  performs: it changes under ``sandbox-exec`` with every write denied as
  well, and only a mount option (``noatime``) can stop it.
- **Network** (``deny_network=True``, reported as ``network_denied``):
  non-loopback IP traffic is denied. Loopback (``localhost``) and Unix-domain
  sockets stay available for local IPC.
- **Other processes' environments** (``ConfinedCommand.isolates_process_environments``):
  under Landlock a confined process cannot read ``/proc/<pid>/environ``,
  ``mem`` or ``maps`` of any process outside its domain, the controller and
  every other process of the user included, because Landlock denies
  ptrace-mode access across the domain boundary; its own ``/proc/self`` and
  its descendants' stay readable. ``sandbox-exec`` cannot deny the macOS
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
  and so does ``HOME`` unless the caller passes it through; ``env_set``
  values are applied last. That environment (``ConfinedCommand.command_env``)
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
  metadata changes, so the filter denies the chmod, chown, utime and xattr
  syscall families, the inode-flag ioctls and io_uring. It cannot see paths,
  so on Linux metadata changes are denied inside the writable roots too
  (``touch`` on an existing file, ``shutil.copy2``/``copystat``, cargo's
  fingerprint timestamps, tar extraction that restores modes); such a
  command fails, which fails closed. On macOS the same helper runs inside ``sandbox-exec``
  and only applies the command's environment. It is unprivileged and needs no mount or user
  namespace, so it works in containers. ``confine`` selects one
  ``NetworkPlan``: a new unprivileged network namespace
  (``unshare --user --map-root-user --net``, with ``lo`` brought up), or the
  current one when it has only loopback (a container started with
  ``--network none``). The namespace state is never cached (only the
  ``unshare`` capability is), and the parent's choice is never the proof:
  the helper checks that its namespace has only ``lo`` immediately before
  exec and runs nothing otherwise.
- **Anything else** (Windows, a Linux kernel without Landlock, a macOS
  process that is already sandboxed): no backend, and ``confine`` returns
  ``SandboxUnavailable``. A command is never run unconfined as a fallback.

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
roots while the command runs, and reads of anything the user can read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import functools
import json
import os
from pathlib import Path
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
# Must match ``_confine_exec.COMMAND_ENV_VARIABLE`` (the helper is not imported).
_COMMAND_ENV_VARIABLE = "OUROBOROS_SANDBOX_COMMAND_ENV"

# macOS: profile parameters ``W0``..``Wn`` carry the writable roots.
_DARWIN_DEVICE_RULES = (
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
    """Whether the command cannot read other processes' environments (Landlock only)."""


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
    """A regular file beneath ``root`` with more than one link, or None.

    Symlinks and linked directories are not followed. A directory that cannot
    be listed or an entry that cannot be examined is returned as well: it may
    hold such a link, so the root cannot be shown to be free of aliases.
    """
    failures: list[str] = []

    def unreadable(error: OSError) -> None:
        failures.append(f"{error.filename}: {error.strerror}")

    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False, onerror=unreadable):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                status = os.lstat(path)
            except OSError as exc:
                return f"{path}: {exc.strerror}"
            if stat.S_ISREG(status.st_mode) and status.st_nlink > 1:
                return path
    return failures[0] if failures else None


def _backend_argv(
    backend: SandboxBackend,
    argv: Sequence[str],
    roots: Sequence[_RootClaim],
    network: NetworkPlan,
) -> tuple[str, ...]:
    """The argv running ``argv`` under ``backend`` with the ``network`` plan.

    Every backend ends in ``_confine_exec.py``, which applies the command's
    environment only when it execs the command, inside the sandbox. On Linux,
    whenever the network is denied, the helper checks immediately before exec
    that its namespace has only ``lo`` (``--require-loopback-only``): the
    plan is a parent-side choice, never the final proof.
    """
    helper = (sys.executable, "-I", "-S", "-B", str(_CONFINE_HELPER))
    claims = [part for path, dev, ino in roots for part in ("--root", path, str(dev), str(ino))]
    if backend is SandboxBackend.SANDBOX_EXEC:
        executable = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
        profile = _darwin_profile(len(roots), deny_network=network is NetworkPlan.PROFILE)
        params = [
            part for index, (path, _, _) in enumerate(roots) for part in ("-D", f"W{index}={path}")
        ]
        return (executable, "-p", profile, *params, "--", *helper, *claims, "--", *argv)
    if network is NetworkPlan.NEW_NAMESPACE:
        unshare = _unshare_prefix()
        if unshare is None:  # pragma: no cover - the plan was selected from it
            raise RuntimeError("unshare is not available")
        # A fresh namespace starts with ``lo`` down; bring it up so only
        # non-loopback traffic is denied.
        prefix: tuple[str, ...] = (*unshare, *helper, "--loopback-up", "--require-loopback-only")
    elif network is NetworkPlan.CURRENT_NAMESPACE:
        prefix = (*helper, "--require-loopback-only")
    else:
        prefix = helper
    return (*prefix, "--landlock", *claims, "--", *argv)


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
    command = (sys.executable, "-I", "-S", "-B", str(_PROBE), str(inside), str(outside))
    argv = command if argv_prefix is None else (*argv_prefix, *command)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            env=_bootstrap_environment({"PATH": os.defpath}),
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
        candidate = SandboxBackend.SANDBOX_EXEC
    elif sys.platform.startswith("linux"):
        candidate = SandboxBackend.LANDLOCK
    else:
        return None
    probe_root = Path(tempfile.mkdtemp(prefix="ouroboros-sandbox-probe-")).resolve()
    try:
        (probe_root / "baseline").mkdir()
        (probe_root / "confined").mkdir()
        baseline = _probe_matrix(None, probe_root / "baseline")
        (probe_root / "confined" / "inside").mkdir()
        inside = _claim_root(str(probe_root / "confined" / "inside"))
        prefix = _backend_argv(candidate, (), (inside,), NetworkPlan.ALLOW)
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
    if _process_has_only_loopback():
        return NetworkPlan.CURRENT_NAMESPACE
    if _unshare_prefix() is not None:
        return NetworkPlan.NEW_NAMESPACE
    return None


@functools.cache
def _unshare_prefix() -> tuple[str, ...] | None:
    """``unshare --user --map-root-user --net`` if it works here; probed once."""
    executable = shutil.which("unshare") or ""
    if not executable:
        return None
    prefix = (executable, "--user", "--map-root-user", "--net", "--")
    probe = shutil.which("true") or "/bin/true"
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*prefix, probe], capture_output=True, timeout=_PROBE_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return prefix if result.returncode == 0 else None


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
    env.setdefault("HOME", temp_dir)
    env.update(overrides or {})
    return env


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
    too. ``argv`` is run directly, never through a shell. ``enabled`` is the
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
    return ConfinedCommand(
        argv=_backend_argv(backend, argv, claims, network),
        env=_bootstrap_environment(env),
        command_env=env,
        cwd=cwd,
        backend=backend,
        writable_roots=tuple(roots),
        network_denied=network is not NetworkPlan.ALLOW,
        isolates_process_environments=backend is SandboxBackend.LANDLOCK,
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
    "sandbox_unavailable_reason",
]
