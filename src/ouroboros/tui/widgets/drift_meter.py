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


class DriftBar(Widget):
    """Individual drift component display with progress bar.

    Shows a labeled progress bar for a single drift component.
    """

    DEFAULT_CSS = """
    DriftBar {
        height: 2;
        width: 100%;
        layout: horizontal;
    }

    DriftBar > Label {
        width: 15;
        padding-right: 1;
    }

    DriftBar > ProgressBar {
        width: 1fr;
    }

    DriftBar > .value {
        width: 8;
        text-align: right;
        padding-left: 1;
    }

    DriftBar.warning > ProgressBar > .bar--complete {
        color: $warning;
    }

    DriftBar.danger > ProgressBar > .bar--complete {
        color: $error;
    }
    """

    value: reactive[float] = reactive(0.0)

    def __init__(
        self,
        label: str,
        value: float = 0.0,
        threshold: float = DRIFT_THRESHOLD,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize drift bar.

        Args:
            label: Display label.
            value: Initial drift value (0.0-1.0).
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

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Label(self._label)
        self._progress_bar = ProgressBar(total=100, show_eta=False, show_percentage=False)
        yield self._progress_bar
        self._value_label = Static(f"{self.value:.1%}", classes="value")
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
        if self._progress_bar is not None:
            self._progress_bar.progress = self.value * 100

        if self._value_label is not None:
            self._value_label.update(f"{self.value:.1%}")

        # Update styling based on threshold
        self.remove_class("warning")
        self.remove_class("danger")

        if self.value > self._threshold * 1.5:
            self.add_class("danger")
        elif self.value > self._threshold:
            self.add_class("warning")


class DriftMeterWidget(Widget):
    """Widget displaying all drift metrics.

    Shows individual drift components and combined drift
    with visual indicators and threshold warnings.

    Attributes:
        goal_drift: Goal drift score (0.0-1.0).
        constraint_drift: Constraint drift score (0.0-1.0).
        ontology_drift: Ontology drift score (0.0-1.0).
    """

    DEFAULT_CSS = """
    DriftMeterWidget {
        height: auto;
        width: 100%;
        padding: 1;
        border: solid $surface;
    }

    DriftMeterWidget > .header {
        text-align: center;
        text-style: bold;
    }

    DriftMeterWidget > .explanation {
        text-align: center;
        margin-bottom: 1;
    }

    DriftMeterWidget > .combined {
        margin-top: 1;
        padding-top: 1;
        border-top: solid $surface;
    }

    DriftMeterWidget > .status {
        text-align: center;
        margin-top: 1;
    }

    DriftMeterWidget > .status.acceptable {
        color: $success;
    }

    DriftMeterWidget > .status.exceeded {
        color: $error;
        text-style: bold;
    }
    """

    goal_drift: reactive[float] = reactive(0.0)
    constraint_drift: reactive[float] = reactive(0.0)
    ontology_drift: reactive[float] = reactive(0.0)

    def __init__(
        self,
        goal_drift: float = 0.0,
        constraint_drift: float = 0.0,
        ontology_drift: float = 0.0,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize drift meter widget.

        Args:
            goal_drift: Initial goal drift.
            constraint_drift: Initial constraint drift.
            ontology_drift: Initial ontology drift.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        # Internal widget references (must be initialized before reactive props)
        self._goal_bar: DriftBar | None = None
        self._constraint_bar: DriftBar | None = None
        self._ontology_bar: DriftBar | None = None
        self._combined_bar: DriftBar | None = None
        self._status_label: Static | None = None

        super().__init__(name=name, id=id, classes=classes)
        self.goal_drift = goal_drift
        self.constraint_drift = constraint_drift
        self.ontology_drift = ontology_drift

    @property
    def combined_drift(self) -> float:
        """Calculate combined drift using weighted formula."""
        return (
            self.goal_drift * 0.5
            + self.constraint_drift * 0.3
            + self.ontology_drift * 0.2
        )

    @property
    def is_acceptable(self) -> bool:
        """Check if drift is within acceptable threshold."""
        return self.combined_drift <= DRIFT_THRESHOLD

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Label("Drift Metrics [dim](deviation from goal)[/dim]", classes="header")

        # Brief explanation
        yield Static(
            "[dim]Lower is better (0% = perfect, 30%+ = warning)[/dim]",
            classes="explanation",
        )

        self._goal_bar = DriftBar(
            "Goal",
            self.goal_drift,
            id="goal-drift",
        )
        yield self._goal_bar

        self._constraint_bar = DriftBar(
            "Constraint",
            self.constraint_drift,
            id="constraint-drift",
        )
        yield self._constraint_bar

        self._ontology_bar = DriftBar(
            "Ontology",
            self.ontology_drift,
            id="ontology-drift",
        )
        yield self._ontology_bar

        self._combined_bar = DriftBar(
            "Combined",
            self.combined_drift,
            id="combined-drift",
            classes="combined",
        )
        yield self._combined_bar

        status_text = self._get_status_text()
        status_class = "acceptable" if self.is_acceptable else "exceeded"
        self._status_label = Static(status_text, classes=f"status {status_class}")
        yield self._status_label

    def _get_status_text(self) -> str:
        """Get status text based on drift."""
        if self.is_acceptable:
            return f"✓ OK (within {DRIFT_THRESHOLD:.0%} threshold)"
        else:
            exceeded_by = self.combined_drift - DRIFT_THRESHOLD
            return f"⚠ Warning: exceeded by {exceeded_by:.1%}"

    def _update_bars(self) -> None:
        """Update all drift bars."""
        if self._goal_bar is not None:
            self._goal_bar.value = self.goal_drift
        if self._constraint_bar is not None:
            self._constraint_bar.value = self.constraint_drift
        if self._ontology_bar is not None:
            self._ontology_bar.value = self.ontology_drift
        if self._combined_bar is not None:
            self._combined_bar.value = self.combined_drift
        if self._status_label is not None:
            self._status_label.update(self._get_status_text())
            self._status_label.remove_class("acceptable")
            self._status_label.remove_class("exceeded")
            status_class = "acceptable" if self.is_acceptable else "exceeded"
            self._status_label.add_class(status_class)

    def watch_goal_drift(self, new_value: float) -> None:
        """React to goal_drift changes."""
        self._update_bars()

    def watch_constraint_drift(self, new_value: float) -> None:
        """React to constraint_drift changes."""
        self._update_bars()

    def watch_ontology_drift(self, new_value: float) -> None:
        """React to ontology_drift changes."""
        self._update_bars()

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


__all__ = ["DriftBar", "DriftMeterWidget"]
