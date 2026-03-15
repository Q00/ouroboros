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

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
import re
from typing import TYPE_CHECKING, Any

from ouroboros.mcp.types import MCPToolResult
from ouroboros.orchestrator.mcp_tools import serialize_tool_result
from ouroboros.orchestrator.runtime_message_projection import project_runtime_message

if TYPE_CHECKING:
    from ouroboros.orchestrator.adapter import AgentMessage


AC_START_PATTERN = re.compile(r"\[AC_START:\s*(\d+)\]", re.IGNORECASE)
AC_COMPLETE_PATTERN = re.compile(r"\[AC_COMPLETE:\s*(\d+)\]", re.IGNORECASE)


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


@dataclass(frozen=True, slots=True)
class ACMarkerUpdate:
    """Normalized acceptance-criterion marker update."""

    started: tuple[int, ...] = ()
    completed: tuple[int, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Return True when no explicit AC markers were detected."""
        return not self.started and not self.completed

    def to_dict(self) -> dict[str, list[int]]:
        """Serialize marker indices for message/event payloads."""
        return {
            "started": list(self.started),
            "completed": list(self.completed),
        }


def _normalize_marker_indices(value: object) -> tuple[int, ...]:
    """Normalize a marker-index collection into unique positive integers."""
    if not isinstance(value, list | tuple):
        return ()

    normalized: list[int] = []
    seen: set[int] = set()
    for item in value:
        if isinstance(item, str):
            item = item.strip()
            if not item.isdigit():
                continue
            parsed = int(item)
        elif isinstance(item, int):
            parsed = item
        else:
            continue

        if parsed <= 0 or parsed in seen:
            continue
        seen.add(parsed)
        normalized.append(parsed)
    return tuple(normalized)


def _extract_text_content_items(value: object) -> tuple[str, ...]:
    """Extract text fragments from serialized MCP-style content items."""
    if not isinstance(value, list | tuple):
        return ()

    parts: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue

        item_type = item.get("type")
        if isinstance(item_type, str) and item_type.strip().lower() != "text":
            continue

        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())

    return tuple(parts)


def _extract_normalized_tool_result_text(value: object) -> str:
    """Extract text from a normalized tool-result object or serialized mapping."""
    text_content = getattr(value, "text_content", None)
    if isinstance(text_content, str) and text_content.strip():
        return text_content.strip()

    if isinstance(value, Mapping):
        serialized_text = value.get("text_content")
        if isinstance(serialized_text, str) and serialized_text.strip():
            return serialized_text.strip()

        serialized_parts = _extract_text_content_items(value.get("content"))
        if serialized_parts:
            return "\n".join(serialized_parts)

    content_items = getattr(value, "content", None)
    content_parts = _extract_text_content_items(content_items)
    if content_parts:
        return "\n".join(content_parts)

    return ""


def _collect_marker_payload_texts(value: object) -> tuple[str, ...]:
    """Collect potential marker-bearing text from normalized message payloads."""
    if not isinstance(value, Mapping):
        return ()

    texts: list[str] = []
    seen: set[str] = set()

    def add_text(candidate: object) -> None:
        text = _extract_normalized_tool_result_text(candidate)
        if text and text not in seen:
            seen.add(text)
            texts.append(text)

    if "tool_result" in value:
        add_text(value.get("tool_result"))

    # Some callers may hand us a serialized tool-result mapping directly.
    if "text_content" in value or "content" in value:
        add_text(value)

    progress = value.get("progress")
    if isinstance(progress, Mapping):
        for text in _collect_marker_payload_texts(progress):
            if text not in seen:
                seen.add(text)
                texts.append(text)

    return tuple(texts)


def _collect_marker_metadata(value: object) -> ACMarkerUpdate:
    """Collect explicit marker metadata from nested normalized progress payloads."""
    if not isinstance(value, Mapping):
        return ACMarkerUpdate()

    direct_markers = coerce_ac_marker_update(value.get("ac_tracking"))
    progress_markers = _collect_marker_metadata(value.get("progress"))
    started = tuple(dict.fromkeys((*direct_markers.started, *progress_markers.started)))
    completed = tuple(dict.fromkeys((*direct_markers.completed, *progress_markers.completed)))
    return ACMarkerUpdate(started=started, completed=completed)


def _extract_message_artifact(
    value: object,
    key: str,
) -> object:
    """Extract a normalized artifact from either the root payload or nested progress."""
    if not isinstance(value, Mapping):
        return None

    if key in value:
        return value.get(key)

    progress = value.get("progress")
    if isinstance(progress, Mapping):
        return progress.get(key)
    return None


def _normalize_tool_input_artifact(value: object) -> dict[str, Any]:
    """Normalize a tool-input artifact into a plain mapping."""
    return dict(value) if isinstance(value, Mapping) else {}


def _normalize_tool_result_artifact(value: object) -> dict[str, Any] | None:
    """Normalize tool-result artifacts into a serialization-safe mapping."""
    if isinstance(value, MCPToolResult):
        return serialize_tool_result(value)

    text_content = _extract_normalized_tool_result_text(value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {
            "content": [],
            "text_content": text_content,
            "is_error": False,
            "meta": {},
        }

        content = value.get("content")
        if isinstance(content, list | tuple):
            normalized["content"] = [dict(item) for item in content if isinstance(item, Mapping)]

        is_error = value.get("is_error")
        if isinstance(is_error, bool):
            normalized["is_error"] = is_error

        meta = value.get("meta")
        if isinstance(meta, Mapping):
            normalized["meta"] = dict(meta)

        return normalized

    if text_content:
        return {
            "content": [],
            "text_content": text_content,
            "is_error": False,
            "meta": {},
        }

    return None


def _extract_string_artifact(value: object, key: str) -> str | None:
    """Extract a normalized string artifact when present."""
    candidate = _extract_message_artifact(value, key)
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return None


def extract_ac_marker_update(content: str) -> ACMarkerUpdate:
    """Extract explicit AC marker indices from message content."""
    started = tuple(int(match.group(1)) for match in AC_START_PATTERN.finditer(content))
    completed = tuple(int(match.group(1)) for match in AC_COMPLETE_PATTERN.finditer(content))
    return ACMarkerUpdate(started=started, completed=completed)


def coerce_ac_marker_update(value: object) -> ACMarkerUpdate:
    """Deserialize marker metadata from a message/event payload."""
    if not isinstance(value, Mapping):
        return ACMarkerUpdate()

    return ACMarkerUpdate(
        started=_normalize_marker_indices(value.get("started")),
        completed=_normalize_marker_indices(value.get("completed")),
    )


def resolve_ac_marker_update(
    content: str,
    message_data: Mapping[str, Any] | None = None,
) -> ACMarkerUpdate:
    """Resolve explicit AC markers from metadata first, then content parsing."""
    message_markers = _collect_marker_metadata(message_data)
    payload_markers = extract_ac_marker_update(
        "\n".join(_collect_marker_payload_texts(message_data))
    )
    content_markers = extract_ac_marker_update(content)
    started = tuple(
        dict.fromkeys(
            (*message_markers.started, *payload_markers.started, *content_markers.started)
        )
    )
    completed = tuple(
        dict.fromkeys(
            (
                *message_markers.completed,
                *payload_markers.completed,
                *content_markers.completed,
            )
        )
    )
    return ACMarkerUpdate(started=started, completed=completed)


@dataclass
class AcceptanceCriterion:
    """State of a single acceptance criterion.

    Attributes:
        index: 1-based index of the AC.
        content: The AC description text.
        status: Current status.
        retry_attempt: Number of reopen retries for this AC (0 on first attempt).
        started_at: When work started on this AC.
        completed_at: When this AC was completed.
    """

    index: int
    content: str
    status: ACStatus = ACStatus.PENDING
    retry_attempt: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def start(self) -> None:
        """Mark AC as in progress."""
        if self.status == ACStatus.FAILED:
            self.reopen()
        self.status = ACStatus.IN_PROGRESS
        self.started_at = datetime.now(UTC)
        self.completed_at = None

    def complete(self) -> None:
        """Mark AC as completed."""
        self.status = ACStatus.COMPLETED
        self.completed_at = datetime.now(UTC)

    def fail(self) -> None:
        """Mark AC as failed."""
        self.status = ACStatus.FAILED
        self.completed_at = datetime.now(UTC)

    def reopen(self) -> None:
        """Reopen a failed AC under the same identity with a new retry attempt."""
        self.retry_attempt += 1
        self.status = ACStatus.PENDING
        self.started_at = None
        self.completed_at = None

    @property
    def attempt_number(self) -> int:
        """Human-readable execution attempt number (1-based)."""
        return self.retry_attempt + 1

    def to_progress_dict(self, *, include_elapsed_display: bool = False) -> dict[str, Any]:
        """Serialize the AC for workflow progress/event payloads."""
        data: dict[str, Any] = {
            "index": self.index,
            "content": self.content,
            "status": self.status.value,
            "retry_attempt": self.retry_attempt,
            "attempt_number": self.attempt_number,
        }
        if include_elapsed_display:
            data["elapsed_display"] = self.elapsed_display
        return data

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
        last_update: Most recent normalized runtime/message artifact snapshot.
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
    last_update: dict[str, Any] = field(default_factory=dict)

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
                ac.to_progress_dict(include_elapsed_display=True) for ac in self.acceptance_criteria
            ],
            "completed_count": self.completed_count,
            "total_count": self.total_count,
            "current_ac_index": self.current_ac_index,
            "current_phase": self.current_phase.value,
            "activity": self.activity.value,
            "activity_detail": self.activity_detail,
            "estimated_remaining": self.estimated_remaining_display,
            "elapsed_display": self.elapsed_display,
            "messages_count": self.messages_count,
            "tool_calls_count": self.tool_calls_count,
            "estimated_tokens": self.estimated_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "last_update": dict(self.last_update),
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
    AC_START_PATTERN = AC_START_PATTERN
    AC_COMPLETE_PATTERN = AC_COMPLETE_PATTERN

    # Heuristic patterns for completion detection
    COMPLETION_PATTERNS = [
        re.compile(
            r"(?:criterion|AC)\s*#?(\d+)\s*(?:is\s+)?(?:complete|done|finished|satisfied)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:completed|finished|done\s+with)\s*(?:criterion|AC)\s*#?(\d+)", re.IGNORECASE
        ),
        re.compile(r"✓\s*(?:criterion|AC)?\s*#?(\d+)", re.IGNORECASE),
    ]

    def __init__(
        self,
        acceptance_criteria: list[str],
        goal: str = "",
        session_id: str = "",
        activity_map: dict[str, ActivityType] | None = None,
    ) -> None:
        """Initialize tracker with acceptance criteria.

        Args:
            acceptance_criteria: List of AC descriptions.
            goal: The workflow goal.
            session_id: Session identifier.
            activity_map: Optional tool-to-activity mapping override.
                If provided, used instead of global TOOL_ACTIVITY_MAP.
                Typically from ExecutionStrategy.get_activity_map().
        """
        self._state = WorkflowState(
            session_id=session_id,
            goal=goal,
            acceptance_criteria=[
                AcceptanceCriterion(index=i + 1, content=ac)
                for i, ac in enumerate(acceptance_criteria)
            ],
        )
        self._activity_map = activity_map or TOOL_ACTIVITY_MAP
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
        message_data: Mapping[str, Any] | None = None,
    ) -> None:
        """Process an agent message to update state.

        Args:
            content: Message content.
            message_type: Type of message (assistant, tool, result).
            tool_name: Name of tool if this is a tool call.
            is_input: Whether this is input (True) or output (False).
            message_data: Optional normalized runtime metadata for the message.
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
        self._parse_ac_markers(content, message_data)

        # Add recent output for display (assistant messages only, not tool results)
        if message_type == "assistant" and not tool_name and content.strip():
            self._state.add_output(content)

        self._state.last_update = self._build_last_update(
            content=content,
            message_type=message_type,
            tool_name=tool_name,
            message_data=message_data,
        )

        # Update phase based on progress
        self._update_phase()

    def process_runtime_message(self, message: AgentMessage) -> None:
        """Project a runtime message through the existing state-update path."""
        projected = project_runtime_message(message)
        message_data = {**message.data, **projected.runtime_metadata}
        self.process_message(
            projected.content,
            message_type=projected.message_type,
            tool_name=projected.tool_name,
            is_input=message.type == "user",
            message_data=message_data,
        )

    def replay_progress_event(self, event_data: Mapping[str, Any]) -> None:
        """Replay a persisted progress payload back into workflow state."""
        progress = event_data.get("progress")
        if not isinstance(progress, Mapping):
            return

        message_type = event_data.get("message_type")
        tool_name = event_data.get("tool_name")
        content_preview = event_data.get("content_preview")

        if isinstance(message_type, str) and message_type.strip():
            self.process_message(
                content=(
                    str(content_preview).strip()
                    if isinstance(content_preview, str)
                    else str(progress.get("last_content_preview", "")).strip()
                ),
                message_type=message_type.strip(),
                tool_name=tool_name.strip()
                if isinstance(tool_name, str) and tool_name.strip()
                else None,
                is_input=message_type.strip() == "user",
                message_data=event_data,
            )

        self._apply_progress_snapshot(progress, message_data=event_data)

    def replay_progress_events(self, events: list[object]) -> None:
        """Replay stored progress events to rebuild workflow state on resume."""
        for event in events:
            event_type = getattr(event, "type", None)
            if event_type is not None and event_type != "orchestrator.progress.updated":
                continue

            event_data = getattr(event, "data", event)
            if isinstance(event_data, Mapping):
                self.replay_progress_event(event_data)

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
        activity = self._activity_map.get(tool_name, ActivityType.BUILDING)

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

    def _parse_ac_markers(
        self,
        content: str,
        message_data: Mapping[str, Any] | None = None,
    ) -> None:
        """Parse AC markers and heuristics from content.

        Args:
            content: Message content to parse.
            message_data: Optional message metadata carrying normalized markers.
        """
        marker_update = resolve_ac_marker_update(content, message_data)

        for ac_num in marker_update.started:
            self._mark_ac_started(ac_num)

        for ac_num in marker_update.completed:
            self._mark_ac_completed(ac_num)

        # Heuristic fallback for completion detection
        if not marker_update.is_empty:
            return

        for pattern in self.COMPLETION_PATTERNS:
            for match in pattern.finditer(content):
                ac_num = int(match.group(1))
                self._mark_ac_completed(ac_num)

    def _apply_progress_snapshot(
        self,
        progress: Mapping[str, Any],
        *,
        message_data: Mapping[str, Any] | None = None,
    ) -> None:
        """Apply non-streamed progress snapshots without double-counting messages."""
        messages_processed = progress.get("messages_processed")
        if isinstance(messages_processed, int):
            self._state.messages_count = max(self._state.messages_count, messages_processed)

        tool_name = None
        for source in (message_data, progress):
            if not isinstance(source, Mapping):
                continue
            raw_tool_name = source.get("tool_name")
            if isinstance(raw_tool_name, str) and raw_tool_name.strip():
                tool_name = raw_tool_name.strip()
                break

        if tool_name:
            self._state.last_tool = tool_name
            self._state.activity = self._activity_map.get(tool_name, ActivityType.BUILDING)
            self._state.activity_detail = tool_name

        content_preview = ""
        for source in (message_data, progress):
            if not isinstance(source, Mapping):
                continue
            for key in ("content_preview", "last_content_preview", "thinking"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    content_preview = value.strip()
                    break
            if content_preview:
                break

        self._parse_ac_markers(content_preview, message_data or progress)
        message_type = ""
        for source in (message_data, progress):
            if not isinstance(source, Mapping):
                continue
            raw_message_type = source.get("message_type") or source.get("last_message_type")
            if isinstance(raw_message_type, str) and raw_message_type.strip():
                message_type = raw_message_type.strip()
                break

        if message_type:
            self._state.last_update = self._build_last_update(
                content=content_preview,
                message_type=message_type,
                tool_name=tool_name,
                message_data=message_data or progress,
            )
        self._update_phase()

    def _build_last_update(
        self,
        *,
        content: str,
        message_type: str,
        tool_name: str | None,
        message_data: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the latest normalized message/tool artifact snapshot for state updates."""
        last_update: dict[str, Any] = {
            "message_type": message_type,
            "content_preview": content[:200],
        }

        resolved_tool_name = tool_name or _extract_string_artifact(message_data, "tool_name")
        if resolved_tool_name:
            last_update["tool_name"] = resolved_tool_name

        tool_input = _normalize_tool_input_artifact(
            _extract_message_artifact(message_data, "tool_input")
        )
        if tool_input:
            last_update["tool_input"] = tool_input

        tool_result = _normalize_tool_result_artifact(
            _extract_message_artifact(message_data, "tool_result")
        )
        if tool_result is not None:
            last_update["tool_result"] = tool_result

        thinking = _extract_string_artifact(message_data, "thinking")
        if thinking:
            last_update["thinking"] = thinking

        runtime_signal = _extract_string_artifact(message_data, "runtime_signal")
        if runtime_signal:
            last_update["runtime_signal"] = runtime_signal

        runtime_status = _extract_string_artifact(message_data, "runtime_status")
        if runtime_status:
            last_update["runtime_status"] = runtime_status

        ac_tracking = resolve_ac_marker_update(content, message_data)
        if not ac_tracking.is_empty:
            last_update["ac_tracking"] = ac_tracking.to_dict()

        return last_update

    def _mark_ac_started(self, ac_index: int) -> None:
        """Mark an AC as started.

        Args:
            ac_index: 1-based AC index.
        """
        if 1 <= ac_index <= len(self._state.acceptance_criteria):
            ac = self._state.acceptance_criteria[ac_index - 1]
            if ac.status in (ACStatus.PENDING, ACStatus.FAILED):
                ac.start()
                self._state.current_ac_index = ac_index
                if ac.retry_attempt > 0:
                    self._state.add_activity(
                        f"Reopened AC #{ac_index} (attempt {ac.attempt_number})"
                    )
                else:
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
            "last_update": dict(self._state.last_update),
            "acceptance_criteria": [
                ac.to_progress_dict() for ac in self._state.acceptance_criteria
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
    "ACMarkerUpdate",
    "AcceptanceCriterion",
    "ActivityType",
    "Phase",
    "WorkflowState",
    "WorkflowStateTracker",
    "coerce_ac_marker_update",
    "extract_ac_marker_update",
    "get_ac_tracking_prompt",
    "resolve_ac_marker_update",
]
