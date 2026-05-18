"""Ralph MCP tool handlers.

Provides ``ouroboros_ralph`` as a first-class background job so clients no
longer have to own the multi-generation loop in prompt/skill pseudo-code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
from typing import Any
from uuid import uuid4

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager
from ouroboros.mcp.tools.evolution_handlers import EvolveStepHandler
from ouroboros.mcp.tools.subagent import (
    build_ralph_subagent,
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
from ouroboros.orchestrator.agent_process import run_with_agent_process
from ouroboros.persistence.event_store import EventStore
from ouroboros.ralph_loop import (
    DEFAULT_GRADE_REGRESSION_WINDOW,
    DEFAULT_OSCILLATION_WINDOW,
    DEFAULT_PER_ITERATION_TIMEOUT_SECONDS,
    EvolveStepLike,
    RalphLoopConfig,
    RalphLoopRunner,
)

MAX_RALPH_GENERATIONS = 10
MIN_PER_ITERATION_TIMEOUT_SECONDS = 30.0
MAX_PER_ITERATION_TIMEOUT_SECONDS = 7200.0
MIN_MAX_TOTAL_SECONDS = 1.0
MAX_MAX_TOTAL_SECONDS = 86400.0
MIN_PROGRESS_WINDOW = 2  # smallest window where strict-decrease / repeat checks are meaningful
MIN_MAX_TOTAL_SECONDS = 1.0
MAX_MAX_TOTAL_SECONDS = 86400.0

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


@dataclass
class RalphHandler:
    """Start a runtime-owned Ralph loop as a background job."""

    evolve_handler: EvolveStepLike | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)
        self._evolve_handler = self.evolve_handler or EvolveStepHandler(
            agent_runtime_backend=self.agent_runtime_backend,
            opencode_mode=self.opencode_mode,
        )

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the public MCP definition."""
        return MCPToolDefinition(
            name="ouroboros_ralph",
            description=(
                "Start a first-class Ralph loop in the background. The loop repeatedly "
                "runs evolve_step until QA passes, convergence is reached, a terminal "
                "evolution action occurs, cancellation is requested, or max_generations "
                "is reached. In non-plugin runtimes, returns a job_id immediately for "
                "ouroboros_job_status, ouroboros_job_wait, ouroboros_job_result, and "
                "ouroboros_cancel_job. In OpenCode plugin mode, returns job_id=None and "
                "delegates the loop to the plugin child session."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="Lineage ID to start or continue.",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Seed YAML content for generation 1. Omit for continuation.",
                    required=False,
                ),
                MCPToolParameter(
                    name="execute",
                    type=ToolInputType.BOOLEAN,
                    description="Whether each generation should execute and evaluate. Default: true.",
                    required=False,
                    default=True,
                ),
                MCPToolParameter(
                    name="parallel",
                    type=ToolInputType.BOOLEAN,
                    description="Whether each generation may execute ACs in parallel. Default: true.",
                    required=False,
                    default=True,
                ),
                MCPToolParameter(
                    name="skip_qa",
                    type=ToolInputType.BOOLEAN,
                    description="Skip post-execution QA. Default: false.",
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="project_dir",
                    type=ToolInputType.STRING,
                    description="Project root forwarded to each evolve_step generation.",
                    required=False,
                ),
                MCPToolParameter(
                    name="max_generations",
                    type=ToolInputType.INTEGER,
                    description="Maximum generations to run before stopping. Default: 10. Range: 1-10.",
                    required=False,
                    default=MAX_RALPH_GENERATIONS,
                ),
                MCPToolParameter(
                    name="per_iteration_timeout_seconds",
                    type=ToolInputType.NUMBER,
                    description=(
                        "Per-iteration wall-clock bound in seconds. In-process "
                        "runtime: hard-enforced via asyncio.timeout, the loop "
                        "stops with stop_reason='iteration_timeout' on expiry. "
                        "OpenCode plugin runtime: advisory bound advertised to "
                        "the child session via prompt + subagent context — the "
                        "child is expected to honor it and return "
                        "stop_reason='iteration_timeout', but the parent MCP "
                        "process cannot interrupt the child, so a non-conforming "
                        "child session may still exceed this bound. "
                        "Default: 1800. Range: 30-7200."
                    ),
                    required=False,
                    default=DEFAULT_PER_ITERATION_TIMEOUT_SECONDS,
                ),
                MCPToolParameter(
                    name="oscillation_window",
                    type=ToolInputType.INTEGER,
                    description=(
                        "Number of trailing iterations whose findings_hash must "
                        "match (and QA must not have passed) to stop with "
                        "stop_reason='oscillation_detected'. Default: 3. "
                        f"Range: {MIN_PROGRESS_WINDOW}-{MAX_RALPH_GENERATIONS}. "
                        "Values < 2 are rejected because a single iteration "
                        "cannot oscillate with itself."
                    ),
                    required=False,
                    default=DEFAULT_OSCILLATION_WINDOW,
                ),
                MCPToolParameter(
                    name="grade_regression_window",
                    type=ToolInputType.INTEGER,
                    description=(
                        "Number of trailing iterations whose non-None grades must "
                        "strictly decrease to stop with "
                        "stop_reason='grade_regressing'. Default: 2. "
                        f"Range: {MIN_PROGRESS_WINDOW}-{MAX_RALPH_GENERATIONS}. "
                        "Values < 2 are rejected because strict-decrease "
                        "requires at least two grades to compare."
                    ),
                    required=False,
                    default=DEFAULT_GRADE_REGRESSION_WINDOW,
                ),
                MCPToolParameter(
                    name="max_total_seconds",
                    type=ToolInputType.NUMBER,
                    description=(
                        "Total wall-clock budget for the entire Ralph loop "
                        "in seconds. In the in-process runner this is "
                        "enforced by RalphLoopRunner: checked at the top of "
                        "every iteration BEFORE launching evolve_step, and "
                        "on exhaustion the loop stops with "
                        "stop_reason='wall_clock_exhausted'. In OpenCode "
                        "plugin mode the bound is forwarded to the child "
                        "session and the plugin is expected to self-enforce; "
                        "the MCP server cannot abort a foreign child "
                        "process. When omitted, a derived ceiling of "
                        "max_generations * per_iteration_timeout_seconds is "
                        "auto-applied (with a WARNING log) for standalone "
                        "callers. Range: 1-86400."
                    ),
                    required=False,
                ),
            ),
        )

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        """Start the Ralph loop job and return a job handle immediately."""
        lineage_id = _normalize_lineage_id(arguments.get("lineage_id"))
        if not lineage_id:
            text = (
                "Ralph needs structured lineage input before it can start.\n\n"
                "For an existing Ralph lineage, invoke `ooo ralph --lineage-id <lineage_id>`.\n"
                "For a plain natural-language request, run `ooo interview` and `ooo seed` "
                "first, then call `ouroboros_ralph` with a fresh lineage_id and the "
                "validated Seed YAML as seed_content."
            )
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                    is_error=True,
                    meta={
                        "status": "input_required",
                        "missing": ["lineage_id"],
                        "next_step": "interview_seed_or_lineage_id",
                    },
                )
            )

        try:
            max_generations = int(arguments.get("max_generations", MAX_RALPH_GENERATIONS))
        except (TypeError, ValueError):
            return Result.err(
                MCPToolError("max_generations must be an integer", tool_name="ouroboros_ralph")
            )
        if max_generations < 1 or max_generations > MAX_RALPH_GENERATIONS:
            return Result.err(
                MCPToolError(
                    f"max_generations must be between 1 and {MAX_RALPH_GENERATIONS}",
                    tool_name="ouroboros_ralph",
                )
            )

        raw_timeout = arguments.get(
            "per_iteration_timeout_seconds",
            DEFAULT_PER_ITERATION_TIMEOUT_SECONDS,
        )
        try:
            per_iteration_timeout_seconds = float(raw_timeout)
        except (TypeError, ValueError):
            return Result.err(
                MCPToolError(
                    "per_iteration_timeout_seconds must be a number",
                    tool_name="ouroboros_ralph",
                )
            )
        if not math.isfinite(per_iteration_timeout_seconds):
            # Reject NaN / +inf / -inf: range comparisons are always False for
            # NaN and asyncio.wait_for(timeout=inf) defeats the bounded-loop
            # contract the public API advertises.
            return Result.err(
                MCPToolError(
                    "per_iteration_timeout_seconds must be a finite number",
                    tool_name="ouroboros_ralph",
                )
            )
        if (
            per_iteration_timeout_seconds < MIN_PER_ITERATION_TIMEOUT_SECONDS
            or per_iteration_timeout_seconds > MAX_PER_ITERATION_TIMEOUT_SECONDS
        ):
            return Result.err(
                MCPToolError(
                    "per_iteration_timeout_seconds must be between "
                    f"{MIN_PER_ITERATION_TIMEOUT_SECONDS:g} and "
                    f"{MAX_PER_ITERATION_TIMEOUT_SECONDS:g}",
                    tool_name="ouroboros_ralph",
                )
            )

        oscillation_window_result = _coerce_window(
            arguments.get("oscillation_window", DEFAULT_OSCILLATION_WINDOW),
            field_name="oscillation_window",
        )
        if isinstance(oscillation_window_result, MCPToolError):
            return Result.err(oscillation_window_result)
        oscillation_window = oscillation_window_result
        if oscillation_window < MIN_PROGRESS_WINDOW or oscillation_window > MAX_RALPH_GENERATIONS:
            return Result.err(
                MCPToolError(
                    "oscillation_window must be between "
                    f"{MIN_PROGRESS_WINDOW} and {MAX_RALPH_GENERATIONS}",
                    tool_name="ouroboros_ralph",
                )
            )

        grade_regression_window_result = _coerce_window(
            arguments.get("grade_regression_window", DEFAULT_GRADE_REGRESSION_WINDOW),
            field_name="grade_regression_window",
        )
        if isinstance(grade_regression_window_result, MCPToolError):
            return Result.err(grade_regression_window_result)
        grade_regression_window = grade_regression_window_result
        if (
            grade_regression_window < MIN_PROGRESS_WINDOW
            or grade_regression_window > MAX_RALPH_GENERATIONS
        ):
            return Result.err(
                MCPToolError(
                    "grade_regression_window must be between "
                    f"{MIN_PROGRESS_WINDOW} and {MAX_RALPH_GENERATIONS}",
                    tool_name="ouroboros_ralph",
                )
            )

        raw_max_total_seconds = arguments.get("max_total_seconds")
        max_total_seconds: float | None
        if raw_max_total_seconds is None:
            max_total_seconds = None
        else:
            try:
                max_total_seconds = float(raw_max_total_seconds)
            except (TypeError, ValueError):
                return Result.err(
                    MCPToolError(
                        "max_total_seconds must be a number",
                        tool_name="ouroboros_ralph",
                    )
                )
            if (
                not math.isfinite(max_total_seconds)
                or max_total_seconds < MIN_MAX_TOTAL_SECONDS
                or max_total_seconds > MAX_MAX_TOTAL_SECONDS
            ):
                return Result.err(
                    MCPToolError(
                        "max_total_seconds must be between "
                        f"{MIN_MAX_TOTAL_SECONDS:g} and "
                        f"{MAX_MAX_TOTAL_SECONDS:g}",
                        tool_name="ouroboros_ralph",
                    )
                )

        if max_total_seconds is None:
            derived = float(max_generations) * per_iteration_timeout_seconds
            logger.warning(
                "max_total_seconds not provided; auto-applying derived ceiling "
                "of %ss based on max_generations × per_iteration_timeout_seconds",
                f"{derived:g}",
            )
            max_total_seconds = derived

        if arguments.get("delegation_depth", 0):
            return Result.err(
                MCPToolError(
                    "nested ouroboros_ralph delegation is not allowed",
                    tool_name="ouroboros_ralph",
                )
            )

        config = RalphLoopConfig(
            lineage_id=lineage_id,
            seed_content=arguments.get("seed_content"),
            execute=bool(arguments.get("execute", True)),
            parallel=bool(arguments.get("parallel", True)),
            skip_qa=bool(arguments.get("skip_qa", False)),
            project_dir=arguments.get("project_dir"),
            max_generations=max_generations,
            per_iteration_timeout_seconds=per_iteration_timeout_seconds,
            max_total_seconds=max_total_seconds,
            oscillation_window=oscillation_window,
            grade_regression_window=grade_regression_window,
        )

        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            # Plugin mode: per_iteration_timeout_seconds and max_total_seconds
            # are forwarded to the child session as instructions. The MCP
            # server cannot enforce them server-side because execution lives
            # in a foreign OpenCode child process this server does not own;
            # the in-process RalphLoopRunner is the only path with hard
            # enforcement. See build_ralph_subagent docstring (#789 review-2).
            payload = build_ralph_subagent(
                lineage_id=config.lineage_id,
                seed_content=config.seed_content,
                execute=config.execute,
                parallel=config.parallel,
                skip_qa=config.skip_qa,
                project_dir=config.project_dir,
                max_generations=config.max_generations,
                per_iteration_timeout_seconds=config.per_iteration_timeout_seconds,
                max_total_seconds=config.max_total_seconds,
                oscillation_window=config.oscillation_window,
                grade_regression_window=config.grade_regression_window,
            )
            await self._event_store.initialize()
            await emit_subagent_dispatched_event(
                self._event_store,
                session_id=config.lineage_id,
                payload=payload,
            )
            return build_subagent_result(
                payload,
                response_shape={
                    "job_id": None,
                    "lineage_id": config.lineage_id,
                    "status": "delegated_to_plugin",
                    "dispatch_mode": "plugin",
                    "max_generations": config.max_generations,
                },
            )

        runner = RalphLoopRunner(self._evolve_handler)

        async def _run_loop(handle) -> MCPToolResult:
            if handle.should_cancel():
                return MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text="Ralph loop cancelled before restart work began.",
                        ),
                    ),
                    is_error=True,
                    meta={"status": "cancelled"},
                )
            result = await runner.run(config)
            return result.to_tool_result()

        job_id = await self._job_manager.allocate_job_id()
        agent_process_id = f"ralph:{config.lineage_id}:{uuid4().hex[:12]}"
        agent_cancel_key = f"mcp_job:{job_id}"

        snapshot = await self._job_manager.start_job(
            job_type="ralph",
            initial_message=f"Queued Ralph loop for {config.lineage_id}",
            runner=run_with_agent_process(
                event_store=self._event_store,
                intent="ralph",
                work_fn=_run_loop,
                process_id=agent_process_id,
                cancel_key=agent_cancel_key,
            ),
            links=JobLinks(lineage_id=config.lineage_id),
            job_id=job_id,
        )

        text = (
            "Started background Ralph loop.\n\n"
            f"Job ID: {snapshot.job_id}\n"
            f"Lineage ID: {config.lineage_id}\n"
            f"Max generations: {config.max_generations}\n\n"
            "Use ouroboros_job_status, ouroboros_job_wait, ouroboros_job_result, "
            "or ouroboros_cancel_job to monitor it."
        )
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "job_id": snapshot.job_id,
                    "lineage_id": config.lineage_id,
                    "status": snapshot.status.value,
                    "cursor": snapshot.cursor,
                    "max_generations": config.max_generations,
                },
            )
        )


