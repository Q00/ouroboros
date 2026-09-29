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

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ouroboros.core.acceptance import AcceptanceState
from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.events.base import BaseEvent

# Above this reward-hacking risk the final gate vetoes an otherwise-passing
# artifact.  It is deliberately high (0.7): it only vetoes *high-confidence*
# gaming signals so a nervous evaluator's mild suspicion never blocks a
# genuine pass.  Defined here (not in pipeline.py) so the gate and the
# ``EvaluationResult.failure_reason`` surface share one source of truth
# without a models<->pipeline import cycle.  pipeline.py imports this.
REWARD_HACKING_VETO_THRESHOLD = 0.7

# Below this Stage 2 score, semantic review withholds approval.  It never
# grants approval above it: acceptance requires executed Stage 1 evidence.
# Shared with ``pipeline.py`` so the gate and the failure reason that has to
# name it cannot drift apart.
SEMANTIC_APPROVAL_SCORE = 0.8


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
    CHECK_PACKAGE = "check_package"
    """The controller's recorded check package decision for one criterion, not a command."""


COMMAND_CHECK_TYPES: tuple[CheckType, ...] = (
    CheckType.LINT,
    CheckType.BUILD,
    CheckType.TEST,
    CheckType.STATIC,
    CheckType.COVERAGE,
)
"""The checks Stage 1 runs as project commands; the same for every criterion."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of a single mechanical check.

    Attributes:
        check_type: Type of check performed
        passed: Whether the check passed
        message: Human-readable result message
        details: Additional check-specific details
        executed: Whether a configured command was attempted, so that
            ``passed`` reports its outcome. An unconfigured check is reported
            as ``passed`` so it does not fail Stage 1, but it is not evidence.
            Defaults to ``False`` so a check that does not declare execution
            never counts as evidence.
    """

    check_type: CheckType
    passed: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    executed: bool = False


class MechanicalDisposition(StrEnum):
    """What a Stage 1 result is evidence of; the one classifier every reader uses.

    Attributes:
        EXECUTED_PASS: At least one check ran a command and every check passed.
            The only disposition that can support acceptance.
        EXECUTED_FAIL: A check that ran a command failed. Authoritative
            rejection; model review is not consulted.
        NO_EVIDENCE: No executed check decided anything: every check was
            skipped, or the only failures were never executed. Not approvable
            and not a rejection; the outcome is unverified and model review
            still runs to supply feedback.
    """

    EXECUTED_PASS = "executed_pass"
    EXECUTED_FAIL = "executed_fail"
    NO_EVIDENCE = "no_evidence"


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

    def __post_init__(self) -> None:
        """Reconcile the aggregate verdict with its checks, fail-closed.

        ``passed`` duplicates what the checks already say, and a directly
        constructed or rehydrated result can make the two disagree. The
        aggregate may only claim a pass that every check confirms with a
        literal ``True``; any disagreement resolves to not passed, so no
        reader (the pipeline's Stage 1 gate, the acceptance gate, failure
        reasons) can see an executed failing check as a pass.
        """
        reconciled = self.passed is True and all(c.passed is True for c in self.checks)
        if reconciled is not self.passed:
            object.__setattr__(self, "passed", reconciled)

    @property
    def failed_checks(self) -> tuple[CheckResult, ...]:
        """Return only the checks that did not report a literal pass."""
        return tuple(c for c in self.checks if c.passed is not True)

    @property
    def executed_checks(self) -> tuple[CheckResult, ...]:
        """Return only the checks whose configured command actually ran."""
        return tuple(c for c in self.checks if c.executed is True)

    @property
    def executed_failures(self) -> tuple[CheckResult, ...]:
        """Return the checks that ran a command and did not pass."""
        return tuple(c for c in self.failed_checks if c.executed is True)

    @property
    def disposition(self) -> MechanicalDisposition:
        """Classify this result once, for the pipeline and every projection.

        An executed failure rejects. A pass counts only when every check
        passed and at least one ran a command (a skipped check reports
        ``passed`` but is not evidence). Everything else, including a failure
        that was never executed, is no evidence.
        """
        if self.executed_failures:
            return MechanicalDisposition.EXECUTED_FAIL
        if self.passed and self.executed_checks:
            return MechanicalDisposition.EXECUTED_PASS
        return MechanicalDisposition.NO_EVIDENCE

    def with_recorded(self, recorded: tuple[CheckResult, ...]) -> "MechanicalResult":
        """This result plus checks the controller already ran for one criterion.

        ``recorded`` holds ``CHECK_PACKAGE`` results only; the one classifier
        (``disposition``) then decides over the command checks and them alike.
        """
        if any(check.check_type is not CheckType.CHECK_PACKAGE for check in recorded):
            raise ValueError("only check package results are recorded evidence")
        checks = self.command_checks().checks + recorded
        return MechanicalResult(
            passed=all(check.passed is True for check in checks),
            checks=checks,
            coverage_score=self.coverage_score,
        )

    def command_checks(self) -> "MechanicalResult":
        """This result without recorded criterion evidence: what every criterion shares."""
        checks = tuple(c for c in self.checks if c.check_type is not CheckType.CHECK_PACKAGE)
        return MechanicalResult(
            passed=all(check.passed is True for check in checks),
            checks=checks,
            coverage_score=self.coverage_score,
        )

    @property
    def has_executed_evidence(self) -> bool:
        """Whether Stage 1 is executed verification evidence for acceptance."""
        return self.disposition is MechanicalDisposition.EXECUTED_PASS


