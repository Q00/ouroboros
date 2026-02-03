"""Workflow state tracking for real-time progress display.

This module provides shared state models for tracking workflow progress
that can be rendered by both CLI (Rich Live) and TUI (Textual).

The ACTracker uses a marker-based protocol for tracking acceptance criteria:
- [AC_START: N] - Agent starts working on AC #N
- [AC_COMPLETE: N] - Agent completes AC #N

The system prompt instructs Claude to emit these markers, with heuristic
fallback for natural language completion detection.

Usage:
    tracker = WorkflowStateTracker(acceptance_criteria)
    tracker.process_message(message)  # Updates state from agent output
    state = tracker.get_state()  # Get current state for rendering
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class ACStatus(Enum):
    """Status of an acceptance criterion."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ActivityType(Enum):
    """Type of activity being performed."""

    IDLE = "idle"
    EXPLORING = "exploring"
    BUILDING = "building"
    TESTING = "testing"
    DEBUGGING = "debugging"
    DOCUMENTING = "documenting"
    FINALIZING = "finalizing"


# Tool name to activity type mapping
TOOL_ACTIVITY_MAP: dict[str, ActivityType] = {
    "Read": ActivityType.EXPLORING,
    "Glob": ActivityType.EXPLORING,
    "Grep": ActivityType.EXPLORING,
    "LS": ActivityType.EXPLORING,
    "Edit": ActivityType.BUILDING,
    "Write": ActivityType.BUILDING,
    "Bash": ActivityType.TESTING,  # Often used for tests
    "Task": ActivityType.EXPLORING,
}


class Phase(Enum):
    """Double Diamond phase."""

    DISCOVER = "Discover"
    DEFINE = "Define"
    DEVELOP = "Develop"
    DELIVER = "Deliver"


@dataclass
class AcceptanceCriterion:
    """State of a single acceptance criterion.

    Attributes:
        index: 1-based index of the AC.
        content: The AC description text.
        status: Current status.
        started_at: When work started on this AC.
        completed_at: When this AC was completed.
    """

    index: int
    content: str
    status: ACStatus = ACStatus.PENDING
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def start(self) -> None:
        """Mark AC as in progress."""
        self.status = ACStatus.IN_PROGRESS
        self.started_at = datetime.now(UTC)

    def complete(self) -> None:
        """Mark AC as completed."""
        self.status = ACStatus.COMPLETED
        self.completed_at = datetime.now(UTC)

    def fail(self) -> None:
        """Mark AC as failed."""
        self.status = ACStatus.FAILED
        self.completed_at = datetime.now(UTC)

    @property
    def elapsed_seconds(self) -> float | None:
        """Seconds spent on this AC."""
        if self.started_at is None:
            return None
        end = self.completed_at or datetime.now(UTC)
        return (end - self.started_at).total_seconds()

    @property
    def elapsed_display(self) -> str:
        """Elapsed time formatted for display."""
        elapsed = self.elapsed_seconds
        if elapsed is None:
            return ""
        elapsed_int = int(elapsed)
        minutes, seconds = divmod(elapsed_int, 60)
        if minutes > 0:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"


