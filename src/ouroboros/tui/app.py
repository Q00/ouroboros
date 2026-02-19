"""Main TUI application using Textual framework.

OuroborosTUI is the main application class that:
- Manages screens (session selector, dashboard, execution, logs, debug)
- Handles global keybindings
- Subscribes to EventStore for live updates
- Provides pause/resume execution control
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import App
from textual.binding import Binding

from ouroboros.tui.events import (
    ACUpdated,
    AgentThinkingUpdated,
    CostUpdated,
    DriftUpdated,
    ExecutionUpdated,
    LogMessage,
    PauseRequested,
    PhaseChanged,
    ResumeRequested,
    SubtaskUpdated,
    ToolCallCompleted,
    ToolCallStarted,
    TUIState,
    WorkflowProgressUpdated,
    create_message_from_event,
)
from ouroboros.tui.screens import (
    DashboardScreen,
    DashboardScreenV3,
    DebugScreen,
    ExecutionScreen,
    LogsScreen,
)
from ouroboros.tui.screens.session_selector import SessionSelectorScreen

if TYPE_CHECKING:
    from ouroboros.events.base import BaseEvent
    from ouroboros.persistence.event_store import EventStore


class OuroborosTUI(App[None]):
    """Main Textual application for Ouroboros TUI."""

    TITLE = "Ouroboros TUI"
    SUB_TITLE = "Workflow Monitor"

    CSS = """
    Screen {
        background: $background;
    }

    Header {
        background: $primary;
        color: $text;
        text-style: bold;
        dock: top;
        height: 3;
    }

    Footer {
        background: $surface;
        color: $text-muted;
        dock: bottom;
        height: 1;
    }

    .hidden {
        display: none;
    }

    /* Global scrollbar styling */
    *:focus {
        border: round $accent;
    }

    /* Ensure smooth transitions */
    * {
        transition: background 150ms;
    }
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("p", "pause", "Pause"),
        Binding("r", "resume", "Resume"),
        Binding("d", "show_debug", "Debug"),
        Binding("l", "show_logs", "Logs"),
        Binding("s", "show_selector", "Select Session"),
        Binding("1", "show_dashboard", "Dashboard", show=False),
        Binding("2", "show_execution", "Execution", show=False),
        Binding("3", "show_logs", "Logs", show=False),
        Binding("4", "show_debug", "Debug", show=False),
    ]

    def __init__(
        self,
        event_store: EventStore | None = None,
        *,
        execution_id: str | None = None,
        driver_class: type | None = None,
    ) -> None:
        """Initialize OuroborosTUI.

        Args:
            event_store: EventStore for live updates (optional for offline mode).
            execution_id: Optional execution ID to monitor initially.
            driver_class: Optional Textual driver class for testing.
        """
        super().__init__(driver_class=driver_class)
        self._event_store = event_store
        self._execution_id: str | None = execution_id
        self._state = TUIState()
        self._subscription_task: asyncio.Task[None] | None = None
        self._is_paused = False
        self._pause_callback: Any | None = None
        self._resume_callback: Any | None = None

    @property
    def state(self) -> TUIState:
        """Get current TUI state."""
        return self._state

    def on_mount(self) -> None:
        """Handle application mount."""
        # Install screens - session selector only if event_store is available
        if self._event_store is not None:
            self.install_screen(SessionSelectorScreen(self._event_store), name="session_selector")
        self.install_screen(DashboardScreenV3(self._state), name="dashboard")
        self.install_screen(ExecutionScreen(self._state), name="execution")
        self.install_screen(LogsScreen(self._state), name="logs")
        self.install_screen(DebugScreen(self._state), name="debug")

        # Start with session selector if available, otherwise dashboard
        if self._event_store is not None:
            self.push_screen("session_selector")
        else:
            self.push_screen("dashboard")

    async def on_session_selector_screen_session_selected(
        self, message: SessionSelectorScreen.SessionSelected
    ) -> None:
        """Handle session selection and switch to the dashboard."""
        self.set_execution(message.execution_id, message.session_id)
        self.push_screen("dashboard")

    def _start_event_subscription(self) -> None:
        """Start background task for event subscription."""
        # Skip if no event loop (e.g., during testing) or no event store
        if self._event_store is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # No event loop running
        if self._subscription_task is not None:
            self._subscription_task.cancel()
        self._subscription_task = asyncio.create_task(self._subscribe_to_events())

    async def _subscribe_to_events(self) -> None:
        """Subscribe to EventStore for live updates."""
        if self._event_store is None or self._execution_id is None:
            return

        last_event_count = 0
        poll_interval = 0.5

        while True:
            try:
                await asyncio.sleep(poll_interval)
                events = await self._event_store.replay("execution", self._execution_id)
                for event in events[last_event_count:]:
                    # Log event reception
                    self._state.add_log(
                        "info",
                        "tui.events",
                        f"Received: {event.type}",
                        {"aggregate_id": event.aggregate_id},
                    )
                    # Also forward to logs screen
                    try:
                        logs_screen = self.get_screen("logs")
                        if logs_screen and hasattr(logs_screen, "add_log"):
                            logs_screen.add_log(
                                "info",
                                "tui.events",
                                f"Received: {event.type}",
                                {"aggregate_id": event.aggregate_id},
                            )
                    except Exception:
                        pass

                    message = create_message_from_event(event)
                    if message is not None:
                        self.post_message(message)
                        self._update_state_from_event(event)

                    # Forward raw event to debug screen
                    try:
                        debug_screen = self.get_screen("debug")
                        if debug_screen and hasattr(debug_screen, "add_raw_event"):
                            debug_screen.add_raw_event(
                                {
                                    "type": event.type,
                                    "aggregate_type": event.aggregate_type,
                                    "aggregate_id": event.aggregate_id,
                                    "data": event.data,
                                    "timestamp": str(event.timestamp),
                                }
                            )
                    except Exception:
                        pass  # Screen might not be installed yet

                last_event_count = len(events)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._state.add_log("error", "tui.subscription", f"Event subscription error: {e}")
                try:
                    logs_screen = self.get_screen("logs")
                    if logs_screen and hasattr(logs_screen, "add_log"):
                        logs_screen.add_log(
                            "error", "tui.subscription", f"Event subscription error: {e}"
                        )
                except Exception:
                    pass

    def _update_state_from_event(self, event: BaseEvent) -> None:
        """Update internal state from an event."""
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
        elif event_type == "ac.decomposition.completed":
            # Handle AC decomposition - add children to tree
            parent_ac_id = event.aggregate_id
            child_ac_ids = data.get("child_ac_ids", [])
            child_contents = data.get("child_contents", [])
            depth = data.get("depth", 0)

            # Update ac_tree with new children
            nodes = self._state.ac_tree.get("nodes", {})
            if parent_ac_id in nodes:
                # Update parent to show decomposed status
                nodes[parent_ac_id]["status"] = "decomposed"
                nodes[parent_ac_id]["children_ids"] = child_ac_ids

                # Add child nodes
                for _i, (child_id, child_content) in enumerate(
                    zip(child_ac_ids, child_contents, strict=False)
                ):
                    nodes[child_id] = {
                        "id": child_id,
                        "content": child_content,
                        "status": "pending",
                        "depth": depth + 1,
                        "parent_id": parent_ac_id,
                        "is_atomic": False,
                        "children_ids": [],
                    }

                self._state.ac_tree["nodes"] = nodes

                # Notify dashboard to update tree
                self._notify_ac_tree_updated()
        elif event_type == "ac.marked_atomic":
            # Handle AC marked as atomic
            ac_id = event.aggregate_id
            nodes = self._state.ac_tree.get("nodes", {})
            if ac_id in nodes:
                nodes[ac_id]["status"] = "atomic"
                nodes[ac_id]["is_atomic"] = True
                self._state.ac_tree["nodes"] = nodes
                self._notify_ac_tree_updated()

    def set_execution(self, execution_id: str, session_id: str = "") -> None:
        """Set the execution to monitor."""
        self._execution_id = execution_id
        self._state.execution_id = execution_id
        self._state.session_id = session_id
        self._state.status = "running"
        self._state.add_log("info", "tui.main", f"Monitoring execution: {execution_id}")
        # Forward to logs screen
        try:
            logs_screen = self.get_screen("logs")
            if logs_screen and hasattr(logs_screen, "add_log"):
                logs_screen.add_log("info", "tui.main", f"Monitoring execution: {execution_id}")
        except Exception:
            pass
        self._start_event_subscription()

    def action_show_selector(self) -> None:
        """Show session selector screen."""
        self.push_screen("session_selector")

    def on_execution_updated(self, message: ExecutionUpdated) -> None:
        self._state.execution_id = message.execution_id
        self._state.session_id = message.session_id
        self._state.status = message.status
        self._state.is_paused = message.status == "paused"
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_execution_updated"):
                screen.on_execution_updated(message)

    def on_phase_changed(self, message: PhaseChanged) -> None:
        self._state.current_phase = message.current_phase
        self._state.iteration = message.iteration
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_phase_changed"):
                screen.on_phase_changed(message)

    def on_drift_updated(self, message: DriftUpdated) -> None:
        self._state.goal_drift = message.goal_drift
        self._state.constraint_drift = message.constraint_drift
        self._state.ontology_drift = message.ontology_drift
        self._state.combined_drift = message.combined_drift
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_drift_updated"):
                screen.on_drift_updated(message)

    def on_cost_updated(self, message: CostUpdated) -> None:
        self._state.total_tokens = message.total_tokens
        self._state.total_cost_usd = message.total_cost_usd
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_cost_updated"):
                screen.on_cost_updated(message)

    def on_ac_updated(self, message: ACUpdated) -> None:
        if message.ac_id:
            nodes = self._state.ac_tree.get("nodes", {})
            if message.ac_id in nodes:
                nodes[message.ac_id]["status"] = message.status
                nodes[message.ac_id]["is_atomic"] = message.is_atomic
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_ac_updated"):
                screen.on_ac_updated(message)

    def on_subtask_updated(self, message: SubtaskUpdated) -> None:
        """Handle sub-task updates and add to AC tree (SSOT)."""
        nodes = self._state.ac_tree.get("nodes", {})
        parent_ac_id = f"ac_{message.ac_index}"
        sub_task_id = message.sub_task_id

        # Add or update subtask node
        nodes[sub_task_id] = {
            "id": sub_task_id,
            "content": message.content,
            "status": message.status,
            "depth": 2,
            "is_atomic": True,
            "children_ids": [],
        }

        # Update parent's children_ids (add if not present)
        if parent_ac_id in nodes:
            parent = nodes[parent_ac_id]
            children = parent.get("children_ids", [])
            if sub_task_id not in children:
                children.append(sub_task_id)
                parent["children_ids"] = children
            parent["is_atomic"] = False

        self._state.ac_tree["nodes"] = nodes
        self._notify_ac_tree_updated()

        # Forward to current screen
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_subtask_updated"):
                screen.on_subtask_updated(message)

    def on_tool_call_started(self, message: ToolCallStarted) -> None:
        """Handle tool call started - track active tools."""
        self._state.active_tools[message.ac_id] = {
            "tool_name": message.tool_name,
            "tool_detail": message.tool_detail,
            "call_index": str(message.call_index),
        }
        self._notify_ac_tree_updated()
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_tool_call_started"):
                screen.on_tool_call_started(message)

    def on_tool_call_completed(self, message: ToolCallCompleted) -> None:
        """Handle tool call completed - move to history."""
        self._state.active_tools.pop(message.ac_id, None)
        history = self._state.tool_history.setdefault(message.ac_id, [])
        history.append(
            {
                "tool_name": message.tool_name,
                "tool_detail": message.tool_detail,
                "call_index": message.call_index,
                "duration_seconds": message.duration_seconds,
                "success": message.success,
            }
        )
        # Keep last 20 entries per AC
        if len(history) > 20:
            self._state.tool_history[message.ac_id] = history[-20:]
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_tool_call_completed"):
                screen.on_tool_call_completed(message)

    def on_agent_thinking_updated(self, message: AgentThinkingUpdated) -> None:
        """Handle agent thinking update."""
        self._state.thinking[message.ac_id] = message.thinking_text
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_agent_thinking_updated"):
                screen.on_agent_thinking_updated(message)

    def on_workflow_progress_updated(self, message: WorkflowProgressUpdated) -> None:
        # Update state with AC tree from workflow progress (smart merge)
        if message.acceptance_criteria:
            self._merge_ac_progress(
                message.acceptance_criteria,
                message.current_ac_index,
            )

        # Update cost/tokens in state
        self._state.total_tokens = message.estimated_tokens
        self._state.total_cost_usd = message.estimated_cost_usd

        # Update phase in state
        if message.current_phase:
            self._state.current_phase = message.current_phase.lower()

        # Forward to current screen
        if self._screen_stack:
            screen = self.screen
            if hasattr(screen, "on_workflow_progress_updated"):
                screen.on_workflow_progress_updated(message)

        # Also forward to execution screen for event timeline
        try:
            execution_screen = self.get_screen("execution")
            if execution_screen and hasattr(execution_screen, "on_workflow_progress_updated"):
                execution_screen.on_workflow_progress_updated(message)
        except Exception:
            pass  # Screen might not be installed yet

        # Update debug screen with new state
        try:
            debug_screen = self.get_screen("debug")
            if debug_screen and hasattr(debug_screen, "update_state"):
                debug_screen.update_state(self._state)
        except Exception:
            pass  # Screen might not be installed yet

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
        nodes: dict[str, Any] = {}
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
            "status": "executing" if current_ac_index is not None else "pending",
            "depth": 0,
            "is_atomic": False,
            "children_ids": child_ids,
        }

        return {
            "root_id": root_id,
            "nodes": nodes,
        }

    def _merge_ac_progress(
        self,
        acceptance_criteria: list[dict[str, Any]],
        current_ac_index: int | None,
    ) -> None:
        """Merge AC progress into existing tree, preserving subtree children.

        Unlike _convert_ac_list_to_tree which rebuilds from scratch,
        this method updates status of existing nodes while preserving
        children_ids and subtask nodes added by decomposition events.

        Args:
            acceptance_criteria: List of AC dicts with index, content, status.
            current_ac_index: Index of current AC being worked on.
        """
        existing_nodes = self._state.ac_tree.get("nodes", {})

        if not existing_nodes:
            # No existing tree - build from scratch
            self._state.ac_tree = self._convert_ac_list_to_tree(
                acceptance_criteria,
                current_ac_index,
            )
            self._notify_ac_tree_updated()
            return

        # Smart merge: update status/content but preserve children
        for ac in acceptance_criteria:
            ac_index = ac.get("index", 0)
            ac_id = f"ac_{ac_index}"

            status = ac.get("status", "pending")
            if status == "in_progress":
                status = "executing"
            elif status not in ("completed", "failed", "executing"):
                status = "pending"

            if ac_id in existing_nodes:
                # Update existing node - preserve children_ids and is_atomic
                existing_nodes[ac_id]["status"] = status
                existing_nodes[ac_id]["content"] = ac.get("content", "")
            else:
                # New AC node - add it
                existing_nodes[ac_id] = {
                    "id": ac_id,
                    "content": ac.get("content", ""),
                    "status": status,
                    "depth": 1,
                    "is_atomic": True,
                    "children_ids": [],
                }

        # Ensure root exists and keep its children_ids in sync
        root_id = self._state.ac_tree.get("root_id", "root")
        expected_child_ids = [f"ac_{ac.get('index', 0)}" for ac in acceptance_criteria]

        if root_id not in existing_nodes:
            existing_nodes[root_id] = {
                "id": root_id,
                "content": "Acceptance Criteria",
                "status": "executing" if current_ac_index is not None else "pending",
                "depth": 0,
                "is_atomic": False,
                "children_ids": expected_child_ids,
            }
        else:
            existing_nodes[root_id]["status"] = (
                "executing" if current_ac_index is not None else "pending"
            )
            # Sync children_ids: add any new ACs not already present
            current_children = existing_nodes[root_id].get("children_ids", [])
            for child_id in expected_child_ids:
                if child_id not in current_children:
                    current_children.append(child_id)
            existing_nodes[root_id]["children_ids"] = current_children

        self._state.ac_tree["nodes"] = existing_nodes
        self._notify_ac_tree_updated()

    def on_log_message(self, message: LogMessage) -> None:
        self._state.add_log(message.level, message.source, message.message, message.data)
        try:
            logs_screen = self.get_screen("logs")
            if logs_screen and hasattr(logs_screen, "add_log"):
                logs_screen.add_log(message.level, message.source, message.message, message.data)
        except Exception:
            pass  # Screen might not be ready

    def on_pause_requested(self, message: PauseRequested) -> None:
        self._state.is_paused = True
        self._state.status = "paused"
        if self._pause_callback is not None:
            asyncio.create_task(self._call_pause_callback(message.execution_id))
        self._state.add_log(
            "info", "tui.control", f"Pause requested for execution {message.execution_id}"
        )

    def on_resume_requested(self, message: ResumeRequested) -> None:
        self._state.is_paused = False
        self._state.status = "running"
        if self._resume_callback is not None:
            asyncio.create_task(self._call_resume_callback(message.execution_id))
        self._state.add_log(
            "info", "tui.control", f"Resume requested for execution {message.execution_id}"
        )

    async def _call_pause_callback(self, execution_id: str) -> None:
        if self._pause_callback is not None:
            try:
                result = self._pause_callback(execution_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                self._state.add_log("error", "tui.control", f"Pause callback failed: {e}")

    async def _call_resume_callback(self, execution_id: str) -> None:
        if self._resume_callback is not None:
            try:
                result = self._resume_callback(execution_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                self._state.add_log("error", "tui.control", f"Resume callback failed: {e}")

    def action_pause(self) -> None:
        if self._state.execution_id and not self._state.is_paused:
            self.post_message(PauseRequested(self._state.execution_id))

    def action_resume(self) -> None:
        if self._state.execution_id and self._state.is_paused:
            self.post_message(ResumeRequested(self._state.execution_id))

    def action_show_dashboard(self) -> None:
        self.switch_screen("dashboard")

    def action_show_execution(self) -> None:
        self.push_screen("execution")

    def action_show_logs(self) -> None:
        self.push_screen("logs")

    def action_show_debug(self) -> None:
        self.push_screen("debug")

    def set_pause_callback(self, callback: Any) -> None:
        self._pause_callback = callback

    def set_resume_callback(self, callback: Any) -> None:
        self._resume_callback = callback

    def update_ac_tree(self, tree_data: dict[str, Any]) -> None:
        self._state.ac_tree = tree_data
        self._notify_ac_tree_updated()

    def _notify_ac_tree_updated(self) -> None:
        """Notify dashboard that AC tree has been updated."""
        if self._screen_stack:
            screen = self.screen
            if isinstance(screen, DashboardScreenV3):
                if hasattr(screen, "_tree") and screen._tree is not None:
                    screen._tree.update_tree(self._state.ac_tree)
            elif isinstance(screen, DashboardScreen):
                if hasattr(screen, "_ac_tree") and screen._ac_tree is not None:
                    screen._ac_tree.update_tree(self._state.ac_tree)

    async def on_unmount(self) -> None:
        if self._subscription_task is not None:
            self._subscription_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._subscription_task


__all__ = ["OuroborosTUI"]
