"""Resume command for Ouroboros.

List in-flight sessions directly from the EventStore (no MCP dependency)
and surface the exec_id so the user can re-attach with `ooo status`.
"""

from __future__ import annotations

import asyncio
import os

import typer

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success
from ouroboros.cli.formatters.tables import create_table, print_table

app = typer.Typer(
    name="resume",
    help="List in-flight sessions and re-attach after MCP disconnect.",
    invoke_without_command=True,
)


async def _get_event_store():
    """Create and initialize an EventStore instance.

    Returns:
        Initialized EventStore.
    """
    from ouroboros.persistence.event_store import EventStore

    db_path = os.path.expanduser("~/.ouroboros/ouroboros.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
    await event_store.initialize()
    return event_store


async def _get_in_flight_sessions(event_store) -> list:
    """Return all running or paused session trackers.

    Args:
        event_store: Initialized EventStore instance.

    Returns:
        List of SessionTracker objects for in-flight sessions.
    """
    from ouroboros.orchestrator.session import SessionRepository, SessionStatus

    repo = SessionRepository(event_store)
    session_events = await event_store.get_all_sessions()

    if not session_events:
        return []

    seen: set[str] = set()
    in_flight: list = []

    for event in session_events:
        session_id = event.aggregate_id
        if session_id in seen:
            continue
        seen.add(session_id)

        result = await repo.reconstruct_session(session_id)
        if result.is_err:
            continue

        tracker = result.value
        if tracker.status in (SessionStatus.RUNNING, SessionStatus.PAUSED):
            in_flight.append(tracker)

    return in_flight


def _display_sessions(sessions: list) -> None:
    """Render in-flight sessions in a numbered table.

    Args:
        sessions: List of SessionTracker objects.
    """
    table = create_table("In-Flight Sessions")
    table.add_column("#", style="bold", no_wrap=True, justify="right")
    table.add_column("Session ID", style="cyan", no_wrap=True)
    table.add_column("Execution ID", style="dim")
    table.add_column("Seed ID", style="dim")
    table.add_column("Status", justify="center")
    table.add_column("Started", style="dim")

    for idx, tracker in enumerate(sessions, 1):
        status = tracker.status.value
        status_style = "success" if status == "running" else "warning"
        table.add_row(
            str(idx),
            tracker.session_id,
            tracker.execution_id or "-",
            tracker.seed_id or "-",
            f"[{status_style}]{status}[/]",
            tracker.start_time.isoformat(),
        )

    print_table(table)


async def _interactive_resume() -> None:
    """List in-flight sessions and prompt the user to pick one to re-attach."""
    try:
        event_store = await _get_event_store()
    except Exception as exc:  # noqa: BLE001
        print_error(f"Failed to open EventStore: {exc}")
        return

    try:
        sessions = await _get_in_flight_sessions(event_store)
    except Exception as exc:  # noqa: BLE001
        print_error(f"Failed to read EventStore: {exc}")
        return
    finally:
        await event_store.close()

    if not sessions:
        print_info("No in-flight sessions found.", "Resume")
        console.print(
            "[dim]Sessions appear here when the MCP server was disconnected mid-execution.[/]"
        )
        return

    _display_sessions(sessions)
    console.print()

    choice = typer.prompt(
        f"Enter number to re-attach (1-{len(sessions)}), or 'q' to quit",
        default="q",
    )

    if choice.strip().lower() == "q":
        print_info("No session selected.", "Resume")
        return

    try:
        index = int(choice) - 1
    except ValueError:
        print_error(f"Invalid selection: {choice!r}")
        raise typer.Exit(1)

    if index < 0 or index >= len(sessions):
        print_error(f"Selection out of range: {choice}. Expected 1-{len(sessions)}.")
        raise typer.Exit(1)

    selected = sessions[index]
    exec_id = selected.execution_id or selected.session_id

    print_success(
        f"Session selected: [bold]{selected.session_id}[/]\n"
        f"Execution ID:     [bold cyan]{exec_id}[/]\n\n"
        "Re-attach by running:\n\n"
        f"    ooo status {exec_id}",
        "Re-attach",
    )


@app.callback(invoke_without_command=True)
def resume(ctx: typer.Context) -> None:
    """List in-flight sessions and get re-attach instructions.

    Reads the EventStore directly — no MCP server required. Use this
    command after an unexpected MCP disconnect to recover the execution ID
    and re-attach with:

        ooo status <exec_id>

    Examples:

        # Interactive: list in-flight sessions and pick one
        ouroboros resume
    """
    if ctx.invoked_subcommand is not None:
        return
    asyncio.run(_interactive_resume())


__all__ = ["app"]
