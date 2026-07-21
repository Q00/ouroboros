"""Runtime-agnostic session lock for orphan detection.

When a runner starts an execution, it acquires a lock by writing a file
containing its PID and boot time. The orphan detector checks whether the
lock holder is still alive by verifying both PID existence AND boot time
match (preventing PID recycling false positives).

Lock files live at: ~/.ouroboros/locks/{session_id}
Format: "{pid}:{process_start_time_epoch}"

This mechanism is intentionally file-based (not DB-based) to avoid
adding write contention to the event store during parallel execution.
Any runtime can participate — just call acquire/release.
"""

from __future__ import annotations

from enum import StrEnum
import logging
import math
import os
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_DIR = Path.home() / ".ouroboros" / "locks"


class ProcessIdentityState(StrEnum):
    """Conservative result of probing a recorded process identity."""

    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


def _ensure_dir() -> Path:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    return LOCK_DIR


def _get_process_start_time(pid: int) -> float | None:
    """Get the start time of a process to detect PID recycling.

    Uses /proc on Linux and sysctl on macOS.
    Returns epoch seconds, or None if unavailable.
    """
    import platform

    try:
        if platform.system() == "Darwin":
            import subprocess

            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0 and result.stdout.strip():
                from datetime import datetime

                # Parse macOS ps lstart format: "Mon Mar 17 14:30:00 2026"
                dt = datetime.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y")
                return dt.timestamp()
        else:
            # Linux: /proc/{pid}/stat field 22 is starttime in clock ticks
            stat_path = Path(f"/proc/{pid}/stat")
            if stat_path.exists():
                stat = stat_path.read_text()
                # Field 2 (comm) is parenthesized and may contain spaces, so a
                # plain split can shift field 22 and manufacture start-time
                # mismatches for otherwise live nested client processes.
                fields_after_comm = stat[stat.rfind(")") + 2 :].split()
                clock_ticks = int(fields_after_comm[19])
                # Convert to seconds using system clock tick rate
                hz = os.sysconf("SC_CLK_TCK")
                boot_time = Path("/proc/stat").read_text()
                for line in boot_time.splitlines():
                    if line.startswith("btime"):
                        btime = int(line.split()[1])
                        return btime + clock_ticks / hz
    except Exception:
        pass
    return None


def lock_path(session_id: str) -> Path:
    """Return the lock file path for a given session."""
    return _ensure_dir() / session_id


def acquire(session_id: str) -> None:
    """Acquire a session lock.

    Called by the runner when execution starts. Records the current PID
    and process start time for reliable liveness detection.
    """
    pid = os.getpid()
    start_time = _get_process_start_time(pid)
    payload = f"{pid}:{start_time}" if start_time else str(pid)

    path = lock_path(session_id)
    path.write_text(payload)
    log.info(
        "session_lock.acquired",
        extra={"session_id": session_id, "pid": pid},
    )


def release(session_id: str) -> None:
    """Release a session lock when execution completes or is cancelled."""
    path = lock_path(session_id)
    try:
        path.unlink(missing_ok=True)
        log.info(
            "session_lock.released",
            extra={"session_id": session_id},
        )
    except OSError:
        pass


def release_if_owned_by_current_process(session_id: str) -> bool:
    """Release a session lock only when the current process owns it."""
    if not is_owned_by_current_process(session_id):
        return False

    release(session_id)
    return True


def is_owned_by_current_process(session_id: str) -> bool:
    """Return True when the current process owns the session lock."""
    path = lock_path(session_id)
    try:
        content = path.read_text().strip()
    except OSError:
        return False

    parts = content.split(":", 1)
    try:
        pid = int(parts[0])
    except ValueError:
        return False
    if pid != os.getpid():
        return False
    if len(parts) > 1 and parts[1] != "None":
        try:
            recorded_start = float(parts[1])
        except ValueError:
            return False
        if process_identity_state(pid, recorded_start) is not ProcessIdentityState.ALIVE:
            return False

    return True


