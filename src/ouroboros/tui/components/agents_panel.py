"""Agent Status Panel - Active/Queued/Completed agents display.

Shows real-time agent pool status including:
- Active agents with current tasks
- Queued agents waiting for tasks
- Completed/failed agent metrics
- Agent pool utilization
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import DataTable, Static

from ouroboros.tui.events import TUIState


@dataclass
class AgentStatus:
    """Status of a single agent."""

    agent_id: str
    state: str  # idle, busy, failed, recovering
    current_task: str | None = None
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_tokens_used: int = 0
    model_tier: str = "sonnet"  # haiku, sonnet, opus


@dataclass
class PoolMetrics:
    """Agent pool metrics."""

    total_agents: int = 0
    active_count: int = 0
    idle_count: int = 0
    failed_count: int = 0
    queued_tasks: int = 0
    utilization_percent: float = 0.0


class AgentsPanel(Widget):
    """Agent pool status panel.

    Displays:
    - Pool metrics summary
    - Individual agent status table
    - Real-time updates from TUIState

    Attributes:
        agents: Current agent statuses.
        metrics: Pool metrics.
    """

    DEFAULT_CSS = """
    AgentsPanel {
        height: auto;
        min-height: 10;
        width: 100%;
        padding: 1;
        border: round $primary;
        background: $surface;
    }

    AgentsPanel > .header {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }

    AgentsPanel > .metrics-row {
        height: 1;
        width: 100%;
        margin-bottom: 1;
    }

    AgentsPanel > .metrics-row > .metric {
        width: 1fr;
        text-align: center;
    }

    AgentsPanel > .metrics-row > .metric > .value {
        color: $text;
        text-style: bold;
    }

    AgentsPanel > .metrics-row > .metric > .label {
        color: $text-muted;
    }

    AgentsPanel > .metrics-row > .metric.high-utilization > .value {
        color: $success;
    }

    AgentsPanel > .metrics-row > .metric.medium-utilization > .value {
        color: $warning;
    }

    AgentsPanel > .metrics-row > .metric.low-utilization > .value {
        color: $error;
    }

    AgentsPanel > DataTable {
        height: 1fr;
        width: 100%;
    }

    AgentsPanel .state-idle {
        color: $success;
    }

    AgentsPanel .state-busy {
        color: $warning;
    }

    AgentsPanel .state-failed {
        color: $error;
    }

    AgentsPanel .state-recovering {
        color: $accent;
    }

    AgentsPanel .tier-haiku {
        color: $secondary;
    }

    AgentsPanel .tier-sonnet {
        color: $primary;
    }

    AgentsPanel .tier-opus {
        color: $warning;
    }
    """

    agents: reactive[dict[str, AgentStatus]] = reactive({}, always_update=True)
    metrics: reactive[PoolMetrics] = reactive(PoolMetrics())

    def __init__(
        self,
        state: TUIState | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize agents panel.

        Args:
            state: TUIState for tracking agent activity.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._state = state
        self._metrics_widgets: dict[str, Static] = {}

    def compose(self) -> ComposeResult:
        """Compose the panel layout."""
        yield Static("╔══ AGENT POOL STATUS ══╗", classes="header")

        # Metrics row
        with Static(classes="metrics-row"):
            self._metrics_widgets["total"] = Static("", classes="metric", id="metric-total")
            yield self._metrics_widgets["total"]

            self._metrics_widgets["active"] = Static("", classes="metric", id="metric-active")
            yield self._metrics_widgets["active"]

            self._metrics_widgets["idle"] = Static("", classes="metric", id="metric-idle")
            yield self._metrics_widgets["idle"]

            self._metrics_widgets["queued"] = Static("", classes="metric", id="metric-queued")
            yield self._metrics_widgets["queued"]

        yield DataTable(id="agents-table")

    def on_mount(self) -> None:
        """Initialize the data table."""
        table = self.query_one("#agents-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True

        # Add columns
        table.add_column("ID", key="id", width=12)
        table.add_column("State", key="state", width=10)
        table.add_column("Tier", key="tier", width=8)
        table.add_column("Task", key="task", width=20)
        table.add_column("Completed", key="completed", width=8)
        table.add_column("Failed", key="failed", width=6)
        table.add_column("Tokens", key="tokens", width=10)

        self._update_metrics_display()

    def _update_metrics_display(self) -> None:
        """Update metrics display."""
        metrics = self.metrics

        # Update total count
        if "total" in self._metrics_widgets:
            self._metrics_widgets["total"].update(
                f"[value]{metrics.total_agents}[/] [label]Total[/]"
            )

        # Update active count
        if "active" in self._metrics_widgets:
            self._metrics_widgets["active"].update(
                f"[value]{metrics.active_count}[/] [label]Active[/]"
            )

        # Update idle count
        if "idle" in self._metrics_widgets:
            self._metrics_widgets["idle"].update(f"[value]{metrics.idle_count}[/] [label]Idle[/]")

        # Update queued count
        if "queued" in self._metrics_widgets:
            self._metrics_widgets["queued"].update(
                f"[value]{metrics.queued_tasks}[/] [label]Queued[/]"
            )

        # Update utilization class on parent
        if metrics.utilization_percent < 50 or metrics.utilization_percent < 80:
            pass

        # Update table rows
        try:
            table = self.query_one("#agents-table", DataTable)
            table.clear()

            for agent_id, status in self.agents.items():
                state_class = f"state-{status.state}"
                tier_class = f"tier-{status.model_tier}"

                table.add_row(
                    f"[cyan]{agent_id}[/]",
                    f"[{state_class}]{status.state.upper()}[/]",
                    f"[{tier_class}]{status.model_tier}[/]",
                    status.current_task or "[dim]--[/]",
                    str(status.tasks_completed),
                    str(status.tasks_failed),
                    self._format_tokens(status.total_tokens_used),
                )

            # Add utilization footer
            if metrics.total_agents > 0:
                table.add_row(
                    "---",
                    "---",
                    "---",
                    "[bold]Utilization:[/]",
                    "---",
                    "---",
                    f"[bold]{metrics.utilization_percent:.0f}%[/]",
                )
        except Exception:
            pass

    def _format_tokens(self, tokens: int) -> str:
        """Format token count for display.

        Args:
            tokens: Token count.

        Returns:
            Formatted string (e.g., "1.2K", "5.3M").
        """
        if tokens >= 1_000_000:
            return f"{tokens / 1_000_000:.1f}M"
        elif tokens >= 1_000:
            return f"{tokens / 1_000:.1f}K"
        else:
            return str(tokens)

    def watch_agents(self, _: dict[str, AgentStatus]) -> None:
        """React to agent list changes."""
        # Recalculate metrics
        metrics = PoolMetrics(
            total_agents=len(self.agents),
            active_count=sum(1 for a in self.agents.values() if a.state == "busy"),
            idle_count=sum(1 for a in self.agents.values() if a.state == "idle"),
            failed_count=sum(1 for a in self.agents.values() if a.state == "failed"),
            queued_tasks=0,  # Would come from state
        )

        if metrics.total_agents > 0:
            metrics.utilization_percent = (metrics.active_count / metrics.total_agents) * 100

        self.metrics = metrics
        self._update_metrics_display()

    def watch_metrics(self, _: PoolMetrics) -> None:
        """React to metrics changes."""
        self._update_metrics_display()

    def update_from_state(self, state: TUIState) -> None:
        """Update agents from TUIState.

        Args:
            state: Current TUI state.
        """
        # Extract agent info from active_tools and tool_history
        agents: dict[str, AgentStatus] = {}

        for ac_id, tool_info in state.active_tools.items():
            # Create synthetic agent status for active tools
            agent_id = f"agent_{ac_id}"
            agents[agent_id] = AgentStatus(
                agent_id=agent_id,
                state="busy",
                current_task=tool_info.get("tool_detail", tool_info.get("tool_name", "Unknown")),
                model_tier="sonnet",  # Default
            )

        # Add some simulated idle agents for demo
        for i in range(3):
            agent_id = f"agent_{i}"
            if agent_id not in agents:
                agents[agent_id] = AgentStatus(
                    agent_id=agent_id,
                    state="idle",
                    model_tier="sonnet",
                )

        self.agents = agents

    def add_agent(
        self,
        agent_id: str,
        state: str = "idle",
        model_tier: str = "sonnet",
    ) -> None:
        """Add a new agent to the pool.

        Args:
            agent_id: Unique agent identifier.
            state: Initial agent state.
            model_tier: Model tier being used.
        """
        agents = dict(self.agents)
        agents[agent_id] = AgentStatus(
            agent_id=agent_id,
            state=state,
            model_tier=model_tier,
        )
        self.agents = agents

    def update_agent_state(
        self,
        agent_id: str,
        state: str | None = None,
        current_task: str | None = None,
    ) -> None:
        """Update agent state.

        Args:
            agent_id: Agent identifier.
            state: New state (if provided).
            current_task: Current task (if provided).
        """
        if agent_id not in self.agents:
            self.add_agent(agent_id, state or "idle")

        agents = dict(self.agents)
        agent = agents[agent_id]

        if state is not None:
            agent.state = state

        if current_task is not None:
            agent.current_task = current_task

        self.agents = agents

    def remove_agent(self, agent_id: str) -> None:
        """Remove an agent from the pool.

        Args:
            agent_id: Agent identifier.
        """
        agents = dict(self.agents)
        agents.pop(agent_id, None)
        self.agents = agents


__all__ = ["AgentStatus", "AgentsPanel", "PoolMetrics"]
