"""MCP command group for Ouroboros.

Start and manage the MCP (Model Context Protocol) server.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from rich.console import Console
import typer

from ouroboros.cli.formatters.panels import print_error, print_info, print_success

# Separate stderr console for stdio transport (stdout is JSON-RPC channel)
_stderr_console = Console(stderr=True)

app = typer.Typer(
    name="mcp",
    help="MCP (Model Context Protocol) server commands.",
    no_args_is_help=True,
)


async def _run_mcp_server(
    host: str,
    port: int,
    transport: str,
    db_path: str | None = None,
) -> None:
    """Run the MCP server.

    Args:
        host: Host to bind to.
        port: Port to bind to.
        transport: Transport type (stdio or sse).
        db_path: Optional path to EventStore database.
    """
    from ouroboros.mcp.server.adapter import create_ouroboros_server
    from ouroboros.persistence.event_store import EventStore

    # Create EventStore with custom path if provided
    event_store = None
    if db_path:
        event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")

    # Create server with all tools pre-registered via dependency injection.
    # Do NOT re-register OUROBOROS_TOOLS here — create_ouroboros_server already
    # registers handlers with proper dependencies (event_store, llm_adapter, etc.).
    server = create_ouroboros_server(
        name="ouroboros-mcp",
        version="1.0.0",
        event_store=event_store,
    )

    tool_count = len(server.info.tools)

    if transport == "stdio":
        # In stdio mode, stdout is the JSON-RPC channel.
        # All human-readable output must go to stderr.
        _stderr_console.print(f"[green]MCP Server starting on {transport}...[/green]")
        _stderr_console.print(f"[blue]Registered {tool_count} tools[/blue]")
        _stderr_console.print("[blue]Reading from stdin, writing to stdout[/blue]")
        _stderr_console.print("[blue]Press Ctrl+C to stop[/blue]")
    else:
        print_success(f"MCP Server starting on {transport}...")
        print_info(f"Registered {tool_count} tools")
        print_info(f"Listening on {host}:{port}")
        print_info("Press Ctrl+C to stop")

    # Start serving
    await server.serve(transport=transport, host=host, port=port)


@app.command()
def serve(
    host: Annotated[
        str,
        typer.Option(
            "--host",
            "-h",
            help="Host to bind to.",
        ),
    ] = "localhost",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="Port to bind to.",
        ),
    ] = 8080,
    transport: Annotated[
        str,
        typer.Option(
            "--transport",
            "-t",
            help="Transport type: stdio or sse.",
        ),
    ] = "stdio",
    db: Annotated[
        str,
        typer.Option(
            "--db",
            help="Path to EventStore database (default: ~/.ouroboros/ouroboros.db)",
        ),
    ] = "",
) -> None:
    """Start the MCP server.

    Exposes Ouroboros functionality via Model Context Protocol,
    allowing Claude Desktop and other MCP clients to interact
    with Ouroboros.

    Available tools:
    - ouroboros_execute_seed: Execute a seed specification
    - ouroboros_session_status: Get session status
    - ouroboros_query_events: Query event history

    Examples:

        # Start with stdio transport (for Claude Desktop)
        ouroboros mcp serve

        # Start with SSE transport on custom port
        ouroboros mcp serve --transport sse --port 9000
    """
    try:
        db_path = db if db else None
        asyncio.run(_run_mcp_server(host, port, transport, db_path))
    except KeyboardInterrupt:
        print_info("\nMCP Server stopped")
    except ImportError as e:
        print_error(f"MCP dependencies not installed: {e}")
        print_info("Install with: uv add mcp")
        raise typer.Exit(1) from e


@app.command()
def info() -> None:
    """Show MCP server information and available tools."""
    from ouroboros.cli.formatters import console
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    # Create server with all tools pre-registered
    server = create_ouroboros_server(
        name="ouroboros-mcp",
        version="1.0.0",
    )

    server_info = server.info

    console.print()
    console.print("[bold]MCP Server Information[/bold]")
    console.print(f"  Name: {server_info.name}")
    console.print(f"  Version: {server_info.version}")
    console.print()

    console.print("[bold]Capabilities[/bold]")
    console.print(f"  Tools: {server_info.capabilities.tools}")
    console.print(f"  Resources: {server_info.capabilities.resources}")
    console.print(f"  Prompts: {server_info.capabilities.prompts}")
    console.print()

    console.print("[bold]Available Tools[/bold]")
    for tool in server_info.tools:
        console.print(f"  [green]{tool.name}[/green]")
        console.print(f"    {tool.description}")
        if tool.parameters:
            console.print("    Parameters:")
            for param in tool.parameters:
                required = "[red]*[/red]" if param.required else ""
                console.print(f"      - {param.name}{required}: {param.description}")
        console.print()


__all__ = ["app"]
