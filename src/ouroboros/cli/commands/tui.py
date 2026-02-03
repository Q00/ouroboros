"""TUI command for Ouroboros.

Launch the interactive TUI monitor for real-time workflow monitoring.
"""

from __future__ import annotations

import asyncio
import os
from typing import Annotated

import typer

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info

app = typer.Typer(
    name="tui",
    help="Interactive TUI monitor for Ouroboros workflows.",
    no_args_is_help=False,
)


@app.command()
def monitor(
    execution_id: Annotated[
        str | None,
        typer.Option(
            "--execution-id",
            "-e",
            help="Execution ID to monitor.",
        ),
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session-id",
            "-s",
            help="Session ID to monitor.",
        ),
    ] = None,
) -> None:
    """Launch interactive TUI monitor.

    Start the terminal UI for real-time monitoring of Ouroboros
    workflow executions.

    Examples:

        # Launch TUI without monitoring a specific execution
        ouroboros tui monitor

        # Monitor a specific execution
        ouroboros tui monitor --execution-id exec_abc123

        # Monitor a specific session
        ouroboros tui monitor --session-id sess_xyz789
    """
    try:
        from ouroboros.tui import OuroborosTUI
    except ImportError as e:
        print_error(
            f"TUI dependencies not installed. Install with: pip install ouroboros[tui]\n"
            f"Error: {e}"
        )
        raise typer.Exit(1) from e

    if execution_id:
        print_info(f"Monitoring execution: {execution_id}")
    elif session_id:
        print_info(f"Monitoring session: {session_id}")
    else:
        print_info("Starting TUI monitor...")

    # Initialize EventStore with same path as runner
    from ouroboros.persistence.event_store import EventStore

    db_path = os.path.expanduser("~/.ouroboros/ouroboros.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")

    # Initialize event store before running TUI
    async def init_and_run() -> None:
        await event_store.initialize()
        tui = OuroborosTUI(event_store=event_store, execution_id=execution_id)
        if session_id:
            tui.set_execution(execution_id or "", session_id)
        await tui.run_async()

    asyncio.run(init_and_run())


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
) -> None:
    """Interactive TUI monitor for Ouroboros workflows.

    Launch a terminal-based user interface for real-time monitoring
    of workflow executions, including:

    - Phase progress visualization (Double Diamond)
    - Drift metrics monitoring
    - Cost/token tracking
    - AC tree visualization
    - Log viewer

    Use keyboard shortcuts to navigate:
    - 1-4: Switch screens
    - p/r: Pause/Resume execution
    - q: Quit
    """
    # If no subcommand, run monitor directly
    if ctx.invoked_subcommand is None:
        monitor(execution_id=None, session_id=None)


__all__ = ["app"]
