"""MCP handler for full-quality ``ooo auto`` sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from ouroboros.auto.adapters import (
    HandlerInterviewBackend,
    HandlerRalphStarter,
    HandlerRunStarter,
    HandlerSeedGenerator,
    load_seed,
    save_seed,
)
from ouroboros.auto.answerer import AutoAnswerContext
from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.ledger import REQUIRED_SECTIONS
from ouroboros.auto.pipeline import AutoPipeline, AutoPipelineResult
from ouroboros.auto.repo_context import repo_auto_answer_context
from ouroboros.auto.resume_render import render_resume_lines
from ouroboros.auto.seed_repairer import SeedRepairer
from ouroboros.auto.state import (
    DEFAULT_PIPELINE_TIMEOUT_SECONDS,
    MAX_PIPELINE_TIMEOUT_SECONDS,
    MIN_PIPELINE_TIMEOUT_SECONDS,
    AutoPhase,
    AutoPipelineState,
    AutoResumeCapability,
    AutoStore,
)
from ouroboros.config import get_opencode_mode
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler, StartExecuteSeedHandler
from ouroboros.mcp.tools.ralph_handlers import RalphHandler
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator import resolve_agent_runtime_backend


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
                        "be non-empty strings."
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
        try:
            result = await self._run(arguments)
        except Exception as exc:
            return Result.err(
                MCPToolError(f"Auto pipeline failed: {exc}", tool_name="ouroboros_auto")
            )
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
        supplied_user_preferences = (
            _parse_user_preferences(arguments.get("user_preferences"))
            if user_preferences_supplied
            else {}
        )
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
                state.user_preferences = dict(supplied_user_preferences)
        else:
            goal = arguments.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                raise ValueError("goal is required when not resuming")
            cwd = str(_resolve_cwd(arguments.get("cwd")))
            runtime_backend = resolve_agent_runtime_backend(self.agent_runtime_backend)
            opencode_mode = _resolved_opencode_mode(runtime_backend, self.opencode_mode)
            max_interview_rounds = _positive_int_arg(arguments, "max_interview_rounds", 12)
            max_repair_rounds = _positive_int_arg(arguments, "max_repair_rounds", 5)
            skip_run = requested_skip_run
            state = AutoPipelineState(goal=goal.strip(), cwd=cwd)
            state.user_preferences = dict(supplied_user_preferences)
            state.max_interview_rounds = max_interview_rounds
            state.max_repair_rounds = max_repair_rounds
            if pipeline_timeout_seconds is not None:
                state.pipeline_timeout_seconds = pipeline_timeout_seconds
        state.runtime_backend = runtime_backend
        state.opencode_mode = opencode_mode
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
        ralph_starter = (
            HandlerRalphStarter(
                RalphHandler(
                    agent_runtime_backend=runtime_backend,
                    opencode_mode=opencode_mode,
                )
            )
            if complete_product
            else None
        )
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
            complete_product=complete_product,
        )
        return await pipeline.run(state)


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


def _parse_user_preferences(value: object) -> dict[str, str]:
    """Validate and normalise the optional ``user_preferences`` MCP arg.

    Returns a dict keyed by ledger section names. Empty input yields an empty
    dict. Any unknown section name or empty/non-string value is rejected with
    ``ValueError`` so callers see a clear contract failure rather than a
    silently-ignored preference.
    """
    if value is None or value == "":
        return {}
    if not isinstance(value, dict):
        raise ValueError("user_preferences must be an object keyed by ledger section names")
    if not value:
        return {}
    valid_sections = frozenset(REQUIRED_SECTIONS)
    cleaned: dict[str, str] = {}
    for raw_key, raw_val in value.items():
        if not isinstance(raw_key, str):
            raise ValueError("user_preferences keys must be strings")
        if raw_key not in valid_sections:
            raise ValueError(
                f"user_preferences key '{raw_key}' is not a valid ledger section "
                f"(allowed: {', '.join(sorted(valid_sections))})"
            )
        if not isinstance(raw_val, str) or not raw_val.strip():
            raise ValueError(f"user_preferences['{raw_key}'] must be a non-empty string")
        cleaned[raw_key] = raw_val.strip()
    return cleaned


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
        )
    if _handler_matches_runtime(handler, agent_runtime_backend, opencode_mode):
        return handler
    return InterviewHandler(
        interview_engine=handler.interview_engine,
        event_store=handler.event_store,
        llm_adapter=handler.llm_adapter,
        llm_backend=llm_backend if llm_backend is not None else handler.llm_backend,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
        data_dir=handler.data_dir,
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
