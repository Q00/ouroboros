"""CLI wrapper for the Ouroboros QA MCP tool.

This command delegates to ``ouroboros.mcp.tools.qa.QAHandler`` so CLI and MCP
QA share the same judge prompt, parsing, thresholds, and result format.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from ouroboros.cli.formatters import console
from ouroboros.mcp.tools.qa import DEFAULT_PASS_THRESHOLD, QAHandler


def _read_text_or_literal(value: str) -> str:
    path = Path(value).expanduser()
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def qa_command(
    artifact: Annotated[
        str,
        typer.Argument(help="Artifact text or path to a file to evaluate."),
    ],
    quality_bar: Annotated[
        str,
        typer.Option(
            "--quality-bar",
            "-q",
            help="Natural-language description of what PASS means.",
        ),
    ] = "Artifact should be correct, complete, internally consistent, and fit for its stated purpose.",
    artifact_type: Annotated[
        str,
        typer.Option(
            "--artifact-type",
            "-t",
            help="Artifact type: code, api_response, document, screenshot, test_output, custom.",
        ),
    ] = "code",
    reference: Annotated[
        str | None,
        typer.Option(
            "--reference",
            "-r",
            help="Optional reference text or path for comparison.",
        ),
    ] = None,
    pass_threshold: Annotated[
        float,
        typer.Option(
            "--pass-threshold",
            help="Score threshold for PASS verdict.",
            min=0.0,
            max=1.0,
        ),
    ] = DEFAULT_PASS_THRESHOLD,
    qa_session_id: Annotated[
        str | None,
        typer.Option(
            "--qa-session-id",
            help="Existing QA session ID for iterative checks.",
        ),
    ] = None,
    seed_content: Annotated[
        str | None,
        typer.Option(
            "--seed-content",
            help="Optional Seed YAML text or path for additional context.",
        ),
    ] = None,
) -> None:
    """Run a QA verdict using the same implementation as the MCP tool."""
    args: dict[str, object] = {
        "artifact": _read_text_or_literal(artifact),
        "quality_bar": quality_bar,
        "artifact_type": artifact_type,
        "pass_threshold": pass_threshold,
    }
    if reference is not None:
        args["reference"] = _read_text_or_literal(reference)
    if qa_session_id is not None:
        args["qa_session_id"] = qa_session_id
    if seed_content is not None:
        args["seed_content"] = _read_text_or_literal(seed_content)

    result = asyncio.run(QAHandler().handle(args))
    if result.is_err:
        console.print(f"[red]QA failed:[/] {result.error}")
        raise typer.Exit(code=1)

    tool_result = result.value
    for item in tool_result.content:
        if getattr(item, "text", None):
            console.print(item.text, markup=False, highlight=False)

    meta = tool_result.meta or {}
    if meta.get("passed") is False:
        raise typer.Exit(code=2)
