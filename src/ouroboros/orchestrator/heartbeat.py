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

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_DIR = Path.home() / ".ouroboros" / "locks"


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
                fields = stat_path.read_text().split()
                clock_ticks = int(fields[21])
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


def is_holder_alive(session_id: str) -> bool:
    """Check if the lock holder for a session is still alive.

    Returns True only if:
    1. A lock file exists
    2. The recorded PID is running
    3. The process start time matches (guards against PID recycling)

    Returns False if no lock exists or the holder is confirmed dead.
    """
    path = lock_path(session_id)
    if not path.exists():
        return False

    try:
        content = path.read_text().strip()
    except OSError:
        return False

    # Parse "pid:start_time" or just "pid"
    parts = content.split(":", 1)
    try:
        pid = int(parts[0])
    except ValueError:
        return False

    recorded_start = float(parts[1]) if len(parts) > 1 and parts[1] != "None" else None

    # Check if process exists
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        release(session_id)  # Clean up stale lock
        return False
    except PermissionError:
        pass  # Process exists, different user

    # Guard against PID recycling
    if recorded_start is not None:
        current_start = _get_process_start_time(pid)
        if current_start is not None and abs(current_start - recorded_start) > 2.0:
            # PID was recycled — different process
            log.info(
                "session_lock.pid_recycled",
                extra={"session_id": session_id, "pid": pid},
            )
            release(session_id)
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
