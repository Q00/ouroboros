"""Tests for the ``ouroboros cleanup`` command (issue #1560)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from typer.testing import CliRunner

from ouroboros.cli.main import app
from ouroboros.core.worktree import (
    discover_managed_workspaces,
    lock_file_is_stale,
    prepare_task_workspace,
    release_lock,
)

runner = CliRunner()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")


def _make_auto_workspace(repo_root: Path, worktree_root: Path, session_id: str):
    with patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root):
        workspace = prepare_task_workspace(repo_root, session_id)
    release_lock(workspace.lock_path, workspace.lock_owner)
    return workspace


class TestDiscoverManagedWorkspaces:
    def test_discovers_leftover_auto_worktree(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        _init_repo(repo_root)
        workspace = _make_auto_workspace(repo_root, worktree_root, "auto_abc123def456")

        found = discover_managed_workspaces(worktree_root)

        assert len(found) == 1
        assert found[0].durable_id == "auto_abc123def456"
        assert found[0].branch == workspace.branch
        assert Path(found[0].repo_root) == repo_root.resolve()

    def test_skips_locks_dir_and_missing_root(self, tmp_path: Path) -> None:
        assert discover_managed_workspaces(tmp_path / "nope") == []
        root = tmp_path / "worktrees"
        (root / ".locks" / "repo").mkdir(parents=True)
        assert discover_managed_workspaces(root) == []


class TestLockFileIsStale:
    def test_missing_lock_is_stale_but_corrupt_lock_is_preserved(self, tmp_path: Path) -> None:
        assert lock_file_is_stale(tmp_path / "missing.json")
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{not json", encoding="utf-8")
        assert not lock_file_is_stale(corrupt)

    def test_live_lock_is_not_stale(self, tmp_path: Path) -> None:
        import os
        import socket

        lock = tmp_path / "live.json"
        lock.write_text(
            json.dumps({"pid": os.getpid(), "host": socket.gethostname()}),
            encoding="utf-8",
        )
        assert not lock_file_is_stale(lock)

    def test_old_remote_lock_is_not_proof_of_dead_owner(self, tmp_path: Path) -> None:
        lock = tmp_path / "remote.json"
        lock.write_text(
            json.dumps(
                {"pid": 123, "host": "remote-host", "updated_at": "2000-01-01T00:00:00+00:00"}
            ),
            encoding="utf-8",
        )
        assert not lock_file_is_stale(lock)


class TestCleanupCommand:
    def _run(self, worktree_root: Path, data_root: Path, *args: str):
        with (
            patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root),
            patch(
                "ouroboros.cli.commands.cleanup.managed_worktree_root",
                return_value=worktree_root,
            ),
            patch(
                "ouroboros.cli.commands.cleanup._auto_data_root",
                return_value=data_root,
            ),
        ):
            return runner.invoke(app, ["cleanup", *args])

    def test_removes_merged_clean_worktree_and_branch(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        _init_repo(repo_root)
        workspace = _make_auto_workspace(repo_root, worktree_root, "auto_merged111111")

        result = self._run(worktree_root, tmp_path / "data")

        assert result.exit_code == 0
        assert not Path(workspace.worktree_path).exists()
        assert workspace.branch not in _git(repo_root, "branch", "--list", workspace.branch)

    def test_dry_run_removes_nothing(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        _init_repo(repo_root)
        workspace = _make_auto_workspace(repo_root, worktree_root, "auto_dryrun111111")

        result = self._run(worktree_root, tmp_path / "data", "--dry-run")

        assert result.exit_code == 0
        assert "Would remove" in result.output
        assert Path(workspace.worktree_path).exists()

    def test_keeps_unmerged_worktree_unless_forced(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        _init_repo(repo_root)
        workspace = _make_auto_workspace(repo_root, worktree_root, "auto_unmerged1111")
        worktree_path = Path(workspace.worktree_path)
        (worktree_path / "feature.txt").write_text("feature\n", encoding="utf-8")
        _git(worktree_path, "add", "feature.txt")
        _git(worktree_path, "commit", "-m", "feature")

        result = self._run(worktree_root, tmp_path / "data")
        assert result.exit_code == 0
        assert worktree_path.exists()

        result = self._run(worktree_root, tmp_path / "data", "--force")
        assert result.exit_code == 0
        assert not worktree_path.exists()
        # Unmerged branch survives even a forced worktree removal.
        assert workspace.branch in _git(repo_root, "branch", "--list", workspace.branch)

    def test_never_touches_dirty_worktree(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        _init_repo(repo_root)
        workspace = _make_auto_workspace(repo_root, worktree_root, "auto_dirty1111111")
        worktree_path = Path(workspace.worktree_path)
        (worktree_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        result = self._run(worktree_root, tmp_path / "data", "--force")

        assert result.exit_code == 0
        assert worktree_path.exists()

    def test_force_preserves_workspace_with_unreadable_owner(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        _init_repo(repo_root)
        workspace = _make_auto_workspace(repo_root, worktree_root, "auto_unknown11111")
        lock_path = Path(workspace.lock_path)
        lock_path.write_text("{invalid", encoding="utf-8")

        result = self._run(worktree_root, tmp_path / "data", "--force")

        assert result.exit_code == 0
        assert Path(workspace.worktree_path).exists()
        assert lock_path.read_text(encoding="utf-8") == "{invalid"

    def test_restore_waits_for_cleanup_then_recreates_workspace(self, tmp_path: Path) -> None:
        import threading

        from ouroboros.cli.commands.cleanup import _prune_workspaces
        from ouroboros.core.worktree import cleanup_task_workspace, restore_task_workspace

        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        _init_repo(repo_root)
        workspace = _make_auto_workspace(repo_root, worktree_root, "auto_concurrent11")
        entered = threading.Event()
        proceed = threading.Event()
        restored: list = []
        errors: list[BaseException] = []

        def paused_cleanup(*args, **kwargs):
            entered.set()
            assert proceed.wait(5)
            return cleanup_task_workspace(*args, **kwargs)

        def prune() -> None:
            try:
                _prune_workspaces(policy="remove", dry_run=False)
            except BaseException as exc:
                errors.append(exc)

        def restore() -> None:
            try:
                restored.append(restore_task_workspace(workspace.durable_id, workspace))
            except BaseException as exc:
                errors.append(exc)

        with (
            patch(
                "ouroboros.cli.commands.cleanup.discover_managed_workspaces",
                return_value=[workspace],
            ),
            patch(
                "ouroboros.cli.commands.cleanup.cleanup_task_workspace", side_effect=paused_cleanup
            ),
        ):
            pruning = threading.Thread(target=prune)
            pruning.start()
            assert entered.wait(5)
            restoring = threading.Thread(target=restore)
            restoring.start()
            try:
                restoring.join(0.1)
                assert restoring.is_alive()
            finally:
                proceed.set()
                pruning.join(10)
                restoring.join(10)
        assert not pruning.is_alive() and not restoring.is_alive()
        assert not errors
        assert len(restored) == 1
        assert Path(workspace.worktree_path).exists()
        assert Path(workspace.lock_path).exists()
        release_lock(workspace.lock_path, workspace.lock_owner)

    def test_cleanup_preserves_workspace_while_prepare_is_in_progress(self, tmp_path: Path) -> None:
        import threading

        from ouroboros.cli.commands.cleanup import _prune_workspaces
        from ouroboros.core.worktree import _ensure_clean_checkout

        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        _init_repo(repo_root)
        workspace = _make_auto_workspace(repo_root, worktree_root, "auto_preparing11")
        entered = threading.Event()
        proceed = threading.Event()
        errors: list[BaseException] = []

        def paused_check(path):
            entered.set()
            assert proceed.wait(5)
            _ensure_clean_checkout(path)

        def prepare() -> None:
            try:
                prepare_task_workspace(repo_root, workspace.durable_id)
            except BaseException as exc:
                errors.append(exc)

        with (
            patch("ouroboros.core.worktree._worktree_root", return_value=worktree_root),
            patch("ouroboros.core.worktree._ensure_clean_checkout", side_effect=paused_check),
            patch(
                "ouroboros.cli.commands.cleanup.discover_managed_workspaces",
                return_value=[workspace],
            ),
        ):
            preparing = threading.Thread(target=prepare)
            preparing.start()
            assert entered.wait(5)
            try:
                removed, skipped, failed = _prune_workspaces(policy="remove", dry_run=False)
                assert not removed and skipped == [workspace] and not failed
                assert Path(workspace.worktree_path).exists()
            finally:
                proceed.set()
                preparing.join(10)
        assert not preparing.is_alive()
        assert not errors
        release_lock(workspace.lock_path, workspace.lock_owner)

    def test_prunes_stale_lock_without_worktree(self, tmp_path: Path) -> None:
        import socket

        worktree_root = tmp_path / "worktrees"
        lock_dir = worktree_root / ".locks" / "repo"
        lock_dir.mkdir(parents=True)
        stale_lock = lock_dir / "auto_gone11111111.json"
        stale_lock.write_text(
            json.dumps({"pid": 999999999, "host": socket.gethostname()}),
            encoding="utf-8",
        )

        result = self._run(worktree_root, tmp_path / "data")

        assert result.exit_code == 0
        assert not stale_lock.exists()

    def test_prunes_completed_state_without_worktree(self, tmp_path: Path) -> None:
        worktree_root = tmp_path / "worktrees"
        worktree_root.mkdir()
        data_root = tmp_path / "data"
        data_root.mkdir()
        complete = data_root / "auto_done11111111.json"
        complete.write_text(json.dumps({"phase": "complete"}), encoding="utf-8")
        blocked = data_root / "auto_blocked111111.json"
        blocked.write_text(json.dumps({"phase": "blocked"}), encoding="utf-8")
        running = data_root / "auto_running111111.json"
        running.write_text(json.dumps({"phase": "executing"}), encoding="utf-8")

        result = self._run(worktree_root, data_root)
        assert result.exit_code == 0
        assert not complete.exists()
        assert blocked.exists()
        assert running.exists()

        result = self._run(worktree_root, data_root, "--state-all")
        assert result.exit_code == 0
        assert not blocked.exists()
        assert running.exists()

    def test_keeps_state_of_session_with_live_worktree(self, tmp_path: Path) -> None:
        repo_root = tmp_path / "repo"
        worktree_root = tmp_path / "worktrees"
        _init_repo(repo_root)
        workspace = _make_auto_workspace(repo_root, worktree_root, "auto_live11111111")
        worktree_path = Path(workspace.worktree_path)
        (worktree_path / "wip.txt").write_text("wip\n", encoding="utf-8")  # dirty → kept

        data_root = tmp_path / "data"
        data_root.mkdir()
        state = data_root / "auto_live11111111.json"
        state.write_text(json.dumps({"phase": "complete"}), encoding="utf-8")

        result = self._run(worktree_root, data_root)

        assert result.exit_code == 0
        assert state.exists()
