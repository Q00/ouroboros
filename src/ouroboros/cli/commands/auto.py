"""Auto command for goal → A-grade Seed → execution handoff."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from enum import Enum
import os
from pathlib import Path
from typing import Annotated

from rich.markup import escape as _rich_escape
import typer

from ouroboros.auto.adapters import (
    EnvRuntimeProbeRunner,
    HandlerInterviewBackend,
    HandlerLateralThinker,
    HandlerRalphPoller,
    HandlerRalphStarter,
    HandlerRunStarter,
    HandlerSeedGenerator,
    HandlerSeedQAEvaluator,
    HandlerSynchronousRunStarter,
    build_answer_refiner,
    load_seed,
    save_seed,
)
from ouroboros.auto.domain_profile import DEFAULT_REGISTRY
from ouroboros.auto.handoff_contract import RUN_HANDOFF_STARTED_STATUS
from ouroboros.auto.intent_guard import diagnose_auto_pipeline_state
from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.pipeline import AutoPipeline, AutoPipelineResult
from ouroboros.auto.policies import apply_domain_policy_defaults

# Import the built-in profile package once so CLI domain activation sees
# production registrations, not just profiles manually loaded by tests.
import ouroboros.auto.profiles  # noqa: F401,E402
from ouroboros.auto.progress import AutoProgressCallback, AutoProgressEvent
from ouroboros.auto.provenance import resolve_provenance
from ouroboros.auto.resume_render import render_resume_lines
from ouroboros.auto.runtime_routing import (
    demote_plugin_opencode_mode,
    resolve_auto_stage_runtime_plan,
)
from ouroboros.auto.seed_repairer import SeedRepairer
from ouroboros.auto.state import (
    DEFAULT_PIPELINE_TIMEOUT_SECONDS,
    MAX_PIPELINE_TIMEOUT_SECONDS,
    MIN_PIPELINE_TIMEOUT_SECONDS,
    AutoCommitPolicy,
    AutoPhase,
    AutoPipelineState,
    AutoResumeCapability,
    AutoStore,
    parse_auto_worktree_policy,
    validate_complete_product_timeout,
)
from ouroboros.auto.worktree import ensure_auto_worktree, release_auto_worktree
from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import (
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ouroboros.config import get_opencode_mode
from ouroboros.mcp.job_manager import JobManager, JobSnapshot, JobStatus
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.evaluation_handlers import LateralThinkHandler
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler, StartExecuteSeedHandler
from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobWaitHandler
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.mcp.tools.ralph_handlers import RalphHandler
from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin
from ouroboros.orchestrator import resolve_agent_runtime_backend
from ouroboros.persistence.event_store import EventStore
from ouroboros.runtime.controls import load_runtime_controls
from ouroboros.runtime.watchdog import Watchdog

_STALE_COMPLETED_RALPH_HANDOFF_STATUSES = frozenset(
    {RUN_HANDOFF_STARTED_STATUS, "ralph_retry_after_blocker"}
)


def _build_configured_ralph_handler(
    *, runtime: str | None, opencode_mode: str | None
) -> RalphHandler:
    """Build the same fully wired Ralph handler used by the MCP composition root.

    The plain ``RalphHandler(...)`` constructor intentionally supports tests and
    plugin-only dispatch, but for in-process/job dispatch it creates a handler
    whose ``EvolveStepHandler`` has no ``EvolutionaryLoop``. That leaks through
    the direct CLI ``ooo auto --complete-product`` path as a background Ralph
    failure: ``EvolutionaryLoop not configured``. Reuse the production MCP
    composition root so CLI and MCP auto handoffs get the same executor,
    evaluator, validator, event store, and job manager wiring.
    """
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    server = create_ouroboros_server(runtime_backend=runtime, opencode_mode=opencode_mode)
    handler = server._tool_handlers["ouroboros_ralph"]  # noqa: SLF001
    if not isinstance(handler, RalphHandler):
        msg = "MCP composition root returned non-Ralph handler for ouroboros_ralph"
        raise TypeError(msg)
    return handler


class AgentRuntimeBackend(str, Enum):  # noqa: UP042
    """Supported runtime backends for auto execution handoff."""

    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"
    HERMES = "hermes"
    GEMINI = "gemini"
    COPILOT = "copilot"
    KIRO = "kiro"
    PI = "pi"
    GJC = "gjc"
    ANTIGRAVITY = "antigravity"
    GROK = "grok"


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
                "Maximum auto interview rounds. Defaults to 50 for new sessions and "
                "to the persisted bound on resume; explicit values raise (never lower) "
                "the bound. The interview closes early once ambiguity converges or "
                "stagnation-driven lateral steps exhaust, so the cap is rarely reached."
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
    no_wait: Annotated[
        bool,
        typer.Option(
            "--no-wait",
            help=(
                "Fire-and-forget: return as soon as the execute run-handoff job is "
                "started instead of waiting for it to finish. WARNING: in direct CLI "
                "mode the in-process job dies with the CLI, so detached execution does "
                "NOT survive process exit unless a persistent owner (e.g. a running "
                "'ouroboros mcp serve') holds it. Default off: auto waits for the run "
                "job to reach a terminal state and reports the verdict."
            ),
        ),
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
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            help="Suppress live phase/grade/repair progress lines; only the final summary prints.",
        ),
    ] = False,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            help=(
                "Top-level pipeline deadline in seconds. Defaults to "
                f"{DEFAULT_PIPELINE_TIMEOUT_SECONDS:g}s (2h) for new sessions. "
                f"Range: {MIN_PIPELINE_TIMEOUT_SECONDS:g}-{MAX_PIPELINE_TIMEOUT_SECONDS:g}. "
                "On resume the deadline is preserved across process restarts; "
                "passing --timeout on resume is rejected."
            ),
        ),
    ] = None,
    complete_product: Annotated[
        bool,
        typer.Option(
            "--complete-product",
            help=(
                "Chain RUN → RALPH_HANDOFF after a successful run handoff so a "
                "single ooo auto invocation iterates Ralph until QA passes, "
                "convergence, or a budget bound trips. Default off."
            ),
        ),
    ] = False,
    domain: Annotated[
        str | None,
        typer.Option(
            "--domain",
            hidden=True,
            help=(
                "Explicitly activate a domain profile by name (e.g. 'coding'). "
                "Overrides auto-detection. Use 'ooo auto --domain coding <goal>' "
                "to force the coding profile regardless of cwd."
            ),
        ),
    ] = None,
    commit_policy: Annotated[
        str | None,
        typer.Option(
            "--commit-policy",
            hidden=True,
            help="Checkpoint policy: ac_checkpoint, final_only, or none.",
        ),
    ] = None,
    worktree_policy: Annotated[
        str | None,
        typer.Option(
            "--worktree-policy",
            hidden=True,
            help="Worktree isolation policy: auto, always, current, or none.",
        ),
    ] = None,
) -> None:
    """Run an A-grade-gated auto pipeline.

    By default the command waits for the execute run-handoff job to reach a
    terminal state and reports the run verdict, because in direct CLI mode the
    in-process job manager dies with the process and would otherwise cancel the
    run on exit. Pass ``--no-wait`` to restore fire-and-forget behaviour.
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
    if timeout is not None and not (
        MIN_PIPELINE_TIMEOUT_SECONDS <= timeout <= MAX_PIPELINE_TIMEOUT_SECONDS
    ):
        print_error(
            f"--timeout must be between {MIN_PIPELINE_TIMEOUT_SECONDS:g} and "
            f"{MAX_PIPELINE_TIMEOUT_SECONDS:g} seconds"
        )
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
                pipeline_timeout_seconds=timeout,
                complete_product=complete_product,
                domain=domain,
                commit_policy=commit_policy,
                worktree_policy=worktree_policy,
                progress_callback=_make_progress_renderer(quiet=quiet),
                wait=not no_wait,
            )
        )
    except typer.Exit:
        raise
    except Exception as exc:
        print_error(f"Auto pipeline failed: {exc}")
        raise typer.Exit(1) from exc

    _print_result(result, show_ledger=show_ledger)
    if no_wait and _is_run_handoff_only_completion(result) and result.job_id:
        print_warning(
            "Detached with --no-wait: the execute job runs in this CLI process only. "
            "It will NOT survive process exit unless a persistent owner (e.g. a running "
            "'ouroboros mcp serve') holds it. Re-run without --no-wait to wait for the "
            f"run verdict, or track it with: ouroboros job wait {result.job_id}"
        )
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


