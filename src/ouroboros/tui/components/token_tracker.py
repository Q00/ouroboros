"""Token Usage Tracker - Real-time cost tracking.

Displays:
- Total tokens consumed
- Per-agent token usage
- Estimated costs
- Budget warnings
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import ProgressBar, Static

from ouroboros.tui.events import TUIState


class BudgetThreshold(Enum):
    """Budget warning thresholds."""

    OK = 0.0
    WARNING = 0.5  # 50% of budget
    HIGH = 0.8  # 80% of budget
    CRITICAL = 0.95  # 95% of budget


@dataclass
class TokenUsage:
    """Token usage for a single entity."""

    entity_id: str
    entity_name: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    model_tier: str = "sonnet"


@dataclass
class TokenSummary:
    """Summary of all token usage."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    budget_usd: float | None = None
    budget_threshold: BudgetThreshold = BudgetThreshold.OK

    # Per-tier breakdown
    haiku_tokens: int = 0
    sonnet_tokens: int = 0
    opus_tokens: int = 0


# Cost per 1M tokens (approximate USD costs)
TIER_COSTS = {
    "haiku": 0.25,
    "sonnet": 3.0,
    "opus": 15.0,
}


class TokenTracker(Widget):
    """Token usage and cost tracking widget.

    Displays:
    - Summary of total usage and cost
    - Budget progress bar (if budget set)
    - Per-tier breakdown
    - Recent per-agent usage

    Attributes:
        summary: Token usage summary.
        per_agent_usage: Token usage by agent.
        budget_usd: Optional budget limit.
    """

    DEFAULT_CSS = """
    TokenTracker {
        height: auto;
        min-height: 8;
        width: 100%;
        padding: 1;
        border: round $success;
        background: $surface;
    }

    TokenTracker > .header {
        text-style: bold;
        color: $success;
        text-align: center;
        margin-bottom: 1;
    }

    TokenTracker > .summary-row {
        height: 1;
        width: 100%;
        margin-bottom: 1;
    }

    TokenTracker > .summary-row > .summary-item {
        width: 1fr;
        text-align: center;
    }

    TokenTracker > .summary-row > .summary-item > .value {
        color: $text;
        text-style: bold;
    }

    TokenTracker > .summary-row > .summary-item > .label {
        color: $text-muted;
    }

    TokenTracker > .tier-row {
        height: 1;
        width: 100%;
        margin-bottom: 1;
    }

    TokenTracker > .tier-row > .tier-item {
        width: 1fr;
        text-align: center;
    }

    TokenTracker > .tier-row > .tier-item > .tier-value {
        color: $secondary;
    }

    TokenTracker > .tier-row > .tier-item > .tier-label {
        color: $text-muted;
    }

    TokenTracker > ProgressBar {
        height: 1;
        margin-top: 1;
        margin-bottom: 1;
    }

    TokenTracker > ProgressBar.budget-ok {
        --bar-background: $success;
    }

    TokenTracker > ProgressBar.budget-warning {
        --bar-background: $warning;
    }

    TokenTracker > ProgressBar.budget-high {
        --bar-background: $error;
    }

    TokenTracker > ProgressBar.budget-critical {
        --bar-background: $error 100% 0%;
        animation: pulse 1s infinite;
    }

    TokenTracker > .budget-status {
        text-align: center;
        color: $text-muted;
    }

    TokenTracker > .budget-status.warning {
        color: $warning;
        text-style: bold;
    }

    TokenTracker > .budget-status.high {
        color: $error;
        text-style: bold;
    }

    TokenTracker > .budget-status.critical {
        color: $error;
        text-style: bold;
        animation: pulse 0.5s infinite;
    }

    TokenTracker > .agent-list {
        height: auto;
        max-height: 10;
        overflow-y: auto;
    }

    TokenTracker > .agent-list > .agent-item {
        height: 1;
        width: 100%;
        padding: 0 1;
    }

    TokenTracker > .agent-list > .agent-item > .agent-name {
        width: 20;
        color: $text;
    }

    TokenTracker > .agent-list > .agent-item > .agent-tokens {
        width: 10;
        text-align: right;
        color: $text;
    }

    TokenTracker > .agent-list > .agent-item > .agent-cost {
        width: 10;
        text-align: right;
        color: $success;
    }
    """

    summary: reactive[TokenSummary] = reactive(TokenSummary())
    per_agent_usage: reactive[dict[str, TokenUsage]] = reactive({}, always_update=True)
    budget_usd: reactive[float | None] = reactive(None)

    def __init__(
        self,
        state: TUIState | None = None,
        budget_usd: float | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize token tracker.

        Args:
            state: TUIState for tracking tokens.
            budget_usd: Optional budget limit in USD.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._state = state
        self.budget_usd = budget_usd

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Static("╔══ TOKEN TRACKER ══╗", classes="header")

        # Summary row
        with Static(classes="summary-row"):
            yield Static(f"[value]0[/] [label]Tokens[/]", classes="summary-item", id="summary-tokens")
            yield Static(f"[value]$0.00[/] [label]Cost[/]", classes="summary-item", id="summary-cost")
            yield Static(f"[value]0[/] [label]Input[/]", classes="summary-item", id="summary-input")
            yield Static(f"[value]0[/] [label]Output[/]", classes="summary-item", id="summary-output")

        # Tier breakdown
        with Static(classes="tier-row"):
            yield Static("[tier-value]0[/] [tier-label]Haiku[/]", classes="tier-item", id="tier-haiku")
            yield Static("[tier-value]0[/] [tier-label]Sonnet[/]", classes="tier-item", id="tier-sonnet")
            yield Static("[tier-value]0[/] [tier-label]Opus[/]", classes="tier-item", id="tier-opus")

        # Budget progress (only show if budget is set)
        if self.budget_usd:
            yield ProgressBar(id="budget-bar", show_eta=False, classes="budget-ok")
            yield Static("", classes="budget-status", id="budget-status")

    def on_mount(self) -> None:
        """Initialize display."""
        self._update_display()

    def _update_display(self) -> None:
        """Update all display elements."""
        summary = self.summary

        # Update summary values
        try:
            self.query_one("#summary-tokens", Static).update(
                f"[value]{self._format_tokens(summary.total_tokens)}[/] [label]Tokens[/]"
            )
            self.query_one("#summary-cost", Static).update(
                f"[value]${summary.total_cost_usd:.2f}[/] [label]Cost[/]"
            )
            self.query_one("#summary-input", Static).update(
                f"[value]{self._format_tokens(summary.total_input_tokens)}[/] [label]Input[/]"
            )
            self.query_one("#summary-output", Static).update(
                f"[value]{self._format_tokens(summary.total_output_tokens)}[/] [label]Output[/]"
            )
        except Exception:
            pass

        # Update tier breakdown
        try:
            self.query_one("#tier-haiku", Static).update(
                f"[tier-value]{self._format_tokens(summary.haiku_tokens)}[/] [tier-label]Haiku[/]"
            )
            self.query_one("#tier-sonnet", Static).update(
                f"[tier-value]{self._format_tokens(summary.sonnet_tokens)}[/] [tier-label]Sonnet[/]"
            )
            self.query_one("#tier-opus", Static).update(
                f"[tier-value]{self._format_tokens(summary.opus_tokens)}[/] [tier-label]Opus[/]"
            )
        except Exception:
            pass

        # Update budget bar
        if self.budget_usd and self.budget_usd > 0:
            try:
                bar = self.query_one("#budget-bar", ProgressBar)
                progress = (summary.total_cost_usd / self.budget_usd) * 100
                bar.progress = min(progress, 100)

                # Update bar color based on threshold
                bar.remove_class("budget-ok", "budget-warning", "budget-high", "budget-critical")
                threshold = summary.budget_threshold

                if threshold == BudgetThreshold.OK:
                    bar.add_class("budget-ok")
                elif threshold == BudgetThreshold.WARNING:
                    bar.add_class("budget-warning")
                elif threshold == BudgetThreshold.HIGH:
                    bar.add_class("budget-high")
                else:
                    bar.add_class("budget-critical")

                # Update status text
                status_widget = self.query_one("#budget-status", Static)
                status_widget.remove_class("warning", "high", "critical")

                if threshold == BudgetThreshold.CRITICAL:
                    status_widget.add_class("critical")
                    status_widget.update(f"CRITICAL: {progress:.0f}% of ${self.budget_usd:.2f} budget used!")
                elif threshold == BudgetThreshold.HIGH:
                    status_widget.add_class("high")
                    status_widget.update(f"WARNING: {progress:.0f}% of budget used")
                elif threshold == BudgetThreshold.WARNING:
                    status_widget.add_class("warning")
                    status_widget.update(f"{progress:.0f}% of ${self.budget_usd:.2f} budget used")
                else:
                    status_widget.update(f"{progress:.0f}% of ${self.budget_usd:.2f} budget used")

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

    def watch_summary(self, _: TokenSummary) -> None:
        """React to summary changes."""
        self._update_display()

    def watch_budget_usd(self, _: float | None) -> None:
        """React to budget changes."""
        self.refresh(recompose=True)

    def update_from_state(self, state: TUIState) -> None:
        """Update tracker from TUIState.

        Args:
            state: Current TUI state.
        """
        summary = TokenSummary(
            total_tokens=state.total_tokens,
            total_cost_usd=state.total_cost_usd,
        )

        # Determine budget threshold
        if self.budget_usd and self.budget_usd > 0:
            ratio = summary.total_cost_usd / self.budget_usd
            if ratio >= 0.95:
                summary.budget_threshold = BudgetThreshold.CRITICAL
            elif ratio >= 0.8:
                summary.budget_threshold = BudgetThreshold.HIGH
            elif ratio >= 0.5:
                summary.budget_threshold = BudgetThreshold.WARNING

        self.summary = summary

    def add_tokens(
        self,
        entity_id: str,
        entity_name: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model_tier: str = "sonnet",
    ) -> None:
        """Add token usage for an entity.

        Args:
            entity_id: Entity identifier.
            entity_name: Entity display name.
            input_tokens: Input tokens consumed.
            output_tokens: Output tokens consumed.
            model_tier: Model tier used.
        """
        total = input_tokens + output_tokens
        cost = (input_tokens * TIER_COSTS.get(model_tier, 3.0) / 1_000_000
                + output_tokens * TIER_COSTS.get(model_tier, 3.0) * 3 / 1_000_000)

        # Update per-agent usage
        agent_usage = self.per_agent_usage.get(entity_id)
        if agent_usage:
            agent_usage.input_tokens += input_tokens
            agent_usage.output_tokens += output_tokens
            agent_usage.total_tokens += total
            agent_usage.estimated_cost_usd += cost
        else:
            self.per_agent_usage[entity_id] = TokenUsage(
                entity_id=entity_id,
                entity_name=entity_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total,
                estimated_cost_usd=cost,
                model_tier=model_tier,
            )

        # Update summary
        summary = self.summary
        summary.total_input_tokens += input_tokens
        summary.total_output_tokens += output_tokens
        summary.total_tokens += total
        summary.total_cost_usd += cost

        # Update tier breakdown
        if model_tier == "haiku":
            summary.haiku_tokens += total
        elif model_tier == "sonnet":
            summary.sonnet_tokens += total
        elif model_tier == "opus":
            summary.opus_tokens += total

        self.summary = summary

    def set_budget(self, budget_usd: float) -> None:
        """Set budget limit.

        Args:
            budget_usd: Budget in USD.
        """
        self.budget_usd = budget_usd

    def reset(self) -> None:
        """Reset all tracking."""
        self.summary = TokenSummary()
        self.per_agent_usage = {}


__all__ = ["BudgetThreshold", "TokenSummary", "TokenTracker", "TokenUsage", "TIER_COSTS"]
