"""Bounded, read-only runtime facts for ``mcp doctor-runtime``.

The collector reads only PATH entries, fixed executable metadata, loopback bind
availability, and metadata for Ouroboros's own MCP PID registry. It never
reads registry contents, process arguments, arbitrary environment values, or
configuration/credential data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import errno
import os
from pathlib import Path
import re
import socket
import stat
import sys
from typing import Any, Literal

from ouroboros.core.filesystem_capability import (
    NoFollowDirectoryChain,
    nofollow_directory_capabilities_available,
    open_nofollow_directory_chain,
)

MAX_PATH_CHARS = 32_768
MAX_PATH_ENTRIES = 128
MAX_REGISTRY_ENTRIES = 128
MAX_REGISTRY_NAME_CHARS = 64
MAX_REGISTRY_RECORD_BYTES = 4_096
_REGISTRY_DIRECTORY_LABEL = "~/.ouroboros/mcp-servers"
_PID_NAME = re.compile(r"^(?P<pid>[1-9][0-9]{0,9})\.pid$")


@dataclass(frozen=True)
class PathCandidate:
    """One fixed executable name found through a PATH entry."""

    name: str
    path: str
    executable: bool


@dataclass(frozen=True)
class PathFacts:
    """Bounded executable provenance for fixed command names."""

    entries_seen: int
    entries_limit: int
    characters_seen: int
    characters_limit: int
    truncated: bool
    candidates: tuple[PathCandidate, ...]
    collisions: dict[str, tuple[str, ...]]
    status: Literal["available", "not_checked"] = "available"
    reason: str | None = None


@dataclass(frozen=True)
class LoopbackProbe:
    """Whether one loopback address accepts an ephemeral TCP bind."""

    family: str
    status: Literal["available", "unavailable", "not_checked"]
    port: int | None
    reason: str | None = None


@dataclass(frozen=True)
class RegistryRecord:
    """Metadata for a declared Ouroboros MCP PID registry record."""

    pid: int
    name: str
    size: int
    mtime_ns: int
    identity_verified: bool
    liveness: str


@dataclass(frozen=True)
class RegistryFacts:
    """Bounded metadata for the owned MCP PID registry."""

    directory: str
    entries_seen: int
    entries_limit: int
    truncated: bool
    records: tuple[RegistryRecord, ...]
    status: Literal["available", "not_checked"]
    reason: str | None = None


@dataclass(frozen=True)
class RuntimeSnapshot:
    """The three A2.2 bounded runtime metadata categories."""

    path: PathFacts
    loopback: tuple[LoopbackProbe, ...]
    registry: RegistryFacts

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _executable_names() -> tuple[str, ...]:
    names = ("ouroboros", "python", "python3")
    if sys.platform == "win32":
        return tuple(f"{name}{suffix}" for name in names for suffix in (".exe", ".cmd", ".bat"))
    return names


def collect_path_facts(path_value: str | None = None) -> PathFacts:
    """Stat only fixed executable names in bounded PATH entries."""
    raw = os.environ.get("PATH", "") if path_value is None else path_value
    if not raw:
        return PathFacts(
            0, MAX_PATH_ENTRIES, 0, MAX_PATH_CHARS, False, (), {}, "not_checked", "missing"
        )
    separator = ";" if sys.platform == "win32" else os.pathsep
    bounded = raw[:MAX_PATH_CHARS]
    cut = len(raw) > MAX_PATH_CHARS
    if cut:
        # A truncated component is not a real PATH directory.
        bounded = bounded.rsplit(separator, 1)[0] if separator in bounded else ""
    entries = bounded.split(separator, MAX_PATH_ENTRIES) if bounded else []
    truncated = cut or len(entries) > MAX_PATH_ENTRIES
    candidates: list[PathCandidate] = []
    seen: set[tuple[str, str]] = set()
    inaccessible = False
    for entry in entries[:MAX_PATH_ENTRIES]:
        directory = Path(entry or ".")
        for name in _executable_names():
            candidate = directory / name
            try:
                mode = candidate.stat().st_mode
                executable = stat.S_ISREG(mode) and os.access(candidate, os.X_OK)
                if not executable:
                    continue
                identity = os.path.realpath(candidate)
                if sys.platform == "win32":
                    identity = identity.casefold()
                if (name, identity) not in seen:
                    seen.add((name, identity))
                    candidates.append(PathCandidate(name, str(candidate), True))
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                inaccessible = True
    by_name: dict[str, list[str]] = {}
    for candidate in candidates:
        by_name.setdefault(candidate.name, []).append(candidate.path)
    collisions = {name: tuple(paths) for name, paths in by_name.items() if len(paths) > 1}
    return PathFacts(
        entries_seen=min(len(entries), MAX_PATH_ENTRIES),
        entries_limit=MAX_PATH_ENTRIES,
        characters_seen=min(len(raw), MAX_PATH_CHARS),
        characters_limit=MAX_PATH_CHARS,
        truncated=truncated,
        candidates=tuple(candidates),
        collisions=collisions,
        status="not_checked" if inaccessible else "available",
        reason="inaccessible_candidate" if inaccessible else None,
    )


def _probe(family: int, label: str) -> LoopbackProbe:
    sock: socket.socket | None = None
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
        host = "127.0.0.1" if family == socket.AF_INET else "::1"
        sock.bind((host, 0))
        return LoopbackProbe(label, "available", int(sock.getsockname()[1]))
    except OSError as exc:
        if exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT):
            return LoopbackProbe(label, "not_checked", None, "unsupported")
        if exc.errno in (errno.EACCES, errno.EPERM):
            return LoopbackProbe(label, "unavailable", None, "permission_denied")
        return LoopbackProbe(label, "unavailable", None, "unavailable")
    finally:
        if sock is not None:
            sock.close()


def probe_loopback() -> tuple[LoopbackProbe, ...]:
    """Bind ephemeral loopback sockets without connecting or resolving names."""
    probes = [_probe(socket.AF_INET, "ipv4")]
    try:
        probes.append(_probe(socket.AF_INET6, "ipv6"))
    except (AttributeError, OSError):
        probes.append(LoopbackProbe("ipv6", "not_checked", None, "unsupported"))
    return tuple(probes)


def _open_owned_registry(directory: Path) -> NoFollowDirectoryChain:
    """Hold a no-follow capability through every registry path component."""
    if not nofollow_directory_capabilities_available():
        raise NotImplementedError
    return open_nofollow_directory_chain(directory)


def collect_registry_facts(registry_dir: Path | None = None) -> RegistryFacts:
    """Inspect bounded no-follow metadata for Ouroboros ``<pid>.pid`` records."""
    if registry_dir is None:
        return RegistryFacts(
            _REGISTRY_DIRECTORY_LABEL,
            0,
            MAX_REGISTRY_ENTRIES,
            False,
            (),
            "not_checked",
            "owner_unavailable",
        )
    records: list[RegistryRecord] = []
    entries_seen = 0
    truncated = False
    try:
        directory_chain = _open_owned_registry(registry_dir)
        try:
            with os.scandir(directory_chain.leaf_fd) as iterator:
                for entry in iterator:
                    if entries_seen >= MAX_REGISTRY_ENTRIES:
                        truncated = True
                        break
                    entries_seen += 1
                    if len(entry.name) > MAX_REGISTRY_NAME_CHARS or entry.is_symlink():
                        continue
                    match = _PID_NAME.fullmatch(entry.name)
                    if match is None:
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_size > MAX_REGISTRY_RECORD_BYTES
                        ):
                            continue
                        records.append(
                            RegistryRecord(
                                pid=int(match.group("pid")),
                                name=entry.name,
                                size=info.st_size,
                                mtime_ns=info.st_mtime_ns,
                                identity_verified=False,
                                liveness="not_checked",
                            )
                        )
                    except (OSError, ValueError, OverflowError):
                        continue
        finally:
            directory_chain.close()
    except NotImplementedError:
        return RegistryFacts(
            _REGISTRY_DIRECTORY_LABEL,
            0,
            MAX_REGISTRY_ENTRIES,
            False,
            (),
            "not_checked",
            "no_follow_unsupported",
        )
    except OSError as exc:
        reason = "symlink" if exc.errno == errno.ELOOP else "unavailable"
        return RegistryFacts(
            _REGISTRY_DIRECTORY_LABEL,
            entries_seen,
            MAX_REGISTRY_ENTRIES,
            False,
            (),
            "not_checked",
            reason,
        )
    records.sort(key=lambda record: record.name)
    return RegistryFacts(
        _REGISTRY_DIRECTORY_LABEL,
        entries_seen,
        MAX_REGISTRY_ENTRIES,
        truncated,
        tuple(records),
        "available",
    )


def collect_runtime_snapshot(
    *, path_value: str | None = None, registry_dir: Path | None = None
) -> RuntimeSnapshot:
    """Collect all bounded runtime facts without reading user-owned contents."""
    return RuntimeSnapshot(
        path=collect_path_facts(path_value),
        loopback=probe_loopback(),
        registry=collect_registry_facts(registry_dir),
    )


__all__ = [
    "MAX_PATH_CHARS",
    "MAX_PATH_ENTRIES",
    "MAX_REGISTRY_ENTRIES",
    "MAX_REGISTRY_RECORD_BYTES",
    "MAX_REGISTRY_NAME_CHARS",
    "LoopbackProbe",
    "PathCandidate",
    "PathFacts",
    "RegistryFacts",
    "RegistryRecord",
    "RuntimeSnapshot",
    "collect_path_facts",
    "collect_registry_facts",
    "collect_runtime_snapshot",
    "probe_loopback",
]
