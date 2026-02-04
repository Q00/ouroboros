"""Debug/inspect screen.

Provides detailed debugging and inspection capabilities:
- Raw event data viewer
- State inspector
- Configuration display
- Performance metrics
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Label, Static, TabbedContent, TabPane

if TYPE_CHECKING:
    from ouroboros.tui.events import TUIState


def _load_config_as_dict() -> dict[str, Any]:
    """Load Ouroboros configuration as a dictionary.

    Returns:
        Configuration dictionary or error message dict if loading fails.
    """
    try:
        from ouroboros.config import config_exists, load_config

        if not config_exists():
            return {"error": "Config not found. Run 'ouroboros init' to create."}

        config = load_config()
        # Convert pydantic model to dict
        return config.model_dump()
    except ImportError:
        return {"error": "Config module not available"}
    except Exception as e:
        return {"error": f"Failed to load config: {e}"}


class JsonViewer(Static):
    """Widget for displaying formatted JSON data."""

    DEFAULT_CSS = """
    JsonViewer {
        height: auto;
        width: 100%;
        padding: 1;
        background: $surface;
    }

    JsonViewer > .json-content {
        width: 100%;
    }

    JsonViewer > .empty {
        color: $text-muted;
        text-style: italic;
    }
    """

    def __init__(
        self,
        data: dict[str, Any] | list[Any] | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize JSON viewer.

        Args:
            data: Data to display as JSON.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._data = data

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        if self._data is None:
            yield Static("No data available", classes="empty")
        elif isinstance(self._data, dict) and not self._data:
            yield Static("(empty object - no data yet)", classes="empty")
        elif isinstance(self._data, list) and not self._data:
            yield Static("(empty list - no events yet)", classes="empty")
        else:
            try:
                formatted = json.dumps(self._data, indent=2, default=str)
                # Truncate if too long
                if len(formatted) > 2000:
                    formatted = formatted[:2000] + "\n... (truncated)"
                yield Static(formatted, classes="json-content")
            except Exception as e:
                yield Static(f"Error formatting JSON: {e}", classes="empty")

    def update_data(self, data: dict[str, Any] | list[Any] | None) -> None:
        """Update the displayed data.

        Args:
            data: New data to display.
        """
        self._data = data
        self.refresh(recompose=True)


class StateInspector(Static):
    """Widget for inspecting TUI state."""

    DEFAULT_CSS = """
    StateInspector {
        height: auto;
        width: 100%;
        padding: 1;
    }

    StateInspector > .section {
        margin-bottom: 1;
    }

    StateInspector > .section > .section-header {
        text-style: bold;
        border-bottom: solid $surface;
        margin-bottom: 1;
    }

    StateInspector > .section > .field {
        height: 1;
    }

    StateInspector > .section > .field > Label {
        width: 20;
        color: $text-muted;
    }

    StateInspector > .section > .field > .value {
        width: 1fr;
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
        """Initialize state inspector.

        Args:
            state: TUI state to inspect.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._state = state

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        if self._state is None:
            yield Static("No state available")
            return

        # Execution section
        with Vertical(classes="section"):
            yield Label("Execution", classes="section-header")
            yield from self._field("Execution ID", self._state.execution_id or "None")
            yield from self._field("Session ID", self._state.session_id or "None")
            yield from self._field("Status", self._state.status)
            yield from self._field("Is Paused", str(self._state.is_paused))

        # Phase section
        with Vertical(classes="section"):
            yield Label("Phase", classes="section-header")
            yield from self._field("Current Phase", self._state.current_phase or "None")
            yield from self._field("Iteration", str(self._state.iteration))

        # Drift section
        with Vertical(classes="section"):
            yield Label("Drift", classes="section-header")
            yield from self._field("Goal", f"{self._state.goal_drift:.3f}")
            yield from self._field("Constraint", f"{self._state.constraint_drift:.3f}")
            yield from self._field("Ontology", f"{self._state.ontology_drift:.3f}")
            yield from self._field("Combined", f"{self._state.combined_drift:.3f}")

        # Cost section
        with Vertical(classes="section"):
            yield Label("Cost", classes="section-header")
            yield from self._field("Total Tokens", str(self._state.total_tokens))
            yield from self._field("Total Cost", f"${self._state.total_cost_usd:.4f}")

        # Logs section
        with Vertical(classes="section"):
            yield Label("Logs", classes="section-header")
            yield from self._field("Log Count", str(len(self._state.logs)))
            yield from self._field("Max Logs", str(self._state.max_logs))

    def _field(self, label: str, value: str) -> ComposeResult:
        """Create a field display."""
        with Horizontal(classes="field"):
            yield Label(f"{label}:")
            yield Static(value, classes="value")

    def update_state(self, state: TUIState) -> None:
        """Update the inspected state.

        Args:
            state: New state to inspect.
        """
        self._state = state
        self.refresh(recompose=True)


class DebugScreen(Screen[None]):
    """Debug/inspect screen.

    Provides debugging and inspection tools.

    Bindings:
        escape: Return to dashboard
        r: Refresh view
        s: Show state
        e: Show events
        c: Show config
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]

    DEFAULT_CSS = """
    DebugScreen {
        layout: vertical;
    }

    DebugScreen > #screen-header {
        dock: top;
        height: 1;
        width: 100%;
        background: $primary;
        text-align: center;
        padding: 0 1;
    }

    DebugScreen > Container {
        height: 1fr;
        width: 100%;
        padding: 1;
    }

    DebugScreen TabbedContent {
        height: 1fr;
    }

    DebugScreen TabPane {
        padding: 1;
    }

    DebugScreen VerticalScroll {
        height: 1fr;
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
        """Initialize debug screen.

        Args:
            state: Initial TUI state.
            name: Screen name.
            id: Screen ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._state = state
        self._raw_events: list[dict[str, Any]] = []
        self._config: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        """Compose the screen layout."""
        yield Static(
            "[bold blue]Ouroboros TUI[/bold blue] — [dim]Debug[/dim]",
            id="screen-header",
        )

        # Load config if not already loaded
        if not self._config:
            self._config = _load_config_as_dict()

        with Container():
            with TabbedContent():
                with TabPane("State", id="tab-state"):
                    with VerticalScroll():
                        yield StateInspector(self._state)

                with TabPane("Events", id="tab-events"):
                    with VerticalScroll():
                        yield JsonViewer(self._raw_events)

                with TabPane("AC Tree", id="tab-tree"):
                    with VerticalScroll():
                        tree_data = self._state.ac_tree if self._state else {}
                        yield JsonViewer(tree_data)

                with TabPane("Config", id="tab-config"):
                    with VerticalScroll():
                        yield JsonViewer(self._config)

        yield Footer()

    def action_back(self) -> None:
        """Return to dashboard."""
        self.app.pop_screen()

    def action_refresh(self) -> None:
        """Refresh the view."""
        # Reload config on refresh
        self._config = _load_config_as_dict()
        self.refresh(recompose=True)

    def reload_config(self) -> None:
        """Reload configuration from disk."""
        self._config = _load_config_as_dict()
        self.refresh(recompose=True)

    def add_raw_event(self, event: dict[str, Any]) -> None:
        """Add a raw event for inspection.

        Args:
            event: Event data dictionary.
        """
        self._raw_events.append(event)
        # Keep only last 50 events
        if len(self._raw_events) > 50:
            self._raw_events = self._raw_events[-50:]

    def set_config(self, config: dict[str, Any]) -> None:
        """Set configuration for display.

        Args:
            config: Configuration dictionary.
        """
        self._config = config
        self.refresh(recompose=True)

    def update_state(self, state: TUIState) -> None:
        """Update the entire state.

        Args:
            state: New TUI state.
        """
        self._state = state
        self.refresh(recompose=True)


__all__ = ["DebugScreen", "JsonViewer", "StateInspector"]
