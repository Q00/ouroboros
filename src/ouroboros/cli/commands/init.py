"""Init command for starting interactive interview.

This command initiates the Big Bang phase interview process.
Supports both LiteLLM (external API) and Claude Code (Max Plan) modes.
"""

import asyncio
from enum import Enum, auto
from pathlib import Path
from typing import Annotated

import click
from rich.prompt import Confirm, Prompt
import typer
import yaml

from ouroboros.bigbang.ambiguity import AmbiguityScorer
from ouroboros.bigbang.interview import (
    MIN_ROUNDS_BEFORE_EARLY_EXIT,
    InterviewEngine,
    InterviewState,
    InterviewStatus,
)
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success, print_warning
from ouroboros.cli.formatters.prompting import multiline_prompt_async
from ouroboros.config import get_clarification_model
from ouroboros.core.initial_context import (
    load_pm_seed_as_context as _load_pm_seed_as_context_result,
)
from ouroboros.core.initial_context import (
    resolve_initial_context_input,
)
from ouroboros.observability import LoggingConfig, configure_logging
from ouroboros.providers import create_llm_adapter
from ouroboros.providers.base import LLMAdapter


class SeedGenerationResult(Enum):
    """Result of seed generation attempt."""

    SUCCESS = auto()
    CANCELLED = auto()
    CONTINUE_INTERVIEW = auto()


class AgentRuntimeBackend(str, Enum):  # noqa: UP042
    """Supported orchestrator runtime backends for workflow handoff."""

    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"


class LLMBackend(str, Enum):  # noqa: UP042
    """Supported interview/seed LLM backends."""

    CLAUDE_CODE = "claude_code"
    LITELLM = "litellm"
    CODEX = "codex"
    OPENCODE = "opencode"


class _DefaultStartGroup(typer.core.TyperGroup):
    """TyperGroup that falls back to 'start' when no subcommand matches.

    This enables the shorthand `ouroboros init "Build a REST API"` which is
    equivalent to `ouroboros init start "Build a REST API"`.
    """

    default_cmd_name: str = "start"

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = [self.default_cmd_name, *args]
        return super().parse_args(ctx, args)


app = typer.Typer(
    name="init",
    help="Start interactive interview to refine requirements.",
    no_args_is_help=False,
    cls=_DefaultStartGroup,
)


def _make_message_callback(debug: bool):
    """Create message callback for streaming output.

    Args:
        debug: If True, show thinking and tool use.

    Returns:
        Callback function or None.
    """
    if not debug:
        return None

    def callback(msg_type: str, content: str) -> None:
        if msg_type == "thinking":
            # Take first line only, truncate if needed
            first_line = content.split("\n")[0].strip()
            display = first_line[:100] + "..." if len(first_line) > 100 else first_line
            if display:
                console.print(f"  [dim]💭 {display}[/dim]")
        elif msg_type == "tool":
            # Tool info now includes details like "Read: /path/to/file"
            console.print(f"  [yellow]🔧 {content}[/yellow]")

    return callback


def _get_adapter(
    use_orchestrator: bool,
    backend: str | None = None,
    for_interview: bool = False,
    debug: bool = False,
) -> LLMAdapter:
    """Get the appropriate LLM adapter.

    Args:
        use_orchestrator: If True, default to Claude Code for compatibility.
        backend: Optional explicit LLM backend override.
        for_interview: If True, enable Read/Glob/Grep tools for codebase exploration.
        debug: If True, show streaming messages (thinking, tool use).

    Returns:
        LLM adapter instance.
    """
    resolved_backend = backend or ("claude_code" if use_orchestrator else "litellm")

    if for_interview:
        # Interview mode: request the interview-specific permission policy and
        # debug/tool callback behavior across all backends that support it.
        return create_llm_adapter(
            backend=resolved_backend,
            use_case="interview",
            allowed_tools=None,
            max_turns=5,
            on_message=_make_message_callback(debug),
            cwd=Path.cwd(),
        )

    return create_llm_adapter(backend=resolved_backend, cwd=Path.cwd())


