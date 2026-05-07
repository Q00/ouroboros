"""Auto command for goal → A-grade Seed → execution handoff."""

from __future__ import annotations

import asyncio
from enum import Enum
import os
from pathlib import Path
from typing import Annotated

from rich.markup import escape as _rich_escape
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
from ouroboros.auto.provenance import resolve_provenance
from ouroboros.auto.seed_repairer import SeedRepairer
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
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
    COPILOT = "copilot"
    KIRO = "kiro"


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
        typer.Option(
            "--runtime",
            help=(
                "Runtime backend used by ooo auto. The same flag is applied to "
                "BOTH (a) interview/Seed authoring (in-process via the matching "
                "MCP authoring handler) AND (b) the run-handoff that dispatches "
                "the executor. The first interview question is generated "
                "in-process even when --runtime is set to a heavyweight backend "
                "like codex; see docs/auto-runtime-semantics.md."
            ),
            case_sensitive=False,
        ),
    ] = None,
    max_interview_rounds: Annotated[
        int | None,
        typer.Option(
            "--max-interview-rounds",
            min=1,
            help=(
                "Maximum auto interview rounds. Defaults to 12 for new sessions and "
                "to the persisted bound on resume; explicit values raise (never lower) "
                "the bound."
            ),
        ),
    ] = None,
    max_repair_rounds: Annotated[
        int | None,
        typer.Option(
            "--max-repair-rounds",
            min=1,
            help=(
                "Maximum Seed repair rounds. Defaults to 5 for new sessions and to "
                "the persisted bound on resume; explicit values raise (never lower) "
                "the bound."
            ),
        ),
    ] = None,
    skip_run: Annotated[
        bool, typer.Option("--skip-run", help="Stop after A-grade Seed creation.")
    ] = False,
    show_ledger: Annotated[
        bool, typer.Option("--show-ledger", help="Print assumptions and non-goals.")
    ] = False,
    status: Annotated[
        bool, typer.Option("--status", help="Print persisted auto session status without running.")
    ] = False,
    attach_execution: Annotated[
        str | None,
        typer.Option(
            "--attach-execution",
            help="Attach an externally verified execution id to an unknown run handoff.",
        ),
    ] = None,
    attach_job: Annotated[
        str | None,
        typer.Option("--attach-job", help="Attach an externally verified job id."),
    ] = None,
    attach_session: Annotated[
        str | None,
        typer.Option("--attach-session", help="Attach an externally verified run session id."),
    ] = None,
    attach_source: Annotated[
        str | None,
        typer.Option("--attach-source", help="Source label for an attached run handle."),
    ] = None,
    reconcile_run: Annotated[
        bool,
        typer.Option(
            "--reconcile-run",
            help="Try to reconcile an unknown run handoff without starting a duplicate run.",
        ),
    ] = False,
    reconcile_source: Annotated[
        str | None,
        typer.Option("--reconcile-source", help="Source label for run handoff reconciliation."),
    ] = None,
) -> None:
    """Run an A-grade-gated auto pipeline.

    The command returns execution IDs after the run starts; it does not wait
    indefinitely for long-running execution completion.
    """
    if status:
        if not resume:
            print_error("--status requires --resume auto_<id>")
            raise typer.Exit(1)
        try:
            _print_status(AutoStore().load(resume))
        except Exception as exc:
            print_error(f"Auto status failed: {exc}")
            raise typer.Exit(1) from exc
        return

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
                attach_execution=attach_execution,
                attach_job=attach_job,
                attach_session=attach_session,
                attach_source=attach_source,
                reconcile_run=reconcile_run,
                reconcile_source=reconcile_source,
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


_DEFAULT_MAX_INTERVIEW_ROUNDS = 12
_DEFAULT_MAX_REPAIR_ROUNDS = 5


