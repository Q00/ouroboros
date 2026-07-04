"""Cleanup command for Ouroboros.

Prunes residue left behind by auto sessions (issue #1560):

- managed worktrees under the configured worktree root whose ``ooo/*`` branch
  is fully merged and whose checkout is clean (``--force`` also removes clean
  worktrees with unmerged branches, without force-deleting the branch)
- the pruned worktrees' ``ooo/*`` branches (safe ``git branch -d`` only)
- stale task lock files whose owning process is gone
- state files (``~/.ouroboros/data/auto_*.json``) of completed sessions whose
  worktree no longer exists (``--state-all`` extends this to blocked/failed
  sessions)

Never touches a worktree with a live (non-stale) lock or a dirty checkout.

Usage:
    ouroboros cleanup                # prune merged-and-clean auto worktrees
    ouroboros cleanup --dry-run      # report what would be removed
    ouroboros cleanup --force        # also remove clean-but-unmerged worktrees
    ouroboros cleanup --state-all    # also prune blocked/failed session state
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from ouroboros.cli.formatters.panels import print_info, print_success, print_warning
from ouroboros.core.worktree import (
    TaskWorkspace,
    WorktreeError,
    cleanup_task_workspace,
    discover_managed_workspaces,
    lock_file_is_stale,
    managed_worktree_root,
)

app = typer.Typer(
    name="cleanup",
    help="Prune leftover auto-session worktrees, branches, locks, and state files.",
    invoke_without_command=True,
)

_TERMINAL_STATE_PHASES_DEFAULT = frozenset({"complete"})
_TERMINAL_STATE_PHASES_ALL = frozenset({"complete", "blocked", "failed"})


def _auto_data_root() -> Path:
    return Path.home() / ".ouroboros" / "data"


def _locks_root(workspaces_root: Path) -> Path:
    return workspaces_root / ".locks"


def _workspace_is_active(workspace: TaskWorkspace) -> bool:
    """A workspace with a live (non-stale) lock belongs to a running session."""
    lock_path = Path(workspace.lock_path)
    return lock_path.exists() and not lock_file_is_stale(lock_path)


def _prune_workspaces(
    *,
    policy: str,
    dry_run: bool,
) -> tuple[list[TaskWorkspace], list[TaskWorkspace], list[TaskWorkspace]]:
    """Return (removed, skipped, failed) managed auto workspaces."""
    removed: list[TaskWorkspace] = []
    skipped: list[TaskWorkspace] = []
    failed: list[TaskWorkspace] = []
    for workspace in discover_managed_workspaces():
        if not workspace.durable_id.startswith("auto_"):
            skipped.append(workspace)
            continue
        if _workspace_is_active(workspace):
            skipped.append(workspace)
            continue
        try:
            if cleanup_task_workspace(workspace, policy=policy, dry_run=dry_run):
                if not dry_run:
                    Path(workspace.lock_path).unlink(missing_ok=True)
                removed.append(workspace)
            else:
                skipped.append(workspace)
        except WorktreeError:
            failed.append(workspace)
    return removed, skipped, failed


def _prune_stale_locks(workspaces_root: Path, *, dry_run: bool) -> list[Path]:
    """Remove stale lock files that no longer have a worktree directory."""
    removed: list[Path] = []
    locks_root = _locks_root(workspaces_root)
    if not locks_root.is_dir():
        return removed
    for repo_dir in sorted(locks_root.iterdir()):
        if not repo_dir.is_dir():
            continue
        for lock_path in sorted(repo_dir.glob("auto_*.json")):
            worktree_dir = workspaces_root / repo_dir.name / lock_path.stem
            if worktree_dir.exists():
                continue
            if not lock_file_is_stale(lock_path):
                continue
            if not dry_run:
                lock_path.unlink(missing_ok=True)
            removed.append(lock_path)
    return removed


def _state_phase(path: Path) -> str | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    phase = raw.get("phase")
    return phase if isinstance(phase, str) else None


def _prune_state_files(
    workspaces_root: Path,
    *,
    phases: frozenset[str],
    dry_run: bool,
) -> list[Path]:
    """Remove terminal session state files whose worktree is gone."""
    removed: list[Path] = []
    data_root = _auto_data_root()
    if not data_root.is_dir():
        return removed
    for state_path in sorted(data_root.glob("auto_*.json")):
        phase = _state_phase(state_path)
        if phase not in phases:
            continue
        session_id = state_path.stem
        has_worktree = (
            any(
                (repo_dir / session_id).exists()
                for repo_dir in workspaces_root.iterdir()
                if repo_dir.is_dir() and repo_dir.name != ".locks"
            )
            if workspaces_root.is_dir()
            else False
        )
        if has_worktree:
            continue
        if not dry_run:
            state_path.unlink(missing_ok=True)
        removed.append(state_path)
    return removed


@app.callback(invoke_without_command=True)
def cleanup(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Report what would be removed without removing anything.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help=(
                "Also remove clean worktrees whose branch is not merged yet. "
                "Unmerged branches themselves are kept."
            ),
        ),
    ] = False,
    state_all: Annotated[
        bool,
        typer.Option(
            "--state-all",
            help=(
                "Prune state files of blocked/failed sessions too "
                "(default prunes only completed sessions)."
            ),
        ),
    ] = False,
) -> None:
    """Prune merged-and-clean auto worktrees, stale locks, and orphaned state."""
    workspaces_root = managed_worktree_root()
    policy = "remove" if force else "prune-merged"

    removed, skipped, failed = _prune_workspaces(policy=policy, dry_run=dry_run)
    stale_locks = _prune_stale_locks(workspaces_root, dry_run=dry_run)
    phases = _TERMINAL_STATE_PHASES_ALL if state_all else _TERMINAL_STATE_PHASES_DEFAULT
    state_files = _prune_state_files(workspaces_root, phases=phases, dry_run=dry_run)

    verb = "Would remove" if dry_run else "Removed"
    for workspace in removed:
        print_info(f"{verb} worktree {workspace.worktree_path} (branch {workspace.branch})")
    for lock_path in stale_locks:
        print_info(f"{verb} stale lock {lock_path}")
    for state_path in state_files:
        print_info(f"{verb} session state {state_path}")
    for workspace in failed:
        print_warning(f"Could not clean {workspace.worktree_path} — see logs")

    if not removed and not stale_locks and not state_files:
        print_success("Nothing to clean up.")
    else:
        print_success(
            f"{verb}: {len(removed)} worktree(s), {len(stale_locks)} stale lock(s), "
            f"{len(state_files)} state file(s); skipped {len(skipped)} (active/dirty/unmerged)."
        )
