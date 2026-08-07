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


def is_untracked_top_level_evidence_path(
    relative: Path,
    *,
    tracked_paths: frozenset[Path] | None,
) -> bool:
    """Return True only when Git proves a top-level evidence path untracked."""
    return bool(
        tracked_paths is not None
        and relative.parts
        and relative.parts[0] == "evidence"
        and not any(
            relative == tracked or relative in tracked.parents or tracked in relative.parents
            for tracked in tracked_paths
        )
    )


__all__ = ["is_untracked_top_level_evidence_path", "load_tracked_workspace_paths"]
