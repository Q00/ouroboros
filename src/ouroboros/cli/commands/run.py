"""Run command group for Ouroboros.

Execute workflows and manage running operations.
Supports both standard workflow execution and agent-runtime orchestrator mode.
"""

import asyncio
from enum import Enum
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from uuid import uuid4

import click
import structlog
import typer
import yaml

log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.mcp.client.manager import MCPClientManager

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success, print_warning
from ouroboros.core.project_paths import resolve_seed_project_path
from ouroboros.core.security import InputValidator
from ouroboros.core.worktree import (
    TaskWorkspace,
    WorktreeError,
    maybe_prepare_task_workspace,
    maybe_restore_task_workspace,
)
from ouroboros.evaluation.verification_artifacts import build_verification_artifacts
from ouroboros.orchestrator.parallel_executor import DEFAULT_MAX_DECOMPOSITION_DEPTH


class _DefaultWorkflowGroup(typer.core.TyperGroup):
    """TyperGroup that falls back to 'workflow' when no subcommand matches.

    This enables the shorthand `ouroboros run seed.yaml` which is equivalent
    to `ouroboros run workflow seed.yaml`.
    """

    default_cmd_name: str = "workflow"

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = [self.default_cmd_name, *args]
        return super().parse_args(ctx, args)


app = typer.Typer(
    name="run",
    help="Execute Ouroboros workflows.",
    no_args_is_help=True,
    cls=_DefaultWorkflowGroup,
)


class AgentRuntimeBackend(str, Enum):  # noqa: UP042
    """Supported orchestrator runtime backends for CLI selection."""

    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"
    HERMES = "hermes"


def _derive_quality_bar(seed: "Seed") -> str:
    """Derive a quality bar string from seed acceptance criteria."""
    ac_lines = [f"- {ac}" for ac in seed.acceptance_criteria]
    return "The execution must satisfy all acceptance criteria:\n" + "\n".join(ac_lines)


def _get_verification_artifact(summary: dict[str, Any], final_message: str) -> str:
    """Prefer the structured verification report when present."""
    verification_report = summary.get("verification_report")
    if isinstance(verification_report, str) and verification_report:
        return verification_report
    return final_message or ""


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


def _resolve_cli_project_dir(seed: "Seed", seed_file: Path) -> Path:
    """Resolve the project directory for CLI execution and verification."""
    stable_base = seed_file.parent.resolve()
    return resolve_seed_project_path(seed, stable_base=stable_base) or stable_base


def _coerce_non_negative_int(value: object, *, source: str) -> int:
    """Parse a non-negative integer from CLI, env, or seed config."""
    if isinstance(value, bool):
        print_error(f"{source} must be a non-negative integer")
        raise typer.Exit(1)

    try:
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str):
            parsed = int(value)
        else:
            raise TypeError
    except (TypeError, ValueError) as exc:
        print_error(f"{source} must be a non-negative integer")
        raise typer.Exit(1) from exc

    if parsed < 0:
        print_error(f"{source} must be a non-negative integer")
        raise typer.Exit(1)
    return parsed


def _coerce_positive_int(value: object, *, source: str) -> int:
    """Parse a positive integer from CLI or env config."""
    parsed = _coerce_non_negative_int(value, source=source)
    if parsed <= 0:
        print_error(f"{source} must be greater than 0")
        raise typer.Exit(1)
    return parsed


def _resolve_max_decomposition_depth(seed_data: dict[str, Any], cli_value: int | None) -> int:
    """Resolve decomposition depth from CLI, env, seed config, then default."""
    if cli_value is not None:
        return _coerce_non_negative_int(cli_value, source="--max-decomposition-depth")

    env_value = os.environ.get("OUROBOROS_MAX_DECOMPOSITION_DEPTH", "").strip()
    if env_value:
        return _coerce_non_negative_int(
            env_value,
            source="OUROBOROS_MAX_DECOMPOSITION_DEPTH",
        )

    orchestrator_config = seed_data.get("orchestrator")
    if isinstance(orchestrator_config, dict) and "max_decomposition_depth" in orchestrator_config:
        return _coerce_non_negative_int(
            orchestrator_config.get("max_decomposition_depth"),
            source="seed.orchestrator.max_decomposition_depth",
        )

    return DEFAULT_MAX_DECOMPOSITION_DEPTH


