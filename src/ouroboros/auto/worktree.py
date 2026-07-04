"""Managed worktree support for auto coding sessions."""

from __future__ import annotations

from ouroboros.auto.state import AutoPipelineState, AutoWorktreePolicy
from ouroboros.core.worktree import (
    TaskWorkspace,
    WorktreeError,
    is_git_repo,
    release_task_workspace,
    restore_task_workspace,
)


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
    if not is_git_repo(state.cwd):
        if state.worktree_policy is AutoWorktreePolicy.ALWAYS:
            raise WorktreeError(
                "Auto worktree policy requires a git repository",
                details={"cwd": state.cwd},
            )
        return None

    persisted = TaskWorkspace.from_progress_dict(state.managed_worktree)
    workspace = restore_task_workspace(
        state.auto_session_id,
        persisted,
        fallback_source_cwd=state.cwd,
        allow_dirty=True,
    )
    state.managed_worktree = workspace.to_progress_dict()
    state.cwd = workspace.effective_cwd
    return workspace


def release_auto_worktree(workspace: TaskWorkspace | None) -> None:
    """Release the auto worktree and apply the configured cleanup policy."""
    release_task_workspace(workspace)
