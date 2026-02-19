"""Dashboard V3 - Simplified Split View with Double Diamond Status.

A clean dashboard featuring:
- Double Diamond phase indicator at top
- Selectable AC Tree with hierarchical Sub-AC structure
- Node Detail panel for selected AC

Layout:
    ┌─────────────────────────────────────────────────────────────────┐
    │  ◇ Discover  →  ◆ Define  →  ◇ Design  →  ◆ Deliver            │
    ├─────────────────────────────────────────────────────────────────┤
    │                              │                                  │
    │  AC EXECUTION TREE           │  NODE DETAIL                     │
    │  └─○ Seed                    │  ID: ac_1                        │
    │    ├─◐ AC1 (running)         │  Status: Executing               │
    │    │ ├─● SubAC1 (complete)   │  Depth: 2                        │
    │    │ └─○ SubAC2 (blocked)    │                                  │
    │    ├─○ AC2                   │  Content:                        │
    │    │ ├─● SubAC1 (complete)   │  Process the input data          │
    │    │ └─◐ SubAC2 (running)    │  and validate...                 │
    │    ├─● AC3 (complete)        │                                  │
    │    └─○ AC4                   │                                  │
    │      └─○ SubAC1              │                                  │
    │                              │                                  │
    └─────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Footer, Label, Static, Tree
from textual.widgets.tree import TreeNode

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

if TYPE_CHECKING:
    from ouroboros.tui.events import TUIState


# ═══════════════════════════════════════════════════════════════════════════════
# STATUS ICONS & VISUAL CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

STATUS_ICONS = {
    "pending": "[dim]○[/]",
    "blocked": "[red]⊘[/]",
    "atomic": "[blue]◆[/]",
    "decomposed": "[cyan]◇[/]",
    "executing": "[bold yellow]◐[/]",
    "running": "[bold yellow]◐[/]",
    "completed": "[bold green]●[/]",
    "complete": "[bold green]●[/]",
    "failed": "[bold red]✖[/]",
}


# ═══════════════════════════════════════════════════════════════════════════════
# DOUBLE DIAMOND STATUS BAR
# ═══════════════════════════════════════════════════════════════════════════════


class DoubleDiamondBar(Static):
    """Simple status bar showing current Double Diamond phase."""

    DEFAULT_CSS = """
    DoubleDiamondBar {
        width: 100%;
        height: 3;
        background: $surface;
        border-bottom: solid $primary;
        padding: 1 2;
    }

    DoubleDiamondBar > .phase-container {
        width: 100%;
        height: 1;
        content-align: center middle;
    }
    """

    phase: reactive[str] = reactive("discover")
    progress_text: reactive[str] = reactive("")

    def compose(self) -> ComposeResult:
        with Horizontal(classes="phase-container"):
            yield Static(id="phase-display")

    def on_mount(self) -> None:
        self._update_display()

    def _update_display(self) -> None:
        phases = [
            ("discover", "Discover"),
            ("define", "Define"),
            ("design", "Design"),
            ("deliver", "Deliver"),
        ]

        parts = []
        for i, (key, label) in enumerate(phases):
            if key == self.phase.lower():
                parts.append(f"[bold green]◆ {label}[/]")
            else:
                parts.append(f"[dim]◇ {label}[/]")

            if i < len(phases) - 1:
                parts.append("[dim]  →  [/]")

        # Append progress counter if available
        if self.progress_text:
            parts.append(f"    {self.progress_text}")

        with contextlib.suppress(Exception):
            self.query_one("#phase-display", Static).update("".join(parts))

    def watch_phase(self, _: str) -> None:
        self._update_display()

    def watch_progress_text(self, _: str) -> None:
        self._update_display()


# ═══════════════════════════════════════════════════════════════════════════════
# NODE DETAIL PANEL
# ═══════════════════════════════════════════════════════════════════════════════


class NodeDetailPanel(Static):
    """Panel showing detailed information about the selected AC node."""

    DEFAULT_CSS = """
    NodeDetailPanel {
        width: 100%;
        height: 100%;
        border: heavy $primary;
        background: $surface;
        padding: 1;
    }

    NodeDetailPanel > .panel-title {
        text-align: center;
        text-style: bold;
        color: $primary;
        width: 100%;
        margin-bottom: 1;
    }

    NodeDetailPanel > .detail-row {
        width: 100%;
        height: 1;
    }

    NodeDetailPanel > .detail-row > .label {
        width: 12;
        color: $text-muted;
    }

    NodeDetailPanel > .detail-row > .value {
        width: 1fr;
        color: $text;
    }

    NodeDetailPanel > .content-section {
        width: 100%;
        max-height: 8;
        margin-top: 1;
        padding-top: 1;
        border-top: dashed $primary-darken-2;
    }

    NodeDetailPanel > .content-section > .content-label {
        color: $text-muted;
        margin-bottom: 1;
    }

    NodeDetailPanel > .content-section > .content-text {
        color: $text;
        width: 100%;
        height: 1fr;
    }

    NodeDetailPanel > .empty-state {
        width: 100%;
        height: 100%;
        text-align: center;
        color: $text-muted;
        padding: 4;
    }

    NodeDetailPanel > .thinking-section {
        width: 100%;
        max-height: 6;
        margin-top: 1;
        padding-top: 1;
        border-top: dashed $primary-darken-2;
    }

    NodeDetailPanel > .thinking-section > .thinking-label {
        color: $text-muted;
        margin-bottom: 0;
    }

    NodeDetailPanel > .thinking-section > .thinking-text {
        color: $warning;
        width: 100%;
    }

    NodeDetailPanel > .tool-history-section {
        width: 100%;
        max-height: 10;
        margin-top: 1;
        padding-top: 1;
        border-top: dashed $primary-darken-2;
    }

    NodeDetailPanel > .tool-history-section > .history-label {
        color: $text-muted;
        margin-bottom: 0;
    }

    NodeDetailPanel > .tool-history-section > .history-item {
        width: 100%;
        height: 1;
        color: $text;
    }
    """

    selected_node: reactive[dict[str, Any] | None] = reactive(None)
    thinking_text: reactive[str] = reactive("")
    tool_history: reactive[list[dict[str, Any]]] = reactive([], always_update=True)

    def compose(self) -> ComposeResult:
        yield Label("╔══ NODE DETAIL ══╗", classes="panel-title")

        if self.selected_node is None:
            yield Static(
                "[dim]Select a node from the tree[/]\n[dim]to view details[/]",
                classes="empty-state",
            )
        else:
            node = self.selected_node
            status = node.get("status", "pending")
            status_icon = STATUS_ICONS.get(status, "○")
            status_display = f"{status_icon} {status.upper()}"

            with Horizontal(classes="detail-row"):
                yield Label("ID:", classes="label")
                yield Static(f"[cyan]{node.get('id', 'N/A')}[/]", classes="value")

            with Horizontal(classes="detail-row"):
                yield Label("Status:", classes="label")
                yield Static(status_display, classes="value")

            with Horizontal(classes="detail-row"):
                yield Label("Depth:", classes="label")
                yield Static(str(node.get("depth", 0)), classes="value")

            with Horizontal(classes="detail-row"):
                yield Label("Atomic:", classes="label")
                is_atomic = node.get("is_atomic", False)
                atomic_display = "[green]Yes[/]" if is_atomic else "[dim]No[/]"
                yield Static(atomic_display, classes="value")

            with Horizontal(classes="detail-row"):
                yield Label("Children:", classes="label")
                children = node.get("children_ids", [])
                children_display = f"[cyan]{len(children)}[/]" if children else "[dim]None[/]"
                yield Static(children_display, classes="value")

            with Container(classes="content-section"):
                yield Label("Content:", classes="content-label")
                content = node.get("content", "No content")
                yield Static(content, classes="content-text")

            # Thinking section
            if self.thinking_text:
                with Container(classes="thinking-section"):
                    yield Label("Thinking:", classes="thinking-label")
                    truncated = self.thinking_text[:200]
                    if len(self.thinking_text) > 200:
                        truncated += "..."
                    yield Static(f"[italic]{truncated}[/]", classes="thinking-text")

            # Tool history section
            if self.tool_history:
                with Container(classes="tool-history-section"):
                    yield Label("Recent Tool Calls:", classes="history-label")
                    for entry in self.tool_history[-8:]:
                        name = entry.get("tool_detail", entry.get("tool_name", "?"))
                        duration = entry.get("duration_seconds", 0)
                        success = entry.get("success", True)
                        status_mark = "[green]OK[/]" if success else "[red]FAIL[/]"
                        yield Static(
                            f"  {status_mark} {name} [dim]{duration:.1f}s[/]",
                            classes="history-item",
                        )

    def watch_selected_node(self, _new_value: dict[str, Any] | None) -> None:
        self.refresh(recompose=True)

    def update_thinking(self, text: str) -> None:
        """Update thinking text display."""
        self.thinking_text = text

    def update_tool_history(self, history: list[dict[str, Any]]) -> None:
        """Update tool history display."""
        self.tool_history = history

    def watch_thinking_text(self, _: str) -> None:
        self.refresh(recompose=True)

    def watch_tool_history(self, _: list[dict[str, Any]]) -> None:
        self.refresh(recompose=True)


# ═══════════════════════════════════════════════════════════════════════════════
# SELECTABLE AC TREE
# ═══════════════════════════════════════════════════════════════════════════════


class NodeSelected(Message):
    """Message emitted when a tree node is selected."""

    def __init__(self, node_data: dict[str, Any]) -> None:
        super().__init__()
        self.node_data = node_data


class SelectableACTree(Static):
    """AC Execution Tree with node selection support.

    Displays hierarchical AC structure:
    - Seed (root)
      - AC1
        - SubAC1
        - SubAC2
      - AC2
        - SubAC1
      - AC3
    """

    DEFAULT_CSS = """
    SelectableACTree {
        width: 100%;
        height: 100%;
        border: heavy $secondary;
        background: $surface;
        padding: 0;
    }

    SelectableACTree > .tree-title {
        text-align: center;
        text-style: bold;
        color: $secondary;
        width: 100%;
        padding: 1;
        border-bottom: solid $secondary;
    }

    SelectableACTree > #ac-tree {
        width: 100%;
        height: 1fr;
        padding: 1;
    }
    """

    tree_data: reactive[dict[str, Any]] = reactive({}, always_update=True)

    def __init__(
        self,
        tree_data: dict[str, Any] | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        if tree_data:
            self.tree_data = tree_data
        self._node_map: dict[str, dict[str, Any]] = {}
        self._active_tools: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Label("╔══ AC EXECUTION TREE ══╗", classes="tree-title")
        tree: Tree[dict[str, Any]] = Tree("Seed", id="ac-tree")
        tree.root.expand()
        yield tree

    def on_mount(self) -> None:
        self._rebuild_tree()

    def _rebuild_tree(self) -> None:
        try:
            tree = self.query_one("#ac-tree", Tree)
        except Exception:
            return

        tree.clear()
        tree.root.label = "[bold]Seed[/]"
        tree.root.data = {"id": "seed", "content": "Seed", "status": "executing", "depth": 0}
        self._node_map = {"seed": tree.root.data}

        if not self.tree_data:
            return

        root_id = self.tree_data.get("root_id", "root")
        nodes = self.tree_data.get("nodes", {})

        if root_id in nodes:
            root_node = nodes[root_id]
            self._add_children(tree.root, root_node, nodes)

        tree.root.expand()

    def _add_children(
        self,
        parent: TreeNode[dict[str, Any]],
        parent_data: dict[str, Any],
        nodes: dict[str, Any],
    ) -> None:
        children_ids = parent_data.get("children_ids", [])

        for child_id in children_ids:
            if child_id not in nodes:
                continue

            child_data = nodes[child_id]
            status = child_data.get("status", "pending")
            content = child_data.get("content", "")[:40]
            icon = STATUS_ICONS.get(status, "○")

            label = f"{icon} {content}"
            if len(child_data.get("content", "")) > 40:
                label += "..."

            # P2: Show inline tool activity for executing nodes
            activity = self._active_tools.get(child_id)
            if activity and status in ("executing", "running"):
                label += f"\n     [dim italic]{activity}[/]"

            child_node = parent.add(label, data=child_data)
            self._node_map[child_id] = child_data

            # Recursively add grandchildren
            if child_data.get("children_ids"):
                self._add_children(child_node, child_data, nodes)
                child_node.expand()

    def update_tree(self, tree_data: dict[str, Any]) -> None:
        self.tree_data = tree_data
        self._rebuild_tree()

    def update_node_status(self, node_id: str, status: str) -> None:
        if node_id in self._node_map:
            self._node_map[node_id]["status"] = status
            self._rebuild_tree()

    def update_node_activity(self, ac_id: str, tool_detail: str) -> None:
        """Show inline tool activity for an executing node."""
        self._active_tools[ac_id] = tool_detail
        self._rebuild_tree()

    def clear_node_activity(self, ac_id: str) -> None:
        """Clear inline tool activity for a node."""
        self._active_tools.pop(ac_id, None)
        self._rebuild_tree()

    def on_tree_node_selected(self, event: Tree.NodeSelected[dict[str, Any]]) -> None:
        if event.node.data:
            self.post_message(NodeSelected(event.node.data))

    def watch_tree_data(self, _: dict[str, Any]) -> None:
        self._rebuild_tree()


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE ACTIVITY BAR
# ═══════════════════════════════════════════════════════════════════════════════


class LiveActivityBar(Static):
    """Compact bar showing all currently active parallel agent tool calls."""

    DEFAULT_CSS = """
    LiveActivityBar {
        width: 100%;
        height: auto;
        min-height: 1;
        max-height: 3;
        background: $surface;
        border-top: solid $accent;
        padding: 0 2;
    }
    """

    active_tools: reactive[dict[str, dict[str, str]]] = reactive({}, always_update=True)

    def render(self) -> str:
        if not self.active_tools:
            return "[dim]No active tool calls[/]"
        parts = []
        for ac_id, info in self.active_tools.items():
            detail = info.get("tool_detail", info.get("tool_name", "?"))
            short_id = ac_id.replace("sub_ac_", "S").replace("ac_", "AC")
            parts.append(f"[yellow]{short_id}[/] {detail}")
        return "  │  ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD SCREEN V3 - Main Screen
# ═══════════════════════════════════════════════════════════════════════════════


class DashboardScreenV3(Screen[None]):
    """Simplified dashboard with Double Diamond status and AC Tree.

    Features:
    - Double Diamond phase indicator at top
    - Selectable AC Tree with hierarchical Sub-AC structure
    - Node Detail panel for selected AC

    Bindings:
        p: Pause execution
        r: Resume execution
        t: Focus tree
        l: Switch to logs view
        d: Switch to debug view
    """

    BINDINGS = [
        Binding("p", "pause", "Pause"),
        Binding("r", "resume", "Resume"),
        Binding("t", "focus_tree", "Tree"),
        Binding("l", "logs", "Logs"),
        Binding("d", "debug", "Debug"),
    ]

    DEFAULT_CSS = """
    DashboardScreenV3 {
        layout: vertical;
        background: $background;
    }

    DashboardScreenV3 > .main-area {
        width: 100%;
        height: 1fr;
        padding: 1;
    }

    DashboardScreenV3 > .main-area > .content-row {
        width: 100%;
        height: 100%;
    }

    DashboardScreenV3 > .main-area > .content-row > .tree-panel {
        width: 2fr;
        height: 100%;
        margin-right: 1;
    }

    DashboardScreenV3 > .main-area > .content-row > .detail-panel {
        width: 1fr;
        min-width: 30;
        height: 100%;
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
        super().__init__(name=name, id=id, classes=classes)
        self._state = state
        self._phase_bar: DoubleDiamondBar | None = None
        self._tree: SelectableACTree | None = None
        self._detail_panel: NodeDetailPanel | None = None
        # Track sub-tasks per AC for tree display
        self._subtasks: dict[int, list[dict[str, Any]]] = {}
        self._activity_bar: LiveActivityBar | None = None

    def compose(self) -> ComposeResult:
        self._phase_bar = DoubleDiamondBar()
        yield self._phase_bar

        with Container(classes="main-area"), Horizontal(classes="content-row"):
            with Container(classes="tree-panel"):
                self._tree = SelectableACTree(
                    tree_data=self._state.ac_tree if self._state else {},
                )
                yield self._tree

            with Container(classes="detail-panel"):
                self._detail_panel = NodeDetailPanel()
                yield self._detail_panel

        self._activity_bar = LiveActivityBar()
        yield self._activity_bar
        yield Footer()

    # ─────────────────────────────────────────────────────────────────────────
    # Message Handlers
    # ─────────────────────────────────────────────────────────────────────────

    def on_node_selected(self, message: NodeSelected) -> None:
        """Handle node selection from tree."""
        if self._detail_panel:
            self._detail_panel.selected_node = message.node_data

    def on_execution_updated(self, message: ExecutionUpdated) -> None:
        pass  # Status display removed

    def on_phase_changed(self, message: PhaseChanged) -> None:
        if self._phase_bar:
            self._phase_bar.phase = message.current_phase

    def on_drift_updated(self, message: DriftUpdated) -> None:
        pass  # Drift display removed

    def on_cost_updated(self, message: CostUpdated) -> None:
        pass  # Cost display removed

    def on_ac_updated(self, message: ACUpdated) -> None:
        if self._tree:
            self._tree.update_node_status(message.ac_id, message.status)

    def on_parallel_batch_started(self, message: ParallelBatchStarted) -> None:
        pass  # Parallel graph removed

    def on_parallel_batch_completed(self, message: ParallelBatchCompleted) -> None:
        pass  # Parallel graph removed

    def on_workflow_progress_updated(self, message: WorkflowProgressUpdated) -> None:
        # Update phase bar
        if self._phase_bar and message.current_phase:
            self._phase_bar.phase = message.current_phase.lower()

        # Tree is updated via app._notify_ac_tree_updated() (SSOT pattern)
        # No need to independently convert AC list here

        # Update progress counter
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
        """Handle sub-task updates.

        Tree state is managed by app.py (SSOT) via _notify_ac_tree_updated.
        This handler only tracks subtasks locally for reference.
        """
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
            self._subtasks[ac_index].append(
                {
                    "id": message.sub_task_id,
                    "index": message.sub_task_index,
                    "content": message.content,
                    "status": message.status,
                }
            )

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

    def on_tool_call_completed(self, message: ToolCallCompleted) -> None:
        """Handle tool call completed - clear inline activity."""
        if self._tree:
            self._tree.clear_node_activity(message.ac_id)
        if self._activity_bar:
            tools = dict(self._activity_bar.active_tools)
            tools.pop(message.ac_id, None)
            self._activity_bar.active_tools = tools

    def on_agent_thinking_updated(self, message: AgentThinkingUpdated) -> None:
        """Handle agent thinking - update detail panel if selected."""
        if self._detail_panel and self._detail_panel.selected_node:
            node_id = self._detail_panel.selected_node.get("id")
            if node_id == message.ac_id:
                self._detail_panel.update_thinking(message.thinking_text)

    def _convert_ac_list_to_tree(
        self,
        acceptance_criteria: list[dict[str, Any]],
        current_ac_index: int | None,
    ) -> dict[str, Any]:
        """Convert flat AC list to tree format."""
        nodes = {}
        child_ids = []
        root_id = "root"

        for ac in acceptance_criteria:
            ac_index = ac.get("index", 0)
            ac_id = f"ac_{ac_index}"
            child_ids.append(ac_id)

            status = ac.get("status", "pending")
            if status == "in_progress":
                status = "executing"

            nodes[ac_id] = {
                "id": ac_id,
                "content": ac.get("content", ""),
                "status": status,
                "depth": 1,
                "is_atomic": True,
                "children_ids": [],
            }

        nodes[root_id] = {
            "id": root_id,
            "content": "Acceptance Criteria",
            "status": "executing" if current_ac_index is not None else "pending",
            "depth": 0,
            "is_atomic": False,
            "children_ids": child_ids,
        }

        return {"root_id": root_id, "nodes": nodes}

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

    def action_logs(self) -> None:
        self.app.push_screen("logs")

    def action_debug(self) -> None:
        self.app.push_screen("debug")


__all__ = [
    "DashboardScreenV3",
    "DoubleDiamondBar",
    "LiveActivityBar",
    "NodeDetailPanel",
    "NodeSelected",
    "SelectableACTree",
]
