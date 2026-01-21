"""SubAgent isolation and lifecycle management.

This module provides:
- SubAgent result validation
- SubAgent lifecycle events (started, completed, failed)
- Error handling that doesn't propagate to parent execution

Story 3.4: SubAgent Isolation
- AC 1, 2: SubAgents receive filtered context
- AC 3: Main context not modified by SubAgent
- AC 4: SubAgent results validated before integration
- AC 5: Failed SubAgent doesn't crash main execution
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ouroboros.core.errors import OuroborosError
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from ouroboros.execution.double_diamond import CycleResult

log = get_logger(__name__)


# =============================================================================
# Errors
# =============================================================================


class SubAgentError(OuroborosError):
    """Error during SubAgent execution.

    Attributes:
        subagent_id: The SubAgent execution ID.
        parent_id: The parent execution ID.
        is_retriable: Whether the error is potentially retriable.
    """

    def __init__(
        self,
        message: str,
        *,
        subagent_id: str | None = None,
        parent_id: str | None = None,
        is_retriable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details)
        self.subagent_id = subagent_id
        self.parent_id = parent_id
        self.is_retriable = is_retriable


class ValidationError(SubAgentError):
    """Error during SubAgent result validation."""

    pass


# =============================================================================
# Result Validation (AC 4)
# =============================================================================


def validate_child_result(
    child_result: CycleResult,
    expected_ac: str,  # noqa: ARG001 - reserved for future semantic validation
) -> Result[CycleResult, ValidationError]:
    """Validate a child SubAgent result before integration.

    This function performs structural validation:
    - Success status check
    - Required phases present (for non-decomposed results)
    - AC content matches expected

    Semantic validation (goal alignment, drift) is deferred to Epic 5 Stage 2.

    Args:
        child_result: The CycleResult from child SubAgent execution.
        expected_ac: The expected acceptance criterion content.

    Returns:
        Result containing the validated CycleResult or ValidationError.
    """
    from ouroboros.execution.double_diamond import Phase

    # Check success status
    if not child_result.success:
        log.warning(
            "subagent.validation.unsuccessful",
            execution_id=child_result.execution_id,
            current_ac=child_result.current_ac[:50],
        )
        return Result.err(
            ValidationError(
                f"Child result unsuccessful: {child_result.execution_id}",
                subagent_id=child_result.execution_id,
                is_retriable=False,
            )
        )

    # For non-decomposed results, check required phases
    if not child_result.is_decomposed:
        required_phases = {Phase.DISCOVER, Phase.DEFINE, Phase.DESIGN, Phase.DELIVER}
        present_phases = set(child_result.phase_results.keys())
        missing_phases = required_phases - present_phases

        if missing_phases:
            log.warning(
                "subagent.validation.missing_phases",
                execution_id=child_result.execution_id,
                missing=[p.value for p in missing_phases],
            )
            return Result.err(
                ValidationError(
                    f"Missing required phases: {[p.value for p in missing_phases]}",
                    subagent_id=child_result.execution_id,
                    is_retriable=False,
                    details={"missing_phases": [p.value for p in missing_phases]},
                )
            )
    else:
        # Decomposed results only need DISCOVER and DEFINE phases
        required_phases = {Phase.DISCOVER, Phase.DEFINE}
        present_phases = set(child_result.phase_results.keys())
        missing_phases = required_phases - present_phases

        if missing_phases:
            log.warning(
                "subagent.validation.missing_phases_decomposed",
                execution_id=child_result.execution_id,
                missing=[p.value for p in missing_phases],
            )
            return Result.err(
                ValidationError(
                    f"Decomposed result missing phases: {[p.value for p in missing_phases]}",
                    subagent_id=child_result.execution_id,
                    is_retriable=False,
                )
            )

    log.info(
        "subagent.validation.passed",
        execution_id=child_result.execution_id,
        is_decomposed=child_result.is_decomposed,
        phase_count=len(child_result.phase_results),
    )

    return Result.ok(child_result)


# =============================================================================
# Lifecycle Events
# =============================================================================


def create_subagent_started_event(
    subagent_id: str,
    parent_execution_id: str,
    child_ac: str,
    depth: int,
) -> BaseEvent:
    """Create event for SubAgent execution start.

    Args:
        subagent_id: The SubAgent execution ID.
        parent_execution_id: The parent execution ID.
        child_ac: The acceptance criterion for this SubAgent.
        depth: Current depth in AC decomposition tree.

    Returns:
        BaseEvent with type 'execution.subagent.started'.
    """
    return BaseEvent(
        type="execution.subagent.started",
        aggregate_type="execution",
        aggregate_id=subagent_id,
        data={
            "parent_execution_id": parent_execution_id,
            "child_ac": child_ac[:200],  # Truncate for storage
            "depth": depth,
        },
    )


def create_subagent_completed_event(
    subagent_id: str,
    parent_execution_id: str,
    success: bool,
    child_count: int = 0,
) -> BaseEvent:
    """Create event for SubAgent execution completion.

    Args:
        subagent_id: The SubAgent execution ID.
        parent_execution_id: The parent execution ID.
        success: Whether the SubAgent completed successfully.
        child_count: Number of child SubAgents spawned (if decomposed).

    Returns:
        BaseEvent with type 'execution.subagent.completed'.
    """
    return BaseEvent(
        type="execution.subagent.completed",
        aggregate_type="execution",
        aggregate_id=subagent_id,
        data={
            "parent_execution_id": parent_execution_id,
            "success": success,
            "child_count": child_count,
        },
    )


def create_subagent_failed_event(
    subagent_id: str,
    parent_execution_id: str,
    error_message: str,
    is_retriable: bool = False,
) -> BaseEvent:
    """Create event for SubAgent execution failure.

    Args:
        subagent_id: The SubAgent execution ID.
        parent_execution_id: The parent execution ID.
        error_message: Description of the failure.
        is_retriable: Whether the failure might be resolved by retry.

    Returns:
        BaseEvent with type 'execution.subagent.failed'.
    """
    return BaseEvent(
        type="execution.subagent.failed",
        aggregate_type="execution",
        aggregate_id=subagent_id,
        data={
            "parent_execution_id": parent_execution_id,
            "error_message": error_message[:500],  # Truncate for storage
            "is_retriable": is_retriable,
        },
    )


def create_subagent_validated_event(
    subagent_id: str,
    parent_execution_id: str,
    validation_passed: bool,
    validation_message: str = "",
) -> BaseEvent:
    """Create event for SubAgent result validation.

    Args:
        subagent_id: The SubAgent execution ID.
        parent_execution_id: The parent execution ID.
        validation_passed: Whether validation passed.
        validation_message: Additional validation details.

    Returns:
        BaseEvent with type 'execution.subagent.validated'.
    """
    return BaseEvent(
        type="execution.subagent.validated",
        aggregate_type="execution",
        aggregate_id=subagent_id,
        data={
            "parent_execution_id": parent_execution_id,
            "validation_passed": validation_passed,
            "validation_message": validation_message[:200],
        },
    )
