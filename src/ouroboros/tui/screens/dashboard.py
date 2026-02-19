"""Dashboard screen - main monitoring view.

The dashboard provides a unified view of:
- Execution status and progress
- Phase progress indicator (Double Diamond)
- Current AC being executed
- Drift metrics visualization
- Cost tracking
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    ACTreeWidget,
    AgentActivityWidget,
    CostTrackerWidget,
    DriftMeterWidget,
    PhaseProgressWidget,
)

if TYPE_CHECKING:
    from ouroboros.tui.events import TUIState


class StatusPanel(Static):
    """Panel showing current execution status.

    Uses incremental updates instead of full recompose for better performance.
    """

    DEFAULT_CSS = """
    StatusPanel {
        height: auto;
        width: 100%;
        padding: 0 1;
        border: round $primary-darken-2;
        background: $surface;
    }

    StatusPanel > .header {
        text-style: bold;
        color: $text;
        margin-bottom: 0;
    }

    StatusPanel > .status-line {
        height: 1;
        width: 100%;
        margin: 0 0;
    }

    StatusPanel > .status-line > Label {
        width: 14;
        color: $text-muted;
    }

    StatusPanel > .status-line > .value {
        width: 1fr;
        color: $text;
    }

    StatusPanel .status.idle {
        color: $text-muted;
    }

    StatusPanel .status.running {
        color: $success;
        text-style: bold;
    }

    StatusPanel .status.paused {
        color: $warning;
        text-style: bold;
    }

    StatusPanel .status.failed {
        color: $error;
        text-style: bold;
    }

    StatusPanel .status.completed {
        color: $primary;
        text-style: bold;
    }
    """

    execution_id: reactive[str] = reactive("")
    session_id: reactive[str] = reactive("")
    status: reactive[str] = reactive("idle")
    current_ac: reactive[str] = reactive("")
    activity: reactive[str] = reactive("")

    def __init__(
        self,
        execution_id: str = "",
        session_id: str = "",
        status: str = "idle",
        current_ac: str = "",
        activity: str = "",
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
            activity: Current agent activity description.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self.execution_id = execution_id
        self.session_id = session_id
        self.status = status
        self.current_ac = current_ac
        self.activity = activity

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
            yield Label("Activity:")
            yield Static(
                self.activity or "[dim]Idle[/dim]",
                classes="value activity-value",
                id="activity-value",
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
        activity: str | None = None,
    ) -> None:
        """Update status values incrementally without full recompose.

        Uses direct element updates for better performance.

        Args:
            execution_id: New execution ID.
            session_id: New session ID.
            status: New status.
            current_ac: New current AC.
            activity: New activity description.
        """
        # Update only changed values and their corresponding UI elements
        if execution_id is not None:
            self.execution_id = execution_id

        if session_id is not None:
            self.session_id = session_id

        if status is not None and status != self.status:
            old_status = self.status
            self.status = status
            try:
                elem = self.query_one("#status-value", Static)
                elem.update(self._format_status(status))
                # Update CSS class for color
                elem.remove_class(old_status)
                elem.add_class(status)
            except Exception:
                pass

        if current_ac is not None and current_ac != self.current_ac:
            self.current_ac = current_ac
            try:
                elem = self.query_one("#ac-value", Static)
                elem.update(self._truncate_ac(current_ac) or "[dim]None[/dim]")
            except Exception:
                pass

        if activity is not None and activity != self.activity:
            self.activity = activity
            try:
                elem = self.query_one("#activity-value", Static)
                elem.update(activity or "[dim]Idle[/dim]")
            except Exception:
                pass


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
        background: $background;
    }

    DashboardScreen > Container {
        height: 1fr;
        width: 100%;
        padding: 0 1;
    }

    DashboardScreen .main-content {
        layout: horizontal;
        height: 1fr;
    }

    DashboardScreen .left-panel {
        width: 1fr;
        min-width: 40;
        max-width: 50;
        padding-right: 1;
    }

    DashboardScreen .left-panel > * {
        margin-bottom: 0;
    }

    DashboardScreen .right-panel {
        width: 2fr;
        min-width: 50;
        padding-left: 1;
    }

    DashboardScreen .right-panel > * {
        margin-bottom: 0;
    }

    /* Widget styling improvements */
    DashboardScreen PhaseProgressWidget {
        border: round $primary-darken-2;
        background: $surface;
        padding: 0 1;
    }

    DashboardScreen DriftMeterWidget {
        height: auto;
        padding: 0;
    }

    DashboardScreen CostTrackerWidget {
        border: round $primary-darken-2;
        background: $surface;
        padding: 0 1;
    }

    DashboardScreen ACProgressWidget {
        border: round $primary-darken-2;
        background: $surface;
        padding: 0 1;
    }

    DashboardScreen ACTreeWidget {
        border: round $primary-darken-2;
        background: $surface;
        padding: 0;
    }

    DashboardScreen AgentActivityWidget {
        border: round $primary-darken-2;
        background: $surface;
        padding: 0 1;
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
        self._agent_activity: AgentActivityWidget | None = None
        self._phase_progress: PhaseProgressWidget | None = None
        self._drift_meter: DriftMeterWidget | None = None
        self._cost_tracker: CostTrackerWidget | None = None
        self._ac_tree: ACTreeWidget | None = None

    def compose(self) -> ComposeResult:
        """Compose the screen layout."""
        yield Header()

        with Container(), Horizontal(classes="main-content"):
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

                # Agent activity (for tool/file/thinking display)
                self._agent_activity = AgentActivityWidget()
                yield self._agent_activity

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
        # Update phase progress
        if self._phase_progress is not None:
            # Map phase name to lowercase (widget expects lowercase)
            phase = message.current_phase.lower() if message.current_phase else "discover"
            iteration = message.completed_count
            self._phase_progress.update_phase(phase, iteration)

        # Update cost tracker
        if self._cost_tracker is not None:
            self._cost_tracker.update_cost(
                total_tokens=message.estimated_tokens,
                total_cost_usd=message.estimated_cost_usd,
            )

        # Update status panel with current AC and activity
        if self._status_panel is not None:
            current_ac_content = ""
            if message.current_ac_index is not None:
                for ac in message.acceptance_criteria:
                    if ac.get("index") == message.current_ac_index:
                        current_ac_content = ac.get("content", "")
                        break
            # Format activity with detail
            activity_display = message.activity or "Idle"
            if message.activity_detail:
                activity_display = f"{message.activity}: {message.activity_detail}"
            # Check if all ACs are completed
            if message.total_count > 0 and message.completed_count >= message.total_count:
                current_status = "completed"
                activity_display = "Done"
            else:
                current_status = "running"
            self._status_panel.update_status(
                status=current_status,
                current_ac=current_ac_content,
                activity=activity_display,
            )

        # Update AC tree with flat list converted to tree format
        if self._ac_tree is not None and message.acceptance_criteria:
            tree_data = self._convert_ac_list_to_tree(
                message.acceptance_criteria,
                message.current_ac_index,
            )
            self._ac_tree.update_tree(tree_data)

        # Update agent activity panel
        if self._agent_activity is not None:
            # Parse activity_detail for tool and file info
            tool_name = message.activity or ""
            file_path = ""
            thinking = message.activity_detail or ""

            # Try to extract file path from activity_detail
            if message.activity_detail:
                detail = message.activity_detail
                # Check for common file path patterns
                if "/" in detail or "\\" in detail:
                    # Might contain a file path
                    parts = detail.split()
                    for part in parts:
                        if "/" in part or part.endswith((".py", ".ts", ".js", ".md")):
                            file_path = part.strip("'\"")
                            break

            self._agent_activity.update_activity(
                current_tool=tool_name,
                current_file=file_path,
                thinking=thinking,
            )

    def _convert_ac_list_to_tree(
        self,
        acceptance_criteria: list[dict[str, Any]],
        current_ac_index: int | None,
    ) -> dict[str, Any]:
        """Convert flat AC list to tree format for ACTreeWidget.

        Creates a simple tree with root node containing all ACs as children.

        Args:
            acceptance_criteria: List of AC dicts with index, content, status.
            current_ac_index: Index of current AC being worked on.

        Returns:
            Tree data dict with root_id and nodes.
        """
        nodes = {}
        child_ids = []

        # Create root node
        root_id = "root"

        # Create child nodes for each AC
        for ac in acceptance_criteria:
            ac_index = ac.get("index", 0)
            ac_id = f"ac_{ac_index}"
            child_ids.append(ac_id)

            # Map status from workflow to tree status
            status = ac.get("status", "pending")
            if status == "in_progress":
                status = "executing"
            elif status == "completed":
                status = "completed"
            else:
                status = "pending"

            # Check if this is the current AC
            is_current = ac_index == current_ac_index

            nodes[ac_id] = {
                "id": ac_id,
                "content": ac.get("content", ""),
                "status": status,
                "depth": 1,
                "is_atomic": True,  # Flat list = all atomic
                "children_ids": [],
            }

        # Create root node
        nodes[root_id] = {
            "id": root_id,
            "content": "Acceptance Criteria",
            "status": "executing" if current_ac_index else "pending",
            "depth": 0,
            "is_atomic": False,
            "children_ids": child_ids,
        }

        return {
            "root_id": root_id,
            "nodes": nodes,
        }

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
