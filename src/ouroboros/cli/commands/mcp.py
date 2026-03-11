"""MCP command group for Ouroboros.

Start and manage the MCP (Model Context Protocol) server.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
from pathlib import Path
import sys
import threading
import time
from typing import Annotated

from rich.console import Console
import typer

from ouroboros.cli.formatters.panels import print_error, print_info, print_success

# PID file for detecting stale instances
_PID_DIR = Path.home() / ".ouroboros"
_PID_FILE = _PID_DIR / "mcp-server.pid"

# Separate stderr console for stdio transport (stdout is JSON-RPC channel)
_stderr_console = Console(stderr=True)


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


def _pid_exists(pid: int) -> bool:
    """Return True when the process id currently exists.

    Windows does not support ``os.kill(pid, 0)`` reliably for existence
    checks, so use a kernel32 handle probe there instead.
    """
    if pid <= 0:
        return False

    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query_limited_information = 0x1000
    still_active = 259

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        error_code = ctypes.get_last_error()
        return error_code == 5  # Access denied → process exists
    try:
        exit_code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0:
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _get_parent_pid_of(pid: int) -> int:
    """Get the parent PID of a process using the Windows Toolhelp API.

    On non-Windows platforms, falls back to 0 (unknown).
    """
    if sys.platform != "win32":
        return 0

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x00000002  # noqa: N806

    class PROCESSENTRY32(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_char * 260),
        ]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return 0

    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32)

    parent_pid = 0
    try:
        if kernel32.Process32First(snapshot, ctypes.byref(entry)):
            while True:
                if entry.th32ProcessID == pid:
                    parent_pid = entry.th32ParentProcessID
                    break
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    return parent_pid


def _get_ancestor_pids() -> list[int]:
    """Build the ancestor PID chain (parent, grandparent, ...).

    Stops at PID 0/4 (System) or when the chain loops.
    """
    ancestors: list[int] = []
    seen: set[int] = set()
    pid = os.getpid()

    while True:
        ppid = _get_parent_pid_of(pid) if sys.platform == "win32" else os.getppid()
        if ppid <= 4 or ppid in seen:
            break
        ancestors.append(ppid)
        seen.add(ppid)
        pid = ppid
        # Only walk the chain on Windows; on Unix os.getppid() is our direct parent
        if sys.platform != "win32":
            break

    return ancestors


def _get_process_creation_time(pid: int) -> int:
    """Get process creation time as FILETIME ticks (Windows only).

    Returns 0 if the process cannot be queried.
    """
    if sys.platform != "win32" or pid <= 0:
        return 0

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return 0
    try:
        creation = ctypes.c_ulonglong()
        exit_t = ctypes.c_ulonglong()
        kernel_t = ctypes.c_ulonglong()
        user_t = ctypes.c_ulonglong()
        if kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_t),
            ctypes.byref(kernel_t),
            ctypes.byref(user_t),
        ):
            return creation.value
        return 0
    finally:
        kernel32.CloseHandle(handle)


def _ancestor_still_same(pid: int, expected_creation: int) -> bool:
    """Check if a PID still belongs to the same process (handles PID recycling).

    Returns False if the process died or got recycled to a different process.
    """
    if not _pid_exists(pid):
        return False
    if expected_creation == 0:
        return True  # Can't verify creation time, trust PID check
    current_creation = _get_process_creation_time(pid)
    if current_creation == 0:
        return True  # Can't read creation time, trust PID check
    return current_creation == expected_creation


def _start_orphan_watchdog() -> None:
    """Start a daemon thread that force-exits when any ancestor process dies.

    The MCP server process chain is typically: Host App -> uv -> ouroboros.
    On Windows, when the host app closes, intermediate processes (uv.exe)
    may linger because they inherit pipe handles. This watchdog tracks both
    PID existence and creation time to reliably detect ancestor death even
    when PIDs are recycled.

    Runs in a daemon THREAD (not asyncio) so it cannot be blocked by the
    event loop. Calls ``os._exit()`` when any ancestor dies.
    """
    raw_pids = _get_ancestor_pids()
    # Build fingerprints: (pid, creation_time) for PID-recycling detection
    ancestors = [
        (pid, _get_process_creation_time(pid))
        for pid in raw_pids
        if _pid_exists(pid)
    ]

    if not ancestors:
        return

    _stderr_console.print(
        f"[dim]Watchdog monitoring {len(ancestors)} ancestor(s): "
        f"{[pid for pid, _ in ancestors]}[/dim]"
    )

    def _watch() -> None:
        try:
            while all(_ancestor_still_same(pid, ct) for pid, ct in ancestors):
                time.sleep(2)

            dead = [
                pid for pid, ct in ancestors if not _ancestor_still_same(pid, ct)
            ]
            _stderr_console.print(
                f"[yellow]Ancestor(s) {dead} exited/recycled "
                f"— forcing MCP server shutdown[/yellow]"
            )
        except Exception as exc:
            _stderr_console.print(f"[red]Watchdog error: {exc}[/red]")
        os._exit(0)

    thread = threading.Thread(target=_watch, daemon=True, name="orphan-watchdog")
    thread.start()


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
    from ouroboros.orchestrator.session import SessionRepository
    from ouroboros.persistence.event_store import EventStore

    # Create EventStore with custom path if provided
    if db_path:
        event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
    else:
        event_store = EventStore()

    # Auto-cancel orphaned sessions on startup.
    # Sessions left in RUNNING/PAUSED state for >1 hour are considered orphaned
    # (e.g., from a previous crash). Cancel them before accepting new requests.
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

    # Manage PID file for stale instance detection
    if _check_stale_instance():
        if transport == "stdio":
            _stderr_console.print("[yellow]Cleaned up stale MCP server PID file[/yellow]")
        else:
            print_info("Cleaned up stale MCP server PID file")

    _write_pid_file()

    # Start orphan watchdog BEFORE serving.  Runs in a daemon thread
    # (not asyncio) so it can't be blocked by the event loop or MCP SDK.
    # When any ancestor process dies, the thread calls os._exit(0).
    _start_orphan_watchdog()

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
    except OSError as e:
        print_error(f"MCP Server failed to start: {e}")
        print_info(
            "If this keeps happening, try:\n"
            "  1. Check if another MCP server is running: cat ~/.ouroboros/mcp-server.pid\n"
            "  2. Kill stale process: kill $(cat ~/.ouroboros/mcp-server.pid)\n"
            "  3. Remove stale PID: rm ~/.ouroboros/mcp-server.pid\n"
            "  4. Restart Claude Code"
        )
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
