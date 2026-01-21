"""Init command for starting interactive interview.

This command initiates the Big Bang phase interview process.
"""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.prompt import Confirm, Prompt

from ouroboros.bigbang.interview import InterviewEngine, MAX_INTERVIEW_ROUNDS
from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success
from ouroboros.providers.litellm_adapter import LiteLLMAdapter

app = typer.Typer(
    name="init",
    help="Start interactive interview to refine requirements.",
    no_args_is_help=False,
)


async def _run_interview(
    initial_context: str,
    resume_id: str | None = None,
    state_dir: Path | None = None,
) -> None:
    """Run the interview process.

    Args:
        initial_context: Initial context or idea for the interview.
        resume_id: Optional interview ID to resume.
        state_dir: Optional custom state directory.
    """
    # Initialize components
    llm_adapter = LiteLLMAdapter()
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
    console.print(
        "[bold cyan]Next steps:[/] Use the interview results to generate a Seed specification."
    )


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
) -> None:
    """Start an interactive interview to refine your requirements.

    This command initiates the Big Bang phase, which transforms vague ideas
    into clear, executable requirements through iterative questioning.

    Example:
        ouroboros init "I want to build a task management CLI tool"

        ouroboros init --resume interview_20260116_120000

        ouroboros init
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

    # Run interview
    try:
        asyncio.run(_run_interview(context or "", resume, state_dir))
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
