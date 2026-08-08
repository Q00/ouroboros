"""Crash-safe activation for setup-owned runtime configuration.

The standalone Claude profile deliberately has no Claude MCP-file side effect.
This module owns only ``~/.ouroboros/config.yaml`` and ``credentials.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile
from typing import Literal

import yaml

from ouroboros.cli.formatters.panels import print_error, print_warning
from ouroboros.core.owner_only import fsync_parent_directory


@dataclass(frozen=True)
class _FileSnapshot:
    """One exact, no-follow activation target generation."""

    kind: Literal["missing", "file", "directory", "symlink", "other"]
    mode: int | None = None
    contents: bytes | None = None
    device: int | None = None
    inode: int | None = None
    modified_ns: int | None = None
    changed_ns: int | None = None
    link_count: int | None = None
    link_target: str | None = None


class _ConcurrentActivationError(OSError):
    """An activation target no longer matches the generation that was read."""


class _DurabilityError(OSError):
    """A replacement was published but its directory sync was not confirmed."""

    def __init__(self, path: Path, published_snapshot: _FileSnapshot) -> None:
        super().__init__(f"Could not confirm replacement durability for {path}")
        self.path = path
        self.published_snapshot = published_snapshot


def _read_regular_snapshot(path: Path, initial_stat: os.stat_result) -> _FileSnapshot:
    """Read a regular file through one no-follow descriptor generation."""
    flags = os.O_RDONLY
    for optional_flag in ("O_CLOEXEC", "O_NONBLOCK", "O_NOFOLLOW"):
        flags |= getattr(os, optional_flag, 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise _ConcurrentActivationError(f"Activation target changed type: {path}")
        if (before.st_dev, before.st_ino) != (initial_stat.st_dev, initial_stat.st_ino):
            raise _ConcurrentActivationError(f"Activation target changed identity: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        before_generation = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_nlink,
        )
        after_generation = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
        )
        if after_generation != before_generation:
            raise _ConcurrentActivationError(f"Activation target changed while read: {path}")
        return _FileSnapshot(
            kind="file",
            mode=stat.S_IMODE(before.st_mode),
            contents=b"".join(chunks),
            device=before.st_dev,
            inode=before.st_ino,
            modified_ns=before.st_mtime_ns,
            changed_ns=before.st_ctime_ns,
            link_count=before.st_nlink,
        )
    finally:
        os.close(descriptor)


def _snapshot_target(path: Path) -> _FileSnapshot:
    """Snapshot without following links or opening special files."""
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return _FileSnapshot(kind="missing")
    mode = stat.S_IMODE(stat_result.st_mode)
    if stat.S_ISREG(stat_result.st_mode):
        return _read_regular_snapshot(path, stat_result)
    if stat.S_ISLNK(stat_result.st_mode):
        return _FileSnapshot(kind="symlink", mode=mode, link_target=os.readlink(path))
    if stat.S_ISDIR(stat_result.st_mode):
        return _FileSnapshot(
            kind="directory",
            mode=mode,
            device=stat_result.st_dev,
            inode=stat_result.st_ino,
        )
    return _FileSnapshot(kind="other", mode=mode)


def _require_snapshot(path: Path, expected: _FileSnapshot) -> _FileSnapshot:
    current = _snapshot_target(path)
    if current != expected:
        raise _ConcurrentActivationError(f"Activation target changed concurrently: {path}")
    return current


def _require_file_target(path: Path, snapshot: _FileSnapshot) -> None:
    if snapshot.kind == "missing":
        return
    if snapshot.kind != "file":
        raise ValueError(f"{path.name} must be a regular file, not {snapshot.kind}")
    if snapshot.link_count != 1:
        raise ValueError(f"{path.name} must not be hard-linked")


def _yaml_mapping(path: Path, snapshot: _FileSnapshot) -> dict[str, object]:
    if snapshot.kind != "file" or snapshot.contents is None:
        raise ValueError(f"{path.name} is not a regular file")
    try:
        loaded = yaml.safe_load(snapshot.contents.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"Could not parse {path.name}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"Invalid non-mapping {path.name} contents")
    return loaded


def _atomic_write_text_if_current_matches(
    path: Path,
    content: str,
    expected: _FileSnapshot,
    *,
    mode: int,
) -> _FileSnapshot:
    """Fsync and replace only the exact regular or missing generation read."""
    _require_snapshot(path, expected)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    replacement_fd: int | None = None
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary_name, mode)
        except OSError:
            pass
        replacement_fd = os.open(temporary_name, os.O_RDONLY)
        prepared_stat = os.fstat(replacement_fd)
        if not stat.S_ISREG(prepared_stat.st_mode):
            raise OSError(f"Could not prepare regular replacement for {path}")
        _require_snapshot(path, expected)
        os.replace(temporary_name, path)
        published_stat = os.fstat(replacement_fd)
        published = _FileSnapshot(
            kind="file",
            mode=stat.S_IMODE(published_stat.st_mode),
            contents=content.encode("utf-8"),
            device=published_stat.st_dev,
            inode=published_stat.st_ino,
            modified_ns=published_stat.st_mtime_ns,
            changed_ns=published_stat.st_ctime_ns,
            link_count=published_stat.st_nlink,
        )
        try:
            os.close(replacement_fd)
        except OSError:
            pass
        replacement_fd = None
        if not fsync_parent_directory(path):
            raise _DurabilityError(path, published)
        return published
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if replacement_fd is not None:
            try:
                os.close(replacement_fd)
            except OSError:
                pass
        try:
            Path(temporary_name).unlink()
        except OSError:
            pass
        raise


def _rollback_file(
    path: Path,
    original: _FileSnapshot,
    expected_current: _FileSnapshot | None,
) -> bool:
    if expected_current is None:
        return True
    try:
        _require_snapshot(path, expected_current)
        if original.kind == "missing":
            path.unlink()
            if not fsync_parent_directory(path):
                raise OSError(f"Could not confirm rollback durability for {path}")
            return True
        if original.kind != "file" or original.contents is None or original.mode is None:
            raise OSError(f"Cannot restore unsupported activation target: {path}")
        _atomic_write_text_if_current_matches(
            path,
            original.contents.decode("utf-8"),
            expected_current,
            mode=original.mode,
        )
        return True
    except (OSError, UnicodeDecodeError) as exc:
        print_warning(f"Preserved current {path.name}; activation rollback was incomplete: {exc}")
        return False


def _directory_topology(paths: tuple[Path, ...]) -> dict[Path, bool]:
    return {path: path.exists() for path in paths}


def _restore_directory_topology(snapshot: dict[Path, bool]) -> None:
    for path, existed_before in sorted(
        snapshot.items(), key=lambda item: len(item[0].parts), reverse=True
    ):
        if existed_before:
            continue
        try:
            path.rmdir()
        except (FileNotFoundError, NotADirectoryError, OSError):
            pass


def activate_claude_runtime(claude_path: str) -> Path | None:
    """Activate config/credentials and return config path, or fail closed."""
    from ouroboros.config.loader import ensure_config_dir
    from ouroboros.config.models import (
        CredentialsConfig,
        OuroborosConfig,
        get_config_dir,
        get_default_config,
        get_default_credentials,
    )

    config_dir_candidate = get_config_dir()
    try:
        directory_generation = _snapshot_target(config_dir_candidate)
    except OSError as exc:
        print_error(f"Could not inspect Ouroboros config directory; aborting: {exc}")
        return None
    if directory_generation.kind not in {"missing", "directory"}:
        print_error(
            "Claude setup requires ~/.ouroboros to be a real directory; "
            f"found {directory_generation.kind}."
        )
        return None
    topology = _directory_topology(
        (
            config_dir_candidate / "data",
            config_dir_candidate / "logs",
            config_dir_candidate,
        )
    )

    try:
        config_dir = ensure_config_dir()
        if directory_generation.kind == "directory":
            _require_snapshot(config_dir_candidate, directory_generation)
        elif _snapshot_target(config_dir).kind != "directory":
            raise ValueError("Ouroboros config directory was not created safely")
        config_path = config_dir / "config.yaml"
        credentials_path = config_dir / "credentials.yaml"
        config_generation = _snapshot_target(config_path)
        credentials_generation = _snapshot_target(credentials_path)
        _require_file_target(config_path, config_generation)
        _require_file_target(credentials_path, credentials_generation)

        config = (
            get_default_config().model_dump(mode="json")
            if config_generation.kind == "missing"
            else _yaml_mapping(config_path, config_generation)
        )
        if config_generation.kind == "file":
            OuroborosConfig.model_validate(config)
        credentials = (
            get_default_credentials().model_dump(mode="json")
            if credentials_generation.kind == "missing"
            else _yaml_mapping(credentials_path, credentials_generation)
        )
        if credentials_generation.kind == "file":
            CredentialsConfig.model_validate(credentials)

        orchestrator = config.get("orchestrator")
        if orchestrator is None:
            orchestrator = {}
            config["orchestrator"] = orchestrator
        if not isinstance(orchestrator, dict):
            raise ValueError("Invalid non-mapping 'orchestrator' section in config.yaml")
        orchestrator["runtime_backend"] = "claude"
        orchestrator["cli_path"] = claude_path
        llm = config.get("llm")
        if llm is None:
            llm = {}
            config["llm"] = llm
        if not isinstance(llm, dict):
            raise ValueError("Invalid non-mapping 'llm' section in config.yaml")
        llm["backend"] = "claude"
        OuroborosConfig.model_validate(config)
        config_content = yaml.dump(config, default_flow_style=False, sort_keys=False)
        credentials_content = yaml.dump(credentials, default_flow_style=False, sort_keys=False)
    except (OSError, ValueError, RecursionError) as exc:
        _restore_directory_topology(topology)
        print_error(f"Claude setup validation failed; aborting without changes: {exc}")
        return None

    config_written: _FileSnapshot | None = None
    credentials_written: _FileSnapshot | None = None
    try:
        _require_snapshot(config_path, config_generation)
        _require_snapshot(credentials_path, credentials_generation)
        if credentials_generation.kind == "missing":
            credentials_written = _atomic_write_text_if_current_matches(
                credentials_path,
                credentials_content,
                credentials_generation,
                mode=0o600,
            )
        expected_credentials = credentials_written or credentials_generation
        _require_snapshot(credentials_path, expected_credentials)
        config_written = _atomic_write_text_if_current_matches(
            config_path,
            config_content,
            config_generation,
            mode=config_generation.mode if config_generation.mode is not None else 0o644,
        )
        _require_snapshot(credentials_path, expected_credentials)
        _require_snapshot(config_path, config_written)
    except _DurabilityError as exc:
        if exc.path == config_path:
            config_written = exc.published_snapshot
        elif exc.path == credentials_path:
            credentials_written = exc.published_snapshot
        _rollback_file(config_path, config_generation, config_written)
        _rollback_file(credentials_path, credentials_generation, credentials_written)
        _restore_directory_topology(topology)
        print_error(f"Claude setup could not durably activate configuration: {exc}")
        return None
    except OSError as exc:
        _rollback_file(config_path, config_generation, config_written)
        _rollback_file(credentials_path, credentials_generation, credentials_written)
        _restore_directory_topology(topology)
        print_error(f"Claude setup could not activate configuration: {exc}")
        return None
    return config_path
