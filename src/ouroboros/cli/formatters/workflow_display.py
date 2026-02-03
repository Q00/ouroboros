"""Rich Live display for workflow progress monitoring.

This module provides a Rich-based live display for monitoring workflow
execution progress in the CLI. It renders the WorkflowState as a
formatted panel with AC progress, activity, and metrics.

Usage:
    from ouroboros.cli.formatters.workflow_display import WorkflowDisplay

    async with WorkflowDisplay(tracker) as display:
        async for message in adapter.execute_task(...):
            tracker.process_message(message.content, ...)
            display.refresh()
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

from rich import box
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskID, TextColumn
from rich.table import Table
from rich.text import Text

from ouroboros.cli.formatters import console

if TYPE_CHECKING:
    from ouroboros.orchestrator.workflow_state import (
        ACStatus,
        WorkflowState,
        WorkflowStateTracker,
    )


# Status icons for AC display (icon, style)
AC_STATUS_ICONS: dict[str, tuple[str, str]] = {
    "pending": ("○", "dim"),
    "in_progress": ("⚡", "yellow"),
    "completed": ("✓", "green"),
    "failed": ("✗", "red"),
}

# Activity type icons
ACTIVITY_ICONS = {
    "idle": "💤",
    "exploring": "🔍",
    "building": "🛠️",
    "testing": "🧪",
    "debugging": "🐛",
    "documenting": "📝",
    "finalizing": "🎉",
}


def _format_ac_line(
    index: int,
    content: str,
    status: str,
    is_current: bool,
    max_width: int = 60,
) -> Text:
    """Format a single AC line for display.

    Args:
        index: 1-based AC index.
        content: AC content text.
        status: AC status string.
        is_current: Whether this is the current AC being worked on.
        max_width: Maximum width for content truncation.

    Returns:
        Formatted Rich Text object.
    """
    icon, icon_style = AC_STATUS_ICONS.get(status, ("○", "dim"))

    # Truncate content if needed
    if len(content) > max_width:
        content = content[: max_width - 3] + "..."

    # Build the line
    line = Text()
    line.append(" ")
    line.append(icon, style=icon_style)
    line.append("  ")
    line.append(f"{index}. ", style="bold" if is_current else "dim")
    line.append(content)

    # Add current marker
    if is_current and status == "in_progress":
        line.append("  ", style="dim")
        line.append("<- current", style="yellow italic")

    return line


def _build_progress_bar(completed: int, total: int) -> Progress:
    """Build a progress bar for AC completion.

    Args:
        completed: Number of completed ACs.
        total: Total number of ACs.

    Returns:
        Rich Progress object.
    """
    progress = Progress(
        BarColumn(bar_width=None),  # None = expand to fill width
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
        transient=True,
        expand=True,
    )
    progress.add_task("", total=total, completed=completed)
    return progress


def render_workflow_state(state: WorkflowState) -> Panel:
    """Render workflow state as a Rich Panel.

    Args:
        state: The workflow state to render.

    Returns:
        Rich Panel containing the formatted display.
    """
    from ouroboros.orchestrator.workflow_state import ACStatus

    # Build header with session ID and elapsed time
    header = Text()
    header.append(state.session_id or "workflow", style="bold cyan")
    header.append(f"  ⏱️  {state.elapsed_display}", style="dim")

    # Build goal line (truncated)
    goal_text = state.goal[:70] + "..." if len(state.goal) > 70 else state.goal
    goal_line = Text(f"Goal: {goal_text}", style="italic dim")

    # Build AC section header
    ac_header = Text()
    ac_header.append("Acceptance Criteria ", style="bold")
    ac_header.append(f"{state.completed_count}/{state.total_count} complete ", style="cyan")
    ac_header.append(f"({state.progress_percent}%)", style="dim")

    # Build AC list
    ac_lines: list[Text] = []
    for ac in state.acceptance_criteria:
        is_current = ac.index == state.current_ac_index
        line = _format_ac_line(
            ac.index,
            ac.content,
            ac.status.value,
            is_current,
        )
        ac_lines.append(line)

    # Build progress bar
    progress_bar = _build_progress_bar(state.completed_count, state.total_count)

    # Build activity section
    activity_icon = ACTIVITY_ICONS.get(state.activity.value, "💻")
    activity_text = Text()
    activity_text.append(f" {activity_icon} ", style="bold")
    activity_text.append(state.activity.value.title(), style="bold")
    if state.activity_detail:
        activity_text.append(f" | {state.activity_detail}", style="dim")

    # Build metrics footer with individual boxes using Table for even distribution
    metrics = Table(box=None, expand=True, show_header=False, padding=(0, 1))
    metrics.add_column(ratio=1)
    metrics.add_column(ratio=1)
    metrics.add_column(ratio=1)
    metrics.add_column(ratio=1)
    metrics.add_row(
        Panel(f"📨 {state.messages_count} msgs", box=box.ASCII, padding=(0, 1)),
        Panel(f"🔧 {state.tool_calls_count} tools", box=box.ASCII, padding=(0, 1)),
        Panel(f"📊 ~{state.estimated_tokens // 1000}K tokens", box=box.ASCII, padding=(0, 1)),
        Panel(f"💰 ~${state.estimated_cost_usd:.2f}", box=box.ASCII, padding=(0, 1)),
    )

    # Compose the panel content
    content_parts = [
        header,
        goal_line,
        Text(""),  # Spacer
        ac_header,
    ]
    content_parts.extend(ac_lines)
    content_parts.append(Text(""))  # Spacer

    # Create a renderables group
    from rich.console import Group as RenderGroup

    renderables = [
        header,
        goal_line,
        Text(""),
        ac_header,
        *ac_lines,
        Text(""),
        progress_bar,
        Text(""),
        activity_text,
        Text(""),
        metrics,
    ]

    return Panel(
        RenderGroup(*renderables),
        title="[bold blue]Ouroboros Workflow[/bold blue]",
        border_style="blue",
        box=box.ASCII,
        padding=(1, 2),
    )


class WorkflowDisplay:
    """Rich Live display manager for workflow progress.

    Wraps Rich's Live display to show real-time workflow progress
    with automatic refresh.

    Attributes:
        tracker: The workflow state tracker to render.
        live: The Rich Live instance.
    """

    def __init__(
        self,
        tracker: WorkflowStateTracker,
        refresh_per_second: float = 4,
    ) -> None:
        """Initialize workflow display.

        Args:
            tracker: Workflow state tracker to render.
            refresh_per_second: How often to refresh the display.
        """
        self._tracker = tracker
        self._refresh_rate = refresh_per_second
        self._live: Live | None = None

    def _render(self) -> Panel:
        """Render current state as a panel.

        Returns:
            Rich Panel with current workflow state.
        """
        return render_workflow_state(self._tracker.state)

    def start(self) -> None:
        """Start the live display."""
        self._live = Live(
            self._render(),
            console=console,
            refresh_per_second=self._refresh_rate,
            transient=False,
        )
        self._live.start()

    def stop(self) -> None:
        """Stop the live display."""
        if self._live:
            self._live.stop()
            self._live = None

    def refresh(self) -> None:
        """Refresh the display with current state."""
        if self._live:
            self._live.update(self._render())

    def __enter__(self) -> "WorkflowDisplay":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        """Context manager exit."""
        self.stop()


@asynccontextmanager
async def workflow_display(
    tracker: WorkflowStateTracker,
    refresh_per_second: float = 4,
) -> AsyncIterator[WorkflowDisplay]:
    """Async context manager for workflow display.

    Args:
        tracker: Workflow state tracker to render.
        refresh_per_second: How often to refresh the display.

    Yields:
        WorkflowDisplay instance.

    Example:
        async with workflow_display(tracker) as display:
            async for message in adapter.execute_task(...):
                tracker.process_message(...)
                display.refresh()
    """
    display = WorkflowDisplay(tracker, refresh_per_second)
    try:
        display.start()
        yield display
    finally:
        display.stop()


__all__ = [
    "WorkflowDisplay",
    "render_workflow_state",
    "workflow_display",
]
