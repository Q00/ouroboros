"""Controller side of an oracle check: target processes, then an in-process comparison.

Threat model: the adversary is the code under test (and the worker that
wrote it), running as the same OS user as the controller. Every target
process is confined by the shared execution sandbox through the check
execution entry point (``check_env.check_command``): it writes only beneath
its checkout copy and scratch directory, and has no network; reads are not
restricted. The design goal is that expected values cannot be read by a
target and that a verdict never rests on more than a target can show.

What an observation is. A Python oracle's target process imports the
candidate's code before it calls anything, and that code runs in the same
process as the harness: it can read the nonce from the process arguments,
find the frame pipe, and write any frame it likes. An observation is
therefore what the candidate's code reports for the inputs it was given, not
proof that the bound callable ran. The nonce and the frames are a parsing
convention that keeps the target's own prints apart from its report; they
are not an authority boundary, and nothing here treats them as one. What
the controller does control: the inputs (sent one case per process, after
the target reported that it resolved), the expected values (never sent), and
the comparison (in its own memory). So a reported value can match an expected
value only if the candidate computed it for those inputs. Held-out inputs are
sent only in the terminal verification, so passing a held-out case means the
candidate produced the rule's output for an input it had never seen; that is
the only kind of pass that can verify a criterion (``boundary/acceptance.py``).
A forged frame for a visible case, whose expected value the specification
states, can make that case pass and nothing more: a visible-only pass is
unverified, and the legacy verifier decides the criterion. A forged frame
can never make a case fail that the candidate's code passes (the candidate
would be forging against itself), and admission runs the base checkout's
code, not the worker's, so neither admission nor a failure's authority
depends on the frames.

One oracle check runs as follows:

1. **Target**, one process per case, in the project interpreter
   (``<interpreter> -I -B -c <harness> target <nonce> <call_kind> <symbol>
   <setup>``) with the checkout copy as cwd, in its own session and process
   group. It makes the oracle's declared setup calls, imports and resolves
   the bound symbol, and writes a ``resolved`` frame; only
   then does the controller send that case's inputs on stdin. It writes the
   observation as a ``result`` frame. Frames are JSON after a per-process
   random nonce on a pipe; every other line is ignored, and the target code's
   own output goes to stderr, which is discarded. A CLI oracle's target is the
   bound script or module: before every case the controller proves its
   files are regular files of the checkout (``_cli_target_files``), and the
   harness ``cli`` role runs the bytes of exactly those files, found again
   by identity in the target process, never a pathname opened anew. When
   the case is over, the controller kills the
   target's whole process group (on Linux also every process still in its
   session), closes its own ends of the pipes, and waits, bounded, for the
   group to be empty. One absolute deadline, taken before anything starts,
   bounds every launch, read, write, kill and reap of the check; reaping may
   take at most ``REAP_MARGIN_SECONDS`` past it, and a case still running at
   its deadline is a timeout.
2. **Comparison**, in the controller process itself, with the comparison
   functions of the harness module (``boundary/harness.py``), imported with
   this module before any target runs. The controller parses each
   frame with a bounded parser (size, depth, node count, integer digits,
   finite floats), validates the observation's fields, and compares against
   the frozen expectations it holds in memory. Nothing is imported, executed,
   or read from disk to decide the verdict after the first target starts.

Expected values therefore never reach a target process, a file, or another
process. A target that escapes the kill (a double fork plus ``setsid``)
cannot change a decided case either: each case is compared in the
controller's memory from frame bytes that were already read, and no later
channel exists.

Outcomes (``OracleRun``):

- a failure before any target code runs (oracle files missing, the harness
  differs from this product version, no interpreter, the first target cannot
  be launched) is indeterminate, and so is a target this host cannot prove to
  be, or not to be, checkout code (``unprovable``);
- on the base (admission, and the one base run of a late binding), anything
  but a clean observation of every case (no ``resolved`` frame, an import
  error, a crash, a timeout, a malformed frame) is indeterminate: a base run
  never counts an accident as the intended failure. ``missing`` (the bound
  symbol does not exist) fails every case, which is the expected reproduction
  failure;
- on a candidate, the target's code has run once the first process starts, so
  every anomaly is that case failing with a counterexample: a crash, a
  timeout, a malformed or oversized frame, a missing field, a value out of
  range, or a target that resolves on one call and not on another. ``missing``
  and an import error on the first case fail every case. One bad case never
  makes the other cases undecided.

Exit codes of ``OracleRun``: 0 every case passed, 1 a case failed (the
failure signature is "seen"), 3 indeterminate, ``None`` with ``timed_out``.
``OracleRun.unavailable`` names why nothing ran when the check execution
entry point (``check_env.check_command``) refused a process, or when the case
selection is empty.

Case selection: with ``include_held_out`` false only the visible cases run and
are compared (the per-attempt gate); the held-out cases, inputs included, are
never sent to any process. The terminal verification runs every case.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
from importlib.machinery import EXTENSION_SUFFIXES
import json
import math
import os
from pathlib import Path
import secrets
import signal
import sys
import time
from typing import Any

from ouroboros.boundary import harness as harness_module
from ouroboros.boundary.binding import Binding, CallKind
from ouroboros.boundary.check_env import (
    CheckCommand,
    CheckInterpreter,
    CheckProcess,
    CheckUnavailable,
    check_command,
    check_scratch,
    default_interpreter,
    spawn_check_process,
)
from ouroboros.boundary.oracle import (
    ORACLE_DATA_PATH,
    ORACLE_HARNESS_PATH,
    ORACLE_HARNESS_SOURCE,
    OracleSpec,
    imported_before_checkout,
)
from ouroboros.core.filesystem_capability import (
    CheckoutFileRefusal,
    RegularFile,
    resolve_checkout_file,
)

_FRAME_LIMIT = 8 * 1024 * 1024
_CLI_OUTPUT_LIMIT = 1024 * 1024
# The harness ``cli`` role writes one short frame on stderr, nothing else.
_CLI_FRAME_LIMIT = 64 * 1024
_READ_CHUNK = 64 * 1024
_MAX_EXCEPTION_NAMES = 64  # an exception's class hierarchy, never longer in practice
_MAX_NAME_CHARS = 200
_MAX_DEPTH = 64
_MAX_NODES = 1_000_000
_MAX_INT_DIGITS = 1000
REAP_MARGIN_SECONDS = 1.0
"""How long past its deadline a killed check process may take to be reaped.

