"""TUI event handlers and message types.

This module defines Textual messages for TUI event communication
and handlers for subscribing to EventStore updates.

Message Types:
- ExecutionUpdated: Execution state changed
- PhaseChanged: Phase transition occurred
- DriftUpdated: Drift metrics updated
- CostUpdated: Cost metrics updated
- LogMessage: New log entry received
- ACUpdated: AC tree node status changed
- WorkflowProgressUpdated: Workflow progress with AC list
- PauseRequested: User requested pause
- ResumeRequested: User requested resume
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from textual.message import Message

if TYPE_CHECKING:
    from ouroboros.events.base import BaseEvent


# =============================================================================
# Textual Messages for TUI Communication
# =============================================================================


class ExecutionUpdated(Message):
    """Message indicating execution state has changed.

    Attributes:
        execution_id: The execution that was updated.
        session_id: Associated session ID.
        status: Current execution status.
        data: Additional execution data.
    """

    def __init__(
        self,
        execution_id: str,
        session_id: str,
        status: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Initialize ExecutionUpdated message.

        Args:
            execution_id: The execution that was updated.
            session_id: Associated session ID.
            status: Current execution status.
            data: Additional execution data.
        """
        super().__init__()
        self.execution_id = execution_id
        self.session_id = session_id
        self.status = status
        self.data = data or {}


class PhaseChanged(Message):
    """Message indicating a phase transition occurred.

    Attributes:
        execution_id: The execution that changed phase.
        previous_phase: The phase that completed.
        current_phase: The new current phase.
        iteration: Current iteration number.
    """

    def __init__(
        self,
        execution_id: str,
        previous_phase: str | None,
        current_phase: str,
        iteration: int,
    ) -> None:
        """Initialize PhaseChanged message.

        Args:
            execution_id: The execution that changed phase.
            previous_phase: The phase that completed.
            current_phase: The new current phase.
            iteration: Current iteration number.
        """
        super().__init__()
        self.execution_id = execution_id
        self.previous_phase = previous_phase
        self.current_phase = current_phase
        self.iteration = iteration


class DriftUpdated(Message):
    """Message indicating drift metrics were updated.

    Attributes:
        execution_id: The execution with updated drift.
        goal_drift: Goal drift score (0.0-1.0).
        constraint_drift: Constraint drift score (0.0-1.0).
        ontology_drift: Ontology drift score (0.0-1.0).
        combined_drift: Combined drift score (0.0-1.0).
        is_acceptable: Whether drift is within threshold.
    """

    def __init__(
        self,
        execution_id: str,
        goal_drift: float,
        constraint_drift: float,
        ontology_drift: float,
        combined_drift: float,
        is_acceptable: bool,
    ) -> None:
        """Initialize DriftUpdated message.

        Args:
            execution_id: The execution with updated drift.
            goal_drift: Goal drift score.
            constraint_drift: Constraint drift score.
            ontology_drift: Ontology drift score.
            combined_drift: Combined drift score.
            is_acceptable: Whether drift is acceptable.
        """
        super().__init__()
        self.execution_id = execution_id
        self.goal_drift = goal_drift
        self.constraint_drift = constraint_drift
        self.ontology_drift = ontology_drift
        self.combined_drift = combined_drift
        self.is_acceptable = is_acceptable


class CostUpdated(Message):
    """Message indicating cost metrics were updated.

    Attributes:
        execution_id: The execution with updated cost.
        total_tokens: Total tokens consumed.
        total_cost_usd: Estimated cost in USD.
        tokens_this_phase: Tokens used in current phase.
    """

    def __init__(
        self,
        execution_id: str,
        total_tokens: int,
        total_cost_usd: float,
        tokens_this_phase: int,
    ) -> None:
        """Initialize CostUpdated message.

        Args:
            execution_id: The execution with updated cost.
            total_tokens: Total tokens consumed.
            total_cost_usd: Estimated cost in USD.
            tokens_this_phase: Tokens used in current phase.
        """
        super().__init__()
        self.execution_id = execution_id
        self.total_tokens = total_tokens
        self.total_cost_usd = total_cost_usd
        self.tokens_this_phase = tokens_this_phase