_DEFAULT_MAX_INTERVIEW_ROUNDS = 50
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
    pipeline_timeout_seconds: float | None = None,
    complete_product: bool = False,
    domain: str | None = None,
    commit_policy: str | None = None,
    worktree_policy: str | None = None,
    progress_callback: AutoProgressCallback | None = None,
    wait: bool = True,
) -> AutoPipelineResult:
    store = AutoStore()
    runtime_override = runtime
    incoming_provenance = resolve_provenance()
    attach_requested = any(
        isinstance(item, str) and item.strip()
        for item in (attach_execution, attach_job, attach_session)
    )
    if attach_requested and not resume:
        raise ValueError("--attach-execution/--attach-job/--attach-session require --resume")
    if reconcile_run and not resume:
        raise ValueError("--reconcile-run requires --resume")
    validate_complete_product_timeout(
        complete_product=complete_product and not bool(resume),
        pipeline_timeout_seconds=pipeline_timeout_seconds,
        option_name="--timeout",
    )
    if resume:
        if pipeline_timeout_seconds is not None:
            raise ValueError(
                "--timeout cannot be changed on resume; the original deadline "
                "is preserved across process restarts"
            )
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
        # Q00/ouroboros#773 (review-3): ``--complete-product`` is durable
        # session intent, not a per-invocation flag. Honor the persisted value
        # on resume so a session originally started with ``--complete-product``
        # keeps chaining RUN → RALPH_HANDOFF even when the operator forgets to
        # re-pass the flag. Lowering on resume is rejected to mirror the
        # ``--max-*-rounds`` policy: a bound that already shaped behavior must
        # be raised explicitly, never silently tightened.
        if state.complete_product and not complete_product:
            complete_product = True
        elif complete_product and not state.complete_product:
            state.complete_product = True
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
        state.complete_product = complete_product
        if pipeline_timeout_seconds is not None:
            state.pipeline_timeout_seconds = float(pipeline_timeout_seconds)

    # 3-step domain profile activation (PR-3, Q00/ouroboros#809 P3):
    # 1. --domain explicit flag wins.
    # 2. Otherwise, auto-detect from cwd via DEFAULT_REGISTRY.detect_best.
    # 3. Otherwise, None (current baked-in behavior remains in charge).
    if domain is not None:
        active_profile = DEFAULT_REGISTRY.get(domain)
        if active_profile is None:
            print_error(
                f"Unknown domain profile: {domain!r}. Register it with DEFAULT_REGISTRY before use."
            )
            raise typer.Exit(1)
        state.active_domain_profile_name = active_profile.name
        apply_domain_policy_defaults(state)
    elif not resume:
        active_profile = DEFAULT_REGISTRY.detect_best(Path(state.cwd))
        state.active_domain_profile_name = active_profile.name if active_profile else None
        apply_domain_policy_defaults(state)
    else:
        # Resume preserves the session-start profile unless the operator
        # explicitly passes --domain to intentionally retarget it.
        pass
    if commit_policy is not None:
        state.commit_policy = AutoCommitPolicy(commit_policy)
    if worktree_policy is not None:
        state.worktree_policy = parse_auto_worktree_policy(worktree_policy)

    runtime_plan = resolve_auto_stage_runtime_plan(
        runtime_override=runtime_override,
        fallback_runtime_backend=runtime,
        fallback_opencode_mode=state.opencode_mode or get_opencode_mode(),
    )
    runtime = runtime_plan.default.runtime_backend
    raw_default_opencode_mode = runtime_plan.default.opencode_mode

    if runtime == "opencode":
        # Q00/ouroboros#782 review-7 BLOCKING #3 + review-8 BLOCKING #1: keep
        # the un-demoted opencode_mode for the Ralph handoff so a CLI launched
        # from *inside* an OpenCode plugin session can still dispatch the new
        # ``--complete-product`` Ralph loop via the plugin ``_subagent``
        # envelope (matching the MCP entrypoint's behavior). The historical
        # CLI demotion to ``subprocess`` was uniform and therefore disabled
        # the plugin Ralph contract introduced by this PR.
        #
        # Persist the un-demoted value as ``state.ralph_opencode_mode`` and
        # honor a previously persisted value on resume — ``state.opencode_mode``
        # itself stores the demoted form (used by authoring/run-handoff
        # handlers), so it cannot serve as a source of truth for plugin Ralph.
        ralph_opencode_mode = state.ralph_opencode_mode or raw_default_opencode_mode
        opencode_mode = ralph_opencode_mode
        opencode_mode = demote_plugin_opencode_mode(opencode_mode)
    else:
        opencode_mode = None
        ralph_opencode_mode = None
    state.runtime_backend = runtime
    state.opencode_mode = opencode_mode
    state.ralph_opencode_mode = ralph_opencode_mode
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

    auto_workspace = ensure_auto_worktree(state)

    authoring_opencode_mode = demote_plugin_opencode_mode(runtime_plan.interview.opencode_mode)
    execute_opencode_mode = demote_plugin_opencode_mode(runtime_plan.execute.opencode_mode)
    interview = InterviewHandler(
        agent_runtime_backend=runtime_plan.interview.runtime_backend,
        opencode_mode=authoring_opencode_mode,
    )
    generate_seed = GenerateSeedHandler(
        agent_runtime_backend=runtime_plan.interview.runtime_backend,
        opencode_mode=authoring_opencode_mode,
    )
    execute_seed = ExecuteSeedHandler(
        agent_runtime_backend=runtime_plan.execute.runtime_backend,
        opencode_mode=execute_opencode_mode,
    )
    start_execute = StartExecuteSeedHandler(
        execute_handler=execute_seed,
        agent_runtime_backend=runtime_plan.execute.runtime_backend,
        opencode_mode=execute_opencode_mode,
    )
    seed_qa = QAHandler(
        agent_runtime_backend=runtime_plan.interview.runtime_backend,
        opencode_mode=authoring_opencode_mode,
    )
    # Parity with the MCP auto path (auto_handler.py): construct a
    # ``lateral_thinker`` so the interview safe-default escalation (Issue
    # #1248) and the EVALUATE → UNSTUCK_LATERAL path have the same lateral
    # handle the MCP handler wires. Gate on the resolved REFLECT stage: a
    # plugin-routed reflect backend cannot be consumed by the synchronous
    # auto pipeline, so leave ``lateral_thinker=None`` in that case (the
    # pre-existing BLOCKED branches still apply, preserving prior behaviour).
    reflect_plugin_mode = should_dispatch_via_plugin(
        runtime_plan.reflect.runtime_backend,
        runtime_plan.reflect.opencode_mode,
    )
    lateral_thinker = None
    if not reflect_plugin_mode:
        lateral_thinker = HandlerLateralThinker(
            LateralThinkHandler(
                agent_runtime_backend=runtime_plan.reflect.runtime_backend,
                opencode_mode=demote_plugin_opencode_mode(runtime_plan.reflect.opencode_mode),
            )
        )
    # AI answer refiner: upgrades generic deterministic auto-answers to concrete,
    # goal-specific ones so interview ambiguity actually converges. Best-effort —
    # any construction failure leaves the deterministic answerer untouched.
    answer_refiner = build_answer_refiner()
    driver = AutoInterviewDriver(
        HandlerInterviewBackend(interview, cwd=state.cwd),
        store=store,
        max_rounds=max_interview_rounds,
        timeout_seconds=state.phase_timeout_seconds(AutoPhase.INTERVIEW),
        lateral_thinker=lateral_thinker,
        answer_refiner=answer_refiner,
    )
    ralph_handler = (
        # Q00/ouroboros#782 review-7/8/10: pass the un-demoted
        # ``ralph_opencode_mode`` so an OpenCode plugin session can take the
        # plugin ``_subagent`` dispatch path. ``opencode_mode`` (demoted) is
        # still correct for the authoring/run-handoff handlers above.
        # Q00/ouroboros#1090: use the full MCP composition root rather than a
        # bare RalphHandler so job-mode Ralph has an EvolutionaryLoop.
        _build_configured_ralph_handler(
            runtime=runtime_plan.execute.runtime_backend,
            opencode_mode=(
                state.ralph_opencode_mode or runtime_plan.execute.opencode_mode
                if runtime_plan.execute.runtime_backend == "opencode"
                else None
            ),
        )
        if complete_product
        else None
    )
    ralph_starter = (
        HandlerRalphStarter(ralph_handler, project_dir=state.cwd)
        if ralph_handler is not None
        else None
    )
    # Q00/ouroboros#773 (review-5 finding 1): wire a poller backed by the same
    # ``RalphHandler`` so a session interrupted in ``RALPH_HANDOFF`` (e.g.
    # client disconnects while the background Ralph job keeps running) can
    # actually be reconciled to ``COMPLETE`` / ``BLOCKED`` / ``FAILED`` on
    # ``--resume`` instead of being stranded in the non-terminal handoff
    # state forever. Sharing the handler reuses the same ``JobManager``
    # (and underlying ``EventStore``) so the poller sees the persisted job.
    ralph_resumer = HandlerRalphPoller(ralph_handler) if ralph_handler is not None else None
    watchdog_event_store = EventStore()
    await watchdog_event_store.initialize()
    watchdog = Watchdog(
        controls=load_runtime_controls(None),
        event_appender=watchdog_event_store,
    )
    pipeline = AutoPipeline(
        driver,
        HandlerSeedGenerator(generate_seed),
        run_starter=(
            HandlerSynchronousRunStarter(execute_seed, cwd=state.cwd)
            if complete_product
            else HandlerRunStarter(
                start_execute,
                cwd=state.cwd,
                use_worktree=auto_workspace is None,
            )
        ),
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
        ralph_starter=ralph_starter,
        ralph_resumer=ralph_resumer,
        complete_product=complete_product,
        seed_qa_evaluator=HandlerSeedQAEvaluator(seed_qa),
        lateral_thinker=lateral_thinker,
        progress_callback=progress_callback,
        watchdog=watchdog,
        probe_runner=EnvRuntimeProbeRunner() if complete_product else None,
    )
    try:
        result = await pipeline.run(state)
        if wait and _is_run_handoff_only_completion(result) and result.job_id:
            # Direct CLI mode has no long-lived owner for the in-process job:
            # once _run_auto returns, ``asyncio.run`` teardown cancels the
            # pending execute-job task (see mcp/job_manager.py drain docstring
            # + the _run_job CancelledError handler), so the run would die at
            # ~200ms without ever executing. Keep the event loop alive and
            # stream the run to a terminal verdict before returning.
            result = await _await_run_handoff_terminal(
                result,
                job_manager=getattr(start_execute, "_job_manager", None),
                event_store=getattr(start_execute, "_event_store", None),
                quiet=progress_callback is None,
            )
    finally:
        release_auto_worktree(auto_workspace)
        await watchdog_event_store.close()
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


