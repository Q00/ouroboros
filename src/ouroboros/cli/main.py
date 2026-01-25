"""Ouroboros CLI main entry point.

This module defines the main Typer application and registers
all command groups for the Ouroboros CLI.
"""

from typing import Annotated

import typer

from ouroboros import __version__
from ouroboros.cli.commands import config, init, mcp, run, status
from ouroboros.cli.formatters import console

# Create the main Typer app
app = typer.Typer(
    name="ouroboros",
    help="Ouroboros - Self-Improving AI Workflow System",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Register command groups
app.add_typer(init.app, name="init")
app.add_typer(run.app, name="run")
app.add_typer(config.app, name="config")
app.add_typer(status.app, name="status")
app.add_typer(mcp.app, name="mcp")


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        console.print(f"[bold cyan]Ouroboros[/] version [green]{__version__}[/]")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            callback=version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = None,
) -> None:
    """Ouroboros - Self-Improving AI Workflow System.

    A self-improving AI workflow system with 6 phases:
    Big Bang, PAL Router, Execution, Resilience, Evaluation, and Consensus.

    Use [bold cyan]ouroboros COMMAND --help[/] for command-specific help.
    """
    pass


__all__ = ["app", "main"]
