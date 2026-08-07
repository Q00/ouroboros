"""Stable process identity for durable cross-process ownership records."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
import platform
import re

from ouroboros.orchestrator.heartbeat import (
    current_process_identity,
    is_process_identity_alive,
)

_IDENTITY_VERSION = 1
_LINUX_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_LINUX_BOOT_ID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _normalize_linux_boot_id(value: object) -> str | None:
    """Validate and normalize Linux's canonical UUID-shaped boot identity."""
    if not isinstance(value, str) or _LINUX_BOOT_ID_PATTERN.fullmatch(value) is None:
        return None
    return value.lower()


def _linux_process_start_ticks(pid: int) -> int | None:
    """Read Linux ``/proc/<pid>/stat`` field 22 without decoding ``comm``.

    ``comm`` is arbitrary non-NUL bytes and may contain spaces or parentheses,
    so splitting decoded text cannot locate field 22 safely. Linux places the
    final ``)`` immediately before field 3; starttime is tail field 20.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    close = raw.rfind(b")")
    if close < 0:
        return None
    fields = raw[close + 1 :].split()
    if len(fields) <= 19:
        return None
    try:
        start_ticks = int(fields[19])
    except ValueError:
        return None
    return start_ticks if start_ticks >= 0 else None


def _linux_boot_id() -> str | None:
    """Return the kernel boot identity used to fence post-reboot PID reuse."""
    try:
        raw = _LINUX_BOOT_ID_PATH.read_bytes().strip()
        value = raw.decode("ascii")
    except (OSError, UnicodeError):
        return None
    return _normalize_linux_boot_id(value)


def current_persisted_process_identity() -> dict[str, object] | None:
    """Build the versioned Linux identity persisted by cross-process owners.

    Linux uses boot identity plus raw process start ticks. Neither depends on
    ``/proc/stat`` epoch ``btime``, which can drift on WSL2 while a process is
    alive. Non-Linux writers keep the existing PID/start-epoch fields only.
    """
    if platform.system() != "Linux":
        return None
    pid = os.getpid()
    boot_id = _linux_boot_id()
    start_ticks = _linux_process_start_ticks(pid)
    if boot_id is None or start_ticks is None:
        return None
    return {
        "version": _IDENTITY_VERSION,
        "platform": "linux",
        "pid": pid,
        "boot_id": boot_id,
        "start_ticks": start_ticks,
    }


def current_persisted_process_owner() -> dict[str, object]:
    """Return legacy owner fields plus the versioned Linux identity when available."""
    pid, start_time = current_process_identity()
    owner: dict[str, object] = {"owner_pid": pid, "owner_start_time": start_time}
    identity = current_persisted_process_identity()
    if identity is not None:
        owner["owner_identity"] = identity
    return owner


def persisted_process_identity_alive(
    identity: object,
    *,
    legacy_pid: int | None,
    legacy_start_time: float | None,
) -> bool | None:
    """Resolve persisted owner liveness without widening recovery authority.

    ``None`` means the record cannot prove either life or death. Linux legacy,
    malformed, and future-version records deliberately return ``None`` so a
    reader never interrupts or releases a possibly-live owner. Non-Linux
    readers retain the pre-existing PID + epoch-start behavior unchanged.
    """
    if platform.system() != "Linux":
        if legacy_pid is None:
            return None
        return is_process_identity_alive(legacy_pid, legacy_start_time)

    if not isinstance(identity, Mapping):
        return None
    if set(identity) != {"version", "platform", "pid", "boot_id", "start_ticks"}:
        return None
    version = identity.get("version")
    identity_platform = identity.get("platform")
    pid = identity.get("pid")
    boot_id = _normalize_linux_boot_id(identity.get("boot_id"))
    start_ticks = identity.get("start_ticks")
    if (
        type(version) is not int
        or version != _IDENTITY_VERSION
        or identity_platform != "linux"
        or type(pid) is not int
        or pid <= 0
        or legacy_pid != pid
        or boot_id is None
        or type(start_ticks) is not int
        or start_ticks < 0
    ):
        return None

    current_boot_id = _linux_boot_id()
    if current_boot_id is None:
        return None
    if current_boot_id != boot_id:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return None

    current_start_ticks = _linux_process_start_ticks(pid)
    if current_start_ticks is None:
        return None
    return current_start_ticks == start_ticks


def persisted_process_owner_alive(owner: Mapping[str, object]) -> bool | None:
    """Resolve liveness from a complete persisted owner record."""
    pid_raw = owner.get("owner_pid")
    start_raw = owner.get("owner_start_time")
    pid = pid_raw if type(pid_raw) is int and pid_raw > 0 else None
    start_time = (
        float(start_raw)
        if isinstance(start_raw, (int, float)) and not isinstance(start_raw, bool)
        else None
    )
    return persisted_process_identity_alive(
        owner.get("owner_identity"),
        legacy_pid=pid,
        legacy_start_time=start_time,
    )
