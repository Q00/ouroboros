"""Tests for persistent PM brownfield snapshot worktrees."""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

from ouroboros.core.pm_snapshot import refresh_pm_snapshot_worktrees


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _commit(repo, "a.txt", "v1\n", "initial")


def _make_remote_and_stale_clone(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create upstream repo, bare remote, and a local clone that goes stale.

    After cloning ``local``, the upstream pushes a second commit to the
    remote, so ``local`` is one commit behind ``origin/main``.
    """
    upstream = tmp_path / "upstream"
    _init_repo(upstream)

    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "--bare", str(upstream), str(remote))
    _git(upstream, "remote", "add", "origin", str(remote))

    local = tmp_path / "local"
    _git(tmp_path, "clone", str(remote), str(local))
    _git(local, "config", "user.email", "test@example.com")
    _git(local, "config", "user.name", "Test User")

    # Remote advances after the clone — local main is now stale.
    _commit(upstream, "a.txt", "v2\n", "second")
    _git(upstream, "push", "origin", "main")
    return upstream, remote, local


def _patched_env(tmp_path: Path):
    return (
        patch(
            "ouroboros.core.pm_snapshot.snapshot_root",
            return_value=tmp_path / "snapshots",
        ),
        patch("ouroboros.core.pm_snapshot.pm_snapshots_enabled", return_value=True),
    )


class TestRefreshPMSnapshotWorktrees:
    def test_disabled_returns_repos_unchanged(self, tmp_path: Path) -> None:
        repos = [{"path": str(tmp_path), "name": "x"}]
        with patch("ouroboros.core.pm_snapshot.pm_snapshots_enabled", return_value=False):
            assert refresh_pm_snapshot_worktrees(repos) == repos

    def test_non_git_path_falls_back_to_original(self, tmp_path: Path) -> None:
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        repos = [{"path": str(plain), "name": "plain"}]
        root_patch, enabled_patch = _patched_env(tmp_path)
        with root_patch, enabled_patch:
            result = refresh_pm_snapshot_worktrees(repos)
        assert result == repos

    def test_missing_path_key_is_passed_through(self, tmp_path: Path) -> None:
        repos = [{"name": "no-path"}]
        root_patch, enabled_patch = _patched_env(tmp_path)
        with root_patch, enabled_patch:
            assert refresh_pm_snapshot_worktrees(repos) == repos

    def test_creates_snapshot_pinned_to_remote_main(self, tmp_path: Path) -> None:
        """A stale local clone must snapshot origin/main, not local HEAD."""
        _, _, local = _make_remote_and_stale_clone(tmp_path)
        assert (local / "a.txt").read_text() == "v1\n"  # local is stale

        repos = [{"path": str(local), "name": "local", "role": "main"}]
        root_patch, enabled_patch = _patched_env(tmp_path)
        with root_patch, enabled_patch:
            result = refresh_pm_snapshot_worktrees(repos)

        entry = result[0]
        snapshot = Path(entry["path"])
        assert snapshot != local
        assert entry["source_path"] == str(local)
        assert entry["name"] == "local"
        assert entry["role"] == "main"
        # Snapshot carries the remote's newer commit …
        assert (snapshot / "a.txt").read_text() == "v2\n"
        # … while the developer's checkout stays untouched.
        assert (local / "a.txt").read_text() == "v1\n"

    def test_refresh_reuses_existing_worktree(self, tmp_path: Path) -> None:
        upstream, _, local = _make_remote_and_stale_clone(tmp_path)
        repos = [{"path": str(local), "name": "local"}]
        root_patch, enabled_patch = _patched_env(tmp_path)

        with root_patch, enabled_patch:
            first = refresh_pm_snapshot_worktrees(repos)
        snapshot = Path(first[0]["path"])
        assert (snapshot / "a.txt").read_text() == "v2\n"

        # Remote advances again; snapshot also accumulates local noise.
        _commit(upstream, "a.txt", "v3\n", "third")
        _git(upstream, "push", "origin", "main")
        (snapshot / "a.txt").write_text("dirty\n", encoding="utf-8")
        (snapshot / "untracked.txt").write_text("junk\n", encoding="utf-8")

        with root_patch, enabled_patch:
            second = refresh_pm_snapshot_worktrees(repos)

        assert Path(second[0]["path"]) == snapshot  # same worktree, no re-create
        assert (snapshot / "a.txt").read_text() == "v3\n"
        assert not (snapshot / "untracked.txt").exists()
        # Only one snapshot dir exists for the repo.
        entries = [p for p in (tmp_path / "snapshots").iterdir() if p.is_dir()]
        assert len(entries) == 1

    def test_repo_without_remote_snapshots_local_head(self, tmp_path: Path) -> None:
        repo = tmp_path / "standalone"
        _init_repo(repo)

        repos = [{"path": str(repo), "name": "standalone"}]
        root_patch, enabled_patch = _patched_env(tmp_path)
        with root_patch, enabled_patch:
            result = refresh_pm_snapshot_worktrees(repos)

        snapshot = Path(result[0]["path"])
        assert snapshot != repo
        assert (snapshot / "a.txt").read_text() == "v1\n"

    def test_stale_snapshot_dir_is_recreated(self, tmp_path: Path) -> None:
        """A leftover plain directory at the snapshot path is replaced."""
        _, _, local = _make_remote_and_stale_clone(tmp_path)
        repos = [{"path": str(local), "name": "local"}]
        root_patch, enabled_patch = _patched_env(tmp_path)

        with root_patch, enabled_patch:
            first = refresh_pm_snapshot_worktrees(repos)
        snapshot = Path(first[0]["path"])

        # Simulate external deletion that leaves a stale registration plus
        # a plain directory reappearing at the same path.
        _git(local, "worktree", "remove", "--force", str(snapshot))
        snapshot.mkdir()
        (snapshot / "leftover.txt").write_text("x\n", encoding="utf-8")

        with root_patch, enabled_patch:
            second = refresh_pm_snapshot_worktrees(repos)

        assert Path(second[0]["path"]) == snapshot
        assert (snapshot / "a.txt").read_text() == "v2\n"
        assert not (snapshot / "leftover.txt").exists()
