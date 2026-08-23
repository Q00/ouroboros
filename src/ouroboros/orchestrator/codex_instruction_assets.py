"""Fingerprint Codex instruction assets across directory and symlink layouts."""

from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Any


def update_codex_instruction_asset_fingerprint(
    digest: Any,
    path: Path,
    *,
    relative_name: str,
    ignore_direct_system: bool = False,
    _seen: frozenset[Path] = frozenset(),
) -> None:
    """Hash active rules/skills, preserving logical roots through symlinks."""
    digest.update(relative_name.encode("utf-8", errors="surrogateescape"))
    digest.update(b"\0")
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        digest.update(b"missing\0")
        return
    except OSError as exc:
        raise RuntimeError("Cannot inspect Codex instruction assets") from exc

    mode = stat_result.st_mode
    if stat.S_ISLNK(mode):
        try:
            link_target = os.readlink(path)
            digest.update(b"symlink\0")
            digest.update(link_target.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
        except OSError as exc:
            raise RuntimeError("Cannot inspect Codex instruction assets") from exc
        target_path = Path(link_target)
        if not target_path.is_absolute():
            target_path = path.parent / target_path
        normalized_target = target_path.expanduser().absolute()
        if normalized_target in _seen:
            digest.update(b"symlink-cycle\0")
            return
        update_codex_instruction_asset_fingerprint(
            digest,
            target_path,
            relative_name=f"{relative_name}->target",
            ignore_direct_system=ignore_direct_system,
            _seen=_seen | {path.expanduser().absolute()},
        )
        return

    if stat.S_ISREG(mode):
        digest.update(b"file\0")
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            raise RuntimeError("Cannot read Codex instruction assets") from exc
        digest.update(b"\0")
        return

    if stat.S_ISDIR(mode):
        digest.update(b"directory\0")
        try:
            children = sorted(path.iterdir(), key=lambda child: child.name)
        except OSError as exc:
            raise RuntimeError("Cannot inspect Codex instruction assets") from exc
        if ignore_direct_system:
            digest.update(b"app-managed-system-skills\0")
            children = [child for child in children if child.name != ".system"]
        for child in children:
            update_codex_instruction_asset_fingerprint(
                digest,
                child,
                relative_name=f"{relative_name}/{child.name}",
                _seen=_seen | {path.expanduser().absolute()},
            )
        digest.update(b"end-directory\0")
        return

    digest.update(f"non-file:{mode}\0".encode("ascii"))