def current_process_identity() -> tuple[int, float | None]:
    """Return the current PID with its start time when the platform exposes it."""
    pid = os.getpid()
    return pid, _get_process_start_time(pid)


def process_start_time(pid: int) -> float | None:
    """Return the start time of ``pid`` (epoch seconds) when the platform exposes it."""
    return _get_process_start_time(pid)


def process_identity_state(
    pid: int,
    start_time: float | None = None,
) -> ProcessIdentityState:
    """Return alive/dead/unknown evidence for a recorded process identity.

    ``DEAD`` is returned only when the OS proves the PID is absent or when a
    readable start time proves that the PID was recycled. Probe failures and
    unusable start-time evidence are ``UNKNOWN`` so lifecycle callers never
    turn an inconclusive platform/process-table lookup into owner loss.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return ProcessIdentityState.UNKNOWN
    if start_time is not None and (
        isinstance(start_time, bool)
        or not isinstance(start_time, int | float)
        or not math.isfinite(float(start_time))
        or start_time <= 0
    ):
        return ProcessIdentityState.UNKNOWN

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessIdentityState.DEAD
    except PermissionError:
        # Permission denial proves that a process occupies this PID, even
        # though its start time may be unavailable to this user.
        return ProcessIdentityState.ALIVE if start_time is None else ProcessIdentityState.UNKNOWN
    except OSError:
        return ProcessIdentityState.UNKNOWN

    if start_time is None:
        return ProcessIdentityState.ALIVE
    current_start = _get_process_start_time(pid)
    if current_start is None:
        return ProcessIdentityState.UNKNOWN
    if abs(current_start - start_time) > 2.0:
        return ProcessIdentityState.DEAD
    return ProcessIdentityState.ALIVE


def is_process_identity_alive(pid: int, start_time: float | None = None) -> bool:
    """Return False only when ``pid``/``start_time`` is confirmed dead.

    ``start_time`` is optional for legacy callers, but when present it guards
    against treating a recycled PID as the original owner. Unknown probe
    results remain conservatively alive for this compatibility predicate;
    callers that need to distinguish uncertainty use
    :func:`process_identity_state`.
    """
    return process_identity_state(pid, start_time) is not ProcessIdentityState.DEAD


def holder_identity_state(session_id: str) -> ProcessIdentityState:
    """Return conservative liveness evidence for a session-lock holder."""
    path = lock_path(session_id)
    if not path.exists():
        return ProcessIdentityState.DEAD

    try:
        content = path.read_text().strip()
    except OSError:
        return ProcessIdentityState.UNKNOWN

    parts = content.split(":", 1)
    try:
        pid = int(parts[0])
    except ValueError:
        return ProcessIdentityState.UNKNOWN

    recorded_start: float | None = None
    if len(parts) > 1 and parts[1] != "None":
        try:
            recorded_start = float(parts[1])
        except ValueError:
            return ProcessIdentityState.UNKNOWN
    return process_identity_state(pid, recorded_start)


def is_holder_alive(session_id: str) -> bool:
    """Check if the lock holder for a session is still alive.

    Confirmed-live and inconclusive holders both remain active. Returns False
    only when no lock exists or its recorded identity is confirmed dead, so
    unknown evidence cannot authorize orphan cleanup.
    """
    if not lock_path(session_id).exists():
        return False
    state = holder_identity_state(session_id)
    if state is ProcessIdentityState.DEAD:
        release(session_id)  # Clean up stale lock
        return False
    return True


def get_alive_sessions() -> set[str]:
    """Return session IDs with live lock holders.

    Scans the lock directory, verifies each, and cleans up stale entries.
    """
    alive: set[str] = set()
    lock_dir = _ensure_dir()

    for entry in lock_dir.iterdir():
        if entry.is_file():
            session_id = entry.name
            if is_holder_alive(session_id):
                alive.add(session_id)

    return alive
