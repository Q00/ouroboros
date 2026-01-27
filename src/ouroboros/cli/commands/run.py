"""Run command group for Ouroboros.

Execute workflows and manage running operations.
Supports both standard workflow execution and orchestrator mode (Claude Agent SDK).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
import yaml

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success

app = typer.Typer(
    name="run",
    help="Execute Ouroboros workflows.",
    no_args_is_help=True,
)


def _load_seed_from_yaml(seed_file: Path) -> dict:
    """Load seed configuration from YAML file.

    Args:
        seed_file: Path to the seed YAML file.

    Returns:
        Seed configuration dictionary.

    Raises:
        typer.Exit: If file cannot be loaded.
    """
    try:
        with open(seed_file) as f:
            return yaml.safe_load(f)
    except Exception as e:
        print_error(f"Failed to load seed file: {e}")
        raise typer.Exit(1) from e


async def _run_orchestrator(
    seed_file: Path,
    resume_session: str | None = None,
) -> None:
    """Run workflow via orchestrator mode (Claude Agent SDK).

    Args:
        seed_file: Path to seed YAML file.
        resume_session: Optional session ID to resume.
    """
    from ouroboros.core.seed import Seed
    from ouroboros.orchestrator import ClaudeAgentAdapter, OrchestratorRunner
    from ouroboros.persistence.event_store import EventStore

    # Load seed
    seed_data = _load_seed_from_yaml(seed_file)

    try:
        seed = Seed.from_dict(seed_data)
    except Exception as e:
        print_error(f"Invalid seed format: {e}")
        raise typer.Exit(1) from e

    print_info(f"Loaded seed: {seed.goal[:80]}...")
    print_info(f"Acceptance criteria: {len(seed.acceptance_criteria)}")

    # Initialize components
    import os
    db_path = os.path.expanduser("~/.ouroboros/ouroboros.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
    await event_store.initialize()

    adapter = ClaudeAgentAdapter()
    runner = OrchestratorRunner(adapter, event_store, console)

    # Execute
    if resume_session:
        print_info(f"Resuming session: {resume_session}")
        result = await runner.resume_session(resume_session, seed)
    else:
        print_info("Starting new orchestrator execution...")
        result = await runner.execute_seed(seed)

    # Handle result
    if result.is_ok:
        res = result.value
        if res.success:
            print_success("Execution completed successfully!")
            print_info(f"Session ID: {res.session_id}")
            print_info(f"Messages processed: {res.messages_processed}")
            print_info(f"Duration: {res.duration_seconds:.1f}s")
        else:
            print_error("Execution failed")
            print_info(f"Session ID: {res.session_id}")
            console.print(f"[dim]Error: {res.final_message[:200]}[/dim]")
            raise typer.Exit(1)
    else:
        print_error(f"Orchestrator error: {result.error}")
        raise typer.Exit(1)


@app.command()
def workflow(
    seed_file: Annotated[
        Path,
        typer.Argument(
            help="Path to the seed YAML file.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    orchestrator: Annotated[
        bool,
        typer.Option(
            "--orchestrator",
            "-o",
            help="Use Claude Agent SDK for execution (Epic 8 mode).",
        ),
    ] = False,
    resume_session: Annotated[
        str | None,
        typer.Option(
            "--resume",
            "-r",
            help="Resume a previous orchestrator session by ID.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Validate seed without executing."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable verbose output."),
    ] = False,
) -> None:
    """Execute a workflow from a seed file.

    Reads the seed YAML configuration and runs the Ouroboros workflow.

    Use --orchestrator to execute via Claude Agent SDK (Epic 8).
    Use --resume with --orchestrator to continue a previous session.

    Examples:

        # Standard workflow execution (placeholder)
        ouroboros run workflow seed.yaml

        # Orchestrator mode (Claude Agent SDK)
        ouroboros run workflow --orchestrator seed.yaml

        # Resume a previous orchestrator session
        ouroboros run workflow --orchestrator --resume orch_abc123 seed.yaml
    """
    if orchestrator or resume_session:
        # Orchestrator mode
        if resume_session and not orchestrator:
            console.print(
                "[yellow]Warning: --resume requires --orchestrator flag. "
                "Enabling orchestrator mode.[/yellow]"
            )
        asyncio.run(_run_orchestrator(seed_file, resume_session))
    else:
        # Standard workflow (placeholder)
        print_info(f"Would execute workflow from: {seed_file}")
        if dry_run:
            console.print("[muted]Dry run mode - no changes will be made[/]")
        if verbose:
            console.print("[muted]Verbose mode enabled[/]")


@app.command()
def resume(
    execution_id: Annotated[
        str | None,
        typer.Argument(help="Execution ID to resume. Uses latest if not specified."),
    ] = None,
) -> None:
    """Resume a paused or failed execution.

    If no execution ID is provided, resumes the most recent execution.

    Note: For orchestrator sessions, use:
        ouroboros run workflow --orchestrator --resume <session_id> seed.yaml
    """
    # Placeholder implementation
    if execution_id:
        print_info(f"Would resume execution: {execution_id}")
    else:
        print_info("Would resume most recent execution")


__all__ = ["app"]
