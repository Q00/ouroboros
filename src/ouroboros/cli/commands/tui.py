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
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help="TUI backend to use: 'python' (default) or 'slt' (native binary).",
        ),
    ] = "python",
) -> None:
    """Launch interactive TUI monitor.

    Starts a terminal UI that shows a list of all sessions found in the
    database. You can then select a session to monitor in real-time.
    """
    if backend == "slt":
        _run_slt_backend(db_path)
        return

    print_info(f"Connecting to database: {db_path}")

    try:
        from ouroboros.tui import OuroborosTUI
    except ImportError as e:
        print_error(
            "TUI dependencies not installed. Install with: pip install 'ouroboros-ai[tui]'",
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


def _run_slt_backend(db_path: Path) -> None:
    import shutil
    import subprocess
    import sys

    bin_path = shutil.which("ouroboros-tui")
    if bin_path is None:
        print_error(
            "ouroboros-tui not found.\n\n"
            "Install options:\n"
            "  Download pre-built binary:\n"
            "    https://github.com/Q00/ouroboros/releases/latest\n\n"
            "  Build from source (requires Rust):\n"
            "    cargo install --path crates/ouroboros-tui",
        )
        raise typer.Exit(1)

    if not sys.stdin.isatty():
        print_error(
            "SLT backend requires an interactive terminal.\n\n"
            "This usually happens when running via 'uvx'. Instead:\n"
            "  1. Run the binary directly:\n"
            "       ouroboros-tui --db-path " + str(db_path) + "\n\n"
            "  2. Or install ouroboros first, then run:\n"
            "       pip install ouroboros-ai\n"
            "       ouroboros monitor --backend slt",
        )
        raise typer.Exit(1)

    args = [bin_path, "--db-path", str(db_path)]
    if os.name == "nt":
        sys.exit(subprocess.call(args))
    else:
        os.execv(bin_path, args)


__all__ = ["app"]
