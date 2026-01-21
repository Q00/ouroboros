"""Rich panels for important messages.

Provides panel templates for displaying info, warning, error,
and success messages with consistent styling.
"""

from rich.panel import Panel

from ouroboros.cli.formatters import console


def info_panel(
    message: str,
    title: str = "Info",
    *,
    expand: bool = False,
) -> Panel:
    """Create an info panel with blue styling.

    Args:
        message: Message content to display.
        title: Panel title.
        expand: Whether to expand panel to full width.

    Returns:
        Configured Rich Panel.
    """
    return Panel(
        f"[info]{message}[/]",
        title=f"[bold blue]{title}[/]",
        border_style="blue",
        expand=expand,
    )


def warning_panel(
    message: str,
    title: str = "Warning",
    *,
    expand: bool = False,
) -> Panel:
    """Create a warning panel with yellow styling.

    Args:
        message: Message content to display.
        title: Panel title.
        expand: Whether to expand panel to full width.

    Returns:
        Configured Rich Panel.
    """
    return Panel(
        f"[warning]{message}[/]",
        title=f"[bold yellow]{title}[/]",
        border_style="yellow",
        expand=expand,
    )


def error_panel(
    message: str,
    title: str = "Error",
    *,
    expand: bool = False,
) -> Panel:
    """Create an error panel with red styling.

    Args:
        message: Message content to display.
        title: Panel title.
        expand: Whether to expand panel to full width.

    Returns:
        Configured Rich Panel.
    """
    return Panel(
        f"[error]{message}[/]",
        title=f"[bold red]{title}[/]",
        border_style="red",
        expand=expand,
    )


def success_panel(
    message: str,
    title: str = "Success",
    *,
    expand: bool = False,
) -> Panel:
    """Create a success panel with green styling.

    Args:
        message: Message content to display.
        title: Panel title.
        expand: Whether to expand panel to full width.

    Returns:
        Configured Rich Panel.
    """
    return Panel(
        f"[success]{message}[/]",
        title=f"[bold green]{title}[/]",
        border_style="green",
        expand=expand,
    )


def print_info(message: str, title: str = "Info") -> None:
    """Print an info message in a panel.

    Args:
        message: Message content to display.
        title: Panel title.
    """
    console.print(info_panel(message, title))


def print_warning(message: str, title: str = "Warning") -> None:
    """Print a warning message in a panel.

    Args:
        message: Message content to display.
        title: Panel title.
    """
    console.print(warning_panel(message, title))


def print_error(message: str, title: str = "Error") -> None:
    """Print an error message in a panel.

    Args:
        message: Message content to display.
        title: Panel title.
    """
    console.print(error_panel(message, title))


def print_success(message: str, title: str = "Success") -> None:
    """Print a success message in a panel.

    Args:
        message: Message content to display.
        title: Panel title.
    """
    console.print(success_panel(message, title))


__all__ = [
    "info_panel",
    "warning_panel",
    "error_panel",
    "success_panel",
    "print_info",
    "print_warning",
    "print_error",
    "print_success",
]