def _make_progress_renderer(*, quiet: bool) -> AutoProgressCallback | None:
    """Build a callback that prints live phase/grade/repair lines, unless quiet."""
    if quiet:
        return None

    def render(event: AutoProgressEvent) -> None:
        if event.kind == "question":
            label = f"question round {event.round}" if event.round is not None else "question"
            text = _rich_escape((event.question or event.message).strip())
            console.print(rf"[dim]\[auto][/] {label} — {text}")
            return
        if event.kind == "answer":
            label = f"answer round {event.round}" if event.round is not None else "answer"
            source = rf" \[{_rich_escape(event.answer_source)}]" if event.answer_source else ""
            question = _rich_escape((event.question or "").strip())
            answer = _rich_escape((event.answer or event.message).strip())
            if question:
                console.print(rf"[dim]\[auto][/] {label}{source} Q — {question}")
            console.print(rf"[dim]\[auto][/] {label}{source} A — {answer}")
            return
        if event.kind == "grade":
            label = f"grade {event.grade}" if event.grade else "grade"
        elif event.kind == "repair":
            label = f"repair round {event.round}"
        else:
            label = event.phase
        # A live trace should be a single dim line per event, not a Rich
        # panel — using ``console.print`` keeps the stream lightweight so
        # consumers can grep on the ``[auto]`` prefix without parsing
        # panel chrome. The leading ``[`` is escaped so Rich treats
        # ``[auto]`` as literal text rather than as a markup style name.
        console.print(rf"[dim]\[auto][/] {label} — {event.message}")

    return render


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
        question = _rich_escape(state.pending_question.strip())
        console.print("Pending question:")
        console.print(f"  {question}")
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
    intent_guard = diagnose_auto_pipeline_state(state)
    console.print(f"IntentGuard: [bold]{intent_guard.status.value}[/]")
    for check in intent_guard.checks:
        message = _rich_escape(check.message)
        action = f" Action: {_rich_escape(check.action)}" if check.action else ""
        console.print(f"  {check.status.value.upper()} {check.code}: {message}{action}")
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
    # Only emit a "Start fresh" hint for terminal-but-recoverable signals
    # (BLOCKED/FAILED). COMPLETE is terminal *and* successful — no hint.
    goal_for_hint = state.goal if state.phase is not AutoPhase.COMPLETE else None
    for line in render_resume_lines(
        state.resume_capability(),
        state.auto_session_id,
        goal=goal_for_hint,
        use_markup=True,
    ):
        console.print(line)


