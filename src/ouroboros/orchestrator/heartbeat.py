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

import hashlib
import logging
import os
from pathlib import Path
import re
from threading import RLock

try:  # pragma: no cover - exercised on Unix CI; fallback supports Windows imports
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

LOCK_DIR = Path.home() / ".ouroboros" / "locks"
_LEASE_OPERATION_LOCK = RLock()
_HELD_LEASE_FDS: dict[str, int] = {}
_SAFE_LOCK_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _ensure_dir() -> Path:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    return LOCK_DIR


def _clear_held_leases_after_fork() -> None:
    """Drop inherited lease descriptors in a post-fork child.

    Advisory-lock state is tied to an open file description. If a child
    retained a copied descriptor, a dead parent could leave its liveness lease
    locked until the unrelated child exits. Do not acquire the inherited
    RLock here: a vanished parent thread may have owned it at fork time.
    """
    global _HELD_LEASE_FDS, _LEASE_OPERATION_LOCK

    inherited_fds = tuple(_HELD_LEASE_FDS.values())
    _HELD_LEASE_FDS = {}
    _LEASE_OPERATION_LOCK = RLock()
    for fd in inherited_fds:
        try:
            os.close(fd)
        except OSError:
            pass


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_clear_held_leases_after_fork)


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
    """Return a containment-safe lock path for a given session identifier."""
    if isinstance(session_id, str) and _SAFE_LOCK_SESSION_ID.fullmatch(session_id) is not None:
        return _ensure_dir() / session_id
    # Old/corrupt persisted session ids still need an observer-safe lookup.
    # New process-local registrations reject them before any effect or lease is
    # created, while this digest prevents a legacy value from escaping LOCK_DIR.
    raw = str(session_id).encode("utf-8", "surrogatepass")
    return _ensure_dir() / f"__invalid_session_id__{hashlib.sha256(raw).hexdigest()}"


def acquire(session_id: str) -> None:
    """Acquire a session lock.

    Called by the runner when execution starts. Records the current PID
    and process start time for reliable liveness detection.
    """
    pid = os.getpid()
    start_time = _get_process_start_time(pid)
    payload = f"{pid}:{start_time}" if start_time else str(pid)

    path = lock_path(session_id)
    # Keep an advisory exclusive lock open for the holder's lifetime. This
    # lets a later process safely distinguish a stale file (lock obtainable)
    # from a live lease (lock busy) without overwriting or deleting the latter.
    # The file payload remains human-readable diagnostic evidence; the held FD
    # is the race-free ownership primitive.
    with _LEASE_OPERATION_LOCK:
        if session_id in _HELD_LEASE_FDS and is_owned_by_current_process(session_id):
            return

        path_existed = path.exists()
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        lock_acquired = False
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_acquired = True
                except BlockingIOError as exc:
                    raise OSError(f"session liveness lease is held: {session_id}") from exc
            elif path_existed and not is_owned_by_current_process(session_id):
                # On platforms without ``fcntl`` preserve safety by refusing
                # to replace any extant lease rather than guessing it is stale.
                raise OSError(f"session liveness lease is already held: {session_id}")

            if (
                path_existed
                and is_holder_alive(session_id)
                and not is_owned_by_current_process(session_id)
            ):
                raise OSError(f"session liveness lease is held: {session_id}")

            if is_owned_by_current_process(session_id):
                _HELD_LEASE_FDS[session_id] = fd
                fd = -1  # ownership transferred to the process-lifetime registry
                return

            os.ftruncate(fd, 0)
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
            _HELD_LEASE_FDS[session_id] = fd
            fd = -1  # ownership transferred to the process-lifetime registry
        except Exception:
            if lock_acquired and fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            raise
        finally:
            if fd >= 0:
                os.close(fd)
    log.info(
        "session_lock.acquired",
        extra={"session_id": session_id, "pid": pid},
    )


def release(session_id: str) -> None:
    """Release a session lock when execution completes or is cancelled."""
    path = lock_path(session_id)
    with _LEASE_OPERATION_LOCK:
        fd = _HELD_LEASE_FDS.pop(session_id, None)
        try:
            path.unlink(missing_ok=True)
            log.info(
                "session_lock.released",
                extra={"session_id": session_id},
            )
        except OSError:
            pass
        finally:
            if fd is not None:
                if fcntl is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                try:
                    os.close(fd)
                except OSError:
                    pass


def release_if_owned_by_current_process(session_id: str) -> bool:
    """Release a session lock only when the current process owns it."""
    # ``acquire`` cannot replace an extant lease, and this lock serializes two
    # local cleanup paths. Once ownership has been checked, no other normal
    # owner can create a replacement until this unlink has completed.
    with _LEASE_OPERATION_LOCK:
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
        current_start = _get_process_start_time(pid)
        if current_start is not None and abs(current_start - recorded_start) > 2.0:
            return False

    return True


def current_process_identity() -> tuple[int, float | None]:
    """Return the current PID with its start time when the platform exposes it."""
    pid = os.getpid()
    return pid, _get_process_start_time(pid)


def process_start_time(pid: int) -> float | None:
    """Return the start time of ``pid`` (epoch seconds) when the platform exposes it."""
    return _get_process_start_time(pid)


def is_process_identity_alive(pid: int, start_time: float | None = None) -> bool:
    """Return True when ``pid`` is alive and still has ``start_time``.

    ``start_time`` is optional for legacy callers, but when present it guards
    against treating a recycled PID as the original owner.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass

    if start_time is not None:
        current_start = _get_process_start_time(pid)
        if current_start is not None and abs(current_start - start_time) > 2.0:
            return False
    return True


def is_holder_alive(session_id: str) -> bool:
    """Check if the lock holder for a session is still alive.

    Returns True only if:
    1. A lock file exists
    2. The recorded PID is running
    3. The process start time matches (guards against PID recycling)

    Returns False if no lock exists or the holder is confirmed dead. This
    observer never unlinks malformed or stale records: deleting after a
    non-atomic read could remove a newly acquired lease from another process.
    Stale filenames are harmless because session ids are unique; a caller that
    deliberately reuses one must resolve it explicitly rather than overwrite
    liveness evidence.
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

    try:
        recorded_start = float(parts[1]) if len(parts) > 1 and parts[1] != "None" else None
    except ValueError:
        # A malformed lease cannot prove that a compatible process owns this
        # session. Treat it as unheld without deleting it: a fresh owner may
        # have atomically replaced the observation between read and cleanup.
        return False

    return is_process_identity_alive(pid, recorded_start)


def get_alive_sessions() -> set[str]:
    """Return session IDs with live lock holders.

    Scans the lock directory and verifies each entry without deleting stale
    paths from an observer race.
    """
    alive: set[str] = set()
    lock_dir = _ensure_dir()

    for entry in lock_dir.iterdir():
        if entry.is_file():
            session_id = entry.name
            if is_holder_alive(session_id):
                alive.add(session_id)

    return alive
