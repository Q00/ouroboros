from __future__ import annotations

import subprocess

from ouroboros.core.lineage import ACResult, EvaluationSummary
from ouroboros.mcp.tools.evolution_handlers import _checkpoint_passed_generation_acs


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
        ac_results=(
            ACResult(ac_index=0, ac_content="Command prints stable output", passed=True),
        ),
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