def _is_run_handoff_only_completion(result: AutoPipelineResult) -> bool:
    """True when auto completed only by handing off execution work.

    The persisted pipeline marks this state COMPLETE to avoid duplicate run
    starts on resume, but it is not verified product completion yet.
    """
    return (
        result.status == "complete"
        and result.run_handoff_status == RUN_HANDOFF_STARTED_STATUS
        and result.execution_job_status != "completed"
        and not result.ralph_job_id
        and not result.ralph_lineage_id
    )


def _is_completed_ralph_product(result: AutoPipelineResult) -> bool:
    """True when ``--complete-product`` reached a terminal Ralph completion."""
    return (
        result.status == "complete"
        and result.ralph_dispatch_mode != "plugin"
        and bool(result.ralph_job_id or result.ralph_lineage_id)
    )


def _is_external_ralph_plugin_completion(result: AutoPipelineResult) -> bool:
    """True when auto is complete but product work lives in an OpenCode child."""
    return result.status == "complete" and result.ralph_dispatch_mode == "plugin"


# Long-poll window (seconds) for each ``JobWaitHandler`` call while streaming
# the run-handoff job to a terminal verdict. The handler returns early on any
# AC/phase progress or terminal status, so this is only an upper bound on how
# long a fully-idle poll blocks before we re-check the snapshot.
_RUN_HANDOFF_WAIT_POLL_SECONDS = 5