The only time a check spends beyond its timeout: killing is immediate, so
this is waiting for the kernel to report what was already killed."""
_POSIX = sys.platform != "win32"
_LINUX = sys.platform.startswith("linux")
_OBSERVED = frozenset({"returned", "raised"})


@dataclass(frozen=True, slots=True)
class OracleRun:
    """What one oracle check did, in the shape of a completed command."""

    return_code: int | None
    timed_out: bool
    launch_error: str | None
    output: str
    result: dict[str, Any] | None
    duration: float
    unavailable: str | None = None
    """Why no target ran (``check_env.CheckUnavailable``, ``no_visible_case``)."""

    @property
    def signature_seen(self) -> bool:
        return self.return_code == 1


NO_VISIBLE_CASE = "no_visible_case"


class _Unavailable(Exception):
    """The entry point refused a target process; nothing more runs for this check."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_Prepare = Callable[[Sequence[str]], CheckCommand | CheckUnavailable]


def _prepared(prepare: _Prepare, argv: Sequence[str]) -> CheckCommand:
    command = prepare(argv)
    if isinstance(command, CheckUnavailable):
        raise _Unavailable(command.reason)
    return command


# --------------------------------------------------------------------------
# Bounded frame parsing


def _bounded_int(text: str) -> int:
    if len(text.lstrip("-")) > _MAX_INT_DIGITS:
        raise ValueError("integer out of range")
    return int(text)


def _bounded_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("float out of range")
    return value


def _reject_constant(text: str) -> Any:
    raise ValueError(f"{text} is not plain JSON")


def _within_bounds(value: Any) -> bool:
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > _MAX_DEPTH or nodes > _MAX_NODES:
            return False
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return True


def parse_frame(raw: bytes) -> dict[str, Any] | None:
    """One frame's JSON object, or ``None`` when it is malformed or out of bounds."""
    if len(raw) > _FRAME_LIMIT:
        return None
    try:
        value = json.loads(
            raw,
            parse_int=_bounded_int,
            parse_float=_bounded_float,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError, OverflowError, MemoryError):
        return None
    if not isinstance(value, dict) or not _within_bounds(value):
        return None
    return value


def valid_entry(entry: Any, case_id: str) -> bool:
    """Whether a ``result`` frame's entry has every field the comparison reads."""
    if not isinstance(entry, dict) or entry.get("case_id") != case_id:
        return False
    text = entry.get("repr")
    # The harness truncates ``repr`` to MAX_REPR; a longer one is forged and
    # would otherwise reach receipts and repair text unbounded.
    if entry.get("outcome") not in _OBSERVED or not isinstance(text, str):
        return False
    if len(text) > harness_module.MAX_REPR:
        return False
    if entry["outcome"] == "returned":
        encodable = entry.get("encodable")
        return isinstance(encodable, bool) and (not encodable or "value" in entry)
    names = entry.get("exception")
    return (
        isinstance(names, list)
        and 0 < len(names) <= _MAX_EXCEPTION_NAMES
        and all(isinstance(n, str) and len(n) <= _MAX_NAME_CHARS for n in names)
    )


