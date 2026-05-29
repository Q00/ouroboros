from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from ouroboros.core.lineage import ACResult, EvaluationSummary
from ouroboros.mcp.tools.evolution_handlers import (
    _checkpoint_passed_generation_acs,
    _resolve_checkpoint_working_dir,
)


@dataclass
class _FakeWorkspace:
    effective_cwd: str


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
    (path / "README.md").write_text("demo\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def test_evolve_checkpoint_commits_only_newly_passed_acs(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    summary = EvaluationSummary(
        final_approved=False,
        highest_stage_passed=3,
        ac_results=(
            ACResult(ac_index=0, ac_content="Command prints stable output", passed=True),
            ACResult(ac_index=1, ac_content="Docs are updated", passed=False),
        ),
    )

    commits, attempts = _checkpoint_passed_generation_acs(
        {
            "commit_policy": "ac_checkpoint",
            "auto_session_id": "auto_test123",
            "execution_id": "exec_123",
        },
        summary,
        repo,
    )
    repeated_commits, repeated_attempts = _checkpoint_passed_generation_acs(
        {
            "commit_policy": "ac_checkpoint",
            "auto_session_id": "auto_test123",
            "checkpoint_commits": commits,
            "checkpoint_attempted_ac_ids": attempts,
        },
        summary,
        repo,
    )

    assert len(commits) == 1
    assert commits[0]["ac_id"] == "AC-1"
    assert attempts == ["AC-1"]
    assert repeated_commits == commits
    assert repeated_attempts == attempts
    log = _git(repo, "log", "-1", "--pretty=%B")
    assert "Acceptance-Criterion: AC-1" in log
    assert "Execution-Id: exec_123" in log


def test_evolve_checkpoint_does_not_retry_attempted_pass_without_diff(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    summary = EvaluationSummary(
        final_approved=True,
        highest_stage_passed=3,
        ac_results=(ACResult(ac_index=0, ac_content="Command prints stable output", passed=True),),
    )

    commits, attempts = _checkpoint_passed_generation_acs(
        {
            "commit_policy": "ac_checkpoint",
            "auto_session_id": "auto_test123",
        },
        summary,
        repo,
    )
    (repo / "feature.py").write_text("print('later')\n", encoding="utf-8")
    repeated_commits, repeated_attempts = _checkpoint_passed_generation_acs(
        {
            "commit_policy": "ac_checkpoint",
            "auto_session_id": "auto_test123",
            "checkpoint_commits": commits,
            "checkpoint_attempted_ac_ids": attempts,
        },
        summary,
        repo,
    )

    assert commits == []
    assert attempts == ["AC-1"]
    assert repeated_commits == []
    assert repeated_attempts == ["AC-1"]
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"


def test_resolve_checkpoint_working_dir_prefers_effective_worktree(tmp_path) -> None:
    """#1281 review blocker 1: when a managed lineage worktree is active, the
    checkpoint must target the worktree that execution mutated — not the
    original project_dir preferred by verification-dir resolution.
    """
    project_dir = tmp_path / "project"
    worktree = tmp_path / "worktree"

    assert _resolve_checkpoint_working_dir(None, project_dir) == project_dir
    assert (
        _resolve_checkpoint_working_dir(_FakeWorkspace(effective_cwd=str(worktree)), project_dir)
        == worktree
    )


def test_evolve_checkpoint_commits_target_worktree_not_parent_project(tmp_path) -> None:
    """The passed AC's diff lives in the lineage worktree; committing the parent
    project_dir (no diff) would burn the attempt as ``no_safe_changes`` and lose
    the checkpoint. Resolving to the effective worktree commits it correctly and
    leaves the parent checkout untouched.
    """
    project_dir = tmp_path / "project"
    _init_repo(project_dir)
    worktree = tmp_path / "worktree"
    _init_repo(worktree)
    # The generation's mutation only exists in the worktree.
    (worktree / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    summary = EvaluationSummary(
        final_approved=True,
        highest_stage_passed=3,
        ac_results=(ACResult(ac_index=0, ac_content="Command prints stable output", passed=True),),
    )

    checkpoint_dir = _resolve_checkpoint_working_dir(
        _FakeWorkspace(effective_cwd=str(worktree)),
        Path(project_dir),
    )
    commits, attempts = _checkpoint_passed_generation_acs(
        {"commit_policy": "ac_checkpoint", "auto_session_id": "auto_test123"},
        summary,
        checkpoint_dir,
    )

    assert len(commits) == 1
    assert commits[0]["ac_id"] == "AC-1"
    # Commit landed in the worktree, not the parent project checkout.
    assert _git(worktree, "rev-list", "--count", "HEAD") == "2"
    assert _git(project_dir, "rev-list", "--count", "HEAD") == "1"
