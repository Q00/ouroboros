from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from ouroboros.core.worktree import WorktreeError, prepare_task_workspace, release_lock


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(path, "add", "product.py")
    _git(path, "commit", "-m", "initial")


def test_prepare_task_workspace_allows_only_untracked_source_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    evidence = repo / "evidence"
    evidence.mkdir()
    (evidence / "readback.md").write_text("verified\n", encoding="utf-8")

    with patch("ouroboros.core.worktree._worktree_root", return_value=tmp_path / "worktrees"):
        workspace = prepare_task_workspace(repo, "orch_test", allow_untracked=True)

    try:
        assert (evidence / "readback.md").read_text(encoding="utf-8") == "verified\n"
        assert not (Path(workspace.worktree_path) / "evidence").exists()
    finally:
        release_lock(workspace.lock_path)


def test_prepare_task_workspace_rejects_tracked_changes_when_untracked_allowed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "product.py").write_text("VALUE = 2\n", encoding="utf-8")

    with (
        patch("ouroboros.core.worktree._worktree_root", return_value=tmp_path / "worktrees"),
        pytest.raises(WorktreeError, match="dirty checkout"),
    ):
        prepare_task_workspace(repo, "orch_test", allow_untracked=True)
