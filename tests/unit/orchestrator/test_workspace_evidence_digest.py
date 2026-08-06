from __future__ import annotations

from pathlib import Path

from ouroboros.orchestrator.parallel_executor import ParallelACExecutor


def test_undeclared_evidence_output_does_not_invalidate_prior_workspace_digest(
    tmp_path: Path,
) -> None:
    product = tmp_path / "product.py"
    product.write_text("VALUE = 1\n", encoding="utf-8")
    before = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "readback.md").write_text("verified\n", encoding="utf-8")
    after = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    assert before == after


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
