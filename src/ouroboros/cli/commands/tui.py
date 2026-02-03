"""TUI command for Ouroboros.

Launch the interactive TUI monitor for real-time workflow monitoring.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer

from ouroboros.cli.formatters.panels import print_error, print_info
from ouroboros.persistence.event_store import EventStore

DEFAULT_DB_PATH = Path(os.path.expanduser("~/.ouroboros/ouroboros.db"))

app = typer.Typer(
    name="tui",
    help="Interactive TUI monitor for Ouroboros workflows.",
    no_args_is_help=False,
)


@app.command(name="monitor")
def monitor_command(
    db_path: Annotated[
        Path,
        typer.Option(
            "--db-path",
            help="Path to the Ouroboros database file to monitor.",
            resolve_path=True,
            show_default=True,
        ),
    ] = DEFAULT_DB_PATH,
) -> None:
    """Launch interactive TUI monitor.

    Starts a terminal UI that shows a list of all sessions found in the
    database. You can then select a session to monitor in real-time.
    """
    print_info(f"Connecting to database: {db_path}")

    try:
        from ouroboros.tui import OuroborosTUI
    except ImportError as e:
        print_error(
            "TUI dependencies not installed. Install with: pip install 'ouroboros[tui]'",
        )
        raise typer.Exit(1) from e

    # Initialize EventStore
    db_path.parent.mkdir(parents=True, exist_ok=True)
    event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")

    # Initialize and run the TUI
    async def init_and_run() -> None:
        await event_store.initialize()
        tui = OuroborosTUI(event_store=event_store)
        await tui.run_async()

    try:
        asyncio.run(init_and_run())
    except Exception as e:
        print_error(f"Failed to run TUI: {e}")
        raise typer.Exit(1) from None


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
) -> None:
    """Interactive TUI monitor for Ouroboros workflows."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(monitor_command)


__all__ = ["app"]
