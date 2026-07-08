"""Seed command for generating a Seed from a completed interview."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from ouroboros.bigbang.interview import InterviewEngine, InterviewStatus
from ouroboros.cli.commands.init import LLMBackend, _generate_seed_from_interview, _get_adapter
from ouroboros.cli.formatters.panels import print_error, print_info
from ouroboros.config import get_clarification_model


def seed_command(
    session_id: Annotated[
        str,
        typer.Argument(help="Interview session ID to crystallize into a Seed."),
    ],
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
    llm_backend: Annotated[
        LLMBackend | None,
        typer.Option(
            "--llm-backend",
            help=(
                "LLM backend for ambiguity scoring and seed generation "
                "(claude_code, litellm, codex, copilot, opencode, gemini, goose, kiro, or pi)."
            ),
            case_sensitive=False,
        ),
    ] = None,
) -> None:
    """Generate a Seed YAML specification from an interview session."""
    try:
        asyncio.run(
            _run_seed_generation(
                session_id,
                state_dir=state_dir,
                llm_backend=llm_backend.value if llm_backend else None,
            )
        )
    except KeyboardInterrupt:
        print_info("Seed generation interrupted.")
        raise typer.Exit(code=0)
    except typer.Exit:
        raise
    except Exception as exc:
        print_error(f"Seed generation failed: {exc}")
        raise typer.Exit(code=1)


async def _run_seed_generation(
    session_id: str,
    *,
    state_dir: Path | None = None,
    llm_backend: str | None = None,
) -> Path:
    """Load an interview and run the existing seed generation path."""
    llm_adapter = _get_adapter(
        use_orchestrator=False,
        backend=llm_backend,
        for_interview=False,
    )
    engine = InterviewEngine(
        llm_adapter=llm_adapter,
        state_dir=state_dir or Path.home() / ".ouroboros" / "data",
        model=get_clarification_model(llm_backend),
    )

    state_result = await engine.load_state(session_id)
    if state_result.is_err:
        print_error(f"Failed to load interview: {state_result.error.message}")
        raise typer.Exit(code=1)

    state = state_result.value
    if state.status != InterviewStatus.COMPLETED:
        print_error(
            f"Interview {session_id} is {state.status}; complete it with "
            f"`ooo interview --resume {session_id}` before generating a Seed."
        )
        raise typer.Exit(code=1)

    seed_path, result = await _generate_seed_from_interview(state, llm_adapter, llm_backend)
    if seed_path is None:
        raise typer.Exit(code=1)

    return seed_path


__all__ = ["seed_command", "_run_seed_generation"]
