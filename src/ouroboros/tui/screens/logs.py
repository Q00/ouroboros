"""Log viewer screen.

Provides real-time log viewing with filtering
and search capabilities.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Footer, Input, Label, Static

if TYPE_CHECKING:
    from ouroboros.tui.events import TUIState


# Log level configuration
LOG_LEVELS: dict[str, dict[str, int | str]] = {
    "debug": {"priority": 0, "color": "$text-muted"},
    "info": {"priority": 1, "color": "$text"},
    "warning": {"priority": 2, "color": "$warning"},
    "error": {"priority": 3, "color": "$error"},
}


class LogEntry(Static):
    """Single log entry display."""

    DEFAULT_CSS = """
    LogEntry {
        height: auto;
        width: 100%;
        padding: 0 1;
    }

    LogEntry > Horizontal {
        height: auto;
        width: 100%;
    }

    LogEntry .timestamp {
        width: 12;
        color: $text-muted;
    }

    LogEntry .level {
        width: 8;
        text-align: center;
    }

    LogEntry .level.debug {
        color: $text-muted;
    }

    LogEntry .level.info {
        color: $primary;
    }

    LogEntry .level.warning {
        color: $warning;
    }

    LogEntry .level.error {
        color: $error;
        text-style: bold;
    }

    LogEntry .source {
        width: 20;
        color: $accent;
    }

    LogEntry .message {
        width: 1fr;
    }
    """

    def __init__(
        self,
        timestamp: datetime,
        level: str,
        source: str,
        message: str,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize log entry.

        Args:
            timestamp: When the log was created.
            level: Log level.
            source: Source module.
            message: Log message.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._timestamp = timestamp
        self._level = level.lower()
        self._source = source
        self._message = message

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        with Horizontal():
            yield Static(
                self._timestamp.strftime("%H:%M:%S"),
                classes="timestamp",
            )
            yield Static(
                f"[{self._level.upper()}]",
                classes=f"level {self._level}",
            )
            yield Static(
                self._truncate(self._source, 18),
                classes="source",
            )
            yield Static(self._message, classes="message")

    def _truncate(self, text: str, max_len: int) -> str:
        """Truncate text to max length."""
        if len(text) > max_len:
            return text[: max_len - 2] + ".."
        return text


class LogFilterBar(Static):
    """Filter bar for log viewing."""

    DEFAULT_CSS = """
    LogFilterBar {
        height: 3;
        width: 100%;
        padding: 0 1;
        background: $surface;
    }

    LogFilterBar > Horizontal {
        height: 100%;
        width: 100%;
        align: left middle;
    }

    LogFilterBar Label {
        width: auto;
        padding-right: 1;
    }

    LogFilterBar Input {
        width: 30;
    }

    LogFilterBar .level-filter {
        width: auto;
        padding-left: 2;
    }

    LogFilterBar .level-btn {
        width: auto;
        padding: 0 1;
        margin-right: 1;
    }

    LogFilterBar .level-btn.active {
        background: $primary;
        text-style: bold;
    }
    """

    min_level: reactive[str] = reactive("debug")
    search_text: reactive[str] = reactive("")

    def __init__(
        self,
        min_level: str = "debug",
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize filter bar.

        Args:
            min_level: Minimum log level to show.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self.min_level = min_level
        self._search_input: Input | None = None

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        with Horizontal():
            yield Label("Search:")
            self._search_input = Input(placeholder="Filter logs...")
            yield self._search_input

            with Horizontal(classes="level-filter"):
                yield Label("Level:")
                for level in ["debug", "info", "warning", "error"]:
                    active_class = "active" if level == self.min_level else ""
                    yield Static(
                        level.upper(),
                        classes=f"level-btn {active_class}",
                        id=f"level-{level}",
                    )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle search input changes."""
        self.search_text = event.value


