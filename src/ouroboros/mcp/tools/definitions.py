"""Ouroboros tool definitions for MCP server.

This module re-exports all handler classes from their dedicated modules
and provides the :func:`get_ouroboros_tools` factory that assembles
the default handler tuple for MCP registration.


Handler modules:
- execution_handlers: ExecuteSeedHandler, StartExecuteSeedHandler
- query_handlers: SessionStatusHandler, QueryEventsHandler, ACDashboardHandler
- authoring_handlers: GenerateSeedHandler, InterviewHandler
- evaluation_handlers: MeasureDriftHandler, EvaluateHandler, LateralThinkHandler
- evolution_handlers: EvolveStepHandler, StartEvolveStepHandler,
                      EvolveRewindHandler, LineageStatusHandler
- job_handlers: CancelExecutionHandler, JobStatusHandler, JobWaitHandler,
                JobResultHandler, CancelJobHandler
- qa: QAHandler
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ouroboros.mcp.tools.ac_tree_hud_handler import ACTreeHUDHandler
from ouroboros.mcp.tools.authoring_handlers import (
    GenerateSeedHandler,
    InterviewHandler,
)
from ouroboros.mcp.tools.evaluation_handlers import (
    ChecklistVerifyHandler,
    EvaluateHandler,
    LateralThinkHandler,
    MeasureDriftHandler,
)
from ouroboros.mcp.tools.evolution_handlers import (
    EvolveRewindHandler,
    EvolveStepHandler,
    LineageStatusHandler,
    StartEvolveStepHandler,
)
from ouroboros.mcp.tools.execution_handlers import (
    ExecuteSeedHandler,
    StartExecuteSeedHandler,
)
from ouroboros.mcp.tools.job_handlers import (
    CancelExecutionHandler,
    CancelJobHandler,
    JobResultHandler,
    JobStatusHandler,
    JobWaitHandler,
)
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.mcp.tools.query_handlers import (
    ACDashboardHandler,  # noqa: F401 — re-exported for adapter.py
    QueryEventsHandler,
    SessionStatusHandler,
)

if TYPE_CHECKING:
    from ouroboros.orchestrator.agent_runtime_context import AgentRuntimeContext


def _resolve_bridge_fields(
    context: AgentRuntimeContext | None,
    mcp_manager: object | None,
    mcp_tool_prefix: str,
) -> tuple[object | None, str]:
    """Pick which (mcp_manager, mcp_tool_prefix) pair the factory should use.

    When ``context`` is provided and carries an ``mcp_bridge``, its bridge
    wins over the legacy explicit kwargs — that is the migration path
    captured by #474. When ``context`` is ``None`` (or carries no
    bridge), the legacy kwargs are returned unchanged so existing
    callers continue to work.
    """
    if context is not None and context.mcp_bridge is not None:
        bridge = context.mcp_bridge
        return getattr(bridge, "manager", None), getattr(bridge, "tool_prefix", "")
    return mcp_manager, mcp_tool_prefix


# ---------------------------------------------------------------------------
# Convenience factory functions
# ---------------------------------------------------------------------------


def execute_seed_handler(
    *,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
    mcp_manager: object | None = None,
    mcp_tool_prefix: str = "",
    opencode_mode: str | None = None,
    context: AgentRuntimeContext | None = None,
) -> ExecuteSeedHandler:
    """Create an ExecuteSeedHandler instance.

    When ``context`` is provided and carries an ``mcp_bridge``, the
    bridge supersedes the explicit ``mcp_manager`` / ``mcp_tool_prefix``
    kwargs. This is the migration path captured by #474; the legacy
    kwargs continue to work for callers that have not adopted
    :class:`AgentRuntimeContext`.
    """
    resolved_manager, resolved_prefix = _resolve_bridge_fields(
        context, mcp_manager, mcp_tool_prefix
    )
    return ExecuteSeedHandler(
        agent_runtime_backend=runtime_backend,
        llm_backend=llm_backend,
        mcp_manager=resolved_manager,
        mcp_tool_prefix=resolved_prefix,
        opencode_mode=opencode_mode,
    )


def start_execute_seed_handler(
    *,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
    mcp_manager: object | None = None,
    mcp_tool_prefix: str = "",
    opencode_mode: str | None = None,
    context: AgentRuntimeContext | None = None,
) -> StartExecuteSeedHandler:
    """Create a StartExecuteSeedHandler instance.

    Accepts the same ``context`` keyword as :func:`execute_seed_handler`;
    see that function's docstring for the migration semantics.
    """
    resolved_manager, resolved_prefix = _resolve_bridge_fields(
        context, mcp_manager, mcp_tool_prefix
    )
    execute_handler = ExecuteSeedHandler(
        agent_runtime_backend=runtime_backend,
        llm_backend=llm_backend,
        mcp_manager=resolved_manager,
        mcp_tool_prefix=resolved_prefix,
        opencode_mode=opencode_mode,
    )
    return StartExecuteSeedHandler(
        execute_handler=execute_handler,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


def session_status_handler() -> SessionStatusHandler:
    """Create a SessionStatusHandler instance."""
    return SessionStatusHandler()


def job_status_handler() -> JobStatusHandler:
    """Create a JobStatusHandler instance."""
    return JobStatusHandler()


def job_wait_handler() -> JobWaitHandler:
    """Create a JobWaitHandler instance."""
    return JobWaitHandler()


def job_result_handler() -> JobResultHandler:
    """Create a JobResultHandler instance."""
    return JobResultHandler()


def ac_tree_hud_handler() -> ACTreeHUDHandler:
    """Create an ACTreeHUDHandler instance."""
    return ACTreeHUDHandler()


def cancel_job_handler() -> CancelJobHandler:
    """Create a CancelJobHandler instance."""
    return CancelJobHandler()


def query_events_handler() -> QueryEventsHandler:
    """Create a QueryEventsHandler instance."""
    return QueryEventsHandler()


def generate_seed_handler(
    *,
    llm_backend: str | None = None,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> GenerateSeedHandler:
    """Create a GenerateSeedHandler instance."""
    return GenerateSeedHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


def measure_drift_handler() -> MeasureDriftHandler:
    """Create a MeasureDriftHandler instance."""
    return MeasureDriftHandler()


def interview_handler(
    *,
    llm_backend: str | None = None,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> InterviewHandler:
    """Create an InterviewHandler instance."""
    return InterviewHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


def lateral_think_handler(
    *,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> LateralThinkHandler:
    """Create a LateralThinkHandler instance."""
    return LateralThinkHandler(
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


def evaluate_handler(
    *,
    llm_backend: str | None = None,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> EvaluateHandler:
    """Create an EvaluateHandler instance."""
    return EvaluateHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


def checklist_verify_handler(
    *,
    evaluate_handler: EvaluateHandler | None = None,
    llm_backend: str | None = None,
) -> ChecklistVerifyHandler:
    """Create a ChecklistVerifyHandler instance."""
    return ChecklistVerifyHandler(
        evaluate_handler=evaluate_handler,
        llm_backend=llm_backend,
    )


def evolve_step_handler(
    *,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> EvolveStepHandler:
    """Create an EvolveStepHandler instance."""
    return EvolveStepHandler(
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


def start_evolve_step_handler(
    *,
    runtime_backend: str | None = None,
    opencode_mode: str | None = None,
) -> StartEvolveStepHandler:
    """Create a StartEvolveStepHandler instance."""
    return StartEvolveStepHandler(
        evolve_handler=EvolveStepHandler(
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
        ),
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )


def lineage_status_handler() -> LineageStatusHandler:
    """Create a LineageStatusHandler instance."""
    return LineageStatusHandler()


def evolve_rewind_handler() -> EvolveRewindHandler:
    """Create an EvolveRewindHandler instance."""
    return EvolveRewindHandler()


# ---------------------------------------------------------------------------
# Tool handler tuple type and factory
# ---------------------------------------------------------------------------
from ouroboros.mcp.tools.brownfield_handler import BrownfieldHandler  # noqa: E402
from ouroboros.mcp.tools.pm_handler import PMInterviewHandler  # noqa: E402

OuroborosToolHandlers = tuple[
    ExecuteSeedHandler
    | StartExecuteSeedHandler
    | SessionStatusHandler
    | JobStatusHandler
    | JobWaitHandler
    | JobResultHandler
    | ACTreeHUDHandler
    | CancelJobHandler
    | QueryEventsHandler
    | GenerateSeedHandler
    | MeasureDriftHandler
    | InterviewHandler
    | EvaluateHandler
    | ChecklistVerifyHandler
    | LateralThinkHandler
    | EvolveStepHandler
    | StartEvolveStepHandler
    | LineageStatusHandler
    | EvolveRewindHandler
    | CancelExecutionHandler
    | BrownfieldHandler
    | PMInterviewHandler
    | QAHandler,
    ...,
]


def get_ouroboros_tools(
    *,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
    mcp_manager: object | None = None,
    mcp_tool_prefix: str = "",
    opencode_mode: str | None = None,
    context: AgentRuntimeContext | None = None,
) -> OuroborosToolHandlers:
    """Create the default set of Ouroboros MCP tool handlers.

    ``opencode_mode`` is threaded into every handler that dispatches a
    ``_subagent`` envelope. When ``runtime_backend`` is an OpenCode variant
    AND ``opencode_mode`` is ``"plugin"`` the handler returns the envelope.
    In every other combination (including ``opencode_mode=None``) the handler
    falls through to its real in-process path. See
    ``ouroboros.mcp.tools.subagent.should_dispatch_via_plugin``.

    When ``context`` is provided and carries an ``mcp_bridge``, the
    bridge supersedes the explicit ``mcp_manager`` / ``mcp_tool_prefix``
    kwargs (see :func:`_resolve_bridge_fields`). This is the migration
    path captured by #474; legacy kwargs continue to work unchanged.
    """
    resolved_manager, resolved_prefix = _resolve_bridge_fields(
        context, mcp_manager, mcp_tool_prefix
    )
    execute_seed = ExecuteSeedHandler(
        agent_runtime_backend=runtime_backend,
        llm_backend=llm_backend,
        mcp_manager=resolved_manager,
        mcp_tool_prefix=resolved_prefix,
        opencode_mode=opencode_mode,
    )
    start_execute = StartExecuteSeedHandler(
        execute_handler=execute_seed,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )
    job_status = JobStatusHandler()
    job_wait = JobWaitHandler()
    job_result = JobResultHandler()
    interview = InterviewHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )
    generate_seed = GenerateSeedHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )
    evaluate = EvaluateHandler(
        llm_backend=llm_backend,
        agent_runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )
    return (
        execute_seed,
        start_execute,
        SessionStatusHandler(),
        job_status,
        job_wait,
        job_result,
        ACTreeHUDHandler(),
        CancelJobHandler(),
        QueryEventsHandler(),
        generate_seed,
        MeasureDriftHandler(),
        interview,
        evaluate,
        ChecklistVerifyHandler(evaluate_handler=evaluate, llm_backend=llm_backend),
        LateralThinkHandler(
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
        ),
        EvolveStepHandler(
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
        ),
        StartEvolveStepHandler(
            evolve_handler=EvolveStepHandler(
                agent_runtime_backend=runtime_backend,
                opencode_mode=opencode_mode,
            ),
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
        ),
        LineageStatusHandler(),
        EvolveRewindHandler(),
        CancelExecutionHandler(),
        BrownfieldHandler(),
        PMInterviewHandler(
            llm_backend=llm_backend,
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
        ),
        QAHandler(
            llm_backend=llm_backend,
            agent_runtime_backend=runtime_backend,
            opencode_mode=opencode_mode,
        ),
    )


# List of all Ouroboros tools for registration
OUROBOROS_TOOLS: OuroborosToolHandlers = get_ouroboros_tools()
