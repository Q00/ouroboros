"""Runtime-specific completion guidance for ``ouroboros setup``."""

from __future__ import annotations

import importlib.util

from rich.console import Console

_CONSOLE_COMMAND_MODULE = "ouroboros.cli.commands.gui"


def console_command_available() -> bool:
    """Whether this install ships ``ouroboros gui``; earlier releases do not."""
    try:
        return importlib.util.find_spec(_CONSOLE_COMMAND_MODULE) is not None
    except (ImportError, ValueError):
        return False


def print_setup_completion(console: Console, runtime: str) -> None:
    """Print next steps that are executable from the selected runtime surface."""
    console.print("\n[bold green]Setup complete![/bold green]")
    console.print("\n[dim]Next steps:[/dim]")
    if runtime in {"host", "host_dispatch"}:
        console.print("  In your MCP host chat, type: ooo run")
    else:
        console.print('  ouroboros init start "your idea here"')
        console.print("  ouroboros run workflow seed.yaml")
    if console_command_available():
        console.print("  ouroboros gui   [dim]# open the Ouroboros console in your browser[/dim]")
    console.print()
