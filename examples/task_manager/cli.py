"""Task Manager CLI using Typer.

This module provides the command-line interface for the task manager.
"""

from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from .models import Task, TaskStatus
from .storage import TaskStorage

# Create the Typer app
app = typer.Typer(
    name="task-manager",
    help="A simple task management CLI application.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

console = Console()
storage = TaskStorage()


@app.command()
def create(
    title: Annotated[str, typer.Argument(help="The title of the task")],
    description: Annotated[
        str,
        typer.Option(
            "--description",
            "-d",
            help="A detailed description of the task",
        ),
    ] = "",
) -> None:
    """Create a new task with a title and optional description.

    Examples:
        task-manager create "Buy groceries"
        task-manager create "Fix bug" -d "Fix the login form validation issue"
    """
    task = Task(title=title, description=description)
    storage.create(task)
    console.print(f"[green]Task created successfully![/]")
    console.print(f"  [bold]ID:[/] {task.id}")
    console.print(f"  [bold]Title:[/] {task.title}")
    if task.description:
        console.print(f"  [bold]Description:[/] {task.description}")


@app.command(name="list")
def list_tasks(
    status: Annotated[
        Optional[TaskStatus],
        typer.Option(
            "--status",
            "-s",
            help="Filter tasks by status",
        ),
    ] = None,
) -> None:
    """List all tasks, optionally filtered by status.

    Examples:
        task-manager list
        task-manager list --status pending
    """
    tasks = storage.get_all()

    if status:
        tasks = [t for t in tasks if t.status == status]

    if not tasks:
        console.print("[yellow]No tasks found.[/]")
        return

    table = Table(title="Tasks")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Title", style="bold")
    table.add_column("Description", max_width=40)
    table.add_column("Status", justify="center")
    table.add_column("Created", justify="right")

    for task in tasks:
        status_style = {
            TaskStatus.PENDING: "yellow",
            TaskStatus.IN_PROGRESS: "blue",
            TaskStatus.COMPLETED: "green",
        }.get(task.status, "white")

        table.add_row(
            task.id[:8],
            task.title,
            task.description[:40] + "..." if len(task.description) > 40 else task.description,
            f"[{status_style}]{task.status.value}[/]",
            task.created_at.strftime("%Y-%m-%d %H:%M"),
        )

    console.print(table)


@app.command()
def show(
    task_id: Annotated[str, typer.Argument(help="The ID of the task to show (can be partial)")],
) -> None:
    """Show details of a specific task.

    Examples:
        task-manager show abc123
    """
    tasks = storage.get_all()
    matching_tasks = [t for t in tasks if t.id.startswith(task_id)]

    if not matching_tasks:
        console.print(f"[red]Task not found with ID starting with: {task_id}[/]")
        raise typer.Exit(1)

    if len(matching_tasks) > 1:
        console.print(f"[yellow]Multiple tasks match. Please be more specific.[/]")
        for task in matching_tasks:
            console.print(f"  {task.id}: {task.title}")
        raise typer.Exit(1)

    task = matching_tasks[0]
    console.print(f"[bold]ID:[/] {task.id}")
    console.print(f"[bold]Title:[/] {task.title}")
    console.print(f"[bold]Description:[/] {task.description or '(no description)'}")
    console.print(f"[bold]Status:[/] {task.status.value}")
    console.print(f"[bold]Created:[/] {task.created_at}")
    console.print(f"[bold]Updated:[/] {task.updated_at}")


@app.command()
def update(
    task_id: Annotated[str, typer.Argument(help="The ID of the task to update (can be partial)")],
    title: Annotated[
        Optional[str],
        typer.Option("--title", "-t", help="New title for the task"),
    ] = None,
    description: Annotated[
        Optional[str],
        typer.Option("--description", "-d", help="New description for the task"),
    ] = None,
    status: Annotated[
        Optional[TaskStatus],
        typer.Option("--status", "-s", help="New status for the task"),
    ] = None,
) -> None:
    """Update an existing task.

    Examples:
        task-manager update abc123 --title "New title"
        task-manager update abc123 --status completed
    """
    from datetime import datetime

    tasks = storage.get_all()
    matching_tasks = [t for t in tasks if t.id.startswith(task_id)]

    if not matching_tasks:
        console.print(f"[red]Task not found with ID starting with: {task_id}[/]")
        raise typer.Exit(1)

    if len(matching_tasks) > 1:
        console.print(f"[yellow]Multiple tasks match. Please be more specific.[/]")
        for task in matching_tasks:
            console.print(f"  {task.id}: {task.title}")
        raise typer.Exit(1)

    task = matching_tasks[0]

    if title is not None:
        task.title = title
    if description is not None:
        task.description = description
    if status is not None:
        task.status = status

    task.updated_at = datetime.now()
    storage.update(task)

    console.print(f"[green]Task updated successfully![/]")
    console.print(f"  [bold]ID:[/] {task.id}")
    console.print(f"  [bold]Title:[/] {task.title}")


@app.command()
def complete(
    task_id: Annotated[str, typer.Argument(help="The ID of the task to mark as complete (can be partial)")],
) -> None:
    """Mark a task as complete.

    Examples:
        task-manager complete abc123
    """
    from datetime import datetime

    tasks = storage.get_all()
    matching_tasks = [t for t in tasks if t.id.startswith(task_id)]

    if not matching_tasks:
        console.print(f"[red]Task not found with ID starting with: {task_id}[/]")
        raise typer.Exit(1)

    if len(matching_tasks) > 1:
        console.print(f"[yellow]Multiple tasks match. Please be more specific.[/]")
        for task in matching_tasks:
            console.print(f"  {task.id}: {task.title}")
        raise typer.Exit(1)

    task = matching_tasks[0]

    if task.status == TaskStatus.COMPLETED:
        console.print(f"[yellow]Task '{task.title}' is already marked as complete.[/]")
        raise typer.Exit(0)

    task.status = TaskStatus.COMPLETED
    task.updated_at = datetime.now()
    storage.update(task)

    console.print(f"[green]✓ Task completed![/]")
    console.print(f"  [bold]ID:[/] {task.id[:8]}")
    console.print(f"  [bold]Title:[/] {task.title}")


@app.command()
def delete(
    task_id: Annotated[str, typer.Argument(help="The ID of the task to delete (can be partial)")],
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Delete a task by ID.

    Examples:
        task-manager delete abc123
        task-manager delete abc123 --force
    """
    tasks = storage.get_all()
    matching_tasks = [t for t in tasks if t.id.startswith(task_id)]

    if not matching_tasks:
        console.print(f"[red]Task not found with ID starting with: {task_id}[/]")
        raise typer.Exit(1)

    if len(matching_tasks) > 1:
        console.print(f"[yellow]Multiple tasks match. Please be more specific.[/]")
        for task in matching_tasks:
            console.print(f"  {task.id}: {task.title}")
        raise typer.Exit(1)

    task = matching_tasks[0]

    if not force:
        confirm = typer.confirm(f"Are you sure you want to delete '{task.title}'?")
        if not confirm:
            console.print("[yellow]Cancelled.[/]")
            raise typer.Exit(0)

    storage.delete(task.id)
    console.print(f"[green]Task deleted successfully![/]")


def main() -> None:
    """Run the CLI application."""
    app()


if __name__ == "__main__":
    main()
