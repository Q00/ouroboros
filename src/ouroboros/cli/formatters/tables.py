"""Rich tables for structured data display.

Provides table formatting utilities with consistent styling
for displaying structured data in the Ouroboros CLI.
"""

from typing import Any

from rich.table import Table

from ouroboros.cli.formatters import console


def create_table(
    title: str | None = None,
    *,
    show_header: bool = True,
    show_lines: bool = False,
    border_style: str = "blue",
    header_style: str = "bold cyan",
    row_styles: list[str] | None = None,
) -> Table:
    """Create a Rich Table with consistent Ouroboros styling.

    Args:
        title: Optional table title.
        show_header: Whether to show the header row.
        show_lines: Whether to show lines between rows.
        border_style: Style for table borders.
        header_style: Style for header row.
        row_styles: Alternating row styles (default: subtle alternation).

    Returns:
        Configured Rich Table instance.

    Example:
        table = create_table("Results")
        table.add_column("Name", style="cyan")
        table.add_column("Status", style="green")
        table.add_row("Task 1", "Complete")
        print_table(table)
    """
    if row_styles is None:
        row_styles = ["", "dim"]

    return Table(
        title=title,
        show_header=show_header,
        show_lines=show_lines,
        border_style=border_style,
        header_style=header_style,
        row_styles=row_styles,
    )


def create_key_value_table(
    data: dict[str, Any],
    title: str | None = None,
    *,
    key_style: str = "cyan",
    value_style: str = "",
) -> Table:
    """Create a two-column table for key-value data.

    Args:
        data: Dictionary of key-value pairs to display.
        title: Optional table title.
        key_style: Style for the key column.
        value_style: Style for the value column.

    Returns:
        Rich Table populated with the key-value data.

    Example:
        info = {"Version": "1.0.0", "Status": "Running"}
        table = create_key_value_table(info, "System Info")
        print_table(table)
    """
    table = create_table(title, show_header=False)
    table.add_column("Key", style=key_style, no_wrap=True)
    table.add_column("Value", style=value_style)

    for key, value in data.items():
        table.add_row(str(key), str(value))

    return table


def create_status_table(
    items: list[dict[str, Any]],
    title: str | None = None,
    *,
    name_key: str = "name",
    status_key: str = "status",
) -> Table:
    """Create a table for displaying status information.

    Automatically applies semantic colors based on status values:
    - "success", "complete", "running": green
    - "warning", "pending": yellow
    - "error", "failed": red

    Args:
        items: List of dictionaries containing at least name and status.
        title: Optional table title.
        name_key: Key for the name/identifier field.
        status_key: Key for the status field.

    Returns:
        Rich Table populated with status information.

    Example:
        items = [
            {"name": "Task 1", "status": "complete"},
            {"name": "Task 2", "status": "running"},
        ]
        table = create_status_table(items, "Task Status")
        print_table(table)
    """
    table = create_table(title)
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center")

    for item in items:
        name = str(item.get(name_key, ""))
        status = str(item.get(status_key, ""))
        status_style = _get_status_style(status)
        table.add_row(name, f"[{status_style}]{status}[/]")

    return table


def _get_status_style(status: str) -> str:
    """Get semantic style for a status value.

    Args:
        status: Status string to style.

    Returns:
        Style string for Rich formatting.
    """
    status_lower = status.lower()
    if status_lower in ("success", "complete", "completed", "running", "active", "ok"):
        return "success"
    elif status_lower in ("warning", "pending", "waiting", "paused"):
        return "warning"
    elif status_lower in ("error", "failed", "failure", "critical"):
        return "error"
    return ""


def print_table(table: Table) -> None:
    """Print a Rich Table to the shared console.

    Args:
        table: Table to print.
    """
    console.print(table)


__all__ = [
    "create_table",
    "create_key_value_table",
    "create_status_table",
    "print_table",
]
