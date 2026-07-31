from __future__ import annotations

import subprocess

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.state import AutoPipelineState, AutoWorktreePolicy
from ouroboros.auto.worktree import (
    auto_worktree_cleanup_eligible,
    ensure_auto_worktree,
    release_auto_worktree,
)
from ouroboros.core.worktree import TaskWorkspace


def _git(repo, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    _git(path, "add", "pyproject.toml")
    _git(path, "commit", "-m", "initial")


def test_coding_auto_policy_creates_managed_worktree_and_updates_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "dirty.txt").write_text("uncommitted caller work\n", encoding="utf-8")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(repo))
    state.active_domain_profile_name = "coding"
    state.worktree_policy = AutoWorktreePolicy.AUTO

    workspace = ensure_auto_worktree(state)
    try:
        assert workspace is not None
        assert state.managed_worktree is not None
        assert state.cwd == workspace.effective_cwd
        assert workspace.worktree_path != str(repo)
        assert workspace.branch == f"ooo/{state.auto_session_id}"
        assert not (repo / "dirty.txt").exists() or (repo / "dirty.txt").read_text() == (
            "uncommitted caller work\n"
        )
    finally:
        release_auto_worktree(workspace, cleanup=True)


def test_coding_auto_policy_reuses_persisted_managed_worktree(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(repo))
    state.active_domain_profile_name = "coding"
    state.worktree_policy = AutoWorktreePolicy.AUTO

    first = ensure_auto_worktree(state)
    release_auto_worktree(first, cleanup=False)

    second = ensure_auto_worktree(state)
    try:
        assert first is not None
        assert second is not None
        assert second.worktree_path == first.worktree_path
        assert second.branch == first.branch
        assert state.managed_worktree is not None
    finally:
        release_auto_worktree(second, cleanup=True)


def test_auto_policy_gracefully_skips_non_repo(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.active_domain_profile_name = "coding"
    state.worktree_policy = AutoWorktreePolicy.AUTO

    assert ensure_auto_worktree(state) is None
    assert state.managed_worktree is None
    assert state.cwd == str(tmp_path)


def test_release_auto_worktree_delegates_to_task_workspace_release(monkeypatch) -> None:
    workspace = TaskWorkspace(
        durable_id="auto_test",
        repo_root="/tmp/repo",
        repo_name="repo",
        original_cwd="/tmp/repo",
        effective_cwd="/tmp/worktrees/repo/auto_test",
        worktree_path="/tmp/worktrees/repo/auto_test",
        branch="ooo/auto_test",
        lock_path="/tmp/worktrees/.locks/repo/auto_test.json",
    )
    released = []

    def capture(candidate, *, cleanup: bool) -> None:
        released.append((candidate, cleanup))

    monkeypatch.setattr("ouroboros.auto.worktree.release_task_workspace", capture)

    release_auto_worktree(workspace, cleanup=False)

    assert released == [(workspace, False)]


def _result(**overrides) -> AutoPipelineResult:
    values = {
        "status": "complete",
        "auto_session_id": "auto_test",
        "phase": "complete",
    }
    values.update(overrides)
    return AutoPipelineResult(**values)


def test_auto_cleanup_requires_positive_terminal_result() -> None:
    assert auto_worktree_cleanup_eligible(None) is False
    assert auto_worktree_cleanup_eligible(_result(status="blocked")) is False
    assert auto_worktree_cleanup_eligible(_result(status="detached")) is False
    assert auto_worktree_cleanup_eligible(_result(status="complete")) is True


def test_auto_cleanup_rejects_live_handoff_and_plugin_delegation() -> None:
    assert (
        auto_worktree_cleanup_eligible(_result(run_handoff_status="started", job_id="job_live"))
        is False
    )
    assert auto_worktree_cleanup_eligible(_result(ralph_dispatch_mode="plugin")) is False


def test_auto_cleanup_accepts_reconciled_terminal_handoff() -> None:
    assert (
        auto_worktree_cleanup_eligible(
            _result(
                run_handoff_status="started",
                job_id="job_done",
                execution_job_status="completed",
            )
        )
        is True
    )
