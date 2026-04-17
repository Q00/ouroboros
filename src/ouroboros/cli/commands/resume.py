"""Resume command for Ouroboros.

List in-flight sessions directly from the EventStore (no MCP dependency).
The command is intentionally read-only: it never creates the data directory,
never writes schema, and never appends events. Read-only is enforced at the
SQLite connection layer via ``EventStore(..., read_only=True)`` so even
unexpected write paths fail fast with ``attempt to write a readonly
database``. Its only job is to surface the identifiers a user needs to
re-attach (inspect with ``ouroboros tui monitor`` or resume execution with
``ouroboros run workflow --orchestrator --resume <session_id> <seed.yaml>``).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success
from ouroboros.cli.formatters.tables import create_table, print_table

app = typer.Typer(
    name="resume",
    help="List in-flight sessions and re-attach after MCP disconnect.",
    invoke_without_command=True,
)


# Default cap on rows shown when many stale sessions accumulate. The user can
# opt into the full list with ``--all``.
DEFAULT_DISPLAY_LIMIT = 20

# Exit code surfaced when the EventStore exists but is unreadable. Pinned so
# users/scripts can branch on it (distinct from the happy "no sessions" path
# which exits 0).
EXIT_CORRUPTED_DB = 2


def _default_db_path() -> str:
    """Return the canonical SQLite path used by the running CLI."""
    return os.path.expanduser("~/.ouroboros/ouroboros.db")


async def _get_event_store(db_path: str | None = None):
    """Open the EventStore read-only.

    Returns ``None`` if the database file does not exist. Intentionally does
    not create ``~/.ouroboros/`` or run ``metadata.create_all`` — this command
    is a recovery tool and must not mutate user state. Read-only is enforced
    at the SQLite connection layer via ``EventStore(..., read_only=True)``
    so any accidental write path raises
    ``sqlite3.OperationalError: attempt to write a readonly database``.
    """
    from ouroboros.persistence.event_store import EventStore

    resolved = db_path or _default_db_path()
    if not Path(resolved).exists():
        return None

    event_store = EventStore(
        f"sqlite+aiosqlite:///{resolved}",
        read_only=True,
    )
    try:
        await event_store.initialize()
    except Exception:
        # Ensure the partially constructed engine is disposed before we bail
        # out — otherwise the outer ``finally`` cannot close it (the variable
        # was never bound in the caller).
        try:
            await event_store.close()
        finally:
            raise
    return event_store


def _is_active_snapshot(snapshot) -> bool:
    """Return True when a snapshot is plausibly running or paused.

    Terminal ``status_event_type`` values short-circuit the expensive replay.
    Non-terminal or progress-only snapshots fall through to reconstruction.
    """
    terminal = {
        "orchestrator.session.completed",
        "orchestrator.session.failed",
        "orchestrator.session.cancelled",
    }
    return snapshot.status_event_type not in terminal


async def _get_in_flight_sessions(event_store, limit: int | None = DEFAULT_DISPLAY_LIMIT) -> list:
    """Return running or paused session trackers, most-recent-first.

    Uses ``get_session_activity_snapshots`` to narrow candidates without
    replaying every event for every session, then reconstructs only the
    snapshots whose latest status is non-terminal.
    """
    from ouroboros.orchestrator.session import SessionRepository, SessionStatus

    repo = SessionRepository(event_store)

    try:
        snapshots = await event_store.get_session_activity_snapshots()
    except AttributeError:
        # Older EventStore builds may lack the snapshot helper. Fall back to
        # the full replay path so the command still works.
        return await _get_in_flight_sessions_fallback(event_store)

    candidates = [s for s in snapshots if _is_active_snapshot(s)]

    def _activity_key(snapshot) -> str:
        return str(snapshot.last_activity or snapshot.start_time or "")

    candidates.sort(key=_activity_key, reverse=True)

    in_flight: list = []
    for snapshot in candidates:
        result = await repo.reconstruct_session(snapshot.session_id)
        if result.is_err:
            continue
        tracker = result.value
        if tracker.status in (SessionStatus.RUNNING, SessionStatus.PAUSED):
            in_flight.append(tracker)
            if limit is not None and len(in_flight) >= limit:
                break

    return in_flight


async def _get_in_flight_sessions_fallback(event_store) -> list:
    """Legacy path: replay every session. Retained only for API compatibility."""
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
    """Render in-flight sessions in a numbered table."""
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


def _format_reattach_guidance(tracker) -> str:
    """Build the post-selection guidance block.

    Prints two commands, matching the real CLI contracts:

    - Inspect:   ``ouroboros tui monitor`` (functional TUI; select the session)
    - Resume:    ``ouroboros run workflow --orchestrator --resume <session_id> <seed.yaml>``

    ``run workflow --resume`` takes a *session_id* (not an execution_id) and
    also requires the seed file, so both identifiers are surfaced explicitly.

    Note: ``ouroboros status execution <exec_id>`` is *registered* but its
    implementation is still a placeholder (see src/ouroboros/cli/commands/status.py),
    so we deliberately do not surface it as an inspection path — it would
    print misleading "Would show details" output.
    """
    exec_id = tracker.execution_id or "<unknown>"
    seed_hint = tracker.seed_id or "<seed.yaml>"

    inspect_line = "ouroboros tui monitor"
    resume_line = f"ouroboros run workflow --orchestrator --resume {tracker.session_id} {seed_hint}"

    lines = [
        f"Session ID:   [bold cyan]{tracker.session_id}[/]",
        f"Execution ID: [bold cyan]{exec_id}[/]",
        "",
        "[bold]Inspect[/] (read-only interactive monitor):",
        f"    {inspect_line}",
        "",
        "[bold]Resume execution[/] (requires the original seed file):",
        f"    {resume_line}",
    ]
    if not tracker.seed_id:
        lines.append(
            "[dim]Seed ID was not recorded for this session — replace "
            "<seed.yaml> with the original seed path.[/]"
        )
    return "\n".join(lines)


async def _interactive_resume(show_all: bool = False) -> int:
    """List in-flight sessions and prompt the user to pick one to re-attach.

    Returns the integer exit code the CLI should emit so callers can pin
    distinct codes for missing DB vs. corrupted DB vs. happy path.
    """
    limit = None if show_all else DEFAULT_DISPLAY_LIMIT

    try:
        event_store = await _get_event_store()
    except Exception as exc:  # noqa: BLE001 — surface any open error verbatim
        print_error(f"Failed to open EventStore: {exc}")
        return EXIT_CORRUPTED_DB

    if event_store is None:
        print_info("No in-flight sessions found.", "Resume")
        console.print(
            "[dim]EventStore does not exist yet. "
            "Sessions appear here after the MCP server has started at least one run.[/]"
        )
        return 0

    try:
        try:
            sessions = await _get_in_flight_sessions(event_store, limit=limit)
        except Exception as exc:  # noqa: BLE001
            print_error(f"Failed to read EventStore: {exc}")
            return EXIT_CORRUPTED_DB
    finally:
        await event_store.close()

    if not sessions:
        print_info("No in-flight sessions found.", "Resume")
        console.print(
            "[dim]Sessions appear here when the MCP server was disconnected mid-execution.[/]"
        )
        return 0

    _display_sessions(sessions)
    console.print()

    choice = typer.prompt(
        f"Enter number to re-attach (1-{len(sessions)}), or 'q' to quit",
        default="q",
    )

    if choice.strip().lower() == "q":
        print_info("No session selected.", "Resume")
        return 0

    try:
        index = int(choice) - 1
    except ValueError:
        print_error(f"Invalid selection: {choice!r}")
        return 1

    if index < 0 or index >= len(sessions):
        print_error(f"Selection out of range: {choice}. Expected 1-{len(sessions)}.")
        return 1

    selected = sessions[index]
    print_success(_format_reattach_guidance(selected), "Re-attach")
    return 0


@app.callback(invoke_without_command=True)
def resume(
    ctx: typer.Context,
    show_all: Annotated[
        bool,
        typer.Option(
            "--all",
            "-a",
            help=(
                f"Show every in-flight session instead of the most recent {DEFAULT_DISPLAY_LIMIT}."
            ),
        ),
    ] = False,
) -> None:
    """List in-flight sessions and get re-attach instructions.

    Reads the EventStore directly — no MCP server required. Use this command
    after an unexpected MCP disconnect to recover the session/execution IDs
    and re-attach.

    Re-attach paths surfaced after selection:

        # Inspect (interactive monitor — the `status execution` placeholder is
        # not wired up yet)
        ouroboros tui monitor

        # Resume execution (requires the original seed file)
        ouroboros run workflow --orchestrator --resume <session_id> <seed.yaml>

    Examples:

        # Interactive: list in-flight sessions and pick one
        ouroboros resume

        # Show every stale active session, not just the 20 most recent
        ouroboros resume --all
    """
    if ctx.invoked_subcommand is not None:
        return
    exit_code = asyncio.run(_interactive_resume(show_all=show_all))
    if exit_code != 0:
        raise typer.Exit(exit_code)


__all__ = ["app"]
