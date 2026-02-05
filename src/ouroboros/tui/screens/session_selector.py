"""Screen for selecting a session to monitor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

if TYPE_CHECKING:
    from ouroboros.persistence.event_store import EventStore

# Columns for the session table
SESSION_COLUMNS = {
    "#": "index",
    "Goal": "seed_goal",
    "Time": "timestamp",
    "Status": "status",
}


class SessionSelectorScreen(Screen[None]):
    """A screen to display and select from a list of past sessions."""

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
    ]

    class SessionSelected(Message):
        """Message sent when a session is selected."""

        def __init__(self, session_id: str, execution_id: str) -> None:
            self.session_id = session_id
            self.execution_id = execution_id
            super().__init__()

    def __init__(
        self, event_store: EventStore, name: str | None = None, id: str | None = None
    ) -> None:
        """Initialize the session selector screen.

        Args:
            event_store: The event store to query for sessions.
            name: The name of the screen.
            id: The ID of the screen.
        """
        super().__init__(name=name, id=id)
        self._event_store = event_store
        self._session_info: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        """Compose the screen layout."""
        yield Header()
        yield Container(
            Static("Select a session to monitor:", classes="label"),
            DataTable(id="session_table", cursor_type="row"),
            classes="selector-container",
        )
        yield Footer()

    async def on_mount(self) -> None:
        """Set up the session table once the DOM is ready."""
        table = self.query_one(DataTable)
        table.add_columns(*SESSION_COLUMNS.keys())
        await self._load_sessions()

    async def on_screen_resume(self) -> None:
        """Refresh sessions when returning to this screen."""
        await self._load_sessions()

    async def _load_sessions(self) -> None:
        """Load sessions from the event store into the table."""
        table = self.query_one(DataTable)
        table.clear()

        try:
            sessions = await self._event_store.get_all_sessions()
            if not sessions:
                table.add_row("[dim]No sessions found in the database.[/dim]")
                return

            # Deduplicate and collect session info
            self._session_info = {}
            for event in sessions:
                agg_id = event.aggregate_id
                if agg_id not in self._session_info:
                    self._session_info[agg_id] = {
                        "session_id": agg_id,
                        "execution_id": event.data.get("execution_id", ""),
                        "seed_goal": event.data.get("seed_goal", ""),
                        "timestamp": event.timestamp,
                        "status": "started",
                    }
                # Update status based on event type
                if "completed" in event.type:
                    self._session_info[agg_id]["status"] = "completed"
                elif "failed" in event.type:
                    self._session_info[agg_id]["status"] = "failed"
                elif "paused" in event.type:
                    self._session_info[agg_id]["status"] = "paused"

            # Sort by timestamp (newest first)
            sorted_sessions = sorted(
                self._session_info.values(),
                key=lambda x: x["timestamp"],
                reverse=True,
            )

            # Add rows with index
            for idx, info in enumerate(sorted_sessions, 1):
                # Format timestamp to be more readable
                ts = info["timestamp"]
                time_str = ts.strftime("%m/%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]

                # Format goal for display (truncate if too long)
                goal = info["seed_goal"] or "[No goal]"
                goal_display = goal[:50] + "..." if len(goal) > 50 else goal

                # Status with color
                status = info["status"]
                status_display = {
                    "started": "[yellow]running[/yellow]",
                    "completed": "[green]done[/green]",
                    "failed": "[red]failed[/red]",
                    "paused": "[cyan]paused[/cyan]",
                }.get(status, status)

                row_data = [
                    str(idx),
                    goal_display,
                    time_str,
                    status_display,
                ]
                table.add_row(*row_data, key=info["session_id"])

        except Exception as e:
            self.notify(f"Failed to load sessions: {e}", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection from DataTable."""
        row_key = event.row_key
        if row_key is None:
            return

        # row_key is the session_id (set in add_row)
        session_id = str(row_key.value) if hasattr(row_key, "value") else str(row_key)

        # Lookup execution_id from cached session info
        info = self._session_info.get(session_id, {})
        execution_id = info.get("execution_id", "")

        self.post_message(self.SessionSelected(session_id, execution_id))
