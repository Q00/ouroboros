"""Tests for evaluation data models."""

import pytest

from ouroboros.evaluation.models import (
    CheckResult,
    CheckType,
    ConsensusResult,
    EvaluationContext,
    EvaluationResult,
    MechanicalResult,
    SemanticResult,
    Vote,
)


class TestCheckType:
    """Tests for CheckType enum."""

    def test_check_type_values(self) -> None:
        """Verify all check types have correct string values."""
        assert CheckType.LINT == "lint"
        assert CheckType.BUILD == "build"
        assert CheckType.TEST == "test"
        assert CheckType.STATIC == "static"
        assert CheckType.COVERAGE == "coverage"

    def test_check_type_is_str_enum(self) -> None:
        """CheckType should be usable as string."""
        assert f"Running {CheckType.LINT}" == "Running lint"


class TestCheckResult:
    """Tests for CheckResult dataclass."""

    def test_creation_minimal(self) -> None:
        """Create CheckResult with minimal arguments."""
        result = CheckResult(
            check_type=CheckType.LINT,
            passed=True,
            message="Lint passed",
        )
        assert result.check_type == CheckType.LINT
        assert result.passed is True
        assert result.message == "Lint passed"
        assert result.details == {}

    def test_creation_with_details(self) -> None:
        """Create CheckResult with details."""
        details = {"return_code": 0, "output": "OK"}
        result = CheckResult(
            check_type=CheckType.TEST,
            passed=False,
            message="Tests failed",
            details=details,
        )
        assert result.details == details

    def test_immutability(self) -> None:
        """CheckResult should be immutable."""
        result = CheckResult(
            check_type=CheckType.BUILD,
            passed=True,
            message="Build OK",
        )
        with pytest.raises(AttributeError):
            result.passed = False  # type: ignore[misc]


class TestMechanicalResult:
    """Tests for MechanicalResult dataclass."""

    def test_creation_passed(self) -> None:
        """Create passed MechanicalResult."""
        checks = (
            CheckResult(CheckType.LINT, True, "OK"),
            CheckResult(CheckType.TEST, True, "OK"),
        )
        result = MechanicalResult(passed=True, checks=checks)
        assert result.passed is True
        assert len(result.checks) == 2
        assert result.coverage_score is None

    def test_creation_with_coverage(self) -> None:
        """Create MechanicalResult with coverage score."""
        result = MechanicalResult(
            passed=True,
            checks=(),
            coverage_score=0.85,
        )
        assert result.coverage_score == 0.85

    def test_failed_checks_property(self) -> None:
        """Test failed_checks property returns only failures."""
        checks = (
            CheckResult(CheckType.LINT, True, "OK"),
            CheckResult(CheckType.BUILD, False, "Failed"),
            CheckResult(CheckType.TEST, False, "Failed"),
        )
        result = MechanicalResult(passed=False, checks=checks)
        failed = result.failed_checks
        assert len(failed) == 2
        assert all(not c.passed for c in failed)


class TestSemanticResult:
    """Tests for SemanticResult dataclass."""

    def test_creation_valid(self) -> None:
        """Create valid SemanticResult."""
        result = SemanticResult(
            score=0.85,
            ac_compliance=True,
            goal_alignment=0.9,
            drift_score=0.1,
            uncertainty=0.2,
            reasoning="Good implementation",
        )
        assert result.score == 0.85
        assert result.ac_compliance is True
        assert result.uncertainty == 0.2

    def test_validation_score_range(self) -> None:
        """Score must be between 0 and 1."""
        with pytest.raises(ValueError, match="score must be between"):
            SemanticResult(
                score=1.5,
                ac_compliance=True,
                goal_alignment=0.9,
                drift_score=0.1,
                uncertainty=0.2,
                reasoning="Test",
            )

    def test_validation_negative_score(self) -> None:
        """Negative scores should fail validation."""
        with pytest.raises(ValueError, match="uncertainty must be between"):
            SemanticResult(
                score=0.5,
                ac_compliance=True,
                goal_alignment=0.9,
                drift_score=0.1,
                uncertainty=-0.1,
                reasoning="Test",
            )


class TestVote:
    """Tests for Vote dataclass."""

    def test_creation_valid(self) -> None:
        """Create valid Vote."""
        vote = Vote(
            model="gpt-4o",
            approved=True,
            confidence=0.95,
            reasoning="Meets all criteria",
        )
        assert vote.model == "gpt-4o"
        assert vote.approved is True
        assert vote.confidence == 0.95

    def test_validation_confidence_range(self) -> None:
        """Confidence must be between 0 and 1."""
        with pytest.raises(ValueError, match="confidence must be between"):
            Vote(
                model="test",
                approved=True,
                confidence=1.5,
                reasoning="Test",
            )


