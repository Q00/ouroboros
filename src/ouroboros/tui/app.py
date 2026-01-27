"""Main TUI application using Textual framework.

OuroborosTUI is the main application class that:
- Manages screens (dashboard, execution, logs, debug)
- Handles global keybindings
- Subscribes to EventStore for live updates
- Provides pause/resume execution control

Usage:
    from ouroboros.tui import OuroborosTUI

    # Create and run the TUI
    app = OuroborosTUI()
    await app.run_async()

    # Or with an existing event store
    app = OuroborosTUI(event_store=store)
    await app.run_async()
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import App
from textual.binding import Binding

from ouroboros.tui.events import (
    ACUpdated,
    CostUpdated,
    DriftUpdated,
    ExecutionUpdated,
    LogMessage,
    PauseRequested,
    PhaseChanged,
    ResumeRequested,
    TUIState,
    create_message_from_event,
)
from ouroboros.tui.screens import (
    DashboardScreen,
    DebugScreen,
    ExecutionScreen,
    LogsScreen,
)

if TYPE_CHECKING:
    from ouroboros.events.base import BaseEvent
    from ouroboros.persistence.event_store import EventStore


class OuroborosTUI(App[None]):
    """Main Textual application for Ouroboros TUI.

    Provides real-time monitoring of Ouroboros workflow execution
    with multiple views and keyboard navigation.

    Screens:
        - dashboard: Main monitoring view (default)
        - execution: Execution detail view
        - logs: Log viewer
        - debug: Debug/inspect view

    Global Bindings:
        q: Quit application
        p: Pause execution
        r: Resume execution
        d: Switch to debug view
        l: Switch to logs view
        1: Switch to dashboard
        2: Switch to execution view
        3: Switch to logs view
        4: Switch to debug view

    Attributes:
        event_store: Optional EventStore for live updates.
        state: Current TUI state.
    """

    TITLE = "Ouroboros TUI"
    SUB_TITLE = "Workflow Monitor"

    CSS = """
    Screen {
        background: $background;
    }

    Header {
        background: $primary;
    }

    Footer {
        background: $surface;
    }

    .hidden {
        display: none;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("p", "pause", "Pause"),
        Binding("r", "resume", "Resume"),
        Binding("d", "show_debug", "Debug"),
        Binding("l", "show_logs", "Logs"),
        Binding("1", "show_dashboard", "Dashboard", show=False),
        Binding("2", "show_execution", "Execution", show=False),
        Binding("3", "show_logs", "Logs", show=False),
        Binding("4", "show_debug", "Debug", show=False),
    ]

    # Note: We don't use SCREENS class variable because we need to pass
    # state to screens. Screens are installed in on_mount() instead.

    def __init__(
        self,
        event_store: EventStore | None = None,
        execution_id: str | None = None,
        *,
        driver_class: type | None = None,
    ) -> None:
        """Initialize OuroborosTUI.

        Args:
            event_store: Optional EventStore for live updates.
            execution_id: Optional execution ID to monitor.
            driver_class: Optional Textual driver class for testing.
        """
        super().__init__(driver_class=driver_class)
        self._event_store = event_store
        self._execution_id = execution_id
        self._state = TUIState()
        self._subscription_task: asyncio.Task[None] | None = None
        self._is_paused = False

        # Callbacks for pause/resume control
        self._pause_callback: Any | None = None
        self._resume_callback: Any | None = None

    @property
    def state(self) -> TUIState:
        """Get current TUI state."""
        return self._state

    def on_mount(self) -> None:
        """Handle application mount."""
        # Install screens
        self.install_screen(DashboardScreen(self._state), name="dashboard")
        self.install_screen(ExecutionScreen(self._state), name="execution")
        self.install_screen(LogsScreen(self._state), name="logs")
        self.install_screen(DebugScreen(self._state), name="debug")

        # Show dashboard by default
        self.push_screen("dashboard")

        # Start event subscription if event store provided
        if self._event_store is not None:
            self._start_event_subscription()

    def _start_event_subscription(self) -> None:
        """Start background task for event subscription."""
        if self._subscription_task is not None:
            return

        self._subscription_task = asyncio.create_task(
            self._subscribe_to_events()
        )

    async def _subscribe_to_events(self) -> None:
        """Subscribe to EventStore for live updates.

        Polls the event store periodically for new events
        and converts them to TUI messages.
        """
        if self._event_store is None:
            return

        last_event_count = 0
        poll_interval = 0.5  # seconds

        while True:
            try:
                await asyncio.sleep(poll_interval)

                if self._execution_id:
                    # Replay events for the specific execution
                    events = await self._event_store.replay(
                        "execution", self._execution_id
                    )

                    # Process only new events
                    for event in events[last_event_count:]:
                        message = create_message_from_event(event)
                        if message is not None:
                            self.post_message(message)
                            self._update_state_from_event(event)

                    last_event_count = len(events)

            except asyncio.CancelledError:
                break
            except Exception as e:
                # Log error but continue polling
                self._state.add_log(
                    "error",
                    "tui.subscription",
                    f"Event subscription error: {e}",
                )

    def _update_state_from_event(self, event: BaseEvent) -> None:
        """Update internal state from an event.

        Args:
            event: The event to process.
        """
        event_type = event.type
        data = event.data

        if event_type == "orchestrator.session.started":
            self._state.execution_id = data.get("execution_id", "")
            self._state.session_id = event.aggregate_id
            self._state.status = "running"

        elif event_type == "orchestrator.session.completed":
            self._state.status = "completed"

        elif event_type == "orchestrator.session.failed":
            self._state.status = "failed"

        elif event_type == "orchestrator.session.paused":
            self._state.status = "paused"
            self._state.is_paused = True

        elif event_type == "execution.phase.completed":
            self._state.current_phase = data.get("phase", "")
            self._state.iteration = data.get("iteration", 0)

        elif event_type == "observability.drift.measured":
            self._state.goal_drift = data.get("goal_drift", 0.0)
            self._state.constraint_drift = data.get("constraint_drift", 0.0)
            self._state.ontology_drift = data.get("ontology_drift", 0.0)
            self._state.combined_drift = data.get("combined_drift", 0.0)

        elif event_type == "observability.cost.updated":
            self._state.total_tokens = data.get("total_tokens", 0)
            self._state.total_cost_usd = data.get("total_cost_usd", 0.0)

    def on_execution_updated(self, message: ExecutionUpdated) -> None:
        """Handle execution update message.

        Args:
            message: Execution update message.
        """
        self._state.execution_id = message.execution_id
        self._state.session_id = message.session_id
        self._state.status = message.status
        self._state.is_paused = message.status == "paused"

        # Forward to current screen (if mounted)
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_execution_updated"):
                screen.on_execution_updated(message)

    def on_phase_changed(self, message: PhaseChanged) -> None:
        """Handle phase change message.

        Args:
            message: Phase change message.
        """
        self._state.current_phase = message.current_phase
        self._state.iteration = message.iteration

        # Forward to current screen (if mounted)
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_phase_changed"):
                screen.on_phase_changed(message)

    def on_drift_updated(self, message: DriftUpdated) -> None:
        """Handle drift update message.

        Args:
            message: Drift update message.
        """
        self._state.goal_drift = message.goal_drift
        self._state.constraint_drift = message.constraint_drift
        self._state.ontology_drift = message.ontology_drift
        self._state.combined_drift = message.combined_drift

        # Forward to current screen (if mounted)
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_drift_updated"):
                screen.on_drift_updated(message)

    def on_cost_updated(self, message: CostUpdated) -> None:
        """Handle cost update message.

        Args:
            message: Cost update message.
        """
        self._state.total_tokens = message.total_tokens
        self._state.total_cost_usd = message.total_cost_usd

        # Forward to current screen (if mounted)
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_cost_updated"):
                screen.on_cost_updated(message)

    def on_ac_updated(self, message: ACUpdated) -> None:
        """Handle AC update message.

        Args:
            message: AC update message.
        """
        # Update AC tree in state
        if message.ac_id:
            nodes = self._state.ac_tree.get("nodes", {})
            if message.ac_id in nodes:
                nodes[message.ac_id]["status"] = message.status
                nodes[message.ac_id]["is_atomic"] = message.is_atomic

        # Forward to current screen (if mounted)
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_ac_updated"):
                screen.on_ac_updated(message)

    def on_log_message(self, message: LogMessage) -> None:
        """Handle log message.

        Args:
            message: Log message.
        """
        self._state.add_log(
            message.level,
            message.source,
            message.message,
            message.data,
        )

        # Forward to logs screen if it's a method
        logs_screen = self.get_screen("logs")
        if isinstance(logs_screen, LogsScreen):
            logs_screen.add_log(
                message.level,
                message.source,
                message.message,
                message.data,
            )

    def on_pause_requested(self, message: PauseRequested) -> None:
        """Handle pause request.

        Args:
            message: Pause request message.
        """
        self._state.is_paused = True
        self._state.status = "paused"

        if self._pause_callback is not None:
            # Call the pause callback (e.g., to pause orchestrator)
            asyncio.create_task(self._call_pause_callback(message.execution_id))

        self._state.add_log(
            "info",
            "tui.control",
            f"Pause requested for execution {message.execution_id}",
        )

    def on_resume_requested(self, message: ResumeRequested) -> None:
        """Handle resume request.

        Args:
            message: Resume request message.
        """
        self._state.is_paused = False
        self._state.status = "running"

        if self._resume_callback is not None:
            # Call the resume callback (e.g., to resume orchestrator)
            asyncio.create_task(self._call_resume_callback(message.execution_id))

        self._state.add_log(
            "info",
            "tui.control",
            f"Resume requested for execution {message.execution_id}",
        )

    async def _call_pause_callback(self, execution_id: str) -> None:
        """Call pause callback safely.

        Args:
            execution_id: Execution to pause.
        """
        if self._pause_callback is not None:
            try:
                result = self._pause_callback(execution_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                self._state.add_log(
                    "error",
                    "tui.control",
                    f"Pause callback failed: {e}",
                )

    async def _call_resume_callback(self, execution_id: str) -> None:
        """Call resume callback safely.

        Args:
            execution_id: Execution to resume.
        """
        if self._resume_callback is not None:
            try:
                result = self._resume_callback(execution_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                self._state.add_log(
                    "error",
                    "tui.control",
                    f"Resume callback failed: {e}",
                )

    def action_pause(self) -> None:
        """Pause execution action."""
        if self._state.execution_id and not self._state.is_paused:
            self.post_message(PauseRequested(self._state.execution_id))

    def action_resume(self) -> None:
        """Resume execution action."""
        if self._state.execution_id and self._state.is_paused:
            self.post_message(ResumeRequested(self._state.execution_id))

    def action_show_dashboard(self) -> None:
        """Show dashboard screen."""
        self.switch_screen("dashboard")

    def action_show_execution(self) -> None:
        """Show execution screen."""
        self.push_screen("execution")

    def action_show_logs(self) -> None:
        """Show logs screen."""
        self.push_screen("logs")

    def action_show_debug(self) -> None:
        """Show debug screen."""
        self.push_screen("debug")

    def set_pause_callback(self, callback: Any) -> None:
        """Set callback for pause requests.

        Args:
            callback: Function to call when pause is requested.
                     Signature: (execution_id: str) -> None or Coroutine
        """
        self._pause_callback = callback

    def set_resume_callback(self, callback: Any) -> None:
        """Set callback for resume requests.

        Args:
            callback: Function to call when resume is requested.
                     Signature: (execution_id: str) -> None or Coroutine
        """
        self._resume_callback = callback

    def set_execution(self, execution_id: str, session_id: str = "") -> None:
        """Set the execution to monitor.

        Args:
            execution_id: Execution ID to monitor.
            session_id: Optional session ID.
        """
        self._execution_id = execution_id
        self._state.execution_id = execution_id
        self._state.session_id = session_id
        self._state.status = "running"

        # Restart subscription with new execution ID
        if self._subscription_task is not None:
            self._subscription_task.cancel()
            self._subscription_task = None
            self._start_event_subscription()

    def update_ac_tree(self, tree_data: dict[str, Any]) -> None:
        """Update the AC tree data.

        Args:
            tree_data: Tree data from ACTree.to_dict().
        """
        self._state.ac_tree = tree_data

        # Update dashboard if visible (and mounted)
        if self._screen_stack:
            screen = self.screen
            if isinstance(screen, DashboardScreen):
                screen.update_state(self._state)

    async def on_unmount(self) -> None:
        """Handle application unmount."""
        # Cancel subscription task
        if self._subscription_task is not None:
            self._subscription_task.cancel()
            try:
                await self._subscription_task
            except asyncio.CancelledError:
                # Expected when task is cancelled during shutdown
                pass


__all__ = ["OuroborosTUI"]
