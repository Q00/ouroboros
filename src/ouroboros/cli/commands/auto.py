"""Auto command for goal → A-grade Seed → execution handoff."""

from __future__ import annotations

import asyncio
from enum import Enum
import os
from pathlib import Path
from typing import Annotated

import typer

from ouroboros.auto.adapters import (
    HandlerInterviewBackend,
    HandlerRunStarter,
    HandlerSeedGenerator,
    load_seed,
    save_seed,
)
from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.pipeline import AutoPipeline, AutoPipelineResult
from ouroboros.auto.seed_repairer import SeedRepairer
from ouroboros.auto.state import AutoPipelineState, AutoStore
from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success
from ouroboros.config import get_opencode_mode
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler, StartExecuteSeedHandler
from ouroboros.orchestrator import resolve_agent_runtime_backend


class AgentRuntimeBackend(str, Enum):  # noqa: UP042
    """Supported runtime backends for auto execution handoff."""

    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"
    HERMES = "hermes"
    GEMINI = "gemini"


app = typer.Typer(
    name="auto", help="Run bounded full-quality ooo auto pipeline.", no_args_is_help=False
)


@app.callback(invoke_without_command=True)
def auto_command(
    goal: Annotated[str | None, typer.Argument(help="Goal/task for ooo auto.")] = None,
    resume: Annotated[
        str | None, typer.Option("--resume", help="Resume an auto session id.")
    ] = None,
    runtime: Annotated[
        AgentRuntimeBackend | None,
        typer.Option("--runtime", help="Execution runtime backend.", case_sensitive=False),
    ] = None,
    max_interview_rounds: Annotated[
        int, typer.Option("--max-interview-rounds", min=1, help="Maximum auto interview rounds.")
    ] = 12,
    max_repair_rounds: Annotated[
        int, typer.Option("--max-repair-rounds", min=1, help="Maximum Seed repair rounds.")
    ] = 5,
    skip_run: Annotated[
        bool, typer.Option("--skip-run", help="Stop after A-grade Seed creation.")
    ] = False,
    show_ledger: Annotated[
        bool, typer.Option("--show-ledger", help="Print assumptions and non-goals.")
    ] = False,
) -> None:
    """Run an A-grade-gated auto pipeline.

    The command returns execution IDs after the run starts; it does not wait
    indefinitely for long-running execution completion.
    """
    if not resume and (goal is None or not goal.strip()):
        print_error("goal is required unless --resume is provided")
        raise typer.Exit(1)
    try:
        result = asyncio.run(
            _run_auto(
                goal=goal,
                resume=resume,
                runtime=runtime.value if runtime else None,
                max_interview_rounds=max_interview_rounds,
                max_repair_rounds=max_repair_rounds,
                skip_run=skip_run,
            )
        )
    except Exception as exc:
        print_error(f"Auto pipeline failed: {exc}")
        raise typer.Exit(1) from exc

    _print_result(result, show_ledger=show_ledger)
    if result.status in {"blocked", "failed"}:
        raise typer.Exit(1)


def _safe_default_cwd() -> Path:
    """Return a safe default cwd without silently retargeting projects."""
    cwd = Path.cwd()
    if cwd == Path("/"):
        return Path.home()
    if not os.access(cwd, os.W_OK | os.X_OK):
        msg = f"current working directory is not writable/searchable: {cwd}"
        raise ValueError(msg)
    return cwd


