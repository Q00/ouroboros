"""Each judged AC attempt records the workspace diff and the verify verdict."""

from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from ouroboros.orchestrator.attempt_evidence import (
    attempt_workspace_evidence,
    judgment_envelope_ok,
    workspace_diff_evidence,
)
from ouroboros.orchestrator.verify_base_signal import no_signal_outcome


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(("git", "-C", str(cwd), *args), check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_diff_evidence_covers_tracked_edits_and_untracked_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    clean = workspace_diff_evidence(repo)
    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "test_new.py").write_text("def test_x(): pass\n", encoding="utf-8")

    edited = workspace_diff_evidence(repo)
    (repo / "test_new.py").write_text("def test_x(): assert False\n", encoding="utf-8")
    reedited = workspace_diff_evidence(repo)

    assert clean is not None and edited is not None and reedited is not None
    assert "module.py" in str(edited["diff_stat"])
    assert edited["untracked"] == ["test_new.py"]
    assert edited["untracked_count"] == 1
    assert edited["head"] == clean["head"]
    assert len({clean["sha256"], edited["sha256"], reedited["sha256"]}) == 3
    # Recording evidence never stages anything.
    staged = subprocess.run(
        ("git", "-C", str(repo), "diff", "--cached", "--name-only"),
        capture_output=True,
        text=True,
        check=True,
    )
    assert staged.stdout == ""


def test_no_diff_evidence_outside_a_git_work_tree(tmp_path: Path) -> None:
    assert workspace_diff_evidence(tmp_path) is None


@pytest.mark.asyncio
async def test_attempt_evidence_records_verdict_and_diff(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "module.py").write_text("VALUE = 3\n", encoding="utf-8")
    executor = SimpleNamespace(_task_cwd=str(repo), _adapter=None)
    result = SimpleNamespace(verify_gate_outcome=no_signal_outcome(1, "", None, False))

    evidence = await attempt_workspace_evidence(executor, result)

    assert evidence["verify_passed"] is False
    assert evidence["verify_no_signal"] is True
    assert evidence["verify_cause"] == "exit_nonzero"
    assert "module.py" in str(evidence["workspace_diff"])


def test_route_judgment_envelope_admits_only_evidence_keys() -> None:
    required = frozenset({"execution_id", "session_id"})
    base = {"execution_id": "e", "session_id": "s"}

    assert judgment_envelope_ok(base, required)
    assert judgment_envelope_ok({**base, "workspace_diff": {}, "verify_passed": True}, required)
    assert not judgment_envelope_ok({**base, "unexpected": 1}, required)
    assert not judgment_envelope_ok({"execution_id": "e"}, required)
