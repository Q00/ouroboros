"""PRD command for generating Product Requirements Documents.

This command initiates a guided interview process for PMs to define
product requirements, with automatic classification of planning vs
development questions, producing a PRDSeed and human-readable PRD document.

Usage:
    ouroboros prd                    Start a new PRD interview
    ouroboros prd --resume <id>     Resume an existing PRD session
"""

import asyncio
import os
from pathlib import Path
from typing import Annotated

from rich.prompt import Confirm, Prompt
import typer

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success, print_warning

app = typer.Typer(
    name="prd",
    help="Generate a Product Requirements Document through guided interview.",
    no_args_is_help=False,
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def prd_command(
    ctx: typer.Context,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume",
            "-r",
            help="Resume an existing PRD interview session by ID.",
        ),
    ] = None,
    model: Annotated[
        str,
        typer.Option(
            "--model",
            "-m",
            help="LLM model to use for the PRD interview.",
        ),
    ] = "anthropic/claude-sonnet-4-20250514",
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Output path for the generated PRD document.",
        ),
    ] = None,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help="Enable debug output.",
        ),
    ] = False,
) -> None:
    """Start or resume a PRD interview to generate product requirements.

    This command guides PMs through a structured interview process to
    define product requirements. Questions are automatically classified
    as planning (PM-answerable) or development (deferred to dev interview).

    The output is a PRDSeed YAML file and a human-readable prd.md document.

    [bold]Examples:[/]

        ouroboros prd                        Start new PRD interview
        ouroboros prd --resume abc123        Resume session
        ouroboros prd --output ./my-prd.md   Custom output path
    """
    if ctx.invoked_subcommand is not None:
        return

    output_path = output or Path(".ouroboros/prd.md")

    console.print(
        "\n[bold cyan]Ouroboros PRD Generator[/] - Product Requirements Document\n"
    )

    if resume:
        print_info(f"Resuming PRD session: {resume}")
    else:
        print_info("Starting new PRD interview session...")

    console.print(
        f"  Model: [dim]{model}[/]\n"
        f"  Output: [dim]{output_path}[/]\n"
    )

    # PRDInterviewEngine integration point — the sibling agent is building
    # PRDInterviewEngine which will be wired in here.
    try:
        asyncio.run(
            _run_prd_interview(
                resume_id=resume,
                model=model,
                output_path=output_path,
                debug=debug,
            )
        )
    except KeyboardInterrupt:
        print_info("\nPRD interview interrupted. Progress has been saved.")
        raise typer.Exit(code=0)


def _check_brownfield(cwd: str | Path) -> list[dict[str, str]]:
    """Detect brownfield project in cwd and prompt user for confirmation.

    If `detect_brownfield(cwd)` finds recognised config files, the user
    is asked whether to register the current directory as a brownfield
    repository for codebase-aware interview context.

    When the user confirms, they are prompted for a project name and
    optional description, then the repo is persisted to
    ``~/.ouroboros/brownfield.json`` using the brownfield schema utilities.

    Args:
        cwd: Working directory to inspect.

    Returns:
        List of brownfield repo dicts (may include previously registered
        repos plus the newly confirmed one).
    """
    from ouroboros.bigbang.brownfield import (
        load_brownfield_repos_as_dicts,
        register_brownfield_repo,
    )
    from ouroboros.bigbang.explore import detect_brownfield

    # Load any previously registered repos via schema utilities
    existing_repos = load_brownfield_repos_as_dicts()

    # Check if cwd is already registered
    cwd_str = str(Path(cwd).resolve())
    already_registered = any(r.get("path") == cwd_str for r in existing_repos)

    if already_registered:
        print_info(f"Brownfield repo already registered: {cwd_str}")
        return existing_repos

    if not detect_brownfield(cwd):
        return existing_repos

    # Brownfield detected — inform and ask for confirmation
    console.print(
        f"\n[bold yellow]Brownfield project detected[/] in [cyan]{cwd_str}[/]"
    )
    console.print(
        "[dim]Config files found — this looks like an existing codebase.[/]\n"
    )

    should_register = Confirm.ask(
        "Register this directory as a brownfield repo for codebase-aware context?",
        default=True,
    )

    if not should_register:
        print_info("Skipping brownfield registration.")
        return existing_repos

    # Gather metadata from user
    name = Prompt.ask(
        "[yellow]Project name[/]",
        default=Path(cwd_str).name,
    )
    desc = Prompt.ask(
        "[yellow]Short description (optional)[/]",
        default="",
    )

    # Persist using brownfield schema utilities (validates + writes to brownfield.json)
    entries = register_brownfield_repo(
        path=cwd_str,
        name=name,
        desc=desc,
    )
    repos = [e.to_dict() for e in entries]

    print_success(f"Registered brownfield repo: {name} ({cwd_str})")
    return repos


