"""Workspace isolation for the investigating self-answer (``auto/investigator.py``).

A runtime with a verified read-only mode reads the real workspace. Any other
runtime reads a throwaway copy, so an edit it makes cannot reach the real
workspace:

- in a git work tree, the copy holds every tracked and every untracked,
  non-ignored file as it is on disk now (``git ls-files --cached --others
  --exclude-standard``), without ``.git``; nothing is registered in the
  repository (no ``git worktree``);
- outside git, the copy is the whole directory without ``.git`` and
  ``__pycache__``.

``workspace_state`` fingerprints the real workspace (git: ``HEAD``, the
``git diff --binary HEAD`` bytes, and every untracked non-ignored file with its
bytes; outside git: the byte manifest digest of ``boundary/tree.py``). The
investigator compares it before and after each investigation and discards the
answer when it changed.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from ouroboros.boundary.tree import tree_digest

_GIT_TIMEOUT_SECONDS = 120


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    ).stdout


def _in_git_work_tree(root: Path) -> bool:
    try:
        return _git(root, "rev-parse", "--is-inside-work-tree").strip() == b"true"
    except (OSError, subprocess.SubprocessError):
        return False


def _listed(root: Path, *args: str) -> list[str]:
    return [os.fsdecode(item) for item in _git(root, "ls-files", "-z", *args).split(b"\0") if item]


def workspace_state(root: Path) -> str:
    """A fingerprint of the real workspace; it changes when any file does."""
    digest = hashlib.sha256()
    if not _in_git_work_tree(root):
        digest.update(b"tree\0" + tree_digest(root).encode())
        return digest.hexdigest()
    try:
        head = _git(root, "rev-parse", "HEAD").strip()
    except subprocess.CalledProcessError:
        head = b""  # a repository without commits
    digest.update(b"head\0" + head + b"\0")
    if head:
        digest.update(b"diff\0" + _git(root, "diff", "--binary", "HEAD", "--", "."))
    for name in sorted(_listed(root, "--others", "--exclude-standard")):
        path = root / name
        digest.update(b"untracked\0" + os.fsencode(name) + b"\0")
        if path.is_file() and not path.is_symlink():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _copy_listed_files(root: Path, destination: Path) -> None:
    for name in _listed(root, "--cached", "--others", "--exclude-standard"):
        source = root / name
        if source.is_dir() and not source.is_symlink():
            continue  # a submodule (gitlink); its files are not listed here
        if not source.exists() and not source.is_symlink():
            continue  # a tracked file deleted on disk
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def create_isolated_copy(root: Path) -> Path:
    """Return a new throwaway copy of ``root``; remove it with :func:`remove_isolated_copy`."""
    scratch = Path(tempfile.mkdtemp(prefix="ouroboros-investigation-"))
    copy = scratch / "repo"
    try:
        if _in_git_work_tree(root):
            copy.mkdir()
            _copy_listed_files(root, copy)
        else:
            shutil.copytree(
                root, copy, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__")
            )
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return copy


def remove_isolated_copy(copy: Path) -> None:
    """Delete a copy made by :func:`create_isolated_copy` (and its scratch folder)."""
    shutil.rmtree(copy.parent, ignore_errors=True)


__all__ = ["create_isolated_copy", "remove_isolated_copy", "workspace_state"]
