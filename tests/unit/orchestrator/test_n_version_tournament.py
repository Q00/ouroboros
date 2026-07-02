"""Tests for N-version tournament scaffolding (PR-X X3, not live-wired)."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from ouroboros.orchestrator import n_version_tournament as nvt
from ouroboros.orchestrator.n_version_tournament import TournamentEntry


class TestPlanTournament:
    def test_picks_up_to_max_distinct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = ["codex", "gemini", "opencode"]

        def _fake_pick(failed: str, *, exclude=None, weights=None):  # type: ignore[no-untyped-def]
            for name in pool:
                if name not in (exclude or set()):
                    return name
            return None

        monkeypatch.setattr(nvt, "pick_alternative_runtime", _fake_pick)
        contestants = nvt.plan_tournament("claude", max_contestants=2)
        assert contestants == ("codex", "gemini")
        assert len(set(contestants)) == 2  # distinct

    def test_stops_when_pool_exhausted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def _fake_pick(failed: str, *, exclude=None, weights=None):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return "codex" if calls["n"] == 1 else None

        monkeypatch.setattr(nvt, "pick_alternative_runtime", _fake_pick)
        assert nvt.plan_tournament("claude", max_contestants=3) == ("codex",)

    def test_zero_max_is_empty(self) -> None:
        assert nvt.plan_tournament("claude", max_contestants=0) == ()


class TestWinnerSelection:
    def test_first_passing_wins(self) -> None:
        entries = [
            TournamentEntry(backend="codex", passed=False),
            TournamentEntry(backend="gemini", passed=True),
            TournamentEntry(backend="opencode", passed=True),
        ]
        winner = nvt.select_tournament_winner(entries)
        assert winner is not None
        assert winner.backend == "gemini"

    def test_no_passing_returns_none(self) -> None:
        entries = [
            TournamentEntry(backend="codex", passed=False),
            TournamentEntry(backend="gemini", passed=False),
        ]
        assert nvt.select_tournament_winner(entries) is None

    def test_empty_returns_none(self) -> None:
        assert nvt.select_tournament_winner([]) is None


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo


class TestWorktreeIsolation:
    def test_contestants_get_isolated_worktrees(self, git_workspace: Path) -> None:
        with nvt.RunWorktreeManager(git_workspace) as manager:
            wt_a = manager.create("codex")
            wt_b = manager.create("gemini")
            # Distinct, real, and separate from the main workspace.
            assert wt_a != wt_b
            assert wt_a.exists() and wt_b.exists()
            assert wt_a.resolve() != git_workspace.resolve()
            # A dirty edit in one contestant never touches the other or main.
            (wt_a / "file.txt").write_text("codex-change\n")
            assert (wt_b / "file.txt").read_text() == "base\n"
            assert (git_workspace / "file.txt").read_text() == "base\n"
        # Cleanup removed the worktrees.
        assert not wt_a.exists()
        assert not wt_b.exists()

    def test_winner_diff_applies_to_workspace(self, git_workspace: Path) -> None:
        with nvt.RunWorktreeManager(git_workspace) as manager:
            winner = manager.create("codex")
            (winner / "file.txt").write_text("winning change\n")
            diff = nvt.export_worktree_diff(winner)
            assert "winning change" in diff
            applied = nvt.apply_diff_to_workspace(git_workspace, diff)
            assert applied is True
        assert (git_workspace / "file.txt").read_text() == "winning change\n"

    def test_empty_diff_is_noop_success(self, git_workspace: Path) -> None:
        assert nvt.apply_diff_to_workspace(git_workspace, "") is True
