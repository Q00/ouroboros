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
    "Session ID": "session_id",
    "Execution ID": "execution_id",
    "Timestamp": "timestamp",
    "Model": "model",
    "Entrypoint": "entrypoint",
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

            # Deduplicate sessions by aggregate_id (keep first/most recent)
            seen_ids: set[str] = set()
            for event in sessions:
                if event.aggregate_id in seen_ids:
                    continue
                seen_ids.add(event.aggregate_id)

                data = event.data
                row_data = [
                    event.aggregate_id,
                    data.get("execution_id", "[N/A]"),
                    str(event.timestamp),
                    data.get("model_name", "[N/A]"),
                    data.get("entrypoint_name", "[N/A]"),
                ]
                table.add_row(*row_data, key=event.aggregate_id)

        except Exception as e:
            self.notify(f"Failed to load sessions: {e}", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection from DataTable."""
        row_key = event.row_key
        if row_key is None:
            return

        table: DataTable[str] = self.query_one(DataTable)
        row = table.get_row(row_key)
        if not row:
            return

        session_id = str(row[0])
        execution_id = str(row[1]) if row[1] else ""

        self.post_message(self.SessionSelected(session_id, execution_id))