@dataclass
class StartRalphHandler(RalphHandler):
    """Compatibility fire-and-forget alias for the runtime-owned Ralph job surface."""

    @property
    def definition(self) -> MCPToolDefinition:
        base = super().definition
        return MCPToolDefinition(
            name="ouroboros_start_ralph",
            description=(
                "Fire-and-forget alias for ouroboros_ralph. Starts the same "
                "runtime-owned Ralph loop. In non-plugin runtimes, returns a "
                "job_id immediately for ouroboros_job_status, ouroboros_job_wait, "
                "ouroboros_job_result, and ouroboros_cancel_job. In OpenCode "
                "plugin mode, delegates to a plugin child session and returns "
                "job_id=None with status='delegated_to_plugin'; results are not "
                "pollable via job_status/job_result."
            ),
            parameters=base.parameters,
        )


def _normalize_lineage_id(value: Any) -> str:
    """Normalize user-provided lineage IDs before starting a mutating Ralph loop."""
    return value.strip() if isinstance(value, str) else ""


def _coerce_window(value: Any, *, field_name: str) -> int | MCPToolError:
    """Strictly coerce an MCP integer field, refusing fractional float truncation.

    The MCP parameter is declared ``INTEGER``. ``int(2.9)`` would silently
    truncate to ``2``, changing loop-stop semantics behind the caller's back,
    so reject any float whose value is not exactly integral. Booleans flow
    through ``int(True) == 1`` and remain handled by the downstream range
    check (``True``/``False`` end up as 1/0, both below the floor).
    """
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return MCPToolError(
            f"{field_name} must be an integer",
            tool_name="ouroboros_ralph",
        )
    # ``isinstance(bool, int)`` is True, but bool truncation is harmless here.
    if isinstance(value, float) and coerced != value:
        return MCPToolError(
            f"{field_name} must be an integer (got fractional value)",
            tool_name="ouroboros_ralph",
        )
    return coerced
