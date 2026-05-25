"""Unit tests for CLI post-run QA verification artifact wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from ouroboros.cli.commands.run import (
    _load_skip_completed_markers,
    _resolve_cli_project_dir,
    _resolve_fat_harness_mode,
    _resolve_max_decomposition_depth,
    _resolve_max_parallel_workers,
    _resolve_resume_fat_harness_mode,
    _run_orchestrator,
)
from ouroboros.core.seed import Seed
from ouroboros.core.types import Result
from ouroboros.evaluation.verification_artifacts import VerificationArtifacts
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.session import SessionTracker

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


def test_resolve_cli_project_dir_prefers_explicit_project_dir(tmp_path: Path) -> None:
    """--project-dir should be the highest-priority run boundary."""
    seed_file = tmp_path / "seeds" / "seed.yaml"
    seed_file.parent.mkdir()
    seed_file.write_text("goal: ignored\n", encoding="utf-8")
    explicit_project = tmp_path / "project"
    explicit_project.mkdir()
    seed = Seed.from_dict(VALID_SEED_DATA)

    assert (
        _resolve_cli_project_dir(
            seed,
            seed_file,
            seed_data=VALID_SEED_DATA,
            project_dir=explicit_project,
        )
        == explicit_project.resolve()
    )


def test_resolve_cli_project_dir_uses_brownfield_target_dir_when_present(
    tmp_path: Path,
) -> None:
    """Seeds in a central library may target an external brownfield repo."""
    seed_file = tmp_path / "seed-library" / "seed.yaml"
    seed_file.parent.mkdir()
    seed_file.write_text("goal: ignored\n", encoding="utf-8")
    target_dir = tmp_path / "work" / "myproject"
    target_dir.mkdir(parents=True)
    seed_data = {
        **VALID_SEED_DATA,
        "brownfield_context": {
            "project_type": "brownfield",
            "target_dir": str(target_dir),
            "context_references": [
                {"path": "main.py", "role": "primary", "summary": "target file"},
            ],
        },
    }
    (target_dir / "main.py").write_text("print('hi')\n", encoding="utf-8")
    seed = Seed.from_dict(seed_data)

    assert _resolve_cli_project_dir(seed, seed_file, seed_data=seed_data) == target_dir.resolve()


def test_resolve_cli_project_dir_falls_back_to_seed_parent_without_project_hints(
    tmp_path: Path,
) -> None:
    """Back-compat path remains the seed file directory."""
    seed_file = tmp_path / "seeds" / "seed.yaml"
    seed_file.parent.mkdir()
    seed_file.write_text("goal: ignored\n", encoding="utf-8")
    seed = Seed.from_dict(VALID_SEED_DATA)

    assert (
        _resolve_cli_project_dir(seed, seed_file, seed_data=VALID_SEED_DATA)
        == seed_file.parent.resolve()
    )


def test_resolve_cli_project_dir_keeps_seed_relative_metadata_project_dir(
    tmp_path: Path,
) -> None:
    """metadata.project_dir keeps working with the existing seed-relative behavior."""
    seed_file = tmp_path / "seeds" / "seed.yaml"
    seed_file.parent.mkdir()
    seed_file.write_text("goal: ignored\n", encoding="utf-8")
    seed_data = {
        **VALID_SEED_DATA,
        "metadata": {**VALID_SEED_DATA["metadata"], "project_dir": "repo-root"},
    }
    seed = Seed.from_dict(seed_data)

    assert (
        _resolve_cli_project_dir(seed, seed_file, seed_data=seed_data)
        == (seed_file.parent / "repo-root").resolve()
    )


@pytest.mark.parametrize("metadata_field", ["project_dir", "working_directory"])
def test_resolve_cli_project_dir_rejects_raw_metadata_project_escape(
    tmp_path: Path, metadata_field: str
) -> None:
    """Raw metadata project fields must not silently fall back after rejection."""
    seed_file = tmp_path / "seeds" / "seed.yaml"
    seed_file.parent.mkdir()
    seed_file.write_text("goal: ignored\n", encoding="utf-8")
    outside_project = tmp_path / "outside-project"
    seed_data = {
        **VALID_SEED_DATA,
        "metadata": {
            **VALID_SEED_DATA["metadata"],
            metadata_field: str(outside_project),
        },
    }
    seed = Seed.from_dict(seed_data)

    with patch("ouroboros.cli.commands.run.print_error") as mock_print:
        with pytest.raises(typer.Exit) as exc_info:
            _resolve_cli_project_dir(seed, seed_file, seed_data=seed_data)

    assert exc_info.value.exit_code == 1
    assert mock_print.call_count == 1
    assert "escapes" in mock_print.call_args[0][0]


def test_resolve_cli_project_dir_uses_parent_when_context_reference_is_file(
    tmp_path: Path,
) -> None:
    """A primary file reference should not become the runtime cwd itself."""
    seed_file = tmp_path / "seed-library" / "seed.yaml"
    seed_file.parent.mkdir()
    seed_file.write_text("goal: ignored\n", encoding="utf-8")
    target_dir = tmp_path / "work" / "myproject"
    target_dir.mkdir(parents=True)
    source_file = target_dir / "src" / "main.py"
    source_file.parent.mkdir()
    source_file.write_text("print('hi')\n", encoding="utf-8")
    seed_data = {
        **VALID_SEED_DATA,
        "brownfield_context": {
            "project_type": "brownfield",
            "target_dir": str(target_dir),
            "context_references": [
                {"path": "src/main.py", "role": "primary", "summary": "target file"},
            ],
        },
    }
    seed = Seed.from_dict(seed_data)

    assert (
        _resolve_cli_project_dir(seed, seed_file, seed_data=seed_data)
        == source_file.parent.resolve()
    )


def test_resolve_fat_harness_mode_defaults_to_enabled() -> None:
    """The #920 PR-5 default flip enables fat-harness without seed opt-in."""
    assert _resolve_fat_harness_mode(VALID_SEED_DATA) is True


