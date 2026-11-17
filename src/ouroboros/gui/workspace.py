"""The git state of a chat's project folder, and the one change the console makes to it.

A hand-off runs each task in a git worktree, so the folder must be a git
repository with at least one commit. The console reads that state (read-only)
to warn early, and on request prepares a folder that is not ready: ``git init``
when it is not a repository, then one empty initial commit. It never writes
git config and never touches a repository that already has commits.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Literal

GitKind = Literal["ready", "no_commits", "not_repo", "unreadable", "no_git"]

INITIAL_COMMIT_MESSAGE = "Start Ouroboros project"
# Used only on the initial commit, and only for the keys git has no value for.
FALLBACK_IDENTITY = (("user.name", "Ouroboros"), ("user.email", "ouroboros@localhost"))

_TIMEOUT = 10.0
_COMMIT_TIMEOUT = 20.0  # a commit may run the user's hooks or signing
MAX_FILES = 50  # paths a result lists; its count always covers every path


@dataclass(frozen=True, slots=True)
class GitState:
    """What git says about a folder; ``detail`` is git's own words when it could not tell."""

    kind: GitKind
    detail: str = ""

    @property
    def needs_prepare(self) -> bool:
        """A folder the console can make ready: not a repository, or one with no commits."""
        return self.kind in ("not_repo", "no_commits")


class GitPrepareError(Exception):
    """Preparing the folder failed; the message says which step and why."""


def _git(
    cwd: str, *args: str, timeout: float = _TIMEOUT, config: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    """Run one git command in ``cwd``: no prompt can reach the console's terminal."""
    overrides = [item for pair in config for item in ("-c", pair)]
    return subprocess.run(  # noqa: S603 - fixed git arguments
        ["git", *overrides, "-C", cwd, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _nearest_git_entry(cwd: str) -> Path | None:
    """The closest folder, ``cwd`` or a parent, holding a ``.git`` entry of any kind.

    ``lexists`` sees a broken symlink too: git skips an entry it cannot read
    and walks on to a parent repository, so the entry itself is what counts.
    """
    path = Path(cwd).resolve()
    return next(
        (folder for folder in (path, *path.parents) if os.path.lexists(folder / ".git")), None
    )


def git_state(cwd: str) -> GitState:
    """Classify ``cwd`` without changing anything in it.

    A ``.git`` entry git cannot use (a broken link, a file naming a missing
    git directory, an empty directory, a repository it refuses to read) makes
    the folder ``unreadable``, even when git falls back to a parent repository.
    """
    declared = _nearest_git_entry(cwd)
    unusable = f"{declared / '.git' if declared else '.git'} is not a git repository git can use."
    try:
        inside = _git(cwd, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0:
            # Never initialize over a .git entry, readable or not.
            if declared is not None:
                return GitState("unreadable", inside.stderr.strip() or unusable)
            return GitState("not_repo")
        if inside.stdout.strip() != "true":
            return GitState(
                "unreadable", "This folder is inside a .git directory or a bare repository."
            )
        top = _git(cwd, "rev-parse", "--show-toplevel")
        if declared is not None and (
            top.returncode != 0 or Path(top.stdout.strip()).resolve() != declared
        ):
            # git skipped the nearest .git entry for a repository further up.
            return GitState("unreadable", unusable)
        head = _git(cwd, "rev-parse", "--verify", "--quiet", "HEAD")
    except OSError:
        return GitState("no_git")
    except subprocess.TimeoutExpired:
        return GitState("unreadable", f"git did not answer within {_TIMEOUT:g} seconds.")
    return GitState("ready" if head.returncode == 0 else "no_commits")


def workspace_problem(state: GitState) -> str | None:
    """Why a hand-off cannot start in a folder in ``state``, or ``None``."""
    if state.kind == "not_repo":
        return (
            "Ouroboros runs each task in a git worktree, and this folder is not a git "
            "repository. Ouroboros can make it one with an empty initial commit."
        )
    if state.kind == "no_commits":
        return (
            "This git repository has no commits yet, so Ouroboros cannot create task "
            "worktrees. Ouroboros can add an empty initial commit."
        )
    if state.kind == "unreadable":
        return f"git cannot use this folder as a work tree: {state.detail}"
    return None  # ready, or no git binary: the run reports its own error


def _missing_identity(cwd: str) -> tuple[str, ...]:
    """``key=value`` overrides for the identity keys git has no value for here."""
    missing = []
    for key, value in FALLBACK_IDENTITY:
        found = _git(cwd, "config", "--get", key)
        if found.returncode != 0 or not found.stdout.strip():
            missing.append(f"{key}={value}")
    return tuple(missing)


def prepare_git(cwd: str) -> str:
    """Make ``cwd`` a repository with one commit; returns what was done.

    A folder that is already ready is left untouched. The initial commit is
    empty even when files are staged (``--only`` with no paths), so nothing of
    the user's is committed. Raises :class:`GitPrepareError` on any failure.
    """
    state = git_state(cwd)
    if state.kind == "ready":
        return "unchanged"
    if not state.needs_prepare:
        raise GitPrepareError(workspace_problem(state) or "git is not installed.")
    try:
        if state.kind == "not_repo":
            init = _git(cwd, "init")
            if init.returncode != 0:
                raise GitPrepareError(f"git init failed: {init.stderr.strip() or init.returncode}")
        identity = _missing_identity(cwd)
        commit = _git(
            cwd,
            "commit",
            "--allow-empty",
            "--only",
            "-m",
            INITIAL_COMMIT_MESSAGE,
            timeout=_COMMIT_TIMEOUT,
            config=identity,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitPrepareError(f"git did not finish within {exc.timeout:g} seconds.") from exc
    except OSError as exc:
        raise GitPrepareError(f"git could not run: {exc}") from exc
    if commit.returncode != 0:
        detail = (commit.stderr or commit.stdout).strip() or f"exit {commit.returncode}"
        raise GitPrepareError(f"git commit failed: {detail}")
    if git_state(cwd).kind != "ready":
        raise GitPrepareError("git reported success, but the folder still has no commit.")
    return "initialized" if state.kind == "not_repo" else "committed"


@dataclass(frozen=True, slots=True)
class FolderChanges:
    """Paths ``git status`` reports as changed: the first ``MAX_FILES``, and how many in all."""

    files: tuple[str, ...]
    count: int


def status_paths(porcelain: str) -> list[str]:
    """Every path in ``git status --porcelain`` output, as git reports it, in its order."""
    return [line[3:] for line in porcelain.splitlines() if line[3:]]


def folder_changes(cwd: str) -> FolderChanges | None:
    """What ``git status`` reports as changed in ``cwd``; ``None`` when git cannot say."""
    if git_state(cwd).kind not in ("ready", "no_commits"):
        return None
    try:
        status = _git(cwd, "status", "--porcelain")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if status.returncode != 0:
        return None
    files = status_paths(status.stdout)
    return FolderChanges(tuple(files[:MAX_FILES]), len(files))


__all__ = [
    "FALLBACK_IDENTITY",
    "INITIAL_COMMIT_MESSAGE",
    "MAX_FILES",
    "FolderChanges",
    "GitPrepareError",
    "GitState",
    "folder_changes",
    "git_state",
    "prepare_git",
    "status_paths",
    "workspace_problem",
]
