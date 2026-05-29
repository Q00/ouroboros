from __future__ import annotations

import subprocess

from ouroboros.auto.checkpoint_commits import checkpoint_final_auto, checkpoint_passed_ac
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.state import AutoCommitPolicy, AutoPhase, AutoPipelineState


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


def test_checkpoint_passed_ac_commits_once_with_metadata(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(repo))
    state.commit_policy = AutoCommitPolicy.AC_CHECKPOINT
    state.execution_id = "exec_123"

    result = checkpoint_passed_ac(
        state,
        repo_cwd=repo,
        ac_id="AC-1",
        ac_text="Command prints stable output",
    )
    duplicate = checkpoint_passed_ac(
        state,
        repo_cwd=repo,
        ac_id="AC-1",
        ac_text="Command prints stable output",
    )

    assert result.status == "committed"
    assert result.commit
    assert duplicate.status == "skipped"
    assert duplicate.reason == "already_committed"
    assert state.checkpoint_attempted_ac_ids == ["AC-1"]
    assert state.checkpoint_commits == [
        {
            "ac_id": "AC-1",
            "ac_text": "Command prints stable output",
            "commit": result.commit,
            "execution_id": "exec_123",
            "policy": "ac_checkpoint",
        }
    ]
    log = _git(repo, "log", "-1", "--pretty=%B")
    assert "ooo: satisfy AC-1 Command prints stable output" in log
    assert f"Auto-Session: {state.auto_session_id}" in log
    assert "Execution-Id: exec_123" in log
    assert "Acceptance-Criterion: AC-1" in log


def test_checkpoint_passed_ac_skips_dirty_secret_only_changes(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(repo))
    state.commit_policy = AutoCommitPolicy.AC_CHECKPOINT

    result = checkpoint_passed_ac(
        state,
        repo_cwd=repo,
        ac_id="AC-2",
        ac_text="Does not commit secrets",
    )

    assert result.status == "skipped"
    assert result.reason == "no_safe_changes"
    assert state.checkpoint_commits == []
    assert state.checkpoint_attempted_ac_ids == ["AC-2"]
    assert _git(repo, "log", "--oneline").count("\n") == 0


