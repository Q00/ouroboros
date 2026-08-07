from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

from ouroboros.orchestrator.parallel_executor import ParallelACExecutor


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(path, "add", "product.py")
    _git(path, "commit", "-m", "initial")


def test_undeclared_evidence_output_does_not_invalidate_prior_workspace_digest(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    before = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "readback.md").write_text("verified\n", encoding="utf-8")
    after = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    assert before == after


def test_root_file_named_evidence_remains_workspace_visible(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    before = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    (tmp_path / "evidence").write_text("not a directory\n", encoding="utf-8")
    after = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    assert before != after


def test_tracked_evidence_output_remains_workspace_visible(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    contract = evidence / "contract.py"
    contract.write_text("ALLOW = False\n", encoding="utf-8")
    _git(tmp_path, "add", "evidence/contract.py")
    _git(tmp_path, "commit", "-m", "track contract")
    before = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    contract.write_text("ALLOW = True\n", encoding="utf-8")
    after = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    assert before != after


def test_non_utf8_tracked_evidence_remains_workspace_visible(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = subprocess.CompletedProcess(
        args=["git"],
        returncode=0,
        stdout=b"evidence/contract-\xff.py\0",
        stderr=b"",
    )

    with patch(
        "ouroboros.orchestrator.workspace_evidence_paths.subprocess.run",
        return_value=result,
    ):
        digest = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    assert digest is not None


def test_git_tracking_failure_keeps_generated_evidence_workspace_visible(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    report = evidence / "readback.md"
    report.write_text("first\n", encoding="utf-8")

    with patch(
        "ouroboros.orchestrator.workspace_evidence_paths.subprocess.run",
        side_effect=OSError("git unavailable"),
    ):
        before = ParallelACExecutor._workspace_content_digest(str(tmp_path))
        report.write_text("second\n", encoding="utf-8")
        after = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    assert before != after


def test_declared_evidence_output_remains_acceptance_visible(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    report = evidence / "readback.md"
    report.write_text("first\n", encoding="utf-8")
    before = ParallelACExecutor._workspace_content_digest(
        str(tmp_path), expected_artifacts=("evidence/readback.md",)
    )

    report.write_text("second\n", encoding="utf-8")
    after = ParallelACExecutor._workspace_content_digest(
        str(tmp_path), expected_artifacts=("evidence/readback.md",)
    )

    assert before != after


def test_nested_product_evidence_directory_remains_workspace_visible(tmp_path: Path) -> None:
    product_evidence = tmp_path / "src" / "evidence"
    product_evidence.mkdir(parents=True)
    report = product_evidence / "model.json"
    report.write_text("{}\n", encoding="utf-8")
    before = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    report.write_text('{"changed": true}\n', encoding="utf-8")
    after = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    assert before != after