def _collect_additional_repos(repos: list[dict[str, str]]) -> list[dict[str, str]]:
    """Prompt the user to register additional brownfield repos inline.

    After auto-detection of the CWD, this offers the user a loop to
    manually register extra repos by providing their path, name, and
    description. Each entry is persisted to ``~/.ouroboros/brownfield.json``
    immediately via the brownfield schema utilities, so interrupts don't
    lose data.

    Args:
        repos: Current list of registered repos (may include the one
            just auto-detected from cwd).

    Returns:
        Updated list of registered repos.
    """
    from ouroboros.bigbang.brownfield import register_brownfield_repo

    add_more = Confirm.ask(
        "Would you like to register additional brownfield repos for context?",
        default=False,
    )

    while add_more:
        repo_path_str = Prompt.ask("[yellow]Repo path[/]")

        if not repo_path_str or not repo_path_str.strip():
            print_warning("Empty path — skipping.")
            add_more = Confirm.ask("Add another repo?", default=False)
            continue

        repo_path = Path(repo_path_str.strip()).expanduser().resolve()

        if not repo_path.is_dir():
            print_warning(f"Path does not exist or is not a directory: {repo_path}")
            add_more = Confirm.ask("Add another repo?", default=False)
            continue

        resolved = str(repo_path)

        # Skip duplicates
        if any(r.get("path") == resolved for r in repos):
            print_info(f"Already registered: {resolved}")
            add_more = Confirm.ask("Add another repo?", default=False)
            continue

        name = Prompt.ask(
            "[yellow]Project name[/]",
            default=repo_path.name,
        )
        desc = Prompt.ask(
            "[yellow]Short description (optional)[/]",
            default="",
        )

        # Persist via brownfield schema utilities (validates + writes)
        entries = register_brownfield_repo(
            path=resolved,
            name=name,
            desc=desc,
        )
        repos = [e.to_dict() for e in entries]

        print_success(f"Registered brownfield repo: {name} ({resolved})")
        add_more = Confirm.ask("Add another repo?", default=False)

    return repos


def _check_existing_prd_seeds() -> bool:
    """Check for existing PRD seeds and prompt for overwrite confirmation.

    Scans ``~/.ouroboros/seeds/`` for any ``prd_seed_*.yaml`` files.
    If found, displays the existing seeds and asks the user whether to
    overwrite or abort.

    Returns:
        True if the user wants to proceed (overwrite), False to abort.
        Also returns True if no existing seeds are found.
    """
    seeds_dir = Path.home() / ".ouroboros" / "seeds"

    if not seeds_dir.is_dir():
        return True

    existing = sorted(seeds_dir.glob("prd_seed_*.yaml"))

    if not existing:
        return True

    # Display existing seeds
    console.print("\n[bold yellow]Existing PRD seed(s) found:[/]\n")
    for seed_path in existing:
        console.print(f"  • [dim]{seed_path.name}[/]")

    console.print()
    should_overwrite = Confirm.ask(
        "Starting a new PRD interview may overwrite existing seed(s). Continue?",
        default=False,
    )

    if not should_overwrite:
        print_info("Aborted. Existing PRD seed(s) preserved.")

    return should_overwrite


def _select_repos(repos: list[dict[str, str]]) -> list[dict[str, str]]:
    """Multi-select UI for choosing which brownfield repos to use as reference.

    Displays a numbered list of registered repos and lets the user pick
    which ones to include in the PRD interview context. Supports
    comma-separated numbers, ranges (e.g. ``1-3``), and ``all``.

    Behaviour:
    - If *repos* is empty, returns ``[]`` immediately.
    - If only one repo is registered, auto-selects it.
    - Otherwise presents the numbered list and prompts for selection.

    Args:
        repos: All registered brownfield repo dicts.

    Returns:
        Subset of *repos* selected by the user.
    """
    if not repos:
        return []

    # Auto-select when only one repo is available
    if len(repos) == 1:
        name = repos[0].get("name", repos[0].get("path", "repo"))
        print_info(f"Auto-selected single brownfield repo: {name}")
        return list(repos)

    # Display numbered list
    console.print("\n[bold cyan]Registered brownfield repos:[/]\n")
    for idx, repo in enumerate(repos, 1):
        name = repo.get("name", "unnamed")
        path = repo.get("path", "")
        desc = repo.get("desc", "")
        desc_part = f" — {desc}" if desc else ""
        console.print(f"  [bold]{idx}[/]) [cyan]{name}[/] [dim]{path}{desc_part}[/]")

    console.print(
        "\n[dim]Enter numbers separated by commas (e.g. 1,3), a range (1-3), "
        "or 'all'. Leave blank to select all.[/]"
    )

    raw = Prompt.ask("[yellow]Select repos[/]", default="all")
    selection = _parse_selection(raw, len(repos))

    if not selection:
        print_warning("No valid selection — using all repos.")
        return list(repos)

    selected = [repos[i] for i in sorted(selection)]

    names = ", ".join(r.get("name", "?") for r in selected)
    print_info(f"Selected {len(selected)} repo(s): {names}")
    return selected


