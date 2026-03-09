"""Cancel command group for Ouroboros.

Cancel stuck or orphaned executions by session ID, cancel all running sessions,
or interactively pick from active executions.
Interacts directly with the EventStore (not via MCP tool).
"""

from __future__ import annotations

import asyncio
import os
from typing import Annotated

import typer

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success, print_warning
from ouroboros.cli.formatters.tables import create_table, print_table

app = typer.Typer(
    name="cancel",
    help="Cancel stuck or orphaned executions.",
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


async def _cancel_session(
    event_store,
    session_id: str,
    reason: str = "Cancelled by user via CLI",
) -> bool:
    """Cancel a single session by ID.

    Args:
        event_store: Initialized EventStore instance.
        session_id: Session ID to cancel.
        reason: Reason for cancellation.

    Returns:
        True if session was cancelled, False if it was not in a cancellable state.
    """
    from ouroboros.orchestrator.session import SessionRepository, SessionStatus

    repo = SessionRepository(event_store)

    # Reconstruct session to verify it exists and check current status
    result = await repo.reconstruct_session(session_id)
    if result.is_err:
        print_error(f"Session not found: {session_id}")
        return False

    tracker = result.value
    if tracker.status == SessionStatus.CANCELLED:
        print_warning(f"Session {session_id} is already cancelled.")
        return False

    if tracker.status in (SessionStatus.COMPLETED, SessionStatus.FAILED):
        print_warning(
            f"Session {session_id} is already {tracker.status.value}. "
            "Only running or paused sessions can be cancelled."
        )
        return False

    # Cancel the session
    cancel_result = await repo.mark_cancelled(
        session_id=session_id,
        reason=reason,
        cancelled_by="user",
    )

    if cancel_result.is_err:
        print_error(f"Failed to cancel session {session_id}: {cancel_result.error}")
        return False

    return True


async def _list_active_sessions(event_store) -> list:
    """List all active (running/paused) sessions.

    Args:
        event_store: Initialized EventStore instance.

    Returns:
        List of SessionTracker objects for active sessions.
    """
    from ouroboros.orchestrator.session import SessionRepository, SessionStatus

    repo = SessionRepository(event_store)
    session_events = await event_store.get_all_sessions()

    if not session_events:
        return []

    active = []
    for event in session_events:
        session_id = event.aggregate_id
        result = await repo.reconstruct_session(session_id)
        if result.is_err:
            continue
        tracker = result.value
        if tracker.status in (SessionStatus.RUNNING, SessionStatus.PAUSED):
            active.append(tracker)

    return active


async def _cancel_all_running(
    event_store,
    reason: str = "Cancelled all running sessions via CLI",
) -> tuple[int, int]:
    """Cancel all running/paused sessions.

    Args:
        event_store: Initialized EventStore instance.
        reason: Reason for cancellation.

    Returns:
        Tuple of (cancelled_count, skipped_count).
    """
    from ouroboros.orchestrator.session import SessionRepository, SessionStatus

    repo = SessionRepository(event_store)

    # Get all session start events
    session_events = await event_store.get_all_sessions()

    if not session_events:
        return (0, 0)

    cancelled = 0
    skipped = 0

    for event in session_events:
        session_id = event.aggregate_id

        # Reconstruct to get current status
        result = await repo.reconstruct_session(session_id)
        if result.is_err:
            skipped += 1
            continue

        tracker = result.value
        if tracker.status not in (SessionStatus.RUNNING, SessionStatus.PAUSED):
            skipped += 1
            continue

        # Cancel this session
        cancel_result = await repo.mark_cancelled(
            session_id=session_id,
            reason=reason,
            cancelled_by="user",
        )

        if cancel_result.is_ok:
            cancelled += 1
            console.print(f"  [dim]Cancelled:[/] {session_id}")
        else:
            skipped += 1

    return (cancelled, skipped)


def _display_active_sessions(sessions: list) -> None:
    """Display active sessions in a numbered table for interactive selection.

    Args:
        sessions: List of SessionTracker objects for active sessions.
    """
    table = create_table("Active Executions")
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
            tracker.execution_id,
            tracker.seed_id,
            f"[{status_style}]{status}[/]",
            tracker.start_time.isoformat(),
        )

    print_table(table)


