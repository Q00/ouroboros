"""Managed worktree support for auto coding sessions."""

from __future__ import annotations

from pathlib import Path

import structlog

from ouroboros.auto.state import AutoPipelineState, AutoWorktreePolicy
from ouroboros.core.worktree import (
    TaskWorkspace,
    WorktreeError,
    is_git_repo,
    release_task_workspace,
    restore_task_workspace,
    sweep_stale_workspaces,
)

log = structlog.get_logger()


def _sweep_abandoned_worktrees() -> None:
    """Best-effort reclaim of worktrees left by sessions that never released."""
    try:
        sweep_stale_workspaces()
    except Exception as exc:  # noqa: BLE001 - housekeeping must never break a session
        log.warning("auto.worktree_sweep_failed", error=str(exc))


def _resume_source_cwd(state: AutoPipelineState, persisted: TaskWorkspace | None) -> str:
    """Resolve the directory a resumed session should provision its worktree from.

    ``state.cwd`` points *into* the previous worktree once a session has run. A
    cleanup policy that reclaims that worktree therefore leaves ``state.cwd``
    dangling, and the repo check below would silently skip worktree creation on
    resume. The workspace records the repo it came from — fall back to it.
    """
    if persisted is not None and not Path(state.cwd).is_dir():
        return persisted.original_cwd
    return state.cwd


def ensure_auto_worktree(state: AutoPipelineState) -> TaskWorkspace | None:
    """Create or restore the auto session worktree when policy requires it.

    ``AUTO`` is intentionally coding-only: non-coding sessions can still opt in
    with ``ALWAYS``, but the default remains the caller's current directory.
    """
    if state.worktree_policy in {AutoWorktreePolicy.NONE, AutoWorktreePolicy.CURRENT}:
        return None
    if (
        state.worktree_policy is AutoWorktreePolicy.AUTO
        and state.active_domain_profile_name != "coding"
    ):
        return None
    persisted = TaskWorkspace.from_progress_dict(state.managed_worktree)
    source_cwd = _resume_source_cwd(state, persisted)

    if not is_git_repo(source_cwd):
        if state.worktree_policy is AutoWorktreePolicy.ALWAYS:
            raise WorktreeError(
                "Auto worktree policy requires a git repository",
                details={"cwd": source_cwd},
            )
        return None

    _sweep_abandoned_worktrees()

    workspace = restore_task_workspace(
        state.auto_session_id,
        persisted,
        fallback_source_cwd=source_cwd,
        allow_dirty=True,
    )
    state.managed_worktree = workspace.to_progress_dict()
    state.cwd = workspace.effective_cwd
    return workspace


def release_auto_worktree(workspace: TaskWorkspace | None) -> None:
    """Release the auto worktree and apply the configured cleanup policy."""
    release_task_workspace(workspace)
