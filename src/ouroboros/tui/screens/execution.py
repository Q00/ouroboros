"""Execution detail screen.

Provides detailed view of the current execution including:
- Full execution history
- Phase-by-phase outputs
- Decomposition details
- Event timeline
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Label, Static

if TYPE_CHECKING:
    from ouroboros.tui.events import TUIState, WorkflowProgressUpdated


class EventTimelineItem(Static):
    """Single item in the event timeline."""

    DEFAULT_CSS = """
    EventTimelineItem {
        height: auto;
        width: 100%;
        padding: 0 1;
        margin-bottom: 1;
    }

    EventTimelineItem > .timestamp {
        width: 20;
        color: $text-muted;
    }

    EventTimelineItem > .event-type {
        width: auto;
        padding: 0 1;
    }

    EventTimelineItem > .event-type.phase {
        color: $primary;
    }

    EventTimelineItem > .event-type.session {
        color: $secondary;
    }

    EventTimelineItem > .event-type.drift {
        color: $warning;
    }

    EventTimelineItem > .event-type.tool {
        color: $accent;
    }

    EventTimelineItem > .details {
        width: 1fr;
        color: $text;
    }
    """

    def __init__(
        self,
        timestamp: datetime,
        event_type: str,
        details: str,
        category: str = "general",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize event timeline item.

        Args:
            timestamp: When the event occurred.
            event_type: Type of event.
            details: Event details.
            category: Event category for styling.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._timestamp = timestamp
        self._event_type = event_type
        self._details = details
        self._category = category

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        with Horizontal():
            yield Static(
                self._timestamp.strftime("%H:%M:%S"),
                classes="timestamp",
            )
            yield Static(
                f"[{self._event_type}]",
                classes=f"event-type {self._category}",
            )
            yield Static(self._details, classes="details")


class PhaseOutputPanel(Static):
    """Panel showing output from a specific phase."""

    DEFAULT_CSS = """
    PhaseOutputPanel {
        height: auto;
        min-height: 5;
        width: 100%;
        padding: 1;
        margin-bottom: 1;
        border: solid $surface;
    }

    PhaseOutputPanel > .phase-header {
        text-style: bold;
        margin-bottom: 1;
    }

    PhaseOutputPanel > .phase-header.discover {
        color: $secondary;
    }

    PhaseOutputPanel > .phase-header.define {
        color: $primary;
    }

    PhaseOutputPanel > .phase-header.design {
        color: $secondary;
    }

    PhaseOutputPanel > .phase-header.deliver {
        color: $primary;
    }

    PhaseOutputPanel > .output {
        width: 100%;
    }

    PhaseOutputPanel > .empty {
        color: $text-muted;
        text-style: italic;
    }
    """

    def __init__(
        self,
        phase_name: str,
        output: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize phase output panel.

        Args:
            phase_name: Name of the phase.
            output: Phase output text.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._phase_name = phase_name
        self._output = output

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        phase_labels = {
            "discover": "Discover (Diverge)",
            "define": "Define (Converge)",
            "design": "Design (Diverge)",
            "deliver": "Deliver (Converge)",
        }
        label = phase_labels.get(self._phase_name, self._phase_name.title())
        yield Label(label, classes=f"phase-header {self._phase_name}")

        if self._output:
            # Truncate long output
            display_output = self._output
            if len(display_output) > 500:
                display_output = display_output[:500] + "..."
            yield Static(display_output, classes="output")
        else:
            yield Static("No output yet", classes="empty")


class ExecutionScreen(Screen[None]):
    """Execution detail screen.

    Shows detailed view of the current execution with
    phase outputs, timeline, and full history.

    Bindings:
        escape: Return to dashboard
        r: Refresh view
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]

    DEFAULT_CSS = """
    ExecutionScreen {
        layout: vertical;
    }

    ExecutionScreen > .screen-header {
        dock: top;
        height: 1;
        width: 100%;
        background: $primary;
        text-align: center;
        padding: 0 1;
    }

    ExecutionScreen > Container {
        height: 1fr;
        width: 100%;
        padding: 1;
    }

    ExecutionScreen .panels {
        layout: horizontal;
        height: 1fr;
    }

    ExecutionScreen .left-panel {
        width: 1fr;
        min-width: 40;
        padding-right: 1;
    }

    ExecutionScreen .right-panel {
        width: 1fr;
        min-width: 40;
        padding-left: 1;
    }

    ExecutionScreen .section-header {
        text-style: bold;
        text-align: center;
        border-bottom: solid $surface;
        margin-bottom: 1;
        padding-bottom: 1;
    }

    ExecutionScreen VerticalScroll {
        height: 1fr;
    }
    """

    def __init__(
        self,
        state: TUIState | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize execution screen.

        Args:
            state: Initial TUI state.
            name: Screen name.
            id: Screen ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._state = state
        self._phase_outputs: dict[str, str] = {}
        self._events: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        """Compose the screen layout."""
        yield Static(
            "[bold blue]Ouroboros TUI[/bold blue] — [dim]Execution Detail[/dim]",
            classes="screen-header",
        )

        with Container():
            exec_id = self._state.execution_id if self._state else "None"
            yield Label(f"Execution: {exec_id}", classes="section-header")

            with Horizontal(classes="panels"):
                with Vertical(classes="left-panel"):
                    yield Label("Phase Outputs", classes="section-header")

                    with VerticalScroll():
                        for phase in ["discover", "define", "design", "deliver"]:
                            output = self._phase_outputs.get(phase, "")
                            yield PhaseOutputPanel(phase, output)

                with Vertical(classes="right-panel"):
                    yield Label("Event Timeline", classes="section-header")

                    with VerticalScroll():
                        if self._events:
                            for event_data in self._events[-20:]:  # Last 20 events
                                yield EventTimelineItem(
                                    timestamp=datetime.fromisoformat(
                                        event_data.get("timestamp", datetime.now(UTC).isoformat())
                                    ),
                                    event_type=event_data.get("type", "unknown"),
                                    details=event_data.get("details", ""),
                                    category=event_data.get("category", "general"),
                                )
                        else:
                            yield Static(
                                "[dim]No events recorded[/dim]",
                                classes="empty",
                            )

        yield Footer()

    def action_back(self) -> None:
        """Return to dashboard."""
        self.app.pop_screen()

    def action_refresh(self) -> None:
        """Refresh the view."""
        self.refresh(recompose=True)

    def update_phase_output(self, phase: str, output: str) -> None:
        """Update output for a phase.

        Args:
            phase: Phase name.
            output: Phase output text.
        """
        self._phase_outputs[phase] = output
        self.refresh(recompose=True)

    def add_event(
        self,
        event_type: str,
        details: str,
        category: str = "general",
    ) -> None:
        """Add an event to the timeline.

        Args:
            event_type: Type of event.
            details: Event details.
            category: Event category.
        """
        self._events.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "type": event_type,
                "details": details,
                "category": category,
            }
        )
        # Keep only last 100 events
        if len(self._events) > 100:
            self._events = self._events[-100:]

    def update_state(self, state: TUIState) -> None:
        """Update the entire state.

        Args:
            state: New TUI state.
        """
        self._state = state
        self.refresh(recompose=True)

    def on_phase_changed(self, message: Any) -> None:
        """Handle phase change event.

        Updates the phase outputs and adds an event to the timeline.

        Args:
            message: PhaseChanged message with phase transition info.
        """
        # Add event to timeline
        if hasattr(message, "current_phase"):
            current = message.current_phase
            previous = getattr(message, "previous_phase", None)
            iteration = getattr(message, "iteration", 0)

            # Build details string
            if previous:
                details = f"{previous} → {current} (iteration {iteration})"
            else:
                details = f"Started {current} (iteration {iteration})"

            self.add_event(
                event_type="phase_changed",
                details=details,
                category="phase",
            )

            # Initialize phase output placeholder if not set
            if current and current not in self._phase_outputs:
                self._phase_outputs[current] = ""

            self.refresh(recompose=True)

    def on_execution_updated(self, message: Any) -> None:
        """Handle execution update event.

        Adds an event to the timeline for execution status changes.

        Args:
            message: ExecutionUpdated message with status info.
        """
        if hasattr(message, "status"):
            status = message.status
            exec_id = getattr(message, "execution_id", "unknown")

            self.add_event(
                event_type="execution_status",
                details=f"Execution {exec_id}: {status}",
                category="session",
            )

            # Extract phase output from event data if available
            data = getattr(message, "data", {})
            if data:
                # Check for phase output in data
                phase_output = data.get("phase_output")
                phase_name = data.get("phase")
                if phase_output and phase_name:
                    self.update_phase_output(phase_name, phase_output)
            else:
                self.refresh(recompose=True)

    def on_workflow_progress_updated(self, message: WorkflowProgressUpdated) -> None:
        """Handle workflow progress update event.

        Updates event timeline and phase outputs from workflow progress.

        Args:
            message: WorkflowProgressUpdated message with progress info.
        """
        # Add activity to event timeline
        activity = getattr(message, "activity", "idle")
        activity_detail = getattr(message, "activity_detail", "")
        current_phase = getattr(message, "current_phase", "Discover")
        completed = getattr(message, "completed_count", 0)
        total = getattr(message, "total_count", 0)

        # Map phase name to lowercase key
        phase_key = current_phase.lower() if current_phase else "discover"

        # Add event to timeline
        if activity_detail:
            self.add_event(
                event_type=activity,
                details=f"{activity_detail} ({completed}/{total} ACs)",
                category="tool" if activity in ("exploring", "building", "testing") else "phase",
            )

        # Update phase output with current activity
        if phase_key in self._phase_outputs or phase_key in [
            "discover",
            "define",
            "design",
            "deliver",
        ]:
            current_output = self._phase_outputs.get(phase_key, "")
            if activity_detail and activity_detail not in current_output:
                # Append new activity to phase output
                new_output = (
                    f"{current_output}\n• {activity_detail}"
                    if current_output
                    else f"• {activity_detail}"
                )
                # Keep only last 500 chars
                if len(new_output) > 500:
                    new_output = "..." + new_output[-497:]
                self._phase_outputs[phase_key] = new_output

        self.refresh(recompose=True)


__all__ = ["EventTimelineItem", "ExecutionScreen", "PhaseOutputPanel"]
