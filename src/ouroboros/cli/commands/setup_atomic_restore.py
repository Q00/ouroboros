"""Failure-atomic directory restoration helpers for setup transactions."""

from __future__ import annotations

from collections.abc import Callable
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
    try:
        restore(stage, prior)
    except BaseException:
        remove(stage)
        raise
    from ouroboros.hermes.artifacts import atomic_remove_generation, atomic_swap_generation

    def commit_check() -> bool:
        return snapshot(path) == expected

    if getattr(prior, "kind", None) == "missing":
        atomic_remove_generation(path, expected_check=commit_check)
        return True
    try:
        atomic_swap_generation(path, stage, expected_check=commit_check)
    except BaseException:
        remove(stage)
        raise
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


def restore_hermes_receipt(path: Path, prior: Any, receipt: Any) -> bool:
    """Restore only while the exact receipted Hermes generation remains live."""
    from ouroboros.cli.commands import setup

    return atomic_restore_generation(
        path,
        prior,
        receipt,
        snapshot=lambda target: receipt if receipt.matches(target) else None,
        restore=lambda target, value: setup._restore_path_snapshot(
            target, value, restore_link_targets=False
        ),
        remove=setup._remove_path_topology,
    )