async def _interactive_cancel(reason: str) -> None:
    """Interactive mode: list active executions and prompt user to pick one.

    Args:
        reason: Reason for cancellation.
    """
    event_store = await _get_event_store()

    try:
        active_sessions = await _list_active_sessions(event_store)

        if not active_sessions:
            print_info("No active executions found.")
            return

        _display_active_sessions(active_sessions)
        console.print()

        # Prompt user to pick a session number
        choice = typer.prompt(
            f"Enter number to cancel (1-{len(active_sessions)}), or 'q' to quit",
            default="q",
        )

        if choice.strip().lower() == "q":
            print_info("Cancelled. No executions were modified.")
            return

        try:
            index = int(choice) - 1
        except ValueError:
            print_error(f"Invalid selection: {choice}")
            raise typer.Exit(1)

        if index < 0 or index >= len(active_sessions):
            print_error(f"Selection out of range: {choice}. Expected 1-{len(active_sessions)}.")
            raise typer.Exit(1)

        selected = active_sessions[index]
        session_id = selected.session_id

        # Confirm before cancelling
        confirm = typer.confirm(
            f"Cancel session {session_id} ({selected.status.value})?",
        )

        if not confirm:
            print_info("Cancelled. No executions were modified.")
            return

        success = await _cancel_session(event_store, session_id, reason)
        if success:
            print_success(f"Cancelled execution: {session_id}")
    finally:
        await event_store.close()


@app.command("execution")
def cancel_execution(
    execution_id: Annotated[
        str | None,
        typer.Argument(help="Session/execution ID to cancel."),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option("--all", "-a", help="Cancel all running/paused executions."),
    ] = False,
    reason: Annotated[
        str,
        typer.Option("--reason", "-r", help="Reason for cancellation."),
    ] = "Cancelled by user via CLI",
) -> None:
    """Cancel a stuck or orphaned execution.

    Cancel a specific execution by session ID, or use --all to cancel
    every running/paused execution. When called without arguments,
    lists active executions and prompts you to pick one.

    This command interacts directly with the event store to emit
    cancellation events.

    Examples:

        # Interactive mode - list and pick
        ouroboros cancel execution

        # Cancel a specific execution
        ouroboros cancel execution orch_abc123def456

        # Cancel all running executions
        ouroboros cancel execution --all

        # Cancel with a custom reason
        ouroboros cancel execution orch_abc123 --reason "Stuck for 2 hours"
    """
    if execution_id and all_:
        print_error("Cannot specify both an execution ID and --all. Choose one.")
        raise typer.Exit(1)

    if not execution_id and not all_:
        # Interactive mode: list active executions and prompt user to pick one
        asyncio.run(_interactive_cancel(reason))
        return

    asyncio.run(_cancel_execution_async(execution_id, all_, reason))


async def _cancel_execution_async(
    execution_id: str | None,
    all_: bool,
    reason: str,
) -> None:
    """Async implementation for cancel execution command.

    Args:
        execution_id: Specific session ID to cancel, or None for --all mode.
        all_: Whether to cancel all running sessions.
        reason: Reason for cancellation.
    """
    event_store = await _get_event_store()

    try:
        if all_:
            print_info("Cancelling all running executions...")
            cancelled, skipped = await _cancel_all_running(event_store, reason)

            if cancelled == 0:
                print_info("No running executions found to cancel.")
            else:
                print_success(f"Cancelled {cancelled} execution(s).")
        else:
            assert execution_id is not None
            success = await _cancel_session(event_store, execution_id, reason)
            if success:
                print_success(f"Cancelled execution: {execution_id}")
    finally:
        await event_store.close()


__all__ = ["app"]