def test_resolve_fat_harness_mode_accepts_fat_harness_execution_mode() -> None:
    """Explicit fat-harness mode remains accepted after #978 P5."""
    seed_data = {**VALID_SEED_DATA, "orchestrator": {"execution_mode": "fat_harness"}}

    assert _resolve_fat_harness_mode(seed_data) is True


def test_resolve_fat_harness_mode_rejects_legacy_execution_mode() -> None:
    """#978 P5 removes the legacy self-report fallback selector."""
    seed_data = {**VALID_SEED_DATA, "orchestrator": {"execution_mode": "legacy"}}

    with pytest.raises(typer.Exit):
        _resolve_fat_harness_mode(seed_data)


def test_resolve_fat_harness_mode_rejects_unknown_execution_mode() -> None:
    seed_data = {**VALID_SEED_DATA, "orchestrator": {"execution_mode": "mystery"}}

    with pytest.raises(typer.Exit):
        _resolve_fat_harness_mode(seed_data)


def test_resolve_resume_fat_harness_mode_uses_persisted_contract() -> None:
    """Resume prefers the durable session contract over seed selectors."""
    seed_data = {**VALID_SEED_DATA, "orchestrator": {"execution_mode": "legacy"}}

    assert _resolve_resume_fat_harness_mode(seed_data, {"fat_harness_mode": True}) is True
    assert _resolve_resume_fat_harness_mode(seed_data, {"fat_harness_mode": False}) is False


def test_resolve_resume_fat_harness_mode_migrates_missing_contract_conservatively() -> None:
    """Only explicit historical legacy selectors resume ungated when contract is absent."""
    legacy_seed = {**VALID_SEED_DATA, "orchestrator": {"execution_mode": "legacy"}}

    assert _resolve_resume_fat_harness_mode(legacy_seed, {}) is False
    assert _resolve_resume_fat_harness_mode(VALID_SEED_DATA, {}) is True


