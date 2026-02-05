"""Cost tracking widget.

Displays token usage and estimated costs
for the current execution.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, Static


class CostTrackerWidget(Widget):
    """Widget displaying cost and token usage.

    Shows total tokens, estimated cost, and
    current phase token usage.

    Attributes:
        total_tokens: Total tokens consumed.
        total_cost_usd: Estimated total cost in USD.
        tokens_this_phase: Tokens used in current phase.
        model_name: Name of the model being used.
    """

    DEFAULT_CSS = """
    CostTrackerWidget {
        height: auto;
        width: 100%;
        padding: 1 2;
    }

    CostTrackerWidget > .header {
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    CostTrackerWidget > .metric {
        height: 1;
        width: 100%;
        layout: horizontal;
        margin: 0;
    }

    CostTrackerWidget > .metric > Label {
        width: 1fr;
        color: $text-muted;
    }

    CostTrackerWidget > .metric > .value {
        width: auto;
        text-align: right;
        min-width: 10;
        color: $text;
    }

    CostTrackerWidget > .metric.total > .value {
        text-style: bold;
        color: $primary;
    }

    CostTrackerWidget > .metric.cost > .value {
        color: $success;
    }

    CostTrackerWidget > .metric.cost.high > .value {
        color: $warning;
        text-style: bold;
    }

    CostTrackerWidget > .metric.cost.very-high > .value {
        color: $error;
        text-style: bold;
    }

    CostTrackerWidget > .separator {
        height: 1;
        border-top: dashed $primary-darken-3;
        margin-top: 1;
        margin-bottom: 1;
    }
    """

    total_tokens: reactive[int] = reactive(0)
    total_cost_usd: reactive[float] = reactive(0.0)
    tokens_this_phase: reactive[int] = reactive(0)
    model_name: reactive[str] = reactive("")

    def __init__(
        self,
        total_tokens: int = 0,
        total_cost_usd: float = 0.0,
        tokens_this_phase: int = 0,
        model_name: str = "",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize cost tracker widget.

        Args:
            total_tokens: Initial total tokens.
            total_cost_usd: Initial total cost.
            tokens_this_phase: Initial phase tokens.
            model_name: Model name being used.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        # Internal widget references (must be initialized before reactive props)
        self._total_tokens_value: Static | None = None
        self._phase_tokens_value: Static | None = None
        self._cost_value: Static | None = None
        self._model_value: Static | None = None
        self._cost_container: Widget | None = None

        super().__init__(name=name, id=id, classes=classes)
        self.total_tokens = total_tokens
        self.total_cost_usd = total_cost_usd
        self.tokens_this_phase = tokens_this_phase
        self.model_name = model_name

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Label("Cost Tracker", classes="header")

        # Total tokens
        with Static(classes="metric total"):
            yield Label("Tokens")
            self._total_tokens_value = Static(
                self._format_tokens(self.total_tokens),
                classes="value",
            )
            yield self._total_tokens_value

        # Estimated cost
        cost_class = self._get_cost_class()
        self._cost_container = Static(classes=f"metric cost {cost_class}")
        with self._cost_container:
            yield Label("Cost")
            self._cost_value = Static(
                self._format_cost(self.total_cost_usd),
                classes="value",
            )
            yield self._cost_value

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

    def _format_cost(self, cost: float) -> str:
        """Format cost for display.

        Args:
            cost: Cost in USD.

        Returns:
            Formatted string (e.g., "$0.05", "$1.23").
        """
        if cost < 0.01:
            return f"${cost:.4f}"
        elif cost < 1.0:
            return f"${cost:.3f}"
        else:
            return f"${cost:.2f}"

    def _truncate_model(self, model: str) -> str:
        """Truncate model name for display.

        Args:
            model: Full model name.

        Returns:
            Truncated model name (max 15 chars).
        """
        if len(model) > 15:
            return model[:12] + "..."
        return model

    def _get_cost_class(self) -> str:
        """Get CSS class based on cost level."""
        if self.total_cost_usd >= 10.0:
            return "very-high"
        elif self.total_cost_usd >= 1.0:
            return "high"
        return ""

    def _update_display(self) -> None:
        """Update all display values."""
        if self._total_tokens_value is not None:
            self._total_tokens_value.update(self._format_tokens(self.total_tokens))

        if self._phase_tokens_value is not None:
            self._phase_tokens_value.update(self._format_tokens(self.tokens_this_phase))

        if self._cost_value is not None:
            self._cost_value.update(self._format_cost(self.total_cost_usd))

        if self._cost_container is not None:
            self._cost_container.remove_class("high")
            self._cost_container.remove_class("very-high")
            cost_class = self._get_cost_class()
            if cost_class:
                self._cost_container.add_class(cost_class)

        if self._model_value is not None and self.model_name:
            self._model_value.update(self._truncate_model(self.model_name))

    def watch_total_tokens(self, new_value: int) -> None:
        """React to total_tokens changes."""
        self._update_display()

    def watch_total_cost_usd(self, new_value: float) -> None:
        """React to total_cost_usd changes."""
        self._update_display()

    def watch_tokens_this_phase(self, new_value: int) -> None:
        """React to tokens_this_phase changes."""
        self._update_display()

    def watch_model_name(self, new_value: str) -> None:
        """React to model_name changes."""
        self._update_display()

    def update_cost(
        self,
        total_tokens: int | None = None,
        total_cost_usd: float | None = None,
        tokens_this_phase: int | None = None,
        model_name: str | None = None,
    ) -> None:
        """Update cost metrics.

        Args:
            total_tokens: New total tokens.
            total_cost_usd: New total cost.
            tokens_this_phase: New phase tokens.
            model_name: New model name.
        """
        if total_tokens is not None:
            self.total_tokens = total_tokens
        if total_cost_usd is not None:
            self.total_cost_usd = total_cost_usd
        if tokens_this_phase is not None:
            self.tokens_this_phase = tokens_this_phase
        if model_name is not None:
            self.model_name = model_name

    def add_tokens(self, tokens: int, cost: float = 0.0) -> None:
        """Add tokens and cost to current totals.

        Args:
            tokens: Tokens to add.
            cost: Cost to add.
        """
        self.total_tokens += tokens
        self.total_cost_usd += cost
        self.tokens_this_phase += tokens

    def reset_phase_tokens(self) -> None:
        """Reset phase token counter (called on phase change)."""
        self.tokens_this_phase = 0


__all__ = ["CostTrackerWidget"]
