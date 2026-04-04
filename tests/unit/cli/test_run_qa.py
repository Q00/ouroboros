"""Unit tests for CLI post-run QA verification artifact wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.cli.commands.run import _run_orchestrator
from ouroboros.core.types import Result
from ouroboros.evaluation.verification_artifacts import VerificationArtifacts
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

VALID_SEED_DATA = {
    "goal": "Test task",
    "constraints": ["Python 3.14+"],
    "acceptance_criteria": ["All tests pass", "No lint errors"],
    "ontology_schema": {
        "name": "TestOntology",
        "description": "Test ontology",
        "fields": [
            {
                "name": "test_field",
                "field_type": "string",
                "description": "A test field",
            }
        ],
    },
    "evaluation_principles": [],
    "exit_conditions": [],
    "metadata": {
        "seed_id": "test-seed-cli-qa",
        "version": "1.0.0",
        "created_at": "2024-01-01T00:00:00Z",
        "ambiguity_score": 0.1,
        "interview_id": None,
    },
}

VALID_SEED_DATA_WITH_RELATIVE_PROJECT = {
    **VALID_SEED_DATA,
    "brownfield_context": {
        "project_type": "brownfield",
        "context_references": [
            {
                "path": "repo-root",
                "role": "primary",
                "summary": "",
            }
        ],
    },
}

FAKE_QA_RESULT: Result[MCPToolResult, str] = Result.ok(
    MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="QA Verdict [PASS]"),),
        is_error=False,
        meta={"score": 0.85},
    )
)

FAKE_VERIFICATION_ARTIFACTS = VerificationArtifacts(
    artifact="Structured verification artifact",
    reference="Raw verification reference",
    artifact_dir="/tmp/ouroboros-artifacts/exec-test",
    manifest_path="/tmp/ouroboros-artifacts/exec-test/manifest.json",
)


@pytest.mark.asyncio
async def test_run_orchestrator_passes_artifact_and_reference_to_qa(tmp_path: Path) -> None:
    """CLI QA should use the generated verification artifact and raw reference."""
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text("goal: ignored\n", encoding="utf-8")

    fake_exec = SimpleNamespace(
        success=True,
        session_id="sess-test",
        messages_processed=5,
        duration_seconds=1.0,
        execution_id="exec-test",
        summary={"verification_report": "Parallel Execution Verification Report"},
        final_message="fallback final message",
    )
    mock_runner = MagicMock()
    mock_runner.execute_seed = AsyncMock(return_value=Result.ok(fake_exec))
    mock_runner.resume_session = AsyncMock()

    with (
        patch("ouroboros.cli.commands.run._load_seed_from_yaml", return_value=VALID_SEED_DATA),
        patch("ouroboros.orchestrator.create_agent_runtime"),
        patch("ouroboros.orchestrator.OrchestratorRunner", return_value=mock_runner),
        patch("ouroboros.persistence.event_store.EventStore") as mock_event_store_cls,
        patch(
            "ouroboros.cli.commands.run.build_verification_artifacts",
            new_callable=AsyncMock,
            return_value=FAKE_VERIFICATION_ARTIFACTS,
        ) as mock_verification,
        patch(
            "ouroboros.mcp.tools.qa.QAHandler.handle",
            new_callable=AsyncMock,
            return_value=FAKE_QA_RESULT,
        ) as mock_qa_handle,
    ):
        mock_event_store_cls.return_value.initialize = AsyncMock()
        await _run_orchestrator(seed_file)

    mock_verification.assert_awaited_once_with(
        "exec-test",
        "Parallel Execution Verification Report",
        seed_file.parent.resolve(),
    )
    qa_args = mock_qa_handle.call_args.args[0]
    assert qa_args["artifact"] == "Structured verification artifact"
    assert qa_args["reference"] == "Raw verification reference"


@pytest.mark.asyncio
async def test_run_orchestrator_uses_seed_relative_project_dir_for_runtime_and_qa(
    tmp_path: Path,
) -> None:
    """CLI execution and QA should share the seed-derived project root."""
    seed_dir = tmp_path / "seed-dir"
    seed_dir.mkdir()
    seed_file = seed_dir / "seed.yaml"
    seed_file.write_text("goal: ignored\n", encoding="utf-8")
    expected_project_dir = (seed_dir / "repo-root").resolve()

    fake_exec = SimpleNamespace(
        success=True,
        session_id="sess-test",
        messages_processed=5,
        duration_seconds=1.0,
        execution_id="exec-test",
        summary={"verification_report": "Parallel Execution Verification Report"},
        final_message="fallback final message",
    )
    mock_runner = MagicMock()
    mock_runner.execute_seed = AsyncMock(return_value=Result.ok(fake_exec))
    mock_runner.resume_session = AsyncMock()

    with (
        patch(
            "ouroboros.cli.commands.run._load_seed_from_yaml",
            return_value=VALID_SEED_DATA_WITH_RELATIVE_PROJECT,
        ),
        patch("ouroboros.orchestrator.create_agent_runtime") as mock_runtime,
        patch("ouroboros.orchestrator.OrchestratorRunner", return_value=mock_runner),
        patch("ouroboros.persistence.event_store.EventStore") as mock_event_store_cls,
        patch(
            "ouroboros.cli.commands.run.build_verification_artifacts",
            new_callable=AsyncMock,
            return_value=FAKE_VERIFICATION_ARTIFACTS,
        ) as mock_verification,
        patch(
            "ouroboros.mcp.tools.qa.QAHandler.handle",
            new_callable=AsyncMock,
            return_value=FAKE_QA_RESULT,
        ),
    ):
        mock_event_store_cls.return_value.initialize = AsyncMock()
        await _run_orchestrator(seed_file)

    mock_runtime.assert_called_once_with(backend=None, cwd=expected_project_dir)
    mock_verification.assert_awaited_once_with(
        "exec-test",
        "Parallel Execution Verification Report",
        expected_project_dir,
    )


@pytest.mark.asyncio
async def test_run_orchestrator_falls_back_when_artifact_generation_fails(tmp_path: Path) -> None:
    """CLI QA should degrade gracefully when raw verification generation fails."""
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text("goal: ignored\n", encoding="utf-8")

    fake_exec = SimpleNamespace(
        success=True,
        session_id="sess-test",
        messages_processed=5,
        duration_seconds=1.0,
        execution_id="exec-test",
        summary={"verification_report": "Parallel Execution Verification Report"},
        final_message="fallback final message",
    )
    mock_runner = MagicMock()
    mock_runner.execute_seed = AsyncMock(return_value=Result.ok(fake_exec))
    mock_runner.resume_session = AsyncMock()

    with (
        patch("ouroboros.cli.commands.run._load_seed_from_yaml", return_value=VALID_SEED_DATA),
        patch("ouroboros.orchestrator.create_agent_runtime"),
        patch("ouroboros.orchestrator.OrchestratorRunner", return_value=mock_runner),
        patch("ouroboros.persistence.event_store.EventStore") as mock_event_store_cls,
        patch(
            "ouroboros.cli.commands.run.build_verification_artifacts",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "ouroboros.mcp.tools.qa.QAHandler.handle",
            new_callable=AsyncMock,
            return_value=FAKE_QA_RESULT,
        ) as mock_qa_handle,
    ):
        mock_event_store_cls.return_value.initialize = AsyncMock()
        await _run_orchestrator(seed_file)

    qa_args = mock_qa_handle.call_args.args[0]
    assert qa_args["artifact"] == "Parallel Execution Verification Report"
    assert qa_args["reference"] == "Verification artifact generation failed: boom"