def test_resolve_max_decomposition_depth_defaults_to_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workflow depth cap should default to 2 when nothing overrides it."""
    monkeypatch.delenv("OUROBOROS_MAX_DECOMPOSITION_DEPTH", raising=False)

    resolved = _resolve_max_decomposition_depth(VALID_SEED_DATA, None)

    assert resolved == 2


def test_resolve_max_decomposition_depth_prefers_cli_then_env_then_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI should win over env, and env should win over the seed override."""
    monkeypatch.setenv("OUROBOROS_MAX_DECOMPOSITION_DEPTH", "4")
    seed_data = {
        **VALID_SEED_DATA,
        "orchestrator": {"max_decomposition_depth": 3},
    }

    assert _resolve_max_decomposition_depth(seed_data, None) == 4
    assert _resolve_max_decomposition_depth(seed_data, 1) == 1


def test_load_skip_completed_markers_parses_yaml_metadata(tmp_path: Path) -> None:
    """The skip-completed marker file should resolve 1-based AC numbers."""
    marker_file = tmp_path / "completed.yaml"
    marker_file.write_text(
        "completed_acs:\n  - ac: 1\n    reason: Done manually\n    commit: abc1234\n  - 2\n",
        encoding="utf-8",
    )

    markers = _load_skip_completed_markers(str(marker_file), total_acs=3)

    assert markers == {
        0: {"reason": "Done manually", "commit": "abc1234"},
        1: {},
    }


def test_resolve_max_parallel_workers_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parallel worker caps should be configurable via environment variable."""
    monkeypatch.setenv("OUROBOROS_MAX_PARALLEL_WORKERS", "5")

    assert _resolve_max_parallel_workers() == 5


def test_resolve_max_parallel_workers_reads_config_when_env_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parallel worker caps should fall back to config when env override is absent."""
    monkeypatch.delenv("OUROBOROS_MAX_PARALLEL_WORKERS", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("orchestrator:\n  max_parallel_workers: 5\n", encoding="utf-8")

    with patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path):
        assert _resolve_max_parallel_workers() == 5


