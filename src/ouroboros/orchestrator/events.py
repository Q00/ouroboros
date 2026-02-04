"""Event creation helpers for orchestrator.

This module provides factory functions for creating orchestrator-related events
following the project's event naming convention (dot.notation.past_tense).

Event Types:
    - orchestrator.session.started: Session began execution
    - orchestrator.session.completed: Session finished successfully
    - orchestrator.session.failed: Session encountered fatal error
    - orchestrator.session.paused: Session paused for resumption
    - orchestrator.progress.updated: Progress checkpoint
    - orchestrator.task.started: Individual task started
    - orchestrator.task.completed: Individual task completed
    - orchestrator.tool.called: Tool was invoked by agent
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ouroboros.events.base import BaseEvent


def create_session_started_event(
    session_id: str,
    execution_id: str,
    seed_id: str,
    seed_goal: str,
) -> BaseEvent:
    """Create session started event.

    Args:
        session_id: Unique session identifier.
        execution_id: Associated workflow execution ID.
        seed_id: ID of the seed being executed.
        seed_goal: Goal from the seed specification.

    Returns:
        BaseEvent for session start.
    """
    return BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id=session_id,
        data={
            "execution_id": execution_id,
            "seed_id": seed_id,
            "seed_goal": seed_goal,
            "start_time": datetime.now(UTC).isoformat(),
        },
    )


def create_session_completed_event(
    session_id: str,
    summary: dict[str, Any],
    messages_processed: int,
) -> BaseEvent:
    """Create session completed event.

    Args:
        session_id: Session that completed.
        summary: Execution summary data.
        messages_processed: Total messages processed.

    Returns:
        BaseEvent for session completion.
    """
    return BaseEvent(
        type="orchestrator.session.completed",
        aggregate_type="session",
        aggregate_id=session_id,
        data={
            "summary": summary,
            "messages_processed": messages_processed,
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )


def create_session_failed_event(
    session_id: str,
    error_message: str,
    error_type: str | None = None,
    messages_processed: int = 0,
) -> BaseEvent:
    """Create session failed event.

    Args:
        session_id: Session that failed.
        error_message: Error description.
        error_type: Type/category of error.
        messages_processed: Messages processed before failure.

    Returns:
        BaseEvent for session failure.
    """
    return BaseEvent(
        type="orchestrator.session.failed",
        aggregate_type="session",
        aggregate_id=session_id,
        data={
            "error": error_message,
            "error_type": error_type,
            "messages_processed": messages_processed,
            "failed_at": datetime.now(UTC).isoformat(),
        },
    )


def create_session_paused_event(
    session_id: str,
    reason: str,
    resume_hint: str | None = None,
) -> BaseEvent:
    """Create session paused event.

    Args:
        session_id: Session being paused.
        reason: Why the session was paused.
        resume_hint: Hint for resumption (e.g., last AC processed).

    Returns:
        BaseEvent for session pause.
    """
    return BaseEvent(
        type="orchestrator.session.paused",
        aggregate_type="session",
        aggregate_id=session_id,
        data={
            "reason": reason,
            "resume_hint": resume_hint,
            "paused_at": datetime.now(UTC).isoformat(),
        },
    )


def create_progress_event(
    session_id: str,
    message_type: str,
    content_preview: str,
    step: int | None = None,
    tool_name: str | None = None,
) -> BaseEvent:
    """Create progress update event.

    Emitted periodically during execution to track progress.
    Useful for reconstructing session state during resumption.

    Args:
        session_id: Session being updated.
        message_type: Type of message ("assistant", "tool", etc.).
        content_preview: Preview of message content (truncated).
        step: Optional step number.
        tool_name: Tool being called (if message_type="tool").

    Returns:
        BaseEvent for progress update.
    """
    data: dict[str, Any] = {
        "message_type": message_type,
        "content_preview": content_preview[:200],  # Truncate for storage
        "timestamp": datetime.now(UTC).isoformat(),
    }

    if step is not None:
        data["step"] = step

    if tool_name:
        data["tool_name"] = tool_name

    return BaseEvent(
        type="orchestrator.progress.updated",
        aggregate_type="session",
        aggregate_id=session_id,
        data=data,
    )


def create_task_started_event(
    session_id: str,
    task_description: str,
    acceptance_criterion: str,
) -> BaseEvent:
    """Create task started event.

    Args:
        session_id: Session executing the task.
        task_description: What the task aims to accomplish.
        acceptance_criterion: AC from the seed being executed.

    Returns:
        BaseEvent for task start.
    """
    return BaseEvent(
        type="orchestrator.task.started",
        aggregate_type="session",
        aggregate_id=session_id,
        data={
            "task_description": task_description,
            "acceptance_criterion": acceptance_criterion,
            "started_at": datetime.now(UTC).isoformat(),
        },
    )


def create_task_completed_event(
    session_id: str,
    acceptance_criterion: str,
    success: bool,
    result_summary: str | None = None,
) -> BaseEvent:
    """Create task completed event.

    Args:
        session_id: Session that completed the task.
        acceptance_criterion: AC that was executed.
        success: Whether the task succeeded.
        result_summary: Summary of what was accomplished.

    Returns:
        BaseEvent for task completion.
    """
    return BaseEvent(
        type="orchestrator.task.completed",
        aggregate_type="session",
        aggregate_id=session_id,
        data={
            "acceptance_criterion": acceptance_criterion,
            "success": success,
            "result_summary": result_summary,
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )


def create_tool_called_event(
    session_id: str,
    tool_name: str,
    tool_input_preview: str | None = None,
) -> BaseEvent:
    """Create tool called event.

    Args:
        session_id: Session where tool was called.
        tool_name: Name of the tool (Read, Edit, Bash, etc.).
        tool_input_preview: Preview of tool input (truncated).

    Returns:
        BaseEvent for tool invocation.
    """
    data: dict[str, Any] = {
        "tool_name": tool_name,
        "called_at": datetime.now(UTC).isoformat(),
    }

    if tool_input_preview:
        data["tool_input_preview"] = tool_input_preview[:100]

    return BaseEvent(
        type="orchestrator.tool.called",
        aggregate_type="session",
        aggregate_id=session_id,
        data=data,
    )


def create_mcp_tools_loaded_event(
    session_id: str,
    tool_count: int,
    server_names: tuple[str, ...],
    conflict_count: int = 0,
    tool_names: list[str] | None = None,
) -> BaseEvent:
    """Create MCP tools loaded event.

    Emitted when MCP tools are discovered and loaded for a session.

    Args:
        session_id: Session loading the tools.
        tool_count: Number of MCP tools loaded.
        server_names: Names of MCP servers providing tools.
        conflict_count: Number of tool name conflicts detected.
        tool_names: Optional list of loaded tool names.

    Returns:
        BaseEvent for MCP tools loaded.
    """
    data: dict[str, Any] = {
        "tool_count": tool_count,
        "server_names": list(server_names),
        "conflict_count": conflict_count,
        "loaded_at": datetime.now(UTC).isoformat(),
    }

    if tool_names:
        data["tool_names"] = tool_names[:50]  # Limit to 50 for storage

    return BaseEvent(
        type="orchestrator.mcp_tools.loaded",
        aggregate_type="session",
        aggregate_id=session_id,
        data=data,
    )


def create_workflow_progress_event(
    execution_id: str,
    session_id: str,
    acceptance_criteria: list[dict[str, Any]],
    completed_count: int,
    total_count: int,
    current_ac_index: int | None = None,
    current_phase: str = "Discover",
    activity: str = "idle",
    activity_detail: str = "",
    elapsed_display: str = "",
    estimated_remaining: str = "",
    messages_count: int = 0,
    tool_calls_count: int = 0,
    estimated_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
) -> BaseEvent:
    """Create workflow progress event.

    Emitted when WorkflowStateTracker updates with new progress.
    Used by TUI to update ACProgressWidget.

    Args:
        execution_id: Current execution ID.
        session_id: Current session ID.
        acceptance_criteria: List of AC dicts with index, content, status, elapsed.
        completed_count: Number of completed ACs.
        total_count: Total number of ACs.
        current_ac_index: Index of AC currently being worked on.
        current_phase: Current Double Diamond phase.
        activity: Current activity type.
        activity_detail: Activity detail string.
        elapsed_display: Total elapsed time display.
        estimated_remaining: Estimated remaining time display.
        messages_count: Total messages processed.
        tool_calls_count: Total tool calls made.
        estimated_tokens: Estimated token usage.
        estimated_cost_usd: Estimated cost in USD.

    Returns:
        BaseEvent for workflow progress update.
    """
    return BaseEvent(
        type="workflow.progress.updated",
        aggregate_type="execution",
        aggregate_id=execution_id,
        data={
            "session_id": session_id,
            "acceptance_criteria": acceptance_criteria,
            "completed_count": completed_count,
            "total_count": total_count,
            "current_ac_index": current_ac_index,
            "current_phase": current_phase,
            "activity": activity,
            "activity_detail": activity_detail,
            "elapsed_display": elapsed_display,
            "estimated_remaining": estimated_remaining,
            "messages_count": messages_count,
            "tool_calls_count": tool_calls_count,
            "estimated_tokens": estimated_tokens,
            "estimated_cost_usd": estimated_cost_usd,
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


def create_drift_measured_event(
    execution_id: str,
    goal_drift: float,
    constraint_drift: float,
    ontology_drift: float,
    combined_drift: float,
    is_acceptable: bool,
) -> BaseEvent:
    """Create drift measured event.

    Emitted when drift is measured during workflow execution.

    Args:
        execution_id: Current execution ID.
        goal_drift: Goal drift value (0.0-1.0).
        constraint_drift: Constraint drift value (0.0-1.0).
        ontology_drift: Ontology drift value (0.0-1.0).
        combined_drift: Combined weighted drift value.
        is_acceptable: Whether drift is within acceptable threshold.

    Returns:
        BaseEvent for drift measurement.
    """
    return BaseEvent(
        type="observability.drift.measured",
        aggregate_type="execution",
        aggregate_id=execution_id,
        data={
            "goal_drift": goal_drift,
            "constraint_drift": constraint_drift,
            "ontology_drift": ontology_drift,
            "combined_drift": combined_drift,
            "is_acceptable": is_acceptable,
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


__all__ = [
    "create_drift_measured_event",
    "create_mcp_tools_loaded_event",
    "create_progress_event",
    "create_session_completed_event",
    "create_session_failed_event",
    "create_session_paused_event",
    "create_session_started_event",
    "create_task_completed_event",
    "create_task_started_event",
    "create_tool_called_event",
    "create_workflow_progress_event",
]