def test_checkpoint_passed_ac_does_not_commit_pre_staged_secret(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    _git(repo, "add", ".env")
    (repo / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(repo))
    state.commit_policy = AutoCommitPolicy.AC_CHECKPOINT

    result = checkpoint_passed_ac(
        state,
        repo_cwd=repo,
        ac_id="AC-2",
        ac_text="Command prints stable output",
    )

    assert result.status == "committed"
    assert _git(repo, "show", "--name-only", "--pretty=", "HEAD") == "feature.py"
    assert ".env" in _git(repo, "diff", "--cached", "--name-only")


def test_checkpoint_passed_ac_does_not_retry_attempted_pass_transition(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(repo))
    state.commit_policy = AutoCommitPolicy.AC_CHECKPOINT

    first = checkpoint_passed_ac(
        state,
        repo_cwd=repo,
        ac_id="AC-6",
        ac_text="Command prints stable output",
    )
    (repo / "feature.py").write_text("print('later')\n", encoding="utf-8")
    second = checkpoint_passed_ac(
        state,
        repo_cwd=repo,
        ac_id="AC-6",
        ac_text="Command prints stable output",
    )

    assert first.status == "skipped"
    assert first.reason == "no_safe_changes"
    assert second.status == "skipped"
    assert second.reason == "already_attempted"
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"


def test_checkpoint_passed_ac_respects_disabled_policy(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(repo))
    state.commit_policy = AutoCommitPolicy.NONE

    result = checkpoint_passed_ac(
        state,
        repo_cwd=repo,
        ac_id="AC-3",
        ac_text="Command prints stable output",
    )

    assert result.status == "skipped"
    assert result.reason == "commit_policy"
    assert state.checkpoint_commits == []


def test_checkpoint_passed_ac_skips_for_final_only_policy(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    state = AutoPipelineState(goal="Write docs", cwd=str(repo))
    state.commit_policy = AutoCommitPolicy.FINAL_ONLY

    result = checkpoint_passed_ac(
        state,
        repo_cwd=repo,
        ac_id="AC-4",
        ac_text="Documentation is complete",
    )

    assert result.status == "skipped"
    assert result.reason == "commit_policy"
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"


def test_checkpoint_passed_ac_gracefully_skips_non_repo(tmp_path) -> None:
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.commit_policy = AutoCommitPolicy.AC_CHECKPOINT

    result = checkpoint_passed_ac(
        state,
        repo_cwd=tmp_path,
        ac_id="AC-5",
        ac_text="Command prints stable output",
    )

    assert result.status == "skipped"
    assert result.reason == "not_git_repo"
    assert state.checkpoint_commits == []


def test_checkpoint_final_auto_commits_once_for_final_only_policy(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "feature.py").write_text("print('done')\n", encoding="utf-8")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(repo))
    state.commit_policy = AutoCommitPolicy.FINAL_ONLY
    state.execution_id = "exec_123"

    result = checkpoint_final_auto(
        state,
        repo_cwd=repo,
        summary="all acceptance criteria passed",
    )
    duplicate = checkpoint_final_auto(
        state,
        repo_cwd=repo,
        summary="all acceptance criteria passed",
    )

    assert result.status == "committed"
    assert duplicate.status == "skipped"
    assert duplicate.reason == "already_attempted"
    assert state.final_checkpoint_attempted is True
    assert state.checkpoint_commits == [
        {
            "ac_id": "FINAL",
            "ac_text": "all acceptance criteria passed",
            "commit": result.commit,
            "execution_id": "exec_123",
            "policy": "final_only",
        }
    ]
    log = _git(repo, "log", "-1", "--pretty=%B")
    assert "ooo: complete auto session all acceptance criteria passed" in log
    assert f"Auto-Session: {state.auto_session_id}" in log
    assert "Execution-Id: exec_123" in log
    assert "Commit-Policy: final_only" in log


def test_pipeline_result_attempts_final_only_commit_on_complete(tmp_path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "feature.py").write_text("print('done')\n", encoding="utf-8")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(repo))
    state.commit_policy = AutoCommitPolicy.FINAL_ONLY
    state.phase = AutoPhase.COMPLETE
    state.last_progress_message = "all acceptance criteria passed"
    pipeline = AutoPipeline(
        interview_driver=None,  # type: ignore[arg-type]
        seed_generator=None,  # type: ignore[arg-type]
    )

    result = pipeline._result(state, SeedDraftLedger.from_goal(state.goal))
    replay = pipeline._result(state, SeedDraftLedger.from_goal(state.goal))

    assert result.status == "complete"
    assert len(result.checkpoint_commits) == 1
    assert replay.checkpoint_commits == result.checkpoint_commits
    assert state.final_checkpoint_attempted is True
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"


def test_pipeline_result_does_not_commit_dirty_current_checkout_by_default(tmp_path) -> None:
    """#1281 review blocker: a default (non-coding / legacy) session must never
    commit the caller's pre-existing dirty checkout when it completes.

    Regression for ouroboros-agent[bot] ``req_1780010936_238``. The prior default
    of ``FINAL_ONLY`` + ``CURRENT`` turned every ``COMPLETE`` result into a git
    commit of the operator's working tree, even for research/documentation/
    ``skip_run`` flows that performed no managed coding work. The conservative
    default is now ``NONE``; final-only commits on the current checkout require
    an explicit operator opt-in.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    # Operator's own uncommitted edits, unrelated to the auto session.
    (repo / "operator_wip.py").write_text("print('local wip')\n", encoding="utf-8")
    state = AutoPipelineState(goal="Summarize research papers", cwd=str(repo))
    # No explicit policy is set: the conservative defaults must apply.
    assert state.commit_policy is AutoCommitPolicy.NONE
    state.phase = AutoPhase.COMPLETE
    state.last_progress_message = "research summary complete"
    pipeline = AutoPipeline(
        interview_driver=None,  # type: ignore[arg-type]
        seed_generator=None,  # type: ignore[arg-type]
    )

    result = pipeline._result(state, SeedDraftLedger.from_goal(state.goal))

    assert result.status == "complete"
    assert result.checkpoint_commits == ()
    assert state.final_checkpoint_attempted is False
    # No commit was created and the operator's WIP stays uncommitted.
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert "operator_wip.py" in _git(repo, "status", "--porcelain")
