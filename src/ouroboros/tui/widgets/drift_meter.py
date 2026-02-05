"""Drift visualization widget.

Displays drift metrics with visual progress bars
and threshold indicators.

Drift components:
- Goal drift: Deviation from seed goal (weight: 0.5)
- Constraint drift: Constraint violations (weight: 0.3)
- Ontology drift: Concept space evolution (weight: 0.2)

Combined drift = (goal * 0.5) + (constraint * 0.3) + (ontology * 0.2)
NFR5 threshold: combined drift <= 0.3
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, ProgressBar, Static


# Drift threshold from NFR5
DRIFT_THRESHOLD = 0.3

# Special value indicating drift has not been measured yet
NOT_MEASURED = -1.0


class DriftBar(Widget):
    """Individual drift component display with progress bar.

    Shows a labeled progress bar for a single drift component.
    Use value=-1.0 (NOT_MEASURED) to display "N/A" state.
    """

    DEFAULT_CSS = """
    DriftBar {
        height: 2;
        width: 100%;
        layout: horizontal;
        margin: 0 0;
    }

    DriftBar > Label {
        width: 12;
        padding-right: 1;
        color: $text-muted;
    }

    DriftBar > ProgressBar {
        width: 1fr;
        padding: 0;
    }

    DriftBar > ProgressBar > .bar--bar {
        color: $primary-darken-2;
    }

    DriftBar > ProgressBar > .bar--complete {
        color: $success;
    }

    DriftBar > .value {
        width: 8;
        text-align: right;
        padding-left: 1;
        color: $text;
    }

    DriftBar.not-measured > .value {
        color: $text-muted;
    }

    DriftBar.warning > ProgressBar > .bar--complete {
        color: $warning;
    }

    DriftBar.warning > .value {
        color: $warning;
    }

    DriftBar.danger > ProgressBar > .bar--complete {
        color: $error;
    }

    DriftBar.danger > .value {
        color: $error;
        text-style: bold;
    }
    """

    value: reactive[float] = reactive(NOT_MEASURED)

    def __init__(
        self,
        label: str,
        value: float = NOT_MEASURED,
        threshold: float = DRIFT_THRESHOLD,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize drift bar.

        Args:
            label: Display label.
            value: Initial drift value (0.0-1.0), or NOT_MEASURED (-1.0) for N/A state.
            threshold: Warning threshold.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        # Internal widget references (must be initialized before reactive props)
        self._progress_bar: ProgressBar | None = None
        self._value_label: Static | None = None
        self._threshold = threshold  # Must be before reactive prop assignment

        super().__init__(name=name, id=id, classes=classes)
        self._label = label
        self.value = value

    @property
    def is_measured(self) -> bool:
        """Check if drift has been measured."""
        return self.value >= 0.0

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Label(self._label)
        self._progress_bar = ProgressBar(total=100, show_eta=False, show_percentage=False)
        yield self._progress_bar
        # Show N/A for unmeasured values
        display_value = f"{self.value:.1%}" if self.is_measured else "[dim]--[/dim]"
        self._value_label = Static(display_value, classes="value")
        yield self._value_label

    def on_mount(self) -> None:
        """Handle mount event."""
        self._update_display()

    def watch_value(self, new_value: float) -> None:
        """React to value changes.

        Args:
            new_value: New drift value.
        """
        self._update_display()

    def _update_display(self) -> None:
        """Update the progress bar and styling."""
        # Clear all state classes first
        self.remove_class("warning")
        self.remove_class("danger")
        self.remove_class("not-measured")

        if not self.is_measured:
            # Not measured yet - show empty bar and N/A
            if self._progress_bar is not None:
                self._progress_bar.progress = 0
            if self._value_label is not None:
                self._value_label.update("[dim]--[/dim]")
            self.add_class("not-measured")
            return

        # Normal measured value
        if self._progress_bar is not None:
            self._progress_bar.progress = self.value * 100

        if self._value_label is not None:
            self._value_label.update(f"{self.value:.1%}")

        # Update styling based on threshold
        if self.value > self._threshold * 1.5:
            self.add_class("danger")
        elif self.value > self._threshold:
            self.add_class("warning")


class DriftMeterWidget(Widget):
    """Widget displaying all drift metrics in compact form.

    Shows combined drift with expandable detail on hover/focus.

    Attributes:
        goal_drift: Goal drift score (0.0-1.0).
        constraint_drift: Constraint drift score (0.0-1.0).
        ontology_drift: Ontology drift score (0.0-1.0).
    """

    DEFAULT_CSS = """
    DriftMeterWidget {
        height: auto;
        width: 100%;
        padding: 0 1;
    }

    DriftMeterWidget > .compact-header {
        height: 1;
        layout: horizontal;
    }

    DriftMeterWidget > .compact-header > .title {
        width: auto;
        color: $text-muted;
    }

    DriftMeterWidget > .compact-header > .drift-values {
        width: 1fr;
        text-align: right;
    }

    DriftMeterWidget > .compact-header > .drift-values.ok {
        color: $success;
    }

    DriftMeterWidget > .compact-header > .drift-values.warning {
        color: $warning;
    }

    DriftMeterWidget > .compact-header > .drift-values.danger {
        color: $error;
        text-style: bold;
    }
    """

    goal_drift: reactive[float] = reactive(NOT_MEASURED)
    constraint_drift: reactive[float] = reactive(NOT_MEASURED)
    ontology_drift: reactive[float] = reactive(NOT_MEASURED)

    def __init__(
        self,
        goal_drift: float = NOT_MEASURED,
        constraint_drift: float = NOT_MEASURED,
        ontology_drift: float = NOT_MEASURED,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize drift meter widget.

        Args:
            goal_drift: Initial goal drift (-1.0 for not measured).
            constraint_drift: Initial constraint drift (-1.0 for not measured).
            ontology_drift: Initial ontology drift (-1.0 for not measured).
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        # Internal widget references (must be initialized before reactive props)
        self._values_label: Static | None = None

        super().__init__(name=name, id=id, classes=classes)
        self.goal_drift = goal_drift
        self.constraint_drift = constraint_drift
        self.ontology_drift = ontology_drift

    @property
    def is_measured(self) -> bool:
        """Check if any drift has been measured."""
        return (
            self.goal_drift >= 0.0
            or self.constraint_drift >= 0.0
            or self.ontology_drift >= 0.0
        )

    @property
    def combined_drift(self) -> float:
        """Calculate combined drift using weighted formula.

        Returns NOT_MEASURED if no drift has been measured yet.
        """
        if not self.is_measured:
            return NOT_MEASURED

        # Use 0.0 for unmeasured components in calculation
        goal = max(0.0, self.goal_drift)
        constraint = max(0.0, self.constraint_drift)
        ontology = max(0.0, self.ontology_drift)

        return goal * 0.5 + constraint * 0.3 + ontology * 0.2

    @property
    def is_acceptable(self) -> bool:
        """Check if drift is within acceptable threshold."""
        if not self.is_measured:
            return True  # Not measured yet = acceptable
        return self.combined_drift <= DRIFT_THRESHOLD

    def compose(self) -> ComposeResult:
        """Compose the widget layout - compact single line."""
        from textual.containers import Horizontal

        with Horizontal(classes="compact-header"):
            yield Static("Drift:", classes="title")
            self._values_label = Static(
                self._format_compact_values(),
                classes=f"drift-values {self._get_status_class()}",
                id="drift-values",
            )
            yield self._values_label

    def _format_compact_values(self) -> str:
        """Format drift values in compact form."""
        if not self.is_measured:
            return "[dim]--[/dim]"

        # Show combined with breakdown: "12% (G:10 C:15 O:8)"
        combined = self.combined_drift
        goal = max(0.0, self.goal_drift)
        constraint = max(0.0, self.constraint_drift)
        ontology = max(0.0, self.ontology_drift)

        return f"{combined:.0%} [dim](G:{goal:.0%} C:{constraint:.0%} O:{ontology:.0%})[/dim]"

    def _get_status_class(self) -> str:
        """Get CSS class based on drift status."""
        if not self.is_measured:
            return "ok"
        if self.combined_drift > DRIFT_THRESHOLD * 1.5:
            return "danger"
        elif self.combined_drift > DRIFT_THRESHOLD:
            return "warning"
        return "ok"

    def _update_display(self) -> None:
        """Update the compact display."""
        if self._values_label is not None:
            self._values_label.update(self._format_compact_values())
            # Update CSS class
            self._values_label.remove_class("ok")
            self._values_label.remove_class("warning")
            self._values_label.remove_class("danger")
            self._values_label.add_class(self._get_status_class())

    def watch_goal_drift(self, new_value: float) -> None:
        """React to goal_drift changes."""
        self._update_display()

    def watch_constraint_drift(self, new_value: float) -> None:
        """React to constraint_drift changes."""
        self._update_display()

    def watch_ontology_drift(self, new_value: float) -> None:
        """React to ontology_drift changes."""
        self._update_display()

    def update_drift(
        self,
        goal_drift: float | None = None,
        constraint_drift: float | None = None,
        ontology_drift: float | None = None,
    ) -> None:
        """Update drift values.

        Args:
            goal_drift: New goal drift value.
            constraint_drift: New constraint drift value.
            ontology_drift: New ontology drift value.
        """
        if goal_drift is not None:
            self.goal_drift = goal_drift
        if constraint_drift is not None:
            self.constraint_drift = constraint_drift
        if ontology_drift is not None:
            self.ontology_drift = ontology_drift


__all__ = ["DriftBar", "DriftMeterWidget", "NOT_MEASURED"]