async def _cancel_run_handoff_job(job_manager: JobManager, job_id: str) -> None:
    """Best-effort cancel of the in-process run job (Ctrl-C / teardown path)."""
    cancel_job = getattr(job_manager, "cancel_job", None)
    if cancel_job is None:
        return
    with contextlib.suppress(Exception):
        await cancel_job(job_id)


def _reconcile_run_handoff_result(
    result: AutoPipelineResult, snapshot: JobSnapshot
) -> AutoPipelineResult:
    """Project a terminal execute-job snapshot back onto the auto result.

    Mirrors ``mcp/tools/auto_handler._reconcile_execution_job_snapshot`` so the
    CLI wait path and the MCP resume path report the same verdict, but reuses
    the live job manager's snapshot instead of re-opening the event store.
    """
    status = result.status
    blocker = result.blocker
    resume_capability = result.resume_capability
    if snapshot.status is JobStatus.COMPLETED:
        status = "complete"
        blocker = None
        resume_capability = AutoResumeCapability.NONE
    elif snapshot.status in {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}:
        detail = snapshot.error or snapshot.result_text or snapshot.message
        blocker = (
            f"execution job {snapshot.status.value}: {detail}"
            if detail
            else f"execution job {snapshot.status.value}"
        )
        status = "failed" if snapshot.status is JobStatus.FAILED else "blocked"
        resume_capability = AutoResumeCapability.NONE
    else:
        # Non-terminal snapshot (should not happen after a terminal wait): keep
        # the handoff-only result intact and only surface the live status.
        return replace(result, execution_job_status=snapshot.status.value)
    return replace(
        result,
        status=status,
        phase="complete" if status == "complete" else result.phase,
        blocker=blocker,
        resume_capability=resume_capability,
        execution_job_status=snapshot.status.value,
        execution_job_error=snapshot.error,
        execution_job_message=snapshot.message,
    )