class LogMessage(Message):
    """Message for new log entries.

    Attributes:
        timestamp: When the log was created.
        level: Log level (debug, info, warning, error).
        source: Source module/component.
        message: Log message content.
        data: Additional structured data.
    """

    def __init__(
        self,
        timestamp: datetime,
        level: str,
        source: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Initialize LogMessage.

        Args:
            timestamp: When the log was created.
            level: Log level.
            source: Source module/component.
            message: Log message content.
            data: Additional structured data.
        """
        super().__init__()
        self.timestamp = timestamp
        self.level = level
        self.source = source
        self.message = message
        self.data = data or {}


class ACUpdated(Message):
    """Message indicating AC tree was updated.

    Attributes:
        execution_id: The execution with updated AC tree.
        ac_id: The AC that was updated.
        status: New AC status.
        depth: Depth in the AC tree.
        is_atomic: Whether AC is atomic.
        parallel_level: Execution level for parallel execution (None if not parallel).
    """

    def __init__(
        self,
        execution_id: str,
        ac_id: str,
        status: str,
        depth: int,
        is_atomic: bool,
        parallel_level: int | None = None,
    ) -> None:
        """Initialize ACUpdated message.

        Args:
            execution_id: The execution with updated AC tree.
            ac_id: The AC that was updated.
            status: New AC status.
            depth: Depth in the AC tree.
            is_atomic: Whether AC is atomic.
            parallel_level: Execution level for parallel execution.
        """
        super().__init__()
        self.execution_id = execution_id
        self.ac_id = ac_id
        self.status = status
        self.depth = depth
        self.is_atomic = is_atomic
        self.parallel_level = parallel_level


class ParallelBatchStarted(Message):
    """Message indicating a parallel batch execution started.

    Attributes:
        execution_id: The parent execution ID.
        batch_index: Index of the batch (0-based).
        ac_ids: List of AC IDs in this batch.
        total_batches: Total number of batches.
    """

    def __init__(
        self,
        execution_id: str,
        batch_index: int,
        ac_ids: list[str],
        total_batches: int,
    ) -> None:
        """Initialize ParallelBatchStarted message.

        Args:
            execution_id: The parent execution ID.
            batch_index: Index of the batch.
            ac_ids: List of AC IDs in this batch.
            total_batches: Total number of batches.
        """
        super().__init__()
        self.execution_id = execution_id
        self.batch_index = batch_index
        self.ac_ids = ac_ids
        self.total_batches = total_batches


class ParallelBatchCompleted(Message):
    """Message indicating a parallel batch execution completed.

    Attributes:
        execution_id: The parent execution ID.
        batch_index: Index of the batch (0-based).
        successful_count: Number of successful ACs in batch.
        failed_count: Number of failed ACs in batch.
        total_in_batch: Total ACs in this batch.
    """

    def __init__(
        self,
        execution_id: str,
        batch_index: int,
        successful_count: int,
        failed_count: int,
        total_in_batch: int,
    ) -> None:
        """Initialize ParallelBatchCompleted message.

        Args:
            execution_id: The parent execution ID.
            batch_index: Index of the batch.
            successful_count: Number of successful ACs.
            failed_count: Number of failed ACs.
            total_in_batch: Total ACs in batch.
        """
        super().__init__()
        self.execution_id = execution_id
        self.batch_index = batch_index
        self.successful_count = successful_count
        self.failed_count = failed_count
        self.total_in_batch = total_in_batch


class WorkflowProgressUpdated(Message):
    """Message indicating workflow progress was updated.

    Carries AC progress list with status and timing info,
    matching the WorkflowState from the orchestrator.

    Attributes:
        execution_id: The execution with updated progress.
        acceptance_criteria: List of AC dicts with index, content, status, elapsed.
        completed_count: Number of completed ACs.
        total_count: Total number of ACs.
        current_ac_index: Index of current AC being worked on.
        current_phase: Current Double Diamond phase.
        activity: Current activity type.
        activity_detail: Activity detail string.
        estimated_remaining: Estimated remaining time display.
        elapsed_display: Total elapsed time display.
        messages_count: Total messages processed.
        tool_calls_count: Total tool calls made.
        estimated_tokens: Estimated token usage.
        estimated_cost_usd: Estimated cost in USD.
    """

    def __init__(
        self,
        execution_id: str,
        acceptance_criteria: list[dict[str, Any]],
        completed_count: int,
        total_count: int,
        current_ac_index: int | None = None,
        current_phase: str = "Discover",
        activity: str = "idle",
        activity_detail: str = "",
        estimated_remaining: str = "",
        elapsed_display: str = "",
        messages_count: int = 0,
        tool_calls_count: int = 0,
        estimated_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """Initialize WorkflowProgressUpdated message."""
        super().__init__()
        self.execution_id = execution_id
        self.acceptance_criteria = acceptance_criteria
        self.completed_count = completed_count
        self.total_count = total_count
        self.current_ac_index = current_ac_index
        self.current_phase = current_phase
        self.activity = activity
        self.activity_detail = activity_detail
        self.estimated_remaining = estimated_remaining
        self.elapsed_display = elapsed_display
        self.messages_count = messages_count
        self.tool_calls_count = tool_calls_count
        self.estimated_tokens = estimated_tokens
        self.estimated_cost_usd = estimated_cost_usd


class SubtaskUpdated(Message):
    """Message indicating a sub-task was updated.

    Used to show hierarchical AC execution in the tree.

    Attributes:
        execution_id: The execution with updated sub-task.
        ac_index: Parent AC index.
        sub_task_index: Sub-task index within the AC.
        sub_task_id: Unique sub-task ID.
        content: Sub-task description.
        status: Sub-task status (executing, completed, failed).
    """

    def __init__(
        self,
        execution_id: str,
        ac_index: int,
        sub_task_index: int,
        sub_task_id: str,
        content: str,
        status: str,
    ) -> None:
        """Initialize SubtaskUpdated message."""
        super().__init__()
        self.execution_id = execution_id
        self.ac_index = ac_index
        self.sub_task_index = sub_task_index
        self.sub_task_id = sub_task_id
        self.content = content
        self.status = status


class PauseRequested(Message):
    """Message indicating user requested execution pause.

    Attributes:
        execution_id: The execution to pause.
        reason: Reason for pause request.
    """

    def __init__(self, execution_id: str, reason: str = "user_request") -> None:
        """Initialize PauseRequested message.

        Args:
            execution_id: The execution to pause.
            reason: Reason for pause request.
        """
        super().__init__()
        self.execution_id = execution_id
        self.reason = reason


class ResumeRequested(Message):
    """Message indicating user requested execution resume.

    Attributes:
        execution_id: The execution to resume.
    """

    def __init__(self, execution_id: str) -> None:
        """Initialize ResumeRequested message.

        Args:
            execution_id: The execution to resume.
        """
        super().__init__()
        self.execution_id = execution_id


class ToolCallStarted(Message):
    """Tool call started during AC execution."""

    def __init__(
        self,
        execution_id: str,
        ac_id: str,
        tool_name: str,
        tool_detail: str,
        tool_input: dict[str, Any] | None = None,
        call_index: int = 0,
    ) -> None:
        super().__init__()
        self.execution_id = execution_id
        self.ac_id = ac_id
        self.tool_name = tool_name
        self.tool_detail = tool_detail
        self.tool_input = tool_input or {}
        self.call_index = call_index


class ToolCallCompleted(Message):
    """Tool call completed during AC execution."""

    def __init__(
        self,
        execution_id: str,
        ac_id: str,
        tool_name: str,
        tool_detail: str,
        call_index: int = 0,
        duration_seconds: float = 0.0,
        success: bool = True,
    ) -> None:
        super().__init__()
        self.execution_id = execution_id
        self.ac_id = ac_id
        self.tool_name = tool_name
        self.tool_detail = tool_detail
        self.call_index = call_index
        self.duration_seconds = duration_seconds
        self.success = success


class AgentThinkingUpdated(Message):
    """Agent thinking/reasoning text updated."""

    def __init__(
        self,
        execution_id: str,
        ac_id: str,
        thinking_text: str,
    ) -> None:
        super().__init__()
        self.execution_id = execution_id
        self.ac_id = ac_id
        self.thinking_text = thinking_text


# =============================================================================
# Event Subscription State
# =============================================================================


@dataclass
class TUIState:
    """Mutable state for TUI display.

    Tracks current execution state for UI rendering.

    Attributes:
        execution_id: Current execution being monitored.
        session_id: Current session ID.
        status: Current execution status.
        current_phase: Current Double Diamond phase.
        iteration: Current iteration number.
        goal_drift: Current goal drift score.
        constraint_drift: Current constraint drift score.
        ontology_drift: Current ontology drift score.
        combined_drift: Current combined drift score.
        total_tokens: Total tokens consumed.
        total_cost_usd: Total cost in USD.
        is_paused: Whether execution is paused.
        ac_tree: Serialized AC tree data.
        logs: Recent log messages.
    """

    execution_id: str = ""
    session_id: str = ""
    status: str = "idle"
    current_phase: str = ""
    iteration: int = 0
    goal_drift: float = 0.0
    constraint_drift: float = 0.0
    ontology_drift: float = 0.0
    combined_drift: float = 0.0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    is_paused: bool = False
    ac_tree: dict[str, Any] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)
    max_logs: int = 100

    # P1: Tool/thinking tracking for dashboard
    active_tools: dict[str, dict[str, str]] = field(default_factory=dict)
    tool_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    thinking: dict[str, str] = field(default_factory=dict)

    def add_log(
        self,
        level: str,
        source: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Add a log entry, maintaining max size.

        Args:
            level: Log level.
            source: Source module.
            message: Log message.
            data: Additional data.
        """
        from datetime import UTC, datetime

        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level,
            "source": source,
            "message": message,
            "data": data or {},
        }
        self.logs.append(entry)

        # Trim to max size
        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs :]


