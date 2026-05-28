"""Auto-mode execution policy defaults."""

from __future__ import annotations

from ouroboros.auto.state import AutoCommitPolicy, AutoPipelineState, AutoWorktreePolicy


def apply_domain_policy_defaults(state: AutoPipelineState) -> None:
    """Apply domain-specific policy defaults to a newly profiled session.

    The defaults are intentionally written into :class:`AutoPipelineState` so
    resume keeps the same isolation/commit behavior even if profile detection
    would later change.
    """
    if state.active_domain_profile_name == "coding":
        from ouroboros.auto.profiles.coding import (
            DEFAULT_COMMIT_POLICY,
            DEFAULT_WORKTREE_POLICY,
        )

        state.commit_policy = DEFAULT_COMMIT_POLICY
        state.worktree_policy = DEFAULT_WORKTREE_POLICY
        return

    state.commit_policy = AutoCommitPolicy.FINAL_ONLY
    state.worktree_policy = AutoWorktreePolicy.CURRENT
