"""Regression tests for PR #317 review findings.

Covers all 6 blocking findings from ouroboros-agent[bot] review:
1. trigger_consensus lost when trigger_context pre-populated
2. trigger_consensus ignored when stage2_enabled=False
3+4. failure_reason misreports Stage 3 rejection as Stage 2
5. working_dir artifact collection (tested at unit level)
6. Spaced paths in _project_dir_from_artifact regex

See: https://github.com/Q00/ouroboros/pull/317#pullrequestreview-4058688105
"""

from __future__ import annotations

from pathlib import Path
import re
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.types import Result
from ouroboros.evaluation.models import (
    ConsensusResult,
    EvaluationContext,
    EvaluationResult,
    SemanticResult,
    Vote,
)
from ouroboros.evaluation.pipeline import EvaluationPipeline, PipelineConfig
from ouroboros.evaluation.trigger import TriggerContext
from ouroboros.mcp.server.adapter import _project_dir_from_artifact

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_semantic(score: float = 0.72, compliance: bool = True) -> SemanticResult:
    return SemanticResult(
        score=score,
        ac_compliance=compliance,
        goal_alignment=score,
        drift_score=0.1,
        uncertainty=0.2,
        reasoning="test",
    )


def _make_consensus(approved: bool) -> ConsensusResult:
    return ConsensusResult(
        approved=approved,
        votes=(
            Vote(model="m1", approved=approved, confidence=0.9, reasoning="ok"),
            Vote(model="m2", approved=approved, confidence=0.8, reasoning="ok"),
            Vote(model="m3", approved=not approved, confidence=0.6, reasoning="dissent"),
        ),
        majority_ratio=0.67 if approved else 0.33,
    )


def _pipeline(
    *,
    stage2_enabled: bool = True,
    stage3_enabled: bool = True,
    stage2_score: float = 0.72,
    stage2_compliance: bool = True,
    consensus_approved: bool = True,
) -> EvaluationPipeline:
    config = PipelineConfig(
        stage1_enabled=False,
        stage2_enabled=stage2_enabled,
        stage3_enabled=stage3_enabled,
    )
    p = EvaluationPipeline(llm_adapter=MagicMock(), config=config)
    p._semantic = MagicMock()
    p._semantic.evaluate = AsyncMock(
        return_value=Result.ok((_make_semantic(stage2_score, stage2_compliance), []))
    )
    p._consensus = MagicMock()
    p._consensus.evaluate = AsyncMock(
        return_value=Result.ok((_make_consensus(consensus_approved), []))
    )
    return p


def _context(trigger: bool = False) -> EvaluationContext:
    return EvaluationContext(
        execution_id="e1",
        seed_id="s1",
        current_ac="ac1",
        artifact="code",
        trigger_consensus=trigger,
    )


# ---------------------------------------------------------------------------
# Finding 1: trigger_consensus lost when trigger_context pre-populated
# ---------------------------------------------------------------------------


class TestFinding1PrePopulatedTriggerContext:
    """trigger_consensus must be merged into a caller-supplied TriggerContext."""

    @pytest.mark.asyncio
    async def test_trigger_consensus_merged_into_existing_context(self) -> None:
        """Pre-populated TriggerContext + trigger_consensus=True → Stage 3 fires."""
        p = _pipeline(consensus_approved=True)
        ctx = _context(trigger=True)

        # Caller passes TriggerContext with drift data but without manual flag
        existing_tc = TriggerContext(
            execution_id="e1",
            drift_score=0.5,
            manual_consensus_request=False,
        )

        result = await p.evaluate(ctx, trigger_context=existing_tc)
        assert result.is_ok
        assert result.value.final_approved is True
        p._consensus.evaluate.assert_called_once()

    @pytest.mark.asyncio
    async def test_existing_context_with_manual_true_preserved(self) -> None:
        """If TriggerContext already has manual=True, don't break it."""
        p = _pipeline(consensus_approved=True)
        ctx = _context(trigger=True)

        existing_tc = TriggerContext(
            execution_id="e1",
            manual_consensus_request=True,
        )

        result = await p.evaluate(ctx, trigger_context=existing_tc)
        assert result.is_ok
        assert result.value.final_approved is True
        p._consensus.evaluate.assert_called_once()

    @pytest.mark.asyncio
    async def test_existing_context_without_trigger_consensus_not_modified(self) -> None:
        """Pre-populated TriggerContext + trigger_consensus=False → normal path."""
        p = _pipeline(stage2_score=0.72, consensus_approved=False)
        ctx = _context(trigger=False)

        # No trigger conditions met → Stage 3 should not fire
        existing_tc = TriggerContext(
            execution_id="e1",
            drift_score=0.0,
            uncertainty_score=0.0,
            manual_consensus_request=False,
        )

        result = await p.evaluate(ctx, trigger_context=existing_tc)
        assert result.is_ok
        # Low score, no trigger → rejected without Stage 3
        assert result.value.final_approved is False
        p._consensus.evaluate.assert_not_called()


# ---------------------------------------------------------------------------
# Finding 2: trigger_consensus ignored when stage2_enabled=False
# ---------------------------------------------------------------------------


