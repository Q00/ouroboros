"""The evaluation -> Ralph handoff builds the next generation on this one's work.

v0.55.0 dev run: after a generation changed files in its managed task
worktree, the chained evolve step bootstrapped the lineage worktree from it
and refused it as a dirty checkout, so evolve never ran. The handoff now
records the generation's changes as a checkpoint commit on the task branch
(``checkpoint_managed_worktree``) and the lineage worktree starts from it. A
checkout Ouroboros does not manage is never committed.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from ouroboros.core.worktree import (
    WorktreeError,
    checkpoint_managed_worktree,
    maybe_restore_task_workspace,
    prepare_task_workspace,
    release_lock,
)


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


@pytest.fixture
def worktrees(tmp_path: Path):
    root = tmp_path / "worktrees"
    with (
        patch("ouroboros.core.worktree._worktree_root", return_value=root),
        patch("ouroboros.core.worktree._worktrees_enabled", return_value=True),
    ):
        yield root


def test_the_next_generation_starts_from_this_generations_work(
    tmp_path: Path, worktrees: Path
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    generation = prepare_task_workspace(repo, "orch_gen1")
    try:
        gen_dir = Path(generation.worktree_path)
        (gen_dir / "product.py").write_text("VALUE = 2\n", encoding="utf-8")
        (gen_dir / "added.py").write_text("NEW = True\n", encoding="utf-8")

        # Before the handoff records the work, the lineage cannot start from it.
        with pytest.raises(WorktreeError, match="dirty checkout"):
            maybe_restore_task_workspace(
                "lineage_a",
                None,
                fallback_source_cwd=gen_dir,
                allow_untracked_evidence=True,
            )

        commit = checkpoint_managed_worktree(gen_dir, message="ooo: generation checkpoint")
        assert commit is not None
        assert _git(gen_dir, "status", "--porcelain") == ""

        lineage = maybe_restore_task_workspace(
            "lineage_a",
            None,
            fallback_source_cwd=gen_dir,
            allow_untracked_evidence=True,
        )
        assert lineage is not None
        try:
            lineage_dir = Path(lineage.worktree_path)
            assert (lineage_dir / "product.py").read_text(encoding="utf-8") == "VALUE = 2\n"
            assert (lineage_dir / "added.py").exists()
            assert _git(lineage_dir, "rev-parse", "HEAD") == commit
        finally:
            release_lock(lineage.lock_path)
    finally:
        release_lock(generation.lock_path)
    # The user's checkout is unchanged: no commit, no change.
    assert _git(repo, "log", "--format=%s") == "initial"
    assert (repo / "product.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_a_checkout_ouroboros_does_not_manage_is_never_committed(
    tmp_path: Path, worktrees: Path
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "product.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert checkpoint_managed_worktree(repo, message="ooo: generation checkpoint") is None
    assert _git(repo, "log", "--format=%s") == "initial"
    assert _git(repo, "status", "--porcelain") == "M product.py"


def test_a_clean_managed_worktree_needs_no_checkpoint(tmp_path: Path, worktrees: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    generation = prepare_task_workspace(repo, "orch_clean")
    try:
        assert (
            checkpoint_managed_worktree(generation.worktree_path, message="ooo: checkpoint") is None
        )
    finally:
        release_lock(generation.lock_path)


def test_a_path_outside_any_repository_is_left_alone(tmp_path: Path, worktrees: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert checkpoint_managed_worktree(plain, message="ooo: checkpoint") is None