# --------------------------------------------------------------------------
# Target processes


def _session_members(leader: int) -> list[int]:
    """Linux: processes still in the target's process group or session."""
    if not _LINUX:
        return []
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    own = os.getpid()
    members = []
    for name in names:
        if not name.isdigit() or int(name) == own:
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                stat = handle.read()
            # Fields after the command name: state, ppid, pgrp, session.
            fields = stat[stat.rfind(b")") + 2 :].split()
            if int(fields[2]) == leader or int(fields[3]) == leader:
                members.append(int(name))
        except (OSError, IndexError, ValueError):
            continue
    return members


def kill_check_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the target's process group (and, on Linux, its session)."""
    if not _POSIX:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        return
    # The group outlives its leader while any member is alive, so it is
    # killed whether or not the leader has already exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    for pid in _session_members(process.pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue


async def reap_check_process(process: CheckProcess, deadline: float) -> int | None:
    """Kill the process group, close the controller's pipe ends, and reap the leader.

    The group is killed whether or not its leader has already exited: it
    outlives the leader while any member is alive. The controller's pipe ends
    are closed, so a process that left the group (a double fork plus
    ``setsid``) and still holds the other ends cannot delay the reap (asyncio
    reports an exit only once every pipe is closed). Waiting for the leader
    and for the group to be empty ends by ``deadline`` (event loop time) plus
    ``REAP_MARGIN_SECONDS``. Returns the leader's exit status, ``None`` when
    it was not reported by then.
    """
    kill_check_group(process)
    # Only the pipes: closing the whole transport would poll the leader
    # from here and race the child watcher for its exit status.
    for fd in (0, 1, 2):
        pipe = process.transport.get_pipe_transport(fd)
        if pipe is not None:
            pipe.close()
    loop = asyncio.get_running_loop()
    limit = deadline + REAP_MARGIN_SECONDS
    try:
        await asyncio.wait_for(process.wait(), timeout=max(limit - loop.time(), 0))
    except TimeoutError:
        pass
    if _POSIX:
        while loop.time() < limit:
            try:
                os.killpg(process.pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            kill_check_group(process)
            await asyncio.sleep(0.02)
    return process.returncode


async def _next_frame(
    process: asyncio.subprocess.Process, nonce: str, deadline: float
) -> tuple[str, dict[str, Any] | None]:
    """``("frame", payload)``, or ``("eof" | "timeout" | "malformed", None)``."""
    assert process.stdout is not None
    prefix = (nonce + " ").encode("ascii")
    loop = asyncio.get_running_loop()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return "timeout", None
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
        except TimeoutError:
            return "timeout", None
        except (ValueError, asyncio.LimitOverrunError):
            return "malformed", None
        if not line:
            return "eof", None
        if not line.startswith(prefix):
            continue  # not a frame: ignored
        payload = parse_frame(line[len(prefix) :])
        return ("frame", payload) if payload is not None else ("malformed", None)


class CappedOutput:
    """Output read under a hard byte cap while it streams.

    ``fill`` stops reading once more than ``limit`` bytes arrived and sets
    ``overflow``; at most ``limit`` bytes (plus one read chunk in flight) are
    ever held, whatever the process writes. The caller kills the process.
    Partial output survives a cancelled ``fill`` (for a timeout).
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.overflow = False
        self._chunks: list[bytes] = []
        self._size = 0

    async def fill(self, reader: asyncio.StreamReader | None) -> None:
        while reader is not None and not self.overflow:
            chunk = await reader.read(_READ_CHUNK)
            if not chunk:
                return
            room = self.limit - self._size
            if len(chunk) > room:
                self._chunks.append(chunk[:room])
                self._size = self.limit
                self.overflow = True
                return
            self._chunks.append(chunk)
            self._size += len(chunk)

    @property
    def data(self) -> bytes:
        return b"".join(self._chunks)


@dataclass(frozen=True, slots=True)
class _Case:
    """One target process.

    ``kind`` is ``observed`` (``entry`` is the observation, possibly abnormal),
    ``resolve`` (``missing``, ``import_error``, or ``unprovable``: this host
    cannot prove the target is a checkout file, which decides nothing),
    ``setup`` (the target never produced a valid ``resolved`` frame; ``entry``
    says how, for a candidate), or ``launch_error``.
    """

    kind: str
    entry: dict[str, Any] | None = None
    resolve: str = "ok"
    detail: str = ""
    timed_out: bool = False


def _timeout_case(case_id: str, *, observed: bool) -> _Case:
    """The case at its deadline: before (setup) or after its inputs were sent."""
    if observed:
        return _Case("observed", entry={"case_id": case_id, "outcome": "timeout"})
    return _Case(
        "setup",
        entry={"case_id": case_id, "outcome": "timeout"},
        resolve="setup_timeout",
        timed_out=True,
    )


async def _python_case(
    command: CheckCommand,
    nonce: str,
    call: dict[str, Any],
    deadline: float,
) -> _Case:
    """One target process, from launch to reap, within ``deadline`` (event loop time)."""
    case_id = call["case_id"]
    loop = asyncio.get_running_loop()
    if loop.time() >= deadline:
        return _timeout_case(case_id, observed=False)
    try:
        process = await spawn_check_process(
            command,
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=_FRAME_LIMIT,
        )
    except OSError as exc:
        return _Case("launch_error", resolve="launch_failed", detail=f"{type(exc).__name__}: {exc}")
    try:
        outcome = await _python_exchange(process, nonce, call, deadline)
    finally:
        await reap_check_process(process, deadline)
        refused = process.launch_problem()
    if refused is not None:
        # Not started from the pinned interpreter: nothing of the check ran.
        raise _Unavailable(refused)
    if loop.time() > deadline and (outcome.entry or {}).get("outcome") != "timeout":
        # Its reap ran past the deadline: nothing it reported stands.
        return _timeout_case(case_id, observed=outcome.kind == "observed")
    return outcome


async def _python_exchange(
    process: CheckProcess, nonce: str, call: dict[str, Any], deadline: float
) -> _Case:
    """Read the ``resolved`` frame, send the case's inputs, read the ``result`` frame."""
    case_id = call["case_id"]
    loop = asyncio.get_running_loop()
    status, frame = await _next_frame(process, nonce, deadline)
    if status == "timeout":
        return _timeout_case(case_id, observed=False)
    if status == "eof":
        code = await reap_check_process(process, deadline)
        return _Case(
            "setup",
            entry={"case_id": case_id, "outcome": "crashed", "exit": code},
            resolve="setup_failed",
            detail="no resolved frame",
        )
    resolve = (frame or {}).get("resolve")
    if (frame or {}).get("phase") != "resolved" or resolve not in (
        "ok",
        "missing",
        "import_error",
        "unprovable",
    ):
        return _Case(
            "setup",
            entry={"case_id": case_id, "outcome": "malformed"},
            resolve="frame_malformed",
        )
    detail = (frame or {}).get("detail")
    if resolve != "ok":
        return _Case(
            "resolve",
            resolve=str(resolve),
            detail=(detail if isinstance(detail, str) else "")[:500],
        )
    assert process.stdin is not None
    try:
        # Bounded like everything else in the case: a target that
        # never reads its stdin cannot stall the controller.
        process.stdin.write((json.dumps(call) + "\n").encode("utf-8"))
        await asyncio.wait_for(process.stdin.drain(), timeout=max(deadline - loop.time(), 0.01))
        process.stdin.close()
    except TimeoutError:
        return _Case("observed", entry={"case_id": case_id, "outcome": "timeout"})
    except (BrokenPipeError, ConnectionResetError):
        pass
    status, frame = await _next_frame(process, nonce, deadline)
    if status == "timeout":
        return _Case("observed", entry={"case_id": case_id, "outcome": "timeout"})
    if status == "eof":
        code = await reap_check_process(process, deadline)
        return _Case("observed", entry={"case_id": case_id, "outcome": "crashed", "exit": code})
    entry = (frame or {}).get("entry")
    if (frame or {}).get("phase") != "result" or not valid_entry(entry, case_id):
        return _Case("observed", entry={"case_id": case_id, "outcome": "malformed"})
    assert isinstance(entry, dict)
    return _Case("observed", entry=entry)


# A module file Python would import before the ``.py`` source beside it.
_EXTENSION_SUFFIXES = (*EXTENSION_SUFFIXES, ".so", ".pyd")


def _names(cwd: Path, directory: str) -> list[str] | None:
    try:
        return os.listdir(cwd / directory) if directory else os.listdir(cwd)
    except OSError:
        return None


def _shadowed(names: Sequence[str], stem: str) -> bool:
    """Whether Python could load ``stem`` from something other than ``stem.py``.

    An extension module is imported before the source file, and bytecode
    in ``__pycache__`` (which a checkout copy never has, so only code that
    already ran can have put it there) stands in for a source file.
    """
    return "__pycache__" in names or any(
        name.startswith(stem + ".") and name.endswith(_EXTENSION_SUFFIXES) for name in names
    )


@dataclass(frozen=True, slots=True)
class _NotProven:
    """Why a CLI target's code is not proven to be checkout files.

    ``resolve`` is ``missing`` when it is not the checkout's code, and
    ``unprovable`` when it may be but this host cannot tell (which decides
    nothing, on the base or on a candidate).
    """

    resolve: str
    reason: str


_NOT_CHECKOUT_CODE = frozenset(
    {
        CheckoutFileRefusal.MISSING,
        CheckoutFileRefusal.LINK,
        CheckoutFileRefusal.NOT_DIRECTORY,
        CheckoutFileRefusal.NOT_REGULAR,
        CheckoutFileRefusal.NOT_CHECKOUT_RELATIVE,
    }
)


def _module_files(cwd: Path, dotted: str) -> tuple[str, ...] | _NotProven:
    """The checkout files ``python -m dotted`` runs from ``cwd``, or why they are not known.

    ``-m`` puts ``cwd`` first on the module search path, but a built-in, frozen
    or already imported module comes before any path, so a top-level name of
    the standard library is never the checkout's (``missing``). Each parent
    must be a regular package (``__init__.py``): a namespace package can be
    merged with, or shadowed by, a package elsewhere on the path
    (``unprovable``). The module is ``<name>.py``, or a package's
    ``__init__.py`` and ``__main__.py``.
    """
    parts = dotted.split(".")
    if imported_before_checkout(parts[0]):
        return _NotProven("missing", "standard_library_module")
    files: list[str] = []
    directory = ""
    for index, part in enumerate(parts):
        names = _names(cwd, directory)
        if names is None or _shadowed(names, part):
            return _NotProven("unprovable", "module_not_provable")
        package = _names(cwd, directory + part) if part in names else None
        if package is not None and "__init__.py" in package:
            if _shadowed(package, "__init__") or _shadowed(package, "__main__"):
                return _NotProven("unprovable", "module_not_provable")
            files.append(f"{directory}{part}/__init__.py")
            directory = f"{directory}{part}/"
            if index == len(parts) - 1:
                files.append(f"{directory}__main__.py")
            continue
        if index < len(parts) - 1:
            return _NotProven("unprovable", "namespace_package")
        files.append(f"{directory}{part}.py")
    return tuple(files)


def _cli_target_files(cwd: Path, symbol: str) -> _NotProven | tuple[tuple[str, RegularFile], ...]:
    """The CLI target's code as proven regular checkout files: each path and its identity.

    Every file is proven through ``resolve_checkout_file`` from ``cwd``,
    never through a link; the target process runs these files' bytes only
    after it finds the same files there (the harness ``cli`` role). Otherwise
    the outcome is the one the harness gives a Python target
    (``boundary/harness.py``): ``missing`` when the code is not a checkout
    file (a link anywhere on its path, a directory, a special file, a
    standard library module), ``unprovable`` when this host cannot tell (no
    no-follow traversal, an unreadable or moving path, a namespace package,
    an extension module or bytecode beside the source) or cannot run the
    proven bytes (an executable that is not a Python script or module).
    """
    if symbol.startswith("-m "):
        files = _module_files(cwd, symbol[3:])
    elif symbol.endswith(".py"):
        files = (symbol,)
    else:
        return _NotProven("unprovable", f"{symbol}: not_a_python_target")
    if isinstance(files, _NotProven):
        return _NotProven(files.resolve, f"{symbol}: {files.reason}")
    proven = []
    for relative in files:
        proof = resolve_checkout_file(cwd, relative)
        if isinstance(proof, CheckoutFileRefusal):
            resolve = "missing" if proof in _NOT_CHECKOUT_CODE else "unprovable"
            return _NotProven(resolve, f"{symbol}: {relative}: {proof.value}")
        proven.append((relative, proof))
    return tuple(proven)


def _cli_launch(
    python: str, harness: str, nonce: str, symbol: str, proven: Sequence[tuple[str, RegularFile]]
) -> list[str]:
    """The argv of the harness ``cli`` role running ``symbol`` from the ``proven`` files."""
    kind, name = ("module", symbol[3:]) if symbol.startswith("-m ") else ("script", symbol)
    identities = [
        part
        for relative, proof in proven
        for part in (
            relative,
            str(proof.device),
            str(proof.inode),
            hashlib.sha256(proof.data).hexdigest(),
        )
    ]
    return [
        python,
        "-I",
        "-B",
        "-c",
        harness,
        "cli",
        nonce,
        kind,
        name,
        str(len(proven)),
        *identities,
    ]


async def _feed(process: asyncio.subprocess.Process, data: bytes) -> None:
    assert process.stdin is not None
    try:
        process.stdin.write(data)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        try:
            process.stdin.close()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


async def _cli_case(
    command: CheckCommand, stdin: str, deadline: float, call_text: str, nonce: str
) -> _Case:
    """One CLI call, from launch to reap, within ``deadline`` (event loop time).

    The process is the harness ``cli`` role: its one frame on stderr, written
    before any target code runs, says whether it found the proven files; a
    call without an ``ok`` frame is ``unprovable`` and nothing it printed is
    an observation. Its stdout is read under ``_CLI_OUTPUT_LIMIT`` while it
    streams. More output than the limit is an oversized observation (this
    case fails on a candidate, the base is undecided): the process group is
    killed at the first byte past the limit, so a target cannot make the
    controller buffer its output. The stdin write shares the case deadline.
    """
    loop = asyncio.get_running_loop()
    timeout = _Case("observed", entry={"outcome": "timeout", "call": call_text})
    if loop.time() >= deadline:
        return timeout
    try:
        process = await spawn_check_process(
            command, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        return _Case("resolve", resolve="missing", detail=f"{call_text}: {exc}")
    output, frames = CappedOutput(_CLI_OUTPUT_LIMIT), CappedOutput(_CLI_FRAME_LIMIT)
    ended: _Case | None = None
    try:
        await asyncio.wait_for(
            asyncio.gather(
                _feed(process, stdin.encode("utf-8")),
                output.fill(process.stdout),
                frames.fill(process.stderr),
            ),
            timeout=max(deadline - loop.time(), 0.01),
        )
        if output.overflow:
            ended = _Case("observed", entry={"outcome": "malformed", "call": call_text})
        else:
            await asyncio.wait_for(process.wait(), timeout=max(deadline - loop.time(), 0.01))
    except TimeoutError:
        ended = timeout
    finally:
        await reap_check_process(process, deadline)
        refused = process.launch_problem()
    if refused is not None:
        # Not started from the pinned interpreter: nothing of the check ran.
        raise _Unavailable(refused)
    if ended is not None:
        return ended
    if loop.time() > deadline:
        # Its reap ran past the deadline: nothing it reported stands.
        return timeout
    resolved = _launch_frame(frames.data, nonce)
    if resolved.get("resolve") != "ok":
        detail = resolved.get("detail")
        return _Case(
            "resolve",
            resolve="unprovable",
            detail=f"{call_text}: {detail if isinstance(detail, str) else 'no launch frame'}"[:500],
        )
    return _Case(
        "observed",
        entry={
            "outcome": "exited",
            "call": call_text,
            "exit_code": process.returncode,
            "stdout": output.data.decode("utf-8", errors="replace"),
        },
    )


def _launch_frame(data: bytes, nonce: str) -> dict[str, Any]:
    """The ``resolved`` frame of a harness ``cli`` process, or ``{}`` when it wrote none."""
    prefix = (nonce + " ").encode("ascii")
    for line in data.splitlines():
        if line.startswith(prefix):
            frame = parse_frame(line[len(prefix) :])
            if frame is not None and frame.get("phase") == "resolved":
                return frame
    return {}


async def _observe(
    oracle: OracleSpec,
    cases: Sequence[Any],
    binding: Binding,
    harness: str,
    cwd: Path,
    prepare: _Prepare,
    python: str,
    deadline: float,
    *,
    on_base: bool,
    reference_run: bool = False,
) -> tuple[str, str, dict[str, dict[str, Any]], bool]:
    """Run ``cases`` by ``deadline``; return ``(resolve, detail, observations, timed_out)``.

    Each case gets an equal share of the time left before ``deadline``
    (event loop time). Raises ``_Unavailable`` when the entry point refuses
    a process. ``reference_run`` (the reference check) makes no setup call
    and sends every symbol reference as its dotted path.
    """
    arg_map = dict(binding.arg_map)
    setup = [] if reference_run else [call.model_dump(mode="json") for call in oracle.setup]
    observations: dict[str, dict[str, Any]] = {}
    loop = asyncio.get_running_loop()
    for position, case in enumerate(cases):
        share = (deadline - loop.time()) / (len(cases) - position)
        case_deadline = loop.time() + share
        if oracle.call_kind is CallKind.CLI:
            argv = harness_module.cli_argv(
                python, str(cwd), binding.symbol, list(oracle.params), arg_map, case.args
            )
            call_text = " ".join(argv[2:] if argv[0] == python else argv)
            # Proven before every case (an earlier case's target may have
            # replaced a file), and run from the proven files only.
            proven = _cli_target_files(cwd, binding.symbol)
            if isinstance(proven, _NotProven):
                outcome = _Case("resolve", resolve=proven.resolve, detail=proven.reason)
            else:
                nonce = secrets.token_hex(16)
                tail = argv[4:] if binding.symbol.startswith("-m ") else argv[3:]
                launch = _cli_launch(python, harness, nonce, binding.symbol, proven)
                outcome = await _cli_case(
                    _prepared(prepare, [*launch, *tail]),
                    case.stdin or "",
                    case_deadline,
                    call_text,
                    nonce,
                )
        else:
            inputs, init = case.args, case.init
            if reference_run:
                inputs, init = (
                    harness_module.named_inputs(inputs),
                    harness_module.named_inputs(init),
                )
            args, kwargs = harness_module.split_args(list(oracle.params), arg_map, inputs)
            nonce = secrets.token_hex(16)
            argv = [
                python,
                "-I",
                "-B",
                "-c",
                harness,
                "target",
                nonce,
                oracle.call_kind.value,
                binding.symbol,
                json.dumps(setup),
            ]
            call = {"case_id": case.case_id, "args": args, "kwargs": kwargs, "init": init}
            outcome = await _python_case(_prepared(prepare, argv), nonce, call, case_deadline)
        entry = dict(outcome.entry or {})
        if on_base:
            # A base run never counts an accident as the intended failure
            # (admission and the base run of a late binding).
            if outcome.kind == "observed" and entry.get("outcome") not in (
                "crashed",
                "timeout",
                "malformed",
            ):
                observations[case.case_id] = entry
                continue
            if outcome.kind == "observed":
                timed_out = entry["outcome"] == "timeout"
                reason = {"timeout": "target_timeout", "crashed": "target_crashed"}
                return reason.get(entry["outcome"], "frame_malformed"), "", {}, timed_out
            if position and outcome.kind == "resolve":
                # An earlier case resolved the same symbol: not a stable target.
                return "resolve_unstable", outcome.detail, {}, False
            return outcome.resolve, outcome.detail, {}, outcome.timed_out
        if outcome.kind == "observed":
            observations[case.case_id] = entry
        elif position == 0 and outcome.kind in ("launch_error", "resolve"):
            # launch_error: no target code has run yet (indeterminate).
            # missing / import_error: every case fails (decided).
            return outcome.resolve, outcome.detail, {}, False
        elif outcome.kind == "resolve":
            observations[case.case_id] = {
                "case_id": case.case_id,
                "outcome": "unresolved",
                "detail": f"{outcome.resolve}: {outcome.detail}",
            }
        else:
            # The target's code already ran (earlier case or this import):
            # a setup crash, hang, garbage frame, or launch failure fails
            # this case only.
            observations[case.case_id] = entry or {
                "case_id": case.case_id,
                "outcome": "crashed",
                "exit": None,
            }
    return "ok", "", observations, False


def _undecided_result(
    oracle: OracleSpec, cases: Sequence[Any], binding: Binding, source: str, resolve: str
) -> dict[str, Any]:
    return {
        "check_id": oracle.check_id,
        "criterion_key": oracle.criterion_key,
        "binding_source": source,
        "symbol": binding.symbol,
        "call_kind": oracle.call_kind.value,
        "resolve": resolve,
        "cases": [
            {"case_id": case.case_id, "held_out": case.held_out, "passed": False, "detail": ""}
            for case in cases
        ],
    }


def _render(oracle: OracleSpec, result: dict[str, Any], code: int | None, detail: str) -> str:
    """Output for receipts and people: never target output, held-out cases by id only."""
    if code not in (0, 1):
        return f"oracle could not run the target: {result.get('resolve')} {detail}".rstrip()
    if code == 0:
        return "oracle: every case passed"
    lines = [oracle.failure_signature]
    for case in result.get("cases") or ():
        if case.get("passed"):
            continue
        if case.get("held_out"):
            lines.append(f"counterexample (held-out): {case.get('case_id')}")
        else:
            lines.append(f"counterexample: {case.get('detail') or case.get('case_id')}")
    return "\n".join(lines)


def _decided(resolve: str, *, on_base: bool) -> bool:
    if resolve in ("ok", "missing"):
        return True
    # On a candidate an import error is the candidate's code failing.
    return resolve == "import_error" and not on_base


async def run_oracle_check(
    package_files: Mapping[str, str],
    oracle: OracleSpec,
    cwd: Path,
    *,
    timeout_seconds: float,
    on_base: bool,
    env: Mapping[str, str] | None = None,
    interpreter: CheckInterpreter | None,
    binding: Binding | None,
    scratch_parent: Path | None = None,
    writable_root: Path | None = None,
    include_held_out: bool = True,
    reference_run: bool = False,
) -> OracleRun:
    """Run one oracle check on the checkout copy at ``cwd`` (see the module docstring).

    Every target process comes from the check execution entry point
    (``check_env.check_command``): the pinned ``interpreter`` (``python3``
    from ``PATH`` when none is given), confinement to ``writable_root``
    (default ``cwd``) and a scratch directory, an environment built from
    scratch with ``env`` (default: this process's) only as the source of the
    allowlisted
    values, and a scratch directory in ``scratch_parent``. With
    ``include_held_out`` false the held-out cases are neither run nor judged.
    ``reference_run`` is the reference check's run (``reference_check.py``):
    the reference has no project, so no setup call is made and each symbol
    reference in a case's inputs reaches it as its dotted path.

    Never raises: an unexpected controller error is an indeterminate check,
    never a verdict and never a reason to fall back to another verifier.
    """
    started = time.monotonic()
    # The one deadline of this check: every target's launch, I/O and reap.
    deadline = asyncio.get_running_loop().time() + max(0.1, timeout_seconds)
    harness = package_files.get(ORACLE_HARNESS_PATH)
    data_text = package_files.get(ORACLE_DATA_PATH)
    source = "declared" if binding is not None else "default"
    bound = binding or oracle.default_binding
    pinned = interpreter or default_interpreter()
    cases = [case for case in oracle.cases if include_held_out or not case.held_out]
    if harness is None or data_text is None:
        launch_error: str | None = "oracle files missing from the package"
    elif harness != ORACLE_HARNESS_SOURCE:
        launch_error = "oracle harness differs from this product version"
    else:
        launch_error = None
    if launch_error is not None:
        return OracleRun(None, False, launch_error, launch_error, None, 0.0)
    assert harness is not None and data_text is not None
    if not cases:
        return OracleRun(None, False, None, NO_VISIBLE_CASE, None, 0.0, NO_VISIBLE_CASE)
    timed_out = False
    try:
        # Parsed before any target starts; held in memory only.
        oracle_data = _selected_data(json.loads(data_text), oracle.check_id, cases)
        with check_scratch(scratch_parent) as scratch:

            def prepare(argv: Sequence[str]) -> CheckCommand | CheckUnavailable:
                return check_command(
                    argv,
                    cwd=cwd,
                    writable_root=writable_root or cwd,
                    interpreter=pinned,
                    scratch=scratch,
                    source=env,
                )

            resolve, detail, observations, timed_out = await _observe(
                oracle,
                cases,
                bound,
                harness,
                cwd,
                prepare,
                pinned.path,
                deadline,
                on_base=on_base,
                reference_run=reference_run,
            )
        result: dict[str, Any] | None = None
        if _decided(resolve, on_base=on_base):
            result = harness_module.compare(
                {
                    "oracle": oracle_data,
                    "check_id": oracle.check_id,
                    "binding": bound.to_dict() if binding is not None else None,
                    "resolve": resolve,
                    "detail": detail,
                    "observations": observations,
                }
            )
    except _Unavailable as refused:
        return OracleRun(
            None, False, None, refused.reason, None, time.monotonic() - started, refused.reason
        )
    except Exception as exc:  # noqa: BLE001 - a controller fault is indeterminate, never a verdict
        resolve, detail, result = "controller_error", type(exc).__name__, None
    if result is None:
        result = _undecided_result(oracle, cases, bound, source, resolve)
        code: int | None = None if timed_out else 3
    else:
        code = 0 if all(case.get("passed") for case in result["cases"]) else 1
    return OracleRun(
        return_code=code,
        timed_out=timed_out,
        launch_error=None,
        output=_render(oracle, result, code, detail),
        result=result,
        duration=time.monotonic() - started,
    )


def _selected_data(data: dict[str, Any], check_id: str, cases: Sequence[Any]) -> dict[str, Any]:
    """The frozen oracle data with this check's cases restricted to ``cases``."""
    ids = {case.case_id for case in cases}
    oracles = []
    for spec in data.get("oracles") or ():
        if spec.get("check_id") == check_id:
            spec = {**spec, "cases": [case for case in spec["cases"] if case["case_id"] in ids]}
        oracles.append(spec)
    return {**data, "oracles": oracles}


__all__ = [
    "REAP_MARGIN_SECONDS",
    "CappedOutput",
    "OracleRun",
    "kill_check_group",
    "parse_frame",
    "NO_VISIBLE_CASE",
    "reap_check_process",
    "run_oracle_check",
    "valid_entry",
]
