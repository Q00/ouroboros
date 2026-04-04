"""Data models for parallel AC execution results.

These dataclasses and enums represent the outcome hierarchy for
parallel acceptance-criteria execution:

    ACExecutionResult → ParallelExecutionStageResult → ParallelExecutionResult

Extracted from :mod:`ouroboros.orchestrator.parallel_executor` to keep
the executor module focused on orchestration logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
    from ouroboros.orchestrator.coordinator import CoordinatorReview
    from ouroboros.orchestrator.level_context import LevelContext


class ACExecutionOutcome(str, Enum):  # noqa: UP042
    """Normalized outcome for a single AC execution."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ACExecutionResult:
    """Result of executing a single AC, including Sub-ACs if decomposed.

    Attributes:
        ac_index: 0-based AC index.
        ac_content: AC description.
        success: Whether execution succeeded.
        messages: All agent messages from execution.
        final_message: Final result message content.
        error: Error message if failed.
        duration_seconds: Execution duration.
        session_id: Claude session ID for this AC.
        retry_attempt: Retry attempt number (0 for the first execution).
        is_decomposed: Whether this AC was decomposed into Sub-ACs.
        sub_results: Results from Sub-AC parallel executions.
        depth: Depth in decomposition tree (0 = root AC).
        decomposition_depth_warning: True when decomposition was skipped because
            the soft depth safety net forced atomic execution.
        outcome: Normalized result classification for aggregation.
        runtime_handle: Backend-neutral runtime handle for same-attempt resume.
    """

    ac_index: int
    ac_content: str
    success: bool
    messages: tuple[AgentMessage, ...] = field(default_factory=tuple)
    final_message: str = ""
    error: str | None = None
    duration_seconds: float = 0.0
    session_id: str | None = None
    retry_attempt: int = 0
    is_decomposed: bool = False
    sub_results: tuple[ACExecutionResult, ...] = field(default_factory=tuple)
    depth: int = 0
    decomposition_depth_warning: bool = False
    outcome: ACExecutionOutcome | None = None
    runtime_handle: RuntimeHandle | None = None

    def __post_init__(self) -> None:
        """Normalize outcome so callers do not infer from error strings."""
        if self.outcome is None:
            object.__setattr__(self, "outcome", self._infer_outcome())

    def _infer_outcome(self) -> ACExecutionOutcome:
        if self.success:
            return ACExecutionOutcome.SUCCEEDED

        error_text = (self.error or "").lower()
        if "not included in dependency graph" in error_text:
            return ACExecutionOutcome.INVALID
        if "skipped: dependency failed" in error_text or "blocked: dependency" in error_text:
            return ACExecutionOutcome.BLOCKED
        return ACExecutionOutcome.FAILED

    @property
    def is_blocked(self) -> bool:
        """True when the AC was blocked by an upstream dependency outcome."""
        return self.outcome == ACExecutionOutcome.BLOCKED

    @property
    def is_failure(self) -> bool:
        """True when the AC executed and failed."""
        return self.outcome == ACExecutionOutcome.FAILED

    @property
    def is_invalid(self) -> bool:
        """True when the AC was not representable in the execution plan."""
        return self.outcome == ACExecutionOutcome.INVALID

    @property
    def attempt_number(self) -> int:
        """Human-readable execution attempt number (1-based)."""
        return self.retry_attempt + 1


class StageExecutionOutcome(str, Enum):  # noqa: UP042
    """Aggregate outcome for a serial execution stage."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class ParallelExecutionStageResult:
    """Aggregate result for one serial stage of AC execution."""

    stage_index: int
    ac_indices: tuple[int, ...]
    results: tuple[ACExecutionResult, ...] = field(default_factory=tuple)
    started: bool = True
    coordinator_review: CoordinatorReview | None = None

    @property
    def level_number(self) -> int:
        """Legacy 1-based level number."""
        return self.stage_index + 1

    @property
    def success_count(self) -> int:
        """Number of successful ACs in this stage."""
        return sum(1 for result in self.results if result.outcome == ACExecutionOutcome.SUCCEEDED)

    @property
    def failure_count(self) -> int:
        """Number of failed ACs in this stage."""
        return sum(1 for result in self.results if result.outcome == ACExecutionOutcome.FAILED)

    @property
    def blocked_count(self) -> int:
        """Number of dependency-blocked ACs in this stage."""
        return sum(1 for result in self.results if result.outcome == ACExecutionOutcome.BLOCKED)

    @property
    def invalid_count(self) -> int:
        """Number of invalidly planned ACs in this stage."""
        return sum(1 for result in self.results if result.outcome == ACExecutionOutcome.INVALID)

    @property
    def skipped_count(self) -> int:
        """Legacy alias for blocked and invalid ACs."""
        return self.blocked_count + self.invalid_count

    @property
    def outcome(self) -> StageExecutionOutcome:
        """Aggregate stage outcome for hybrid execution handling."""
        if not self.results:
            return (
                StageExecutionOutcome.BLOCKED
                if not self.started
                else StageExecutionOutcome.SUCCEEDED
            )
        if self.failure_count == 0 and self.blocked_count == 0 and self.invalid_count == 0:
            return StageExecutionOutcome.SUCCEEDED
        if self.success_count == 0 and self.failure_count == 0:
            return StageExecutionOutcome.BLOCKED
        if self.success_count == 0 and self.blocked_count == 0 and self.invalid_count == 0:
            return StageExecutionOutcome.FAILED
        return StageExecutionOutcome.PARTIAL

    @property
    def has_terminal_issue(self) -> bool:
        """True when the stage should block some downstream work."""
        return self.failure_count > 0 or self.blocked_count > 0


@dataclass(frozen=True, slots=True)
class ParallelExecutionResult:
    """Result of parallel AC execution.

    Attributes:
        results: Individual results for each AC.
        success_count: Number of successful ACs.
        failure_count: Number of failed ACs.
        skipped_count: Number of skipped ACs (due to failed dependencies).
        blocked_count: Number of ACs blocked by dependency failures.
        invalid_count: Number of ACs missing from the execution plan.
        stages: Per-stage aggregated outcomes.
        reconciled_level_contexts: Current shared-workspace handoff contexts
            accumulated after each completed stage. Retry/reopen orchestration
            can pass these back into a later execution attempt so reopened ACs
            start from the post-reconcile workspace state instead of the
            original pre-failure context.
        total_messages: Total messages processed across all ACs.
        total_duration_seconds: Total execution time.
    """

    results: tuple[ACExecutionResult, ...]
    success_count: int
    failure_count: int
    skipped_count: int = 0
    blocked_count: int = 0
    invalid_count: int = 0
    stages: tuple[ParallelExecutionStageResult, ...] = field(default_factory=tuple)
    reconciled_level_contexts: tuple[LevelContext, ...] = field(default_factory=tuple)
    total_messages: int = 0
    total_duration_seconds: float = 0.0

    @property
    def all_succeeded(self) -> bool:
        """Return True if all ACs succeeded."""
        return self.failure_count == 0 and self.blocked_count == 0 and self.invalid_count == 0

    @property
    def any_succeeded(self) -> bool:
        """Return True if at least one AC succeeded."""
        return self.success_count > 0


__all__ = [
    "ACExecutionOutcome",
    "ACExecutionResult",
    "ParallelExecutionResult",
    "ParallelExecutionStageResult",
    "StageExecutionOutcome",
]
