"""Claim-then-verify primitives for removing setup-owned artifacts.

An ownership check and a later destructive filesystem operation cannot be
safely separated in time: an operator may replace a managed artifact between
the check and the deletion. These helpers close that window by atomically
renaming the artifact aside first, re-validating the *claimed* generation,
and either deleting it or restoring it untouched.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import shutil


def remove_path(path: Path) -> None:
    """Remove a file, symlink, or directory tree."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def claim_and_remove_setup_owned(path: Path, *, is_owned: Callable[[Path], bool]) -> bool:
    """Atomically claim *path* and delete it only if the claimed generation is owned.

    Returns False without touching anything when *path* is missing, and
    restores the claimed generation untouched when it is a symlink or fails
    the ownership re-check — the concurrent-replacement case.
    """
    claimed = path.with_name(f".{path.name}.{os.urandom(8).hex()}.removing")
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return False
    try:
        owned = not claimed.is_symlink() and is_owned(claimed)
    except BaseException:
        os.replace(claimed, path)
        raise
    if not owned:
        os.replace(claimed, path)
        return False
    remove_path(claimed)
    return True
