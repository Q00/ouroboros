"""Convenience CLI surface for running Ouroboros through Zcode.

The regular commands expose every backend knob. This module provides a shorter
front door for users who just want "use Zcode for this" without remembering the
runtime, LLM-backend, and CLI-path flags.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import click
import typer

from ouroboros.cli.commands import init as init_command
from ouroboros.cli.commands import qa as qa_command_module
from ouroboros.cli.commands import run as run_command
from ouroboros.mcp.tools.qa import DEFAULT_PASS_THRESHOLD

_DEFAULT_ZCODE_CLI_PATH = Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")


class _DefaultStartGroup(typer.core.TyperGroup):
    """Use ``start`` when the user runs ``ouroboros zcode "..."``."""

    default_cmd_name: str = "start"

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        has_explicit_command = any(arg in self.commands for arg in args)
        wants_group_help = any(arg in {"--help", "-h"} for arg in args)
        if args and not has_explicit_command and not wants_group_help:
            args = [self.default_cmd_name, *args]
        return super().parse_args(ctx, args)


app = typer.Typer(
    name="zcode",
    help="Convenience commands that use Zcode automatically.",
    no_args_is_help=True,
    cls=_DefaultStartGroup,
)


def _prepare_zcode_environment(*, llm_backend: bool = False) -> None:
    """Fill in the local app-bundle path when the user has not configured one."""
    if not os.environ.get("OUROBOROS_ZCODE_CLI_PATH") and _DEFAULT_ZCODE_CLI_PATH.exists():
        os.environ["OUROBOROS_ZCODE_CLI_PATH"] = str(_DEFAULT_ZCODE_CLI_PATH)

    if llm_backend:
        os.environ["OUROBOROS_LLM_BACKEND"] = "zcode"


@app.command("start")
def start(
    context: Annotated[
        str | None,
        typer.Argument(help="What you want to build, check, or turn into a Seed."),
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option("--resume", "-r", help="Resume an existing interview by ID."),
    ] = None,
    state_dir: Annotated[
        Path | None,
        typer.Option(
            "--state-dir",
            help="Custom directory for interview state files.",
            exists=True,
            file_okay=False,
            dir_okay=True,
        ),
    ] = None,
    debug: Annotated[
        bool,
        typer.Option("--debug", "-d", help="Show verbose logs."),
    ] = False,
) -> None:
    """Start an interview using Zcode for authoring and workflow handoff."""
    _prepare_zcode_environment(llm_backend=True)
    init_command.start(
        context=context,
        resume=resume,
        state_dir=state_dir,
        orchestrator=True,
        runtime=init_command.AgentRuntimeBackend.ZCODE,
        llm_backend=init_command.LLMBackend.ZCODE,
        debug=debug,
    )


@app.command("qa")
def qa(
    artifact: Annotated[
        str,
        typer.Argument(help="Text or path to the file you want Zcode to evaluate."),
    ],
    quality_bar: Annotated[
        str,
        typer.Option(
            "--quality-bar",
            "-q",
            help="Plain-language description of what PASS means.",
        ),
    ] = "PASS if this is clear, correct, and useful.",
    artifact_type: Annotated[
        str,
        typer.Option(
            "--artifact-type",
            "-t",
            help="Artifact type: code, document, api_response, screenshot, test_output, custom.",
        ),
    ] = "document",
    reference: Annotated[
        str | None,
        typer.Option("--reference", "-r", help="Optional reference text or file path."),
    ] = None,
    pass_threshold: Annotated[
        float,
        typer.Option("--pass-threshold", min=0.0, max=1.0, help="Score threshold for PASS."),
    ] = DEFAULT_PASS_THRESHOLD,
) -> None:
    """Evaluate text or a file with Zcode-backed Ouroboros QA."""
    _prepare_zcode_environment(llm_backend=True)
    qa_command_module.qa_command(
        artifact=artifact,
        quality_bar=quality_bar,
        artifact_type=artifact_type,
        reference=reference,
        pass_threshold=pass_threshold,
        qa_session_id=None,
        seed_content=None,
    )


@app.command("run")
def run(
    seed_file: Annotated[
        Path,
        typer.Argument(
            help="Path to a Seed YAML file.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    project_dir: Annotated[
        Path | None,
        typer.Option(
            "--project-dir",
            help="Project directory to run in.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    resume_session: Annotated[
        str | None,
        typer.Option("--resume", "-r", help="Resume a previous orchestrator session by ID."),
    ] = None,
    no_qa: Annotated[
        bool,
        typer.Option("--no-qa", help="Skip post-execution QA evaluation."),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option("--debug", "-d", help="Show verbose logs."),
    ] = False,
    sequential: Annotated[
        bool,
        typer.Option("--sequential", "-s", help="Execute acceptance criteria sequentially."),
    ] = False,
    max_decomposition_depth: Annotated[
        int | None,
        typer.Option("--max-decomposition-depth", min=0, help="Maximum recursive split depth."),
    ] = None,
) -> None:
    """Run a Seed with Zcode as the execution runtime."""
    _prepare_zcode_environment()
    run_command.workflow(
        seed_file=seed_file,
        orchestrator=True,
        resume_session=resume_session,
        mcp_config=None,
        mcp_tool_prefix="",
        project_dir=project_dir,
        dry_run=False,
        debug=debug,
        sequential=sequential,
        runtime=run_command.AgentRuntimeBackend.ZCODE,
        no_qa=no_qa,
        max_decomposition_depth=max_decomposition_depth,
        skip_completed=None,
    )
