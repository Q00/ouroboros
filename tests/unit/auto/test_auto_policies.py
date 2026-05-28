from __future__ import annotations

from ouroboros.auto.policies import apply_domain_policy_defaults
from ouroboros.auto.profiles.coding import DEFAULT_COMMIT_POLICY, DEFAULT_WORKTREE_POLICY
from ouroboros.auto.state import AutoCommitPolicy, AutoPipelineState, AutoWorktreePolicy


def test_coding_profile_defaults_to_ac_checkpoints_and_auto_worktree() -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.active_domain_profile_name = "coding"

    apply_domain_policy_defaults(state)

    assert state.commit_policy is DEFAULT_COMMIT_POLICY
    assert state.worktree_policy is DEFAULT_WORKTREE_POLICY
    assert state.commit_policy is AutoCommitPolicy.AC_CHECKPOINT
    assert state.worktree_policy is AutoWorktreePolicy.AUTO


def test_non_coding_profile_defaults_to_conservative_policies() -> None:
    state = AutoPipelineState(goal="Summarize papers", cwd="/tmp/project")
    state.active_domain_profile_name = "research"

    apply_domain_policy_defaults(state)

    assert state.commit_policy is AutoCommitPolicy.FINAL_ONLY
    assert state.worktree_policy is AutoWorktreePolicy.CURRENT
