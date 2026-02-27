"""Screen for selecting a lineage to inspect."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from ouroboros.core.lineage import OntologyLineage
from ouroboros.evolution.projector import LineageProjector

if TYPE_CHECKING:
    from ouroboros.persistence.event_store import EventStore

LINEAGE_COLUMNS = {
    "#": "index",
    "Lineage ID": "lineage_id",
    "Goal": "goal",
    "Time": "timestamp",
    "Status": "status",
    "Gens": "generation_count",
}

STATUS_COLORS = {
    "active": "[yellow]active[/yellow]",
    "converged": "[green]converged[/green]",
    "exhausted": "[red]exhausted[/red]",
    "aborted": "[dim]aborted[/dim]",
}


class LineageSelectorScreen(Screen[None]):
    """A screen to display and select from a list of evolutionary lineages."""

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("r", "refresh", "Refresh"),
    ]

    DEFAULT_CSS = """
    LineageSelectorScreen {
        layout: vertical;
    }

    LineageSelectorScreen .selector-container {
        width: 100%;
        height: 1fr;
        padding: 1 2;
    }

    LineageSelectorScreen .label {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    """

    class LineageSelected(Message):
        """Message sent when a lineage is selected."""

        def __init__(self, lineage_id: str, lineage: OntologyLineage) -> None:
            self.lineage_id = lineage_id
            self.lineage = lineage
            super().__init__()

    def __init__(
        self,
        event_store: EventStore,
        name: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id)
        self._event_store = event_store
        self._lineage_cache: dict[str, OntologyLineage] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("Select a lineage to inspect:", classes="label"),
            DataTable(id="lineage_table", cursor_type="row"),
            classes="selector-container",
        )
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(*LINEAGE_COLUMNS.keys())
        await self._load_lineages()

    async def on_screen_resume(self) -> None:
        await self._load_lineages()

    async def _load_lineages(self) -> None:
        table = self.query_one(DataTable)
        table.clear()

        try:
            creation_events = await self._event_store.get_all_lineages()
            if not creation_events:
                table.add_row("[dim]No lineages found in the database.[/dim]")
                return

            self._lineage_cache.clear()
            projector = LineageProjector()

            for idx, event in enumerate(creation_events, 1):
                lineage_id = event.aggregate_id

                # Replay and project the lineage
                events = await self._event_store.replay_lineage(lineage_id)
                lineage = projector.project(events)

                if lineage is None:
                    continue

                self._lineage_cache[lineage_id] = lineage

                # Format timestamp
                ts = lineage.created_at
                time_str = (
                    ts.strftime("%m/%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
                )

                # Format goal
                goal = lineage.goal or "[No goal]"
                goal_display = goal[:45] + "..." if len(goal) > 45 else goal

                # Format status with color
                status_display = STATUS_COLORS.get(
                    lineage.status.value, lineage.status.value
                )

                # Format lineage ID (short)
                lid_short = lineage_id[:16] + "..." if len(lineage_id) > 16 else lineage_id

                row_data = [
                    str(idx),
                    lid_short,
                    goal_display,
                    time_str,
                    status_display,
                    str(len(lineage.generations)),
                ]
                table.add_row(*row_data, key=lineage_id)

        except Exception as e:
            self.notify(f"Failed to load lineages: {e}", severity="error")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_key = event.row_key
        if row_key is None:
            return

        lineage_id = str(row_key.value) if hasattr(row_key, "value") else str(row_key)
        lineage = self._lineage_cache.get(lineage_id)
        if lineage is not None:
            self.post_message(self.LineageSelected(lineage_id, lineage))

    def action_go_back(self) -> None:
        self.app.pop_screen()

    async def action_refresh(self) -> None:
        await self._load_lineages()
        self.notify("Lineages refreshed")


__all__ = ["LineageSelectorScreen"]
