"""Tests for persisted post-run verification artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.evaluation.mechanical import CommandResult, MechanicalConfig
from ouroboros.evaluation.verification_artifacts import build_verification_artifacts


def _git_diff_side_effect(command: tuple[str, ...]) -> str:
    if command[:3] == ("git", "status", "--short"):
        return " M src/ouroboros/example.py\n?? tests/unit/test_example.py\n"
    if command[:3] == ("git", "diff", "--stat"):
        return (
            " src/ouroboros/example.py | 4 ++--\n"
            " tests/unit/test_example.py | 8 ++++++++\n"
            " 2 files changed, 10 insertions(+), 2 deletions(-)\n"
        )
    raise AssertionError(f"Unexpected git command: {command}")


class TestBuildVerificationArtifacts:
    """Tests for raw-log verification artifact generation."""

    @pytest.mark.asyncio
    async def test_persists_raw_outputs_and_renders_canonical_summary(
        self,
        tmp_path: Path,
    ) -> None:
        """The rendered artifact should include canonical commands and raw log paths."""
        config = MechanicalConfig(
            lint_command=("uv", "run", "ruff", "check", "."),
            test_command=("uv", "run", "pytest", "--tb=short", "-q"),
            timeout_seconds=30,
            working_dir=tmp_path,
        )

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            if command and command[0] == "git":
                return CommandResult(0, _git_diff_side_effect(command), "")
            if "ruff" in command:
                return CommandResult(0, "All checks passed!\n", "")
            if "pytest" in command:
                return CommandResult(
                    0,
                    (
                        "tests/unit/test_example.py::test_flow PASSED\n"
                        "tests/unit/test_example.py::test_edge_case PASSED\n"
                        "2 passed in 0.12s\n"
                    ),
                    "",
                )
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            artifacts = await build_verification_artifacts(
                "exec_test123",
                "Execution completed successfully.",
                tmp_path,
            )

        assert "Integrated Verification: present" in artifacts.artifact
        assert "Canonical Test Command: uv run pytest --tb=short -q" in artifacts.artifact
        assert "- src/ouroboros/example.py" in artifacts.artifact
        assert "2 passed in 0.12s" in artifacts.artifact
        assert "Stdout Log:" in artifacts.reference
        assert "git status --short" in artifacts.reference

        manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
        assert manifest["execution_id"] == "exec_test123"
        assert manifest["has_integrated_verification"] is True
        assert manifest["changed_files"] == [
            "src/ouroboros/example.py",
            "tests/unit/test_example.py",
        ]
        assert len(manifest["runs"]) == 2
        assert (
            Path(manifest["runs"][1]["stdout_path"])
            .read_text(encoding="utf-8")
            .endswith("2 passed in 0.12s\n")
        )

    @pytest.mark.asyncio
    async def test_marks_missing_integrated_verification_explicitly(
        self,
        tmp_path: Path,
    ) -> None:
        """If no canonical test command exists, the artifact should say so directly."""
        config = MechanicalConfig(
            lint_command=("uv", "run", "ruff", "check", "."),
            timeout_seconds=30,
            working_dir=tmp_path,
        )

        async def fake_run_command(
            command: tuple[str, ...],
            timeout: int,  # noqa: ARG001
            working_dir: Path | None = None,  # noqa: ARG001
        ) -> CommandResult:
            if command and command[0] == "git":
                return CommandResult(0, _git_diff_side_effect(command), "")
            if "ruff" in command:
                return CommandResult(0, "All checks passed!\n", "")
            raise AssertionError(f"Unexpected command: {command}")

        artifact_root = tmp_path / "artifact-store"
        with (
            patch(
                "ouroboros.evaluation.verification_artifacts._ARTIFACT_BASE_DIR",
                artifact_root,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.build_mechanical_config",
                return_value=config,
            ),
            patch(
                "ouroboros.evaluation.verification_artifacts.run_command",
                new=AsyncMock(side_effect=fake_run_command),
            ),
        ):
            artifacts = await build_verification_artifacts("exec_no_test", "", tmp_path)

        assert "Integrated Verification: missing" in artifacts.artifact
        assert "Canonical Test Command: (not detected)" in artifacts.artifact

        manifest = json.loads(Path(artifacts.manifest_path).read_text(encoding="utf-8"))
        assert manifest["has_integrated_verification"] is False
        assert len(manifest["runs"]) == 1
