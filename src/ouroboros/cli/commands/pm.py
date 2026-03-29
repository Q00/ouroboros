"""PM command for generating Product Requirements Documents.

This command initiates a guided interview process for PMs to define
product requirements, with automatic classification of planning vs
development questions, producing a PMSeed and human-readable PM document.

Usage:
    ouroboros pm                    Start a new PM interview
    ouroboros pm --resume <id>     Resume an existing PM session
"""

import asyncio
from pathlib import Path
from typing import Annotated, Any

from rich.prompt import Confirm, Prompt
import typer

from ouroboros.bigbang.interview import InterviewRound
from ouroboros.bigbang.pm_completion import (
    build_pm_completion_summary,
    maybe_complete_pm_interview,
)
from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success, print_warning
from ouroboros.cli.formatters.prompting import multiline_prompt_async
from ouroboros.config import get_clarification_model
from ouroboros.core.types import Result
from ouroboros.observability import LoggingConfig, configure_logging

app = typer.Typer(
    name="pm",
    help="Generate a Product Requirements Document through guided interview.",
    no_args_is_help=False,
    invoke_without_command=True,
)


def _create_pm_litellm_adapter() -> Any:
    """Construct the PM interview adapter or raise actionable guidance.

    The PM CLI currently relies on the LiteLLM-backed path. On base installs
    without the optional ``litellm`` extra, importing the adapter crashes with
    ``ModuleNotFoundError``. Convert that into a user-facing error instead.
    """
    try:
        from ouroboros.providers.litellm_adapter import LiteLLMAdapter
    except ModuleNotFoundError as exc:
        if exc.name == "litellm":
            msg = (
                "PM interviews require the optional LiteLLM dependency. "
                "Reinstall with `ouroboros-ai[litellm]`, or if you use uv tool: "
                "`uv tool install --force --with litellm ouroboros-ai`."
            )
            raise RuntimeError(msg) from exc
        raise

    return LiteLLMAdapter()