def _load_seed_id_from_yaml(seed_file: Path) -> str | None:
    """Extract the seed_id from a seed YAML file without full model parsing.

    Used for early validation (e.g., checking checkpoint existence) before
    the full :func:`Seed.from_dict` parse happens inside ``_run_orchestrator``.

    Applies the same file-size DoS guard as :func:`_load_seed_from_yaml` so a
    pathological seed file can't bypass the size check by being read via this
    early-probe path.

    Returns:
        ``seed_id`` string when present, ``None`` on any error or if the field
        is absent / empty (including the size-guard failure case).
    """
    try:
        file_size = seed_file.stat().st_size
        is_valid, _err = InputValidator.validate_seed_file_size(file_size)
        if not is_valid:
            return None
        with open(seed_file) as _f:
            _data = yaml.safe_load(_f)
        return ((_data or {}).get("metadata") or {}).get("seed_id") or None
    except Exception:  # noqa: BLE001
        return None


def _validate_compounding_resume_not_fresh_seed(
    seed_id: str,
    compounding_resume_session_id: str | None,
    checkpoint_store: "Any | None" = None,
) -> str | None:
    """Return an error message if ``--compounding --resume`` targets a fresh seed.

    Enforces the mutual exclusivity rule: if the user supplies
    ``--compounding --resume <id>``, a compounding checkpoint for this
    ``seed_id`` **must** already exist.  Using ``--resume`` with a brand-new
    seed that has never been run in compounding mode is contradictory —
    there is no prior chain to rehydrate.

    Returns:
        ``None`` when the combination is valid (no error), or a human-readable
        error message string when the seed is "fresh" (no checkpoint found).

    Skips validation (returns ``None``) when:

    - ``compounding_resume_session_id`` is ``None`` — user is not resuming.
    - The default checkpoint directory does not exist — fresh installation with
      no prior runs; we fail-open so new users aren't blocked.
    - ``checkpoint_store`` raises any exception — fail-open (don't block the run).

    Args:
        seed_id: Seed identifier to look up in the checkpoint store.
        compounding_resume_session_id: The ``--resume`` value; ``None`` means
            the user is not requesting a checkpoint resume.
        checkpoint_store: Injected store for testing.  When ``None``, the
            function constructs a :class:`~ouroboros.persistence.checkpoint.CheckpointStore`
            backed by ``~/.ouroboros/data/checkpoints``.  Pass a mock store in
            unit tests to avoid touching the real filesystem.

    [[INVARIANT: _validate_compounding_resume_not_fresh_seed returns None when checkpoint dir absent]]
    [[INVARIANT: --compounding --resume with no prior checkpoint raises an error not a silent fresh run]]
    """
    if compounding_resume_session_id is None:
        return None  # Not resuming; nothing to check.

    if checkpoint_store is None:
        from ouroboros.persistence.checkpoint import CheckpointStore as _CS

        default_path = Path.home() / ".ouroboros" / "data" / "checkpoints"
        if not default_path.exists():
            # Fresh environment — no checkpoints possible; skip validation
            # so new users aren't blocked by the guard.
            return None
        try:
            checkpoint_store = _CS(base_path=default_path)
            checkpoint_store.initialize()
        except Exception:  # noqa: BLE001
            return None  # Can't set up store; fail open.

    try:
        load_result = checkpoint_store.load(seed_id)
        if load_result.is_err:
            return (
                f"Cannot resume: no compounding checkpoint found for seed '{seed_id}'. "
                "The seed has not been run in compounding mode before, or its "
                "checkpoint has been deleted. "
                "To start a fresh compounding run, omit --resume."
            )
        # A checkpoint exists, but it might belong to a different mode (e.g. the
        # seed was previously run in parallel mode and produced a non-compounding
        # checkpoint).  CompoundingCheckpointState always stamps state["mode"] =
        # "compounding"; reject anything else with the same user-facing message
        # so --resume only resumes compounding runs.
        checkpoint = load_result.value
        state = getattr(checkpoint, "state", None) or {}
        if state.get("mode") != "compounding":
            return (
                f"Cannot resume: no compounding checkpoint found for seed '{seed_id}'. "
                "The seed has not been run in compounding mode before, or its "
                "checkpoint has been deleted. "
                "To start a fresh compounding run, omit --resume."
            )
    except Exception:  # noqa: BLE001
        return None  # Store error; fail open.

    return None  # Compounding checkpoint found; valid to resume.


