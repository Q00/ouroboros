"""Claim-then-verify primitives for publishing and removing owned artifacts.

A component that installs artifacts into directories it shares with operators
(runtime skill registries, bridge extensions, instruction guides) may replace
or delete only the exact generations it produced. An ownership check and a
later destructive filesystem operation cannot be safely separated in time: an
operator may replace the artifact between the check and the mutation. These
helpers close that window by atomically renaming the existing entry aside
first, re-validating the *claimed* generation, and only then deleting or
replacing it. Restoration is no-clobber: when another process recreates the
canonical path while a claim is held, both generations are preserved — the
recreated entry stays canonical and the claimed one remains beside it under
its claim name. The final rename never follows symlinks, so a link inserted
concurrently can never route a write to its target.

Related, deliberately separate machinery: :mod:`ouroboros.hermes.artifacts`
implements a heavier journaled variant of the same idea (swap-intent files
and restart recovery), and :mod:`ouroboros.codex.artifacts` carries its own
fingerprint-gated replacement. Consolidating those onto these primitives is a
candidate follow-up, not something callers should assume has happened.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import shutil
import tempfile


class UnownedArtifactError(OSError):
    """The target of a publication or removal is not the caller-owned generation."""


def remove_path(path: Path) -> None:
    """Remove a file, symlink, or directory tree."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def restore_claimed(claimed: Path, path: Path) -> bool:
    """Put a claimed generation back unless the canonical path was recreated.

    Returns True when restored. When another process recreated *path* while
    the claim was held, restoring would clobber that new generation, so both
    are preserved instead: the recreated entry stays canonical and the
    claimed one remains beside it under its claim name.
    """
    if os.path.lexists(path):
        return False
    try:
        os.replace(claimed, path)
    except FileNotFoundError:
        return False
    return True


def claim_and_remove_owned(path: Path, *, is_owned: Callable[[Path], bool]) -> bool:
    """Atomically claim *path* and delete it only if the claimed generation is owned.

    Returns False without touching anything when *path* is missing, and
    restores the claimed generation (no-clobber) when it is a symlink or
    fails the ownership re-check — the concurrent-replacement case.
    """
    claimed = path.with_name(f".{path.name}.{os.urandom(8).hex()}.removing")
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return False
    try:
        owned = not claimed.is_symlink() and is_owned(claimed)
    except BaseException:
        restore_claimed(claimed, path)
        raise
    if not owned:
        restore_claimed(claimed, path)
        return False
    remove_path(claimed)
    return True


def publish_owned_file(
    path: Path,
    content: str,
    *,
    is_owned: Callable[[Path], bool],
    mode: int | None = None,
) -> None:
    """Atomically publish one setup-owned file generation.

    Any existing entry is claimed (renamed aside) and re-validated before the
    replacement, so an operator file or symlink that appeared after the
    caller's own checks is restored untouched and the publication fails with
    :class:`UnownedArtifactError` instead of replacing it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    claimed: Path | None = None
    if os.path.lexists(path):
        claimed = path.with_name(f".{path.name}.{os.urandom(8).hex()}.replacing")
        os.replace(path, claimed)
        try:
            owned = not claimed.is_symlink() and is_owned(claimed)
        except BaseException:
            restore_claimed(claimed, path)
            raise
        if not owned:
            restore_claimed(claimed, path)
            raise UnownedArtifactError(f"preserved user-managed file at {path}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        if mode is not None and hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(temp_name).unlink()
        except OSError:
            pass
        if claimed is not None:
            restore_claimed(claimed, path)
        raise
    if claimed is not None:
        remove_path(claimed)
