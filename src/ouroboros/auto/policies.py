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

    # Non-coding / unknown domains never auto-commit or relocate the caller's
    # checkout by default. Final-only commits on the current checkout are an
    # explicit operator opt-in (``--commit-policy final_only``) rather than a
    # silent side effect of completing a research/documentation/skip_run flow.
    state.commit_policy = AutoCommitPolicy.NONE
    state.worktree_policy = AutoWorktreePolicy.CURRENT
