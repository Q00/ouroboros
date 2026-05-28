"""CLI wrappers for MCP background job monitoring."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from ouroboros.cli.formatters.panels import print_error
from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobStatusHandler, JobWaitHandler
from ouroboros.mcp.types import MCPToolResult

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


__all__ = ["app"]