class TestConsensusResult:
    """Tests for ConsensusResult dataclass."""

    def test_creation_approved(self) -> None:
        """Create approved ConsensusResult."""
        votes = (
            Vote("model-a", True, 0.9, "Good"),
            Vote("model-b", True, 0.85, "Good"),
            Vote("model-c", False, 0.7, "Concerns"),
        )
        result = ConsensusResult(
            approved=True,
            votes=votes,
            majority_ratio=0.67,
            disagreements=("Concerns",),
        )
        assert result.approved is True
        assert result.approving_votes == 2
        assert result.total_votes == 3

    def test_properties(self) -> None:
        """Test computed properties."""
        votes = (
            Vote("a", True, 0.9, "OK"),
            Vote("b", True, 0.9, "OK"),
            Vote("c", True, 0.9, "OK"),
        )
        result = ConsensusResult(approved=True, votes=votes, majority_ratio=1.0)
        assert result.approving_votes == 3
        assert result.total_votes == 3
        assert result.disagreements == ()


class TestEvaluationContext:
    """Tests for EvaluationContext dataclass."""

    def test_creation_minimal(self) -> None:
        """Create with minimal required fields."""
        ctx = EvaluationContext(
            execution_id="exec-1",
            seed_id="seed-1",
            current_ac="User can login",
            artifact="def login(): pass",
        )
        assert ctx.execution_id == "exec-1"
        assert ctx.artifact_type == "code"
        assert ctx.constraints == ()

    def test_creation_full(self) -> None:
        """Create with all fields."""
        ctx = EvaluationContext(
            execution_id="exec-1",
            seed_id="seed-1",
            current_ac="User can login",
            artifact="def login(): pass",
            artifact_type="code",
            goal="Build auth system",
            constraints=("Must be secure", "No external deps"),
        )
        assert ctx.goal == "Build auth system"
        assert len(ctx.constraints) == 2


class TestEvaluationResult:
    """Tests for EvaluationResult dataclass."""

    def test_creation_minimal(self) -> None:
        """Create minimal EvaluationResult."""
        result = EvaluationResult(execution_id="exec-1")
        assert result.final_approved is False
        assert result.highest_stage_completed == 0
        assert result.events == []

    def test_highest_stage_completed_stage1(self) -> None:
        """Test highest_stage_completed with stage1 only."""
        result = EvaluationResult(
            execution_id="exec-1",
            stage1_result=MechanicalResult(passed=True, checks=()),
        )
        assert result.highest_stage_completed == 1

    def test_highest_stage_completed_stage3(self) -> None:
        """Test highest_stage_completed with all stages."""
        result = EvaluationResult(
            execution_id="exec-1",
            stage1_result=MechanicalResult(passed=True, checks=()),
            stage2_result=SemanticResult(
                score=0.9,
                ac_compliance=True,
                goal_alignment=0.9,
                drift_score=0.1,
                uncertainty=0.4,
                reasoning="Test",
            ),
            stage3_result=ConsensusResult(
                approved=True,
                votes=(),
                majority_ratio=1.0,
            ),
        )
        assert result.highest_stage_completed == 3

    def test_failure_reason_approved(self) -> None:
        """No failure reason when approved."""
        result = EvaluationResult(
            execution_id="exec-1",
            final_approved=True,
        )
        assert result.failure_reason is None

    def test_failure_reason_stage1_failed(self) -> None:
        """Failure reason for stage1 failure."""
        result = EvaluationResult(
            execution_id="exec-1",
            stage1_result=MechanicalResult(
                passed=False,
                checks=(CheckResult(CheckType.LINT, False, "Failed"),),
            ),
        )
        assert "Stage 1 failed" in result.failure_reason
        assert "lint" in result.failure_reason

    def test_failure_reason_stage2_failed(self) -> None:
        """Failure reason for stage2 failure."""
        result = EvaluationResult(
            execution_id="exec-1",
            stage1_result=MechanicalResult(passed=True, checks=()),
            stage2_result=SemanticResult(
                score=0.3,
                ac_compliance=False,
                goal_alignment=0.4,
                drift_score=0.5,
                uncertainty=0.2,
                reasoning="Poor implementation",
            ),
        )
        assert "Stage 2 failed" in result.failure_reason
        assert "AC non-compliance" in result.failure_reason
