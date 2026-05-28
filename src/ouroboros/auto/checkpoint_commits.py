"""Git checkpoint commits for verified auto acceptance criteria."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess

from ouroboros.auto.state import AutoCommitPolicy, AutoPipelineState

_SECRET_PATH_RE = re.compile(r"(^|/)(\.env(?:\.|$)|.*secret.*|.*credential.*)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CheckpointCommitResult:
    """Outcome of a checkpoint commit attempt."""

    status: str
    commit: str | None = None
    reason: str | None = None


def checkpoint_passed_ac(
    state: AutoPipelineState,
    *,
    repo_cwd: str | Path,
    ac_id: str,
    ac_text: str,
) -> CheckpointCommitResult:
    """Commit current git changes for a newly passed acceptance criterion."""
    if state.commit_policy is not AutoCommitPolicy.AC_CHECKPOINT:
        return CheckpointCommitResult(status="skipped", reason="commit_policy")
    if any(entry.get("ac_id") == ac_id for entry in state.checkpoint_commits):
        return CheckpointCommitResult(status="skipped", reason="already_committed")
    if ac_id in state.checkpoint_attempted_ac_ids:
        return CheckpointCommitResult(status="skipped", reason="already_attempted")
    state.checkpoint_attempted_ac_ids.append(ac_id)

    repo = Path(repo_cwd).expanduser().resolve()
    if not _is_git_repo(repo):
        return CheckpointCommitResult(status="skipped", reason="not_git_repo")

    paths = _changed_paths(repo)
    safe_paths = [path for path in paths if not _SECRET_PATH_RE.search(path)]
    if not safe_paths:
        return CheckpointCommitResult(status="skipped", reason="no_safe_changes")

    _git(repo, "add", "--", *safe_paths)
    if not _staged_changes(repo, safe_paths):
        return CheckpointCommitResult(status="skipped", reason="no_staged_changes")

    subject = f"ooo: satisfy {ac_id} {_summarize_ac(ac_text)}".strip()
    message = "\n".join(
        [
            subject[:72],
            "",
            f"Auto-Session: {state.auto_session_id}",
            f"Execution-Id: {state.execution_id or 'none'}",
            f"Acceptance-Criterion: {ac_id}",
        ]
    )
    _git(repo, "commit", "-m", message, "--", *safe_paths)
    commit = _git(repo, "rev-parse", "--short", "HEAD")
    state.checkpoint_commits.append(
        {
            "ac_id": ac_id,
            "ac_text": ac_text,
            "commit": commit,
            "execution_id": state.execution_id,
            "policy": state.commit_policy.value,
        }
    )
    return CheckpointCommitResult(status="committed", commit=commit)


def checkpoint_final_auto(
    state: AutoPipelineState,
    *,
    repo_cwd: str | Path,
    summary: str = "final verified auto result",
) -> CheckpointCommitResult:
    """Commit final git changes once when final-only policy is selected."""
    if state.commit_policy is not AutoCommitPolicy.FINAL_ONLY:
        return CheckpointCommitResult(status="skipped", reason="commit_policy")
    if state.final_checkpoint_attempted:
        return CheckpointCommitResult(status="skipped", reason="already_attempted")

    state.final_checkpoint_attempted = True
    repo = Path(repo_cwd).expanduser().resolve()
    if not _is_git_repo(repo):
        return CheckpointCommitResult(status="skipped", reason="not_git_repo")

    paths = _changed_paths(repo)
    safe_paths = [path for path in paths if not _SECRET_PATH_RE.search(path)]
    if not safe_paths:
        return CheckpointCommitResult(status="skipped", reason="no_safe_changes")

    _git(repo, "add", "--", *safe_paths)
    if not _staged_changes(repo, safe_paths):
        return CheckpointCommitResult(status="skipped", reason="no_staged_changes")

    message = "\n".join(
        [
            f"ooo: complete auto session {_summarize_ac(summary)}"[:72],
            "",
            f"Auto-Session: {state.auto_session_id}",
            f"Execution-Id: {state.execution_id or 'none'}",
            "Commit-Policy: final_only",
        ]
    )
    _git(repo, "commit", "-m", message, "--", *safe_paths)
    commit = _git(repo, "rev-parse", "--short", "HEAD")
    state.checkpoint_commits.append(
        {
            "ac_id": "FINAL",
            "ac_text": summary,
            "commit": commit,
            "execution_id": state.execution_id,
            "policy": state.commit_policy.value,
        }
    )
    return CheckpointCommitResult(status="committed", commit=commit)


def _is_git_repo(repo: Path) -> bool:
    try:
        _git(repo, "rev-parse", "--is-inside-work-tree")
    except RuntimeError:
        return False
    return True


def _changed_paths(repo: Path) -> list[str]:
    output = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths: list[str] = []
    entries = output.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:]
        if path:
            paths.append(path)
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            index += 1
    return paths


def _staged_changes(repo: Path, paths: list[str]) -> bool:
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", *paths],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 1


def _summarize_ac(ac_text: str) -> str:
    summary = " ".join(ac_text.split())
    if len(summary) > 48:
        summary = summary[:45].rstrip() + "..."
    return summary


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()
