"""The one execution entry point for check processes, and their interpreter.

Admission and candidate verification execute Python scripts that a model
wrote, an oracle check runs the implementation under test in target
processes, and the reference check runs the constructor's reference
implementation. They always run on a throwaway copy of the checkout (or, for
a reference, a scratch directory holding only the reference module), without
a shell, under the per-check timeout (``boundary/admission.py``,
``boundary/oracle_run.py``). This module owns how every such process starts:

- **One entry point, confined.** Every process the check package starts (a
  script check, an oracle target process, a reference implementation run,
  the base run of a declared binding) gets its argv, environment and working
  directory from ``check_command``, which confines it with the shared
  execution sandbox (``runtime/exec_sandbox.confine``): it may write only
  beneath its own copy and its scratch directory, and its network is denied
  (loopback and Unix-domain sockets stay available). When the sandbox cannot
  confine it (no backend on this host, no network isolation, an aliased
  writable root), ``check_command`` returns ``CheckUnavailable`` with the
  sandbox's reason, nothing runs, and the caller records that check as
  indeterminate; there is no unconfined fallback. The sandbox's unsafe off
  switch (``OUROBOROS_EXEC_SANDBOX=off`` or ``execution.exec_sandbox:
  false``, ``config/exec_sandbox.py``) is read once, when the interpreter
  is pinned, and holds for the run.
- **Environment built from scratch.** The sandbox builds the environment
  from an allowlist (``CHECK_ENV_COPIED``: ``PATH``, the locale, ``TZ``,
  Python I/O settings, and on Windows the system variables any process
  needs); ``HOME`` is a directory in the check's scratch directory and the
  temp directory is the scratch directory itself, removed afterwards
  (``check_scratch``); ``VIRTUAL_ENV`` names the interpreter's virtualenv,
  whose scripts directory leads ``PATH``. ``PYTHONPATH`` is never copied.
- **Pinned interpreter.** A check's ``python3``/``python`` runs with the
  project's virtualenv interpreter when one is found (the checkout's, or the
  main working tree's when the checkout is a linked git worktree, then an
  active ``VIRTUAL_ENV``), else ``python3`` from ``PATH``. It is resolved
  once, before the worker starts, and pinned by the real path of its binary
  and that file's SHA-256 (``CheckInterpreter``). ``check_command`` verifies
  the pin before every process it prepares: a replaced symlink or binary
  makes the check indeterminate (``interpreter_changed``), never a run of
  the replacement. The choice is recorded in the admission and verification
  receipts.
- **Launched from the pinned binary.** A process starts later than it is
  prepared, so a pinned path could name another program by then. On POSIX a
  command that runs the pinned interpreter starts as ``boundary/_pinned_exec.py``
  under the controller's own interpreter (inside the sandbox, where it is
  confined): it opens the pinned binary once, checks it is still the pinned
  one reached from the pinned path, reports ``verified`` or ``changed`` on a
  pipe only the controller reads, closes it, and executes the interpreter
  with the virtualenv's path as ``argv[0]`` (so ``pyvenv.cfg`` is found as
  before). ``spawn_check_process`` starts every check process and reads that
  report (``CheckProcess.launch_problem``); a process that did not report
  ``verified`` ran nothing of the check's and is indeterminate
  (``interpreter_changed``, or ``launch_unverified`` when it reported
  nothing). Residual window: on Linux none, the verified file descriptor is
  what is executed (``fexecve``); on macOS, which cannot execute a
  descriptor, and for an interpreter that is a ``#!`` script wrapper, the
  instant between the check and ``execve`` of the real path.
  The pin covers the interpreter binary only (not its shared libraries,
  standard library or ``pyvenv.cfg``). On Windows the pinned path is
  executed as prepared.

Held-out cases: an expected value never leaves the controller's memory (it
is in no process's argv, environment or files), and a held-out input reaches
only the one target process that runs that case (over its stdin; for a CLI
oracle, as that process's arguments), and only in the final verification.
So the macOS residual below (another process's argv and environment are
readable) exposes no expected value.

Residual risk: reads are not restricted, so a check can read files the user
can read. On macOS ``sandbox-exec`` cannot deny ``KERN_PROCARGS2``, so a
confined process can read the argv and environment of the user's other
processes, the Ouroboros process included (``ConfinedCommand.isolates_process_environments``
is false there); under Landlock on Linux it cannot. Opt out of the check
package with ``--no-check-package``, ``OUROBOROS_CHECK_PACKAGE=off``, or
``boundary.check_package: off``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile

from ouroboros.config.exec_sandbox import exec_sandbox_enabled
from ouroboros.core.project_env import project_venv_python, venv_python
from ouroboros.runtime.exec_sandbox import DEFAULT_ENV_PASSTHROUGH, SandboxUnavailable, confine

# Windows cannot start a process, or find its system directories, without these.
CHECK_ENV_COPIED_WINDOWS: tuple[str, ...] = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PROGRAMDATA",
)
# Copied from the value source into every check process: the sandbox's own
# allowlist (``PATH``, the locale, ``TZ``, Python I/O settings), plus on
# Windows the system variables any process needs.
CHECK_ENV_COPIED: tuple[str, ...] = DEFAULT_ENV_PASSTHROUGH + (
    CHECK_ENV_COPIED_WINDOWS if sys.platform == "win32" else ()
)
CHECK_INTERPRETER_NAMES = frozenset({"python3", "python"})


def _interpreter_venv(interpreter: str | None) -> Path | None:
    """The virtualenv that ``interpreter`` belongs to (its ``pyvenv.cfg``), if any."""
    if not interpreter or not Path(interpreter).is_absolute():
        return None
    root = Path(interpreter).parent.parent
    return root if (root / "pyvenv.cfg").is_file() else None


def _check_overrides(scratch: Path, interpreter: str, source: Mapping[str, str]) -> dict[str, str]:
    """What a check process sets beyond the sandbox's environment: ``HOME`` and the venv."""
    home = scratch / "home"
    home.mkdir(exist_ok=True)
    env = {"HOME": str(home)}
    if sys.platform == "win32":
        env["USERPROFILE"] = str(home)
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    venv = _interpreter_venv(interpreter)
    if venv is not None:
        env["VIRTUAL_ENV"] = str(venv)
        scripts = str(Path(interpreter).parent)
        env["PATH"] = os.pathsep.join(filter(None, (scripts, source.get("PATH"))))
    return env


