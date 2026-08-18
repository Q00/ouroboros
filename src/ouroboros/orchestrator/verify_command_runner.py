"""Run an AC verify command through the resolved real POSIX shell.

The verifier deliberately does not emulate a shell. If the machine has no
real POSIX shell, the gate records an unavailable judgment instead of running
a subtly different command.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
import contextlib
from dataclasses import dataclass
import os
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


@dataclass(slots=True)
class _WindowsJob:
    handle: object
    close_handle: Callable[[object], object]
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_handle(self.handle)


def _create_windows_job(pid: int) -> _WindowsJob:
    """Assign ``pid`` to a kill-on-close Windows Job Object."""
    import ctypes
    from ctypes import wintypes

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    close = kernel32.CloseHandle
    try:
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        process = kernel32.OpenProcess(0x0001 | 0x0100 | 0x1000, False, pid)
        if not process:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not kernel32.AssignProcessToJobObject(job, process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            close(process)
    except Exception:
        close(job)
        raise
    return _WindowsJob(job, close)


def _running_on_windows() -> bool:
    return os.name == "nt"


def _spawn_kwargs() -> dict[str, object]:
    """Create a verifier process that cannot execute before containment."""
    if _running_on_windows():
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        suspended = getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        return {"creationflags": new_group | suspended}
    return {"start_new_session": True}


def _resume_windows_process(pid: int) -> None:
    """Resume the primary thread only after Job Object assignment."""
    import ctypes
    from ctypes import wintypes

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenThread.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        has_thread = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while has_thread:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                if not thread:
                    raise OSError(ctypes.get_last_error(), "OpenThread failed")
                try:
                    if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                        raise OSError(ctypes.get_last_error(), "ResumeThread failed")
                finally:
                    kernel32.CloseHandle(thread)
                return
            has_thread = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    raise OSError("suspended verifier thread was not found")


async def _terminate(proc: asyncio.subprocess.Process, job: _WindowsJob | None = None) -> None:
    if _running_on_windows():
        if job is not None:
            job.close()
        else:
            # Job assignment failed before ResumeThread, so this process is
            # still suspended and cannot have descendants to terminate.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    else:
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(proc.wait(), timeout=1.0)


async def _terminate_shielded(
    proc: asyncio.subprocess.Process, job: _WindowsJob | None = None
) -> bool:
    """Reach a bounded terminal process state before propagating cancellation.

    Returns whether another cancellation arrived while cleanup was in flight.
    The cleanup task itself remains shielded so repeated cancellation cannot
    detach an owned verifier process.
    """
    cleanup = asyncio.create_task(_terminate(proc, job))
    cancelled_during_cleanup = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled_during_cleanup = True
    await cleanup
    return cancelled_during_cleanup


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

    job: _WindowsJob | None = None
    if _running_on_windows():
        try:
            job = _create_windows_job(proc.pid)
            _resume_windows_process(proc.pid)
        except Exception as exc:
            if await _terminate_shielded(proc, job):
                raise asyncio.CancelledError
            return VerifyRun(returncode=1, output="", start_error=str(exc))

    try:
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        if await _terminate_shielded(proc, job):
            raise asyncio.CancelledError
        return VerifyRun(returncode=1, output="", timed_out=True)
    except BaseException:
        await _terminate_shielded(proc, job)
        raise
    finally:
        if job is not None:
            job.close()

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
