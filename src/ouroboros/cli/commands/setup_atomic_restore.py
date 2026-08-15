"""Failure-atomic directory restoration helpers for setup transactions."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import tempfile
from typing import Any


def atomic_restore_generation(
    path: Path,
    prior: Any,
    expected: Any,
    *,
    snapshot: Callable[[Path], Any],
    restore: Callable[[Path, Any], None],
    remove: Callable[[Path], None],
) -> bool:
    """Stage a prior generation and swap it live without partial restoration."""
    if snapshot(path) != expected:
        return False
    stage = Path(tempfile.mkdtemp(prefix=".ouroboros-rollback-", dir=path.parent))
    remove(stage)
    restore(stage, prior)
    failed = path.with_name(f".{path.name}.failed.{os.getpid()}")
    os.replace(path, failed)
    if getattr(prior, "kind", None) == "missing":
        remove(failed)
        return True
    try:
        os.replace(stage, path)
    except BaseException:
        if failed.exists() and not path.exists():
            os.replace(failed, path)
        raise
    remove(failed)
    return True


def restore_hermes(path: Path, prior: Any, expected: Any) -> bool:
    """Restore a Hermes topology snapshot through the generic atomic swap."""
    from ouroboros.cli.commands import setup

    return atomic_restore_generation(
        path,
        prior,
        expected,
        snapshot=lambda target: setup._snapshot_path(target, follow_links=False),
        restore=lambda target, value: setup._restore_path_snapshot(
            target, value, restore_link_targets=False
        ),
        remove=setup._remove_path_topology,
    )
