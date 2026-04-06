"""MCP command group for Ouroboros.

Start and manage the MCP (Model Context Protocol) server.
"""

from __future__ import annotations

import asyncio
from enum import Enum
import os
from pathlib import Path
from typing import Annotated

from rich.console import Console
import typer

from ouroboros.cli.formatters.panels import print_error, print_info, print_success

# PID file for detecting stale instances
_PID_DIR = Path.home() / ".ouroboros"
_PID_FILE = _PID_DIR / "mcp-server.pid"

# Separate stderr console for stdio transport (stdout is JSON-RPC channel)
_stderr_console = Console(stderr=True)


class AgentRuntimeBackend(str, Enum):  # noqa: UP042
    """Supported orchestrator runtime backends for MCP commands."""

    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"


class LLMBackend(str, Enum):  # noqa: UP042
    """Supported LLM-only backends for MCP commands."""

    CLAUDE_CODE = "claude_code"
    LITELLM = "litellm"
    CODEX = "codex"
    OPENCODE = "opencode"


def _write_pid_file() -> bool:
    """Write current PID to file for stale instance detection.

    Returns:
        True if the PID file was written successfully, False otherwise.
    """
    try:
        _PID_DIR.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return False
    return True


def _cleanup_pid_file() -> None:
    """Remove PID file on clean shutdown."""
    try:
        _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _check_stale_instance() -> bool:
    """Check for and clean up stale MCP server instances.

    Returns:
        True if a stale instance was cleaned up.
    """
    try:
        pid_exists = _PID_FILE.exists()
    except OSError:
        return False

    if not pid_exists:
        return False

    try:
        old_pid = int(_PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        _cleanup_pid_file()
        return True

    try:
        os.kill(old_pid, 0)  # Signal 0 = check existence
        return False  # Process is alive
    except ProcessLookupError:
        _cleanup_pid_file()
        return True
    except PermissionError:
        return False  # Process exists but we can't signal it
    except OSError:
        # Windows: os.kill(pid, 0) raises OSError (WinError 87)
        # since signal 0 is not supported. Treat as stale.
        _cleanup_pid_file()
        return True


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
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
) -> None:
    """Run the MCP server.

    Args:
        host: Host to bind to.
        port: Port to bind to.
        transport: Transport type (stdio or sse).
        db_path: Optional path to EventStore database.
        runtime_backend: Optional orchestrator runtime backend override.
        llm_backend: Optional LLM-only backend override.
    """
    from ouroboros.mcp.server.adapter import create_ouroboros_server, validate_transport
    from ouroboros.orchestrator.session import SessionRepository
    from ouroboros.persistence.event_store import EventStore

    # Validate transport early, before any expensive startup work
    try:
        transport = validate_transport(transport)
    except ValueError:
        print_error(f"Invalid transport {transport!r}. Must be 'stdio' or 'sse'.")
        raise typer.Exit(code=1)

    # Create EventStore with custom path if provided
    if db_path:
        event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
    else:
        event_store = EventStore()

    # Auto-cancel orphaned sessions on startup.
    # Sessions left in RUNNING/PAUSED state for >1 hour are considered orphaned
    # (e.g., from a previous crash). Cancel them before accepting new requests.
    # NOTE: find_orphaned_sessions now checks for active runtime processes first,
    # so sessions with live claude/codex agents won't be cancelled even if stale.
    try:
        await event_store.initialize()
        repo = SessionRepository(event_store)
        cancelled = await repo.cancel_orphaned_sessions()
        if cancelled:
            _stderr_console.print(
                f"[yellow]Auto-cancelled {len(cancelled)} orphaned session(s)[/yellow]"
            )
    except Exception as e:
        # Auto-cleanup is best-effort — don't prevent server from starting
        _stderr_console.print(f"[yellow]Warning: auto-cleanup failed: {e}[/yellow]")

    # Auto-discover and connect MCP bridge for server-to-server communication
    from ouroboros.mcp.bridge import create_bridge_from_env

    mcp_bridge = create_bridge_from_env(cwd=Path.cwd())
    if mcp_bridge is not None:
        try:
            results = await mcp_bridge.connect()
            connected = sum(1 for r in results.values() if r.is_ok)
            _stderr_console.print(
                f"[blue]MCP Bridge: {connected}/{len(results)} upstream server(s) connected[/blue]"
            )
        except Exception as e:
            _stderr_console.print(f"[yellow]MCP Bridge connection failed: {e}[/yellow]")
            mcp_bridge = None

    # Create server with all tools pre-registered via dependency injection.
    # Do NOT re-register OUROBOROS_TOOLS here — create_ouroboros_server already
    # registers handlers with proper dependencies (event_store, llm_adapter, etc.).
    server = create_ouroboros_server(
        name="ouroboros-mcp",
        version="1.0.0",
        event_store=event_store,
        runtime_backend=runtime_backend,
        llm_backend=llm_backend,
        mcp_bridge=mcp_bridge,
    )

    tool_count = len(server.info.tools)

    # Detect Codex seatbelt sandbox and warn about network restrictions.
    _sandbox_network_disabled = os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1"
    _console_out = _stderr_console if transport == "stdio" else Console()

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

    if _sandbox_network_disabled:
        _console_out.print(
            "[dim]Note: CODEX_SANDBOX_NETWORK_DISABLED=1 detected. "
            "MCP-spawned runtimes usually retain network access. "
            "If agent tasks fail with network errors, try: "
            "--sandbox danger-full-access[/dim]"
        )

    # Manage PID file for stale instance detection
    if _check_stale_instance():
        if transport == "stdio":
            _stderr_console.print("[yellow]Cleaned up stale MCP server PID file[/yellow]")
        else:
            print_info("Cleaned up stale MCP server PID file")

    _write_pid_file()

    # Start serving
    try:
        await server.serve(transport=transport, host=host, port=port)
    finally:
        _cleanup_pid_file()


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
    runtime: Annotated[
        AgentRuntimeBackend | None,
        typer.Option(
            "--runtime",
            help="Agent runtime backend for orchestrator-driven tools (claude, codex, or opencode).",
            case_sensitive=False,
        ),
    ] = None,
    llm_backend: Annotated[
        LLMBackend | None,
        typer.Option(
            "--llm-backend",
            help=(
                "LLM backend for interview/seed/evaluation tools (claude_code, litellm, codex, or opencode)."
            ),
            case_sensitive=False,
        ),
    ] = None,
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

        # Start with OpenCode runtime
        ouroboros mcp serve --runtime opencode

        # Use Codex CLI for LLM-only tools as well
        ouroboros mcp serve --runtime codex --llm-backend codex

    """
    # Guard: prevent recursive MCP server spawning.
    # When ouroboros spawns a runtime (Codex/Claude/OpenCode), the child process
    # inherits this env var. If that runtime's MCP config tries to spawn another
    # ouroboros server, the nested instance exits cleanly instead of creating a
    # process tree explosion.
    if os.environ.get("_OUROBOROS_NESTED"):
        _stderr_console.print("[dim]Nested ouroboros MCP server detected — exiting cleanly[/dim]")
        raise typer.Exit(0)
    os.environ["_OUROBOROS_NESTED"] = "1"

    try:
        db_path = db if db else None
        asyncio.run(
            _run_mcp_server(
                host,
                port,
                transport,
                db_path,
                runtime.value if runtime else None,
                llm_backend.value if llm_backend else None,
            )
        )
    except KeyboardInterrupt:
        print_info("\nMCP Server stopped")
    except ImportError as e:
        print_error(f"MCP dependencies not installed: {e}")
        print_info("Install with: uv add mcp")
        raise typer.Exit(1) from e
    except OSError as e:
        print_error(f"MCP Server failed to start: {e}")
        print_info(
            "If this keeps happening, try:\n"
            "  1. Check if another MCP server is running: cat ~/.ouroboros/mcp-server.pid\n"
            "  2. Kill stale process: kill $(cat ~/.ouroboros/mcp-server.pid)\n"
            "  3. Remove stale PID: rm ~/.ouroboros/mcp-server.pid\n"
            "  4. Restart your MCP client"
        )
        raise typer.Exit(1) from e


@app.command()
def info(
    runtime: Annotated[
        AgentRuntimeBackend | None,
        typer.Option(
            "--runtime",
            help="Agent runtime backend for orchestrator-driven tools (claude, codex, or opencode).",
            case_sensitive=False,
        ),
    ] = None,
    llm_backend: Annotated[
        LLMBackend | None,
        typer.Option(
            "--llm-backend",
            help=(
                "LLM backend for interview/seed/evaluation tools (claude_code, litellm, codex, or opencode)."
            ),
            case_sensitive=False,
        ),
    ] = None,
) -> None:
    """Show MCP server information and available tools."""
    from ouroboros.cli.formatters import console
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    # Create server with all tools pre-registered
    server = create_ouroboros_server(
        name="ouroboros-mcp",
        version="1.0.0",
        runtime_backend=runtime.value if runtime else None,
        llm_backend=llm_backend.value if llm_backend else None,
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
