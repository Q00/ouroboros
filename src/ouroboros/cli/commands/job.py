"""CLI wrappers for MCP background job monitoring."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated

import typer

from ouroboros.cli.formatters.panels import print_error
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobStatusHandler, JobWaitHandler
from ouroboros.mcp.types import MCPToolResult
from ouroboros.persistence.event_store import EventStore

app = typer.Typer(
    name="job",
    help="Inspect background Ouroboros jobs.",
    no_args_is_help=True,
)


def _emit_result(result: MCPToolResult) -> None:
    """Print stable text output from a job handler."""
    text = result.text_content
    if text:
        typer.echo(text)


def _run_job_handler(handler, arguments: dict[str, object]) -> None:
    """Run an async MCP job handler and map errors to CLI exit status."""
    result = asyncio.run(handler.handle(arguments))
    if result.is_err:
        print_error(result.error.message)
        raise typer.Exit(1)
    _emit_result(result.value)
    if result.value.is_error:
        raise typer.Exit(1)


def _default_db_path() -> str:
    """Return the canonical SQLite EventStore path."""
    return os.path.expanduser("~/.ouroboros/ouroboros.db")


async def _open_read_only_event_store(db_path: str | None = None) -> EventStore:
    """Open the EventStore in true read-only mode without creating files."""
    resolved = os.path.expanduser(db_path or _default_db_path())
    if not Path(resolved).exists():
        raise FileNotFoundError(resolved)
    event_store = EventStore(f"sqlite+aiosqlite:///{resolved}", read_only=True)
    try:
        await event_store.initialize()
    except Exception:
        try:
            await event_store.close()
        finally:
            raise
    return event_store


def _event_payload(event: BaseEvent) -> dict[str, object]:
    """Serialize a persisted event for JSON CLI output."""
    return {
        "id": event.id,
        "type": event.type,
        "timestamp": event.timestamp.isoformat(),
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "data": event.data,
        "consensus_id": event.consensus_id,
        "event_version": event.event_version,
    }


async def _read_job_events(
    job_id: str,
    *,
    since: int,
    limit: int,
    db_path: str | None,
) -> dict[str, object]:
    """Read job aggregate events through a read-only EventStore."""
    event_store = await _open_read_only_event_store(db_path)
    try:
        events, cursor = await event_store.get_events_after(
            "job",
            job_id,
            since,
            limit=limit,
        )
    finally:
        await event_store.close()

    return {
        "job_id": job_id,
        "cursor": cursor,
        "count": len(events),
        "events": [_event_payload(event) for event in events],
        "read_only": True,
    }


@app.command(name="status")
def status(
    job_id: Annotated[str, typer.Argument(help="Job ID returned by a start tool.")],
    view: Annotated[
        str,
        typer.Option(
            "--view",
            help="'full' (default), 'summary', or 'compact'.",
        ),
    ] = "full",
) -> None:
    """Print the latest status for a background job."""
    _run_job_handler(JobStatusHandler(), {"job_id": job_id, "view": view})


@app.command(name="wait")
def wait(
    job_id: Annotated[str, typer.Argument(help="Job ID returned by a start tool.")],
    cursor: Annotated[
        int,
        typer.Option("--cursor", help="Previous cursor from job status or job wait."),
    ] = 0,
    timeout_seconds: Annotated[
        int,
        typer.Option(
            "--timeout-seconds",
            help="Maximum seconds to wait for a change; defaults to an immediate snapshot.",
        ),
    ] = 0,
    view: Annotated[
        str,
        typer.Option(
            "--view",
            help="'full' (default), 'summary', or 'compact'.",
        ),
    ] = "full",
    stream: Annotated[
        str,
        typer.Option(
            "--stream",
            help="'progress' (default) or 'linked'.",
        ),
    ] = "progress",
) -> None:
    """Wait briefly for a background job update."""
    _run_job_handler(
        JobWaitHandler(),
        {
            "job_id": job_id,
            "cursor": cursor,
            "timeout_seconds": timeout_seconds,
            "view": view,
            "stream": stream,
        },
    )


@app.command(name="result")
def result(
    job_id: Annotated[str, typer.Argument(help="Job ID returned by a start tool.")],
) -> None:
    """Print the terminal result for a completed background job."""
    _run_job_handler(JobResultHandler(), {"job_id": job_id})


@app.command(name="events")
def events(
    job_id: Annotated[str, typer.Argument(help="Job ID returned by a start tool.")],
    since: Annotated[
        int,
        typer.Option("--since", help="Last seen EventStore rowid cursor."),
    ] = 0,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum job events to return."),
    ] = 100,
    db_path: Annotated[
        str | None,
        typer.Option(
            "--db-path",
            help="Path to ouroboros.db; defaults to ~/.ouroboros/ouroboros.db.",
        ),
    ] = None,
) -> None:
    """Print read-only job EventStore events as cursor-paged JSON."""
    if since < 0:
        print_error("since must be a non-negative integer")
        raise typer.Exit(1)
    if limit <= 0:
        print_error("limit must be a positive integer")
        raise typer.Exit(1)

    try:
        payload = asyncio.run(
            _read_job_events(
                job_id,
                since=since,
                limit=limit,
                db_path=db_path,
            )
        )
    except FileNotFoundError as exc:
        print_error(f"EventStore database not found: {exc.filename or exc}")
        raise typer.Exit(1) from exc
    except Exception as exc:
        print_error(f"Failed to read EventStore: {exc}")
        raise typer.Exit(1) from exc

    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


__all__ = ["app"]
