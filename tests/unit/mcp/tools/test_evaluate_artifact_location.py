"""``ouroboros_evaluate`` keeps its artifact file out of the user's project.

The evaluate step used to write ``.ouroboros_eval_artifact.md`` into the
evaluated directory, so the file shipped in every patch it evaluated. It now
lives under the product data dir and still reaches the evaluator as a file.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.evaluation.artifact_collector import (
    EVAL_ARTIFACT_FILENAME,
    eval_artifact_path,
    with_artifact_file,
)
from ouroboros.evaluation.models import (
    ArtifactBundle,
    CheckResult,
    CheckType,
    EvaluationResult,
    FileArtifact,
    MechanicalResult,
)
from ouroboros.mcp.tools.evaluation_handlers import EvaluateHandler

ARTIFACT = "Implemented the fix in src/app.py and added tests."


@pytest.mark.asyncio
async def test_evaluate_writes_no_artifact_file_into_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    pipeline = AsyncMock()
    pipeline.evaluate = AsyncMock(
        return_value=Result.ok(
            EvaluationResult(
                execution_id="x1",
                stage1_result=MechanicalResult(
                    passed=True,
                    checks=(
                        CheckResult(
                            check_type=CheckType.TEST,
                            passed=True,
                            message="skipped",
                            details={"skipped": True},
                        ),
                    ),
                ),
                final_approved=False,
            )
        )
    )
    with (
        patch("ouroboros.evaluation.EvaluationPipeline", return_value=pipeline),
        patch(
            "ouroboros.persistence.event_store.EventStore",
            return_value=AsyncMock(initialize=AsyncMock()),
        ),
        patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture"),
        patch(
            "ouroboros.evaluation.ensure_mechanical_toml",
            AsyncMock(return_value=None),
        ),
    ):
        outcome = await EvaluateHandler().handle(
            {
                "session_id": "x1",
                "artifact": ARTIFACT,
                "acceptance_criterion": "Only AC",
                "working_dir": str(project),
            }
        )

    assert outcome.is_ok
    assert not (project / EVAL_ARTIFACT_FILENAME).exists()
    stored = eval_artifact_path(project)
    assert stored.is_relative_to(home / ".ouroboros" / "data")
    assert stored.read_text(encoding="utf-8") == ARTIFACT


def test_artifact_file_joins_collected_files_first(tmp_path: Path) -> None:
    path = tmp_path / EVAL_ARTIFACT_FILENAME
    collected = ArtifactBundle(
        files=(FileArtifact(file_path="/p/src/app.py", content="x = 1"),),
        text_summary=ARTIFACT,
        total_chars=5,
    )

    bundle = with_artifact_file(collected, path, ARTIFACT)

    assert [item.file_path for item in bundle.files] == [str(path), "/p/src/app.py"]
    assert bundle.files[0].content == ARTIFACT
    assert bundle.total_chars == 5 + len(ARTIFACT)
    assert with_artifact_file(bundle, path, ARTIFACT) is bundle
    # Without collected files the prompt inlines the artifact text instead.
    bare = ArtifactBundle(text_summary=ARTIFACT)
    assert with_artifact_file(bare, path, ARTIFACT) is bare


def test_artifact_paths_are_per_project(tmp_path: Path) -> None:
    assert eval_artifact_path(tmp_path / "a") != eval_artifact_path(tmp_path / "b")
    assert eval_artifact_path(tmp_path / "a").name == EVAL_ARTIFACT_FILENAME
