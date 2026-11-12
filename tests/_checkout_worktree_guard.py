"""Detect tests that register git worktrees in the developer's own checkout.

``ooo auto`` (and other task-workspace paths) create a managed worktree for the
repository that contains their working directory: ``git worktree add -b
ooo/<id> <worktree_root>/<repo>/<id>``. ``tests/conftest.py`` isolates
``$HOME``, so ``<worktree_root>`` lands in a per-test temporary home, but the
*repository* is still resolved from the process cwd. A test that drives such a
path without its own temporary repository therefore registers a worktree and a
branch in the checkout the suite runs from, and nothing removes them.

The guard watches the checkout's worktree registry (``<git common dir>/
worktrees``), which records every linked worktree's path. A registration that
appears during a test and points into this pytest process's temporary roots was
created by that test: other processes (a concurrent run in another worktree of
the same repository, a developer's own ``git worktree add``) write elsewhere,
so they never trip it. The guard only reads; it reports the exact cleanup
commands instead of removing anything from the developer's repository.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import subprocess

import pytest

_GIT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class LeakedWorktree:
    """One worktree registration a test left in the watched checkout."""

    name: str
    path: Path
    branch: str | None


def checkout_worktree_registry(repo_root: Path) -> Path | None:
    """Return ``<git common dir>/worktrees`` for ``repo_root``, or ``None``.

    ``None`` means the suite is not running from a git checkout (or git is
    unavailable), in which case there is nothing to protect.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=repo_root,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Git paths use filesystem encoding, including non-UTF-8 byte names on POSIX.
    common_dir = os.fsdecode(result.stdout.rstrip(b"\n"))
    if result.returncode != 0 or not common_dir:
        return None
    # ``--git-common-dir`` may print a path relative to ``cwd`` on older git.
    return (repo_root / common_dir).resolve() / "worktrees"


def registered_worktree_names(registry: Path) -> frozenset[str]:
    """Return the admin entry names currently in ``registry``."""
    try:
        return frozenset(os.listdir(registry))
    except FileNotFoundError:
        # A repository with no linked worktrees has no registry directory yet.
        return frozenset()


def _read_entry_file(entry: Path, name: str) -> str | None:
    try:
        return os.fsdecode((entry / name).read_bytes().rstrip(b"\n"))
    except OSError:
        return None


def _entry_worktree_path(entry: Path) -> Path | None:
    gitdir = _read_entry_file(entry, "gitdir")
    if not gitdir:
        return None
    # ``gitdir`` names ``<worktree>/.git``; git may store it relative to the
    # admin entry when ``worktree.useRelativePaths`` is enabled.
    return (entry / gitdir).resolve().parent


def _entry_branch(entry: Path) -> str | None:
    head = _read_entry_file(entry, "HEAD")
    prefix = "ref: refs/heads/"
    if head and head.startswith(prefix):
        return head[len(prefix) :]
    return None


def leaked_worktrees(
    registry: Path,
    before: frozenset[str],
    roots: Iterable[Path],
) -> list[LeakedWorktree]:
    """Return registrations added since ``before`` whose path is under ``roots``."""
    resolved_roots = [root.resolve() for root in roots]
    leaks: list[LeakedWorktree] = []
    for name in sorted(registered_worktree_names(registry) - before):
        entry = registry / name
        path = _entry_worktree_path(entry)
        if path is None or not any(path.is_relative_to(root) for root in resolved_roots):
            continue
        leaks.append(LeakedWorktree(name=name, path=path, branch=_entry_branch(entry)))
    return leaks


def describe_leaks(repo_root: Path, leaks: Iterable[LeakedWorktree]) -> str:
    """Render a failure message naming each leak and the commands that remove it."""
    leak_list = list(leaks)
    repo = shlex.quote(str(repo_root))
    lines = [
        f"This test registered {len(leak_list)} git worktree(s) in the checkout the "
        f"suite runs from ({repo_root}). Run the code under test against a "
        "temporary repository or directory (the auto_project_cwd fixture, or "
        "monkeypatch.chdir(tmp_path)) instead of the process cwd. To remove what "
        "it left behind:",
    ]
    for leak in leak_list:
        lines.append(f"  git -C {repo} worktree remove --force {shlex.quote(str(leak.path))}")
        if leak.branch is not None:
            lines.append(f"  git -C {repo} branch -D {shlex.quote(leak.branch)}")
    return "\n".join(lines)


def fail_if_worktrees_leaked(
    repo_root: Path,
    registry: Path,
    before: frozenset[str],
    roots: Iterable[Path],
) -> None:
    """Fail the current test when it left a worktree registered under ``roots``."""
    leaks = leaked_worktrees(registry, before, roots)
    if leaks:
        pytest.fail(describe_leaks(repo_root, leaks), pytrace=False)
