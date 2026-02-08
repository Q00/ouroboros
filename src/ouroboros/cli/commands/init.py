"""Init command for starting interactive interview.

This command initiates the Big Bang phase interview process.
Supports both LiteLLM (external API) and Claude Code (Max Plan) modes.
"""

import asyncio
from enum import Enum, auto
from pathlib import Path
from typing import Annotated

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from rich.prompt import Confirm, Prompt
import typer

from ouroboros.bigbang.ambiguity import AmbiguityScorer
from ouroboros.bigbang.interview import (
    MIN_ROUNDS_BEFORE_EARLY_EXIT,
    SOFT_LIMIT_WARNING_THRESHOLD,
    InterviewEngine,
    InterviewState,
    InterviewStatus,
)
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success, print_warning
from ouroboros.observability import LoggingConfig, configure_logging
from ouroboros.providers.base import LLMAdapter
from ouroboros.providers.litellm_adapter import LiteLLMAdapter


class SeedGenerationResult(Enum):
    """Result of seed generation attempt."""

    SUCCESS = auto()
    CANCELLED = auto()
    CONTINUE_INTERVIEW = auto()


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


async def _multiline_prompt_async(prompt_text: str) -> str:
    """Get multiline input with proper paste handling.

    Behavior:
    - Enter: Submit input
    - Ctrl+J: Insert newline
    - Paste: Multiline text is preserved (via bracketed paste mode)

    Args:
        prompt_text: The prompt to display.

    Returns:
        The user's input (may contain newlines from paste).

    Raises:
        EOFError: If end-of-file is reached (e.g., stdin closed).
        KeyboardInterrupt: If user presses Ctrl+C.
    """
    bindings = KeyBindings()

    @bindings.add("c-j")
    def insert_newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("c-m")
    def submit(event: KeyPressEvent) -> None:
        event.current_buffer.validate_and_handle()

    console.print(f"[bold green]{prompt_text}[/] [dim](Enter: submit, Ctrl+J: newline)[/]")

    session: PromptSession[str] = PromptSession(
        message="> ",
        multiline=True,
        prompt_continuation="  ",
        key_bindings=bindings,
    )

    return await session.prompt_async()


def _get_adapter(
    use_orchestrator: bool,
    for_interview: bool = False,
    debug: bool = False,
) -> LLMAdapter:
    """Get the appropriate LLM adapter.

    Args:
        use_orchestrator: If True, use Claude Code (Max Plan). Otherwise LiteLLM.
        for_interview: If True, enable Read/Glob/Grep tools for codebase exploration.
        debug: If True, show streaming messages (thinking, tool use).

    Returns:
        LLM adapter instance.
    """
    if use_orchestrator:
        from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter

        if for_interview:
            # Interview mode: permissive - allow MCP, read tools, etc.
            # Only dangerous tools (Write, Edit, Bash, Task) are blocked
            return ClaudeCodeAdapter(
                permission_mode="bypassPermissions",  # Auto-approve tool use
                allowed_tools=None,  # Permissive mode: MCP + read-only tools
                max_turns=5,  # Allow more turns for MCP tool use
                on_message=_make_message_callback(debug),
            )
        return ClaudeCodeAdapter()
    else:
        return LiteLLMAdapter()


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
        response = await _multiline_prompt_async("Your response")

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
            # Show warning for rounds beyond soft limit
            if current_round >= SOFT_LIMIT_WARNING_THRESHOLD:
                print_warning(
                    f"Round {current_round}: Diminishing returns expected. "
                    "Consider generating Seed to check ambiguity score."
                )

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
) -> None:
    """Run the interview process.

    Args:
        initial_context: Initial context or idea for the interview.
        resume_id: Optional interview ID to resume.
        state_dir: Optional custom state directory.
        use_orchestrator: If True, use Claude Code (Max Plan) instead of LiteLLM.
    """
    # Initialize components
    llm_adapter = _get_adapter(use_orchestrator, for_interview=True, debug=debug)
    engine = InterviewEngine(
        llm_adapter=llm_adapter,
        state_dir=state_dir or Path.home() / ".ouroboros" / "data",
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
        seed_path, result = await _generate_seed_from_interview(state, llm_adapter)

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
        await _start_workflow(seed_path, use_orchestrator)


async def _generate_seed_from_interview(
    state: InterviewState,
    llm_adapter: LLMAdapter,
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
        scorer = AmbiguityScorer(llm_adapter=llm_adapter)
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
        generator = SeedGenerator(llm_adapter=llm_adapter)
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


async def _start_workflow(seed_path: Path, use_orchestrator: bool = False, parallel: bool = True) -> None:
    """Start workflow from generated seed.

    Args:
        seed_path: Path to the seed YAML file.
        use_orchestrator: Whether to use Claude Code orchestrator.
        parallel: Execute independent ACs in parallel. Default: True.
    """
    console.print()
    console.print("[bold cyan]Starting workflow...[/]")

    if use_orchestrator:
        # Direct function call instead of subprocess
        from ouroboros.cli.commands.run import _run_orchestrator

        try:
            await _run_orchestrator(seed_path, resume_session=None, parallel=parallel)
        except typer.Exit:
            pass  # Normal exit
        except KeyboardInterrupt:
            print_info("Workflow interrupted.")
    else:
        # Standard workflow (placeholder for now)
        print_info(f"Would execute workflow from: {seed_path}")
        print_info("Standard workflow execution not yet implemented.")


@app.command()
def start(
    context: Annotated[
        str | None,
        typer.Argument(
            help="Initial context or idea (interactive prompt if not provided)."
        ),
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

        ouroboros init start --resume interview_20260116_120000

        ouroboros init start
    """
    # Get initial context if not provided
    if not resume and not context:
        console.print("[bold cyan]Welcome to Ouroboros Interview![/]")
        console.print()
        console.print(
            "This interactive process will help refine your ideas into clear requirements.",
        )
        console.print(
            "You control when to stop - no arbitrary round limit.",
        )
        console.print()

        context = asyncio.run(_multiline_prompt_async("What would you like to build?"))

    if not resume and not context:
        print_error("Initial context is required when not resuming.")
        raise typer.Exit(code=1)

    # Configure logging based on debug flag
    if debug:
        configure_logging(LoggingConfig(log_level="DEBUG"))
        print_info("Debug mode enabled - showing verbose logs")

    # Show mode info
    if orchestrator:
        print_info("Using Claude Code (Max Plan) - no API key required")
    else:
        print_info("Using LiteLLM - API key required")

    # Run interview
    try:
        asyncio.run(_run_interview(context or "", resume, state_dir, orchestrator, debug))
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
    llm_adapter = LiteLLMAdapter()
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
