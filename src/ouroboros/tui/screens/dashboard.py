"""Dashboard screen - main monitoring view.

The dashboard provides a unified view of:
- Execution status and progress
- Phase progress indicator (Double Diamond)
- Current AC being executed
- Drift metrics visualization
- Cost tracking
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, Static

from ouroboros.tui.events import (
    ACUpdated,
    CostUpdated,
    DriftUpdated,
    ExecutionUpdated,
    PauseRequested,
    PhaseChanged,
    ResumeRequested,
    WorkflowProgressUpdated,
)
from ouroboros.tui.widgets import (
    ACProgressItem,
    ACProgressWidget,
    ACTreeWidget,
    CostTrackerWidget,
    DriftMeterWidget,
    PhaseProgressWidget,
)

if TYPE_CHECKING:
    from ouroboros.tui.events import TUIState


class StatusPanel(Static):
    """Panel showing current execution status."""

    DEFAULT_CSS = """
    StatusPanel {
        height: auto;
        width: 100%;
        padding: 1;
        border: solid $surface;
    }

    StatusPanel > .header {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    StatusPanel > .status-line {
        height: 1;
        width: 100%;
    }

    StatusPanel > .status-line > Label {
        width: 15;
    }

    StatusPanel > .status-line > .value {
        width: 1fr;
    }

    StatusPanel > .status.running {
        color: $success;
    }

    StatusPanel > .status.paused {
        color: $warning;
    }

    StatusPanel > .status.failed {
        color: $error;
    }

    StatusPanel > .status.completed {
        color: $primary;
    }
    """

    execution_id: reactive[str] = reactive("")
    session_id: reactive[str] = reactive("")
    status: reactive[str] = reactive("idle")
    current_ac: reactive[str] = reactive("")

    def __init__(
        self,
        execution_id: str = "",
        session_id: str = "",
        status: str = "idle",
        current_ac: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize status panel.

        Args:
            execution_id: Current execution ID.
            session_id: Current session ID.
            status: Current status.
            current_ac: Current acceptance criterion.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self.execution_id = execution_id
        self.session_id = session_id
        self.status = status
        self.current_ac = current_ac

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Label("Execution Status", classes="header")

        with Horizontal(classes="status-line"):
            yield Label("Status:")
            yield Static(
                self._format_status(self.status),
                classes=f"value status {self.status}",
                id="status-value",
            )

        with Horizontal(classes="status-line"):
            yield Label("Execution:")
            yield Static(
                self.execution_id or "[dim]None[/dim]",
                classes="value",
                id="execution-value",
            )

        with Horizontal(classes="status-line"):
            yield Label("Session:")
            yield Static(
                self.session_id or "[dim]None[/dim]",
                classes="value",
                id="session-value",
            )

        with Horizontal(classes="status-line"):
            yield Label("Current AC:")
            yield Static(
                self._truncate_ac(self.current_ac) or "[dim]None[/dim]",
                classes="value",
                id="ac-value",
            )

    def _format_status(self, status: str) -> str:
        """Format status for display."""
        status_icons = {
            "idle": "[ ] Idle",
            "running": "[*] Running",
            "paused": "[||] Paused",
            "completed": "[OK] Completed",
            "failed": "[X] Failed",
        }
        return status_icons.get(status, status)

    def _truncate_ac(self, ac: str) -> str:
        """Truncate AC for display."""
        if len(ac) > 50:
            return ac[:47] + "..."
        return ac

    def update_status(
        self,
        execution_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        current_ac: str | None = None,
    ) -> None:
        """Update status values.

        Args:
            execution_id: New execution ID.
            session_id: New session ID.
            status: New status.
            current_ac: New current AC.
        """
        if execution_id is not None:
            self.execution_id = execution_id
        if session_id is not None:
            self.session_id = session_id
        if status is not None:
            self.status = status
        if current_ac is not None:
            self.current_ac = current_ac

        # Force refresh
        self.refresh(recompose=True)


class DashboardScreen(Screen[None]):
    """Main dashboard screen for monitoring execution.

    Provides unified view of execution status, phase progress,
    drift metrics, and cost tracking.

    Bindings:
        p: Pause execution
        r: Resume execution
        l: Switch to logs view
        d: Switch to debug view
        e: Switch to execution detail view
    """

    BINDINGS = [
        Binding("p", "pause", "Pause"),
        Binding("r", "resume", "Resume"),
        Binding("l", "logs", "Logs"),
        Binding("d", "debug", "Debug"),
        Binding("e", "execution", "Execution"),
    ]

    DEFAULT_CSS = """
    DashboardScreen {
        layout: vertical;
    }

    DashboardScreen > Container {
        height: 1fr;
        width: 100%;
        padding: 1;
    }

    DashboardScreen .main-content {
        layout: horizontal;
        height: 1fr;
    }

    DashboardScreen .left-panel {
        width: 1fr;
        min-width: 40;
        padding-right: 1;
    }

    DashboardScreen .right-panel {
        width: 1fr;
        min-width: 40;
        padding-left: 1;
    }

    DashboardScreen .bottom-panel {
        height: auto;
        max-height: 15;
        margin-top: 1;
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
        """Initialize dashboard screen.

        Args:
            state: Initial TUI state.
            name: Screen name.
            id: Screen ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._state = state
        self._status_panel: StatusPanel | None = None
        self._phase_progress: PhaseProgressWidget | None = None
        self._drift_meter: DriftMeterWidget | None = None
        self._cost_tracker: CostTrackerWidget | None = None
        self._ac_tree: ACTreeWidget | None = None
        self._ac_progress: ACProgressWidget | None = None

    def compose(self) -> ComposeResult:
        """Compose the screen layout."""
        yield Header()

        with Container():
            with Horizontal(classes="main-content"):
                with Vertical(classes="left-panel"):
                    # Status panel
                    self._status_panel = StatusPanel(
                        execution_id=self._state.execution_id if self._state else "",
                        session_id=self._state.session_id if self._state else "",
                        status=self._state.status if self._state else "idle",
                    )
                    yield self._status_panel

                    # Phase progress
                    self._phase_progress = PhaseProgressWidget(
                        current_phase=self._state.current_phase if self._state else "",
                        iteration=self._state.iteration if self._state else 0,
                    )
                    yield self._phase_progress

                    # AC progress list
                    self._ac_progress = ACProgressWidget()
                    yield self._ac_progress

                    # Cost tracker
                    self._cost_tracker = CostTrackerWidget(
                        total_tokens=self._state.total_tokens if self._state else 0,
                        total_cost_usd=self._state.total_cost_usd if self._state else 0.0,
                    )
                    yield self._cost_tracker

                with Vertical(classes="right-panel"):
                    # Drift meter
                    self._drift_meter = DriftMeterWidget(
                        goal_drift=self._state.goal_drift if self._state else 0.0,
                        constraint_drift=self._state.constraint_drift if self._state else 0.0,
                        ontology_drift=self._state.ontology_drift if self._state else 0.0,
                    )
                    yield self._drift_meter

                    # AC tree
                    self._ac_tree = ACTreeWidget(
                        tree_data=self._state.ac_tree if self._state else {},
                    )
                    yield self._ac_tree

        yield Footer()

    def on_execution_updated(self, message: ExecutionUpdated) -> None:
        """Handle execution update message.

        Args:
            message: Execution update message.
        """
        if self._status_panel is not None:
            self._status_panel.update_status(
                execution_id=message.execution_id,
                session_id=message.session_id,
                status=message.status,
            )

    def on_phase_changed(self, message: PhaseChanged) -> None:
        """Handle phase change message.

        Args:
            message: Phase change message.
        """
        if self._phase_progress is not None:
            self._phase_progress.update_phase(
                message.current_phase,
                message.iteration,
            )
        if self._cost_tracker is not None:
            self._cost_tracker.reset_phase_tokens()

    def on_drift_updated(self, message: DriftUpdated) -> None:
        """Handle drift update message.

        Args:
            message: Drift update message.
        """
        if self._drift_meter is not None:
            self._drift_meter.update_drift(
                goal_drift=message.goal_drift,
                constraint_drift=message.constraint_drift,
                ontology_drift=message.ontology_drift,
            )

    def on_cost_updated(self, message: CostUpdated) -> None:
        """Handle cost update message.

        Args:
            message: Cost update message.
        """
        if self._cost_tracker is not None:
            self._cost_tracker.update_cost(
                total_tokens=message.total_tokens,
                total_cost_usd=message.total_cost_usd,
                tokens_this_phase=message.tokens_this_phase,
            )

    def on_ac_updated(self, message: ACUpdated) -> None:
        """Handle AC update message.

        Args:
            message: AC update message.
        """
        if self._ac_tree is not None:
            self._ac_tree.update_node_status(message.ac_id, message.status)

    def on_workflow_progress_updated(self, message: WorkflowProgressUpdated) -> None:
        """Handle workflow progress update message.

        Args:
            message: Workflow progress update message.
        """
        if self._ac_progress is not None:
            # Convert message data to ACProgressItem list
            items = [
                ACProgressItem(
                    index=ac.get("index", 0),
                    content=ac.get("content", ""),
                    status=ac.get("status", "pending"),
                    elapsed_display=ac.get("elapsed_display", ""),
                    is_current=ac.get("index") == message.current_ac_index,
                )
                for ac in message.acceptance_criteria
            ]
            self._ac_progress.update_progress(
                acceptance_criteria=items,
                completed_count=message.completed_count,
                total_count=message.total_count,
                estimated_remaining=message.estimated_remaining,
            )

        # Also update status panel with current AC
        if self._status_panel is not None and message.current_ac_index is not None:
            for ac in message.acceptance_criteria:
                if ac.get("index") == message.current_ac_index:
                    self._status_panel.update_status(current_ac=ac.get("content", ""))
                    break

    def action_pause(self) -> None:
        """Handle pause action."""
        if self._state and self._state.execution_id:
            self.post_message(PauseRequested(self._state.execution_id))

    def action_resume(self) -> None:
        """Handle resume action."""
        if self._state and self._state.execution_id:
            self.post_message(ResumeRequested(self._state.execution_id))

    def action_logs(self) -> None:
        """Switch to logs screen."""
        self.app.push_screen("logs")

    def action_debug(self) -> None:
        """Switch to debug screen."""
        self.app.push_screen("debug")

    def action_execution(self) -> None:
        """Switch to execution detail screen."""
        self.app.push_screen("execution")

    def update_state(self, state: TUIState) -> None:
        """Update the entire state.

        Args:
            state: New TUI state.
        """
        self._state = state

        if self._status_panel is not None:
            self._status_panel.update_status(
                execution_id=state.execution_id,
                session_id=state.session_id,
                status=state.status,
            )

        if self._phase_progress is not None:
            self._phase_progress.update_phase(state.current_phase, state.iteration)

        if self._drift_meter is not None:
            self._drift_meter.update_drift(
                goal_drift=state.goal_drift,
                constraint_drift=state.constraint_drift,
                ontology_drift=state.ontology_drift,
            )

        if self._cost_tracker is not None:
            self._cost_tracker.update_cost(
                total_tokens=state.total_tokens,
                total_cost_usd=state.total_cost_usd,
            )

        if self._ac_tree is not None:
            self._ac_tree.update_tree(state.ac_tree)


__all__ = ["DashboardScreen", "StatusPanel"]
