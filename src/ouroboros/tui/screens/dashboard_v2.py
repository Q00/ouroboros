"""Dashboard V2 - Tree-Centric Command Center.

A redesigned dashboard with the AC Tree as the main focus,
designed for monitoring parallel AC execution with a
"Command Center" aesthetic.

Layout:
    ┌─────────────────────────────────────────────────────────┐
    │ ◆ OUROBOROS COMMAND CENTER          [Phase] [Drift] [$] │
    ├─────────────────────────────────────────────────────────┤
    │                                                         │
    │                    AC EXECUTION TREE                    │
    │                                                         │
    │    ┌─[OK] Setup environment                             │
    │    ├─[*] Process data                                   │
    │    │   ├─[*] Parse input ──────────┐                    │
    │    │   └─[*] Validate ─────────────┼─▶ [  ] Finalize    │
    │    └─[  ] Cleanup                                       │
    │                                                         │
    ├─────────────────────────────────────────────────────────┤
    │ ▸ Current: Processing data... │ Tools: 12 │ Time: 2:34  │
    └─────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Footer, Label, Static

from ouroboros.tui.events import (
    ACUpdated,
    CostUpdated,
    DriftUpdated,
    ExecutionUpdated,
    ParallelBatchCompleted,
    ParallelBatchStarted,
    PauseRequested,
    PhaseChanged,
    ResumeRequested,
    WorkflowProgressUpdated,
)
from ouroboros.tui.widgets import (
    ACTreeWidget,
    ParallelGraphWidget,
)

if TYPE_CHECKING:
    from ouroboros.tui.events import TUIState


# ═══════════════════════════════════════════════════════════════════════════════
# MINI WIDGETS - Compact status indicators for the command bar
# ═══════════════════════════════════════════════════════════════════════════════


class MiniPhaseIndicator(Static):
    """Compact phase indicator showing current Double Diamond phase."""

    DEFAULT_CSS = """
    MiniPhaseIndicator {
        width: auto;
        height: 1;
        padding: 0 1;
    }
    """

    phase: reactive[str] = reactive("discover")

    PHASE_ICONS = {
        "discover": "[bold cyan]◇[/] DIS",
        "define": "[bold blue]◆[/] DEF",
        "design": "[bold magenta]◇[/] DES",
        "deliver": "[bold green]◆[/] DEL",
    }

    def render(self) -> str:
        return self.PHASE_ICONS.get(self.phase.lower(), "[dim]---[/]")


class MiniDriftIndicator(Static):
    """Compact drift indicator with color-coded status."""

    DEFAULT_CSS = """
    MiniDriftIndicator {
        width: auto;
        height: 1;
        padding: 0 1;
    }
    """

    drift: reactive[float] = reactive(0.0)
    threshold: float = 0.3

    def render(self) -> str:
        pct = int(self.drift * 100)
        if self.drift >= self.threshold:
            return f"[bold red]⚠ {pct}%[/]"
        elif self.drift >= self.threshold * 0.7:
            return f"[yellow]◉ {pct}%[/]"
        else:
            return f"[green]● {pct}%[/]"


class MiniCostIndicator(Static):
    """Compact cost indicator."""

    DEFAULT_CSS = """
    MiniCostIndicator {
        width: auto;
        height: 1;
        padding: 0 1;
    }
    """

    tokens: reactive[int] = reactive(0)
    cost_usd: reactive[float] = reactive(0.0)

    def render(self) -> str:
        if self.tokens >= 1_000_000:
            token_str = f"{self.tokens / 1_000_000:.1f}M"
        elif self.tokens >= 1_000:
            token_str = f"{self.tokens / 1_000:.0f}K"
        else:
            token_str = str(self.tokens)

        cost_color = (
            "red" if self.cost_usd >= 1.0 else "yellow" if self.cost_usd >= 0.1 else "green"
        )
        return f"[{cost_color}]${self.cost_usd:.2f}[/] [dim]({token_str})[/]"


class MiniStatusIndicator(Static):
    """Compact status indicator with animation."""

    DEFAULT_CSS = """
    MiniStatusIndicator {
        width: auto;
        height: 1;
        padding: 0 1;
    }
    """

    status: reactive[str] = reactive("idle")

    STATUS_DISPLAY = {
        "idle": "[dim]○ IDLE[/]",
        "running": "[bold green]◉ RUNNING[/]",
        "paused": "[bold yellow]◎ PAUSED[/]",
        "completed": "[bold cyan]✓ DONE[/]",
        "failed": "[bold red]✗ FAILED[/]",
    }

    def render(self) -> str:
        return self.STATUS_DISPLAY.get(self.status, f"[dim]{self.status}[/]")


# ═══════════════════════════════════════════════════════════════════════════════
# COMMAND BAR - Top bar with all status indicators
# ═══════════════════════════════════════════════════════════════════════════════


class CommandBar(Static):
    """Top command bar with logo and compact status indicators."""

    DEFAULT_CSS = """
    CommandBar {
        width: 100%;
        height: 3;
        background: $surface;
        border-bottom: heavy $primary;
    }

    CommandBar > .bar-content {
        width: 100%;
        height: 1;
        padding: 0 2;
        margin-top: 1;
    }

    CommandBar > .bar-content > .logo {
        width: auto;
    }

    CommandBar > .bar-content > .spacer {
        width: 1fr;
    }

    CommandBar > .bar-content > .indicators {
        width: auto;
        height: 1;
    }

    CommandBar > .bar-content > .indicators > * {
        margin-left: 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(classes="bar-content"):
            yield Static(
                "[bold cyan]◆[/] [bold]OUROBOROS[/] [dim]Command Center[/]", classes="logo"
            )
            yield Static("", classes="spacer")
            with Horizontal(classes="indicators"):
                yield MiniStatusIndicator(id="mini-status")
                yield MiniPhaseIndicator(id="mini-phase")
                yield MiniDriftIndicator(id="mini-drift")
                yield MiniCostIndicator(id="mini-cost")


# ═══════════════════════════════════════════════════════════════════════════════
# ACTIVITY BAR - Bottom bar with current activity
# ═══════════════════════════════════════════════════════════════════════════════


class ActivityBar(Static):
    """Bottom activity bar showing current operation."""

    DEFAULT_CSS = """
    ActivityBar {
        width: 100%;
        height: 3;
        background: $surface;
        border-top: heavy $primary-darken-2;
    }

    ActivityBar > .bar-content {
        width: 100%;
        height: 1;
        padding: 0 2;
        margin-top: 1;
    }

    ActivityBar > .bar-content > .activity-text {
        width: 1fr;
    }

    ActivityBar > .bar-content > .stats {
        width: auto;
    }
    """

    activity: reactive[str] = reactive("")
    tool_count: reactive[int] = reactive(0)
    elapsed: reactive[str] = reactive("")

    def compose(self) -> ComposeResult:
        with Horizontal(classes="bar-content"):
            yield Static(id="activity-text", classes="activity-text")
            yield Static(id="stats-text", classes="stats")

    def on_mount(self) -> None:
        self._update_display()

    def watch_activity(self, _value: str) -> None:
        self._update_display()

    def watch_tool_count(self, _value: int) -> None:
        self._update_display()

    def watch_elapsed(self, _value: str) -> None:
        self._update_display()

    def _update_display(self) -> None:
        try:
            activity_elem = self.query_one("#activity-text", Static)
            if self.activity:
                activity_elem.update(f"[bold cyan]▸[/] {self.activity}")
            else:
                activity_elem.update("[dim]▸ Waiting for activity...[/]")

            stats_elem = self.query_one("#stats-text", Static)
            stats_parts = []
            if self.tool_count > 0:
                stats_parts.append(f"[cyan]Tools: {self.tool_count}[/]")
            if self.elapsed:
                stats_parts.append(f"[dim]Time: {self.elapsed}[/]")
            stats_elem.update(" │ ".join(stats_parts) if stats_parts else "")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# ENHANCED AC TREE - Main focus area with improved styling
# ═══════════════════════════════════════════════════════════════════════════════


class EnhancedACTree(Static):
    """Enhanced AC Tree with better styling and selection support."""

    DEFAULT_CSS = """
    EnhancedACTree {
        width: 100%;
        height: 1fr;
        padding: 1 2;
        border: round $primary;
        background: $background;
    }

    EnhancedACTree > .tree-header {
        width: 100%;
        height: 1;
        text-align: center;
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }

    EnhancedACTree > .tree-container {
        width: 100%;
        height: 1fr;
    }

    EnhancedACTree > .tree-container > ACTreeWidget {
        height: 100%;
        max-height: none;
        padding: 0;
        border: none;
        background: transparent;
    }

    EnhancedACTree > .parallel-section {
        width: 100%;
        height: auto;
        margin-top: 1;
        padding-top: 1;
        border-top: dashed $primary-darken-2;
    }

    EnhancedACTree > .parallel-section > ParallelGraphWidget {
        height: auto;
        max-height: 10;
        padding: 0;
        border: none;
        background: transparent;
    }
    """

    show_parallel_graph: reactive[bool] = reactive(False)

    def __init__(
        self,
        tree_data: dict[str, Any] | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._tree_data = tree_data or {}
        self._ac_tree: ACTreeWidget | None = None
        self._parallel_graph: ParallelGraphWidget | None = None

    def compose(self) -> ComposeResult:
        yield Label(
            "─────────────── [bold]AC EXECUTION TREE[/] ───────────────",
            classes="tree-header",
        )

        with Vertical(classes="tree-container"):
            self._ac_tree = ACTreeWidget(tree_data=self._tree_data)
            yield self._ac_tree

        if self.show_parallel_graph:
            with Vertical(classes="parallel-section"):
                self._parallel_graph = ParallelGraphWidget()
                yield self._parallel_graph

    def update_tree(self, tree_data: dict[str, Any]) -> None:
        """Update the tree data."""
        self._tree_data = tree_data
        if self._ac_tree is not None:
            self._ac_tree.update_tree(tree_data)

    def update_node_status(self, ac_id: str, status: str) -> None:
        """Update a single node's status."""
        if self._ac_tree is not None:
            self._ac_tree.update_node_status(ac_id, status)

    def get_tree_widget(self) -> ACTreeWidget | None:
        """Get the underlying AC tree widget."""
        return self._ac_tree

    def get_parallel_graph(self) -> ParallelGraphWidget | None:
        """Get the parallel graph widget."""
        return self._parallel_graph


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD V2 - Main Screen
# ═══════════════════════════════════════════════════════════════════════════════


class DashboardScreenV2(Screen[None]):
    """Tree-centric dashboard for monitoring AC execution.

    Features:
    - Large, central AC Tree view
    - Compact command bar with all status indicators
    - Bottom activity bar for current operation
    - Support for parallel execution visualization

    Bindings:
        p: Pause execution
        r: Resume execution
        g: Toggle parallel graph view
        l: Switch to logs view
        d: Switch to debug view
        e: Switch to execution detail view
    """

    BINDINGS = [
        Binding("p", "pause", "Pause"),
        Binding("r", "resume", "Resume"),
        Binding("g", "toggle_graph", "Graph"),
        Binding("l", "logs", "Logs"),
        Binding("d", "debug", "Debug"),
        Binding("e", "execution", "Detail"),
    ]

    DEFAULT_CSS = """
    DashboardScreenV2 {
        layout: vertical;
        background: $background;
    }

    DashboardScreenV2 > .main-container {
        width: 100%;
        height: 1fr;
        padding: 1 2;
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
        """Initialize dashboard V2.

        Args:
            state: Initial TUI state.
            name: Screen name.
            id: Screen ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._state = state
        self._command_bar: CommandBar | None = None
        self._activity_bar: ActivityBar | None = None
        self._enhanced_tree: EnhancedACTree | None = None

    def compose(self) -> ComposeResult:
        # Command bar (top)
        self._command_bar = CommandBar()
        yield self._command_bar

        # Main tree area
        with Container(classes="main-container"):
            self._enhanced_tree = EnhancedACTree(
                tree_data=self._state.ac_tree if self._state else {},
            )
            yield self._enhanced_tree

        # Activity bar (bottom)
        self._activity_bar = ActivityBar()
        yield self._activity_bar

        yield Footer()

    # ─────────────────────────────────────────────────────────────────────────
    # Message Handlers
    # ─────────────────────────────────────────────────────────────────────────

    def on_execution_updated(self, message: ExecutionUpdated) -> None:
        """Handle execution update message."""
        if self._command_bar:
            try:
                status_widget = self._command_bar.query_one("#mini-status", MiniStatusIndicator)
                status_widget.status = message.status
            except Exception:
                pass

    def on_phase_changed(self, message: PhaseChanged) -> None:
        """Handle phase change message."""
        if self._command_bar:
            try:
                phase_widget = self._command_bar.query_one("#mini-phase", MiniPhaseIndicator)
                phase_widget.phase = message.current_phase
            except Exception:
                pass

    def on_drift_updated(self, message: DriftUpdated) -> None:
        """Handle drift update message."""
        if self._command_bar:
            try:
                drift_widget = self._command_bar.query_one("#mini-drift", MiniDriftIndicator)
                drift_widget.drift = message.combined_drift
            except Exception:
                pass

    def on_cost_updated(self, message: CostUpdated) -> None:
        """Handle cost update message."""
        if self._command_bar:
            try:
                cost_widget = self._command_bar.query_one("#mini-cost", MiniCostIndicator)
                cost_widget.tokens = message.total_tokens
                cost_widget.cost_usd = message.total_cost_usd
            except Exception:
                pass

    def on_ac_updated(self, message: ACUpdated) -> None:
        """Handle AC update message."""
        if self._enhanced_tree:
            self._enhanced_tree.update_node_status(message.ac_id, message.status)

    def on_parallel_batch_started(self, message: ParallelBatchStarted) -> None:
        """Handle parallel batch started message."""
        if self._activity_bar:
            self._activity_bar.activity = f"Executing batch {message.batch_index + 1}/{message.total_batches} ({len(message.ac_ids)} ACs in parallel)"

        # Update parallel graph if available
        if self._enhanced_tree:
            graph = self._enhanced_tree.get_parallel_graph()
            if graph:
                graph.set_executing(message.ac_ids)

    def on_parallel_batch_completed(self, message: ParallelBatchCompleted) -> None:
        """Handle parallel batch completed message."""
        if self._activity_bar:
            status = "✓" if message.failed_count == 0 else f"⚠ {message.failed_count} failed"
            self._activity_bar.activity = f"Batch {message.batch_index + 1} completed: {message.successful_count}/{message.total_in_batch} {status}"

    def on_workflow_progress_updated(self, message: WorkflowProgressUpdated) -> None:
        """Handle workflow progress update message."""
        # Update command bar indicators
        if self._command_bar:
            try:
                status_widget = self._command_bar.query_one("#mini-status", MiniStatusIndicator)
                if message.total_count > 0 and message.completed_count >= message.total_count:
                    status_widget.status = "completed"
                else:
                    status_widget.status = "running"

                phase_widget = self._command_bar.query_one("#mini-phase", MiniPhaseIndicator)
                phase_widget.phase = (
                    message.current_phase.lower() if message.current_phase else "discover"
                )

                cost_widget = self._command_bar.query_one("#mini-cost", MiniCostIndicator)
                cost_widget.tokens = message.estimated_tokens
                cost_widget.cost_usd = message.estimated_cost_usd
            except Exception:
                pass

        # Update activity bar
        if self._activity_bar:
            activity_display = message.activity or "Idle"
            if message.activity_detail:
                activity_display = f"{message.activity}: {message.activity_detail}"
            self._activity_bar.activity = activity_display
            self._activity_bar.tool_count = message.tool_calls_count
            self._activity_bar.elapsed = message.elapsed_display

        # Update AC tree
        if self._enhanced_tree and message.acceptance_criteria:
            tree_data = self._convert_ac_list_to_tree(
                message.acceptance_criteria,
                message.current_ac_index,
            )
            self._enhanced_tree.update_tree(tree_data)

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
            elif status == "completed":
                status = "completed"
            else:
                status = "pending"

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
            "status": "executing" if current_ac_index else "pending",
            "depth": 0,
            "is_atomic": False,
            "children_ids": child_ids,
        }

        return {"root_id": root_id, "nodes": nodes}

    # ─────────────────────────────────────────────────────────────────────────
    # Actions
    # ─────────────────────────────────────────────────────────────────────────

    def action_pause(self) -> None:
        """Handle pause action."""
        if self._state and self._state.execution_id:
            self.post_message(PauseRequested(self._state.execution_id))

    def action_resume(self) -> None:
        """Handle resume action."""
        if self._state and self._state.execution_id:
            self.post_message(ResumeRequested(self._state.execution_id))

    def action_toggle_graph(self) -> None:
        """Toggle parallel graph view."""
        if self._enhanced_tree:
            self._enhanced_tree.show_parallel_graph = not self._enhanced_tree.show_parallel_graph
            self._enhanced_tree.refresh(recompose=True)

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
        """Update the entire state."""
        self._state = state

        if self._command_bar:
            try:
                self._command_bar.query_one(
                    "#mini-status", MiniStatusIndicator
                ).status = state.status
                self._command_bar.query_one(
                    "#mini-phase", MiniPhaseIndicator
                ).phase = state.current_phase
                self._command_bar.query_one(
                    "#mini-drift", MiniDriftIndicator
                ).drift = state.combined_drift
                cost_widget = self._command_bar.query_one("#mini-cost", MiniCostIndicator)
                cost_widget.tokens = state.total_tokens
                cost_widget.cost_usd = state.total_cost_usd
            except Exception:
                pass

        if self._enhanced_tree:
            self._enhanced_tree.update_tree(state.ac_tree)


__all__ = ["DashboardScreenV2"]
