"""Evolve fallback evaluation: executed Stage 1 grants, semantic review is feedback.

Issue #2449. When the worker output has no ``### Task N`` markers, one semantic
verdict over every acceptance criterion used to set ``final_approved``. It is
now advisory: approval requires executed Stage 1 checks, and an unverified or
rejected generation carries the semantic review as ``feedback_metadata`` that
Wonder and Reflect render for the next generation.
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.evaluation.mechanical import MechanicalConfig
from ouroboros.evaluation.models import SemanticResult
from ouroboros.mcp.server import evolution_pipeline_evaluation as module
from ouroboros.mcp.server.adapter import _parse_legacy_execution_task_summary
from ouroboros.mcp.server.evolution_pipeline_evaluation import (
    EVOLVE_STAGE1_ENV,
    evaluate_generation_with_pipeline,
    evolve_stage1_enabled,
)
from ouroboros.mcp.server.spec_verification_adapter import (
    evaluation_summary_for_unavailable_spec_verification,
)

SEMANTIC_PATH = "ouroboros.evaluation.semantic.SemanticEvaluator.evaluate"


def _seed() -> Any:
    return SimpleNamespace(
        metadata=SimpleNamespace(seed_id="seed-evolve"),
        goal="Ship a CLI that prints its version",
        constraints=("Python 3.12",),
        acceptance_criteria=("`cli --version` prints 1.0", "Tests cover the flag"),
    )


def _semantic(*, approve: bool) -> SemanticResult:
    return SemanticResult(
        score=0.95 if approve else 0.3,
        ac_compliance=approve,
        goal_alignment=0.9,
        drift_score=0.1,
        uncertainty=0.1,
        reasoning="The version flag is wired but no test exercises it."
        if not approve
        else "All criteria appear satisfied.",
        evidence=("cli.py: parser.add_argument('--version')",),
    )


def _config(command: tuple[str, ...] | None, working_dir: Path) -> MechanicalConfig:
    return MechanicalConfig(test_command=command, working_dir=working_dir)


async def _run(
    tmp_path: Path,
    *,
    approve: bool,
    command: tuple[str, ...] | None,
    stage1_enabled: bool = True,
    project_dir: str | None = "set",
) -> tuple[Any, AsyncMock, AsyncMock]:
    resolved_dir = str(tmp_path) if project_dir == "set" else project_dir
    semantic = AsyncMock(return_value=Result.ok((_semantic(approve=approve), [])))
    mechanical = AsyncMock(return_value=_config(command, tmp_path))
    with (
        patch(SEMANTIC_PATH, semantic),
        patch.object(module, "_project_mechanical_config", mechanical),
    ):
        summary = await evaluate_generation_with_pipeline(
            seed=_seed(),
            artifact="Implemented cli.py",
            project_dir=resolved_dir,
            llm_adapter=AsyncMock(),
            semantic_model="semantic-model",
            detector_backend=None,
            stage1_enabled=stage1_enabled,
        )
    return summary, semantic, mechanical


def _feedback(summary: Any) -> dict[str, Any]:
    return {entry.code: entry for entry in summary.feedback_metadata}


PASSING = (sys.executable, "-c", "pass")
FAILING = (sys.executable, "-c", "raise SystemExit(1)")


class TestEvolveFallbackAuthority:
    @pytest.mark.asyncio
    async def test_semantic_approve_without_executed_checks_is_unverified(
        self, tmp_path: Path
    ) -> None:
        summary, semantic, _ = await _run(tmp_path, approve=True, command=None)

        assert summary.final_approved is False
        assert summary.approval_status == "not_evaluated"
        assert summary.run_verdict_passed is False
        assert (summary.failure_reason or "").startswith("Not approved: unverified.")
        semantic.assert_awaited_once()

        feedback = _feedback(summary)
        assert feedback["acceptance_unverified"].severity == "warning"
        assert feedback["acceptance_unverified"].details["executed_checks"] == 0
        review = feedback["semantic_review"]
        assert review.message == "All criteria appear satisfied."
        assert review.details["advisory"] is True
        assert review.details["withheld"] is None

    @pytest.mark.asyncio
    async def test_semantic_reject_without_executed_checks_carries_feedback(
        self, tmp_path: Path
    ) -> None:
        summary, _, _ = await _run(tmp_path, approve=False, command=None)

        assert summary.final_approved is False
        assert summary.approval_status == "not_evaluated"
        review = _feedback(summary)["semantic_review"]
        assert review.severity == "warning"
        assert review.message == "The version flag is wired but no test exercises it."
        assert "AC non-compliance" in review.details["withheld"]

    @pytest.mark.asyncio
    async def test_executed_pass_and_semantic_approve_is_approved(self, tmp_path: Path) -> None:
        summary, _, mechanical = await _run(tmp_path, approve=True, command=PASSING)

        assert summary.final_approved is True
        assert summary.approval_status == "approved"
        assert summary.failure_reason is None
        assert "acceptance_unverified" not in _feedback(summary)
        mechanical.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_executed_pass_and_semantic_reject_is_rejected(self, tmp_path: Path) -> None:
        summary, _, _ = await _run(tmp_path, approve=False, command=PASSING)

        assert summary.final_approved is False
        assert summary.approval_status == "rejected"
        assert "AC non-compliance" in (summary.failure_reason or "")
        assert "semantic_review" in _feedback(summary)

    @pytest.mark.asyncio
    async def test_failed_executed_check_rejects_without_semantic_review(
        self, tmp_path: Path
    ) -> None:
        summary, semantic, _ = await _run(tmp_path, approve=True, command=FAILING)

        assert summary.final_approved is False
        assert summary.approval_status == "rejected"
        assert (summary.failure_reason or "").startswith("Stage 1 failed")
        semantic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stage1_disabled_is_unverified_and_says_why(self, tmp_path: Path) -> None:
        summary, _, mechanical = await _run(
            tmp_path, approve=True, command=PASSING, stage1_enabled=False
        )

        assert summary.final_approved is False
        assert summary.approval_status == "not_evaluated"
        assert EVOLVE_STAGE1_ENV in _feedback(summary)["acceptance_unverified"].message
        mechanical.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unresolved_project_dir_is_unverified_and_says_why(self, tmp_path: Path) -> None:
        summary, _, mechanical = await _run(
            tmp_path, approve=True, command=PASSING, project_dir=None
        )

        assert summary.final_approved is False
        message = _feedback(summary)["acceptance_unverified"].message
        assert "project directory could not be resolved" in message
        mechanical.assert_not_awaited()


class TestProjectMechanicalConfig:
    @pytest.mark.asyncio
    async def test_failed_detection_leaves_stage1_empty(self, tmp_path: Path) -> None:
        """A detector exception is logged; Stage 1 then has no configured check."""
        with patch(
            "ouroboros.evaluation.detector.ensure_mechanical_toml",
            AsyncMock(side_effect=RuntimeError("provider down")),
        ) as ensure:
            config = await module._project_mechanical_config(tmp_path, AsyncMock(), None)

        ensure.assert_awaited_once()
        assert config.test_command is None
        assert config.working_dir == tmp_path

    @pytest.mark.asyncio
    async def test_existing_toml_is_read_without_detection(self, tmp_path: Path) -> None:
        toml = tmp_path / ".ouroboros" / "mechanical.toml"
        toml.parent.mkdir()
        toml.write_text('test = "cargo test"\n', encoding="utf-8")
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n', encoding="utf-8")
        with patch("ouroboros.evaluation.detector.ensure_mechanical_toml", AsyncMock()) as ensure:
            config = await module._project_mechanical_config(tmp_path, AsyncMock(), None)

        ensure.assert_not_awaited()
        assert config.test_command == ("cargo", "test")


class TestStage1Environment:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, True),
            ("", True),
            ("true", True),
            ("TRUE", True),
            ("1", True),
            ("false", False),
            (" False ", False),
            ("0", False),
        ],
    )
    def test_documented_values(self, raw: str | None, expected: bool) -> None:
        environ = {} if raw is None else {EVOLVE_STAGE1_ENV: raw}
        assert evolve_stage1_enabled(environ) is expected

    def test_unrecognized_value_keeps_stage1_enabled(self) -> None:
        """A malformed value never silently disables the only evidence source."""
        assert evolve_stage1_enabled({EVOLVE_STAGE1_ENV: "flase"}) is True


def test_worker_self_report_alone_never_approves() -> None:
    """Every task reported COMPLETED is still not an approval (G19)."""
    seed = SimpleNamespace(acceptance_criteria=("Implement feature", "Add tests"))
    artifact = "### Task 1: [COMPLETED] Implement feature\n### Task 2: [COMPLETED] Add tests"

    summary = _parse_legacy_execution_task_summary(artifact, seed)
    assert summary is not None
    assert summary.execution_completion_status == "completed"
    assert summary.final_approved is False

    unavailable = evaluation_summary_for_unavailable_spec_verification(
        summary, seed, "Spec assertion extraction produced no independently usable assertions."
    )
    assert unavailable.final_approved is False
    assert unavailable.run_verdict_passed is False