def _load_skip_completed_markers(
    marker_path: str | None,
    *,
    total_acs: int,
) -> dict[int, dict[str, Any]]:
    """Load a YAML marker file describing already-satisfied top-level ACs."""
    if not marker_path:
        return {}

    path = Path(marker_path).expanduser()
    if not path.exists() or not path.is_file():
        print_error(f"--skip-completed file not found: {path}")
        raise typer.Exit(1)

    try:
        raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print_error(f"Failed to read --skip-completed file: {exc}")
        raise typer.Exit(1) from exc

    if raw_data is None:
        return {}

    if isinstance(raw_data, dict):
        raw_entries = raw_data.get("completed_acs", [])
    elif isinstance(raw_data, list):
        raw_entries = raw_data
    else:
        print_error("--skip-completed must be a YAML list or a mapping with completed_acs")
        raise typer.Exit(1)

    if not isinstance(raw_entries, list):
        print_error("--skip-completed completed_acs must be a YAML list")
        raise typer.Exit(1)

    resolved: dict[int, dict[str, Any]] = {}
    for index, entry in enumerate(raw_entries, start=1):
        source = f"{path}: completed_acs[{index}]"
        if isinstance(entry, dict):
            ac_number = _coerce_non_negative_int(entry.get("ac"), source=f"{source}.ac")
            metadata = {
                "reason": entry.get("reason"),
                "commit": entry.get("commit"),
            }
        else:
            ac_number = _coerce_non_negative_int(entry, source=source)
            metadata = {}

        if ac_number < 1 or ac_number > total_acs:
            print_error(
                f"{source} references AC {ac_number}, but the seed only has {total_acs} ACs"
            )
            raise typer.Exit(1)
        resolved[ac_number - 1] = metadata

    return resolved


def _resolve_max_parallel_workers() -> int:
    """Resolve the parallel worker cap from the environment."""
    env_value = os.environ.get("OUROBOROS_MAX_PARALLEL_WORKERS", "").strip()
    if env_value:
        return _coerce_positive_int(
            env_value,
            source="OUROBOROS_MAX_PARALLEL_WORKERS",
        )
    return 3


