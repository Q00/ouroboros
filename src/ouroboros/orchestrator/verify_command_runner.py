"""Run an AC verify command through the resolved real POSIX shell.

The verifier deliberately does not emulate a shell. If the machine has no
real POSIX shell, the gate records an unavailable judgment instead of running
a subtly different command.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import contextlib
from dataclasses import dataclass
import os
from pathlib import PureWindowsPath
import subprocess


@dataclass(frozen=True, slots=True)
class VerifyRun:
    """What running a verify command produced, before any judgment.

    ``start_error`` and ``timed_out`` are kept separate from ``returncode``
    because the gate must not read "could not start" or "ran too long" as an
    ordinary non-zero exit — they are different facts about the run.
    """

    returncode: int
    output: str
    timed_out: bool = False
    start_error: str | None = None


def _running_on_windows() -> bool:
    return os.name == "nt"


def _spawn_kwargs() -> dict[str, object]:
    """Create an independently terminable verifier process group."""
    if _running_on_windows():
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)}
    return {"start_new_session": True}


async def _terminate_windows_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Use Windows' tree-aware terminator, never a shell command."""
    system_root = os.environ.get("SYSTEMROOT", "").strip() or r"C:\Windows"
    taskkill = str(PureWindowsPath(system_root) / "System32" / "taskkill.exe")
    try:
        killer = await asyncio.create_subprocess_exec(
            taskkill,
            "/PID",
            str(proc.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.communicate(), timeout=5.0)
    except Exception:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    if _running_on_windows():
        await _terminate_windows_process_tree(proc)
    else:
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await proc.wait()


async def _run_process(
    argv: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_seconds: float,
) -> VerifyRun:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=dict(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **_spawn_kwargs(),  # type: ignore[arg-type]
        )
    except Exception as exc:  # pragma: no cover - spawn failure is environmental
        return VerifyRun(returncode=1, output="", start_error=str(exc))

    try:
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        await _terminate(proc)
        return VerifyRun(returncode=1, output="", timed_out=True)

    return VerifyRun(
        returncode=proc.returncode if proc.returncode is not None else 1,
        output=(stdout_bytes or b"").decode("utf-8", errors="replace"),
    )


async def run_with_shell(
    argv: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_seconds: float,
) -> VerifyRun:
    """Run the command through a resolved POSIX shell, unmodified."""
    return await _run_process(argv, cwd=cwd, env=env, timeout_seconds=timeout_seconds)