async def _run_interview_loop(
    engine: InterviewEngine,
    state: InterviewState,
) -> InterviewState:
    """Run the interview question loop until completion or user exit.

    Implements tiered confirmation:
    - Rounds 1-3: Auto-continue (minimum context)
    - Rounds 4-15: Ask "Continue?" after each round
    - Rounds 16+: Ask "Continue?" with diminishing returns warning

    Args:
        engine: Interview engine instance.
        state: Current interview state.

    Returns:
        Updated interview state.
    """
    while not state.is_complete:
        current_round = state.current_round_number
        console.print(f"[bold]Round {current_round}[/]")

        # Generate question
        with console.status("[cyan]Generating question...[/]", spinner="dots"):
            question_result = await engine.ask_next_question(state)

        if question_result.is_err:
            print_error(f"Failed to generate question: {question_result.error.message}")
            should_retry = Confirm.ask("Retry?", default=True)
            if not should_retry:
                break
            continue

        question = question_result.value

        # Display question
        console.print()
        console.print(f"[bold yellow]Q:[/] {question}")
        console.print()

        # Get user response (multiline-safe for paste)
        response = await multiline_prompt_async("Your response")

        if not response.strip():
            print_error("Response cannot be empty. Please try again.")
            continue

        # Record response
        record_result = await engine.record_response(state, response, question)
        if record_result.is_err:
            print_error(f"Failed to record response: {record_result.error.message}")
            continue

        state = record_result.value

        # Save state immediately after recording
        save_result = await engine.save_state(state)
        if save_result.is_err:
            print_error(f"Warning: Failed to save state: {save_result.error.message}")

        console.print()

        # Tiered confirmation logic
        if current_round >= MIN_ROUNDS_BEFORE_EARLY_EXIT:
            should_continue = Confirm.ask(
                "Continue with more questions?",
                default=True,
            )
            if not should_continue:
                complete_result = await engine.complete_interview(state)
                if complete_result.is_ok:
                    state = complete_result.value
                await engine.save_state(state)
                break

    return state


async def _run_interview(
    initial_context: str,
    resume_id: str | None = None,
    state_dir: Path | None = None,
    use_orchestrator: bool = False,
    debug: bool = False,
    workflow_runtime_backend: str | None = None,
    llm_backend: str | None = None,
) -> None:
    """Run the interview process.

    Args:
        initial_context: Initial context or idea for the interview.
        resume_id: Optional interview ID to resume.
        state_dir: Optional custom state directory.
        use_orchestrator: If True, use Claude Code (Max Plan) instead of LiteLLM.
        workflow_runtime_backend: Optional agent runtime backend for the workflow handoff.
        llm_backend: Optional LLM backend override for interview and seed generation.
    """
    # Initialize components
    llm_adapter = _get_adapter(
        use_orchestrator,
        backend=llm_backend,
        for_interview=True,
        debug=debug,
    )
    engine = InterviewEngine(
        llm_adapter=llm_adapter,
        state_dir=state_dir or Path.home() / ".ouroboros" / "data",
        model=get_clarification_model(llm_backend),
    )

    # Load or start interview
    if resume_id:
        print_info(f"Resuming interview: {resume_id}")
        state_result = await engine.load_state(resume_id)
        if state_result.is_err:
            print_error(f"Failed to load interview: {state_result.error.message}")
            raise typer.Exit(code=1)
        state = state_result.value
    else:
        print_info("Starting new interview session...")
        state_result = await engine.start_interview(initial_context)
        if state_result.is_err:
            print_error(f"Failed to start interview: {state_result.error.message}")
            raise typer.Exit(code=1)
        state = state_result.value

    console.print()
    console.print(f"[bold cyan]Interview Session: {state.interview_id}[/]")
    console.print("[muted]No round limit - you decide when to stop[/]")
    console.print()

    # Run initial interview loop
    state = await _run_interview_loop(engine, state)

    # Outer loop for retry on high ambiguity
    while True:
        # Interview complete
        console.print()
        print_success("Interview completed!")
        console.print(f"[muted]Total rounds: {len(state.rounds)}[/]")
        console.print(f"[muted]Interview ID: {state.interview_id}[/]")

        # Save final state
        save_result = await engine.save_state(state)
        if save_result.is_ok:
            console.print(f"[muted]State saved to: {save_result.value}[/]")

        console.print()

        # Ask if user wants to proceed to Seed generation
        should_generate_seed = Confirm.ask(
            "[bold cyan]Proceed to generate Seed specification?[/]",
            default=True,
        )

        if not should_generate_seed:
            console.print(
                "[muted]You can resume later with:[/] "
                f"[bold]ouroboros init start --resume {state.interview_id}[/]"
            )
            return

        # Generate Seed
        seed_path, result = await _generate_seed_from_interview(state, llm_adapter, llm_backend)

        if result == SeedGenerationResult.CONTINUE_INTERVIEW:
            # Re-open interview for more questions
            console.print()
            print_info("Continuing interview to reduce ambiguity...")
            state.status = InterviewStatus.IN_PROGRESS
            await engine.save_state(state)  # Save status change immediately

            # Continue interview loop (reusing the same helper)
            state = await _run_interview_loop(engine, state)
            continue

        if result == SeedGenerationResult.CANCELLED:
            return

        # Success - proceed to workflow
        break

    # Ask if user wants to start workflow
    console.print()
    should_start_workflow = Confirm.ask(
        "[bold cyan]Start workflow now?[/]",
        default=True,
    )

    if should_start_workflow:
        await _start_workflow(
            seed_path,
            use_orchestrator,
            runtime_backend=workflow_runtime_backend,
        )