async def _initialize_mcp_manager(
    config_path: Path,
    tool_prefix: str,  # noqa: ARG001
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
    debug: bool = False,
    parallel: bool = True,
    no_qa: bool = False,
    runtime_backend: str | None = None,
    max_decomposition_depth: int | None = None,
    skip_completed: str | None = None,
    mode: str | None = None,
    compounding_resume_session_id: str | None = None,
) -> None:
    """Run workflow via orchestrator mode.

    Args:
        seed_file: Path to seed YAML file.
        resume_session: Optional session ID to resume (orchestrator-level).
        mcp_config: Optional path to MCP config file.
        mcp_tool_prefix: Prefix for MCP tool names.
        debug: Show verbose logs and agent thinking.
        parallel: Execute independent ACs in parallel. Default: True.
        no_qa: Skip post-execution QA. Default: False.
        runtime_backend: Optional orchestrator runtime backend override.
        max_decomposition_depth: Optional recursive decomposition depth cap override.
        skip_completed: Optional path to a marker file for already-satisfied ACs.
        mode: Execution mode override ("parallel" | "compounding"). When set,
            takes precedence over the ``parallel`` flag.
        compounding_resume_session_id: When set alongside ``mode="compounding"``,
            passed through to ``execute_serial`` as ``resume_session_id`` so
            checkpoint-based resume kicks in.  Mutually exclusive with
            ``skip_completed``.
    """
    from ouroboros.core.seed import Seed
    from ouroboros.orchestrator import OrchestratorRunner, create_agent_runtime
    from ouroboros.orchestrator.session import SessionRepository
    from ouroboros.persistence.event_store import EventStore

    # Load seed
    seed_data = _load_seed_from_yaml(seed_file)

    try:
        seed = Seed.from_dict(seed_data)
    except Exception as e:
        print_error(f"Invalid seed format: {e}")
        raise typer.Exit(1) from e

    resolved_max_decomposition_depth = _resolve_max_decomposition_depth(
        seed_data,
        max_decomposition_depth,
    )
    resolved_max_parallel_workers = _resolve_max_parallel_workers()
    externally_satisfied_acs: dict[int, dict[str, Any]] | None = None
    if skip_completed:
        if resume_session:
            print_warning("--skip-completed is ignored when resuming an existing session.")
        else:
            externally_satisfied_acs = _load_skip_completed_markers(
                skip_completed,
                total_acs=len(seed.acceptance_criteria),
            )

    if debug:
        print_info(f"Loaded seed: {seed.goal[:80]}...")
        print_info(f"Acceptance criteria: {len(seed.acceptance_criteria)}")
        print_info(f"Max decomposition depth: {resolved_max_decomposition_depth}")
        print_info(f"Max parallel workers: {resolved_max_parallel_workers}")
        if externally_satisfied_acs:
            print_info(f"Externally satisfied ACs: {len(externally_satisfied_acs)}")
        if compounding_resume_session_id:
            print_info(
                f"Compounding resume: will load checkpoint for session "
                f"{compounding_resume_session_id}"
            )

    # Initialize MCP manager if config provided
    mcp_manager = None
    if mcp_config:
        if debug:
            print_info(f"Loading MCP configuration from: {mcp_config}")
        mcp_manager = await _initialize_mcp_manager(mcp_config, mcp_tool_prefix)

    # Initialize components
    db_path = os.path.expanduser("~/.ouroboros/ouroboros.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
    await event_store.initialize()

    project_dir = _resolve_cli_project_dir(seed, seed_file)
    session_repo = SessionRepository(event_store)
    workspace: TaskWorkspace | None = None
    execution_id: str | None = None
    session_id_for_run: str | None = None

    try:
        if resume_session:
            reconstructed = await session_repo.reconstruct_session(resume_session)
            if reconstructed.is_err:
                print_error(f"Failed to reconstruct session: {reconstructed.error}")
                raise typer.Exit(1)
            persisted = TaskWorkspace.from_progress_dict(
                reconstructed.value.progress.get("workspace")
            )
            workspace = maybe_restore_task_workspace(
                resume_session,
                persisted,
                fallback_source_cwd=project_dir,
            )
            session_id_for_run = resume_session
            execution_id = reconstructed.value.execution_id
        else:
            session_id_for_run = f"orch_{uuid4().hex[:12]}"
            execution_id = f"exec_{uuid4().hex[:12]}"
            workspace = maybe_prepare_task_workspace(project_dir, session_id_for_run)
    except WorktreeError as e:
        print_error(f"Task workspace error: {e.message}")
        raise typer.Exit(1) from e

    if workspace is not None:
        print_info(f"Task worktree: {workspace.worktree_path}")
        print_info(f"Task branch: {workspace.branch}")

    adapter = create_agent_runtime(
        backend=runtime_backend,
        cwd=Path(workspace.effective_cwd) if workspace else project_dir,
    )

    # Set up checkpoint store for compounding mode so per-AC checkpoints are
    # written and checkpoint-based resume works end-to-end from the CLI.
    # Failures are silenced (best-effort) so a broken checkpoint path never
    # prevents a fresh compounding run from starting.
    _cli_checkpoint_store = None
    if mode == "compounding":
        try:
            from ouroboros.persistence.checkpoint import CheckpointStore as _CheckpointStore

            _cli_checkpoint_store = _CheckpointStore()  # default ~/.ouroboros/data/checkpoints
            _cli_checkpoint_store.initialize()
        except Exception:  # noqa: BLE001
            _cli_checkpoint_store = None

    runner = OrchestratorRunner(
        adapter,
        event_store,
        console,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
        debug=debug,
        task_workspace=workspace,
        max_decomposition_depth=resolved_max_decomposition_depth,
        max_parallel_workers=resolved_max_parallel_workers,
        checkpoint_store=_cli_checkpoint_store,
    )

    # Execute
    try:
        if resume_session and not compounding_resume_session_id:
            # Orchestrator-level session resume (non-compounding path).
            if debug:
                print_info(f"Resuming session: {resume_session}")
            result = await runner.resume_session(resume_session, seed)
        else:
            if debug:
                print_info("Starting new orchestrator execution...")
            if mode == "compounding":
                if compounding_resume_session_id:
                    print_info(
                        f"Compounding resume: continuing from checkpoint "
                        f"(session {compounding_resume_session_id})"
                    )
                else:
                    print_info(
                        "Compounding mode: ACs run strictly serially; each AC "
                        "sees a postmortem of every prior AC"
                    )
            elif parallel:
                print_info("Parallel mode: independent ACs will run concurrently")
            else:
                print_info("Sequential mode: ACs will run one at a time")
            execute_kwargs: dict[str, Any] = {
                "seed": seed,
                "execution_id": execution_id,
                "session_id": session_id_for_run,
                "parallel": parallel,
                "mode": mode,
            }
            if externally_satisfied_acs:
                execute_kwargs["externally_satisfied_acs"] = externally_satisfied_acs
            if compounding_resume_session_id is not None:
                execute_kwargs["resume_session_id"] = compounding_resume_session_id
            result = await runner.execute_seed(**execute_kwargs)

        # Handle result
        if result.is_ok:
            res = result.value
            if res.success:
                print_success("Execution completed successfully!")
                print_info(f"Session ID: {res.session_id}")
                print_info(f"Messages processed: {res.messages_processed}")
                print_info(f"Duration: {res.duration_seconds:.1f}s")

                # Post-execution QA
                if not no_qa:
                    from ouroboros.mcp.tools.qa import QAHandler

                    print_info("Running post-execution QA...")
                    qa_handler = QAHandler()
                    quality_bar = _derive_quality_bar(seed)
                    execution_artifact = _get_verification_artifact(res.summary, res.final_message)
                    verification_working_dir = (
                        Path(workspace.effective_cwd) if workspace is not None else project_dir
                    )
                    try:
                        verification = await build_verification_artifacts(
                            res.execution_id,
                            execution_artifact,
                            verification_working_dir,
                        )
                        artifact = verification.artifact
                        reference = verification.reference
                    except Exception as e:
                        artifact = execution_artifact
                        reference = f"Verification artifact generation failed: {e}"

                    qa_result = await qa_handler.handle(
                        {
                            "artifact": artifact,
                            "artifact_type": "test_output",
                            "quality_bar": quality_bar,
                            "reference": reference,
                            "seed_content": yaml.dump(seed_data, default_flow_style=False),
                            "pass_threshold": 0.80,
                        }
                    )
                    if qa_result.is_ok:
                        console.print(qa_result.value.content[0].text)
                    else:
                        print_warning(f"QA evaluation skipped: {qa_result.error}")
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
            if debug:
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
            "--orchestrator/--no-orchestrator",
            "-o/-O",
            help="Use the agent-runtime orchestrator for execution. Enabled by default.",
        ),
    ] = True,
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
    debug: Annotated[
        bool,
        typer.Option("--debug", "-d", help="Show logs and agent thinking (verbose output)."),
    ] = False,
    sequential: Annotated[
        bool,
        typer.Option(
            "--sequential",
            "-s",
            help="Execute ACs sequentially instead of in parallel (default: parallel).",
        ),
    ] = False,
    compounding: Annotated[
        bool,
        typer.Option(
            "--compounding",
            help=(
                "Run ACs strictly one at a time with compounding context: each "
                "AC reads a postmortem of every prior AC (files changed, "
                "invariants, gotchas). Halts on first failure after retries. "
                "Pins CLAUDE.md into the system prompt for every AC."
            ),
        ),
    ] = False,
    runtime: Annotated[
        AgentRuntimeBackend | None,
        typer.Option(
            "--runtime",
            help="Agent runtime backend for orchestrator mode (claude, codex, opencode, or hermes).",
            case_sensitive=False,
        ),
    ] = None,
    no_qa: Annotated[
        bool,
        typer.Option(
            "--no-qa",
            help="Skip post-execution QA evaluation.",
        ),
    ] = False,
    max_decomposition_depth: Annotated[
        int | None,
        typer.Option(
            "--max-decomposition-depth",
            min=0,
            help=(
                "Maximum recursive AC decomposition depth. "
                "0 disables decomposition; 1 allows one split; default 2."
            ),
        ),
    ] = None,
    skip_completed: Annotated[
        str | None,
        typer.Option(
            "--skip-completed",
            help=(
                "Path to a YAML marker file listing already-satisfied top-level ACs. "
                "Entries use 1-based AC numbers under completed_acs."
            ),
        ),
    ] = None,
) -> None:
    """Execute a workflow from a seed file.

    Reads the seed YAML configuration and runs the Ouroboros workflow.
    Orchestrator mode is enabled by default.

    Use --no-orchestrator for legacy standard workflow mode.
    Use --resume to continue a previous session.
    Use --mcp-config to connect to external MCP servers for additional tools.

    Examples:

        # Run a workflow (shorthand -- orchestrator mode by default)
        ouroboros run seed.yaml

        # Explicit subcommand (equivalent)
        ouroboros run workflow seed.yaml

        # Legacy standard workflow mode
        ouroboros run seed.yaml --no-orchestrator

        # With MCP server integration
        ouroboros run seed.yaml --mcp-config mcp.yaml

        # Resume a previous session
        ouroboros run seed.yaml --resume orch_abc123

        # Use Codex CLI runtime
        ouroboros run seed.yaml --runtime codex

        # Use Hermes CLI runtime
        ouroboros run seed.yaml --runtime hermes

        # Debug output
        ouroboros run seed.yaml --debug

        # Skip post-execution QA
        ouroboros run seed.yaml --no-qa

        # Limit recursive decomposition depth
        ouroboros run seed.yaml --max-decomposition-depth 1

        # Skip ACs already satisfied by the working tree
        ouroboros run seed.yaml --skip-completed docs/completed.yaml
    """
    # Validate MCP config requires orchestrator mode
    if mcp_config and not orchestrator and not resume_session:
        print_warning("--mcp-config requires --orchestrator flag. Enabling orchestrator mode.")
        orchestrator = True

    # --compounding and --sequential are mutually exclusive. They mean
    # different things: --compounding runs a curated per-AC loop with a
    # rolling postmortem chain, while --sequential (legacy) just disables
    # the parallel executor's fan-out.
    if compounding and sequential:
        print_error("--compounding and --sequential are mutually exclusive; pick one.")
        raise typer.Exit(1)

    execution_mode = "compounding" if compounding else None

    # When --compounding and --resume are combined, the resume ID is used for
    # checkpoint-based compounding resume (passed as resume_session_id to
    # execute_serial).  This is mutually exclusive with plain orchestrator
    # session resume: --compounding --resume never calls runner.resume_session().
    compounding_resume: str | None = resume_session if compounding else None
    # For the orchestrator-level session resume path, only use resume_session
    # when NOT in compounding mode (compounding handles its own checkpoint resume).
    orchestrator_resume: str | None = resume_session if not compounding else None

    # Mutual exclusivity: --compounding --resume + --skip-completed don't mix.
    # Checkpoint resume already handles AC-skipping; warn and ignore --skip-completed.
    if compounding_resume and skip_completed:
        print_warning(
            "--skip-completed is ignored when using --compounding --resume "
            "(checkpoint resume already handles AC skipping)."
        )
        skip_completed = None

    # (b) Mutual exclusivity: --compounding --resume must reference a seed that was
    # already run.  Providing --resume with a fresh seed path (one that has no prior
    # compounding checkpoint) is contradictory and will never produce correct results.
    if compounding_resume:
        _seed_id_for_validation = _load_seed_id_from_yaml(seed_file)
        if _seed_id_for_validation:
            _resume_error = _validate_compounding_resume_not_fresh_seed(
                _seed_id_for_validation, compounding_resume
            )
            if _resume_error:
                print_error(_resume_error)
                raise typer.Exit(1)
        else:
            # Skipping the fresh-seed guard. _load_seed_id_from_yaml returns
            # None when the YAML is unreadable or has no metadata.seed_id; the
            # downstream Seed.from_dict parse in _run_orchestrator will surface
            # the real error if the file is structurally bad. Log so a
            # maintainer chasing "why didn't my --resume reject this seed?" can
            # find the breadcrumb.
            log.debug(
                "cli.run.compounding_resume.fresh_seed_validation_skipped",
                seed_file=str(seed_file),
                reason="_load_seed_id_from_yaml returned None (no metadata.seed_id or unreadable)",
            )

    if orchestrator or resume_session:
        # Orchestrator mode
        if resume_session and not orchestrator:
            console.print(
                "[yellow]Warning: --resume requires --orchestrator flag. "
                "Enabling orchestrator mode.[/yellow]"
            )
        try:
            asyncio.run(
                _run_orchestrator(
                    seed_file,
                    orchestrator_resume,
                    mcp_config,
                    mcp_tool_prefix,
                    debug,
                    parallel=not sequential,
                    no_qa=no_qa,
                    runtime_backend=runtime.value if runtime else None,
                    max_decomposition_depth=max_decomposition_depth,
                    skip_completed=skip_completed,
                    mode=execution_mode,
                    compounding_resume_session_id=compounding_resume,
                )
            )
        except (ValueError, NotImplementedError) as e:
            print_error(str(e))
            raise typer.Exit(1) from e
    else:
        # Standard workflow (placeholder)
        print_info(f"Would execute workflow from: {seed_file}")
        if dry_run:
            console.print("[muted]Dry run mode - no changes will be made[/]")
        if debug:
            console.print("[muted]Debug mode enabled[/]")


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


__all__ = [
    "app",
    "_validate_compounding_resume_not_fresh_seed",
    "_load_seed_id_from_yaml",
]
