"""Data models for the evaluation pipeline.

This module defines immutable data structures for all three evaluation stages.
All models use frozen dataclasses with slots for immutability and performance.

Classes:
    CheckType: Enum of mechanical check types
    CheckResult: Single mechanical check result
    MechanicalResult: Aggregated Stage 1 results
    SemanticResult: Stage 2 LLM evaluation results
    Vote: Single model vote in consensus
    VoterRole: Role in deliberative consensus
    ConsensusResult: Aggregated Stage 3 results
    DeliberationResult: Aggregated Stage 3 deliberative results
    EvaluationContext: Input context for evaluation
    EvaluationResult: Complete pipeline output
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ouroboros.events.base import BaseEvent


class VoterRole(StrEnum):
    """Roles in deliberative consensus.

    Each role has a specific perspective in the 2-round deliberation:
    - ADVOCATE: Argues in favor, finds strengths
    - DEVIL: Critical perspective using ontological questions
    - JUDGE: Weighs both sides, makes final decision
    """

    ADVOCATE = "advocate"
    DEVIL = "devil"
    JUDGE = "judge"


class CheckType(StrEnum):
    """Types of mechanical checks in Stage 1.

    Attributes:
        LINT: Code style and formatting checks
        BUILD: Compilation and build validation
        TEST: Unit and integration test execution
        STATIC: Static analysis (type checking, etc.)
        COVERAGE: Test coverage threshold verification
    """

    LINT = "lint"
    BUILD = "build"
    TEST = "test"
    STATIC = "static"
    COVERAGE = "coverage"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of a single mechanical check.

    Attributes:
        check_type: Type of check performed
        passed: Whether the check passed
        message: Human-readable result message
        details: Additional check-specific details
    """

    check_type: CheckType
    passed: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MechanicalResult:
    """Aggregated result of Stage 1 mechanical verification.

    All checks must pass for the overall result to pass.
    Coverage score is tracked separately for NFR9 compliance.

    Attributes:
        passed: True if all checks passed
        checks: Tuple of individual check results
        coverage_score: Test coverage percentage (0.0-1.0), None if not measured
    """

    passed: bool
    checks: tuple[CheckResult, ...]
    coverage_score: float | None = None

    @property
    def failed_checks(self) -> tuple[CheckResult, ...]:
        """Return only the checks that failed."""
        return tuple(c for c in self.checks if not c.passed)


