"""Init command for starting interactive interview.

This command initiates the Big Bang phase interview process.
Supports both LiteLLM (external API) and Claude Code (Max Plan) modes.
"""

import asyncio
from pathlib import Path
from typing import Annotated

from rich.prompt import Confirm, Prompt
import typer

from ouroboros.bigbang.ambiguity import AmbiguityScorer
from ouroboros.bigbang.interview import MAX_INTERVIEW_ROUNDS, InterviewEngine, InterviewState
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success, print_warning
from ouroboros.providers.base import LLMAdapter
from ouroboros.providers.litellm_adapter import LiteLLMAdapter

app = typer.Typer(
    name="init",
    help="Start interactive interview to refine requirements.",
    no_args_is_help=False,
)


def _get_adapter(use_orchestrator: bool) -> LLMAdapter:
    """Get the appropriate LLM adapter.

    Args:
        use_orchestrator: If True, use Claude Code (Max Plan). Otherwise LiteLLM.

    Returns:
        LLM adapter instance.
    """
    if use_orchestrator:
        from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter

        return ClaudeCodeAdapter()
    else:
        return LiteLLMAdapter()


async def _run_interview(
    initial_context: str,
    resume_id: str | None = None,
    state_dir: Path | None = None,
    use_orchestrator: bool = False,
) -> None:
    """Run the interview process.

    Args:
        initial_context: Initial context or idea for the interview.
        resume_id: Optional interview ID to resume.
        state_dir: Optional custom state directory.
        use_orchestrator: If True, use Claude Code (Max Plan) instead of LiteLLM.
    """
    # Initialize components
    llm_adapter = _get_adapter(use_orchestrator)
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
    console.print(
        f"[bold cyan]Interview Session: {state.interview_id}[/]",
    )
    console.print(f"[muted]Max rounds: {MAX_INTERVIEW_ROUNDS}[/]")
    console.print()

    # Interview loop
    while not state.is_complete:
        current_round = state.current_round_number
        console.print(
            f"[bold]Round {current_round}/{MAX_INTERVIEW_ROUNDS}[/]",
        )

        # Generate question
        with console.status(
            "[cyan]Generating question...[/]",
            spinner="dots",
        ):
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

        # Get user response
        response = Prompt.ask("[bold green]Your response[/]")

        if not response.strip():
            print_error("Response cannot be empty. Please try again.")
            continue

        # Record response
        record_result = await engine.record_response(state, response, question)
        if record_result.is_err:
            print_error(f"Failed to record response: {record_result.error.message}")
            continue

        state = record_result.value

        # Save state
        save_result = await engine.save_state(state)
        if save_result.is_err:
            print_error(f"Warning: Failed to save state: {save_result.error.message}")

        console.print()

        # Check if user wants to continue or finish early
        if not state.is_complete and current_round >= 3:
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
    seed_path = await _generate_seed_from_interview(state, llm_adapter)

    if seed_path is None:
        return

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
) -> Path | None:
    """Generate Seed from completed interview.

    Args:
        state: Completed interview state.
        llm_adapter: LLM adapter for scoring and generation.

    Returns:
        Path to generated seed file, or None if failed.
    """
    console.print()
    console.print("[bold cyan]Generating Seed specification...[/]")

    # Step 1: Calculate ambiguity score
    with console.status("[cyan]Calculating ambiguity score...[/]", spinner="dots"):
        scorer = AmbiguityScorer(llm_adapter=llm_adapter)
        score_result = await scorer.score(state)

    if score_result.is_err:
        print_error(f"Failed to calculate ambiguity: {score_result.error.message}")
        return None

    ambiguity_score = score_result.value
    console.print(f"[muted]Ambiguity score: {ambiguity_score.overall_score:.2f}[/]")

    if not ambiguity_score.is_ready_for_seed:
        print_warning(
            f"Ambiguity score ({ambiguity_score.overall_score:.2f}) is too high. "
            "Consider more interview rounds to clarify requirements."
        )
        should_force = Confirm.ask(
            "[yellow]Generate Seed anyway?[/]",
            default=False,
        )
        if not should_force:
            return None

    # Step 2: Generate Seed
    with console.status("[cyan]Generating Seed from interview...[/]", spinner="dots"):
        generator = SeedGenerator(llm_adapter=llm_adapter)
        # For forced generation, we need to bypass the threshold check
        if ambiguity_score.is_ready_for_seed:
            seed_result = await generator.generate(state, ambiguity_score)
        else:
            # Create a modified score that passes threshold for forced generation
            from ouroboros.bigbang.ambiguity import AmbiguityScore as AmbScore

            forced_score = AmbScore(
                overall_score=0.19,  # Just under threshold
                breakdown=ambiguity_score.breakdown,
            )
            seed_result = await generator.generate(state, forced_score)

    if seed_result.is_err:
        print_error(f"Failed to generate Seed: {seed_result.error.message}")
        return None

    seed = seed_result.value

    # Step 3: Save Seed
    seed_path = Path.home() / ".ouroboros" / "seeds" / f"{seed.metadata.seed_id}.yaml"
    save_result = await generator.save_seed(seed, seed_path)

    if save_result.is_err:
        print_error(f"Failed to save Seed: {save_result.error.message}")
        return None

    print_success(f"Seed generated: {seed_path}")
    return seed_path


async def _start_workflow(seed_path: Path, use_orchestrator: bool = False) -> None:
    """Start workflow from generated seed.

    Args:
        seed_path: Path to the seed YAML file.
        use_orchestrator: Whether to use Claude Code orchestrator.
    """
    console.print()
    console.print("[bold cyan]Starting workflow...[/]")

    if use_orchestrator:
        # Direct function call instead of subprocess
        from ouroboros.cli.commands.run import _run_orchestrator

        try:
            await _run_orchestrator(seed_path, resume_session=None)
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
        console.print(
            "[bold cyan]Welcome to Ouroboros Interview![/]",
        )
        console.print()
        console.print(
            "This interactive process will help refine your ideas into clear requirements.",
        )
        console.print(
            f"You'll be asked up to {MAX_INTERVIEW_ROUNDS} questions to reduce ambiguity.",
        )
        console.print()

        context = Prompt.ask(
            "[bold]What would you like to build?[/]",
        )

    if not resume and not context:
        print_error("Initial context is required when not resuming.")
        raise typer.Exit(code=1)

    # Show mode info
    if orchestrator:
        print_info("Using Claude Code (Max Plan) - no API key required")
    else:
        print_info("Using LiteLLM - API key required")

    # Run interview
    try:
        asyncio.run(_run_interview(context or "", resume, state_dir, orchestrator))
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