async def _await_run_handoff_terminal(
    result: AutoPipelineResult,
    *,
    job_manager: JobManager | None,
    event_store: EventStore | None,
    quiet: bool,
) -> AutoPipelineResult:
    """Wait for the started execute job to finish, streaming run progress.

    Returns the original ``result`` unchanged when there is nothing to wait on
    (no job handle, or the job is not owned by this manager — e.g. a plugin
    dispatch). Otherwise polls the live job manager to a terminal state, prints
    the run receipt via the existing ``ouroboros_job_result`` renderer, and
    reconciles the run verdict onto the returned result.
    """
    job_id = result.job_id
    if not job_id or job_manager is None or event_store is None:
        return result
    try:
        snapshot = await job_manager.get_snapshot(job_id)
    except ValueError:
        # Job handle not owned by this manager (e.g. plugin-mode dispatch):
        # honestly leave the handoff-only result as-is for the caller to render.
        return result

    if not quiet:
        print_info(
            f"Waiting for run job {job_id} to finish (Ctrl-C to cancel the run and detach)..."
        )
    wait_handler = JobWaitHandler(job_manager=job_manager, event_store=event_store)
    cursor = 0
    last_line = ""
    try:
        while not snapshot.is_terminal:
            wait_result = await wait_handler.handle(
                {
                    "job_id": job_id,
                    "cursor": cursor,
                    "timeout_seconds": _RUN_HANDOFF_WAIT_POLL_SECONDS,
                    "view": "compact",
                    "wait_for": "ac_change",
                }
            )
            if wait_result.is_err:
                break
            value = wait_result.value
            meta = value.meta or {}
            with contextlib.suppress(TypeError, ValueError):
                cursor = int(meta.get("cursor", cursor))
            line = (value.text_content or "").strip()
            if not quiet and meta.get("changed") and line and line != last_line:
                last_line = line
                console.print(rf"[dim]\[run][/] {_rich_escape(line)}")
            if meta.get("is_terminal"):
                break
            snapshot = await job_manager.get_snapshot(job_id)
    except (asyncio.CancelledError, KeyboardInterrupt):
        await _cancel_run_handoff_job(job_manager, job_id)
        print_warning(f"Interrupted — cancelled run job {job_id}")
        raise

    snapshot = await job_manager.get_snapshot(job_id)
    if not quiet:
        result_handler = JobResultHandler(job_manager=job_manager, event_store=event_store)
        receipt = await result_handler.handle({"job_id": job_id})
        if receipt.is_ok and receipt.value.text_content:
            console.print(receipt.value.text_content)
    return _reconcile_run_handoff_result(result, snapshot)