async def _run_auto(
    *,
    goal: str | None,
    resume: str | None,
    runtime: str | None,
    max_interview_rounds: int,
    max_repair_rounds: int,
    skip_run: bool,
) -> AutoPipelineResult:
    store = AutoStore()
    if resume:
        state = store.load(resume)
        persisted_runtime = state.runtime_backend
        if persisted_runtime is None and state.opencode_mode is not None:
            persisted_runtime = "opencode"
        if runtime is not None and persisted_runtime not in {None, runtime}:
            msg = (
                f"resume runtime mismatch: session uses {persisted_runtime}, "
                f"but --runtime {runtime} was requested"
            )
            raise ValueError(msg)
        runtime = resolve_agent_runtime_backend(runtime or persisted_runtime)
        max_interview_rounds = state.max_interview_rounds
        max_repair_rounds = state.max_repair_rounds
        skip_run = skip_run or state.skip_run
    else:
        if goal is None or not goal.strip():
            raise ValueError("goal is required when not resuming")
        runtime = resolve_agent_runtime_backend(runtime)
        state = AutoPipelineState(goal=goal.strip(), cwd=str(_safe_default_cwd()))
        state.runtime_backend = runtime
        state.skip_run = skip_run
        state.max_interview_rounds = max_interview_rounds
        state.max_repair_rounds = max_repair_rounds

    if runtime == "opencode":
        opencode_mode = state.opencode_mode or get_opencode_mode()
        if opencode_mode == "plugin":
            opencode_mode = "subprocess"
    else:
        opencode_mode = None
    state.runtime_backend = runtime
    state.opencode_mode = opencode_mode
    state.skip_run = skip_run

    authoring_opencode_mode = "subprocess" if opencode_mode == "plugin" else opencode_mode
    interview = InterviewHandler(
        agent_runtime_backend=runtime, opencode_mode=authoring_opencode_mode
    )
    generate_seed = GenerateSeedHandler(
        agent_runtime_backend=runtime, opencode_mode=authoring_opencode_mode
    )
    execute_seed = ExecuteSeedHandler(agent_runtime_backend=runtime, opencode_mode=opencode_mode)
    start_execute = StartExecuteSeedHandler(
        execute_handler=execute_seed, agent_runtime_backend=runtime, opencode_mode=opencode_mode
    )
    driver = AutoInterviewDriver(
        HandlerInterviewBackend(interview, cwd=state.cwd),
        store=store,
        max_rounds=max_interview_rounds,
    )
    pipeline = AutoPipeline(
        driver,
        HandlerSeedGenerator(generate_seed),
        run_starter=HandlerRunStarter(start_execute, cwd=state.cwd),
        store=store,
        repairer=SeedRepairer(max_repair_rounds=max_repair_rounds),
        seed_saver=save_seed,
        seed_loader=load_seed,
        skip_run=skip_run,
    )
    result = await pipeline.run(state)
    return result


def _print_result(result: AutoPipelineResult, *, show_ledger: bool) -> None:
    if result.status == "complete":
        print_success("Auto pipeline completed")
    elif result.status in {"blocked", "failed"}:
        print_error("Auto pipeline did not complete")
    else:
        print_info("Auto pipeline status")
    console.print(f"Auto session: [cyan]{result.auto_session_id}[/]")
    console.print(f"Status: [bold]{result.status}[/]")
    if result.grade:
        console.print(f"Seed grade: [bold]{result.grade}[/]")
    if result.interview_session_id:
        console.print(f"Interview session: {result.interview_session_id}")
    if result.seed_path:
        console.print(f"Seed: {result.seed_path}")
    if result.job_id or result.execution_id or result.run_session_id:
        console.print("Execution started:")
        console.print(f"  Job ID: {result.job_id}")
        console.print(f"  Execution ID: {result.execution_id}")
        console.print(f"  Session ID: {result.run_session_id}")
    if show_ledger:
        if result.assumptions:
            console.print("Assumptions:")
            for item in result.assumptions:
                console.print(f"  - {item}")
        if result.non_goals:
            console.print("Non-goals:")
            for item in result.non_goals:
                console.print(f"  - {item}")
    if result.blocker:
        console.print(f"Blocker: [yellow]{result.blocker}[/]")
    console.print(f"Resume: [bold]ooo auto --resume {result.auto_session_id}[/]")


__all__ = ["app"]
