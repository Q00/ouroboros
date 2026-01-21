"""Status command group for Ouroboros.

Check system status and execution history.
"""

from typing import Annotated

import typer

from ouroboros.cli.formatters.panels import print_info
from ouroboros.cli.formatters.tables import create_status_table, print_table

app = typer.Typer(
    name="status",
    help="Check Ouroboros system status.",
    no_args_is_help=True,
)


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
