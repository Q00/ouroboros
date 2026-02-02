"""Run command group for Ouroboros.

Execute workflows and manage running operations.
Supports both standard workflow execution and orchestrator mode (Claude Agent SDK).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
import yaml

if TYPE_CHECKING:
    from ouroboros.mcp.client.manager import MCPClientManager

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success, print_warning
from ouroboros.core.security import InputValidator

app = typer.Typer(
    name="run",
    help="Execute Ouroboros workflows.",
    no_args_is_help=True,
)


def _load_seed_from_yaml(seed_file: Path) -> dict[str, Any]:
    """Load seed configuration from YAML file.

    Args:
        seed_file: Path to the seed YAML file.

    Returns:
        Seed configuration dictionary.

    Raises:
        typer.Exit: If file cannot be loaded or exceeds size limit.
    """
    # Security: Validate file size to prevent DoS
    file_size = seed_file.stat().st_size
    is_valid, error_msg = InputValidator.validate_seed_file_size(file_size)
    if not is_valid:
        print_error(f"Seed file validation failed: {error_msg}")
        raise typer.Exit(1)

    try:
        with open(seed_file) as f:
            data: dict[str, Any] = yaml.safe_load(f)
            return data
    except Exception as e:
        print_error(f"Failed to load seed file: {e}")
        raise typer.Exit(1) from e


async def _initialize_mcp_manager(
    config_path: Path,
    tool_prefix: str,
) -> "MCPClientManager | None":
    """Initialize MCPClientManager from config file.

    Args:
        config_path: Path to MCP config YAML.
        tool_prefix: Prefix to add to MCP tool names.

    Returns:
        Configured MCPClientManager or None on error.
    """
    from ouroboros.mcp.client.manager import MCPClientManager
    from ouroboros.orchestrator.mcp_config import load_mcp_config

    # Load configuration
    result = load_mcp_config(config_path)
    if result.is_err:
        print_error(f"Failed to load MCP config: {result.error}")
        return None

    config = result.value

    # Create manager with connection settings
    manager = MCPClientManager(
        max_retries=config.connection.retry_attempts,
        health_check_interval=config.connection.health_check_interval,
        default_timeout=config.connection.timeout_seconds,
    )

    # Add all servers
    for server_config in config.servers:
        add_result = await manager.add_server(server_config)
        if add_result.is_err:
            print_warning(f"Failed to add MCP server '{server_config.name}': {add_result.error}")
        else:
            print_info(f"Added MCP server: {server_config.name}")

    # Connect to all servers
    if manager.servers:
        print_info("Connecting to MCP servers...")
        connect_results = await manager.connect_all()

        connected_count = 0
        for server_name, connect_result in connect_results.items():
            if connect_result.is_ok:
                server_info = connect_result.value
                print_success(f"  Connected to '{server_name}' ({len(server_info.tools)} tools)")
                connected_count += 1
            else:
                print_warning(f"  Failed to connect to '{server_name}': {connect_result.error}")

        if connected_count == 0:
            print_warning("No MCP servers connected. Continuing without external tools.")
            return None

        print_info(f"Connected to {connected_count}/{len(manager.servers)} MCP servers")

    return manager


async def _run_orchestrator(
    seed_file: Path,
    resume_session: str | None = None,
    mcp_config: Path | None = None,
    mcp_tool_prefix: str = "",
) -> None:
    """Run workflow via orchestrator mode (Claude Agent SDK).

    Args:
        seed_file: Path to seed YAML file.
        resume_session: Optional session ID to resume.
        mcp_config: Optional path to MCP config file.
        mcp_tool_prefix: Prefix for MCP tool names.
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

    # Initialize MCP manager if config provided
    mcp_manager = None
    if mcp_config:
        print_info(f"Loading MCP configuration from: {mcp_config}")
        mcp_manager = await _initialize_mcp_manager(mcp_config, mcp_tool_prefix)

    # Initialize components
    import os
    db_path = os.path.expanduser("~/.ouroboros/ouroboros.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
    await event_store.initialize()

    adapter = ClaudeAgentAdapter()
    runner = OrchestratorRunner(
        adapter,
        event_store,
        console,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
    )

    # Execute
    try:
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
    finally:
        # Cleanup MCP connections
        if mcp_manager:
            print_info("Disconnecting MCP servers...")
            await mcp_manager.disconnect_all()


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
    mcp_config: Annotated[
        Path | None,
        typer.Option(
            "--mcp-config",
            help="Path to MCP client configuration YAML file for external tool integration.",
        ),
    ] = None,
    mcp_tool_prefix: Annotated[
        str,
        typer.Option(
            "--mcp-tool-prefix",
            help="Prefix to add to all MCP tool names (e.g., 'mcp_').",
        ),
    ] = "",
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
    Use --mcp-config to connect to external MCP servers for additional tools.

    MCP Configuration File Format (YAML):

        mcp_servers:
          - name: "filesystem"
            transport: "stdio"
            command: "npx"
            args: ["-y", "@anthropic/mcp-server-filesystem", "/workspace"]
          - name: "github"
            transport: "stdio"
            command: "npx"
            args: ["-y", "@anthropic/mcp-server-github"]
            env:
              GITHUB_TOKEN: "${GITHUB_TOKEN}"
        connection:
          timeout_seconds: 30
          retry_attempts: 3

    Examples:

        # Standard workflow execution (placeholder)
        ouroboros run workflow seed.yaml

        # Orchestrator mode (Claude Agent SDK)
        ouroboros run workflow --orchestrator seed.yaml

        # With MCP server integration
        ouroboros run workflow --orchestrator --mcp-config mcp.yaml seed.yaml

        # With MCP tool prefix
        ouroboros run workflow -o --mcp-config mcp.yaml --mcp-tool-prefix "ext_" seed.yaml

        # Resume a previous orchestrator session
        ouroboros run workflow --orchestrator --resume orch_abc123 seed.yaml
    """
    # Validate MCP config requires orchestrator mode
    if mcp_config and not orchestrator and not resume_session:
        print_warning(
            "--mcp-config requires --orchestrator flag. Enabling orchestrator mode."
        )
        orchestrator = True

    if orchestrator or resume_session:
        # Orchestrator mode
        if resume_session and not orchestrator:
            console.print(
                "[yellow]Warning: --resume requires --orchestrator flag. "
                "Enabling orchestrator mode.[/yellow]"
            )
        asyncio.run(_run_orchestrator(
            seed_file,
            resume_session,
            mcp_config,
            mcp_tool_prefix,
        ))
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