async def _generate_seed_from_interview(
    state: InterviewState,
    llm_adapter: LLMAdapter,
    llm_backend: str | None = None,
) -> tuple[Path | None, SeedGenerationResult]:
    """Generate Seed from completed interview.

    Args:
        state: Completed interview state.
        llm_adapter: LLM adapter for scoring and generation.

    Returns:
        Tuple of (path to generated seed file or None, result status).
    """
    console.print()
    console.print("[bold cyan]Generating Seed specification...[/]")

    # Step 1: Calculate ambiguity score
    with console.status("[cyan]Calculating ambiguity score...[/]", spinner="dots"):
        scorer = AmbiguityScorer(
            llm_adapter=llm_adapter,
            model=get_clarification_model(llm_backend),
        )
        score_result = await scorer.score(state)

    if score_result.is_err:
        print_error(f"Failed to calculate ambiguity: {score_result.error.message}")
        return None, SeedGenerationResult.CANCELLED

    ambiguity_score = score_result.value
    console.print(f"[muted]Ambiguity score: {ambiguity_score.overall_score:.2f}[/]")

    if not ambiguity_score.is_ready_for_seed:
        print_warning(
            f"Ambiguity score ({ambiguity_score.overall_score:.2f}) is too high. "
            "Consider more interview rounds to clarify requirements."
        )
        console.print()
        console.print("[bold]What would you like to do?[/]")
        console.print("  [cyan]1[/] - Continue interview with more questions")
        console.print("  [cyan]2[/] - Generate Seed anyway (force)")
        console.print("  [cyan]3[/] - Cancel")
        console.print()

        choice = Prompt.ask(
            "[yellow]Select option[/]",
            choices=["1", "2", "3"],
            default="1",
        )

        if choice == "1":
            return None, SeedGenerationResult.CONTINUE_INTERVIEW
        elif choice == "3":
            return None, SeedGenerationResult.CANCELLED
        # choice == "2" falls through to generate anyway

    # Step 2: Generate Seed
    with console.status("[cyan]Generating Seed from interview...[/]", spinner="dots"):
        generator = SeedGenerator(
            llm_adapter=llm_adapter,
            model=get_clarification_model(llm_backend),
        )
        # For forced generation, we need to bypass the threshold check
        if ambiguity_score.is_ready_for_seed:
            seed_result = await generator.generate(state, ambiguity_score)
        else:
            # TODO: Add force=True parameter to SeedGenerator.generate() instead of this hack
            # Creating a modified score to bypass threshold check
            from ouroboros.bigbang.ambiguity import AmbiguityScore as AmbScore

            FORCED_SCORE_VALUE = 0.19  # Just under threshold (0.2)
            forced_score = AmbScore(
                overall_score=FORCED_SCORE_VALUE,
                breakdown=ambiguity_score.breakdown,
            )
            seed_result = await generator.generate(state, forced_score)

    if seed_result.is_err:
        print_error(f"Failed to generate Seed: {seed_result.error.message}")
        return None, SeedGenerationResult.CANCELLED

    seed = seed_result.value

    # Step 3: Save Seed
    seed_path = Path.home() / ".ouroboros" / "seeds" / f"{seed.metadata.seed_id}.yaml"
    save_result = await generator.save_seed(seed, seed_path)

    if save_result.is_err:
        print_error(f"Failed to save Seed: {save_result.error.message}")
        return None, SeedGenerationResult.CANCELLED

    print_success(f"Seed generated: {seed_path}")
    return seed_path, SeedGenerationResult.SUCCESS