@app.callback(invoke_without_command=True)
def pm_command(
    ctx: typer.Context,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume",
            "-r",
            help="Resume an existing PM interview session by ID.",
        ),
    ] = None,
    output: Annotated[
        str | None,
        typer.Option(
            "--output",
            "-o",
            help="Output directory for the generated PM document (default: .ouroboros/).",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            "-m",
            help="LLM model to use for the PM interview.",
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
    """Start or resume a PM interview to generate product requirements.

    This command guides PMs through a structured interview process to
    define product requirements. Questions are automatically classified
    as planning (PM-answerable) or development (deferred to dev interview).

    The output is a PMSeed JSON file saved to ~/.ouroboros/seeds/ and
    a human-readable pm.md saved to the output directory.

    [bold]Examples:[/]

        ouroboros pm                            Start new PM interview
        ouroboros pm --resume abc123            Resume session
        ouroboros pm --output ./docs            Save pm.md to ./docs/
    """
    if ctx.invoked_subcommand is not None:
        return

    if model is None:
        model = get_clarification_model()

    if debug:
        configure_logging(LoggingConfig(log_level="DEBUG"))
        print_info("Debug mode enabled - showing verbose logs")

    console.print("\n[bold cyan]Ouroboros PM Generator[/] - Product Requirements Document\n")

    if resume:
        print_info(f"Resuming PM session: {resume}")
    else:
        print_info("Starting new PM interview session...")

    console.print(f"  Model: [dim]{model}[/]\n")

    try:
        asyncio.run(
            _run_pm_interview(
                resume_id=resume,
                model=model,
                debug=debug,
                output_dir=output,
            )
        )
    except KeyboardInterrupt:
        print_info("\nPM interview interrupted. Progress has been saved.")
        raise typer.Exit(code=0)


def _load_brownfield_from_db() -> list[dict[str, str]]:
    """Load brownfield repos from DB for PM interview context.

    Loads all registered brownfield repos and lets the user select
    which ones to include via ``_select_repos()``.
    No cwd-based detection — repos come from the DB only.

    Returns:
        List of brownfield repo dicts from the database.
    """
    from ouroboros.bigbang.brownfield import load_brownfield_repos_as_dicts

    repos = load_brownfield_repos_as_dicts()
    if repos:
        print_info(f"Loaded {len(repos)} brownfield repo(s) from registry.")
    return repos


def _check_existing_pm_seeds() -> bool:
    """Check for existing PM seeds and prompt for overwrite confirmation.

    Scans ``~/.ouroboros/seeds/`` for any ``pm_seed_*.json`` files.
    If found, displays the existing seeds and asks the user whether to
    overwrite or abort.

    Returns:
        True if the user wants to proceed (overwrite), False to abort.
        Also returns True if no existing seeds are found.
    """
    seeds_dir = Path.home() / ".ouroboros" / "seeds"

    if not seeds_dir.is_dir():
        return True

    existing = sorted(seeds_dir.glob("pm_*.json"))

    if not existing:
        return True

    # Display existing seeds
    console.print("\n[bold yellow]Existing PM seed(s) found:[/]\n")
    for seed_path in existing:
        console.print(f"  • [dim]{seed_path.name}[/]")

    console.print()
    should_overwrite = Confirm.ask(
        "Starting a new PM interview may overwrite existing seed(s). Continue?",
        default=False,
    )

    if not should_overwrite:
        print_info("Aborted. Existing PM seed(s) preserved.")

    return should_overwrite


def _select_repos(repos: list[dict[str, str]]) -> list[dict[str, str]]:
    """Multi-select UI for choosing which brownfield repos to use as reference.

    Displays a numbered list of registered repos and lets the user pick
    which ones to include in the PM interview context. Supports
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


def _save_cli_pm_meta(session_id: str, engine: Any) -> None:
    """Persist PM-specific metadata so ``--resume`` can restore it.

    Writes to ``~/.ouroboros/data/pm_meta_{session_id}.json`` — the same
    location used by the MCP handler's ``_save_pm_meta``.
    """
    import json

    data_dir = Path.home() / ".ouroboros" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    meta_path = data_dir / f"pm_meta_{session_id}.json"

    # Build pending_reframe from the engine's reframe map (mirrors MCP _save_pm_meta)
    pending_reframe: dict[str, str] | None = None
    if engine._reframe_map:
        reframed = next(reversed(engine._reframe_map))
        pending_reframe = {
            "reframed": reframed,
            "original": engine._reframe_map[reframed],
        }

    meta = {
        "deferred_items": list(engine.deferred_items),
        "decide_later_items": list(engine.decide_later_items),
        "codebase_context": engine.codebase_context,
        "pending_reframe": pending_reframe,
        "cwd": "",
        "brownfield_repos": list(engine._selected_brownfield_repos),
        "classifications": [c.output_type.value for c in getattr(engine, "classifications", [])],
    }

    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


async def _run_pm_interview(
    resume_id: str | None,
    model: str,
    debug: bool,  # noqa: ARG001
    output_dir: str | None = None,
) -> None:
    """Run the PM interview loop.

    Starts by asking the opening question ("What do you want to build?"),
    then enters the guided interview loop with question classification.

    Args:
        resume_id: Optional session ID to resume.
        model: LLM model identifier.
        debug: Enable debug output.
        output_dir: Optional output directory for the generated PM document.
    """
    from ouroboros.bigbang.pm_interview import PMInterviewEngine

    try:
        adapter = _create_pm_litellm_adapter()
    except RuntimeError as exc:
        print_error(str(exc))
        raise typer.Exit(code=1) from exc
    engine = PMInterviewEngine.create(llm_adapter=adapter, model=model)

    # Check for existing PM seeds before starting a new session
    if not resume_id:
        if not _check_existing_pm_seeds():
            raise typer.Exit(code=0)

    # Load brownfield repos from DB (registered via ooo setup)
    brownfield_repos: list[dict[str, str]] = []
    if not resume_id:
        brownfield_repos = _load_brownfield_from_db()
        brownfield_repos = _select_repos(brownfield_repos)

    if resume_id:
        # Resume existing session
        state_result = await engine.load_state(resume_id)
        if state_result.is_err:
            print_error(f"Failed to resume session: {state_result.error}")
            raise typer.Exit(code=1)
        state = state_result.value

        # Restore PM-specific metadata (deferred items, decide-later, etc.)
        data_dir = Path.home() / ".ouroboros" / "data"
        pm_meta_path = data_dir / f"pm_meta_{resume_id}.json"
        if pm_meta_path.exists():
            import json

            try:
                meta = json.loads(pm_meta_path.read_text(encoding="utf-8"))
                engine.restore_meta(meta)
            except (json.JSONDecodeError, OSError):
                print_warning("Could not load PM metadata; continuing without it.")
                # Still install PM steering even without full meta
                engine._install_pm_steering()
        else:
            # No pm_meta file — still install PM steering for resumed session
            engine._install_pm_steering()

        print_success(f"Resumed session: {resume_id}")
    else:
        # New session — ask the opening question first
        opening = engine.get_opening_question()
        console.print(f"\n[bold yellow]?[/] {opening}\n")

        user_answer = await multiline_prompt_async("Your response")

        if not user_answer.strip():
            print_error("No response provided. Exiting.")
            raise typer.Exit(code=1)

        print_info("Starting interview...")
        state_result = await engine.ask_opening_and_start(
            user_response=user_answer,
            brownfield_repos=brownfield_repos if brownfield_repos else None,
        )
        if state_result.is_err:
            print_error(f"Failed to start interview: {state_result.error}")
            raise typer.Exit(code=1)
        state = state_result.value
        print_success(f"Interview started (session: {state.interview_id})")

        # Save pm_meta so --resume can find it later
        _save_cli_pm_meta(state.interview_id, engine)

    # Interview loop
    while not state.is_complete:
        # Check for a pending unanswered question from a previous session
        if state.rounds and state.rounds[-1].user_response is None:
            question = state.rounds[-1].question
        else:
            print_info("Generating next question...")
            q_result = await engine.ask_next_question(state)
            if q_result.is_err:
                print_error(f"Question generation failed: {q_result.error}")
                break

            question = q_result.value

        # Append unanswered round so the pending question is persisted
        # in InterviewState.  This ensures --resume can re-display it.
        # Skip if we already have this as the last unanswered round (resume case).
        if not (state.rounds and state.rounds[-1].user_response is None):
            state.rounds.append(
                InterviewRound(
                    round_number=state.current_round_number,
                    question=question,
                    user_response=None,
                )
            )
            state.mark_updated()

        console.print(f"\n[bold yellow]?[/] {question}\n")

        # Check if this question was classified as skippable —
        # if so, show a hint that the user can defer it.
        classification = engine.get_last_classification()
        if classification == "decide_later":
            console.print(
                "[dim]  💡 This question can be deferred. "
                'Type "decide later" or "skip" to defer it.[/]\n'
            )
        elif classification == "deferred":
            console.print(
                "[dim]  💡 This is a technical question that can be deferred to the dev phase. "
                'Type "defer" or "skip" to defer it.[/]\n'
            )

        # Persist state + meta AFTER displaying the question but BEFORE
        # waiting for input so that an interruption preserves the pending
        # question and --resume shows the same question.
        save_result = await engine.save_state(state)
        if isinstance(save_result, Result) and save_result.is_err:
            print_error(f"Failed to save state: {save_result.error}")
            break
        _save_cli_pm_meta(state.interview_id, engine)

        user_response = await multiline_prompt_async("Your response")

        # Allow early exit
        if user_response.strip().lower() in ("done", "exit", "quit", "/done"):
            print_info("Finishing interview...")
            # Remove the synthetic unanswered round before completing
            # so extraction never sees a question the user didn't answer.
            if state.rounds and state.rounds[-1].user_response is None:
                state.rounds.pop()

            completion = await engine.check_completion(state)
            complete_result = await engine.complete_interview(state)
            if complete_result.is_err:
                print_error(f"Failed to complete interview: {complete_result.error}")
                break

            state = complete_result.value
            save_result = await engine.save_state(state)
            if save_result.is_err:
                print_error(f"Failed to save completed state: {save_result.error}")
                break
            _save_cli_pm_meta(state.interview_id, engine)

            decide_later_summary = engine.format_decide_later_summary()
            summary_text = build_pm_completion_summary(
                session_id=state.interview_id,
                completion=completion,
                stored_ambiguity_score=state.ambiguity_score,
                deferred_count=len(engine.deferred_items),
                decide_later_count=len(engine.decide_later_items),
                decide_later_summary=decide_later_summary,
            )
            console.print(f"\n[bold green]{summary_text}[/]\n")
            break

        # Pop the unanswered round before recording so record_response
        # can create a proper answered round (mirrors MCP handler pattern).
        if state.rounds and state.rounds[-1].user_response is None:
            state.rounds.pop()

        # Handle user-initiated skip (decide later / defer to dev)
        _lower = user_response.strip().lower()
        if classification == "decide_later" and _lower in (
            "decide later",
            "skip",
            "[decide_later]",
        ):
            record_result = await engine.skip_as_decide_later(state, question)
            if isinstance(record_result, Result) and record_result.is_err:
                print_error(f"Failed to skip question: {record_result.error}")
                break
            save_result = await engine.save_state(state)
            if isinstance(save_result, Result) and save_result.is_err:
                print_error(f"Failed to save state: {save_result.error}")
                break
            _save_cli_pm_meta(state.interview_id, engine)
            continue
        if classification == "deferred" and _lower in (
            "defer",
            "skip",
            "[deferred]",
        ):
            record_result = await engine.skip_as_deferred(state, question)
            if isinstance(record_result, Result) and record_result.is_err:
                print_error(f"Failed to defer question: {record_result.error}")
                break
            save_result = await engine.save_state(state)
            if isinstance(save_result, Result) and save_result.is_err:
                print_error(f"Failed to save state: {save_result.error}")
                break
            _save_cli_pm_meta(state.interview_id, engine)
            continue

        record_result = await engine.record_response(state, user_response, question)
        if isinstance(record_result, Result) and record_result.is_err:
            print_error(f"Failed to record response: {record_result.error}")
            break
        if isinstance(record_result, Result):
            state = record_result.value

        state.clear_stored_ambiguity()
        completion_result = await maybe_complete_pm_interview(state, engine)
        if completion_result.is_err:
            print_error(f"Failed to complete interview: {completion_result.error}")
            break

        state, completion = completion_result.value
        if completion is not None:
            save_result = await engine.save_state(state)
            if save_result.is_err:
                print_error(f"Failed to save completed state: {save_result.error}")
                break
            _save_cli_pm_meta(state.interview_id, engine)

            decide_later_summary = engine.format_decide_later_summary()
            summary_text = build_pm_completion_summary(
                session_id=state.interview_id,
                completion=completion,
                stored_ambiguity_score=state.ambiguity_score,
                deferred_count=len(engine.deferred_items),
                decide_later_count=len(engine.decide_later_items),
                decide_later_summary=decide_later_summary,
            )
            console.print(f"\n[bold green]{summary_text}[/]\n")
            break

        save_result = await engine.save_state(state)
        if isinstance(save_result, Result) and save_result.is_err:
            print_error(f"Failed to save state: {save_result.error}")
            break
        _save_cli_pm_meta(state.interview_id, engine)

    # Show decide-later summary at interview end
    decide_later_summary = engine.format_decide_later_summary()
    if decide_later_summary:
        console.print(f"\n[bold yellow]{decide_later_summary}[/]\n")

    # Generate PM seed and document — only if there are actual answered rounds
    answered_rounds = [r for r in state.rounds if r.user_response is not None]
    if answered_rounds and state.is_complete:
        console.print("\n[bold cyan]Generating PM...[/]\n")
        seed_result = await engine.generate_pm_seed(state)
        if seed_result.is_ok:
            seed = seed_result.value
            seed_path = engine.save_pm_seed(seed)
            print_success(f"PM seed saved: {seed_path}")

            # Save human-readable pm.md alongside the seed
            from ouroboros.bigbang.pm_document import save_pm_document

            pm_dir = Path(output_dir) if output_dir else Path.cwd() / ".ouroboros"
            pm_path = save_pm_document(seed, output_dir=pm_dir)
            print_success(f"PM document saved: {pm_path}")
        else:
            print_error(f"Failed to generate PM: {seed_result.error}")
    elif state.rounds and not state.is_complete:
        print_info("Interview not complete — skipping PM generation.")