@contextmanager
def check_scratch(parent: Path | None = None) -> Iterator[Path]:
    """A fresh owner-only scratch directory for check processes, removed on exit.

    The directory is created in ``parent`` (the run's work directory, beside
    the checkout copy and never inside it) or, without one, in the system
    temp directory.
    """
    scratch = Path(tempfile.mkdtemp(prefix="ouroboros-check-env-", dir=parent))
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


INTERPRETER_CHANGED = "interpreter_changed"
INTERPRETER_UNAVAILABLE = "interpreter_unavailable"
LAUNCH_UNVERIFIED = "launch_unverified"
"""A process that should have reported its pinned launch reported nothing."""
_PINNED_EXEC = Path(__file__).with_name("_pinned_exec.py")
_PINNED_LAUNCH = sys.platform != "win32"
# In a prepared argv, where ``spawn_check_process`` puts the launch report's
# pipe descriptor (known only once the process is started).
_LAUNCH_CHANNEL = "{launch-channel}"


def _file_sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


@dataclass(frozen=True, slots=True)
class CheckInterpreter:
    """The interpreter that runs checks, pinned when it was chosen (``pin_interpreter``)."""

    path: str
    source: str
    """``project_venv``, ``active_venv``, or ``python3_fallback``."""
    realpath: str
    """The binary ``path`` resolved to when it was pinned."""
    sha256: str
    """SHA-256 of that binary when it was pinned; empty when it could not be read."""
    sandbox_enabled: bool = True
    """The execution-sandbox policy read when it was pinned (``config/exec_sandbox.py``):
    ``False`` only when the unsafe off switch was set. Fixed for the run."""

    @property
    def realpath_sha256(self) -> str:
        """SHA-256 of ``realpath`` as text: the pin's location, recordable without the path."""
        return hashlib.sha256(self.realpath.encode("utf-8")).hexdigest()

    def problem(self) -> str | None:
        """``None`` while the pin holds, else why this interpreter must not run."""
        if not self.sha256:
            return INTERPRETER_UNAVAILABLE
        try:
            real = os.path.realpath(self.path)
            unchanged = real == self.realpath and _file_sha256(real) == self.sha256
        except OSError:
            unchanged = False
        return None if unchanged else INTERPRETER_CHANGED


