"""HUD Dashboard - Enhanced dashboard with orchestration visibility.

Extends Dashboard V3 with:
- Agents Panel: Agent pool status
- Token Tracker: Real-time cost tracking
- Progress Tracker: Visual progress indicators
- Event Log: Scrollable event history

Layout:
    ┌─────────────────────────────────────────────────────────────────┐
    │  ◇ Discover  →  ◆ Define  →  ◇ Design  →  ◆ Deliver            │
    ├─────────────────────────────────────────────────────────────────┤
    │                              │                                  │
    │  AC EXECUTION TREE           │  NODE DETAIL                     │
    │  └─○ Seed                    │  ID: ac_1                        │
    │    ├─◐ AC1 (running)         │  Status: Executing               │
    │    │ ├─● SubAC1 (complete)   │  Depth: 2                        │
    │    └─○ AC2                   │  Content:                        │
    │                              │                                  │
    ├─────────────────────────────────────────────────────────────────┤
    │  AGENT POOL  │  TOKEN TRACKER  │  PROGRESS  │  EVENT LOG        │
    │  3 active    │  50K tokens     │  45% done  │  [14:30] Phase... │
    └─────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Footer, Label, Static, Tree

from ouroboros.tui.components import AgentsPanel, EventLog, ProgressTracker, TokenTracker
from ouroboros.tui.components.event_log import EventType, EventSeverity
from ouroboros.tui.components.progress import Phase
from ouroboros.tui.events import (
    ACUpdated,
    AgentThinkingUpdated,
    CostUpdated,
    DriftUpdated,
    ExecutionUpdated,
    ParallelBatchCompleted,
    ParallelBatchStarted,
    PauseRequested,
    PhaseChanged,
    ResumeRequested,
    SubtaskUpdated,
    ToolCallCompleted,
    ToolCallStarted,
    WorkflowProgressUpdated,
)
from ouroboros.tui.screens.dashboard_v3 import (
    STATUS_ICONS,
    DoubleDiamondBar,
    LiveActivityBar,
    NodeDetailPanel,
    NodeSelected,
    SelectableACTree,
)

if TYPE_CHECKING:
    from ouroboros.tui.events import TUIState


class HUDDashboardScreen(Screen[None]):
    """Enhanced dashboard with HUD components.

    Features:
    - Double Diamond phase indicator at top
    - Selectable AC Tree with hierarchical Sub-AC structure
    - Node Detail panel for selected AC
    - Bottom HUD row with: Agents Panel, Token Tracker, Progress, Event Log

    Bindings:
        p: Pause execution
        r: Resume execution
        t: Focus tree
        h: Toggle HUD visibility
        l: Switch to logs view
        d: Switch to debug view
    """

    BINDINGS = [
        Binding("p", "pause", "Pause"),
        Binding("r", "resume", "Resume"),
        Binding("t", "focus_tree", "Tree"),
        Binding("h", "toggle_hud", "Toggle HUD"),
        Binding("l", "logs", "Logs"),
        Binding("d", "debug", "Debug"),
    ]

    DEFAULT_CSS = """
    HUDDashboardScreen {
        layout: vertical;
        background: $background;
    }

    HUDDashboardScreen > .main-area {
        width: 100%;
        height: 1fr;
        padding: 1;
    }

    HUDDashboardScreen > .main-area > .content-row {
        width: 100%;
        height: 100%;
    }

    HUDDashboardScreen > .main-area > .content-row > .tree-panel {
        width: 2fr;
        height: 100%;
        margin-right: 1;
    }

    HUDDashboardScreen > .main-area > .content-row > .detail-panel {
        width: 1fr;
        min-width: 30;
        height: 100%;
    }

    HUDDashboardScreen > .hud-row {
        height: auto;
        min-height: 10;
        max-height: 15;
        width: 100%;
        padding: 0 1;
    }

    HUDDashboardScreen > .hud-row.hidden {
        display: none;
    }

    HUDDashboardScreen > .hud-row > .hud-panel {
        width: 1fr;
        height: 100%;
        margin-right: 1;
    }

    HUDDashboardScreen > .hud-row > .hud-panel:last-child {
        margin-right: 0;
    }
    """

    hud_visible: reactive[bool] = reactive(True)

    def __init__(
        self,
        state: TUIState | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize HUD dashboard.

        Args:
            state: Initial TUI state.
            name: Screen name.
            id: Screen ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._state = state
        self._phase_bar: DoubleDiamondBar | None = None
        self._tree: SelectableACTree | None = None
        self._detail_panel: NodeDetailPanel | None = None
        self._activity_bar: LiveActivityBar | None = None

        # HUD components
        self._agents_panel: AgentsPanel | None = None
        self._token_tracker: TokenTracker | None = None
        self._progress_tracker: ProgressTracker | None = None
        self._event_log: EventLog | None = None

        # Track sub-tasks per AC for tree display
        self._subtasks: dict[int, list[dict[str, Any]]] = {}

    def compose(self) -> ComposeResult:
        """Compose the screen layout."""
        self._phase_bar = DoubleDiamondBar()
        yield self._phase_bar

        with Container(classes="main-area"):
            with Horizontal(classes="content-row"):
                with Container(classes="tree-panel"):
                    self._tree = SelectableACTree(
                        tree_data=self._state.ac_tree if self._state else {},
                    )
                    yield self._tree

                with Container(classes="detail-panel"):
                    self._detail_panel = NodeDetailPanel()
                    yield self._detail_panel

        # HUD row - visibility controlled by reactive
        with Horizontal(classes="hud-row", id="hud-row"):
            self._agents_panel = AgentsPanel(self._state)
            yield self._agents_panel

            self._token_tracker = TokenTracker(self._state, budget_usd=10.0)
            yield self._token_tracker

            self._progress_tracker = ProgressTracker(self._state)
            yield self._progress_tracker

            self._event_log = EventLog(self._state, max_entries=100)
            yield self._event_log

        self._activity_bar = LiveActivityBar()
        yield self._activity_bar
        yield Footer()

    def on_mount(self) -> None:
        """Initialize display."""
        self._update_hud_visibility()

    def _update_hud_visibility(self) -> None:
        """Update HUD row visibility based on state."""
        try:
            hud_row = self.query_one("#hud-row", Horizontal)
            if self.hud_visible:
                hud_row.remove_class("hidden")
            else:
                hud_row.add_class("hidden")
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # Message Handlers
    # ─────────────────────────────────────────────────────────────────────────

    def on_node_selected(self, message: NodeSelected) -> None:
        """Handle node selection from tree."""
        if self._detail_panel:
            self._detail_panel.selected_node = message.node_data

    def on_execution_updated(self, message: ExecutionUpdated) -> None:
        """Handle execution updates - forward to HUD components."""
        if self._state:
            if self._agents_panel:
                self._agents_panel.update_from_state(self._state)
            if self._token_tracker:
                self._token_tracker.update_from_state(self._state)
            if self._progress_tracker:
                self._progress_tracker.update_from_state(self._state)
            if self._event_log:
                self._event_log.update_from_state(self._state)

    def on_phase_changed(self, message: PhaseChanged) -> None:
        """Handle phase changes."""
        if self._phase_bar:
            self._phase_bar.phase = message.current_phase

        # Update progress tracker
        if self._progress_tracker and self._event_log:
            phase_map = {
                "discover": Phase.DISCOVER,
                "define": Phase.DEFINE,
                "design": Phase.DESIGN,
                "deliver": Phase.DELIVER,
            }
            phase = phase_map.get(message.current_phase.lower(), Phase.DISCOVER)
            self._progress_tracker.set_phase_progress(phase, 0.0, f"Started {message.current_phase}")

            # Log phase change
            self._event_log.add_entry(
                f"Phase: {message.previous_phase or 'start'} → {message.current_phase} (iter {message.iteration})",
                source="phase",
                event_type=EventType.PHASE,
            )

    def on_drift_updated(self, message: DriftUpdated) -> None:
        """Handle drift updates - log to event log."""
        if self._event_log and message.combined_drift > 0.5:
            self._event_log.add_entry(
                f"Drift alert: {message.combined_drift:.2f} combined",
                source="drift",
                severity=EventSeverity.WARNING,
                event_type=EventType.ALL,
            )

    def on_cost_updated(self, message: CostUpdated) -> None:
        """Handle cost updates - forward to token tracker."""
        if self._token_tracker and self._state:
            self._token_tracker.update_from_state(self._state)

    def on_ac_updated(self, message: ACUpdated) -> None:
        """Handle AC updates."""
        if self._tree:
            self._tree.update_node_status(message.ac_id, message.status)

    def on_parallel_batch_started(self, message: ParallelBatchStarted) -> None:
        """Handle parallel batch start."""
        if self._progress_tracker:
            self._progress_tracker.set_current_activity(
                f"Parallel batch {message.batch_index + 1}/{message.total_batches} started"
            )

    def on_parallel_batch_completed(self, message: ParallelBatchCompleted) -> None:
        """Handle parallel batch completion."""
        if self._event_log:
            self._event_log.add_entry(
                f"Batch {message.batch_index + 1}: {message.successful_count} successful, {message.failed_count} failed",
                source="parallel",
                event_type=EventType.ALL,
            )

    def on_workflow_progress_updated(self, message: WorkflowProgressUpdated) -> None:
        """Handle workflow progress updates."""
        # Update phase bar
        if self._phase_bar and message.current_phase:
            self._phase_bar.phase = message.current_phase.lower()

        # Update progress tracker
        if self._progress_tracker:
            self._progress_tracker.set_overall_progress(
                percent=(message.completed_count / message.total_count * 100) if message.total_count > 0 else 0,
                completed=message.completed_count,
                total=message.total_count,
                elapsed=message.elapsed_display or "",
                eta=message.estimated_remaining or "",
            )

            if message.activity_detail:
                self._progress_tracker.set_current_activity(message.activity_detail)

        # Update token tracker
        if self._token_tracker and self._state:
            self._token_tracker.update_from_state(self._state)

        # Update agents panel
        if self._agents_panel and self._state:
            self._agents_panel.update_from_state(self._state)

        # Update phase bar progress counter
        if self._phase_bar:
            completed = message.completed_count
            total = message.total_count
            if total > 0:
                elapsed = message.elapsed_display or ""
                cost = f"${message.estimated_cost_usd:.2f}" if message.estimated_cost_usd else ""
                parts = [f"[cyan][{completed}/{total} AC][/]"]
                if elapsed:
                    parts.append(f"[dim]{elapsed}[/]")
                if cost:
                    parts.append(f"[dim]{cost}[/]")
                self._phase_bar.progress_text = "  ".join(parts)

    def on_subtask_updated(self, message: SubtaskUpdated) -> None:
        """Handle sub-task updates."""
        ac_index = message.ac_index

        if ac_index not in self._subtasks:
            self._subtasks[ac_index] = []

        existing = next(
            (st for st in self._subtasks[ac_index] if st["id"] == message.sub_task_id),
            None,
        )

        if existing:
            existing["status"] = message.status
        else:
            self._subtasks[ac_index].append({
                "id": message.sub_task_id,
                "index": message.sub_task_index,
                "content": message.content,
                "status": message.status,
            })

    def on_tool_call_started(self, message: ToolCallStarted) -> None:
        """Handle tool call started - show inline activity."""
        if self._tree:
            self._tree.update_node_activity(message.ac_id, message.tool_detail)

        if self._activity_bar:
            tools = dict(self._activity_bar.active_tools)
            tools[message.ac_id] = {
                "tool_name": message.tool_name,
                "tool_detail": message.tool_detail,
            }
            self._activity_bar.active_tools = tools

        # Log to event log
        if self._event_log:
            self._event_log.add_entry(
                f"Tool started: {message.tool_detail}",
                source="tool",
                event_type=EventType.TOOL,
            )

    def on_tool_call_completed(self, message: ToolCallCompleted) -> None:
        """Handle tool call completed - clear inline activity."""
        if self._tree:
            self._tree.clear_node_activity(message.ac_id)

        if self._activity_bar:
            tools = dict(self._activity_bar.active_tools)
            tools.pop(message.ac_id, None)
            self._activity_bar.active_tools = tools

        # Add token usage to tracker
        if self._token_tracker:
            # Estimate tokens from duration (simplified)
            estimated_tokens = int(message.duration_seconds * 100)
            self._token_tracker.add_tokens(
                entity_id=message.ac_id,
                entity_name=message.ac_id,
                input_tokens=estimated_tokens // 2,
                output_tokens=estimated_tokens // 2,
                model_tier="sonnet",
            )

    def on_agent_thinking_updated(self, message: AgentThinkingUpdated) -> None:
        """Handle agent thinking - update detail panel if selected."""
        if self._detail_panel and self._detail_panel.selected_node:
            node_id = self._detail_panel.selected_node.get("id")
            if node_id == message.ac_id:
                self._detail_panel.update_thinking(message.thinking_text)

        # Update progress tracker activity
        if self._progress_tracker:
            thinking = message.thinking_text[:50] + "..." if len(message.thinking_text) > 50 else message.thinking_text
            self._progress_tracker.set_current_activity(f"Thinking: {thinking}")

    # ─────────────────────────────────────────────────────────────────────────
    # Actions
    # ─────────────────────────────────────────────────────────────────────────

    def action_pause(self) -> None:
        if self._state and self._state.execution_id:
            self.post_message(PauseRequested(self._state.execution_id))

    def action_resume(self) -> None:
        if self._state and self._state.execution_id:
            self.post_message(ResumeRequested(self._state.execution_id))

    def action_focus_tree(self) -> None:
        if self._tree:
            try:
                tree_widget = self._tree.query_one("#ac-tree", Tree)
                tree_widget.focus()
            except Exception:
                pass

    def action_toggle_hud(self) -> None:
        """Toggle HUD row visibility."""
        self.hud_visible = not self.hud_visible

    def watch_hud_visible(self, _: bool) -> None:
        """React to HUD visibility changes."""
        self._update_hud_visibility()

    def action_logs(self) -> None:
        self.app.push_screen("logs")

    def action_debug(self) -> None:
        self.app.push_screen("debug")


__all__ = ["HUDDashboardScreen"]
