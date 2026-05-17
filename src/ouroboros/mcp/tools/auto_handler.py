"""MCP handler for full-quality ``ooo auto`` sessions."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import difflib
import inspect
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from ouroboros.auto.adapters import (
    HandlerEvaluator,
    HandlerInterviewBackend,
    HandlerLateralThinker,
    HandlerRalphPoller,
    HandlerRalphStarter,
    HandlerRunStarter,
    HandlerSeedGenerator,
    load_seed,
    save_seed,
)
from ouroboros.auto.answerer import (
    AutoAnswerContext,
    risky_user_preference_blocker_for,
)
from ouroboros.auto.execution_acceptance import (
    has_auto_wrapper_context,
    is_auto_reporting_acceptance_criterion,
)
from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.ledger import (
    REQUIRED_SECTIONS,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.pipeline import AutoPipeline, AutoPipelineResult
from ouroboros.auto.repo_context import repo_auto_answer_context
from ouroboros.auto.resume_render import render_resume_lines
from ouroboros.auto.seed_repairer import SeedRepairer
from ouroboros.auto.state import (
    DEFAULT_PIPELINE_TIMEOUT_SECONDS,
    MAX_PIPELINE_TIMEOUT_SECONDS,
    MIN_PIPELINE_TIMEOUT_SECONDS,
    TERMINAL_PHASES,
    AutoPhase,
    AutoPipelineState,
    AutoResumeCapability,
    AutoStore,
)
from ouroboros.config import get_opencode_mode
from ouroboros.core.file_lock import file_lock
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.evaluation_handlers import LateralThinkHandler
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler, StartExecuteSeedHandler
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.mcp.tools.ralph_handlers import RalphHandler
from ouroboros.mcp.tools.subagent import (
    build_subagent_payload,
    build_subagent_result,
    emit_subagent_dispatched_event,
    should_dispatch_via_plugin,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator import resolve_agent_runtime_backend
from ouroboros.orchestrator.agent_process import run_with_agent_process
from ouroboros.orchestrator.heartbeat import current_process_identity, is_process_identity_alive
from ouroboros.persistence.event_store import EventStore

_START_AUTO_PENDING_LEASE_SECONDS = 60.0


@dataclass(slots=True)
class AutoHandler:
    """Run a bounded goal → A-grade Seed → execution handoff pipeline."""

    interview_handler: InterviewHandler | None = field(default=None, repr=False)
    generate_seed_handler: GenerateSeedHandler | None = field(default=None, repr=False)
    start_execute_seed_handler: StartExecuteSeedHandler | None = field(default=None, repr=False)
    store: AutoStore | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)
    mcp_manager: object | None = field(default=None, repr=False)
    mcp_tool_prefix: str = ""

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_auto",
            description=(
                "Run full-quality ooo auto: automatically interview, generate an A-grade Seed, "
                "and start execution only after the A-grade gate passes. All loops are bounded."
            ),
            parameters=(
                MCPToolParameter(
                    "goal", ToolInputType.STRING, "Goal/task for ooo auto", required=False
                ),
                MCPToolParameter("cwd", ToolInputType.STRING, "Working directory", required=False),
                MCPToolParameter(
                    "resume", ToolInputType.STRING, "Auto session id to resume", required=False
                ),
                MCPToolParameter(
                    "max_interview_rounds",
                    ToolInputType.INTEGER,
                    "Max interview rounds",
                    required=False,
                    default=12,
                ),
                MCPToolParameter(
                    "max_repair_rounds",
                    ToolInputType.INTEGER,
                    "Max repair rounds",
                    required=False,
                    default=5,
                ),
                MCPToolParameter(
                    "skip_run",
                    ToolInputType.BOOLEAN,
                    "Stop after A-grade Seed",
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    "attach_execution",
                    ToolInputType.STRING,
                    "Attach an externally verified execution id to an unknown run handoff",
                    required=False,
                ),
                MCPToolParameter(
                    "attach_job",
                    ToolInputType.STRING,
                    "Attach an externally verified job id to an unknown run handoff",
                    required=False,
                ),
                MCPToolParameter(
                    "attach_session",
                    ToolInputType.STRING,
                    "Attach an externally verified run session id to an unknown run handoff",
                    required=False,
                ),
                MCPToolParameter(
                    "attach_source",
                    ToolInputType.STRING,
                    "Source label for an attached run handle",
                    required=False,
                ),
                MCPToolParameter(
                    "reconcile_run",
                    ToolInputType.BOOLEAN,
                    "Try to reconcile an unknown run handoff without starting a duplicate run",
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    "reconcile_source",
                    ToolInputType.STRING,
                    "Source label for run handoff reconciliation",
                    required=False,
                ),
                MCPToolParameter(
                    "pipeline_timeout_seconds",
                    ToolInputType.NUMBER,
                    (
                        "Top-level pipeline deadline in seconds. Defaults to "
                        f"{DEFAULT_PIPELINE_TIMEOUT_SECONDS:g}s for new sessions. "
                        f"Range: {MIN_PIPELINE_TIMEOUT_SECONDS:g}-"
                        f"{MAX_PIPELINE_TIMEOUT_SECONDS:g}. Cannot be changed on "
                        "resume; the deadline is preserved across process restarts."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    "user_preferences",
                    ToolInputType.OBJECT,
                    (
                        "Caller-supplied user preferences keyed by ledger section name "
                        "(e.g. runtime_context, constraints, non_goals). The Driver "
                        "tags matching answers with [from-auto][user_preference] in the "
                        "ledger. Keys must be valid ledger section names; values must "
                        "be non-empty strings or non-empty lists of strings/numbers. "
                        "On resume, null/empty values clear the persisted preference "
                        "for that section."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    "complete_product",
                    ToolInputType.BOOLEAN,
                    (
                        "When true, chain RUN → RALPH_HANDOFF after a successful run "
                        "handoff so a single ouroboros_auto invocation iterates Ralph "
                        "until QA passes, convergence, or a budget bound trips. "
                        "Defaults to false (opt-in)."
                    ),
                    required=False,
                    default=False,
                ),
            ),
        )

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        auto_session_id = _auto_session_id_from_arguments(arguments)
        start_lease_token = _start_auto_lease_token_from_arguments(arguments)
        release_start_lease = False
        store = self.store or AutoStore()
        if auto_session_id is None and start_lease_token is not None:
            return Result.err(
                MCPToolError(
                    "_start_auto_lease_token is reserved for internal start_auto dispatches",
                    tool_name="ouroboros_auto",
                )
            )
        if auto_session_id is not None and start_lease_token is not None:
            token_error = _validate_start_lease_token(store, auto_session_id, start_lease_token)
            if token_error is not None:
                return Result.err(token_error)
            release_start_lease = True
        elif auto_session_id is not None:
            try:
                state = store.load(auto_session_id)
            except ValueError as exc:
                return Result.err(MCPToolError(str(exc), tool_name="ouroboros_auto"))
            start_lease_token, lease_error = _reserve_start_lease(
                store,
                auto_session_id,
                mode="direct_auto",
                ttl_seconds=max(1.0, state.pipeline_timeout_seconds),
            )
            if lease_error is not None:
                return Result.err(lease_error)
            release_start_lease = True
        try:
            result = await self._run(arguments)
        except Exception as exc:
            if auto_session_id is not None and release_start_lease:
                _release_start_lease(store, auto_session_id, token=start_lease_token)
            return Result.err(
                MCPToolError(f"Auto pipeline failed: {exc}", tool_name="ouroboros_auto")
            )
        result = await _reconcile_execution_job_snapshot(result)
        release_session_id = result.auto_session_id or auto_session_id
        if release_session_id and release_start_lease:
            _release_start_lease(store, release_session_id, token=start_lease_token)
        meta = _result_meta(result)
        text = _format_result(result)
        if result.run_subagent is not None:
            meta["_subagent"] = result.run_subagent
            text = json.dumps({**meta, "message": text})
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=result.status in {"blocked", "failed"},
                meta=meta,
            )
        )

    async def _run(self, arguments: dict[str, Any]) -> AutoPipelineResult:
        store = self.store or AutoStore()
        resume = arguments.get("resume")
        requested_skip_run = bool(arguments.get("skip_run", False))
        complete_product = bool(arguments.get("complete_product", False))
        attach_execution = _optional_text_arg(arguments, "attach_execution")
        attach_job = _optional_text_arg(arguments, "attach_job")
        attach_session = _optional_text_arg(arguments, "attach_session")
        attach_source = _optional_text_arg(arguments, "attach_source")
        reconcile_run = bool(arguments.get("reconcile_run", False))
        reconcile_source = _optional_text_arg(arguments, "reconcile_source")
        attach_requested = any((attach_execution, attach_job, attach_session))
        if attach_requested and not (isinstance(resume, str) and resume):
            raise ValueError("attach_* arguments require resume")
        if reconcile_run and not (isinstance(resume, str) and resume):
            raise ValueError("reconcile_run requires resume")
        pipeline_timeout_seconds = _optional_pipeline_timeout(arguments)
        if pipeline_timeout_seconds is not None and isinstance(resume, str) and resume:
            raise ValueError(
                "pipeline_timeout_seconds cannot be changed on resume; the "
                "original deadline is preserved across process restarts"
            )
        # Distinguish "caller did not pass user_preferences" from "caller
        # passed an empty mapping". Only validate/parse when the caller
        # actually supplied the arg so a resume call can defer to persisted
        # state without being forced to resupply.
        user_preferences_supplied = (
            "user_preferences" in arguments and arguments.get("user_preferences") is not None
        )
        supplied_user_preferences: dict[str, str | None] = {}
        if isinstance(resume, str) and resume:
            state = store.load(resume)
            cwd = state.cwd
            runtime_backend = state.runtime_backend or self.agent_runtime_backend
            if runtime_backend is None and state.opencode_mode is not None:
                runtime_backend = "opencode"
            runtime_backend = resolve_agent_runtime_backend(runtime_backend)
            opencode_mode = _resolved_opencode_mode(
                runtime_backend, state.opencode_mode or self.opencode_mode
            )
            max_interview_rounds = state.max_interview_rounds
            max_repair_rounds = state.max_repair_rounds
            skip_run = requested_skip_run or state.skip_run
            # Resume contract: caller-supplied preferences override persisted
            # ones; otherwise the original session's preferences are reused so
            # the same input converges to the same Seed.
            if user_preferences_supplied:
                supplied_user_preferences = _parse_user_preferences(
                    arguments.get("user_preferences"),
                    allow_deletions=True,
                )
                state.user_preferences = _merge_resume_user_preferences(
                    state.goal,
                    state.user_preferences,
                    supplied_user_preferences,
                )
                state.ledger = _reseed_preference_ledger(
                    state.goal,
                    state.ledger,
                    state.user_preferences,
                ).to_dict()
            # Q00/ouroboros#773 (review-3): ``complete_product`` is durable
            # session intent, not a per-invocation flag. Honor the persisted
            # value so MCP callers that omit ``complete_product`` on resume
            # still chain RUN → RALPH_HANDOFF for sessions that originally
            # opted in. Mirrors the CLI policy in ``cli/commands/auto.py``.
            if state.complete_product and not complete_product:
                complete_product = True
            elif complete_product and not state.complete_product:
                state.complete_product = True
        else:
            supplied_user_preferences = (
                _parse_user_preferences(arguments.get("user_preferences"))
                if user_preferences_supplied
                else {}
            )
            goal = arguments.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                raise ValueError("goal is required when not resuming")
            cwd = str(_resolve_cwd(arguments.get("cwd")))
            runtime_backend = resolve_agent_runtime_backend(self.agent_runtime_backend)
            opencode_mode = _resolved_opencode_mode(runtime_backend, self.opencode_mode)
            max_interview_rounds = _positive_int_arg(arguments, "max_interview_rounds", 12)
            max_repair_rounds = _positive_int_arg(arguments, "max_repair_rounds", 5)
            skip_run = requested_skip_run
            goal_text = goal.strip()
            state = AutoPipelineState(goal=goal_text, cwd=cwd)
            state.user_preferences = _merge_goal_user_preferences(
                goal_text, supplied_user_preferences
            )
            state.ledger = _seed_initial_ledger_from_user_preferences(
                goal_text, state.user_preferences
            ).to_dict()
            state.max_interview_rounds = max_interview_rounds
            state.max_repair_rounds = max_repair_rounds
            state.complete_product = complete_product
            if pipeline_timeout_seconds is not None:
                state.pipeline_timeout_seconds = pipeline_timeout_seconds
        state.runtime_backend = runtime_backend
        state.opencode_mode = opencode_mode
        # Q00/ouroboros#782 review-8 BLOCKING #1: persist the (un-demoted)
        # mode for the Ralph handoff so resume reconstructs plugin Ralph
        # dispatch even when the historical demoted form is later relied on
        # for authoring/run-handoff handlers (matches the CLI behavior).
        if opencode_mode is not None:
            state.ralph_opencode_mode = state.ralph_opencode_mode or opencode_mode
        state.skip_run = skip_run

        authoring_opencode_mode = "subprocess" if opencode_mode == "plugin" else opencode_mode
        interview_handler = _authoring_interview_handler(
            self.interview_handler,
            llm_backend=self.llm_backend,
            agent_runtime_backend=runtime_backend,
            opencode_mode=authoring_opencode_mode,
        )
        generate_seed_handler = _authoring_seed_handler(
            self.generate_seed_handler,
            llm_backend=self.llm_backend,
            agent_runtime_backend=runtime_backend,
            opencode_mode=authoring_opencode_mode,
        )
        start_execute = _execution_start_handler(
            self.start_execute_seed_handler,
            llm_backend=self.llm_backend,
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
            mcp_manager=self.mcp_manager,
            mcp_tool_prefix=self.mcp_tool_prefix,
        )

        context_provider = _build_context_provider(dict(state.user_preferences))
        driver = AutoInterviewDriver(
            HandlerInterviewBackend(interview_handler, cwd=cwd),
            store=store,
            max_rounds=max_interview_rounds,
            timeout_seconds=state.phase_timeout_seconds(AutoPhase.INTERVIEW),
            context_provider=context_provider,
        )
        # Q00/ouroboros#782 review-11 BLOCKING #1: pass the un-demoted
        # ``state.ralph_opencode_mode`` (already populated above at line 251-252,
        # and preserved across CLI-created sessions where ``state.opencode_mode``
        # holds the demoted authoring/run-handoff form) so MCP-side resumes of
        # an OpenCode plugin ``--complete-product`` session take the plugin
        # ``_subagent`` dispatch path instead of silently downgrading Ralph to
        # in-process job mode. Mirrors the CLI fix in ``cli/commands/auto.py``.
        ralph_opencode_mode = state.ralph_opencode_mode or opencode_mode
        ralph_handler = (
            RalphHandler(
                agent_runtime_backend=runtime_backend,
                opencode_mode=ralph_opencode_mode,
            )
            if complete_product
            else None
        )
        ralph_starter = HandlerRalphStarter(ralph_handler) if ralph_handler is not None else None
        # Q00/ouroboros#773 (review-5 finding 1): wire a poller backed by the
        # same ``RalphHandler`` so MCP-side resumes of an interrupted
        # ``RALPH_HANDOFF`` checkpoint actually reconcile the persisted job
        # to a terminal auto phase. The same handler is reused so both the
        # starter and the poller share a ``JobManager`` (and underlying
        # ``EventStore``) — without that share the poller would query a
        # fresh, empty job table.
        ralph_resumer = HandlerRalphPoller(ralph_handler) if ralph_handler is not None else None
        # RFC #809 Phase 2.1 — wire the QA-backed evaluator only when the
        # session is in complete-product mode. Outside that mode the chain
        # is RUN → COMPLETE (async run handoff) so there is no synchronous
        # artifact to grade; instantiating QAHandler would be wasted setup.
        #
        # Plugin-mode skip: ``QAHandler`` / ``LateralThinkHandler`` dispatch
        # to OpenCode Task panes when ``opencode_mode == "plugin"``. The
        # auto pipeline's Phase 2.1/2.2 advisory layer is synchronous and
        # cannot consume out-of-band subagent output, so we leave both
        # adapters unwired in plugin mode. The chain then falls back to
        # the pre-Phase-2.1 behaviour (RUN → RALPH_HANDOFF → COMPLETE) —
        # the existing Ralph plugin delegation continues to drive
        # complete-product sessions in OpenCode Task panes as before.
        evaluator = None
        lateral_thinker = None
        opencode_plugin_mode = opencode_mode == "plugin"
        if complete_product and not opencode_plugin_mode:
            qa_handler = QAHandler(
                llm_backend=self.llm_backend,
                agent_runtime_backend=runtime_backend,
                opencode_mode=opencode_mode,
            )
            evaluator = HandlerEvaluator(qa_handler)
            # RFC #809 Phase 2.2 — wire the persona-driven lateral advisor
            # alongside the evaluator. Same gating: only when complete-product
            # is on and we are NOT in plugin mode.
            lateral_handler = LateralThinkHandler(
                agent_runtime_backend=runtime_backend,
                opencode_mode=opencode_mode,
            )
            lateral_thinker = HandlerLateralThinker(lateral_handler)
        pipeline = AutoPipeline(
            driver,
            HandlerSeedGenerator(generate_seed_handler),
            run_starter=HandlerRunStarter(start_execute, cwd=cwd),
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
            evaluator=evaluator,
            lateral_thinker=lateral_thinker,
        )
        return await pipeline.run(state)


@dataclass
class StartAutoHandler:
    """Start an ``ooo auto`` pipeline in the background and return a job ID.

    The full auto pipeline (Socratic interview + repair loops + seed
    generation + optional run/Ralph handoff) routinely runs longer than
    an MCP client's default tool-call timeout. This handler wraps
    :class:`AutoHandler` in a :class:`JobManager`-backed background job so
    the caller gets a ``job_id`` immediately and polls for the verdict
    via ``ouroboros_job_status`` / ``ouroboros_job_wait`` /
    ``ouroboros_job_result``.

    For new sessions, this handler pre-allocates and persists an
    ``AutoPipelineState`` before enqueuing the job, then runs the inner
    ``AutoHandler`` through the normal resume path. That makes the
    ``auto_session_id`` immediately available for recovery even if the MCP
    server exits before the caller fetches ``ouroboros_job_result``.
    Plugin mode is terminal here: the handler returns an OpenCode subagent
    envelope directly instead of hiding that envelope inside a background
    job result that the bridge cannot intercept.
    """

    interview_handler: InterviewHandler | None = field(default=None, repr=False)
    generate_seed_handler: GenerateSeedHandler | None = field(default=None, repr=False)
    start_execute_seed_handler: StartExecuteSeedHandler | None = field(default=None, repr=False)
    store: AutoStore | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)
    mcp_manager: object | None = field(default=None, repr=False)
    mcp_tool_prefix: str = ""
    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)
        self._store = self.store or AutoStore()
        self._inner_auto = AutoHandler(
            interview_handler=self.interview_handler,
            generate_seed_handler=self.generate_seed_handler,
            start_execute_seed_handler=self.start_execute_seed_handler,
            store=self._store,
            llm_backend=self.llm_backend,
            agent_runtime_backend=self.agent_runtime_backend,
            opencode_mode=self.opencode_mode,
            mcp_manager=self.mcp_manager,
            mcp_tool_prefix=self.mcp_tool_prefix,
        )

    @property
    def definition(self) -> MCPToolDefinition:
        inner_def = self._inner_auto.definition
        return MCPToolDefinition(
            name="ouroboros_start_auto",
            description=(
                "Start ooo auto in the background and return auto_session_id + "
                "job_id immediately. Resume with the returned auto_session_id; "
                "poll with ouroboros_job_status / ouroboros_job_wait and read "
                "final state via ouroboros_job_result."
            ),
            parameters=inner_def.parameters,
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        # Mirror AutoHandler._run's pre-flight: either ``goal`` (new session)
        # or ``resume`` (existing session) must be supplied. We validate
        # eagerly so the caller sees a synchronous contract error instead of
        # a background job that fails immediately.
        resume = arguments.get("resume")
        goal = arguments.get("goal")
        has_resume = isinstance(resume, str) and bool(resume.strip())
        has_goal = isinstance(goal, str) and bool(goal.strip())
        if not has_resume and not has_goal:
            return Result.err(
                MCPToolError(
                    "goal is required when not resuming",
                    tool_name="ouroboros_start_auto",
                )
            )
        try:
            attach_requested = any(
                _optional_text_arg(arguments, field_name)
                for field_name in ("attach_execution", "attach_job", "attach_session")
            )
            requested_pipeline_timeout = _optional_pipeline_timeout(arguments)
        except ValueError as exc:
            return Result.err(MCPToolError(str(exc), tool_name="ouroboros_start_auto"))
        if attach_requested and not has_resume:
            return Result.err(
                MCPToolError(
                    "attach_* arguments require resume",
                    tool_name="ouroboros_start_auto",
                )
            )
        if bool(arguments.get("reconcile_run", False)) and not has_resume:
            return Result.err(
                MCPToolError(
                    "reconcile_run requires resume",
                    tool_name="ouroboros_start_auto",
                )
            )
        if has_resume and requested_pipeline_timeout is not None:
            return Result.err(
                MCPToolError(
                    "pipeline_timeout_seconds cannot be changed on resume; the "
                    "original deadline is preserved across process restarts",
                    tool_name="ouroboros_start_auto",
                )
            )

        runner_arguments = dict(arguments)
        if has_resume:
            auto_session_id = resume.strip()
            try:
                state = self._store.load(auto_session_id)
            except ValueError as exc:
                return Result.err(MCPToolError(str(exc), tool_name="ouroboros_start_auto"))
            runner_arguments["resume"] = auto_session_id
        else:
            try:
                state = self._preallocate_state(
                    arguments,
                    goal.strip(),  # type: ignore[union-attr]
                    pipeline_timeout_seconds=requested_pipeline_timeout,
                )
            except ValueError as exc:
                return Result.err(MCPToolError(str(exc), tool_name="ouroboros_start_auto"))
            auto_session_id = state.auto_session_id
            runner_arguments["resume"] = auto_session_id
            runner_arguments.pop("pipeline_timeout_seconds", None)
            # The freshly preallocated state already contains the canonical
            # merged preference map derived from the structured goal plus any
            # explicit caller preferences.  Do not pass the original fresh-call
            # preference payload back through the resume runner: AutoHandler's
            # resume contract treats a supplied preference map as an override,
            # which would drop goal-derived sections and make start_auto
            # diverge from the synchronous auto path.
            runner_arguments.pop("user_preferences", None)

        already_running = await self._active_session_error(auto_session_id)
        if already_running is not None:
            return Result.err(already_running)

        plugin_dispatch = _state_dispatches_via_plugin(
            state,
            fallback_runtime_backend=self.agent_runtime_backend,
            fallback_opencode_mode=self.opencode_mode,
        )
        lease_token, lease_error = _reserve_start_lease(
            self._store,
            auto_session_id,
            mode="plugin_pending" if plugin_dispatch else "job_pending",
            ttl_seconds=_START_AUTO_PENDING_LEASE_SECONDS,
        )
        if lease_error is not None:
            return Result.err(lease_error)
        runner_arguments["_start_auto_lease_token"] = lease_token

        if plugin_dispatch:
            payload = _build_auto_subagent(runner_arguments, auto_session_id=auto_session_id)
            try:
                await self._event_store.initialize()
                await emit_subagent_dispatched_event(
                    self._event_store,
                    session_id=auto_session_id,
                    payload=payload,
                )
                _update_start_lease(
                    self._store,
                    auto_session_id,
                    token=lease_token,
                    mode="plugin_dispatched",
                    ttl_seconds=max(1.0, state.pipeline_timeout_seconds),
                )
            except Exception as exc:
                _release_start_lease(self._store, auto_session_id, token=lease_token)
                return Result.err(
                    MCPToolError(
                        "Failed to dispatch plugin auto session "
                        f"{auto_session_id}: {exc}. The auto session was persisted; "
                        f"resume with ouroboros_start_auto resume={auto_session_id} "
                        f"or ouroboros_auto resume={auto_session_id}.",
                        tool_name="ouroboros_start_auto",
                        is_retriable=True,
                        details={"auto_session_id": auto_session_id, "session_id": auto_session_id},
                    )
                )
            return build_subagent_result(
                payload,
                response_shape={
                    "job_id": None,
                    "auto_session_id": auto_session_id,
                    "session_id": auto_session_id,
                    "status": "delegated_to_plugin",
                    "dispatch_mode": "plugin",
                },
            )

        async def _runner() -> MCPToolResult:
            result = await self._inner_auto.handle(runner_arguments)
            if result.is_err:
                raise RuntimeError(str(result.error))
            return result.value

        initial_label = auto_session_id if has_resume else goal.strip()[:80]  # type: ignore[union-attr]
        runner = run_with_agent_process(
            event_store=self._event_store,
            intent="auto",
            work_fn=lambda _handle: _runner(),
        )
        try:
            snapshot = await self._job_manager.start_job(
                job_type="auto",
                initial_message=f"Queued ooo auto for {initial_label}",
                runner=runner,
                links=JobLinks(session_id=auto_session_id),
            )
            _update_start_lease(
                self._store,
                auto_session_id,
                token=lease_token,
                mode="job",
                job_id=snapshot.job_id,
                ttl_seconds=None,
            )
        except Exception as exc:
            if inspect.iscoroutine(runner):
                runner.close()
            _release_start_lease(self._store, auto_session_id, token=lease_token)
            return Result.err(
                MCPToolError(
                    "Failed to enqueue background auto session "
                    f"{auto_session_id}: {exc}. The auto session was persisted; "
                    f"resume with ouroboros_start_auto resume={auto_session_id} "
                    f"or ouroboros_auto resume={auto_session_id}.",
                    tool_name="ouroboros_start_auto",
                    is_retriable=True,
                    details={"auto_session_id": auto_session_id, "session_id": auto_session_id},
                )
            )

        text = (
            f"Started background auto session. job_id={snapshot.job_id}\n\n"
            f"Auto session ID: {auto_session_id}\n\n"
            "Poll with ouroboros_job_status / ouroboros_job_wait."
        )
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "job_id": snapshot.job_id,
                    "auto_session_id": auto_session_id,
                    "session_id": auto_session_id,
                    "status": "queued",
                    "dispatch_mode": "job",
                },
            )
        )

    def _preallocate_state(
        self,
        arguments: dict[str, Any],
        goal: str,
        *,
        pipeline_timeout_seconds: float | None,
    ) -> AutoPipelineState:
        """Persist a new auto session before the background job starts."""
        cwd = str(_resolve_cwd(arguments.get("cwd")))
        state = AutoPipelineState(goal=goal, cwd=cwd)
        state.max_interview_rounds = _positive_int_arg(arguments, "max_interview_rounds", 12)
        state.max_repair_rounds = _positive_int_arg(arguments, "max_repair_rounds", 5)
        state.skip_run = bool(arguments.get("skip_run", False))
        state.complete_product = bool(arguments.get("complete_product", False))
        supplied_user_preferences: dict[str, str | None] = {}
        if "user_preferences" in arguments and arguments.get("user_preferences") is not None:
            supplied_user_preferences = _parse_user_preferences(arguments.get("user_preferences"))
        state.user_preferences = _merge_goal_user_preferences(goal, supplied_user_preferences)
        state.ledger = _seed_initial_ledger_from_user_preferences(
            goal, state.user_preferences
        ).to_dict()
        if pipeline_timeout_seconds is not None:
            state.pipeline_timeout_seconds = pipeline_timeout_seconds
        runtime_backend = resolve_agent_runtime_backend(self.agent_runtime_backend)
        opencode_mode = _resolved_opencode_mode(runtime_backend, self.opencode_mode)
        state.runtime_backend = runtime_backend
        state.opencode_mode = opencode_mode
        if opencode_mode is not None:
            state.ralph_opencode_mode = opencode_mode
        self._store.save(state)
        return state

    async def _active_session_error(self, auto_session_id: str) -> MCPToolError | None:
        """Return an error when another start_auto already owns this session."""
        lease_error, released_stale_lease = await self._active_start_lease_error(auto_session_id)
        if lease_error is not None:
            return lease_error
        if released_stale_lease:
            return None
        active_job = await self._find_active_job(auto_session_id)
        if active_job is not None:
            return MCPToolError(
                "Auto session already has an active background job: "
                f"auto_session_id={auto_session_id}, job_id={active_job.job_id}. "
                "Poll that job or cancel it before starting another resume.",
                tool_name="ouroboros_start_auto",
                is_retriable=True,
                details={
                    "auto_session_id": auto_session_id,
                    "session_id": auto_session_id,
                    "job_id": active_job.job_id,
                    "status": active_job.status.value,
                },
            )
        return None

    async def _find_active_job(self, auto_session_id: str):
        finder = getattr(self._job_manager, "find_active_job_by_session", None)
        if finder is None or not inspect.iscoroutinefunction(finder):
            return None
        return await finder(auto_session_id, job_type="auto")

    async def _active_start_lease_error(
        self, auto_session_id: str
    ) -> tuple[MCPToolError | None, bool]:
        lease = _read_start_lease(self._store, auto_session_id)
        if lease is None:
            return None, False
        if _lease_is_expired(lease):
            _release_start_lease(self._store, auto_session_id, token=lease.get("token"))
            return None, True
        mode = str(lease.get("mode") or "unknown")
        job_id = lease.get("job_id")
        if isinstance(job_id, str) and job_id:
            try:
                snapshot = await self._job_manager.get_snapshot(job_id)
            except ValueError:
                _release_start_lease(self._store, auto_session_id, token=lease.get("token"))
                return None, True
            if snapshot.is_terminal:
                _release_start_lease(self._store, auto_session_id, token=lease.get("token"))
                return None, True
            if not _lease_owner_is_alive(lease):
                _release_start_lease(self._store, auto_session_id, token=lease.get("token"))
                return None, True
            return (
                MCPToolError(
                    "Auto session already has an active background job: "
                    f"auto_session_id={auto_session_id}, job_id={job_id}. "
                    "Poll that job or cancel it before starting another resume.",
                    tool_name="ouroboros_start_auto",
                    is_retriable=True,
                    details={
                        "auto_session_id": auto_session_id,
                        "session_id": auto_session_id,
                        "job_id": job_id,
                        "status": snapshot.status.value,
                        "lease_mode": mode,
                        "lease_owner_pid": lease.get("owner_pid"),
                    },
                ),
                False,
            )
        if mode.startswith("plugin"):
            if not _lease_owner_is_alive(lease):
                _release_start_lease(self._store, auto_session_id, token=lease.get("token"))
                return None, True
            try:
                state = self._store.load(auto_session_id)
            except ValueError:
                _release_start_lease(self._store, auto_session_id, token=lease.get("token"))
                return None, True
            if state.phase in TERMINAL_PHASES:
                _release_start_lease(self._store, auto_session_id, token=lease.get("token"))
                return None, True
            return (
                MCPToolError(
                    "Auto session already has an active plugin dispatch: "
                    f"auto_session_id={auto_session_id}. Wait for the OpenCode task "
                    "to finish or retry after the dispatch lease expires.",
                    tool_name="ouroboros_start_auto",
                    is_retriable=True,
                    details={
                        "auto_session_id": auto_session_id,
                        "session_id": auto_session_id,
                        "dispatch_mode": "plugin",
                        "lease_expires_at": lease.get("expires_at"),
                        "lease_mode": mode,
                    },
                ),
                False,
            )
        return (
            MCPToolError(
                "Auto session already has a pending start lease: "
                f"auto_session_id={auto_session_id}. Retry after the lease expires.",
                tool_name="ouroboros_start_auto",
                is_retriable=True,
                details={
                    "auto_session_id": auto_session_id,
                    "session_id": auto_session_id,
                    "lease_expires_at": lease.get("expires_at"),
                    "lease_mode": mode,
                },
            ),
            False,
        )


def _build_auto_subagent(
    arguments: dict[str, Any],
    *,
    auto_session_id: str,
):
    """Build the immediate OpenCode dispatch envelope for ``start_auto``."""
    prompt = (
        "Run the Ouroboros auto pipeline for the preallocated session below.\n\n"
        "Use the MCP tool `ouroboros_auto` directly with the provided arguments. "
        "Do not call `ouroboros_start_auto` from this child session; this dispatch "
        "already owns the async handoff.\n\n"
        f"Auto session ID: {auto_session_id}\n\n"
        "Arguments:\n"
        f"```json\n{json.dumps(arguments, ensure_ascii=False, indent=2)}\n```\n\n"
        "Return the final auto result, including any resume guidance or downstream "
        "run/Ralph delegation receipt surfaced by `ouroboros_auto`."
    )
    return build_subagent_payload(
        tool_name="ouroboros_start_auto",
        title=f"Auto: {auto_session_id}",
        prompt=prompt,
        context={
            "auto_session_id": auto_session_id,
            "arguments": arguments,
        },
    )


def _auto_session_id_from_arguments(arguments: dict[str, Any]) -> str | None:
    resume = arguments.get("resume")
    if isinstance(resume, str) and resume.strip():
        return resume.strip()
    return None


def _state_dispatches_via_plugin(
    state: AutoPipelineState,
    *,
    fallback_runtime_backend: str | None,
    fallback_opencode_mode: str | None,
) -> bool:
    runtime_backend = state.runtime_backend or fallback_runtime_backend
    if runtime_backend is None and state.opencode_mode is not None:
        runtime_backend = "opencode"
    runtime_backend = resolve_agent_runtime_backend(runtime_backend)
    opencode_mode = _resolved_opencode_mode(
        runtime_backend,
        state.opencode_mode or fallback_opencode_mode,
    )
    return should_dispatch_via_plugin(runtime_backend, opencode_mode)


def _start_auto_lease_token_from_arguments(arguments: dict[str, Any]) -> str | None:
    token = arguments.get("_start_auto_lease_token")
    if isinstance(token, str) and token:
        return token
    return None


def _start_lease_path(store: AutoStore, auto_session_id: str) -> Path:
    return store.path_for(auto_session_id).with_suffix(".start_auto_lease.json")


def _read_start_lease(store: AutoStore, auto_session_id: str) -> dict[str, Any] | None:
    path = _start_lease_path(store, auto_session_id)
    with file_lock(path):
        return _read_start_lease_locked(path)


def _read_start_lease_locked(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _reserve_start_lease(
    store: AutoStore,
    auto_session_id: str,
    *,
    mode: str,
    ttl_seconds: float,
) -> tuple[str, MCPToolError | None]:
    path = _start_lease_path(store, auto_session_id)
    token = uuid4().hex
    with file_lock(path):
        existing = _read_start_lease_locked(path)
        if existing is not None and not _lease_is_expired(existing):
            if _lease_owner_is_alive(existing):
                return "", _lease_conflict_error(auto_session_id, existing)
        _write_start_lease_locked(
            path,
            {
                "token": token,
                "mode": mode,
                "created_at": _utc_iso(),
                "expires_at": _utc_iso(ttl_seconds=ttl_seconds),
                **_current_lease_owner(),
            },
        )
    return token, None


def _update_start_lease(
    store: AutoStore,
    auto_session_id: str,
    *,
    token: str,
    mode: str,
    ttl_seconds: float | None,
    job_id: str | None = None,
) -> None:
    path = _start_lease_path(store, auto_session_id)
    with file_lock(path):
        lease = _read_start_lease_locked(path)
        if lease is None or lease.get("token") != token:
            return
        lease["mode"] = mode
        lease["updated_at"] = _utc_iso()
        if job_id is not None:
            lease["job_id"] = job_id
        if ttl_seconds is None:
            lease.pop("expires_at", None)
        else:
            lease["expires_at"] = _utc_iso(ttl_seconds=ttl_seconds)
        _write_start_lease_locked(path, lease)


def _release_start_lease(
    store: AutoStore,
    auto_session_id: str,
    *,
    token: object | None = None,
) -> None:
    path = _start_lease_path(store, auto_session_id)
    with file_lock(path):
        if token is not None:
            lease = _read_start_lease_locked(path)
            if lease is not None and lease.get("token") != token:
                return
        path.unlink(missing_ok=True)


def _validate_start_lease_token(
    store: AutoStore, auto_session_id: str, token: str
) -> MCPToolError | None:
    lease = _read_start_lease(store, auto_session_id)
    if lease is None:
        return MCPToolError(
            "Invalid start_auto lease token: no active lease exists for "
            f"auto_session_id={auto_session_id}.",
            tool_name="ouroboros_auto",
            is_retriable=True,
            details={"auto_session_id": auto_session_id, "session_id": auto_session_id},
        )
    if _lease_is_expired(lease):
        return MCPToolError(
            "Invalid start_auto lease token: lease has expired for "
            f"auto_session_id={auto_session_id}.",
            tool_name="ouroboros_auto",
            is_retriable=True,
            details={
                "auto_session_id": auto_session_id,
                "session_id": auto_session_id,
                "lease_mode": lease.get("mode"),
                "lease_expires_at": lease.get("expires_at"),
            },
        )
    if lease.get("token") != token:
        return MCPToolError(
            f"Invalid start_auto lease token for auto_session_id={auto_session_id}.",
            tool_name="ouroboros_auto",
            is_retriable=True,
            details={
                "auto_session_id": auto_session_id,
                "session_id": auto_session_id,
                "lease_mode": lease.get("mode"),
            },
        )
    return None


def _write_start_lease_locked(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _lease_is_expired(lease: dict[str, Any]) -> bool:
    expires_at = lease.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= datetime.now(UTC)


def _current_lease_owner() -> dict[str, Any]:
    pid, start_time = current_process_identity()
    return {
        "owner_pid": pid,
        "owner_start_time": start_time,
    }


def _lease_owner_is_alive(lease: dict[str, Any]) -> bool:
    pid = lease.get("owner_pid")
    if not isinstance(pid, int):
        # Legacy non-expiring job leases did not record an owner. Treat them as
        # active because process-local task state cannot prove cross-process
        # staleness.
        return True
    start_time = lease.get("owner_start_time")
    if start_time is not None and not isinstance(start_time, int | float):
        start_time = None
    return is_process_identity_alive(pid, float(start_time) if start_time is not None else None)


def _lease_conflict_error(auto_session_id: str, lease: dict[str, Any]) -> MCPToolError:
    mode = str(lease.get("mode") or "unknown")
    return MCPToolError(
        "Auto session already has a pending start lease: "
        f"auto_session_id={auto_session_id}. Retry after the lease expires.",
        tool_name="ouroboros_start_auto",
        is_retriable=True,
        details={
            "auto_session_id": auto_session_id,
            "session_id": auto_session_id,
            "lease_mode": mode,
            "lease_expires_at": lease.get("expires_at"),
        },
    )


def _utc_iso(*, ttl_seconds: float | None = None) -> str:
    value = datetime.now(UTC)
    if ttl_seconds is not None:
        value += timedelta(seconds=ttl_seconds)
    return value.isoformat()


def _result_meta(result: AutoPipelineResult) -> dict[str, Any]:
    """Build MCP metadata for clients that render auto progress outside CLI text."""
    meta: dict[str, Any] = {
        "status": result.status,
        "auto_session_id": result.auto_session_id,
        "phase": result.phase,
        "current_round": result.current_round,
        "last_progress_message": result.last_progress_message,
        "last_progress_at": result.last_progress_at,
        "resume_capability": result.resume_capability.value,
        "blocker": result.blocker,
        "seed_path": result.seed_path,
        "seed_origin": result.seed_origin,
        "grade": result.grade,
        "last_grade": result.last_grade,
        "interview_session_id": result.interview_session_id,
        "execution_id": result.execution_id,
        "job_id": result.job_id,
        "run_session_id": result.run_session_id,
    }
    if result.execution_job_status:
        meta["execution_job_status"] = result.execution_job_status
    if result.execution_job_error:
        meta["execution_job_error"] = result.execution_job_error
    if result.execution_job_message:
        meta["execution_job_message"] = result.execution_job_message
    # Only advertise a runnable resume_command when --resume actually has
    # something to do. NONE-capability sessions (COMPLETE, or unrecoverable
    # BLOCKED/FAILED) must not surface a resume action via metadata —
    # otherwise clients keying off ``meta.resume_command`` would push users
    # into a guaranteed-failing ``--resume`` path even though the
    # human-readable text intentionally omits the hint.
    if result.resume_capability is not AutoResumeCapability.NONE:
        meta["resume_command"] = f"ooo auto --resume {result.auto_session_id}"
    if result.pending_question:
        meta["pending_question"] = result.pending_question
    if result.run_handoff_status:
        meta["run_handoff_status"] = result.run_handoff_status
    if result.run_handoff_guidance:
        meta["run_handoff_guidance"] = result.run_handoff_guidance
    if result.attached_run_handle:
        meta["attached_run_handle"] = result.attached_run_handle
        meta["attached_run_source"] = result.attached_run_source
        meta["attached_at"] = result.attached_at
    if result.run_reconciliation_status:
        meta["run_reconciliation_status"] = result.run_reconciliation_status
        meta["run_reconciliation_source"] = result.run_reconciliation_source
        meta["run_reconciled_at"] = result.run_reconciled_at
    # Q00/ouroboros#773 (review-4): surface Ralph handoff tracking handles on
    # the MCP result contract. Without these, plugin-mode dispatches and
    # mid-loop checkpoints expose no structured handle for clients to monitor
    # or correlate the Ralph work, forcing them to read local state files
    # out-of-band. Each field is emitted only when populated so default-off
    # ``complete_product=False`` runs keep the legacy meta shape byte-identical.
    if result.ralph_job_id:
        meta["ralph_job_id"] = result.ralph_job_id
    if result.ralph_lineage_id:
        meta["ralph_lineage_id"] = result.ralph_lineage_id
    if result.ralph_dispatch_mode:
        meta["ralph_dispatch_mode"] = result.ralph_dispatch_mode
    # RFC #809 Phase 2.1 — surface the EVALUATE verdict when present. None
    # signals "EVALUATE did not run" so clients can distinguish "not graded"
    # from "graded and failed".
    if result.last_qa_score is not None:
        meta["last_qa_score"] = result.last_qa_score
    if result.last_qa_verdict is not None:
        meta["last_qa_verdict"] = result.last_qa_verdict
    if result.last_qa_differences:
        meta["last_qa_differences"] = list(result.last_qa_differences)
    if result.last_qa_suggestions:
        meta["last_qa_suggestions"] = list(result.last_qa_suggestions)
    # RFC #809 Phase 2.2 — surface the UNSTUCK_LATERAL persona advisory when
    # present so clients can distinguish "QA failed and lateral surfaced a
    # reframing" from "QA failed without lateral context".
    if result.last_lateral_persona is not None:
        meta["last_lateral_persona"] = result.last_lateral_persona
    if result.last_lateral_approach_summary is not None:
        meta["last_lateral_approach_summary"] = result.last_lateral_approach_summary
    if result.last_lateral_text is not None:
        meta["last_lateral_text"] = result.last_lateral_text
    # Always emit the ledger-provenance surface so MCP clients can distinguish
    # "computed and empty" (no resolved sections yet, or no per-source split
    # available) from "field not provided at all".  Empty containers are part
    # of the contract — consumers should treat absence as a protocol error.
    meta["ledger_provenance"] = {
        source: list(sections) for source, sections in result.ledger_provenance.items()
    }
    meta["evidence_backed_sections"] = list(result.evidence_backed_sections)
    meta["assumption_only_sections"] = list(result.assumption_only_sections)
    return meta


async def _reconcile_execution_job_snapshot(result: AutoPipelineResult) -> AutoPipelineResult:
    """Surface terminal execution job failures/cancellations on auto resume."""
    if not result.job_id:
        return result
    try:
        snapshot = await JobManager().get_snapshot(result.job_id)
    except Exception:
        return result
    blocker = result.blocker
    status = result.status
    resume_capability = result.resume_capability
    if snapshot.status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}:
        # A started execution handoff is not the same as product completion.
        # AutoPipeline persists COMPLETE after returning a durable run handle so
        # it does not start duplicate work on resume; the MCP surface should
        # still report the linked job as non-terminal and keep resume polling
        # discoverable until the background job reaches a terminal state.
        status = snapshot.status.value
        resume_capability = AutoResumeCapability.RESUME
    elif snapshot.status in {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}:
        detail = snapshot.error or snapshot.result_text or snapshot.message
        blocker = (
            f"execution job {snapshot.status.value}: {detail}"
            if detail
            else f"execution job {snapshot.status.value}"
        )
        status = "failed" if snapshot.status is JobStatus.FAILED else "blocked"
        resume_capability = AutoResumeCapability.NONE
    elif snapshot.status is JobStatus.COMPLETED:
        status = "complete"
        resume_capability = AutoResumeCapability.NONE
    return replace(
        result,
        status=status,
        blocker=blocker,
        resume_capability=resume_capability,
        execution_job_status=snapshot.status.value,
        execution_job_error=snapshot.error,
        execution_job_message=snapshot.message,
    )


def _resolved_opencode_mode(runtime_backend: str | None, opencode_mode: str | None) -> str | None:
    if runtime_backend != "opencode":
        return None
    return opencode_mode or get_opencode_mode()


def _optional_text_arg(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value in {None, ""}:
        return None
    if not isinstance(value, str) or not value.strip():
        msg = f"{name} must be a non-empty string"
        raise ValueError(msg)
    return value.strip()


def _optional_pipeline_timeout(arguments: dict[str, Any]) -> float | None:
    """Validate the optional ``pipeline_timeout_seconds`` MCP argument.

    Returns ``None`` when omitted, otherwise a float in the inclusive
    ``[MIN_PIPELINE_TIMEOUT_SECONDS, MAX_PIPELINE_TIMEOUT_SECONDS]`` window.
    """
    value = arguments.get("pipeline_timeout_seconds")
    if value in {None, ""}:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = "pipeline_timeout_seconds must be a number"
        raise ValueError(msg)
    timeout = float(value)
    if not (MIN_PIPELINE_TIMEOUT_SECONDS <= timeout <= MAX_PIPELINE_TIMEOUT_SECONDS):
        msg = (
            "pipeline_timeout_seconds must be between "
            f"{MIN_PIPELINE_TIMEOUT_SECONDS:g} and {MAX_PIPELINE_TIMEOUT_SECONDS:g}"
        )
        raise ValueError(msg)
    return timeout


def _parse_user_preferences(
    value: object,
    *,
    allow_deletions: bool = False,
) -> dict[str, str | None]:
    """Validate and normalise the optional ``user_preferences`` MCP arg.

    Returns a dict keyed by ledger section names. Empty input yields an empty
    dict. Any unknown section name or empty/non-stringifiable value is
    rejected with ``ValueError`` so callers see a clear contract failure
    rather than a silently-ignored preference.

    Accepted value shapes per key:

    * ``str``  — stripped; must be non-empty unless resume deletion is allowed.
    * ``list[str | int | float]`` — each item stringified+stripped, empties
      dropped, joined with ``"\\n"``; final result must be non-empty.
    * ``None`` / empty string / empty list — only when ``allow_deletions`` is
      true, clears a persisted preference for that key.

    The downstream ``answerer.py`` still consumes the value as a single
    string (via ``raw_value.strip()``); only the input contract widens.
    """
    if value is None or value == "":
        return {}
    if not isinstance(value, dict):
        raise ValueError("user_preferences must be an object keyed by ledger section names")
    if not value:
        return {}
    valid_sections = frozenset(REQUIRED_SECTIONS)
    cleaned: dict[str, str | None] = {}
    for raw_key, raw_val in value.items():
        if not isinstance(raw_key, str):
            raise ValueError("user_preferences keys must be strings")
        if raw_key not in valid_sections:
            suggestion = difflib.get_close_matches(raw_key, sorted(valid_sections), n=1, cutoff=0.6)
            hint = f" (did you mean: '{suggestion[0]}'?)" if suggestion else ""
            raise ValueError(
                f"user_preferences key '{raw_key}' is not a valid ledger section "
                f"(allowed: {', '.join(sorted(valid_sections))}){hint}"
            )
        if raw_val is None and allow_deletions:
            cleaned[raw_key] = None
            continue
        if isinstance(raw_val, str):
            normalised = raw_val.strip()
            if not normalised:
                if allow_deletions:
                    cleaned[raw_key] = None
                    continue
                raise ValueError(
                    f"user_preferences['{raw_key}'] must be a non-empty string or "
                    "list of strings/numbers"
                )
            cleaned[raw_key] = normalised
            continue
        if isinstance(raw_val, list):
            parts: list[str] = []
            for item in raw_val:
                if isinstance(item, bool) or not isinstance(item, str | int | float):
                    raise ValueError(
                        f"user_preferences['{raw_key}'] must be a non-empty string or "
                        "list of strings/numbers"
                    )
                text = str(item).strip()
                if text:
                    parts.append(text)
            if not parts:
                if allow_deletions:
                    cleaned[raw_key] = None
                    continue
                raise ValueError(
                    f"user_preferences['{raw_key}'] must be a non-empty string or "
                    "list of strings/numbers"
                )
            cleaned[raw_key] = "\n".join(parts)
            continue
        raise ValueError(
            f"user_preferences['{raw_key}'] must be a non-empty string or list of strings/numbers"
        )
    return cleaned


def _merge_goal_user_preferences(goal: str, supplied: dict[str, str | None]) -> dict[str, str]:
    """Merge explicit MCP preferences over preferences derived from the goal text."""
    merged = _derive_goal_user_preferences(goal)
    for key, value in supplied.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _merge_resume_user_preferences(
    _goal: str,
    persisted: dict[str, str],
    supplied: dict[str, str | None],
) -> dict[str, str]:
    """Merge resume preferences without dropping previously persisted facts."""
    # Fresh sessions already persist goal-derived preferences. On resume the
    # persisted map is the durable source of truth; re-deriving from the goal
    # would resurrect sections the operator explicitly cleared earlier.
    merged = dict(persisted)
    for key, value in supplied.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _seed_initial_ledger_from_user_preferences(
    goal: str, user_preferences: dict[str, str]
) -> SeedDraftLedger:
    """Create an initial ledger seeded with explicit goal-prompt preferences."""
    ledger = SeedDraftLedger.from_goal(goal)
    for section in REQUIRED_SECTIONS:
        if section == "goal":
            continue
        value = user_preferences.get(section)
        if not value:
            continue
        if _user_preference_would_bypass_risky_gate(goal, value):
            continue
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.goal_prompt",
                value=value,
                source=LedgerSource.USER_PREFERENCE,
                confidence=0.86,
                status=LedgerStatus.CONFIRMED,
                rationale=(
                    "Derived from explicit structured text in the initial ooo auto goal prompt."
                ),
            ),
        )
    return ledger


def _reseed_preference_ledger(
    goal: str,
    existing_ledger: dict[str, Any] | None,
    user_preferences: dict[str, str],
) -> SeedDraftLedger:
    """Refresh preference-derived ledger entries after a resume override.

    The auto pipeline treats a persisted ledger as the Seed source of truth.
    Therefore a resume call that overrides ``state.user_preferences`` must also
    refresh the preconfirmed preference entries; otherwise stale values from a
    preallocated start_auto request can survive even though the preference map
    was corrected. Non-preference interview facts are preserved.
    """
    refreshed = _seed_initial_ledger_from_user_preferences(goal, user_preferences)
    if not existing_ledger:
        return refreshed
    existing = SeedDraftLedger.from_dict(existing_ledger)
    refreshed.question_history = list(existing.question_history)
    for section_name, section in existing.sections.items():
        for entry in section.entries:
            if (
                entry.source == LedgerSource.USER_PREFERENCE
                or entry.key.endswith(".goal_prompt")
                or entry.key.endswith(".user_preference")
            ):
                continue
            if section_name == "goal" and entry.key == "goal.primary":
                continue
            refreshed.add_entry(section_name, entry)
    return refreshed


def _user_preference_would_bypass_risky_gate(goal: str, value: str) -> bool:
    """Return True when pre-confirming a preference would skip answerer safety.

    Normal user_preferences only become confirmed ledger entries through
    ``AutoAnswerer._maybe_apply_user_preference()``, which runs the risky
    fallback gate against both the current question and the converged goal.
    Preallocating a ledger for structured MCP prompts must honor that same
    policy: unsafe preference text may remain in ``state.user_preferences`` so
    the interview answerer can surface the blocker at the right question, but
    it must not be persisted as already-confirmed ledger evidence.
    """
    return risky_user_preference_blocker_for(question=value, goal_text=goal) is not None


def _derive_goal_user_preferences(goal: str) -> dict[str, str]:
    """Extract explicit ledger preferences from a structured multiline auto goal.

    This is intentionally conservative: it recognizes operator-authored section
    labels and concrete command/file constraints, then lets the normal
    interview/repair pipeline validate and refine the resulting ledger. It does
    not invent product behavior beyond common local-repository observation
    framing that is explicitly present in the prompt.
    """
    sections = _extract_goal_sections(goal)
    preferences: dict[str, str] = {}

    runtime = _section_text(sections, "runtime context", "runtime_context")
    if runtime:
        preferences["runtime_context"] = runtime

    non_goals = _section_text(sections, "non-goals", "non goals", "non_goals")
    if non_goals:
        preferences["non_goals"] = non_goals

    actors = _section_text(sections, "actors", "actor")
    if actors:
        preferences["actors"] = actors

    inputs = _section_text(sections, "inputs", "input")
    if inputs:
        preferences["inputs"] = inputs

    success = _section_text(sections, "success criteria", "acceptance criteria")
    if success:
        execution_success = _execution_success_criteria(success, context_text=goal)
        if execution_success:
            preferences["acceptance_criteria"] = execution_success

    outputs = _section_text(sections, "outputs", "deliverables")
    if outputs:
        preferences["outputs"] = outputs

    constraint_lines = _matching_lines(
        goal,
        (
            "do not",
            "keep ",
            "local file edits are allowed",
            "running targeted tests is allowed",
            "network access is not required",
            "no credentials are required",
        ),
    )
    if constraint_lines:
        preferences["constraints"] = "\n".join(constraint_lines)

    verification_lines = _matching_lines(
        goal,
        (
            "uv run pytest",
            "targeted test",
            "test command",
            "test result",
            "pytest test",
        ),
    )
    if verification_lines:
        preferences["verification_plan"] = "\n".join(verification_lines)

    failure_lines = _matching_lines(
        goal,
        (
            "if ",
            "blocked",
            "unavailable",
            "interpreted as normal text",
            "previous ",
            "manual fallback",
        ),
    )
    if failure_lines:
        preferences["failure_modes"] = "\n".join(failure_lines)

    return preferences


def _extract_goal_sections(goal: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in goal.splitlines():
        line = raw_line.rstrip()
        header = _section_header(line)
        if header is not None:
            current = header
            sections.setdefault(current, [])
            continue
        if current is not None and line.strip():
            sections[current].append(_clean_prompt_line(line))
    return sections


def _section_header(line: str) -> str | None:
    match = re.match(r"^\s*([A-Za-z][A-Za-z0-9 _/-]{1,60}):\s*$", line)
    if match is None:
        return None
    return " ".join(match.group(1).replace("_", " ").casefold().split())


def _section_text(sections: dict[str, list[str]], *names: str) -> str:
    values: list[str] = []
    for name in names:
        values.extend(sections.get(" ".join(name.replace("_", " ").casefold().split()), ()))
    return "\n".join(dict.fromkeys(value for value in values if value.strip()))


def _execution_success_criteria(text: str, *, context_text: str = "") -> str:
    strip_auto_wrapper = has_auto_wrapper_context("\n".join((context_text, text)))
    lines = [
        line
        for line in (_clean_prompt_line(raw) for raw in text.splitlines())
        if line and not (strip_auto_wrapper and is_auto_reporting_acceptance_criterion(line))
    ]
    return "\n".join(dict.fromkeys(lines))


def _matching_lines(text: str, needles: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for line in text.splitlines():
        cleaned = _clean_prompt_line(line)
        lowered = cleaned.casefold()
        if cleaned and any(needle in lowered for needle in needles):
            matches.append(cleaned)
    return list(dict.fromkeys(matches))


def _clean_prompt_line(line: str) -> str:
    return re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()


def _build_context_provider(user_preferences: dict[str, str]):
    """Return a context_provider that augments repo context with user preferences."""

    def provider(cwd: str) -> AutoAnswerContext:
        base = repo_auto_answer_context(cwd)
        return AutoAnswerContext(
            repo_facts=base.repo_facts,
            evidence=base.evidence,
            user_preferences=user_preferences,
        )

    return provider


def _positive_int_arg(arguments: dict[str, Any], name: str, default: int) -> int:
    value = arguments.get(name, default)
    if value in {None, ""}:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)
    if value <= 0:
        msg = f"{name} must be >= 1"
        raise ValueError(msg)
    return value


def _safe_default_cwd() -> Path:
    cwd = Path.cwd()
    if cwd == Path("/"):
        return Path.home()
    return _require_writable_cwd(cwd)


def _resolve_cwd(value: object) -> Path:
    if value is None or value == "":
        return _safe_default_cwd()
    return _require_writable_cwd(Path(str(value)).expanduser())


def _require_writable_cwd(cwd: Path) -> Path:
    resolved = cwd.resolve()
    if not resolved.is_dir():
        msg = f"working directory is not a directory: {resolved}"
        raise ValueError(msg)
    if not os.access(resolved, os.W_OK | os.X_OK):
        msg = f"working directory is not writable/searchable: {resolved}"
        raise ValueError(msg)
    return resolved


def _authoring_interview_handler(
    handler: InterviewHandler | None,
    *,
    llm_backend: str | None,
    agent_runtime_backend: str | None,
    opencode_mode: str | None,
) -> InterviewHandler:
    if handler is None:
        return InterviewHandler(
            llm_backend=llm_backend,
            agent_runtime_backend=agent_runtime_backend,
            opencode_mode=opencode_mode,
            suppress_tool_use_prompt_cues=True,
        )
    if _handler_matches_runtime(handler, agent_runtime_backend, opencode_mode) and getattr(
        handler, "suppress_tool_use_prompt_cues", False
    ):
        # Only short-circuit when llm_backend is also already aligned. Otherwise
        # a reused handler would silently keep its previous backend/model
        # provider on later sessions when the caller explicitly overrides it.
        backend_matches = llm_backend is None or handler.llm_backend == llm_backend
        if handler.llm_adapter is None and backend_matches:
            return handler
        return InterviewHandler(
            interview_engine=handler.interview_engine,
            event_store=handler.event_store,
            # Preserve an explicit caller-supplied llm_adapter. When the auto
            # path inherits a handler that has llm_adapter=None (production
            # default), handle() will build the isolated default adapter
            # itself; we only need to ensure we don't silently *replace* an
            # explicitly injected non-default adapter here.
            llm_adapter=handler.llm_adapter,
            llm_backend=llm_backend if llm_backend is not None else handler.llm_backend,
            agent_runtime_backend=agent_runtime_backend,
            opencode_mode=opencode_mode,
            data_dir=handler.data_dir,
            suppress_tool_use_prompt_cues=True,
        )
    return InterviewHandler(
        interview_engine=handler.interview_engine,
        event_store=handler.event_store,
        llm_adapter=None,
        llm_backend=llm_backend if llm_backend is not None else handler.llm_backend,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
        data_dir=handler.data_dir,
        suppress_tool_use_prompt_cues=True,
    )


def _authoring_seed_handler(
    handler: GenerateSeedHandler | None,
    *,
    llm_backend: str | None,
    agent_runtime_backend: str | None,
    opencode_mode: str | None,
) -> GenerateSeedHandler:
    if handler is None:
        return GenerateSeedHandler(
            llm_backend=llm_backend,
            agent_runtime_backend=agent_runtime_backend,
            opencode_mode=opencode_mode,
        )
    if _handler_matches_runtime(handler, agent_runtime_backend, opencode_mode):
        return handler
    return GenerateSeedHandler(
        interview_engine=handler.interview_engine,
        seed_generator=handler.seed_generator,
        llm_adapter=handler.llm_adapter,
        llm_backend=llm_backend if llm_backend is not None else handler.llm_backend,
        event_store=handler.event_store,
        data_dir=handler.data_dir,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
    )


def _handler_matches_runtime(
    handler: object, agent_runtime_backend: str | None, opencode_mode: str | None
) -> bool:
    return (
        getattr(handler, "agent_runtime_backend", None) == agent_runtime_backend
        and getattr(handler, "opencode_mode", None) == opencode_mode
    )


def _execution_start_handler(
    handler: StartExecuteSeedHandler | None,
    *,
    llm_backend: str | None,
    agent_runtime_backend: str | None,
    opencode_mode: str | None,
    mcp_manager: object | None,
    mcp_tool_prefix: str,
) -> StartExecuteSeedHandler:
    event_store = getattr(handler, "event_store", None) or getattr(handler, "_event_store", None)
    job_manager = getattr(handler, "job_manager", None) or getattr(handler, "_job_manager", None)
    original_execute = getattr(handler, "execute_handler", None) or getattr(
        handler, "_execute_handler", None
    )
    if (
        handler is not None
        and _handler_matches_runtime(handler, agent_runtime_backend, opencode_mode)
        and getattr(original_execute, "mcp_manager", None) is mcp_manager
        and getattr(original_execute, "mcp_tool_prefix", "") == mcp_tool_prefix
    ):
        return handler
    llm_adapter = getattr(original_execute, "llm_adapter", None)
    resolved_llm_backend = (
        llm_backend if llm_backend is not None else getattr(original_execute, "llm_backend", None)
    )
    execute_seed = ExecuteSeedHandler(
        event_store=event_store,
        llm_adapter=llm_adapter,
        llm_backend=resolved_llm_backend,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
        mcp_manager=mcp_manager,
        mcp_tool_prefix=mcp_tool_prefix,
    )
    return StartExecuteSeedHandler(
        execute_handler=execute_seed,
        event_store=event_store,
        job_manager=job_manager,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
    )


def _format_result(result: AutoPipelineResult) -> str:
    lines = [
        f"Auto session: {result.auto_session_id}",
        f"Status: {result.status}",
        f"Phase: {result.phase}",
    ]
    if result.grade:
        lines.append(f"Seed grade: {result.grade}")
    if result.interview_session_id:
        lines.append(f"Interview session: {result.interview_session_id}")
    if result.seed_path:
        lines.append(f"Seed: {result.seed_path}")
    lines.append(f"Seed origin: {result.seed_origin}")
    if result.job_id or result.execution_id or result.run_session_id:
        lines.extend(
            [
                "Execution started:",
                f"  job_id: {result.job_id}",
                f"  execution_id: {result.execution_id}",
                f"  session_id: {result.run_session_id}",
            ]
        )
    if result.run_handoff_status:
        lines.append(f"Run handoff status: {result.run_handoff_status}")
    if result.execution_job_status:
        lines.append(f"Execution job status: {result.execution_job_status}")
    if result.execution_job_error:
        lines.append(f"Execution job error: {result.execution_job_error}")
    elif result.execution_job_message and result.execution_job_status in {
        "failed",
        "cancelled",
        "interrupted",
    }:
        lines.append(f"Execution job message: {result.execution_job_message}")
    if result.run_handoff_guidance:
        lines.append(f"Run handoff guidance: {result.run_handoff_guidance}")
    if result.attached_run_handle:
        lines.append(f"Attached run handle: {result.attached_run_handle}")
        lines.append(f"Attached run source: {result.attached_run_source}")
        lines.append(f"Attached at: {result.attached_at}")
    if result.run_reconciliation_status:
        lines.append(f"Run reconciliation status: {result.run_reconciliation_status}")
        lines.append(f"Run reconciliation source: {result.run_reconciliation_source}")
        lines.append(f"Run reconciled at: {result.run_reconciled_at}")
    if result.ralph_dispatch_mode or result.ralph_job_id or result.ralph_lineage_id:
        lines.append("Ralph handoff:")
        if result.ralph_dispatch_mode:
            lines.append(f"  dispatch_mode: {result.ralph_dispatch_mode}")
        if result.ralph_job_id:
            lines.append(f"  job_id: {result.ralph_job_id}")
        if result.ralph_lineage_id:
            lines.append(f"  lineage_id: {result.ralph_lineage_id}")
    # RFC #809 Phase 2.1 — render the EVALUATE verdict when present so resume
    # surfaces tell the user whether the session converged on AC verification
    # or stalled with QA findings the operator must act on.
    if result.last_qa_verdict is not None:
        score = f"{result.last_qa_score:.2f}" if result.last_qa_score is not None else "n/a"
        lines.append(f"QA verdict: {result.last_qa_verdict} (score {score})")
        if result.last_qa_differences:
            lines.append("  differences:")
            lines.extend(f"  - {item}" for item in result.last_qa_differences[:3])
        if result.last_qa_suggestions:
            lines.append("  suggestions:")
            lines.extend(f"  - {item}" for item in result.last_qa_suggestions[:3])
    # RFC #809 Phase 2.2 — render the lateral persona advisory when present.
    if result.last_lateral_persona is not None:
        lines.append(f"Lateral persona: {result.last_lateral_persona}")
        if result.last_lateral_approach_summary:
            lines.append(f"  approach: {result.last_lateral_approach_summary}")
    if result.assumptions:
        lines.append("Assumptions:")
        lines.extend(f"- {item}" for item in result.assumptions)
    if result.non_goals:
        lines.append("Non-goals:")
        lines.extend(f"- {item}" for item in result.non_goals)
    if result.blocker:
        lines.append(f"Blocker: {result.blocker}")
    capability = result.resume_capability
    lines.extend(
        render_resume_lines(
            capability,
            result.auto_session_id,
            goal=None,
            use_markup=False,
        )
    )
    return "\n".join(lines)