class TestFinding2Stage2Disabled:
    """trigger_consensus=True + stage2_enabled=False → Stage 3 must still run."""

    @pytest.mark.asyncio
    async def test_stage2_disabled_trigger_consensus_fires_stage3(self) -> None:
        p = _pipeline(
            stage2_enabled=False,
            stage3_enabled=True,
            consensus_approved=True,
        )
        ctx = _context(trigger=True)

        result = await p.evaluate(ctx)
        assert result.is_ok
        assert result.value.final_approved is True
        p._consensus.evaluate.assert_called_once()
        # Stage 2 never ran
        assert result.value.stage2_result is None

    @pytest.mark.asyncio
    async def test_stage2_disabled_no_trigger_skips_stage3(self) -> None:
        """Without trigger_consensus, stage2_disabled means no Stage 3 either."""
        p = _pipeline(
            stage2_enabled=False,
            stage3_enabled=True,
            consensus_approved=True,
        )
        ctx = _context(trigger=False)

        result = await p.evaluate(ctx)
        assert result.is_ok
        # No Stage 2 data, no manual trigger → approved by default
        assert result.value.final_approved is True
        p._consensus.evaluate.assert_not_called()


# ---------------------------------------------------------------------------
# Finding 3+4: failure_reason ordering — Stage 3 must take priority
# ---------------------------------------------------------------------------


class TestFinding3And4FailureReasonOrdering:
    """When Stage 3 rejects, failure_reason must say Stage 3, not Stage 2."""

    @pytest.mark.asyncio
    async def test_pipeline_build_result_stage3_rejection(self) -> None:
        """Pipeline._build_result reports Stage 3 when Stage 3 rejected."""
        p = _pipeline(
            stage2_score=0.50,
            stage2_compliance=False,
            consensus_approved=False,
        )
        ctx = _context(trigger=True)

        result = await p.evaluate(ctx)
        assert result.is_ok
        eval_result = result.value
        assert eval_result.final_approved is False
        assert eval_result.stage3_result is not None
        # Critical: must say Stage 3, NOT Stage 2
        assert "Stage 3 failed" in (eval_result.failure_reason or "")

    def test_model_failure_reason_stage3_over_stage2(self) -> None:
        """EvaluationResult.failure_reason property also prioritises Stage 3."""
        s2 = _make_semantic(score=0.50, compliance=False)
        s3 = _make_consensus(approved=False)

        result = EvaluationResult(
            execution_id="e1",
            stage2_result=s2,
            stage3_result=s3,
            final_approved=False,
        )

        assert result.failure_reason is not None
        assert "Stage 3 failed" in result.failure_reason
        assert "Stage 2" not in result.failure_reason

    def test_model_failure_reason_stage2_when_no_stage3(self) -> None:
        """Without Stage 3, Stage 2 failure is correctly reported."""
        s2 = _make_semantic(score=0.50, compliance=False)

        result = EvaluationResult(
            execution_id="e1",
            stage2_result=s2,
            final_approved=False,
        )

        assert result.failure_reason is not None
        assert "Stage 2 failed" in result.failure_reason

    def test_model_failure_reason_approved_returns_none(self) -> None:
        result = EvaluationResult(
            execution_id="e1",
            final_approved=True,
        )
        assert result.failure_reason is None


# ---------------------------------------------------------------------------
# Finding 6: Spaced paths in _project_dir_from_artifact
# ---------------------------------------------------------------------------


class TestFinding6SpacedPaths:
    """_project_dir_from_artifact handles paths with spaces (quoted)."""

    def test_quoted_path_with_spaces_matched(self) -> None:
        """Regex matches quoted paths containing spaces."""
        pattern = r'(?:Write|Edit|File): (?:"([^"]+)"|(/[^\s]+))'
        matches = re.finditer(pattern, 'File: "/home/my user/project/main.py"')
        paths = [m.group(1) or m.group(2) for m in matches]
        assert paths == ["/home/my user/project/main.py"]

    def test_unquoted_path_still_works(self) -> None:
        pattern = r'(?:Write|Edit|File): (?:"([^"]+)"|(/[^\s]+))'
        matches = re.finditer(pattern, "Write: /home/user/project/main.py")
        paths = [m.group(1) or m.group(2) for m in matches]
        assert paths == ["/home/user/project/main.py"]

    def test_project_dir_from_artifact_quoted_path(self) -> None:
        """Integration: quoted path with spaces resolves to project root."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create project structure with space in path
            project = Path(tmpdir) / "my project"
            project.mkdir()
            (project / ".git").mkdir()
            src = project / "src"
            src.mkdir()

            artifact = f'File: "{src}/main.py"'
            result = _project_dir_from_artifact(artifact)
            assert result == str(project)

    def test_project_dir_from_artifact_unquoted_no_regression(self) -> None:
        """Existing non-spaced paths still work."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "myproject"
            project.mkdir()
            (project / "go.mod").touch()
            src = project / "cmd"
            src.mkdir()

            artifact = f"Edit: {src}/main.go"
            result = _project_dir_from_artifact(artifact)
            assert result == str(project)
