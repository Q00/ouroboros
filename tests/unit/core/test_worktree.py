"""Tests for task worktree management."""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from ouroboros.core.worktree import (
    TaskWorkspace,
    WorktreeError,
    _acquire_lock,
    _branch_exists,
    cleanup_task_workspace,
    maybe_prepare_task_workspace,
    maybe_restore_task_workspace,
    prepare_task_workspace,
    release_lock,
    release_task_workspace,
    restore_task_workspace,
)


def _workspace(path_root: Path) -> TaskWorkspace:
    return TaskWorkspace(
        durable_id="orch_test",
        repo_root=str(path_root / "repo"),
        repo_name="repo",
        original_cwd=str(path_root / "repo"),
        effective_cwd=str(path_root / "worktrees" / "repo" / "orch_test"),
        worktree_path=str(path_root / "worktrees" / "repo" / "orch_test"),
        branch="ooo/orch_test",
        lock_path=str(path_root / "worktrees" / ".locks" / "repo" / "orch_test.json"),
    )


class TestMaybePrepareTaskWorkspace:
    """Tests for config-gated workspace provisioning."""

    def test_returns_none_when_worktrees_disabled(self, tmp_path: Path) -> None:
        with (
            patch("ouroboros.core.worktree._worktrees_enabled", return_value=False),
            patch("ouroboros.core.worktree.prepare_task_workspace") as prepare_mock,
        ):
            result = maybe_prepare_task_workspace(tmp_path, "orch_test")

        assert result is None
        prepare_mock.assert_not_called()

    def test_returns_none_when_source_cwd_is_not_git_repo(self, tmp_path: Path) -> None:
        with (
            patch("ouroboros.core.worktree._worktrees_enabled", return_value=True),
            patch("ouroboros.core.worktree._try_resolve_repo_root", return_value=None),
            patch("ouroboros.core.worktree.prepare_task_workspace") as prepare_mock,
        ):
            result = maybe_prepare_task_workspace(tmp_path, "orch_test")

        assert result is None
        prepare_mock.assert_not_called()

    def test_allows_dirty_delegated_parent_workspace(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        prepared_workspace = _workspace(tmp_path)
        with (
            patch("ouroboros.core.worktree._worktrees_enabled", return_value=True),
            patch("ouroboros.core.worktree._try_resolve_repo_root", return_value=repo_root),
            patch(
                "ouroboros.core.worktree.prepare_task_workspace",
                return_value=prepared_workspace,
            ) as prepare_mock,
        ):
            result = maybe_prepare_task_workspace(tmp_path, "orch_test", allow_dirty=True)

        assert result == prepared_workspace
        prepare_mock.assert_called_once_with(tmp_path, "orch_test", allow_dirty=True)


class TestMaybeRestoreTaskWorkspace:
    """Tests for config-gated workspace restoration."""

    def test_returns_none_for_new_workspace_when_disabled(self, tmp_path: Path) -> None:
        with (
            patch("ouroboros.core.worktree._worktrees_enabled", return_value=False),
            patch("ouroboros.core.worktree.restore_task_workspace") as restore_mock,
        ):
            result = maybe_restore_task_workspace(
                "orch_test",
                persisted=None,
                fallback_source_cwd=tmp_path,
            )

        assert result is None
        restore_mock.assert_not_called()

    def test_returns_none_for_new_workspace_when_source_cwd_is_not_git_repo(
        self, tmp_path: Path
    ) -> None:
        with (
            patch("ouroboros.core.worktree._worktrees_enabled", return_value=True),
            patch("ouroboros.core.worktree._try_resolve_repo_root", return_value=None),
            patch("ouroboros.core.worktree.restore_task_workspace") as restore_mock,
        ):
            result = maybe_restore_task_workspace(
                "orch_test",
                persisted=None,
                fallback_source_cwd=tmp_path,
            )

        assert result is None
        restore_mock.assert_not_called()

    def test_restores_persisted_workspace_even_when_disabled(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path)
        worktree_path = Path(workspace.worktree_path)
        worktree_path.mkdir(parents=True)
        lock_owner = {"pid": 1234}

        with (
            patch("ouroboros.core.worktree._worktrees_enabled", return_value=False),
            patch("ouroboros.core.worktree._acquire_lock", return_value=lock_owner) as acquire_mock,
        ):
            restored = maybe_restore_task_workspace(
                workspace.durable_id,
                persisted=workspace,
                fallback_source_cwd=tmp_path,
            )

        assert restored is not None
        assert restored.worktree_path == workspace.worktree_path
        assert restored.lock_owner == lock_owner
        acquire_mock.assert_called_once()


class TestRestoreTaskWorkspace:
    """Tests for restore_task_workspace fallback behavior."""

    def test_scan_fallback_uses_common_repo_root(self, tmp_path: Path) -> None:
        worktree_root = tmp_path / "worktrees"
        worktree_path = worktree_root / "repo" / "orch_test"
        source_repo = tmp_path / "source" / "repo"
        source_dir = source_repo / "src"

        worktree_path.mkdir(parents=True)
        source_dir.mkdir(parents=True)

        lock_owner = {"pid": 4321}

        with (
            patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root),
            patch("ouroboros.core.worktree._resolve_common_repo_root", return_value=source_repo),
            patch("ouroboros.core.worktree._resolve_repo_root", return_value=source_repo),
            patch("ouroboros.core.worktree._acquire_lock", return_value=lock_owner),
        ):
            restored = restore_task_workspace(
                "orch_test",
                persisted=None,
                fallback_source_cwd=source_dir,
            )

        assert restored.repo_root == str(source_repo)
        assert restored.effective_cwd == str(worktree_path / "src")
        assert restored.lock_owner == lock_owner

    def test_scan_fallback_chooses_match_for_callers_repo(self, tmp_path: Path) -> None:
        worktree_root = tmp_path / "worktrees"
        foreign_worktree = worktree_root / "repo-b" / "orch_test"
        caller_worktree = worktree_root / "repo-a" / "orch_test"
        caller_repo = tmp_path / "repos" / "repo-a"
        foreign_repo = tmp_path / "repos" / "repo-b"
        source_dir = caller_repo / "src"

        foreign_worktree.mkdir(parents=True)
        caller_worktree.mkdir(parents=True)
        source_dir.mkdir(parents=True)

        lock_owner = {"pid": 4321}

        def fake_common_repo_root(path: Path) -> Path:
            resolved = path.resolve()
            if resolved == caller_worktree.resolve():
                return caller_repo
            if resolved == foreign_worktree.resolve():
                return foreign_repo
            raise AssertionError(f"unexpected path: {path}")

        with (
            patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root),
            patch(
                "ouroboros.core.worktree._resolve_common_repo_root",
                side_effect=fake_common_repo_root,
            ),
            patch("ouroboros.core.worktree._resolve_repo_root", return_value=caller_repo),
            patch("ouroboros.core.worktree._acquire_lock", return_value=lock_owner),
        ):
            restored = restore_task_workspace(
                "orch_test",
                persisted=None,
                fallback_source_cwd=source_dir,
            )

        assert restored.repo_root == str(caller_repo)
        assert restored.worktree_path == str(caller_worktree)
        assert restored.effective_cwd == str(caller_worktree / "src")
        assert restored.lock_owner == lock_owner

    def test_scan_fallback_ignores_foreign_repo_and_prepares_new_workspace(
        self, tmp_path: Path
    ) -> None:
        worktree_root = tmp_path / "worktrees"
        foreign_worktree = worktree_root / "repo-b" / "orch_test"
        caller_repo = tmp_path / "repos" / "repo-a"
        foreign_repo = tmp_path / "repos" / "repo-b"
        source_dir = caller_repo / "src"

        foreign_worktree.mkdir(parents=True)
        source_dir.mkdir(parents=True)
        prepared_workspace = _workspace(tmp_path)

        with (
            patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root),
            patch("ouroboros.core.worktree._resolve_common_repo_root", return_value=foreign_repo),
            patch("ouroboros.core.worktree._resolve_repo_root", return_value=caller_repo),
            patch(
                "ouroboros.core.worktree.prepare_task_workspace",
                return_value=prepared_workspace,
            ) as prepare_mock,
        ):
            restored = restore_task_workspace(
                "orch_test",
                persisted=None,
                fallback_source_cwd=source_dir,
            )

        assert restored == prepared_workspace
        prepare_mock.assert_called_once_with(source_dir, "orch_test", allow_dirty=False)

    def test_scan_fallback_raises_for_multiple_matches_in_same_repo(self, tmp_path: Path) -> None:
        worktree_root = tmp_path / "worktrees"
        first_worktree = worktree_root / "repo-a" / "orch_test"
        second_worktree = worktree_root / "repo-b" / "orch_test"
        caller_repo = tmp_path / "repos" / "repo-a"
        source_dir = caller_repo / "src"

        first_worktree.mkdir(parents=True)
        second_worktree.mkdir(parents=True)
        source_dir.mkdir(parents=True)

        with (
            patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root),
            patch("ouroboros.core.worktree._resolve_common_repo_root", return_value=caller_repo),
            patch("ouroboros.core.worktree._resolve_repo_root", return_value=caller_repo),
        ):
            with patch("ouroboros.core.worktree.prepare_task_workspace") as prepare_mock:
                with pytest.raises(WorktreeError, match="Multiple managed worktrees"):
                    restore_task_workspace(
                        "orch_test",
                        persisted=None,
                        fallback_source_cwd=source_dir,
                    )

        prepare_mock.assert_not_called()


