"""Runtime-specific completion guidance for ``ouroboros setup``."""

from __future__ import annotations

from rich.console import Console


def print_setup_completion(console: Console, runtime: str) -> None:
    """Print next steps that are executable from the selected runtime surface."""
    console.print("\n[bold green]Setup complete![/bold green]")
    console.print("\n[dim]Next steps:[/dim]")
    if runtime in {"host", "host_dispatch"}:
        console.print("  In your MCP host chat, type: ooo run\n")
        return
    console.print('  ouroboros init start "your idea here"')
    console.print("  ouroboros run workflow seed.yaml\n")
