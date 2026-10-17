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


def is_declared_contract_path(relative: Path, declared_paths: set[Path]) -> bool:
    return any(
        relative == declared or relative in declared.parents or declared in relative.parents
        for declared in declared_paths
    )


def collect_bounded_workspace_paths(
    root: Path,
    *,
    declared_paths: set[Path],
    tracked_paths: frozenset[Path] | None,
    ignored_paths: frozenset[Path] | None,
    ignored_directory_names: frozenset[str],
    max_entries: int,
    max_depth: int,
) -> list[Path] | None:
    """List relevant workspace entries within strict entry and depth limits."""
    paths: list[Path] = []
    pending_directories = [(root, 0)]
    visited_entries = 0
    while pending_directories:
        directory, depth = pending_directories.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    visited_entries += 1
                    if visited_entries >= max_entries:
                        return None
                    entries.append(entry)
        except OSError:
            return None

        child_directories: list[Path] = []
        for entry in sorted(entries, key=lambda item: item.name):
            path = Path(entry.path)
            is_symlink = entry.is_symlink()
            is_directory = entry.is_dir(follow_symlinks=False)
            relative = path.relative_to(root)
            declared = is_declared_contract_path(relative, declared_paths)
            ignored = (
                is_untracked_top_level_evidence_path(
                    relative, tracked_paths=tracked_paths, is_directory=is_directory
                )
                or is_git_ignored_path(relative, ignored_paths=ignored_paths)
                or any(part in ignored_directory_names for part in relative.parts)
            )
            if ignored and not declared:
                continue
            paths.append(path)
            if is_directory and not is_symlink:
                if depth >= max_depth:
                    return None
                child_directories.append(path)
        pending_directories.extend((path, depth + 1) for path in reversed(child_directories))
    return sorted(paths, key=lambda path: path.as_posix())


__all__ = [
    "is_git_ignored_path",
    "is_untracked_top_level_evidence_path",
    "is_declared_contract_path",
    "collect_bounded_workspace_paths",
    "load_ignored_workspace_paths",
    "load_tracked_workspace_paths",
]