def _parse_selection(raw: str, total: int) -> set[int]:
    """Parse a user selection string into a set of 0-based indices.

    Supports:
    - ``all`` or empty string → all indices
    - Comma-separated numbers: ``1,3,5``
    - Ranges: ``2-4`` (inclusive, 1-based)
    - Combinations: ``1,3-5,7``

    Invalid tokens are silently ignored.  Out-of-range numbers are
    clipped to valid bounds.

    Args:
        raw: Raw user input string.
        total: Total number of repos available.

    Returns:
        Set of valid 0-based indices.
    """
    stripped = raw.strip().lower()
    if not stripped or stripped == "all":
        return set(range(total))

    indices: set[int] = set()
    for token in stripped.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            parts = token.split("-", 1)
            try:
                start = int(parts[0].strip())
                end = int(parts[1].strip())
            except ValueError:
                continue
            # 1-based inclusive → 0-based
            for i in range(max(1, start), min(total, end) + 1):
                indices.add(i - 1)
        else:
            try:
                num = int(token)
            except ValueError:
                continue
            if 1 <= num <= total:
                indices.add(num - 1)
    return indices


async def _run_prd_interview(
    resume_id: str | None,
    model: str,
    output_path: Path,
    debug: bool,  # noqa: ARG001
) -> None:
    """Run the PRD interview loop.

    Starts by asking the opening question ("What do you want to build?"),
    then enters the guided interview loop with question classification.

    Args:
        resume_id: Optional session ID to resume.
        model: LLM model identifier.
        output_path: Path for the generated PRD document.
        debug: Enable debug output.
    """
    from ouroboros.bigbang.prd_interview import PRDInterviewEngine
    from ouroboros.providers.litellm_adapter import LiteLLMAdapter

    adapter = LiteLLMAdapter()
    engine = PRDInterviewEngine.create(llm_adapter=adapter, model=model)

    # Check for existing PRD seeds before starting a new session
    if not resume_id:
        if not _check_existing_prd_seeds():
            raise typer.Exit(code=0)

    # Detect brownfield before starting a new session
    brownfield_repos: list[dict[str, str]] = []
    if not resume_id:
        brownfield_repos = _check_brownfield(os.getcwd())
        brownfield_repos = _collect_additional_repos(brownfield_repos)
        brownfield_repos = _select_repos(brownfield_repos)

    if resume_id:
        # Resume existing session
        state_result = await engine.load_state(resume_id)
        if state_result.is_err:
            print_error(f"Failed to resume session: {state_result.error}")
            raise typer.Exit(code=1)
        state = state_result.value
        print_success(f"Resumed session: {resume_id}")
    else:
        # New session — ask the opening question first
        opening = engine.get_opening_question()
        console.print(f"\n[bold yellow]?[/] {opening}\n")

        user_answer = console.input("[bold green]> [/]")

        if not user_answer.strip():
            print_error("No response provided. Exiting.")
            raise typer.Exit(code=1)

        state_result = await engine.ask_opening_and_start(
            user_response=user_answer,
            brownfield_repos=brownfield_repos if brownfield_repos else None,
        )
        if state_result.is_err:
            print_error(f"Failed to start interview: {state_result.error}")
            raise typer.Exit(code=1)
        state = state_result.value
        print_success(f"Interview started (session: {state.interview_id})")

    # Interview loop
    while not state.is_complete:
        q_result = await engine.ask_next_question(state)
        if q_result.is_err:
            print_error(f"Question generation failed: {q_result.error}")
            break

        question = q_result.value
        console.print(f"\n[bold yellow]?[/] {question}\n")

        user_response = console.input("[bold green]> [/]")

        # Allow early exit
        if user_response.strip().lower() in ("done", "exit", "quit", "/done"):
            print_info("Finishing interview...")
            await engine.complete_interview(state)
            break

        await engine.record_response(state, user_response, question)
        await engine.save_state(state)

    # Show decide-later summary at interview end
    decide_later_summary = engine.format_decide_later_summary()
    if decide_later_summary:
        console.print(f"\n[bold yellow]{decide_later_summary}[/]\n")

    # Generate PRD seed and document
    if state.rounds:
        console.print("\n[bold cyan]Generating PRD...[/]\n")
        seed_result = await engine.generate_prd_seed(state)
        if seed_result.is_ok:
            seed = seed_result.value
            seed_path = engine.save_prd_seed(seed)
            doc_path = engine.save_prd_document(seed, output_path.parent)
            print_success(f"PRD seed saved: {seed_path}")
            print_success(f"PRD document saved: {doc_path}")
        else:
            print_error(f"Failed to generate PRD: {seed_result.error}")
