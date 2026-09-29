"""Content-free, static machine facts for MCP doctor.

A2.1 deliberately excludes runtime, PATH, network, port, process, command,
and configuration-content probes. Every fact is either an ``ok`` value or a
stable ``not_checked`` reason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import importlib.metadata
import os
from pathlib import Path
import platform
import shutil
import stat
import struct
import sys
from typing import Any, Literal

ProbeStatus = Literal["ok", "not_checked"]

REASON_PERMISSION = "permission_denied"
REASON_MISSING = "missing"
REASON_UNSUPPORTED = "unsupported"
REASON_UNEXPECTED = "unexpected_failure"


@dataclass(frozen=True)
class SnapshotProbe:
    """One safe snapshot fact."""

    status: ProbeStatus
    value: Any = None
    reason: str | None = None


@dataclass(frozen=True)
class MachineSnapshot:
    """Typed static facts collected by the opt-in doctor probe."""

    os: SnapshotProbe
    architecture: SnapshotProbe
    python: SnapshotProbe
    executable: SnapshotProbe
    package: SnapshotProbe
    disk: SnapshotProbe
    config_path: SnapshotProbe

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _not_checked(reason: str) -> SnapshotProbe:
    return SnapshotProbe(status="not_checked", reason=reason)


def _safe(call: Callable[[], object]) -> SnapshotProbe:
    try:
        value = call()
    except PermissionError:
        return _not_checked(REASON_PERMISSION)
    except FileNotFoundError:
        return _not_checked(REASON_MISSING)
    except (NotImplementedError, AttributeError):
        return _not_checked(REASON_UNSUPPORTED)
    except Exception:
        return _not_checked(REASON_UNEXPECTED)
    return SnapshotProbe(status="ok", value=value)


def _probe_os() -> SnapshotProbe:
    def collect() -> dict[str, str]:
        if sys.platform == "win32":
            version = sys.getwindowsversion()
            return {
                "system": "Windows",
                "release": f"{version.major}.{version.minor}",
                "version": str(version.build),
            }
        # platform.uname() can fall back to shell commands on Windows.
        info = os.uname()
        return {"system": info.sysname, "release": info.release, "version": info.version}

    return _safe(collect)


def _probe_architecture() -> SnapshotProbe:
    def collect() -> dict[str, str | int]:
        if sys.platform == "win32":
            # Pointer width is a process fact, not native machine architecture.
            # Do not infer the latter from environment variables or shell output.
            raise NotImplementedError
        return {"machine": os.uname().machine, "bits": struct.calcsize("P") * 8}

    return _safe(collect)


def _probe_python() -> SnapshotProbe:
    return _safe(
        lambda: {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        }
    )


def _probe_executable() -> SnapshotProbe:
    if not sys.executable:
        return _not_checked(REASON_MISSING)
    return _safe(lambda: {"path": str(Path(sys.executable))})


def _probe_package() -> SnapshotProbe:
    def collect() -> dict[str, str]:
        distribution = importlib.metadata.distribution("ouroboros-ai")
        return {
            "name": distribution.metadata.get("Name", "ouroboros-ai"),
            "version": distribution.version,
            "location": str(distribution.locate_file("")),
        }

    def collect_available() -> dict[str, str]:
        try:
            return collect()
        except importlib.metadata.PackageNotFoundError as exc:
            raise FileNotFoundError from exc

    return _safe(collect_available)


def _probe_disk() -> SnapshotProbe:
    def collect() -> dict[str, int]:
        usage = shutil.disk_usage(Path.home())
        return {
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_bytes": usage.used,
        }

    return _safe(collect)


def _probe_config_path() -> SnapshotProbe:
    def collect() -> dict[str, str]:
        path = Path.home() / ".ouroboros" / "config.yaml"
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            kind = "symlink"
        elif stat.S_ISREG(metadata.st_mode):
            kind = "regular_file"
        else:
            kind = "other"
        return {"path": str(path), "kind": kind}

    return _safe(collect)


def collect_machine_snapshot() -> MachineSnapshot:
    """Collect bounded static metadata without reading config contents."""
    return MachineSnapshot(
        os=_probe_os(),
        architecture=_probe_architecture(),
        python=_probe_python(),
        executable=_probe_executable(),
        package=_probe_package(),
        disk=_probe_disk(),
        config_path=_probe_config_path(),
    )


__all__ = ["MachineSnapshot", "ProbeStatus", "SnapshotProbe", "collect_machine_snapshot"]
