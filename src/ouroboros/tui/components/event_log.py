"""Event Log - Scrollable event history with filtering.

Displays:
- Scrollable event timeline
- Filterable by event type
- Search capability
- Color-coded by severity
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, RichLog, Static

from ouroboros.tui.events import TUIState


class EventType(Enum):
    """Event types for filtering."""

    ALL = "all"
    SESSION = "session"
    PHASE = "phase"
    AGENT = "agent"
    TOOL = "tool"
    ERROR = "error"
    DEBUG = "debug"


class EventSeverity(Enum):
    """Event severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    DEBUG = "debug"


@dataclass
class LogEntry:
    """A single log entry."""

    timestamp: datetime
    event_type: EventType
    severity: EventSeverity
    source: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class EventLog(Widget):
    """Scrollable event log with filtering.

    Displays:
    - Rich log with syntax highlighting
    - Event type filter buttons
    - Search input
    - Auto-scroll to latest

    Attributes:
        entries: Log entries.
        filter_type: Current event type filter.
        search_query: Current search query.
        auto_scroll: Whether to auto-scroll to new entries.
    """

    DEFAULT_CSS = """
    EventLog {
        height: auto;
        min-height: 15;
        width: 100%;
        padding: 1;
        border: round $accent;
        background: $surface;
    }

    EventLog > .header {
        text-style: bold;
        color: $accent;
        text-align: center;
        margin-bottom: 1;
    }

    EventLog > .controls-row {
        height: 1;
        width: 100%;
        margin-bottom: 1;
    }

    EventLog > .controls-row > .filter-buttons {
        height: 1;
        width: 1fr;
    }

    EventLog > .controls-row > .filter-buttons > .filter-btn {
        width: auto;
        padding: 0 1;
        margin-right: 1;
        border: round $surface;
    }

    EventLog > .controls-row > .filter-buttons > .filter-btn.active {
        background: $primary;
        color: $text;
        text-style: bold;
    }

    EventLog > .controls-row > .search-container {
        width: 1fr;
    }

    EventLog > .controls-row > .search-container > Input {
        width: 100%;
    }

    EventLog > RichLog {
        height: 1fr;
        width: 100%;
        border: solid $surface;
    }

    EventLog > .stats-row {
        height: 1;
        width: 100%;
        margin-top: 1;
        color: $text-muted;
    }

    /* Event severity colors */
    EventLog .severity-info {
        color: $primary;
    }

    EventLog .severity-warning {
        color: $warning;
    }

    EventLog .severity-error {
        color: $error;
        text-style: bold;
    }

    EventLog .severity-debug {
        color: $text-muted;
        text-style: dim;
    }
    """

    entries: reactive[list[LogEntry]] = reactive([], always_update=True)
    filter_type: reactive[EventType] = reactive(EventType.ALL)
    search_query: reactive[str] = reactive("")
    auto_scroll: reactive[bool] = reactive(True)
    max_entries: int = 500

    def __init__(
        self,
        state: TUIState | None = None,
        max_entries: int = 500,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialize event log.

        Args:
            state: TUIState for tracking logs.
            max_entries: Maximum entries to keep.
            name: Widget name.
            id: Widget ID.
            classes: CSS classes.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._state = state
        self.max_entries = max_entries

    def compose(self) -> ComposeResult:
        """Compose the widget layout."""
        yield Static("╔══ EVENT LOG ══╗", classes="header")

        # Controls row
        with Static(classes="controls-row"):
            # Filter buttons
            with Static(classes="filter-buttons"):
                yield Static("[A]ll", classes="filter-btn active", id="filter-all")
                yield Static("[S]ession", classes="filter-btn", id="filter-session")
                yield Static("[P]hase", classes="filter-btn", id="filter-phase")
                yield Static("[T]ool", classes="filter-btn", id="filter-tool")
                yield Static("[E]rror", classes="filter-btn", id="filter-error")

            # Search input
            with Static(classes="search-container"):
                yield Input(placeholder="Search logs...", id="search-input")

        # Rich log for display
        yield RichLog(id="log-display", wrap=True, markup=True)

        # Stats row
        yield Static("", classes="stats-row", id="stats-row")

    def on_mount(self) -> None:
        """Initialize display."""
        self._update_display()

    def _update_display(self) -> None:
        """Update log display based on filter and search."""
        try:
            log = self.query_one("#log-display", RichLog)
        except Exception:
            return

        # Clear and repopulate
        log.clear()

        # Filter entries
        filtered = self._filter_entries()

        # Add entries to log
        for entry in filtered:
            timestamp = entry.timestamp.strftime("%H:%M:%S")
            severity_class = f"severity-{entry.severity.value}"

            # Format the message
            formatted = f"[{severity_class}]{timestamp}[/] [dim]{entry.source}[/] {entry.message}"

            # Add to log
            log.write(formatted)

        # Auto-scroll if enabled
        if self.auto_scroll:
            log.scroll_end()

        # Update stats
        self._update_stats(len(filtered))

    def _filter_entries(self) -> list[LogEntry]:
        """Filter entries based on current filter and search.

        Returns:
            Filtered list of entries.
        """
        filtered = list(self.entries)

        # Apply type filter
        if self.filter_type != EventType.ALL:
            if self.filter_type == EventType.ERROR:
                filtered = [e for e in filtered if e.severity == EventSeverity.ERROR]
            else:
                filtered = [e for e in filtered if e.event_type == self.filter_type]

        # Apply search filter
        if self.search_query:
            query_lower = self.search_query.lower()
            filtered = [
                e for e in filtered
                if query_lower in e.message.lower()
                or query_lower in e.source.lower()
            ]

        return filtered

    def _update_stats(self, count: int) -> None:
        """Update stats display.

        Args:
            count: Number of entries currently displayed.
        """
        try:
            stats = self.query_one("#stats-row", Static)
            total = len(self.entries)
            stats.update(f"Showing {count} of {total} entries")
        except Exception:
            pass

    def watch_entries(self, _: list[LogEntry]) -> None:
        """React to entry list changes."""
        self._update_display()

    def watch_filter_type(self, _: EventType) -> None:
        """React to filter changes."""
        self._update_display()
        self._update_filter_buttons()

    def watch_search_query(self, _: str) -> None:
        """React to search changes."""
        self._update_display()

    def _update_filter_buttons(self) -> None:
        """Update filter button active states."""
        filter_map = {
            EventType.ALL: "filter-all",
            EventType.SESSION: "filter-session",
            EventType.PHASE: "filter-phase",
            EventType.TOOL: "filter-tool",
            EventType.ERROR: "filter-error",
        }

        target_id = filter_map.get(self.filter_type, "filter-all")

        for btn_id in filter_map.values():
            try:
                btn = self.query_one(f"#{btn_id}", Static)
                if btn_id == target_id:
                    btn.add_class("active")
                else:
                    btn.remove_class("active")
            except Exception:
                pass

    def update_from_state(self, state: TUIState) -> None:
        """Update log from TUIState.

        Args:
            state: Current TUI state.
        """
        # Convert state logs to LogEntry format
        new_entries = []
        for log_entry in state.logs:
            # Determine event type and severity
            severity = EventSeverity.INFO
            if log_entry.get("level") == "error":
                severity = EventSeverity.ERROR
            elif log_entry.get("level") == "warning":
                severity = EventSeverity.WARNING
            elif log_entry.get("level") == "debug":
                severity = EventSeverity.DEBUG

            # Determine event type from source
            source = log_entry.get("source", "")
            event_type = EventType.ALL
            if "session" in source.lower():
                event_type = EventType.SESSION
            elif "phase" in source.lower():
                event_type = EventType.PHASE
            elif "tool" in source.lower():
                event_type = EventType.TOOL

            try:
                timestamp = datetime.fromisoformat(log_entry.get("timestamp", datetime.now().isoformat()))
            except Exception:
                timestamp = datetime.now()

            new_entries.append(LogEntry(
                timestamp=timestamp,
                event_type=event_type,
                severity=severity,
                source=source,
                message=log_entry.get("message", ""),
                data=log_entry.get("data", {}),
            ))

        # Merge with existing, avoiding duplicates
        existing_timestamps = {e.timestamp.isoformat() for e in self.entries}
        for entry in new_entries:
            if entry.timestamp.isoformat() not in existing_timestamps:
                self.entries.append(entry)

        # Trim to max
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries:]

    def add_entry(
        self,
        message: str,
        source: str = "system",
        severity: EventSeverity = EventSeverity.INFO,
        event_type: EventType = EventType.ALL,
    ) -> None:
        """Add a log entry.

        Args:
            message: Log message.
            source: Source of the log.
            severity: Severity level.
            event_type: Event type.
        """
        entry = LogEntry(
            timestamp=datetime.now(),
            event_type=event_type,
            severity=severity,
            source=source,
            message=message,
        )

        entries = list(self.entries)
        entries.append(entry)

        # Trim to max
        if len(entries) > self.max_entries:
            entries = entries[-self.max_entries:]

        self.entries = entries

    def set_filter(self, filter_type: EventType) -> None:
        """Set event type filter.

        Args:
            filter_type: Filter to apply.
        """
        self.filter_type = filter_type

    def set_search(self, query: str) -> None:
        """Set search query.

        Args:
            query: Search string.
        """
        self.search_query = query

    def clear(self) -> None:
        """Clear all entries."""
        self.entries = []

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle search input changes.

        Args:
            event: Input changed event.
        """
        if event.input.id == "search-input":
            self.search_query = event.value


__all__ = ["EventLog", "EventSeverity", "EventType", "LogEntry"]