def _print_detached_guidance(result: AutoPipelineResult) -> None:
    """Print stable handles and wait/retrieve commands for detached auto work."""
    console.print("Detached result handles:")
    console.print(f"  Auto session ID: {result.auto_session_id}")
    if result.job_id:
        console.print(f"  Execution job ID: {result.job_id}")
    if result.ralph_job_id:
        console.print(f"  Ralph job ID: {result.ralph_job_id}")
    if result.ralph_lineage_id:
        console.print(f"  Ralph lineage ID: {result.ralph_lineage_id}")

    console.print(f"Wait: ooo auto --resume {result.auto_session_id}")
    console.print(f"Retrieve: ooo auto --status --resume {result.auto_session_id}")
    if result.ralph_job_id:
        console.print(f"Wait job (CLI): ouroboros job wait {result.ralph_job_id}")
        console.print(f"Retrieve job (CLI): ouroboros job result {result.ralph_job_id}")
        console.print(f'Wait job (MCP): ouroboros_job_wait(job_id="{result.ralph_job_id}")')
        console.print(f'Retrieve job (MCP): ouroboros_job_result(job_id="{result.ralph_job_id}")')


def _print_result(result: AutoPipelineResult, *, show_ledger: bool) -> None:
    handoff_only = _is_run_handoff_only_completion(result)
    completed_ralph_product = _is_completed_ralph_product(result)
    external_ralph_plugin = _is_external_ralph_plugin_completion(result)
    if handoff_only:
        print_info("Auto run handoff started")
    elif result.status == "detached":
        print_info("Auto pipeline detached")
    elif result.status == "complete":
        print_success("Auto pipeline completed")
    elif result.status in {"blocked", "failed"}:
        print_error("Auto pipeline did not complete")
    else:
        print_info("Auto pipeline status")
    console.print(f"Auto session: [cyan]{result.auto_session_id}[/]")
    displayed_status = "run_handoff_started" if handoff_only else result.status
    console.print(f"Status: [bold]{displayed_status}[/]")
    if result.artifact_state:
        console.print(f"Artifact state: [bold]{result.artifact_state}[/]")
    if handoff_only:
        console.print(
            "Product status: [yellow]not verified complete; execution is still external/pending[/]"
        )
    elif result.status == "detached":
        console.print(
            "Product status: [yellow]not verified complete; background work is still running[/]"
        )
    elif completed_ralph_product:
        console.print("Product status: [green]completed by Ralph loop[/]")
    elif external_ralph_plugin:
        console.print(
            "Product status: [yellow]not verified complete; Ralph loop is external/pending[/]"
        )
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
    if result.run_handoff_status and not (
        completed_ralph_product
        and result.run_handoff_status in _STALE_COMPLETED_RALPH_HANDOFF_STATUSES
    ):
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
    if result.status == "detached":
        _print_detached_guidance(result)
    if result.checkpoint_commits:
        console.print("Checkpoint commits:")
        for entry in result.checkpoint_commits:
            console.print(f"  {entry.get('ac_id')}: {entry.get('commit')}")
    if show_ledger:
        if result.assumptions:
            console.print("Assumptions:")
            for item in result.assumptions:
                console.print(f"  - {item}")
        if result.assumption_sources:
            console.print("Assumption sources:")
            for record in result.assumption_sources:
                console.print(
                    f"  - source={_rich_escape(record.source)}; "
                    f"confidence={record.confidence:.2f}; "
                    f"text={_rich_escape(record.text)}"
                )
        if result.defaulted_sections:
            console.print("Defaulted sections:")
            for item in result.defaulted_sections:
                console.print(f"  - {_rich_escape(item)}")
        if result.non_goals:
            console.print("Non-goals:")
            for item in result.non_goals:
                console.print(f"  - {item}")
    if result.blocker:
        console.print(f"Blocker: [yellow]{result.blocker}[/]")
    for line in render_resume_lines(
        result.resume_capability,
        result.auto_session_id,
        goal=None,
        use_markup=True,
    ):
        console.print(line)


__all__ = ["app"]