def test_resolve_max_parallel_workers_rejects_invalid_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid config worker caps should fail the CLI path clearly."""
    monkeypatch.delenv("OUROBOROS_MAX_PARALLEL_WORKERS", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("orchestrator:\n  max_parallel_workers: 0\n", encoding="utf-8")

    with (
        patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path),
        pytest.raises(typer.Exit) as exc_info,
    ):
        _resolve_max_parallel_workers()

    assert exc_info.value.exit_code == 1


def test_resolve_max_parallel_workers_ignores_unrelated_invalid_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrelated invalid config should not block CLI worker-cap resolution."""
    monkeypatch.delenv("OUROBOROS_MAX_PARALLEL_WORKERS", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("economics:\n  default_tier: invalid_tier\n", encoding="utf-8")

    with patch("ouroboros.config.loader.get_config_dir", return_value=tmp_path):
        assert _resolve_max_parallel_workers() == 3


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
async def test_run_orchestrator_passes_resolved_execution_caps_to_runner(tmp_path: Path) -> None:
    """CLI orchestration should pass resolved execution caps into the runner."""
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
    seed_data = {
        **VALID_SEED_DATA,
        "orchestrator": {"max_decomposition_depth": 3},
    }

    with (
        patch("ouroboros.cli.commands.run._load_seed_from_yaml", return_value=seed_data),
        patch("ouroboros.orchestrator.create_agent_runtime"),
        patch(
            "ouroboros.orchestrator.OrchestratorRunner", return_value=mock_runner
        ) as mock_runner_cls,
        patch("ouroboros.cli.commands.run._resolve_max_parallel_workers", return_value=7),
        patch("ouroboros.persistence.event_store.EventStore") as mock_event_store_cls,
        patch(
            "ouroboros.cli.commands.run.build_verification_artifacts",
            new_callable=AsyncMock,
            return_value=FAKE_VERIFICATION_ARTIFACTS,
        ),
        patch(
            "ouroboros.mcp.tools.qa.QAHandler.handle",
            new_callable=AsyncMock,
            return_value=FAKE_QA_RESULT,
        ),
    ):
        mock_event_store_cls.return_value.initialize = AsyncMock()
        await _run_orchestrator(seed_file)

    assert mock_runner_cls.call_args.kwargs["max_decomposition_depth"] == 3
    assert mock_runner_cls.call_args.kwargs["max_parallel_workers"] == 7
    assert mock_runner_cls.call_args.kwargs["fat_harness_mode"] is True


@pytest.mark.asyncio
async def test_run_orchestrator_passes_default_fat_harness_mode_to_runner(tmp_path: Path) -> None:
    """The default #920 PR-5 path selects fat-harness without seed opt-in."""
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
    seed_data = {**VALID_SEED_DATA, "orchestrator": {"max_decomposition_depth": 2}}

    with (
        patch("ouroboros.cli.commands.run._load_seed_from_yaml", return_value=seed_data),
        patch("ouroboros.orchestrator.create_agent_runtime"),
        patch(
            "ouroboros.orchestrator.OrchestratorRunner", return_value=mock_runner
        ) as mock_runner_cls,
        patch("ouroboros.persistence.event_store.EventStore") as mock_event_store_cls,
        patch(
            "ouroboros.cli.commands.run.build_verification_artifacts",
            new_callable=AsyncMock,
            return_value=FAKE_VERIFICATION_ARTIFACTS,
        ),
        patch(
            "ouroboros.mcp.tools.qa.QAHandler.handle",
            new_callable=AsyncMock,
            return_value=FAKE_QA_RESULT,
        ),
    ):
        mock_event_store_cls.return_value.initialize = AsyncMock()
        await _run_orchestrator(seed_file)

    assert mock_runner_cls.call_args.kwargs["fat_harness_mode"] is True


@pytest.mark.asyncio
async def test_run_orchestrator_resume_uses_persisted_fat_harness_contract(
    tmp_path: Path,
) -> None:
    """Resume trusts the stored session contract instead of revalidating old seed modes."""
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text("goal: ignored\n", encoding="utf-8")

    tracker = SessionTracker.create(
        "exec-resume",
        VALID_SEED_DATA["metadata"]["seed_id"],
        session_id="sess-resume",
    )
    fake_exec = SimpleNamespace(
        success=True,
        session_id="sess-resume",
        messages_processed=1,
        duration_seconds=1.0,
        execution_id="exec-resume",
        summary={},
        final_message="resumed",
    )
    mock_runner = MagicMock()
    mock_runner.resume_session = AsyncMock(return_value=Result.ok(fake_exec))
    seed_data = {**VALID_SEED_DATA, "orchestrator": {"execution_mode": "legacy"}}

    with (
        patch("ouroboros.cli.commands.run._load_seed_from_yaml", return_value=seed_data),
        patch("ouroboros.orchestrator.create_agent_runtime"),
        patch(
            "ouroboros.orchestrator.OrchestratorRunner", return_value=mock_runner
        ) as mock_runner_cls,
        patch("ouroboros.persistence.event_store.EventStore") as mock_event_store_cls,
        patch("ouroboros.orchestrator.session.SessionRepository") as mock_repo_cls,
        patch("ouroboros.cli.commands.run.maybe_restore_task_workspace", return_value=None),
    ):
        mock_event_store_cls.return_value.initialize = AsyncMock()
        mock_repo_cls.return_value.reconstruct_session = AsyncMock(return_value=Result.ok(tracker))

        await _run_orchestrator(seed_file, resume_session="sess-resume", no_qa=True)

    assert mock_runner_cls.call_args.kwargs["fat_harness_mode"] is False
    mock_runner.resume_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_orchestrator_passes_skip_completed_markers_to_runner(tmp_path: Path) -> None:
    """CLI orchestration should pass parsed skip-completed markers into the runner."""
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text("goal: ignored\n", encoding="utf-8")
    marker_file = tmp_path / "completed.yaml"
    marker_file.write_text(
        "completed_acs:\n  - ac: 1\n    reason: Hybrid flow\n    commit: deadbee\n",
        encoding="utf-8",
    )

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
        ),
        patch(
            "ouroboros.mcp.tools.qa.QAHandler.handle",
            new_callable=AsyncMock,
            return_value=FAKE_QA_RESULT,
        ),
    ):
        mock_event_store_cls.return_value.initialize = AsyncMock()
        await _run_orchestrator(seed_file, skip_completed=str(marker_file))

    execute_kwargs = mock_runner.execute_seed.await_args.kwargs
    assert execute_kwargs["externally_satisfied_acs"] == {
        0: {"reason": "Hybrid flow", "commit": "deadbee"},
    }


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
