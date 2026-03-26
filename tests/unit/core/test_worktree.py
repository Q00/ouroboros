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
    maybe_prepare_task_workspace,
    maybe_restore_task_workspace,
    prepare_task_workspace,
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

    def test_returns_none_for_dirty_delegated_parent_workspace(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        with (
            patch("ouroboros.core.worktree._worktrees_enabled", return_value=True),
            patch("ouroboros.core.worktree._try_resolve_repo_root", return_value=repo_root),
            patch("ouroboros.core.worktree._checkout_is_dirty", return_value=True),
            patch("ouroboros.core.worktree.prepare_task_workspace") as prepare_mock,
        ):
            result = maybe_prepare_task_workspace(tmp_path, "orch_test", allow_dirty=True)

        assert result is None
        prepare_mock.assert_not_called()


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
