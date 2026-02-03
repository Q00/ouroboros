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
    """

    def __init__(
        self,
        execution_id: str,
        ac_id: str,
        status: str,
        depth: int,
        is_atomic: bool,
    ) -> None:
        """Initialize ACUpdated message.

        Args:
            execution_id: The execution with updated AC tree.
            ac_id: The AC that was updated.
            status: New AC status.
            depth: Depth in the AC tree.
            is_atomic: Whether AC is atomic.
        """
        super().__init__()
        self.execution_id = execution_id
        self.ac_id = ac_id
        self.status = status
        self.depth = depth
        self.is_atomic = is_atomic


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
        activity: Current activity type.
        activity_detail: Activity detail string.
        estimated_remaining: Estimated remaining time display.
        elapsed_display: Total elapsed time display.
    """

    def __init__(
        self,
        execution_id: str,
        acceptance_criteria: list[dict[str, Any]],
        completed_count: int,
        total_count: int,
        current_ac_index: int | None = None,
        activity: str = "idle",
        activity_detail: str = "",
        estimated_remaining: str = "",
        elapsed_display: str = "",
    ) -> None:
        """Initialize WorkflowProgressUpdated message.

        Args:
            execution_id: The execution with updated progress.
            acceptance_criteria: List of AC progress dicts.
            completed_count: Number of completed ACs.
            total_count: Total number of ACs.
            current_ac_index: Index of current AC.
            activity: Current activity type.
            activity_detail: Activity detail string.
            estimated_remaining: Estimated remaining time.
            elapsed_display: Total elapsed time display.
        """
        super().__init__()
        self.execution_id = execution_id
        self.acceptance_criteria = acceptance_criteria
        self.completed_count = completed_count
        self.total_count = total_count
        self.current_ac_index = current_ac_index
        self.activity = activity
        self.activity_detail = activity_detail
        self.estimated_remaining = estimated_remaining
        self.elapsed_display = elapsed_display


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
        )

    # Return None for unhandled event types
    return None


__all__ = [
    "ACUpdated",
    "CostUpdated",
    "DriftUpdated",
    "ExecutionUpdated",
    "LogMessage",
    "PauseRequested",
    "PhaseChanged",
    "ResumeRequested",
    "TUIState",
    "create_message_from_event",
]