async def _run_auto(
    *,
    goal: str | None,
    resume: str | None,
    runtime: str | None,
    max_interview_rounds: int | None,
    max_repair_rounds: int | None,
    skip_run: bool,
    attach_execution: str | None = None,
    attach_job: str | None = None,
    attach_session: str | None = None,
    attach_source: str | None = None,
    reconcile_run: bool = False,
    reconcile_source: str | None = None,
) -> AutoPipelineResult:
    store = AutoStore()
    incoming_provenance = resolve_provenance()
    attach_requested = any(
        isinstance(item, str) and item.strip()
        for item in (attach_execution, attach_job, attach_session)
    )
    if attach_requested and not resume:
        raise ValueError("--attach-execution/--attach-job/--attach-session require --resume")
    if reconcile_run and not resume:
        raise ValueError("--reconcile-run requires --resume")
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
        # Loop bounds: explicit CLI override wins; otherwise honour persisted
        # value so unattended resume keeps the original budget. Lowering a
        # bound on resume is rejected — a bound that already blocked must be
        # raised, never tightened, to avoid trapping the session further.
        if max_interview_rounds is None:
            max_interview_rounds = state.max_interview_rounds
        elif max_interview_rounds < state.max_interview_rounds:
            msg = (
                f"--max-interview-rounds {max_interview_rounds} is lower than the "
                f"persisted bound ({state.max_interview_rounds}); refuse to tighten "
                "a bound on resume"
            )
            raise ValueError(msg)
        else:
            state.max_interview_rounds = max_interview_rounds
        if max_repair_rounds is None:
            max_repair_rounds = state.max_repair_rounds
        elif max_repair_rounds < state.max_repair_rounds:
            msg = (
                f"--max-repair-rounds {max_repair_rounds} is lower than the "
                f"persisted bound ({state.max_repair_rounds}); refuse to tighten "
                "a bound on resume"
            )
            raise ValueError(msg)
        else:
            state.max_repair_rounds = max_repair_rounds
        skip_run = skip_run or state.skip_run
    else:
        if goal is None or not goal.strip():
            raise ValueError("goal is required when not resuming")
        runtime = resolve_agent_runtime_backend(runtime)
        if max_interview_rounds is None:
            max_interview_rounds = _DEFAULT_MAX_INTERVIEW_ROUNDS
        if max_repair_rounds is None:
            max_repair_rounds = _DEFAULT_MAX_REPAIR_ROUNDS
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
    if incoming_provenance is not None:
        if resume and state.provenance is None:
            msg = (
                "cannot attach provenance on resume of a session originally "
                "invoked without provenance; re-attribution would mislead audit"
            )
            raise ValueError(msg)
        if state.provenance is not None and state.provenance != incoming_provenance:
            msg = (
                "provenance conflict on resume: persisted state recorded "
                f"{state.provenance} but caller supplied {incoming_provenance}"
            )
            raise ValueError(msg)
        state.provenance = incoming_provenance

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
        timeout_seconds=state.phase_timeout_seconds(AutoPhase.INTERVIEW),
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
        attach_execution_id=attach_execution,
        attach_job_id=attach_job,
        attach_run_session_id=attach_session,
        attach_source=attach_source,
        reconcile_run=reconcile_run,
        reconcile_source=reconcile_source,
    )
    result = await pipeline.run(state)
    return result


_OPENCODE_RUNTIMES = frozenset({"opencode", "opencode_cli"})


def _format_runtime_labels(
    runtime_backend: str | None, opencode_mode: str | None
) -> tuple[str, str]:
    """Return (authoring backend, run backend) labels for status/result output.

    Both auto entry points demote ``opencode_mode == "plugin"`` to
    ``"subprocess"`` because a ``_subagent`` envelope would have no
    receiver outside an active OpenCode bridge plugin session. The two
    entry points differ in scope:

    - ``cli/commands/auto.py`` (this file) overwrites
      ``state.opencode_mode`` to ``"subprocess"`` for **both** authoring
      and run-handoff handlers.
    - ``mcp/tools/auto_handler.py`` only demotes the authoring handlers
      and keeps the persisted ``"plugin"`` value for the run-handoff
      handler, because that one is invoked from inside the OpenCode
      session that owns the bridge plugin.

    The labels here faithfully reflect the persisted ``state``: in CLI
    flow that is always ``"subprocess"`` after demotion, in MCP flow it
    can still be ``"plugin"`` (visible via ``--status`` on a session
    that was created by the MCP entry point). Authoring is always shown
    as in-process because both entry points hand the authoring handler
    the demoted ``"subprocess"`` value.
    """
    backend_name = runtime_backend or "unspecified"
    authoring = f"in-process ({backend_name})"
    backend_key = (runtime_backend or "").strip().lower()
    mode_key = (opencode_mode or "").strip().lower()
    if backend_key in _OPENCODE_RUNTIMES and mode_key:
        run_label = f"{runtime_backend} ({opencode_mode})"
    else:
        run_label = backend_name
    return authoring, run_label