def pin_interpreter(path: str, source: str) -> CheckInterpreter:
    """Pin ``path`` by the real path of its binary and that file's SHA-256.

    A path with a directory part is made absolute here, against the current
    directory, but not resolved (a virtualenv's ``bin/python3`` stays its
    own path): every check runs in its own copy of the checkout, where a
    relative path would name nothing. A bare name is left as it is. The
    sandbox policy is read here, once, and travels with the pin.
    """
    if os.path.dirname(path):
        path = os.path.abspath(path)
    real = os.path.realpath(path)
    try:
        digest = _file_sha256(real) if os.path.isfile(real) else ""
    except OSError:
        digest = ""
    return CheckInterpreter(path, source, real, digest, exec_sandbox_enabled())


def default_interpreter() -> CheckInterpreter:
    """``python3`` from ``PATH``, pinned now (for a caller that pinned none, such as a test)."""
    return pin_interpreter(shutil.which("python3") or "python3", "python3_fallback")


@dataclass(frozen=True, slots=True)
class CheckCommand:
    """What to spawn: ``argv`` in ``cwd`` with exactly ``env`` (never through a shell).

    ``argv`` and ``env`` are the sandbox's (``ConfinedCommand.argv`` and its
    launchers' bootstrap ``env``); the check itself runs with the check
    environment inside the sandbox. Start it with ``spawn_check_process``
    (``stdin`` ``DEVNULL`` or a pipe the caller owns, ``stdout``/``stderr``
    pipes or ``DEVNULL``): the only other descriptor it inherits is the
    write end of its launch report's pipe, which the launch step closes
    before the interpreter runs.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str


@dataclass(frozen=True, slots=True)
class CheckUnavailable:
    """Nothing was prepared; the caller records the check as indeterminate with ``reason``."""

    reason: str
    detail: str = ""


def check_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    writable_root: Path,
    interpreter: CheckInterpreter,
    scratch: Path,
    source: Mapping[str, str] | None = None,
) -> CheckCommand | CheckUnavailable:
    """How to run ``argv`` for the check package, confined, or why it must not run.

    A bare ``python3``/``python`` (or the pinned path) in ``argv[0]`` becomes
    the pinned interpreter, after the pin is verified, and on POSIX runs
    through ``_pinned_exec.py``, which verifies it again as it starts it
    (start it with ``spawn_check_process``). The command is confined by the
    shared execution sandbox (``runtime/exec_sandbox.confine``): it may write
    only beneath ``writable_root`` (the check's own copy) and ``scratch``
    (its temp and ``HOME`` directory, from ``check_scratch``), and its
    network is denied. ``source`` (default: this process's environment) is
    only the value source of ``CHECK_ENV_COPIED``. When the sandbox cannot
    confine the command, its typed reason is returned and nothing runs.
    """
    problem = interpreter.problem()
    if problem is not None:
        return CheckUnavailable(problem, interpreter.path)
    resolved = tuple(argv)
    if resolved and resolved[0] in (*CHECK_INTERPRETER_NAMES, interpreter.path):
        resolved = (interpreter.path, *resolved[1:])
        if _PINNED_LAUNCH:
            # Started from the pinned binary itself (``_pinned_exec.py``).
            resolved = (
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(_PINNED_EXEC),
                interpreter.path,
                interpreter.realpath,
                interpreter.sha256,
                _LAUNCH_CHANNEL,
                "--",
                *resolved[1:],
            )
    values = os.environ if source is None else source
    confined = confine(
        resolved,
        cwd=str(cwd),
        writable_roots=(str(writable_root),),
        temp_dir=str(scratch),
        deny_network=True,
        env_source=values,
        env_passthrough=CHECK_ENV_COPIED,
        env_set=_check_overrides(scratch, interpreter.path, values),
        enabled=interpreter.sandbox_enabled,
    )
    if isinstance(confined, SandboxUnavailable):
        return CheckUnavailable(confined.reason.value, confined.detail)
    return CheckCommand(confined.argv, confined.env, confined.cwd)


class CheckProcess(asyncio.subprocess.Process):
    """A started check process: its transport (so its pipes can be closed) and launch report."""

    def __init__(
        self,
        transport: asyncio.SubprocessTransport,
        protocol: asyncio.subprocess.SubprocessStreamProtocol,
        loop: asyncio.AbstractEventLoop,
        report: int | None = None,
    ) -> None:
        super().__init__(transport, protocol, loop)
        self.transport = transport
        self._report = report
        self._launch: str | None = None

    def launch_problem(self) -> str | None:
        """``None`` when the process started from the pinned interpreter (or needed none).

        Read once the process has ended: ``interpreter_changed`` when the
        launch found another interpreter than the pinned one and ran
        nothing, ``launch_unverified`` when it reported nothing (it failed,
        or was killed at its deadline, before it could report). Every caller
        of ``spawn_check_process`` reads it once the process is reaped.
        """
        if self._report is not None:
            os.set_blocking(self._report, False)
            try:
                report = os.read(self._report, 64)
            except BlockingIOError:
                report = b""
            finally:
                os.close(self._report)
                self._report = None
            self._launch = {b"verified": None, b"changed": INTERPRETER_CHANGED}.get(
                report, LAUNCH_UNVERIFIED
            )
        return self._launch


async def spawn_check_process(
    command: CheckCommand, *, stdin: int, stderr: int, limit: int = 64 * 1024
) -> CheckProcess:
    """Start ``command`` in its own session and process group, its stdout on a pipe.

    ``command`` comes from ``check_command``; ``stdin`` and ``stderr`` are
    ``PIPE`` or ``DEVNULL``. A command that runs the pinned interpreter gets
    the write end of a fresh pipe for its launch report, which only the
    launch step holds and closes before the interpreter runs
    (``CheckProcess.launch_problem``). Raises ``OSError`` when it cannot be
    started.
    """
    argv = list(command.argv)
    report: int | None = None
    channel: tuple[int, ...] = ()
    if _LAUNCH_CHANNEL in argv:
        report, writer = os.pipe()
        argv[argv.index(_LAUNCH_CHANNEL)] = str(writer)
        channel = (writer,)
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.subprocess_exec(
            lambda: asyncio.subprocess.SubprocessStreamProtocol(limit=limit, loop=loop),
            *argv,
            cwd=command.cwd,
            env=dict(command.env),
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            start_new_session=sys.platform != "win32",
            pass_fds=channel,
        )
    except BaseException:
        if report is not None:
            os.close(report)
        raise
    finally:
        for descriptor in channel:
            os.close(descriptor)
    return CheckProcess(transport, protocol, loop, report)


def resolve_check_interpreter(
    checkout: Path, environ: Mapping[str, str] | None = None
) -> CheckInterpreter:
    """Pick and pin the interpreter for ``python3``/``python`` in a check's argv."""
    found = project_venv_python(checkout)
    if found is not None:
        return pin_interpreter(str(found), "project_venv")
    source = os.environ if environ is None else environ
    active = source.get("VIRTUAL_ENV", "").strip()
    if active:
        found = venv_python(Path(active))
        if found is not None:
            return pin_interpreter(str(found), "active_venv")
    return default_interpreter()


__all__ = [
    "CHECK_ENV_COPIED",
    "CHECK_ENV_COPIED_WINDOWS",
    "CHECK_INTERPRETER_NAMES",
    "INTERPRETER_CHANGED",
    "INTERPRETER_UNAVAILABLE",
    "LAUNCH_UNVERIFIED",
    "CheckCommand",
    "CheckInterpreter",
    "CheckProcess",
    "CheckUnavailable",
    "check_command",
    "check_scratch",
    "default_interpreter",
    "pin_interpreter",
    "resolve_check_interpreter",
    "spawn_check_process",
]