# =============================================================================
# Event Store Subscription Handler
# =============================================================================


def create_message_from_event(event: BaseEvent) -> Message | None:
    """Convert an EventStore event to a TUI message.

    Args:
        event: The BaseEvent from EventStore.

    Returns:
        Corresponding TUI Message, or None if event type not handled.
    """
    event_type = event.type
    data = event.data

    if event_type == "orchestrator.session.started":
        return ExecutionUpdated(
            execution_id=data.get("execution_id", ""),
            session_id=event.aggregate_id,
            status="running",
            data=data,
        )

    elif event_type == "orchestrator.session.completed":
        return ExecutionUpdated(
            execution_id=data.get("execution_id", event.aggregate_id),
            session_id=event.aggregate_id,
            status="completed",
            data=data,
        )

    elif event_type == "orchestrator.session.failed":
        return ExecutionUpdated(
            execution_id=data.get("execution_id", event.aggregate_id),
            session_id=event.aggregate_id,
            status="failed",
            data=data,
        )

    elif event_type == "orchestrator.session.paused":
        return ExecutionUpdated(
            execution_id=data.get("execution_id", event.aggregate_id),
            session_id=event.aggregate_id,
            status="paused",
            data=data,
        )

    elif event_type == "execution.phase.completed":
        return PhaseChanged(
            execution_id=event.aggregate_id,
            previous_phase=data.get("previous_phase"),
            current_phase=data.get("phase", ""),
            iteration=data.get("iteration", 0),
        )

    elif event_type == "observability.drift.measured":
        return DriftUpdated(
            execution_id=event.aggregate_id,
            goal_drift=data.get("goal_drift", 0.0),
            constraint_drift=data.get("constraint_drift", 0.0),
            ontology_drift=data.get("ontology_drift", 0.0),
            combined_drift=data.get("combined_drift", 0.0),
            is_acceptable=data.get("is_acceptable", True),
        )

    elif event_type == "observability.cost.updated":
        return CostUpdated(
            execution_id=event.aggregate_id,
            total_tokens=data.get("total_tokens", 0),
            total_cost_usd=data.get("total_cost_usd", 0.0),
            tokens_this_phase=data.get("tokens_this_phase", 0),
        )

    elif event_type.startswith("decomposition.ac."):
        status = "pending"
        if "completed" in event_type:
            status = "completed"
        elif "started" in event_type:
            status = "executing"
        elif "marked_atomic" in event_type:
            status = "atomic"

        return ACUpdated(
            execution_id=event.aggregate_id,
            ac_id=data.get("ac_id", ""),
            status=status,
            depth=data.get("depth", 0),
            is_atomic=data.get("is_atomic", False),
            parallel_level=data.get("parallel_level"),
        )

    elif event_type == "execution.subagent.started":
        # SubAgent started - extract parallel level if available
        return ACUpdated(
            execution_id=event.aggregate_id,
            ac_id=data.get("subagent_id", ""),
            status="executing",
            depth=data.get("depth", 0),
            is_atomic=False,
            parallel_level=data.get("parallel_level"),
        )

    elif event_type == "execution.subagent.completed":
        return ACUpdated(
            execution_id=event.aggregate_id,
            ac_id=data.get("subagent_id", ""),
            status="completed",
            depth=data.get("depth", 0),
            is_atomic=False,
            parallel_level=data.get("parallel_level"),
        )

    elif event_type == "execution.subagent.failed":
        return ACUpdated(
            execution_id=event.aggregate_id,
            ac_id=data.get("subagent_id", ""),
            status="failed",
            depth=data.get("depth", 0),
            is_atomic=False,
            parallel_level=data.get("parallel_level"),
        )

    elif event_type == "execution.decomposition.level_started":
        # Parallel batch started
        return ParallelBatchStarted(
            execution_id=event.aggregate_id,
            batch_index=data.get("level", 0),
            ac_ids=data.get("child_indices", []),
            total_batches=data.get("total_levels", 1),
        )

    elif event_type == "execution.decomposition.level_completed":
        # Parallel batch completed
        return ParallelBatchCompleted(
            execution_id=event.aggregate_id,
            batch_index=data.get("level", 0),
            successful_count=data.get("successful", 0),
            failed_count=data.get("total", 0) - data.get("successful", 0),
            total_in_batch=data.get("total", 0),
        )

    elif event_type == "workflow.progress.updated":
        return WorkflowProgressUpdated(
            execution_id=data.get("execution_id", event.aggregate_id),
            acceptance_criteria=data.get("acceptance_criteria", []),
            completed_count=data.get("completed_count", 0),
            total_count=data.get("total_count", 0),
            current_ac_index=data.get("current_ac_index"),
            current_phase=data.get("current_phase", "Discover"),
            activity=data.get("activity", "idle"),
            activity_detail=data.get("activity_detail", ""),
            estimated_remaining=data.get("estimated_remaining", ""),
            elapsed_display=data.get("elapsed_display", ""),
            messages_count=data.get("messages_count", 0),
            tool_calls_count=data.get("tool_calls_count", 0),
            estimated_tokens=data.get("estimated_tokens", 0),
            estimated_cost_usd=data.get("estimated_cost_usd", 0.0),
        )

    elif event_type == "execution.subtask.updated":
        return SubtaskUpdated(
            execution_id=event.aggregate_id,
            ac_index=data.get("ac_index", 0),
            sub_task_index=data.get("sub_task_index", 0),
            sub_task_id=data.get("sub_task_id", ""),
            content=data.get("content", ""),
            status=data.get("status", "pending"),
        )

    elif event_type == "execution.tool.started":
        return ToolCallStarted(
            execution_id=event.aggregate_id,
            ac_id=data.get("ac_id", ""),
            tool_name=data.get("tool_name", ""),
            tool_detail=data.get("tool_detail", ""),
            tool_input=data.get("tool_input"),
            call_index=data.get("call_index", 0),
        )

    elif event_type == "execution.tool.completed":
        return ToolCallCompleted(
            execution_id=event.aggregate_id,
            ac_id=data.get("ac_id", ""),
            tool_name=data.get("tool_name", ""),
            tool_detail=data.get("tool_detail", ""),
            call_index=data.get("call_index", 0),
            duration_seconds=data.get("duration_seconds", 0.0),
            success=data.get("success", True),
        )

    elif event_type == "execution.agent.thinking":
        return AgentThinkingUpdated(
            execution_id=event.aggregate_id,
            ac_id=data.get("ac_id", ""),
            thinking_text=data.get("thinking_text", ""),
        )

    # Return None for unhandled event types
    return None


__all__ = [
    "ACUpdated",
    "AgentThinkingUpdated",
    "CostUpdated",
    "DriftUpdated",
    "ExecutionUpdated",
    "LogMessage",
    "ParallelBatchCompleted",
    "ParallelBatchStarted",
    "PauseRequested",
    "PhaseChanged",
    "ResumeRequested",
    "SubtaskUpdated",
    "TUIState",
    "ToolCallCompleted",
    "ToolCallStarted",
    "WorkflowProgressUpdated",
    "create_message_from_event",
]