def _print_status(state: AutoPipelineState) -> None:
    """Print a compact read-only summary for a persisted auto session."""
    print_info("Auto session status")
    console.print(f"Auto session: [cyan]{state.auto_session_id}[/]")
    console.print(f"Phase: [bold]{state.phase.value}[/]")
    authoring, run_label = _format_runtime_labels(state.runtime_backend, state.opencode_mode)
    console.print(f"Authoring backend: [bold]{authoring}[/]")
    console.print(f"Run backend: [bold]{run_label}[/]")
    invoked_by = state.invoked_by()
    if invoked_by != "direct":
        source = (state.provenance or {}).get("source", "unknown")
        console.print(f"Invoked by: [bold]{invoked_by}[/] (source={_rich_escape(source)})")
    console.print(f"Last progress: {state.last_progress_message}")
    console.print(f"Last progress at: {state.last_progress_at}")
    if state.interview_session_id:
        console.print(f"Interview session: {state.interview_session_id}")
    console.print(f"Current interview round: {state.current_round}")
    if state.pending_question:
        question = state.pending_question.replace("\n", " ").strip()
        if len(question) > 160:
            question = f"{question[:157]}..."
        console.print(f"Pending question: {question}")
    if state.seed_path:
        console.print(f"Seed: {state.seed_path}")
    console.print(f"Seed origin: {state.seed_origin.value}")
    if state.last_grade:
        console.print(f"Seed grade: [bold]{state.last_grade}[/]")
    if state.job_id or state.execution_id or state.run_session_id:
        console.print("Execution:")
        console.print(f"  Job ID: {state.job_id}")
        console.print(f"  Execution ID: {state.execution_id}")
        console.print(f"  Session ID: {state.run_session_id}")
    if state.run_handoff_status:
        console.print(f"Run handoff status: [bold]{state.run_handoff_status}[/]")
    if state.run_handoff_guidance:
        console.print(f"Run handoff guidance: [yellow]{state.run_handoff_guidance}[/]")
    if state.attached_run_handle:
        console.print(f"Attached run handle: {state.attached_run_handle}")
        console.print(f"Attached run source: {state.attached_run_source}")
        console.print(f"Attached at: {state.attached_at}")
    if state.run_reconciliation_status:
        console.print(f"Run reconciliation status: {state.run_reconciliation_status}")
        console.print(f"Run reconciliation source: {state.run_reconciliation_source}")
        console.print(f"Run reconciled at: {state.run_reconciled_at}")
    if state.last_error:
        console.print(f"Blocker: [yellow]{state.last_error}[/]")
    if state.auto_answer_log:
        recent = state.auto_answer_log[-5:]
        console.print(f"Recent auto answers (last {len(recent)}):")
        for entry in recent:
            round_value = entry.get("round", "?")
            source = _rich_escape(str(entry.get("source", "?")))
            # Persisted question/answer text comes straight from the
            # interview backend and may contain "[" / "]" sequences that
            # Rich would otherwise interpret as markup, breaking the
            # rendered text or raising a parse error. Escape both fields
            # before printing so the status surface stays robust against
            # arbitrary backend output.
            question = _rich_escape(str(entry.get("question", "")))
            answer = _rich_escape(str(entry.get("answer", "")))
            console.print(f"  round {round_value} \\[{source}] Q: {question}")
            console.print(f"    A: {answer}")
    console.print(f"Resume: [bold]ooo auto --resume {state.auto_session_id}[/]")


def _print_result(result: AutoPipelineResult, *, show_ledger: bool) -> None:
    if result.status == "complete":
        print_success("Auto pipeline completed")
    elif result.status in {"blocked", "failed"}:
        print_error("Auto pipeline did not complete")
    else:
        print_info("Auto pipeline status")
    console.print(f"Auto session: [cyan]{result.auto_session_id}[/]")
    console.print(f"Status: [bold]{result.status}[/]")
    authoring, run_label = _format_runtime_labels(result.runtime_backend, result.opencode_mode)
    console.print(f"Authoring backend: [bold]{authoring}[/]")
    console.print(f"Run backend: [bold]{run_label}[/]")
    if result.invoked_by != "direct":
        source = (result.provenance or {}).get("source", "unknown")
        console.print(f"Invoked by: [bold]{result.invoked_by}[/] (source={_rich_escape(source)})")
    if result.grade:
        console.print(f"Seed grade: [bold]{result.grade}[/]")
    if result.interview_session_id:
        console.print(f"Interview session: {result.interview_session_id}")
    if result.seed_path:
        console.print(f"Seed: {result.seed_path}")
    console.print(f"Seed origin: {result.seed_origin}")
    if result.job_id or result.execution_id or result.run_session_id:
        console.print("Execution started:")
        console.print(f"  Job ID: {result.job_id}")
        console.print(f"  Execution ID: {result.execution_id}")
        console.print(f"  Session ID: {result.run_session_id}")
    if result.run_handoff_status:
        console.print(f"Run handoff status: [bold]{result.run_handoff_status}[/]")
    if result.run_handoff_guidance:
        console.print(f"Run handoff guidance: [yellow]{result.run_handoff_guidance}[/]")
    if result.attached_run_handle:
        console.print(f"Attached run handle: {result.attached_run_handle}")
        console.print(f"Attached run source: {result.attached_run_source}")
        console.print(f"Attached at: {result.attached_at}")
    if result.run_reconciliation_status:
        console.print(f"Run reconciliation status: {result.run_reconciliation_status}")
        console.print(f"Run reconciliation source: {result.run_reconciliation_source}")
        console.print(f"Run reconciled at: {result.run_reconciled_at}")
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
