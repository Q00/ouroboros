"""Git-backed path classification for acceptance workspace evidence."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


def load_tracked_workspace_paths(root: Path) -> frozenset[Path] | None:
    """Return tracked paths, or ``None`` when Git cannot prove the set."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--cached", "-z"],
            cwd=root,
            capture_output=True,
            text=False,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if tracked.returncode != 0:
        return None
    return frozenset(Path(os.fsdecode(value)) for value in tracked.stdout.split(b"\0") if value)


def load_ignored_workspace_paths(root: Path) -> frozenset[Path] | None:
    """Return Git-ignored paths, or ``None`` when Git cannot prove the set.

    Ignored directories are collapsed to the directory entry (``--directory``)
    so a build tree such as ``target/`` or ``dist/`` is one path, and any
    descendant of it is classified by :func:`is_git_ignored_path`. Only
    untracked ignored paths are returned: a tracked file matched by an ignore
    rule stays tracked evidence.
    """
    try:
        ignored = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
            cwd=root,
            capture_output=True,
            text=False,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if ignored.returncode != 0:
        return None
    return frozenset(
        Path(os.fsdecode(value).rstrip("/")) for value in ignored.stdout.split(b"\0") if value
    )


def is_git_ignored_path(relative: Path, *, ignored_paths: frozenset[Path] | None) -> bool:
    """Return True when ``relative`` is, or lives under, a Git-ignored path.

    Ignore rules describe build outputs, caches, and local state the project
    itself declared as non-source. A verify command that refreshes them is
    still an observer of the acceptance-relevant workspace, and a sibling
    worker that regenerates them has not changed acceptance evidence.
    """
    return bool(
        ignored_paths is not None
        and relative.parts
        and any(relative == ignored or ignored in relative.parents for ignored in ignored_paths)
    )


def is_untracked_top_level_evidence_path(
    relative: Path,
    *,
    tracked_paths: frozenset[Path] | None,
    is_directory: bool,
) -> bool:
    """Return True only for an untracked evidence directory or descendant.

    A regular file or symlink named exactly evidence is not contained by that
    directory and must remain visible to workspace trust gates.
    """
    return bool(
        tracked_paths is not None
        and relative.parts
        and relative.parts[0] == "evidence"
        and (len(relative.parts) > 1 or is_directory)
        and not any(
            relative == tracked or relative in tracked.parents or tracked in relative.parents
            for tracked in tracked_paths
        )
    )


__all__ = [
    "is_git_ignored_path",
    "is_untracked_top_level_evidence_path",
    "load_ignored_workspace_paths",
    "load_tracked_workspace_paths",
]
