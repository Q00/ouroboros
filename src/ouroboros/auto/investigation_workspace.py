"""Workspace isolation for the investigating self-answer (``auto/investigator.py``).

A runtime with a verified read-only mode reads the real workspace. Any other
runtime reads a throwaway copy, so an edit it makes cannot reach the real
workspace:

- in a git work tree, the copy holds every tracked and every untracked,
  non-ignored file as it is on disk now (``git ls-files --cached --others
  --exclude-standard``), without ``.git``; nothing is registered in the
  repository (no ``git worktree``);
- outside git, the copy is the whole directory without ``.git`` and
  ``__pycache__``;
- a symbolic link never leads outside the copy (``_copy_entry``), and the
  finished copy is walked to prove that no path resolves outside it.

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
# A link to a file outside the workspace is copied as bytes only up to this size.
MAX_LINKED_FILE_BYTES = 5_000_000
_SKIPPED_DIRS = frozenset({".git", "__pycache__"})


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


class IsolationError(RuntimeError):
    """The copy could not be made safe; no runtime may run in it."""


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _copy_entry(root: Path, destination: Path, name: str) -> None:
    """Copy one workspace entry so that nothing in the copy leads outside it.

    A regular file is copied. A symbolic link is never copied as it is:

    - target inside the workspace: a relative link to the same path inside the
      copy (it resolves inside the copy even when that path is absent);
    - target outside the workspace: its bytes as a regular file when it is a
      readable regular file up to ``MAX_LINKED_FILE_BYTES``, else skipped;
    - a dangling link whose target would be outside the workspace: skipped.
    """
    source = root / name
    target = destination / name
    if source.is_symlink():
        resolved = Path(os.path.realpath(source))
        if _inside(resolved, root):
            target.parent.mkdir(parents=True, exist_ok=True)
            relative = os.path.relpath(destination / resolved.relative_to(root), target.parent)
            target.symlink_to(relative)
            return
        try:
            if not resolved.is_file() or resolved.stat().st_size > MAX_LINKED_FILE_BYTES:
                return
            data = resolved.read_bytes()
        except OSError:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return
    if source.is_dir() or not source.exists():
        return  # a submodule (gitlink) or a tracked file deleted on disk
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)


def _workspace_entries(root: Path) -> list[str]:
    """Every file and symbolic link to copy, relative to ``root``."""
    if _in_git_work_tree(root):
        return _listed(root, "--cached", "--others", "--exclude-standard")
    entries: list[str] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        kept = [d for d in dirs if d not in _SKIPPED_DIRS]
        entries += [
            (base / d).relative_to(root).as_posix() for d in kept if (base / d).is_symlink()
        ]
        dirs[:] = [d for d in kept if not (base / d).is_symlink()]
        entries += [(base / f).relative_to(root).as_posix() for f in files]
    return entries


def _escaping_paths(copy: Path) -> list[str]:
    """Paths in ``copy`` that resolve outside it (there must be none)."""
    escaping: list[str] = []
    for current, dirs, files in os.walk(copy, followlinks=False):
        for name in (*dirs, *files):
            path = Path(current) / name
            if not _inside(Path(os.path.realpath(path)), copy):
                escaping.append(path.relative_to(copy).as_posix())
    return escaping


def create_isolated_copy(root: Path) -> Path:
    """Return a new throwaway copy of ``root``; remove it with :func:`remove_isolated_copy`.

    Raises :class:`IsolationError` when any path in the finished copy resolves
    outside it, so no runtime is launched there.
    """
    root = root.resolve()
    scratch = Path(tempfile.mkdtemp(prefix="ouroboros-investigation-")).resolve()
    copy = scratch / "repo"
    try:
        copy.mkdir()
        for name in _workspace_entries(root):
            _copy_entry(root, copy, name)
        escaping = _escaping_paths(copy)
        if escaping:
            raise IsolationError(f"copy paths resolve outside the copy: {escaping[:5]}")
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return copy


def remove_isolated_copy(copy: Path) -> None:
    """Delete a copy made by :func:`create_isolated_copy` (and its scratch folder)."""
    shutil.rmtree(copy.parent, ignore_errors=True)


__all__ = [
    "IsolationError",
    "create_isolated_copy",
    "remove_isolated_copy",
    "workspace_state",
]
