"""Rich progress bars for async operations.

Provides spinner-based progress indication for long-running operations,
with async-aware context managers for clean progress display.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn, TimeElapsedColumn

from ouroboros.cli.formatters import console


def create_progress() -> Progress:
    """Create a Progress instance with spinner for async operations.

    Returns:
        Progress instance configured with spinner, text, and elapsed time columns.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )


@contextmanager
def progress_spinner(description: str = "Processing...") -> Iterator[tuple[Progress, TaskID]]:
    """Context manager for displaying a progress spinner.

    Args:
        description: Text description to display alongside the spinner.

    Yields:
        Tuple of (Progress instance, TaskID) for the running task.

    Example:
        with progress_spinner("Loading configuration...") as (progress, task):
            # Do work here
            pass
    """
    progress = create_progress()
    with progress:
        task_id = progress.add_task(description, total=None)
        yield progress, task_id


@asynccontextmanager
async def async_progress_spinner(
    description: str = "Processing...",
) -> AsyncIterator[tuple[Progress, TaskID]]:
    """Async context manager for displaying a progress spinner.

    Use this for async operations that need visual progress indication.

    Args:
        description: Text description to display alongside the spinner.

    Yields:
        Tuple of (Progress instance, TaskID) for the running task.

    Example:
        async with async_progress_spinner("Fetching data...") as (progress, task):
            await fetch_data()
    """
    progress = create_progress()
    with progress:
        task_id = progress.add_task(description, total=None)
        yield progress, task_id


def create_determinate_progress(
    total: float,
    description: str = "Processing...",
) -> tuple[Progress, TaskID]:
    """Create a determinate progress bar for operations with known total.

    Args:
        total: Total number of steps/items.
        description: Text description to display.

    Returns:
        Tuple of (Progress instance, TaskID). Caller must manage Progress lifecycle.

    Example:
        progress, task = create_determinate_progress(100, "Processing files...")
        with progress:
            for i in range(100):
                do_work()
                progress.update(task, advance=1)
    """
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )
    task_id = progress.add_task(description, total=total)
    return progress, task_id


__all__ = [
    "create_progress",
    "progress_spinner",
    "async_progress_spinner",
    "create_determinate_progress",
]