class LogsScreen(Screen[None]):
    """Log viewer screen.

    Shows real-time logs with filtering and search.

    Bindings:
        escape: Return to dashboard
        c: Clear logs
        d: Set level to debug
        i: Set level to info
        w: Set level to warning
        e: Set level to error
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("c", "clear", "Clear"),
        Binding("d", "level_debug", "Debug"),
        Binding("i", "level_info", "Info"),
        Binding("w", "level_warning", "Warning"),
        Binding("e", "level_error", "Error"),
    ]

    DEFAULT_CSS = """
    LogsScreen {
        layout: vertical;
    }

    LogsScreen > Container {
        height: 1fr;
        width: 100%;
    }

    LogsScreen VerticalScroll {
        height: 1fr;
        scrollbar-gutter: stable;
    }

    LogsScreen .empty-message {
        text-align: center;
        color: $text-muted;
        padding: 2;
    }
    """

    min_level: reactive[str] = reactive("debug")
    search_text: reactive[str] = reactive("")

    def __init__(
        self,
        state: TUIState | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize logs screen.

        Args:
            state: Initial TUI state.
            name: Screen name.
            id: Screen ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._state = state
        self._logs: list[dict[str, Any]] = []
        if state and state.logs:
            self._logs = list(state.logs)
        self._filter_bar: LogFilterBar | None = None
        self._log_scroll: VerticalScroll | None = None
        self._entry_counter: int = 0  # Unique ID counter

    def compose(self) -> ComposeResult:
        """Compose the screen layout."""
        yield Static(
            "[bold blue]Ouroboros TUI[/bold blue] — [dim]Logs[/dim]",
            id="screen-header",
        )

        self._filter_bar = LogFilterBar(min_level=self.min_level)
        yield self._filter_bar

        with Container():
            self._log_scroll = VerticalScroll(id="log-container")
            with self._log_scroll:
                filtered = self._get_filtered_logs()
                if filtered:
                    for log in filtered:
                        self._entry_counter += 1
                        yield LogEntry(
                            timestamp=datetime.fromisoformat(log["timestamp"]),
                            level=log.get("level", "info"),
                            source=log.get("source", "unknown"),
                            message=log.get("message", ""),
                            id=f"log-entry-{self._entry_counter}",
                        )
                else:
                    yield Static(
                        "No logs to display",
                        classes="empty-message",
                        id="empty-logs-message",
                    )

        yield Footer()

    def on_show(self) -> None:
        """Called when the screen is shown - refresh logs display."""
        self._refresh_logs()

    def _refresh_logs(self) -> None:
        """Refresh log display after filter change or new log."""
        try:
            container = self.query_one("#log-container", VerticalScroll)
        except Exception:
            return

        # Remove all children and re-add filtered logs
        container.remove_children()
        filtered = self._get_filtered_logs()

        if filtered:
            for log in filtered:
                self._entry_counter += 1
                container.mount(
                    LogEntry(
                        timestamp=datetime.fromisoformat(log["timestamp"]),
                        level=log.get("level", "info"),
                        source=log.get("source", "unknown"),
                        message=log.get("message", ""),
                        id=f"log-entry-{self._entry_counter}",
                    )
                )
        else:
            self._entry_counter += 1
            container.mount(
                Static(
                    "No logs to display",
                    classes="empty-message",
                    id=f"empty-logs-{self._entry_counter}",
                )
            )

    def _get_filtered_logs(self) -> list[dict[str, Any]]:
        """Get logs filtered by level and search."""
        min_priority = int(LOG_LEVELS.get(self.min_level, {}).get("priority", 0))

        # Use state logs if available, otherwise use local logs
        if self._state:
            source_logs = self._state.logs
        else:
            source_logs = self._logs

        filtered = []
        for log in source_logs:
            level = log.get("level", "info").lower()
            level_priority = int(LOG_LEVELS.get(level, {}).get("priority", 1))

            # Filter by level
            if level_priority < min_priority:
                continue

            # Filter by search text
            if self.search_text:
                search_lower = self.search_text.lower()
                message = log.get("message", "").lower()
                source = log.get("source", "").lower()
                if search_lower not in message and search_lower not in source:
                    continue

            filtered.append(log)

        return filtered

    def watch_min_level(self, new_level: str) -> None:
        """React to level filter changes."""
        self._refresh_logs()

    def watch_search_text(self, new_text: str) -> None:
        """React to search text changes."""
        self._refresh_logs()

    def action_back(self) -> None:
        """Return to dashboard."""
        self.app.pop_screen()

    def action_clear(self) -> None:
        """Clear all logs."""
        self._logs.clear()
        self._refresh_logs()

    def action_level_debug(self) -> None:
        """Set level filter to debug."""
        self.min_level = "debug"

    def action_level_info(self) -> None:
        """Set level filter to info."""
        self.min_level = "info"

    def action_level_warning(self) -> None:
        """Set level filter to warning."""
        self.min_level = "warning"

    def action_level_error(self) -> None:
        """Set level filter to error."""
        self.min_level = "error"

    def add_log(
        self,
        level: str,
        source: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Add a log entry.

        Args:
            level: Log level.
            source: Source module.
            message: Log message.
            data: Additional data.
        """
        self._logs.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": level,
                "source": source,
                "message": message,
                "data": data or {},
            }
        )

        # Keep only last 500 logs
        if len(self._logs) > 500:
            self._logs = self._logs[-500:]

        # Refresh display and auto-scroll to bottom
        self._refresh_logs()
        if self._log_scroll is not None:
            self._log_scroll.scroll_end(animate=False)

    def update_state(self, state: TUIState) -> None:
        """Update the entire state.

        Args:
            state: New TUI state.
        """
        self._state = state
        if state.logs:
            self._logs = list(state.logs)
        self._refresh_logs()


__all__ = ["LogEntry", "LogFilterBar", "LogsScreen"]
