"""Managed task worktrees on Git older than 2.36.0.

A run in a managed task worktree proves the worktree belongs to its source
checkout from Git's worktree population (``worktree list --porcelain -z``,
Git 2.36.0). On an older Git that proof is impossible, so provisioning a run's
task worktree is refused before anything exists on disk. Worktrees that never
reach the managed resolver (the auto session worktree, an evolution lineage
worktree) are still created.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from ouroboros.auto.state import AutoPipelineState, AutoWorktreePolicy
from ouroboros.auto.worktree import ensure_auto_worktree
from ouroboros.core import project_identity
from ouroboros.core.project_identity import resolve_project_identity
from ouroboros.core.worktree import (
    TaskWorkspace,
    WorktreeError,
    maybe_prepare_task_workspace,
    maybe_restore_task_workspace,
    release_task_workspace,
)

_OLD_GIT = b"git version 2.34.1\n"
_SUPPORTED_GIT = b"git version 2.36.0\n"


@contextmanager
def _git_reporting(version: bytes, worktree_root: Path) -> Iterator[None]:
    """Fake ``git --version`` for project identity; every other Git call is real."""
    real_run_git_command = project_identity._run_git_command

    def fake(*arguments: str) -> bytes:
        if arguments == ("--version",):
            return version
        return real_run_git_command(*arguments)

    with (
        patch("ouroboros.core.project_identity._run_git_command", side_effect=fake),
        patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root),
        patch("ouroboros.core.worktree._worktrees_enabled", return_value=True),
    ):
        yield


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test User"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    subprocess.run(["git", "add", "pyproject.toml"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)


def _branches(repo: Path) -> str:
    return subprocess.run(
        ["git", "branch", "--list", "ooo/*"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _assert_refused_before_creation(error: WorktreeError, repo: Path, root: Path) -> None:
    assert "found 2.34.1" in str(error)
    assert "orchestrator.use_worktrees: false" in str(error)
    assert not root.exists() or not any(root.rglob("*"))
    assert _branches(repo) == ""


def test_old_git_refuses_a_run_task_worktree_before_creating_it(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    root = tmp_path / "worktrees"

    with _git_reporting(_OLD_GIT, root), pytest.raises(WorktreeError) as caught:
        maybe_prepare_task_workspace(repo, "orch_old_git")

    _assert_refused_before_creation(caught.value, repo, root)


def test_old_git_refuses_to_bootstrap_a_run_task_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    root = tmp_path / "worktrees"

    with _git_reporting(_OLD_GIT, root), pytest.raises(WorktreeError) as caught:
        maybe_restore_task_workspace("orch_old_git", None, fallback_source_cwd=repo)

    _assert_refused_before_creation(caught.value, repo, root)


def test_old_git_refuses_to_restore_a_persisted_run_task_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    root = tmp_path / "worktrees"
    persisted = TaskWorkspace(
        durable_id="orch_old_git",
        repo_root=str(repo),
        repo_name="repo",
        original_cwd=str(repo),
        effective_cwd=str(root / "repo" / "orch_old_git"),
        worktree_path=str(root / "repo" / "orch_old_git"),
        branch="ooo/orch_old_git",
        lock_path=str(root / ".locks" / "repo" / "orch_old_git.json"),
    )

    with _git_reporting(_OLD_GIT, root), pytest.raises(WorktreeError) as caught:
        maybe_restore_task_workspace("orch_old_git", persisted, fallback_source_cwd=repo)

    _assert_refused_before_creation(caught.value, repo, root)


def test_old_git_still_creates_a_worktree_that_skips_managed_identity(tmp_path: Path) -> None:
    """An evolution lineage worktree is never a run's task workspace."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    root = tmp_path / "worktrees"

    with _git_reporting(_OLD_GIT, root):
        workspace = maybe_restore_task_workspace(
            "lin_old_git",
            None,
            fallback_source_cwd=repo,
            resolves_managed_identity=False,
        )
        assert workspace is not None
        release_task_workspace(workspace, cleanup=False)

    assert Path(workspace.worktree_path).is_dir()


def test_supported_git_provisions_a_run_task_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    root = tmp_path / "worktrees"

    with _git_reporting(_SUPPORTED_GIT, root):
        workspace = maybe_prepare_task_workspace(repo, "orch_new_git")
        assert workspace is not None
        release_task_workspace(workspace, cleanup=False)

    assert Path(workspace.worktree_path).is_dir()
    assert "ooo/orch_new_git" in _branches(repo)


def test_old_git_auto_session_worktree_still_runs_with_direct_identity(tmp_path: Path) -> None:
    """The default ``ooo auto`` path is untouched by the run task-worktree refusal.

    Auto makes its own worktree and the run executes inside it without a task
    workspace, so project identity is the direct resolver's degraded identity.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    root = tmp_path / "worktrees"
    state = AutoPipelineState(goal="build a CLI", cwd=str(repo))
    state.active_domain_profile_name = "coding"
    state.worktree_policy = AutoWorktreePolicy.AUTO

    with _git_reporting(_OLD_GIT, root):
        workspace = ensure_auto_worktree(state)
        assert workspace is not None
        identity = resolve_project_identity(state.cwd)
        release_task_workspace(workspace, cleanup=False)

    assert state.cwd == workspace.effective_cwd
    assert identity.project_root == str(Path(workspace.effective_cwd).resolve())
    assert identity.degraded_reason == project_identity.DEGRADED_REASON_GIT_VERSION