async def _start_workflow(
    seed_path: Path,
    use_orchestrator: bool = False,
    parallel: bool = True,
    runtime_backend: str | None = None,
) -> None:
    """Start workflow from generated seed.

    Args:
        seed_path: Path to the seed YAML file.
        use_orchestrator: Whether to use Claude Code orchestrator.
        parallel: Execute independent ACs in parallel. Default: True.
        runtime_backend: Optional runtime backend for orchestrator execution.
    """
    console.print()
    console.print("[bold cyan]Starting workflow...[/]")

    if use_orchestrator:
        # Direct function call instead of subprocess
        from ouroboros.cli.commands.run import _run_orchestrator

        try:
            await _run_orchestrator(
                seed_path,
                resume_session=None,
                parallel=parallel,
                runtime_backend=runtime_backend,
            )
        except typer.Exit:
            pass  # Normal exit
        except KeyboardInterrupt:
            print_info("Workflow interrupted.")
    else:
        # Standard workflow (placeholder for now)
        print_info(f"Would execute workflow from: {seed_path}")
        print_info("Standard workflow execution not yet implemented.")


def _find_pm_seeds(seeds_dir: Path | None = None) -> list[Path]:
    """Find all pm_seed YAML files in the seeds directory.

    Args:
        seeds_dir: Directory to scan. Defaults to ~/.ouroboros/seeds/.

    Returns:
        List of paths to pm_seed files (JSON or YAML), sorted by modification time (newest first).
    """
    seeds_dir = seeds_dir or Path.home() / ".ouroboros" / "seeds"
    if not seeds_dir.is_dir():
        return []
    # Support both JSON (new) and YAML (legacy) PM seed formats
    pm_seeds = sorted(
        list(seeds_dir.glob("pm_seed_*.json")) + list(seeds_dir.glob("pm_seed_*.yaml")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return pm_seeds


def _has_dev_seed(seeds_dir: Path | None = None) -> bool:
    """Check if any dev seed (non-PM) exists in the seeds directory.

    Looks for seed.json or any YAML seed file that is NOT a pm_seed.

    Args:
        seeds_dir: Directory to check. Defaults to ~/.ouroboros/seeds/.

    Returns:
        True if a dev seed file exists.
    """
    seeds_dir = seeds_dir or Path.home() / ".ouroboros" / "seeds"
    if not seeds_dir.is_dir():
        return False
    # Check for seed.json
    if (seeds_dir / "seed.json").exists():
        return True
    # Check for any non-pm seed YAML files
    return any(not yaml_file.name.startswith("pm_seed_") for yaml_file in seeds_dir.glob("*.yaml"))


def _display_pm_seed_info(seed_path: Path) -> dict[str, str]:
    """Read and display summary info for a PM seed file.

    Args:
        seed_path: Path to the pm_seed YAML file.

    Returns:
        Dict with 'name', 'goal', and 'pm_id' extracted from the file.
        Falls back to defaults if the file is malformed.
    """
    try:
        with open(seed_path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError("PM seed file is not a YAML mapping")
        name = data.get("product_name", "") or "Unnamed"
        goal = data.get("goal", "") or "No goal specified"
        pm_id = data.get("pm_id", seed_path.stem)
    except (yaml.YAMLError, OSError, ValueError, AttributeError):
        name = seed_path.stem
        goal = "No goal specified"
        pm_id = seed_path.stem
    return {"name": name, "goal": goal, "pm_id": pm_id}


def _notify_pm_seed_detected(pm_seeds: list[Path]) -> None:
    """Display a prominent notification that PM seed(s) were auto-detected.

    Shows a bordered panel with seed details so the user clearly sees
    that PM output is available for use as dev interview context.

    Args:
        pm_seeds: List of detected PM seed file paths.
    """
    console.print()
    console.print("[bold cyan]╔══════════════════════════════════════════════╗[/]")
    console.print(
        "[bold cyan]║[/]  [bold yellow]PM Seed Auto-Detected[/]                      [bold cyan]║[/]"
    )
    console.print("[bold cyan]╚══════════════════════════════════════════════╝[/]")
    console.print()

    for seed_path in pm_seeds:
        info = _display_pm_seed_info(seed_path)
        goal_display = info["goal"][:80] + "..." if len(info["goal"]) > 80 else info["goal"]
        console.print(f"  [bold]{info['name']}[/] [dim]({info['pm_id']})[/]")
        console.print(f"  [dim]{goal_display}[/]")
        console.print()

    console.print(
        "[dim]A PM seed contains product requirements from a prior PM interview.\n"
        "Using it as initial context gives the dev interview a head start.[/]"
    )
    console.print()


def _prompt_pm_seed_selection(pm_seeds: list[Path]) -> Path | None:
    """Prompt user to select a PM seed to use as initial context.

    Shows a notification banner, lists available seeds, and asks the user
    to pick one or skip. For a single seed, offers a simple yes/no confirmation.

    Args:
        pm_seeds: List of available PM seed paths.

    Returns:
        Selected PM seed path, or None if user declines.
    """
    _notify_pm_seed_detected(pm_seeds)

    if len(pm_seeds) == 1:
        # Single seed — simple yes/no confirmation
        use_it = Confirm.ask(
            "[yellow]Use this PM seed as initial context for the dev interview?[/]",
            default=True,
        )
        return pm_seeds[0] if use_it else None

    # Multiple seeds — numbered selection
    console.print("[bold]Available PM seeds:[/]")
    console.print()
    for i, seed_path in enumerate(pm_seeds, 1):
        info = _display_pm_seed_info(seed_path)
        goal_display = info["goal"][:80] + "..." if len(info["goal"]) > 80 else info["goal"]
        console.print(f"  [cyan]{i}[/] - [bold]{info['name']}[/] ({info['pm_id']})")
        console.print(f"      {goal_display}")
    console.print("  [cyan]0[/] - Skip (start fresh interview)")
    console.print()

    choice = Prompt.ask(
        "[yellow]Select PM seed[/]",
        choices=[str(i) for i in range(len(pm_seeds) + 1)],
        default="1",
    )

    idx = int(choice)
    if idx == 0:
        return None
    return pm_seeds[idx - 1]


def _load_pm_seed_as_context(seed_path: Path) -> str:
    """Load a PM seed YAML and convert to initial_context string.

    Args:
        seed_path: Path to the pm_seed YAML file.

    Returns:
        YAML-formatted string for use as dev interview initial_context.
    """
    result = _load_pm_seed_as_context_result(seed_path)
    if result.is_err:
        raise ValueError(str(result.error))
    return result.value


@app.command()
def start(
    context: Annotated[
        str | None,
        typer.Argument(help="Initial context or idea (interactive prompt if not provided)."),
    ] = None,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume",
            "-r",
            help="Resume an existing interview by ID.",
        ),
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
    orchestrator: Annotated[
        bool,
        typer.Option(
            "--orchestrator",
            "-o",
            help="Use Claude Code (Max Plan) instead of LiteLLM. No API key required.",
        ),
    ] = False,
    runtime: Annotated[
        AgentRuntimeBackend | None,
        typer.Option(
            "--runtime",
            help=(
                "Agent runtime backend for the workflow execution step after seed generation "
                "(claude, codex, or opencode)."
            ),
            case_sensitive=False,
        ),
    ] = None,
    llm_backend: Annotated[
        LLMBackend | None,
        typer.Option(
            "--llm-backend",
            help=(
                "LLM backend for interview, ambiguity scoring, and seed generation "
                "(claude_code, litellm, codex, or opencode)."
            ),
            case_sensitive=False,
        ),
    ] = None,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            "-d",
            help="Show verbose logs including debug messages.",
        ),
    ] = False,
) -> None:
    """Start an interactive interview to refine your requirements.

    This command initiates the Big Bang phase, which transforms vague ideas
    into clear, executable requirements through iterative questioning.

    Example:
        ouroboros init start "I want to build a task management CLI tool"

        ouroboros init start --orchestrator "Build a REST API"

        ouroboros init start --orchestrator --runtime codex "Build a REST API"

        ouroboros init start --llm-backend codex "Build a REST API"

        ouroboros init start --resume interview_20260116_120000

        ouroboros init start
    """
    # Get initial context if not provided
    if not resume:
        # Auto-detect PM seeds and offer to use as context
        seeds_dir = Path.home() / ".ouroboros" / "seeds"
        if not _has_dev_seed(seeds_dir):
            pm_seeds = _find_pm_seeds(seeds_dir)
            if pm_seeds:
                if context:
                    # User provided context but PM seed exists — notify and ask
                    _notify_pm_seed_detected(pm_seeds)
                    use_pm = Confirm.ask(
                        "[yellow]Use PM seed instead of the provided context?[/]",
                        default=False,
                    )
                    if use_pm:
                        selected = (
                            _prompt_pm_seed_selection(pm_seeds)
                            if len(pm_seeds) > 1
                            else pm_seeds[0]
                        )
                        if selected:
                            context = _load_pm_seed_as_context(selected)
                            print_success(f"Using PM seed: {selected.name}")
                else:
                    # No context provided — offer PM seed as primary option
                    selected = _prompt_pm_seed_selection(pm_seeds)
                    if selected:
                        context = _load_pm_seed_as_context(selected)
                        print_success(f"Using PM seed: {selected.name}")

        if not context:
            console.print("[bold cyan]Welcome to Ouroboros Interview![/]")
            console.print()
            console.print(
                "This interactive process will help refine your ideas into clear requirements.",
            )
            console.print(
                "You control when to stop - no arbitrary round limit.",
            )
            console.print()

            context = asyncio.run(multiline_prompt_async("What would you like to build?"))

        if context:
            resolved_context = resolve_initial_context_input(context, cwd=Path.cwd())
            if resolved_context.is_err:
                print_error(str(resolved_context.error))
                raise typer.Exit(code=1)
            context = resolved_context.value

    if not resume and not context:
        print_error("Initial context is required when not resuming.")
        raise typer.Exit(code=1)

    # Configure logging based on debug flag
    if debug:
        configure_logging(LoggingConfig(log_level="DEBUG"))
        print_info("Debug mode enabled - showing verbose logs")

    if runtime and not orchestrator:
        print_warning(
            "--runtime only affects the workflow execution step when --orchestrator is enabled."
        )

    # Show mode info
    if orchestrator:
        print_info("Using Claude Code (Max Plan) - no API key required")
        if runtime:
            print_info(f"Workflow runtime backend: {runtime.value}")
    else:
        print_info("Using LiteLLM - API key required")

    if llm_backend:
        print_info(f"Interview LLM backend: {llm_backend.value}")

    # Run interview
    try:
        asyncio.run(
            _run_interview(
                context or "",
                resume,
                state_dir,
                orchestrator,
                debug,
                runtime.value if runtime else None,
                llm_backend.value if llm_backend else None,
            )
        )
    except KeyboardInterrupt:
        console.print()
        print_info("Interview interrupted. Progress has been saved.")
        raise typer.Exit(code=0)
    except Exception as e:
        print_error(f"Interview failed: {e}")
        raise typer.Exit(code=1)


@app.command("list")
def list_interviews(
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
) -> None:
    """List all interview sessions."""
    llm_adapter = create_llm_adapter(backend="litellm")
    engine = InterviewEngine(
        llm_adapter=llm_adapter,
        state_dir=state_dir or Path.home() / ".ouroboros" / "data",
    )

    interviews = asyncio.run(engine.list_interviews())

    if not interviews:
        print_info("No interviews found.")
        return

    console.print("[bold cyan]Interview Sessions:[/]")
    console.print()

    for interview in interviews:
        status_color = "green" if interview["status"] == "completed" else "yellow"
        console.print(
            f"[bold]{interview['interview_id']}[/] "
            f"[{status_color}]{interview['status']}[/] "
            f"({interview['rounds']} rounds)"
        )
        console.print(f"  Updated: {interview['updated_at']}")
        console.print()


__all__ = ["app"]
