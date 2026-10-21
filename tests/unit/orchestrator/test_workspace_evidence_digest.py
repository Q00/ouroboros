from __future__ import annotations

import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import ouroboros.orchestrator.parallel_executor as parallel_executor_module
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


def test_git_ignored_build_output_does_not_invalidate_workspace_digest(tmp_path: Path) -> None:
    """Build trees the project itself declared non-source are not evidence.

    ``target/``, ``dist/``, coverage data and similar ignore-rule paths are
    refreshed by ordinary test commands; treating that as a workspace
    mutation rejected every verify_command that ran a real build.
    """
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("target/\n*.coverage\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-m", "ignore build output")
    before = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "debug.bin").write_bytes(b"\x00\x01")
    (tmp_path / "run.coverage").write_text("data\n", encoding="utf-8")
    after = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    assert before == after


def test_untracked_source_file_still_invalidates_workspace_digest(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("target/\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-m", "ignore build output")
    before = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    (tmp_path / "new_module.py").write_text("VALUE = 2\n", encoding="utf-8")
    after = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    assert before != after


def test_workspace_digest_refuses_when_entry_budget_is_exceeded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b\n", encoding="utf-8")
    monkeypatch.setattr(parallel_executor_module, "_WORKSPACE_FINGERPRINT_MAX_ENTRIES", 2)

    assert ParallelACExecutor._workspace_content_digest(str(tmp_path)) is None


def test_workspace_digest_does_not_enumerate_past_entry_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b\n", encoding="utf-8")
    monkeypatch.setattr(parallel_executor_module, "_WORKSPACE_FINGERPRINT_MAX_ENTRIES", 2)
    real_scandir = os.scandir
    enumerated = 0

    class CountingScandir:
        def __init__(self, path):
            self._iterator = real_scandir(path)

        def __enter__(self):
            self._iterator.__enter__()
            return self

        def __exit__(self, *args):
            return self._iterator.__exit__(*args)

        def __iter__(self):
            nonlocal enumerated
            for entry in self._iterator:
                enumerated += 1
                yield entry

    monkeypatch.setattr(parallel_executor_module.os, "scandir", CountingScandir)

    assert ParallelACExecutor._workspace_content_digest(str(tmp_path)) is None
    assert enumerated == 2


def test_workspace_digest_refuses_when_depth_budget_is_exceeded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _init_repo(tmp_path)
    deep = tmp_path / "nested" / "deeper"
    deep.mkdir(parents=True)
    (deep / "source.py").write_text("code\n", encoding="utf-8")
    monkeypatch.setattr(parallel_executor_module, "_WORKSPACE_FINGERPRINT_MAX_DEPTH", 1)

    assert ParallelACExecutor._workspace_content_digest(str(tmp_path)) is None


def test_workspace_digest_hashes_symlinks_without_following_targets(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    target = outside / "value.py"
    target.write_text("first\n", encoding="utf-8")
    link = tmp_path / "external"
    link.symlink_to(outside, target_is_directory=True)
    (tmp_path / "cycle").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "repeat").symlink_to(outside, target_is_directory=True)

    before = ParallelACExecutor._workspace_content_digest(str(tmp_path))
    target.write_text("second\n", encoding="utf-8")
    after_target_change = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    assert before is not None
    assert after_target_change == before
    link.unlink()
    link.symlink_to(tmp_path / "another", target_is_directory=True)
    after_link_change = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    assert after_link_change is not None
    assert after_link_change != before


def test_declared_artifact_under_ignored_path_remains_observable(tmp_path: Path) -> None:
    """An expected artifact is evidence even when an ignore rule covers it."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("dist/\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-m", "ignore dist")
    (tmp_path / "dist").mkdir()
    report = tmp_path / "dist" / "report.md"
    report.write_text("v1\n", encoding="utf-8")
    before = ParallelACExecutor._workspace_content_digest(
        str(tmp_path), expected_artifacts=("dist/report.md",)
    )

    report.write_text("v2\n", encoding="utf-8")
    after = ParallelACExecutor._workspace_content_digest(
        str(tmp_path), expected_artifacts=("dist/report.md",)
    )

    assert before != after


def test_digest_outside_git_ignores_nothing_by_ignore_rules(tmp_path: Path) -> None:
    """Without Git to prove the ignore set, every path stays observable."""
    (tmp_path / ".gitignore").write_text("target/\n", encoding="utf-8")
    before = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "out.bin").write_bytes(b"x")
    after = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    assert before != after