@dataclass
class WorkflowState:
    """Complete workflow state for rendering.

    Attributes:
        session_id: Current session identifier.
        goal: The workflow goal.
        acceptance_criteria: List of AC states.
        current_ac_index: Index of AC currently being worked on (1-based, 0 if none).
        activity: Current activity type.
        activity_detail: Detail about current activity.
        last_tool: Last tool that was called.
        messages_count: Total messages processed.
        tool_calls_count: Total tool calls.
        estimated_tokens: Estimated token count.
        estimated_cost_usd: Estimated cost in USD.
        start_time: When execution started.
        activity_log: Recent activity entries.
    """

    session_id: str = ""
    goal: str = ""
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    current_ac_index: int = 0
    current_phase: Phase = Phase.DISCOVER
    activity: ActivityType = ActivityType.IDLE
    activity_detail: str = ""
    last_tool: str = ""
    messages_count: int = 0
    tool_calls_count: int = 0
    estimated_tokens: int = 0
    estimated_cost_usd: float = 0.0
    start_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    activity_log: list[str] = field(default_factory=list)
    max_activity_log: int = 3
    recent_outputs: list[str] = field(default_factory=list)
    max_recent_outputs: int = 2

    @property
    def completed_count(self) -> int:
        """Number of completed ACs."""
        return sum(1 for ac in self.acceptance_criteria if ac.status == ACStatus.COMPLETED)

    @property
    def total_count(self) -> int:
        """Total number of ACs."""
        return len(self.acceptance_criteria)

    @property
    def progress_fraction(self) -> float:
        """Progress as a fraction (0.0 to 1.0)."""
        if self.total_count == 0:
            return 0.0
        return self.completed_count / self.total_count

    @property
    def progress_percent(self) -> int:
        """Progress as a percentage (0 to 100)."""
        return int(self.progress_fraction * 100)

    @property
    def elapsed_seconds(self) -> float:
        """Seconds elapsed since start."""
        return (datetime.now(UTC) - self.start_time).total_seconds()

    @property
    def elapsed_display(self) -> str:
        """Elapsed time formatted for display (e.g., '5m 12s')."""
        elapsed = int(self.elapsed_seconds)
        minutes, seconds = divmod(elapsed, 60)
        hours, minutes = divmod(minutes, 60)

        if hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"

    @property
    def estimated_remaining_seconds(self) -> float | None:
        """Estimated seconds remaining based on current progress."""
        if self.completed_count == 0:
            return None  # Can't estimate without any completed ACs
        elapsed = self.elapsed_seconds
        # Calculate average time per AC and multiply by remaining
        avg_time_per_ac = elapsed / self.completed_count
        remaining_acs = self.total_count - self.completed_count
        return avg_time_per_ac * remaining_acs

    @property
    def estimated_remaining_display(self) -> str:
        """Estimated remaining time formatted for display."""
        remaining = self.estimated_remaining_seconds
        if remaining is None:
            return ""
        remaining_int = int(remaining)
        minutes, seconds = divmod(remaining_int, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"~{hours}h {minutes}m remaining"
        elif minutes > 0:
            return f"~{minutes}m remaining"
        else:
            return f"~{seconds}s remaining"

    def add_activity(self, entry: str) -> None:
        """Add an activity log entry.

        Args:
            entry: Activity description.
        """
        self.activity_log.append(entry)
        if len(self.activity_log) > self.max_activity_log:
            self.activity_log = self.activity_log[-self.max_activity_log :]

    def add_output(self, output: str) -> None:
        """Add a recent output entry (for display under activity).

        Args:
            output: Output text (will be truncated).
        """
        # Truncate and clean the output
        clean = output.strip().replace("\n", " ")[:60]
        if clean:
            self.recent_outputs.append(clean)
            if len(self.recent_outputs) > self.max_recent_outputs:
                self.recent_outputs = self.recent_outputs[-self.max_recent_outputs :]

    def to_tui_message_data(self, execution_id: str = "") -> dict[str, Any]:
        """Convert state to TUI message-compatible data.

        Returns a dictionary suitable for creating a WorkflowProgressUpdated
        message for the TUI.

        Args:
            execution_id: Execution ID for the message.

        Returns:
            Dictionary with message data.
        """
        return {
            "execution_id": execution_id or self.session_id,
            "acceptance_criteria": [
                {
                    "index": ac.index,
                    "content": ac.content,
                    "status": ac.status.value,
                    "elapsed_display": ac.elapsed_display,
                }
                for ac in self.acceptance_criteria
            ],
            "completed_count": self.completed_count,
            "total_count": self.total_count,
            "current_ac_index": self.current_ac_index,
            "activity": self.activity.value,
            "activity_detail": self.activity_detail,
            "estimated_remaining": self.estimated_remaining_display,
            "elapsed_display": self.elapsed_display,
        }


# Claude 3.5 Sonnet pricing (as of 2024)
CLAUDE_INPUT_PRICE_PER_1M = 3.0  # $3 per 1M input tokens
CLAUDE_OUTPUT_PRICE_PER_1M = 15.0  # $15 per 1M output tokens
CHARS_PER_TOKEN_ESTIMATE = 4  # Rough estimate


class WorkflowStateTracker:
    """Tracks workflow state from agent messages.

    Processes agent messages to extract AC progress using markers
    and heuristics, estimates token usage, and tracks activity.

    The tracker expects Claude to use explicit markers:
    - [AC_START: N] when beginning work on AC #N
    - [AC_COMPLETE: N] when finishing AC #N

    It also uses heuristic fallback to detect completions from
    natural language patterns.
    """

    # Regex patterns for AC markers
    AC_START_PATTERN = re.compile(r"\[AC_START:\s*(\d+)\]", re.IGNORECASE)
    AC_COMPLETE_PATTERN = re.compile(r"\[AC_COMPLETE:\s*(\d+)\]", re.IGNORECASE)

    # Heuristic patterns for completion detection
    COMPLETION_PATTERNS = [
        re.compile(r"(?:criterion|AC)\s*#?(\d+)\s*(?:is\s+)?(?:complete|done|finished|satisfied)", re.IGNORECASE),
        re.compile(r"(?:completed|finished|done\s+with)\s*(?:criterion|AC)\s*#?(\d+)", re.IGNORECASE),
        re.compile(r"✓\s*(?:criterion|AC)?\s*#?(\d+)", re.IGNORECASE),
    ]

    def __init__(
        self,
        acceptance_criteria: list[str],
        goal: str = "",
        session_id: str = "",
    ) -> None:
        """Initialize tracker with acceptance criteria.

        Args:
            acceptance_criteria: List of AC descriptions.
            goal: The workflow goal.
            session_id: Session identifier.
        """
        self._state = WorkflowState(
            session_id=session_id,
            goal=goal,
            acceptance_criteria=[
                AcceptanceCriterion(index=i + 1, content=ac)
                for i, ac in enumerate(acceptance_criteria)
            ],
        )
        self._input_chars = 0
        self._output_chars = 0

    @property
    def state(self) -> WorkflowState:
        """Get current workflow state."""
        return self._state

    def process_message(
        self,
        content: str,
        message_type: str = "assistant",
        tool_name: str | None = None,
        is_input: bool = False,
    ) -> None:
        """Process an agent message to update state.

        Args:
            content: Message content.
            message_type: Type of message (assistant, tool, result).
            tool_name: Name of tool if this is a tool call.
            is_input: Whether this is input (True) or output (False).
        """
        self._state.messages_count += 1

        # Update token estimates
        char_count = len(content)
        if is_input:
            self._input_chars += char_count
        else:
            self._output_chars += char_count

        self._update_cost_estimate()

        # Update tool tracking
        if tool_name:
            self._state.tool_calls_count += 1
            self._state.last_tool = tool_name
            self._update_activity_from_tool(tool_name, content)

        # Parse AC markers and heuristics
        self._parse_ac_markers(content)

        # Add recent output for display (assistant messages only, not tool results)
        if message_type == "assistant" and not tool_name and content.strip():
            self._state.add_output(content)

        # Update phase based on progress
        self._update_phase()

    def _update_cost_estimate(self) -> None:
        """Update token and cost estimates."""
        input_tokens = self._input_chars // CHARS_PER_TOKEN_ESTIMATE
        output_tokens = self._output_chars // CHARS_PER_TOKEN_ESTIMATE

        self._state.estimated_tokens = input_tokens + output_tokens

        input_cost = (input_tokens / 1_000_000) * CLAUDE_INPUT_PRICE_PER_1M
        output_cost = (output_tokens / 1_000_000) * CLAUDE_OUTPUT_PRICE_PER_1M
        self._state.estimated_cost_usd = input_cost + output_cost

    def _update_activity_from_tool(self, tool_name: str, content: str) -> None:
        """Update activity type based on tool usage.

        Args:
            tool_name: Name of the tool being used.
            content: Tool call content/arguments.
        """
        activity = TOOL_ACTIVITY_MAP.get(tool_name, ActivityType.BUILDING)

        # Refine activity based on content patterns
        content_lower = content.lower()
        if tool_name == "Bash":
            if any(kw in content_lower for kw in ["test", "pytest", "jest", "npm test"]):
                activity = ActivityType.TESTING
            elif any(kw in content_lower for kw in ["debug", "print", "log"]):
                activity = ActivityType.DEBUGGING

        self._state.activity = activity

        # Extract detail from content
        if tool_name in ("Edit", "Write"):
            # Try to extract file path
            path_match = re.search(r'["\']?([^\s"\']+\.\w+)["\']?', content)
            if path_match:
                self._state.activity_detail = f"{tool_name} {path_match.group(1)}"
            else:
                self._state.activity_detail = tool_name
        elif tool_name in ("Read", "Glob", "Grep"):
            self._state.activity_detail = f"Searching with {tool_name}"
        else:
            self._state.activity_detail = tool_name

    def _parse_ac_markers(self, content: str) -> None:
        """Parse AC markers and heuristics from content.

        Args:
            content: Message content to parse.
        """
        # Check for explicit AC_START markers
        for match in self.AC_START_PATTERN.finditer(content):
            ac_num = int(match.group(1))
            self._mark_ac_started(ac_num)

        # Check for explicit AC_COMPLETE markers
        for match in self.AC_COMPLETE_PATTERN.finditer(content):
            ac_num = int(match.group(1))
            self._mark_ac_completed(ac_num)

        # Heuristic fallback for completion detection
        for pattern in self.COMPLETION_PATTERNS:
            for match in pattern.finditer(content):
                ac_num = int(match.group(1))
                self._mark_ac_completed(ac_num)

    def _mark_ac_started(self, ac_index: int) -> None:
        """Mark an AC as started.

        Args:
            ac_index: 1-based AC index.
        """
        if 1 <= ac_index <= len(self._state.acceptance_criteria):
            ac = self._state.acceptance_criteria[ac_index - 1]
            if ac.status == ACStatus.PENDING:
                ac.start()
                self._state.current_ac_index = ac_index
                self._state.add_activity(f"Started AC #{ac_index}")

    def _mark_ac_completed(self, ac_index: int) -> None:
        """Mark an AC as completed.

        Args:
            ac_index: 1-based AC index.
        """
        if 1 <= ac_index <= len(self._state.acceptance_criteria):
            ac = self._state.acceptance_criteria[ac_index - 1]
            if ac.status in (ACStatus.PENDING, ACStatus.IN_PROGRESS):
                ac.complete()
                self._state.add_activity(f"Completed AC #{ac_index}")

                # Move to next pending AC
                self._advance_current_ac()

    def _advance_current_ac(self) -> None:
        """Advance current_ac_index to next pending AC."""
        for i, ac in enumerate(self._state.acceptance_criteria):
            if ac.status == ACStatus.PENDING:
                self._state.current_ac_index = i + 1
                return
        # All done
        self._state.current_ac_index = 0
        self._state.activity = ActivityType.FINALIZING
        self._state.activity_detail = "All ACs completed"

    def _update_phase(self) -> None:
        """Update current phase based on progress."""
        progress = self._state.progress_fraction
        if progress == 0:
            self._state.current_phase = Phase.DISCOVER
        elif progress < 0.33:
            self._state.current_phase = Phase.DEFINE
        elif progress < 0.66:
            self._state.current_phase = Phase.DEVELOP
        else:
            self._state.current_phase = Phase.DELIVER

    def to_dict(self) -> dict[str, Any]:
        """Export state as dictionary for events/serialization.

        Returns:
            State dictionary compatible with TUIState updates.
        """
        return {
            "session_id": self._state.session_id,
            "goal": self._state.goal,
            "completed_acs": self._state.completed_count,
            "total_acs": self._state.total_count,
            "progress_percent": self._state.progress_percent,
            "current_ac_index": self._state.current_ac_index,
            "activity": self._state.activity.value,
            "activity_detail": self._state.activity_detail,
            "messages_count": self._state.messages_count,
            "tool_calls_count": self._state.tool_calls_count,
            "estimated_tokens": self._state.estimated_tokens,
            "estimated_cost_usd": self._state.estimated_cost_usd,
            "elapsed_seconds": self._state.elapsed_seconds,
            "acceptance_criteria": [
                {
                    "index": ac.index,
                    "content": ac.content,
                    "status": ac.status.value,
                }
                for ac in self._state.acceptance_criteria
            ],
        }


# System prompt addition for AC tracking
AC_TRACKING_PROMPT = """
## Progress Tracking

As you work through each acceptance criterion, use these markers to track progress:
- When you START working on a criterion: [AC_START: N] (where N is the criterion number)
- When you COMPLETE a criterion: [AC_COMPLETE: N]

Example:
"[AC_START: 1] I'll begin implementing the first criterion..."
"...implementation done. [AC_COMPLETE: 1]"
"[AC_START: 2] Moving on to the second criterion..."

This helps track your progress through the acceptance criteria.
"""


def get_ac_tracking_prompt() -> str:
    """Get the AC tracking instructions to add to system prompt.

    Returns:
        Prompt text for AC tracking instructions.
    """
    return AC_TRACKING_PROMPT


__all__ = [
    "ACStatus",
    "AcceptanceCriterion",
    "ActivityType",
    "Phase",
    "WorkflowState",
    "WorkflowStateTracker",
    "get_ac_tracking_prompt",
]