@dataclass(frozen=True, slots=True)
class SemanticResult:
    """Result of Stage 2 semantic evaluation.

    Uses LLM to evaluate AC compliance, goal alignment, drift, and
    reward-hacking risk.  Uncertainty score determines if Stage 3
    consensus is needed.

    Attributes:
        score: Overall evaluation score (0.0-1.0)
        ac_compliance: Whether acceptance criteria are met
        goal_alignment: Alignment with original goal (0.0-1.0)
        drift_score: Deviation from seed intent (0.0-1.0, lower is better)
        uncertainty: Model uncertainty about evaluation (0.0-1.0)
        reasoning: Explanation of the evaluation
        reward_hacking_risk: Suspicion that the artifact games the
            evaluator rather than solving the real task (0.0-1.0).
            Distinct from drift_score.
        questions_used: Socratic / ontology-gap questions the evaluator
            actually asked while verifying the artifact.  Exposing these
            to the user is an anti-reward-hacking mechanism (#367) —
            the evaluator has to show its work.
        evidence: Concrete evidence (file snippets, behavior observations,
            etc.) the evaluator relied on when deciding the verdict.
    """

    score: float
    ac_compliance: bool
    goal_alignment: float
    drift_score: float
    uncertainty: float
    reasoning: str
    reward_hacking_risk: float = 0.0
    questions_used: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate score ranges."""
        for attr in (
            "score",
            "goal_alignment",
            "drift_score",
            "uncertainty",
            "reward_hacking_risk",
        ):
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
    is_single_model: bool = False
    # PR-X X2: honest label of executor/reviewer independence
    # ("independent" | "same_vendor" | "unavailable" | "unverified" |
    # None when not resolved). See evaluation.reviewer_independence.
    reviewer_independence: str | None = None

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
class FileArtifact:
    """A single file collected from execution output.

    Attributes:
        file_path: Absolute path to the file
        content: File content (may be truncated)
        ac_indices: Which ACs modified this file
        truncated: Whether content was truncated to fit token budget
    """

    file_path: str
    content: str
    ac_indices: tuple[int, ...] = ()
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactBundle:
    """Bundle of file artifacts collected from execution.

    Provides actual source code to the semantic evaluator instead of
    relying solely on agent text summaries.

    Attributes:
        files: Collected file artifacts
        text_summary: Original text summary (backward compat)
        total_chars: Total characters across all files
    """

    files: tuple[FileArtifact, ...] = ()
    text_summary: str = ""
    total_chars: int = 0


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """Input context for the evaluation pipeline.

    Attributes:
        execution_id: Unique identifier for the execution
        seed_id: Identifier of the seed being evaluated against
        current_ac: The acceptance criterion being evaluated
        current_ac_spec: Structured success contract for ``current_ac`` when
            the seed declared one (verify_command / expected_artifacts /
            output_assertion).  ``None`` for bare-string ACs — the evaluator
            then falls back to judging ``current_ac`` text alone (today's
            behavior).
        artifact: The output artifact to evaluate
        artifact_type: Type of artifact (code, document, etc.)
        goal: Original goal from seed
        constraints: Constraints from seed
        artifact_bundle: Optional file-based artifacts for richer evaluation
    """

    execution_id: str
    seed_id: str
    current_ac: str
    artifact: str
    current_ac_spec: AcceptanceCriterionSpec | None = None
    artifact_type: str = "code"
    goal: str = ""
    constraints: tuple[str, ...] = ()
    artifact_bundle: ArtifactBundle | None = None
    trigger_consensus: bool = False
    # PR-X X2: the runtime backend that produced ``artifact``, when known. Lets
    # consensus keep the executor's own vendor out of the reviewer jury. ``None``
    # (the default) means "unknown" — today's behavior, no independence binding.
    executor_backend: str | None = None
    # Checks the controller already ran for ``current_ac`` (its recorded check
    # package decision, ``CheckType.CHECK_PACKAGE``). Stage 1 adds them to the
    # project command checks for this criterion only.
    recorded_checks: tuple[CheckResult, ...] = ()


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
    def has_executed_evidence(self) -> bool:
        """Whether Stage 1 produced executed verification evidence."""
        return self.stage1_result is not None and self.stage1_result.has_executed_evidence

    @property
    def acceptance_state(self) -> AcceptanceState:
        """Return whether this result is approved, rejected, or unverified."""
        return derive_acceptance_state(
            final_approved=self.final_approved,
            stage1_result=self.stage1_result,
        )

    @property
    def failure_reason(self) -> str | None:
        """Return the reason for failure, if any."""
        return build_failure_reason(
            final_approved=self.final_approved,
            stage1_result=self.stage1_result,
            stage2_result=self.stage2_result,
            stage3_result=self.stage3_result,
        )


def model_review_withhold_reason(
    *,
    stage2_result: SemanticResult | None,
    stage3_result: ConsensusResult | None,
) -> str | None:
    """Return why model review withheld approval, or ``None`` if it did not.

    Model review (Stage 2 semantic, Stage 3 consensus, and the reward-hacking
    veto) is advisory: it can withhold approval and explain why, but it can
    never grant acceptance. Every stage that ran is consulted and any one of
    them withholding settles it; a Stage 3 approval does not lift a Stage 2
    block. The first withholding gate is named, in the order below.
    """
    if stage3_result is not None and not stage3_result.approved:
        return f"Stage 3 failed: Consensus not reached ({stage3_result.majority_ratio:.0%})"
    if stage2_result is not None and not stage2_result.ac_compliance:
        return (
            "Stage 2 failed: AC non-compliance "
            f"(ac_compliance=false; semantic score {stage2_result.score:.2f} "
            "did not gate this)"
        )
    if stage2_result is not None and stage2_result.score < SEMANTIC_APPROVAL_SCORE:
        return (
            "Stage 2 failed: semantic score "
            f"{stage2_result.score:.2f} < {SEMANTIC_APPROVAL_SCORE:.2f} "
            "(ac_compliance=true; the score gate decided this)"
        )
    if (
        stage2_result is not None
        and stage2_result.reward_hacking_risk >= REWARD_HACKING_VETO_THRESHOLD
    ):
        return (
            "Stage 2 veto: reward-hacking risk "
            f"{stage2_result.reward_hacking_risk:.2f} >= "
            f"{REWARD_HACKING_VETO_THRESHOLD:.2f}; artifact appears optimized to game the "
            "evaluator rather than solve the real task"
        )
    return None


def decide_final_approval(
    *,
    stage1_result: MechanicalResult | None,
    stage2_result: SemanticResult | None,
    stage3_result: ConsensusResult | None,
) -> bool:
    """The single acceptance gate: executed evidence grants, model review withholds.

    Approval requires Stage 1 to have run at least one configured check with
    every check passing. Model review that ran may withhold that approval;
    no model verdict can supply it.
    """
    if stage1_result is None or not stage1_result.has_executed_evidence:
        return False
    return (
        model_review_withhold_reason(
            stage2_result=stage2_result,
            stage3_result=stage3_result,
        )
        is None
    )


def derive_acceptance_state(
    *,
    final_approved: bool,
    stage1_result: MechanicalResult | None,
) -> AcceptanceState:
    """Classify an evaluation outcome for callers and feedback loops."""
    if final_approved:
        return AcceptanceState.APPROVED
    if stage1_result is None:
        return AcceptanceState.UNVERIFIED
    disposition = stage1_result.disposition
    if disposition is MechanicalDisposition.NO_EVIDENCE:
        return AcceptanceState.UNVERIFIED
    # EXECUTED_FAIL, or EXECUTED_PASS that model review withheld.
    return AcceptanceState.REJECTED


def aggregate_acceptance_state(states: Sequence[AcceptanceState]) -> AcceptanceState:
    """The run's state from its criteria' states (each from ``derive_acceptance_state``).

    Approved only when every criterion is; rejected when any criterion was
    rejected on executed evidence or model review; otherwise unverified.
    """
    if states and all(state is AcceptanceState.APPROVED for state in states):
        return AcceptanceState.APPROVED
    if any(state is AcceptanceState.REJECTED for state in states):
        return AcceptanceState.REJECTED
    return AcceptanceState.UNVERIFIED


def build_failure_reason(
    *,
    final_approved: bool,
    stage1_result: MechanicalResult | None,
    stage2_result: SemanticResult | None,
    stage3_result: ConsensusResult | None,
) -> str | None:
    """Return why an evaluation was not approved, or ``None`` when it was.

    A failed executed check is named first because it is the decisive
    evidence. Without executed evidence the result is unverified, and the
    reason says so before appending whatever model review concluded, so the
    feedback is not mistaken for a verdict. With executed evidence that
    passed, the withholding model gate is named.
    """
    if final_approved:
        return None
    disposition = stage1_result.disposition if stage1_result is not None else None
    if stage1_result is not None and disposition is MechanicalDisposition.EXECUTED_FAIL:
        failed = stage1_result.executed_failures
        return f"Stage 1 failed: {', '.join(c.check_type for c in failed)}"

    model_reason = model_review_withhold_reason(
        stage2_result=stage2_result,
        stage3_result=stage3_result,
    )
    if stage1_result is None or disposition is MechanicalDisposition.NO_EVIDENCE:
        if stage1_result is None:
            missing = "Stage 1 did not run"
        elif stage1_result.failed_checks:
            missing = "Stage 1 reported failures from no executed check"
        else:
            missing = "Stage 1 ran no configured check"
        reason = f"Not approved: unverified. No executed verification evidence ({missing})."
        if model_reason is not None:
            return f"{reason} Advisory model review also withheld approval: {model_reason}"
        if stage2_result is not None or stage3_result is not None:
            return (
                f"{reason} Advisory model review raised no objection, but cannot grant acceptance."
            )
        return reason
    return model_reason or "Unknown failure"
