from __future__ import annotations

import subprocess

from ouroboros.auto.state import AutoPipelineState, AutoWorktreePolicy
from ouroboros.auto.worktree import ensure_auto_worktree, release_auto_worktree
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
        release_auto_worktree(workspace)


def test_coding_auto_policy_reuses_persisted_managed_worktree(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(repo))
    state.active_domain_profile_name = "coding"
    state.worktree_policy = AutoWorktreePolicy.AUTO

    first = ensure_auto_worktree(state)
    release_auto_worktree(first)

    second = ensure_auto_worktree(state)
    try:
        assert first is not None
        assert second is not None
        assert second.worktree_path == first.worktree_path
        assert second.branch == first.branch
        assert state.managed_worktree is not None
    finally:
        release_auto_worktree(second)


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
    monkeypatch.setattr("ouroboros.auto.worktree.release_task_workspace", released.append)

    release_auto_worktree(workspace)

    assert released == [workspace]
