"""Event definitions for AC decomposition and atomicity detection.

These events enable full traceability of AC lifecycle through event sourcing:
- Atomicity checks and their results
- AC decomposition into children
- AC marking as atomic (ready for execution)

Event naming follows dot.notation.past_tense convention.
"""

from __future__ import annotations

from ouroboros.events.base import BaseEvent


def create_ac_atomicity_checked_event(
    ac_id: str,
    execution_id: str,
    is_atomic: bool,
    complexity_score: float,
    tool_count: int,
    estimated_duration: int,
    reasoning: str,
) -> BaseEvent:
    """Factory for AC atomicity check event.

    Emitted when an AC's atomicity has been evaluated.

    Args:
        ac_id: Unique identifier for the AC.
        execution_id: Associated execution ID.
        is_atomic: Whether the AC is atomic.
        complexity_score: Normalized complexity (0.0-1.0).
        tool_count: Estimated number of tools required.
        estimated_duration: Estimated duration in seconds.
        reasoning: Explanation of the atomicity decision.

    Returns:
        BaseEvent with type "ac.atomicity.checked".
    """
    return BaseEvent(
        type="ac.atomicity.checked",
        aggregate_type="ac_decomposition",
        aggregate_id=ac_id,
        data={
            "execution_id": execution_id,
            "is_atomic": is_atomic,
            "complexity_score": complexity_score,
            "tool_count": tool_count,
            "estimated_duration": estimated_duration,
            "reasoning": reasoning,
        },
    )


def create_ac_decomposed_event(
    parent_ac_id: str,
    execution_id: str,
    child_ac_ids: list[str],
    child_contents: list[str],
    depth: int,
    reasoning: str,
) -> BaseEvent:
    """Factory for AC decomposition event.

    Emitted when a non-atomic AC is decomposed into child ACs.

    Args:
        parent_ac_id: ID of the parent AC being decomposed.
        execution_id: Associated execution ID.
        child_ac_ids: List of child AC IDs.
        child_contents: List of child AC content strings.
        depth: Current depth in the AC tree.
        reasoning: Explanation of the decomposition strategy.

    Returns:
        BaseEvent with type "ac.decomposition.completed".
    """
    return BaseEvent(
        type="ac.decomposition.completed",
        aggregate_type="ac_decomposition",
        aggregate_id=parent_ac_id,
        data={
            "execution_id": execution_id,
            "child_ac_ids": child_ac_ids,
            "child_contents": child_contents,
            "child_count": len(child_ac_ids),
            "depth": depth,
            "reasoning": reasoning,
        },
    )


def create_ac_marked_atomic_event(
    ac_id: str,
    execution_id: str,
    depth: int,
) -> BaseEvent:
    """Factory for AC marked atomic event.

    Emitted when an AC is confirmed as atomic and ready for direct execution.

    Args:
        ac_id: Unique identifier for the AC.
        execution_id: Associated execution ID.
        depth: Current depth in the AC tree.

    Returns:
        BaseEvent with type "ac.marked_atomic".
    """
    return BaseEvent(
        type="ac.marked_atomic",
        aggregate_type="ac_decomposition",
        aggregate_id=ac_id,
        data={
            "execution_id": execution_id,
            "depth": depth,
        },
    )


def create_ac_decomposition_failed_event(
    ac_id: str,
    execution_id: str,
    error_message: str,
    error_type: str,
    depth: int,
) -> BaseEvent:
    """Factory for AC decomposition failure event.

    Emitted when decomposition fails (max depth, cyclic, LLM error).

    Args:
        ac_id: Unique identifier for the AC.
        execution_id: Associated execution ID.
        error_message: Human-readable error description.
        error_type: Type of error (e.g., "max_depth", "cyclic", "llm_failure").
        depth: Current depth when failure occurred.

    Returns:
        BaseEvent with type "ac.decomposition.failed".
    """
    return BaseEvent(
        type="ac.decomposition.failed",
        aggregate_type="ac_decomposition",
        aggregate_id=ac_id,
        data={
            "execution_id": execution_id,
            "error_message": error_message,
            "error_type": error_type,
            "depth": depth,
        },
    )