class TestWorktreeHardening:
    """Tests for malformed lock and invalid durable-id handling."""

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    @classmethod
    def _init_repo(cls, repo: Path) -> None:
        repo.mkdir()
        cls._git(repo, "init", "-b", "main")
        cls._git(repo, "config", "user.email", "test@example.com")
        cls._git(repo, "config", "user.name", "Test User")
        (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
        cls._git(repo, "add", "tracked.txt")
        cls._git(repo, "commit", "-m", "initial")

    def test_acquire_lock_raises_worktree_error_for_malformed_lock_file(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path)
        lock_path = Path(workspace.lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("{not-json")

        with pytest.raises(WorktreeError, match="Invalid task workspace lock file"):
            _acquire_lock(lock_path, workspace)

    def test_branch_exists_normalizes_git_invocation_failures(self, tmp_path: Path) -> None:
        with patch(
            "ouroboros.core.worktree.subprocess.run",
            side_effect=OSError("spawn failed"),
        ):
            with pytest.raises(WorktreeError, match="Git command failed"):
                _branch_exists(tmp_path, "ooo/orch_test")

    def test_prepare_task_workspace_rejects_invalid_durable_id(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        invalid_branch = subprocess.CompletedProcess(
            args=["git", "check-ref-format"],
            returncode=1,
            stdout="",
            stderr="invalid branch name",
        )

        with (
            patch("ouroboros.core.worktree._resolve_repo_root", return_value=repo_root),
            patch("ouroboros.core.worktree._ensure_clean_checkout"),
            patch("ouroboros.core.worktree._run_git_process", return_value=invalid_branch),
        ):
            with pytest.raises(WorktreeError, match="Invalid durable task identifier"):
                prepare_task_workspace(repo_root, "bad id")

    def test_prepare_task_workspace_explains_empty_repository_recovery(
        self,
        tmp_path: Path,
    ) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        repo_root.mkdir()
        self._git(repo_root, "init", "-b", "main")

        with patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root):
            with pytest.raises(WorktreeError, match="Create an initial commit first") as exc_info:
                prepare_task_workspace(repo_root, "orch_test_empty")

        assert "git commit --allow-empty" in exc_info.value.message

    def test_maybe_prepare_creates_worktree_from_dirty_source_when_allowed(
        self, tmp_path: Path
    ) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        self._init_repo(repo_root)
        (repo_root / "dirty.txt").write_text("uncommitted caller work\n", encoding="utf-8")

        with (
            patch("ouroboros.core.worktree._worktrees_enabled", return_value=True),
            patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root),
        ):
            workspace = maybe_prepare_task_workspace(
                repo_root,
                "orch_test_dirty",
                allow_dirty=True,
            )

        try:
            assert workspace is not None
            assert workspace.worktree_path != str(repo_root)
            assert Path(workspace.worktree_path).is_dir()
            assert workspace.effective_cwd == workspace.worktree_path
            assert not (Path(workspace.worktree_path) / "dirty.txt").exists()
            assert (repo_root / "dirty.txt").read_text(encoding="utf-8") == (
                "uncommitted caller work\n"
            )
        finally:
            if workspace is not None:
                release_lock(workspace.lock_path)

    def test_prune_merged_policy_removes_clean_worktree_branch_and_lock(
        self, tmp_path: Path
    ) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        self._init_repo(repo_root)

        with (
            patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root),
            patch("ouroboros.core.worktree._worktree_cleanup_policy", return_value="prune-merged"),
        ):
            workspace = prepare_task_workspace(repo_root, "orch_test_cleanup")
            release_task_workspace(workspace)

        assert not Path(workspace.worktree_path).exists()
        assert not Path(workspace.lock_path).exists()
        assert not _branch_exists(repo_root, workspace.branch)

    def test_cleanup_prunes_stale_registration_when_directory_deleted_externally(
        self, tmp_path: Path
    ) -> None:
        import shutil as _shutil

        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        self._init_repo(repo_root)

        with patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root):
            workspace = prepare_task_workspace(repo_root, "orch_test_stale_reg")
            release_lock(workspace.lock_path)
            # Simulate external deletion: directory gone, .git/worktrees
            # registration (and branch lock) left behind.
            _shutil.rmtree(workspace.worktree_path)

            removed = cleanup_task_workspace(workspace, policy="prune-merged")

        assert removed is True
        # Stale registration dropped and merged branch deleted despite the
        # leftover .git/worktrees metadata.
        assert workspace.worktree_path not in self._git(
            repo_root, "worktree", "list", "--porcelain"
        )
        assert not _branch_exists(repo_root, workspace.branch)

    def test_prune_merged_policy_keeps_unmerged_worktree_branch_but_releases_lock(
        self, tmp_path: Path
    ) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        self._init_repo(repo_root)

        with (
            patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root),
            patch("ouroboros.core.worktree._worktree_cleanup_policy", return_value="prune-merged"),
        ):
            workspace = prepare_task_workspace(repo_root, "orch_test_unmerged")
            worktree_path = Path(workspace.worktree_path)
            (worktree_path / "feature.txt").write_text("feature\n", encoding="utf-8")
            self._git(worktree_path, "add", "feature.txt")
            self._git(worktree_path, "commit", "-m", "feature")

            release_task_workspace(workspace)

        assert Path(workspace.worktree_path).exists()
        assert not Path(workspace.lock_path).exists()
        assert _branch_exists(repo_root, workspace.branch)

    def test_remove_policy_removes_clean_unmerged_worktree_but_keeps_branch(
        self, tmp_path: Path
    ) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        self._init_repo(repo_root)

        with patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root):
            workspace = prepare_task_workspace(repo_root, "orch_test_remove")
            worktree_path = Path(workspace.worktree_path)
            (worktree_path / "feature.txt").write_text("feature\n", encoding="utf-8")
            self._git(worktree_path, "add", "feature.txt")
            self._git(worktree_path, "commit", "-m", "feature")

            removed = cleanup_task_workspace(workspace, policy="remove")
            release_lock(workspace.lock_path)

        assert removed is True
        assert not Path(workspace.worktree_path).exists()
        assert _branch_exists(repo_root, workspace.branch)

    def test_remove_policy_keeps_dirty_worktree_but_releases_lock(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        self._init_repo(repo_root)

        with (
            patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root),
            patch("ouroboros.core.worktree._worktree_cleanup_policy", return_value="remove"),
        ):
            workspace = prepare_task_workspace(repo_root, "orch_test_dirty_cleanup")
            worktree_path = Path(workspace.worktree_path)
            (worktree_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            release_task_workspace(workspace)

        assert Path(workspace.worktree_path).exists()
        assert not Path(workspace.lock_path).exists()
        assert _branch_exists(repo_root, workspace.branch)
