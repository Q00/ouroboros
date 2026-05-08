"""Status command group for Ouroboros.

Check system status and execution history.
"""

import asyncio
from typing import Annotated

import typer

from ouroboros.auto.listeners import replay_ralph_job_events
from ouroboros.auto.state import AutoPhase, AutoStore
from ouroboros.cli.formatters.panels import print_error, print_info
from ouroboros.cli.formatters.tables import create_status_table, print_table
from ouroboros.persistence.event_store import EventStore

app = typer.Typer(
    name="status",
    help="Check Ouroboros system status.",
    no_args_is_help=True,
)


def _format_auto_status(state) -> str:
    """Render a unified auto + ralph status block as plain text.

    Pinned by the snapshot test in ``tests/integration/auto/test_status_unified.py``.
    Each line is intentionally compact (one fact per line) so a human can grep
    the output and a Cucumber-style assertion can match it line-by-line. Layout
    mirrors :py:meth:`SessionStatusHandler._handle_auto_session` so the CLI and
    MCP surfaces never disagree.
    """
    lines = [
        "Auto status",
        "===========",
        f"Auto session: {state.auto_session_id}",
        f"Phase: {state.phase.value}",
    ]

    is_terminal = state.phase in {
        AutoPhase.COMPLETE,
        AutoPhase.BLOCKED,
        AutoPhase.FAILED,
    }
    lines.append(f"Terminal: {is_terminal}")
    lines.append(f"Last progress: {state.last_progress_message}")

    is_gap_window = (
        state.ralph_lineage_id is not None
        and state.ralph_job_id is None
        and state.ralph_dispatch_mode != "plugin"
    )

    if state.ralph_dispatch_mode == "plugin":
        lines.append("Ralph (plugin):")
        lines.append("  dispatch_mode: plugin")
        lines.append("  guidance: ralph delegated to OpenCode Task widget; follow that lifecycle")
    elif state.ralph_job_id is not None:
        lines.append("Ralph (job):")
        lines.append(f"  job_id: {state.ralph_job_id}")
        lines.append(f"  lineage_id: {state.ralph_lineage_id}")
        lines.append(f"  status: {state.ralph_job_status}")
        lines.append(f"  current_generation: {state.ralph_current_generation}")
        lines.append(f"  stop_reason: {state.ralph_stop_reason}")
    elif is_gap_window:
        lines.append("Ralph (pending):")
        lines.append(f"  lineage_id: {state.ralph_lineage_id}")
        lines.append("  pending: starting ralph")

    if state.last_error:
        lines.append(f"Blocker: {state.last_error}")

    return "\n".join(lines) + "\n"


async def _load_auto_status_state(
    auto_session_id: str,
    *,
    auto_store: AutoStore | None = None,
    event_store: EventStore | None = None,
):
    """Load an auto state and mirror linked Ralph job events before rendering."""
    store = auto_store or AutoStore()
    state = store.load(auto_session_id)
    if state.ralph_dispatch_mode == "plugin" or state.ralph_job_id is None:
        return state

    owns_event_store = event_store is None
    events = event_store or EventStore()
    try:
        await events.initialize()
        applied = await replay_ralph_job_events(state, events)
        if applied:
            store.save(state)
    finally:
        if owns_event_store:
            await events.close()
    return state


@app.command()
def auto(
    auto_session_id: Annotated[
        str,
        typer.Argument(help="Auto session id to inspect (auto_<hex>)."),
    ],
) -> None:
    """Show unified auto + ralph status for an ``ooo auto`` session.

    Q00/ouroboros#782 — renders both the auto pipeline phase and the ralph
    sub-block in a single human-readable view.
    """
    if not auto_session_id.startswith("auto_"):
        print_error("auto_session_id must start with auto_")
        raise typer.Exit(1)
    try:
        state = asyncio.run(_load_auto_status_state(auto_session_id))
    except ValueError as exc:
        print_error(f"Auto status failed: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(_format_auto_status(state), nl=False)


@app.command()
def executions(
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Number of executions to show."),
    ] = 10,
    all_: Annotated[
        bool,
        typer.Option("--all", "-a", help="Show all executions."),
    ] = False,
) -> None:
    """List recent executions.

    Shows execution history with status information.
    """
    # Placeholder implementation with example data
    example_data = [
        {"name": "exec-001", "status": "complete"},
        {"name": "exec-002", "status": "running"},
        {"name": "exec-003", "status": "failed"},
    ]
    table = create_status_table(example_data, "Recent Executions")
    print_table(table)

    if not all_:
        print_info(f"Showing last {limit} executions. Use --all to see more.")


@app.command()
def execution(
    execution_id: Annotated[
        str,
        typer.Argument(help="Execution ID to inspect."),
    ],
    events: Annotated[
        bool,
        typer.Option("--events", "-e", help="Show execution events."),
    ] = False,
) -> None:
    """Show details for a specific execution.

    Displays execution metadata, progress, and optionally events.
    """
    # Placeholder implementation
    print_info(f"Would show details for execution: {execution_id}")
    if events:
        print_info("Would include event history")


@app.command()
def health() -> None:
    """Check system health.

    Verifies database connectivity, provider configuration, and system resources.
    """
    # Placeholder implementation with example data
    health_data = [
        {"name": "Database", "status": "ok"},
        {"name": "Configuration", "status": "ok"},
        {"name": "Providers", "status": "warning"},
    ]
    table = create_status_table(health_data, "System Health")
    print_table(table)


__all__ = ["app"]
