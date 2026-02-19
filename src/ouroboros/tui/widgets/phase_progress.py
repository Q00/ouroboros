"""Phase progress indicator widget.

Displays the current phase in the Double Diamond cycle
with visual progress indication.

The Double Diamond has four phases:
1. Discover (Diverge) - Explore problem space
2. Define (Converge) - Narrow down approach
3. Design (Diverge) - Create solutions
4. Deliver (Converge) - Implement and validate
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, Static

# Phase configuration with display properties
PHASES = [
    {"name": "discover", "label": "Discover", "type": "diverge"},
    {"name": "define", "label": "Define", "type": "converge"},
    {"name": "design", "label": "Design", "type": "diverge"},
    {"name": "deliver", "label": "Deliver", "type": "converge"},
]


class PhaseIndicator(Static):
    """Individual phase indicator.

    Shows a single phase with active/inactive state
    and diverge/converge styling.
    """

    DEFAULT_CSS = """
    PhaseIndicator {
        width: 7;
        height: 3;
        padding: 0;
        text-align: center;
        content-align: center middle;
        border: solid $surface-darken-1;
        background: $surface-darken-1;
    }

    PhaseIndicator.active {
        background: $primary;
        border: solid $primary;
        text-style: bold;
        color: $text;
    }

    PhaseIndicator.completed {
        background: $success-darken-2;
        border: solid $success-darken-1;
        color: $text;
    }

    PhaseIndicator.diverge {
        color: $secondary;
    }

    PhaseIndicator.converge {
        color: $warning;
    }

    PhaseIndicator.diverge.active,
    PhaseIndicator.converge.active {
        color: $text;
    }
    """

    def __init__(
        self,
        phase_name: str,
        phase_label: str,
        phase_type: str,
        is_active: bool = False,
        is_completed: bool = False,
    ) -> None:
        """Initialize phase indicator.

        Args:
            phase_name: Internal phase name.
            phase_label: Display label.
            phase_type: 'diverge' or 'converge'.
            is_active: Whether this is the current phase.
            is_completed: Whether this phase is completed.
        """
        super().__init__(phase_label)
        self.phase_name = phase_name
        self.phase_type = phase_type
        self._is_active = is_active
        self._is_completed = is_completed
        self.add_class(phase_type)
        if is_active:
            self.add_class("active")
        if is_completed:
            self.add_class("completed")

    def set_active(self, active: bool) -> None:
        """Set active state.

        Args:
            active: Whether this phase is active.
        """
        self._is_active = active
        if active:
            self.add_class("active")
            self.remove_class("completed")
        else:
            self.remove_class("active")

    def set_completed(self, completed: bool) -> None:
        """Set completed state.

        Args:
            completed: Whether this phase is completed.
        """
        self._is_completed = completed
        if completed and not self._is_active:
            self.add_class("completed")
        else:
            self.remove_class("completed")


class PhaseProgressWidget(Widget):
    """Widget showing Double Diamond phase progress.

    Displays all four phases with the current phase highlighted
    and completed phases marked.

    Attributes:
        current_phase: The current active phase name.
        iteration: Current iteration number.
    """

    DEFAULT_CSS = """
    PhaseProgressWidget {
        height: auto;
        width: 100%;
        padding: 0;
    }

    PhaseProgressWidget > Horizontal {
        height: auto;
        width: 100%;
        align: center middle;
    }

    PhaseProgressWidget > .header {
        text-style: bold;
        color: $text;
        margin-bottom: 0;
    }

    PhaseProgressWidget > .iteration-info {
        margin-top: 0;
        color: $text-muted;
    }

    PhaseProgressWidget .arrow {
        width: 2;
        height: 3;
        content-align: center middle;
        color: $text-muted;
    }

    PhaseProgressWidget .diamond-symbol {
        width: 2;
        height: 3;
        content-align: center middle;
        color: $warning;
        text-style: bold;
    }
    """

    current_phase: reactive[str] = reactive("")
    iteration: reactive[int] = reactive(0)

    def __init__(
        self,
        current_phase: str = "",
        iteration: int = 0,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize phase progress widget.

        Args:
            current_phase: Initial current phase.
            iteration: Initial iteration number.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        # Internal widget references (must be initialized before reactive props)
        self._phase_indicators: dict[str, PhaseIndicator] = {}

        super().__init__(name=name, id=id, classes=classes)
        self.current_phase = current_phase
        self.iteration = iteration

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Label("Double Diamond", classes="header")

        with Horizontal():
            # First Diamond: Problem Space
            yield Static(" ◇ ", classes="diamond-symbol")

            for i, phase in enumerate(PHASES[:2]):
                is_active = phase["name"] == self.current_phase
                is_completed = self._is_phase_completed(phase["name"])

                indicator = PhaseIndicator(
                    phase_name=phase["name"],
                    phase_label=phase["label"],
                    phase_type=phase["type"],
                    is_active=is_active,
                    is_completed=is_completed,
                )
                self._phase_indicators[phase["name"]] = indicator
                yield indicator

                if i == 0:
                    yield Static(" → ", classes="arrow")

            # Second Diamond: Solution Space
            yield Static(" ◇ ", classes="diamond-symbol")

            for i, phase in enumerate(PHASES[2:]):
                is_active = phase["name"] == self.current_phase
                is_completed = self._is_phase_completed(phase["name"])

                indicator = PhaseIndicator(
                    phase_name=phase["name"],
                    phase_label=phase["label"],
                    phase_type=phase["type"],
                    is_active=is_active,
                    is_completed=is_completed,
                )
                self._phase_indicators[phase["name"]] = indicator
                yield indicator

                if i == 0:
                    yield Static(" → ", classes="arrow")

    def _is_phase_completed(self, phase_name: str) -> bool:
        """Check if a phase is completed based on current phase.

        Args:
            phase_name: Phase to check.

        Returns:
            True if phase is completed.
        """
        phase_order = [p["name"] for p in PHASES]

        if not self.current_phase:
            return False

        try:
            current_idx = phase_order.index(self.current_phase)
            phase_idx = phase_order.index(phase_name)
            return phase_idx < current_idx
        except ValueError:
            return False

    def watch_current_phase(self, new_phase: str) -> None:
        """React to current_phase changes.

        Args:
            new_phase: New current phase name.
        """
        for phase_name, indicator in self._phase_indicators.items():
            is_active = phase_name == new_phase
            is_completed = self._is_phase_completed(phase_name)
            indicator.set_active(is_active)
            indicator.set_completed(is_completed)

    def watch_iteration(self, new_iteration: int) -> None:
        """React to iteration changes.

        Args:
            new_iteration: New iteration number.
        """
        # Iteration label will be updated on next compose
        # For live updates, we'd need to track the label widget
        pass

    def update_phase(self, phase: str, iteration: int | None = None) -> None:
        """Update the current phase and optionally iteration.

        Args:
            phase: New current phase name.
            iteration: Optional new iteration number.
        """
        self.current_phase = phase
        if iteration is not None:
            self.iteration = iteration


__all__ = ["PhaseIndicator", "PhaseProgressWidget"]