@dataclass(frozen=True, slots=True)
class SemanticResult:
    """Result of Stage 2 semantic evaluation.

    Uses LLM to evaluate AC compliance, goal alignment, and drift.
    Uncertainty score determines if Stage 3 consensus is needed.

    Attributes:
        score: Overall evaluation score (0.0-1.0)
        ac_compliance: Whether acceptance criteria are met
        goal_alignment: Alignment with original goal (0.0-1.0)
        drift_score: Deviation from seed intent (0.0-1.0, lower is better)
        uncertainty: Model uncertainty about evaluation (0.0-1.0)
        reasoning: Explanation of the evaluation
    """

    score: float
    ac_compliance: bool
    goal_alignment: float
    drift_score: float
    uncertainty: float
    reasoning: str

    def __post_init__(self) -> None:
        """Validate score ranges."""
        for attr in ("score", "goal_alignment", "drift_score", "uncertainty"):
            value = getattr(self, attr)
            if not 0.0 <= value <= 1.0:
                msg = f"{attr} must be between 0.0 and 1.0, got {value}"
                raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Vote:
    """Single model vote in Stage 3 consensus.

    Attributes:
        model: Model identifier that cast the vote
        approved: Whether the model approves the output
        confidence: Model's confidence in its decision (0.0-1.0)
        reasoning: Explanation of the vote
        role: Role in deliberative consensus (optional, for deliberative mode)
    """

    model: str
    approved: bool
    confidence: float
    reasoning: str
    role: VoterRole | None = None

    def __post_init__(self) -> None:
        """Validate confidence range."""
        if not 0.0 <= self.confidence <= 1.0:
            msg = f"confidence must be between 0.0 and 1.0, got {self.confidence}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    """Aggregated result of Stage 3 multi-model consensus.

    Requires 2/3 majority for approval with minimum 3 models.

    Attributes:
        approved: True if consensus reached approval
        votes: Tuple of individual model votes
        majority_ratio: Ratio of approving votes (0.0-1.0)
        disagreements: Tuple of reasoning strings from dissenting votes
    """

    approved: bool
    votes: tuple[Vote, ...]
    majority_ratio: float
    disagreements: tuple[str, ...] = ()

    @property
    def approving_votes(self) -> int:
        """Count of votes that approved."""
        return sum(1 for v in self.votes if v.approved)

    @property
    def total_votes(self) -> int:
        """Total number of votes cast."""
        return len(self.votes)


class FinalVerdict(StrEnum):
    """Final verdict from Judge in deliberative consensus."""

    APPROVED = "approved"
    REJECTED = "rejected"
    CONDITIONAL = "conditional"


@dataclass(frozen=True, slots=True)
class JudgmentResult:
    """Result from the Judge in deliberative consensus.

    Attributes:
        verdict: Final decision (approved/rejected/conditional)
        confidence: Judge's confidence in decision (0.0-1.0)
        reasoning: Explanation of the judgment
        conditions: Conditions for approval (if conditional)
    """

    verdict: FinalVerdict
    confidence: float
    reasoning: str
    conditions: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        """Validate confidence range."""
        if not 0.0 <= self.confidence <= 1.0:
            msg = f"confidence must be between 0.0 and 1.0, got {self.confidence}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class DeliberationResult:
    """Result of 2-round deliberative consensus.

    Round 1: Advocate and Devil's Advocate present positions
    Round 2: Judge reviews both and makes final decision

    Attributes:
        final_verdict: The Judge's final decision
        advocate_position: The Advocate's vote and reasoning
        devil_position: The Devil's Advocate vote and reasoning
        judgment: The Judge's full judgment
        is_root_solution: Whether Devil confirmed this addresses root cause
    """

    final_verdict: FinalVerdict
    advocate_position: Vote
    devil_position: Vote
    judgment: JudgmentResult
    is_root_solution: bool

    @property
    def approved(self) -> bool:
        """Whether the final verdict is approval."""
        return self.final_verdict == FinalVerdict.APPROVED

    @property
    def has_conditions(self) -> bool:
        """Whether approval is conditional."""
        return self.final_verdict == FinalVerdict.CONDITIONAL


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """Input context for the evaluation pipeline.

    Attributes:
        execution_id: Unique identifier for the execution
        seed_id: Identifier of the seed being evaluated against
        current_ac: The acceptance criterion being evaluated
        artifact: The output artifact to evaluate
        artifact_type: Type of artifact (code, document, etc.)
        goal: Original goal from seed
        constraints: Constraints from seed
    """

    execution_id: str
    seed_id: str
    current_ac: str
    artifact: str
    artifact_type: str = "code"
    goal: str = ""
    constraints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """Complete evaluation pipeline result.

    Contains results from all stages that were executed,
    final approval status, and generated events for audit trail.

    Attributes:
        execution_id: Execution identifier for tracing
        stage1_result: Mechanical verification result (if executed)
        stage2_result: Semantic evaluation result (if executed)
        stage3_result: Consensus result (if triggered)
        final_approved: Overall approval status
        events: List of events generated during evaluation
    """

    execution_id: str
    stage1_result: MechanicalResult | None = None
    stage2_result: SemanticResult | None = None
    stage3_result: ConsensusResult | None = None
    final_approved: bool = False
    events: list[BaseEvent] = field(default_factory=list)

    @property
    def highest_stage_completed(self) -> int:
        """Return the highest stage number that completed."""
        if self.stage3_result is not None:
            return 3
        if self.stage2_result is not None:
            return 2
        if self.stage1_result is not None:
            return 1
        return 0

    @property
    def failure_reason(self) -> str | None:
        """Return the reason for failure, if any."""
        if self.final_approved:
            return None
        if self.stage1_result and not self.stage1_result.passed:
            failed = self.stage1_result.failed_checks
            return f"Stage 1 failed: {', '.join(c.check_type for c in failed)}"
        if self.stage2_result and not self.stage2_result.ac_compliance:
            return f"Stage 2 failed: AC non-compliance (score={self.stage2_result.score:.2f})"
        if self.stage3_result and not self.stage3_result.approved:
            return (
                f"Stage 3 failed: Consensus not reached ({self.stage3_result.majority_ratio:.0%})"
            )
        return "Unknown failure"
